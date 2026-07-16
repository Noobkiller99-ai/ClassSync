from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from itertools import groupby
from pathlib import Path

logger = logging.getLogger(__name__)

# Load .env if present (must happen before Flask reads os.environ)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)  # existing env vars take precedence
except ImportError:
    pass

# NOTE: For local HTTP development set OAUTHLIB_INSECURE_TRANSPORT=1 in .env
# Do NOT set it here — on Render the app runs over HTTPS via ProxyFix.

from flask import Flask, flash, redirect, render_template, request, session, url_for, has_request_context
from werkzeug.middleware.proxy_fix import ProxyFix

from .google_calendar import GoogleCalendarClient
from .security import decrypt_json, encrypt_json
from .store import (
    clear_events,
    clear_mandatory_sessions,
    database_path,
    delete_setting,
    get_all_user_tokens,
    get_mandatory_sessions,
    get_setting,
    init_db,
    list_event_payloads,
    mark_many_synced,
    save_events,
    save_mandatory_sessions,
    set_setting,
)
from .tcs import (
    TcsClient,
    TcsError,
    apply_mandatory_flags,
    next_two_weeks,
    parse_tcs_attendance,
    serialize_events,
)


def create_app(test_config: dict | None = None) -> Flask:
    # True when running locally (not on Render or Vercel, or any cloud with RENDER or VERCEL env set)
    IS_LOCAL = not (os.getenv("RENDER") or os.getenv("VERCEL"))

    app = Flask(__name__, instance_relative_config=True)
    # Respect X-Forwarded-Proto from Render/any reverse proxy so OAuth
    # callback URLs are built with https:// in production.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=str(database_path(app.instance_path)),
        SAMPLE_ATTENDANCE_PATH=str(Path.cwd() / "scripts" / "attendance_sample.json"),
        USE_SAMPLE_TCS=os.getenv("TCS_SAMPLE_MODE") == "1",
        SYNC_WINDOW_NOW=None,
        ADMIN_TOKEN=os.getenv("ADMIN_TOKEN", ""),
        GOOGLE_CONFIGURED=bool(os.getenv("GOOGLE_CLIENT_ID")),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    init_db(app.config["DATABASE"])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _user_token() -> str:
        """Return (or create) a stable anonymous token for the current browser session."""
        if "user_token" not in session:
            session["user_token"] = str(uuid.uuid4())
        return session["user_token"]

    # ── Health check (used by Render) ────────────────────────────────────────

    @app.get("/health")
    def health():
        from flask import jsonify
        return jsonify({"status": "ok"})

    @app.get("/google8cee1b0974b32ae6.html")
    def google_verification():
        return "google-site-verification: google8cee1b0974b32ae6.html"


    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        tok = _user_token()
        db = app.config["DATABASE"]
        events_raw: list[dict] = get_setting(db, tok, "preview_events", [])  # type: ignore[assignment]
        google_ready = bool(get_setting(db, tok, "google_credentials", None))
        tcs_ready = bool(session.get("tcs_credentials_encrypted"))
        batch = _get_user_batch(app, tok)
        mandatory_data = get_mandatory_sessions(db, batch)
        synced = bool(
            google_ready
            and any(e.get("synced_event_id") for e in list_event_payloads(db, tok))
        )
        # Derive display name from stored email
        username = ""
        if tcs_ready:
            creds = _stored_tcs_credentials(app, tok)
            if creds:
                raw = creds.get("username", "").split("@")[0]
                username = raw.replace(".", " ").title()

        # Derive Google email from stored credentials
        google_email = ""
        if google_ready:
            g_creds = get_setting(db, tok, "google_credentials", None)
            if isinstance(g_creds, dict):
                google_email = g_creds.get("email", "")

        event_groups = _group_events(events_raw)
        return render_template(
            "index.html",
            events=events_raw,
            google_ready=google_ready,
            google_configured=app.config["GOOGLE_CONFIGURED"],
            tcs_ready=tcs_ready,
            mandatory_data=mandatory_data,
            is_local=IS_LOCAL,
            event_groups=event_groups,
            synced=synced,
            username=username,
            batch=batch,
            sample_mode=app.config["USE_SAMPLE_TCS"],
            admin_enabled=bool(app.config["ADMIN_TOKEN"]),
            google_email=google_email,
        )

    @app.post("/tcs/login")
    def tcs_login():
        tok = _user_token()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Please enter your TCS iON email and password.", "error")
            return redirect(url_for("index"))
        if not username.lower().endswith("@spjimr.org"):
            flash("Class Sync is limited to SPJIMR student accounts (@spjimr.org).", "error")
            return redirect(url_for("index"))
        credentials = {"username": username, "password": password}
        try:
            events = _fetch_timetable(app, credentials, tok)
        except TcsError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        db = app.config["DATABASE"]
        session["tcs_credentials_encrypted"] = encrypt_json(credentials)
        batch = _extract_batch_from_email(username)
        set_setting(db, tok, "batch", batch)
        set_setting(db, tok, "preview_events", serialize_events(events))
        save_events(db, tok, events)
        flash(
            f"TCS iON connected — {len(events)} events loaded for the next 2 weeks.",
            "success",
        )
        return redirect(url_for("index"))

    @app.post("/tcs/reset")
    def tcs_reset():
        tok = _user_token()
        db = app.config["DATABASE"]
        session.pop("tcs_credentials_encrypted", None)
        delete_setting(db, tok, "batch")
        delete_setting(db, tok, "preview_events")
        delete_setting(db, tok, "google_credentials")
        clear_events(db, tok)
        flash("Session cleared. Enter your TCS iON credentials to start over.", "info")
        return redirect(url_for("index"))

    @app.post("/wisenet/upload")
    def wisenet_upload():
        """
        Receives uploaded Course Outline PDFs, extracts their course codes,
        parses the mandatory sessions, and saves them centrally in the database by batch.
        """
        from .wisenet import parse_mandatory_sessions_from_pdf
        
        tok = _user_token()
        db = app.config["DATABASE"]
        batch = _get_user_batch(app, tok)
        
        uploaded_files = request.files.getlist("pdf_files")
        if not uploaded_files or not uploaded_files[0].filename:
            flash("No files selected.", "error")
            return redirect(url_for("index"))
            
        success_count = 0
        skipped_count = 0
        error_count = 0
        
        for file in uploaded_files:
            filename = file.filename or "unknown.pdf"
            if not filename.lower().endswith(".pdf"):
                skipped_count += 1
                continue
                
            try:
                pdf_bytes = file.read()
                course_code = _extract_course_code(filename, pdf_bytes)
                if not course_code:
                    logger.warning("Could not determine course code for uploaded file: %s", filename)
                    error_count += 1
                    continue
                
                session_info = parse_mandatory_sessions_from_pdf(pdf_bytes, course_code)
                if session_info:
                    current_sessions = get_mandatory_sessions(db, batch)
                    existing = current_sessions.get(course_code, [])
                    if not getattr(session_info, "all_sessions", None):
                        merged = session_info.mandatory_sessions
                    else:
                        covered_set = set(session_info.all_sessions)
                        mandatory_set = set(session_info.mandatory_sessions)
                        keep_existing = [s for s in existing if s not in covered_set]
                        merged = sorted(list(set(keep_existing + list(mandatory_set))))
                    
                    current_sessions[course_code] = merged
                    save_mandatory_sessions(db, batch, current_sessions)
                    success_count += 1
                    
            except Exception as e:
                logger.error("Error processing uploaded PDF %s: %s", filename, e)
                error_count += 1
                
        if success_count > 0:
            mandatory_data = get_mandatory_sessions(db, batch)
            from .store import reapply_mandatory_flags_to_batch
            reapply_mandatory_flags_to_batch(db, batch, mandatory_data)
            flash(
                f"Successfully processed {success_count} course outline(s)! "
                f"Mandatory sessions are now marked in red.",
                "success"
            )
        else:
            flash("Could not parse any mandatory sessions from the uploaded files.", "error")
            
        return redirect(url_for("index"))

    @app.post("/preview")
    @app.get("/preview")
    def preview():
        tok = _user_token()
        credentials = _stored_tcs_credentials(app, tok)
        if not credentials:
            flash("Add your TCS iON credentials first.", "error")
            return redirect(url_for("index"))
        try:
            events = _fetch_timetable(app, credentials, tok)
        except TcsError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        db = app.config["DATABASE"]
        set_setting(db, tok, "preview_events", serialize_events(events))
        save_events(db, tok, events)
        flash(f"Timetable refreshed — {len(events)} events for the next 2 weeks.", "success")
        return redirect(url_for("index"))

    @app.post("/sync")
    def sync():
        tok = _user_token()
        db = app.config["DATABASE"]
        if not get_setting(db, tok, "preview_events", []):
            flash("Fetch your TCS iON timetable first.", "error")
            return redirect(url_for("index"))
        if not app.config["GOOGLE_CONFIGURED"]:
            flash(
                "Google Calendar is not configured on this server. "
                "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env and restart.",
                "warning",
            )
            return redirect(url_for("index"))
        stored_creds = get_setting(db, tok, "google_credentials", None)
        if _google_credentials_valid(stored_creds):
            # Credentials already stored and valid — sync directly without re-authenticating.
            try:
                result = _sync_calendar(app, tok)
                flash(f"Synced! {result.imported} events added to your Google Calendar.", "success")
            except Exception as exc:
                flash(f"Calendar sync failed: {exc}", "error")
            return redirect(url_for("index"))
        # No valid credentials yet — start the OAuth flow.
        session["post_google_redirect"] = "sync"
        return redirect(url_for("google_login"))

    @app.get("/google/login")
    def google_login():
        tok = _user_token()
        db = app.config["DATABASE"]
        # If the user already had credentials, this is a re-auth — use the
        # simplified prompt so they just click their saved account.
        existing_creds = get_setting(db, tok, "google_credentials", None)
        is_reauth = _google_credentials_valid(existing_creds)
        client = GoogleCalendarClient()
        auth_url, state = client.authorization_url_with_state(reauth=is_reauth)
        if state:
            session["google_oauth_state"] = state
        return redirect(auth_url)

    @app.get("/google/callback")
    def google_callback():
        tok = _user_token()
        # Honour explicit dry_run param (used in tests / when no GOOGLE_CLIENT_ID)
        is_dry_run = request.args.get("dry_run") == "1"
        client = GoogleCalendarClient(dry_run=is_dry_run or None)
        saved_state = session.pop("google_oauth_state", None)
        try:
            token = client.fetch_token(request.url, state=saved_state)
        except Exception as exc:
            flash(f"Google sign-in failed: {exc}", "error")
            return redirect(url_for("index"))
        db = app.config["DATABASE"]
        set_setting(db, tok, "google_credentials", token)
        flash("Google Calendar connected.", "success")
        if session.pop("post_google_redirect", None) == "sync":
            try:
                result = _sync_calendar(app, tok)
                flash(f"Done! {result.imported} events synced to your Google Calendar.", "success")
            except Exception as exc:
                flash(f"Calendar sync failed: {exc}", "error")
        return redirect(url_for("index"))

    @app.post("/admin/refresh")
    def admin_refresh():
        tok = _user_token()
        if app.config["ADMIN_TOKEN"]:
            supplied = (
                request.form.get("admin_token", "")
                or request.headers.get("X-Admin-Token", "")
            )
            if supplied != app.config["ADMIN_TOKEN"]:
                flash("Invalid admin token.", "error")
                return redirect(url_for("index"))
        credentials = _stored_tcs_credentials(app, tok)
        if not credentials:
            flash("Cannot refresh until TCS credentials are saved.", "error")
            return redirect(url_for("index"))
        db = app.config["DATABASE"]
        events = _fetch_timetable(app, credentials, tok)
        set_setting(db, tok, "preview_events", serialize_events(events))
        save_events(db, tok, events)
        result = _sync_calendar(app, tok)
        flash(
            f"Admin refresh complete — {len(events)} fetched, {result.imported} synced.",
            "success",
        )
        return redirect(url_for("index"))

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/terms")
    def terms():
        return render_template("terms.html")

    return app


# ── Private helpers ────────────────────────────────────────────────────────────

def _fetch_timetable(app: Flask, credentials: dict, user_token: str | None = None) -> list:
    if app.config.get("USE_SAMPLE_TCS", False):
        sample_path = Path(app.config["SAMPLE_ATTENDANCE_PATH"])
        if not sample_path.exists():
            raise TcsError("No local TCS sample file found.")
        events = next_two_weeks(
            parse_tcs_attendance(sample_path.read_text(encoding="utf-8")),
            now=_sync_window_now(app),
        )
    else:
        now = _sync_window_now(app)
        client = TcsClient()
        events = next_two_weeks(
            client.fetch_timetable(credentials["username"], credentials["password"], now=now),
            now=now,
        )
    # Apply mandatory flags if central Wisenet data is available
    batch = _extract_batch_from_email(credentials.get("username", ""))
    mandatory_data = get_mandatory_sessions(app.config["DATABASE"], batch)
    if mandatory_data:
        events = apply_mandatory_flags(events, mandatory_data)
    return events


def _sync_calendar(app: Flask, user_token: str):
    db = app.config["DATABASE"]
    credentials = get_setting(db, user_token, "google_credentials", None)
    client = GoogleCalendarClient(
        credentials=credentials,
        dry_run=bool(credentials and credentials.get("dry_run")) or app.config.get("TESTING", False),
    )
    payloads = list_event_payloads(db, user_token)
    result = client.sync(payloads)
    mark_many_synced(db, user_token, result.event_ids)
    return result


def _stored_tcs_credentials(app: Flask, user_token: str | None = None) -> dict | None:
    if not has_request_context():
        return None
    if user_token and user_token != session.get("user_token"):
        return None
    encrypted = session.get("tcs_credentials_encrypted")
    return decrypt_json(encrypted) if encrypted else None


def _sync_window_now(app: Flask) -> datetime | None:
    configured = app.config.get("SYNC_WINDOW_NOW")
    if isinstance(configured, str):
        return datetime.fromisoformat(configured)
    return configured


def _group_events(events: list[dict]) -> list[dict]:
    """Group a flat list of serialised events into day buckets for the template."""
    from datetime import timedelta
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    sorted_events = sorted(events, key=lambda e: e.get("starts_at", ""))
    groups: list[dict] = []
    for date_str, day_iter in groupby(
        sorted_events, key=lambda e: e.get("starts_at", "")[:10]
    ):
        try:
            d = datetime.fromisoformat(date_str).date()
            is_today = d == today
            if is_today:
                label = f"Today · {d.strftime('%A, %b %d')}"
            elif d == tomorrow:
                label = f"Tomorrow · {d.strftime('%A, %b %d')}"
            else:
                label = d.strftime("%A, %b %d")
        except Exception:
            is_today = False
            label = date_str
        groups.append(
            {
                "date": date_str,
                "label": label,
                "is_today": is_today,
                "events": list(day_iter),
            }
        )
    return groups


def _extract_course_code(filename: str, pdf_bytes: bytes) -> str | None:
    import re
    # 1. Search filename for patterns like "FIN521"
    match = re.search(r'\b([A-Z]{3,4}\s*\d{3})\b', filename.upper())
    if match:
        return match.group(1).replace(" ", "").upper()
    
    # 2. Extract first page text from PDF bytes using pdfplumber
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if pdf.pages:
                first_page_text = pdf.pages[0].extract_text() or ""
                # Look for codes like FIN 521 or FIN521
                match = re.search(r'\b([A-Z]{3,4})\s*(\d{3})\b', first_page_text.upper())
                if match:
                    return f"{match.group(1)}{match.group(2)}".upper()
    except Exception as e:
        logger.warning("Failed to extract course code from PDF text: %s", e)
    
    return None


def _extract_batch_from_email(email: str) -> str:
    if not email:
        return "general"
    username = email.split("@")[0].strip().lower()
    parts = [p.strip() for p in username.split(".") if p.strip()]
    if parts:
        if len(parts) == 1:
            import re
            if re.match(r'^(pgp|pgdm|pgpm|gmp|fmb|epgp)\d+', username):
                return username
            return "general"
        else:
            return parts[0]
    return "general"


def _get_user_batch(app: Flask, user_token: str) -> str:
    db = app.config["DATABASE"]
    batch = get_setting(db, user_token, "batch", None)
    if batch:
        return str(batch)
    creds = _stored_tcs_credentials(app, user_token)
    if creds:
        return _extract_batch_from_email(creds.get("username", ""))
    return "general"


def _google_credentials_valid(credentials: object) -> bool:
    """Return True if *credentials* look usable without a fresh OAuth round-trip.

    A credential dict is valid as long as it has a refresh_token; the Google
    client library will transparently refresh the access token as needed.
    """
    if not isinstance(credentials, dict):
        return False
    if credentials.get("dry_run"):
        return True
    return bool(credentials.get("refresh_token"))


# Public aliases used by tests
fetch_timetable = _fetch_timetable
sync_calendar = _sync_calendar
stored_tcs_credentials = _stored_tcs_credentials
sync_window_now = _sync_window_now
extract_batch_from_email = _extract_batch_from_email
get_user_batch = _get_user_batch
google_credentials_valid = _google_credentials_valid
