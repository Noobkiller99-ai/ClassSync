from __future__ import annotations

import os
import uuid
from datetime import datetime
from itertools import groupby
from pathlib import Path

# Load .env if present (must happen before Flask reads os.environ)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)  # existing env vars take precedence
except ImportError:
    pass

# NOTE: For local HTTP development set OAUTHLIB_INSECURE_TRANSPORT=1 in .env
# Do NOT set it here — on Render the app runs over HTTPS via ProxyFix.

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .google_calendar import GoogleCalendarClient
from .scheduler import WeeklyScheduler
from .security import decrypt_json, encrypt_json
from .store import (
    clear_events,
    clear_mandatory_sessions,
    database_path,
    delete_setting,
    get_all_users_with_credentials,
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

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        tok = _user_token()
        db = app.config["DATABASE"]
        events_raw: list[dict] = get_setting(db, tok, "preview_events", [])  # type: ignore[assignment]
        google_ready = bool(get_setting(db, tok, "google_credentials", None))
        tcs_ready = bool(get_setting(db, tok, "tcs_credentials_encrypted", None))
        wisenet_ready = bool(get_setting(db, tok, "wisenet_cookies", None))
        mandatory_data = get_mandatory_sessions(db, tok) if wisenet_ready else {}
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

        event_groups = _group_events(events_raw)
        return render_template(
            "index.html",
            events=events_raw,
            event_groups=event_groups,
            google_ready=google_ready,
            google_configured=app.config["GOOGLE_CONFIGURED"],
            tcs_ready=tcs_ready,
            wisenet_ready=wisenet_ready,
            mandatory_data=mandatory_data,
            synced=synced,
            username=username,
            sample_mode=app.config["USE_SAMPLE_TCS"],
            admin_enabled=bool(app.config["ADMIN_TOKEN"]),
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
        set_setting(db, tok, "tcs_credentials_encrypted", encrypt_json(credentials))
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
        delete_setting(db, tok, "tcs_credentials_encrypted")
        delete_setting(db, tok, "preview_events")
        delete_setting(db, tok, "google_credentials")
        delete_setting(db, tok, "wisenet_credentials_encrypted")  # legacy
        delete_setting(db, tok, "wisenet_cookies")
        clear_events(db, tok)
        clear_mandatory_sessions(db, tok)
        flash("Session cleared. Enter your TCS iON credentials to start over.", "info")
        return redirect(url_for("index"))

    # ── Wisenet (Moodle LMS) routes ───────────────────────────────────────────

    @app.post("/wisenet/connect")
    def wisenet_connect():
        """
        Launch a headed browser popup so the user can sign in via Google SSO.
        No email or password is ever collected — the user simply clicks their
        SPJIMR account in the browser window that opens on their desktop.
        After login, only session cookies are stored (encrypted). No passwords.
        """
        tok = _user_token()
        db = app.config["DATABASE"]
        # Extract an email hint from the existing Google OAuth token (if any)
        # so Playwright can pre-fill the Google account email field for convenience.
        google_creds = get_setting(db, tok, "google_credentials", None)
        hint_email = ""
        if google_creds and isinstance(google_creds, dict):
            # google_credentials may contain the user's email via token info
            hint_email = (
                google_creds.get("email")
                or google_creds.get("id_token_email")
                or ""
            )
        try:
            from .wisenet import login_with_browser_popup, build_requests_session
            cookies, sesskey, userid = login_with_browser_popup(hint_email=hint_email)
        except Exception as exc:
            flash(f"Wisenet login failed: {exc}", "error")
            return redirect(url_for("index"))
        # Store cookies (encrypted) — no passwords ever stored
        set_setting(db, tok, "wisenet_cookies", encrypt_json({
            "cookies": cookies,
            "sesskey": sesskey,
            "userid": userid,
        }))
        # Immediately scrape mandatory sessions
        return redirect(url_for("wisenet_sync"))

    # Keep old route name as alias for any bookmarked links
    app.add_url_rule("/wisenet/login", view_func=wisenet_connect, methods=["POST"])

    @app.post("/wisenet/sync")
    @app.get("/wisenet/sync")
    def wisenet_sync():
        """Re-scrape Wisenet mandatory sessions using stored cookies."""
        tok = _user_token()
        db = app.config["DATABASE"]
        # Try new cookie-based storage first, fall back to legacy credential storage
        stored = get_setting(db, tok, "wisenet_cookies", None)
        if stored:
            session_data = decrypt_json(stored)
        else:
            # Legacy path: had credentials stored — prompt to reconnect
            flash("Please reconnect Wisenet — click \"Connect Wisenet\" to open the login browser.", "info")
            return redirect(url_for("index"))
        if not session_data:
            flash("Wisenet session data could not be read. Please reconnect.", "error")
            return redirect(url_for("index"))
        try:
            from .wisenet import build_client_from_cookies
            client = build_client_from_cookies(
                cookies=session_data["cookies"],
                sesskey=session_data["sesskey"],
                userid=session_data["userid"],
            )
            mandatory_data = client.get_all_mandatory_sessions()
        except Exception as exc:
            flash(f"Wisenet scrape failed: {exc}", "error")
            return redirect(url_for("index"))
        save_mandatory_sessions(db, tok, mandatory_data)
        _reapply_mandatory_flags(app, tok, mandatory_data)
        total = sum(len(v) for v in mandatory_data.values())
        flash(
            f"Wisenet sync done \u2014 {len(mandatory_data)} courses scanned, "
            f"{total} mandatory sessions marked in red.",
            "success",
        )
        return redirect(url_for("index"))

    @app.post("/wisenet/reset")
    def wisenet_reset():
        tok = _user_token()
        db = app.config["DATABASE"]
        delete_setting(db, tok, "wisenet_credentials_encrypted")  # legacy cleanup
        delete_setting(db, tok, "wisenet_cookies")
        clear_mandatory_sessions(db, tok)
        flash("Wisenet disconnected.", "info")
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
        if not get_setting(db, tok, "google_credentials", None):
            session["post_google_redirect"] = "sync"
            return redirect(url_for("google_login"))
        try:
            result = _sync_calendar(app, tok)
            flash(f"Synced! {result.imported} events added to your Google Calendar.", "success")
        except Exception as exc:
            flash(f"Calendar sync failed: {exc}", "error")
        return redirect(url_for("index"))

    @app.get("/google/login")
    def google_login():
        client = GoogleCalendarClient()
        auth_url, state = client.authorization_url_with_state()
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

    # ── Weekly background refresh ─────────────────────────────────────────────


    def weekly_job() -> None:
        """Refresh timetable and re-sync calendar for all fully-configured users."""
        with app.app_context():
            db = app.config["DATABASE"]
            for tok in get_all_users_with_credentials(db):
                try:
                    credentials = _stored_tcs_credentials(app, tok)
                    if not credentials:
                        continue
                    events = _fetch_timetable(app, credentials, tok)
                    set_setting(db, tok, "preview_events", serialize_events(events))
                    save_events(db, tok, events)
                    _sync_calendar(app, tok)
                except Exception:
                    pass  # Don't let one user's failure block others

    if not app.config["TESTING"]:
        WeeklyScheduler(weekly_job).start()

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
    # Apply mandatory flags if Wisenet data is available
    if user_token:
        mandatory_data = get_mandatory_sessions(app.config["DATABASE"], user_token)
        if mandatory_data:
            events = apply_mandatory_flags(events, mandatory_data)
    return events


def _sync_calendar(app: Flask, user_token: str):
    db = app.config["DATABASE"]
    credentials = get_setting(db, user_token, "google_credentials", None)
    client = GoogleCalendarClient(
        credentials=credentials,
        dry_run=bool(credentials and credentials.get("dry_run")),
    )
    payloads = list_event_payloads(db, user_token)
    result = client.sync(payloads)
    mark_many_synced(db, user_token, result.event_ids)
    return result


def _stored_tcs_credentials(app: Flask, user_token: str) -> dict | None:
    encrypted = get_setting(
        app.config["DATABASE"], user_token, "tcs_credentials_encrypted", None
    )
    return decrypt_json(encrypted)  # type: ignore[arg-type]


def _sync_window_now(app: Flask) -> datetime | None:
    configured = app.config.get("SYNC_WINDOW_NOW")
    if isinstance(configured, str):
        return datetime.fromisoformat(configured)
    return configured


def _group_events(events: list[dict]) -> list[dict]:
    """Group a flat list of serialised events into day buckets for the template."""
    today = datetime.now().date()
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
            elif d == today.replace(day=today.day + 1) if today.day < 28 else today:
                label = d.strftime("%A, %b %d")
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


def _fetch_wisenet_mandatory_sessions_from_cookies(
    session_data: dict,
) -> dict[str, list[int]]:
    """Scrape Wisenet mandatory sessions using previously captured cookies."""
    from .wisenet import build_client_from_cookies
    client = build_client_from_cookies(
        cookies=session_data["cookies"],
        sesskey=session_data["sesskey"],
        userid=session_data["userid"],
    )
    return client.get_all_mandatory_sessions()


def _reapply_mandatory_flags(
    app: Flask,
    user_token: str,
    mandatory_data: dict[str, list[int]],
) -> None:
    """Re-flag any stored timetable events as mandatory and re-save."""
    db = app.config["DATABASE"]
    events_raw: list[dict] = get_setting(db, user_token, "preview_events", [])  # type: ignore
    if not events_raw:
        return
    # We need to deserialise, re-apply flags, re-serialise
    from .models import TimetableEvent
    from datetime import datetime as _DT
    updated: list[dict] = []
    for e in events_raw:
        code = (e.get("course_code") or "").split("-")[0].strip().upper()
        sess = e.get("session_number", "")
        is_mandatory = False
        if code in mandatory_data and sess:
            try:
                is_mandatory = int(sess) in mandatory_data[code]
            except ValueError:
                pass
        # Update the dict fields directly (payloads are dicts, not dataclasses)
        e_updated = dict(e)
        e_updated["mandatory"] = is_mandatory
        if is_mandatory:
            subject = e.get("title", "").replace("🔴 MANDATORY: ", "")
            e_updated["title"] = f"🔴 MANDATORY: {subject}"
        updated.append(e_updated)
    set_setting(db, user_token, "preview_events", updated)


# Public aliases used by tests
fetch_timetable = _fetch_timetable
sync_calendar = _sync_calendar
stored_tcs_credentials = _stored_tcs_credentials
sync_window_now = _sync_window_now
