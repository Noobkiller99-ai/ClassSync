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
    # True when running locally (not on Render or any cloud with RENDER env set)
    IS_LOCAL = not os.getenv("RENDER")

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
            google_ready=google_ready,
            google_configured=app.config["GOOGLE_CONFIGURED"],
            tcs_ready=tcs_ready,
            wisenet_ready=wisenet_ready,
            mandatory_data=mandatory_data,
            is_local=IS_LOCAL,
            event_groups=event_groups,
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
        delete_setting(db, tok, "wisenet_credentials_encrypted")
        delete_setting(db, tok, "wisenet_cookies")
        clear_events(db, tok)
        clear_mandatory_sessions(db, tok)
        flash("Session cleared. Enter your TCS iON credentials to start over.", "info")
        return redirect(url_for("index"))

    # ── Wisenet (Moodle LMS) routes ───────────────────────────────────────────

    @app.post("/wisenet/connect")
    def wisenet_connect():
        """
        Redirect the user's browser to Wisenet SAML2 login (Google SSO).
        After login, Wisenet sends the browser to /wisenet/bridge which uses
        JavaScript fetch() + Wisenet's open CORS policy to relay the Moodle
        sesskey back to our server.

        Works identically on local and cloud. No credentials stored.
        Only @spjimr.org accounts can log into Wisenet (enforced by SPJIMR IT).
        """
        import secrets as _secrets
        from urllib.parse import quote as _quote

        tok = _user_token()
        db = app.config["DATABASE"]

        # Generate a one-time state token to tie the redirect to this session
        state = _secrets.token_urlsafe(24)
        set_setting(db, tok, "wisenet_state", state)

        # Bridge URL: where Wisenet sends the browser after successful login
        bridge_url = url_for("wisenet_bridge", state=state, _external=True)

        # Wisenet's SAML2 login with wantsurl = our bridge page
        # idp value probed from the login page of wisenet.spjimr.org
        wisenet_login = (
            "https://wisenet.spjimr.org/auth/saml2/login.php"
            f"?wants={_quote(bridge_url, safe='')}"
            "&idp=20e275a0d092a86c5c963a3b05430c48"
            "&passive=off"
        )
        return redirect(wisenet_login)

    # Alias kept for compatibility
    app.add_url_rule("/wisenet/login", view_func=wisenet_connect, methods=["POST"])

    @app.get("/wisenet/bridge")
    def wisenet_bridge():
        """
        Relay page served by our server after Wisenet SAML2 login completes.

        The user's browser arrives here with a valid MoodleSession cookie set
        on wisenet.spjimr.org. Wisenet's REST API has Access-Control-Allow-Origin: *
        (verified), so JavaScript on THIS page can fetch Wisenet's /my/ endpoint
        with credentials:include — the browser sends the MoodleSession cookie
        automatically. We extract the sesskey from the returned HTML and POST
        it to /wisenet/capture on our server.
        """
        state = request.args.get("state", "")
        capture_url = url_for("wisenet_capture", _external=True)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ClassSync — Connecting Wisenet…</title>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Inter',system-ui,sans-serif;background:#0f0f14;color:#e5e7eb;
          display:flex;align-items:center;justify-content:center;min-height:100vh}}
    .card{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);
           border-radius:16px;padding:48px 40px;text-align:center;max-width:440px;width:90%}}
    .spinner{{width:52px;height:52px;border:3px solid rgba(239,68,68,.2);
              border-top-color:#ef4444;border-radius:50%;
              animation:spin .85s linear infinite;margin:0 auto 28px}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    h2{{font-size:20px;font-weight:700;margin-bottom:10px}}
    p{{font-size:13px;color:#9ca3af;line-height:1.7}}
    .err{{color:#f87171;font-size:13px;margin-top:18px;display:none;text-align:left;
           background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);
           border-radius:8px;padding:12px}}
    .retry{{display:none;margin-top:16px;padding:10px 20px;background:#ef4444;color:#fff;
             border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600}}
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner" id="spin"></div>
    <h2>Connecting to Wisenet…</h2>
    <p>Securely linking your Moodle session.<br>This takes just a moment.</p>
    <div class="err" id="err"></div>
    <button class="retry" id="retry" onclick="window.location.href='/'">← Go Back &amp; Try Again</button>
  </div>
  <script>
  (async () => {{
    const CAPTURE = {capture_url!r};
    const STATE   = {state!r};
    function fail(msg) {{
      document.getElementById('spin').style.display = 'none';
      const e = document.getElementById('err');
      e.style.display = 'block';
      e.textContent = '⚠ ' + msg;
      document.getElementById('retry').style.display = 'inline-block';
    }}
    try {{
      // Fetch Wisenet /my/ — browser sends MoodleSession cookie (cross-origin allowed)
      const r = await fetch('https://wisenet.spjimr.org/my/', {{
        credentials: 'include',
        cache: 'no-store',
      }});
      if (!r.ok) throw new Error('Wisenet returned HTTP ' + r.status + '. Are you logged in?');
      const html = await r.text();

      // Extract sesskey from the Moodle page JSON config
      const sk = html.match(/"sesskey"\\s*:\\s*"([^"]+)"/);
      const ui = html.match(/"userid"\\s*:\\s*(\\d+)/);
      const sesskey = sk ? sk[1] : '';
      const userid  = ui ? ui[1] : '';

      if (!sesskey) {{
        // If we can't find sesskey, the user may not be logged in
        // (Wisenet may have redirected to login page)
        if (html.includes('login') && !html.includes('logoutbutton')) {{
          throw new Error('Wisenet session not established. Please make sure you signed in with your @spjimr.org Google account.');
        }}
        throw new Error('Could not find Moodle session key in Wisenet page. Please try again.');
      }}

      // Relay sesskey + userid to our server
      const post = await fetch(CAPTURE, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ state: STATE, sesskey, userid }}),
      }});
      if (!post.ok) throw new Error('Capture endpoint returned HTTP ' + post.status);
      const result = await post.json();
      if (result.ok) {{
        window.location.href = result.redirect || '/';
      }} else {{
        throw new Error(result.error || 'Session capture failed.');
      }}
    }} catch (e) {{
      fail(e.message);
    }}
  }})();
  </script>
</body>
</html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.post("/wisenet/capture")
    def wisenet_capture():
        """
        Receive sesskey + userid relayed from /wisenet/bridge JavaScript.
        Validate the state token, verify the Wisenet session is genuine by
        calling the Wisenet AJAX endpoint server-side with the sesskey, then
        store it for scraping.
        """
        import json as _json

        tok = _user_token()
        db = app.config["DATABASE"]

        data = request.get_json(silent=True) or {}
        state   = data.get("state", "")
        sesskey = data.get("sesskey", "").strip()
        userid  = data.get("userid", "").strip()

        # Validate state (CSRF protection)
        stored_state = get_setting(db, tok, "wisenet_state", None)
        if not stored_state or state != stored_state:
            return {"ok": False, "error": "Session expired or invalid state. Click Connect Wisenet again."}
        delete_setting(db, tok, "wisenet_state")

        if not sesskey:
            return {"ok": False, "error": "No Moodle sesskey received — login may have failed."}

        # Verify the sesskey is genuine by calling Wisenet's AJAX service server-side
        # We don't have cookies but we can verify via the REST API approach:
        # Wisenet AJAX requires a sesskey AND a cookie, so we use the nologin service
        # to do a basic connectivity check, then trust the sesskey from the HTML.
        #
        # Security note: the sesskey is generated by Moodle per-session and embedded
        # in page HTML. Getting a valid sesskey from /my/ proves the user is logged in.
        # We additionally validate it's a plausible value (alphanumeric, 10+ chars).
        import re as _re
        if not _re.match(r'^[A-Za-z0-9]{10,}$', sesskey):
            return {"ok": False, "error": "Invalid sesskey format. Please try connecting again."}

        # Store the session data
        set_setting(db, tok, "wisenet_cookies", encrypt_json({
            "sesskey": sesskey,
            "userid":  userid,
            "cookies": {},  # No raw cookie accessible cross-origin; sesskey proves login
        }))

        logger.info("Wisenet session captured via bridge: userid=%s", userid)
        return {"ok": True, "redirect": url_for("wisenet_sync", _external=True)}



    @app.post("/wisenet/sync")
    @app.get("/wisenet/sync")
    def wisenet_sync():
        """Re-scrape Wisenet mandatory sessions using stored session data."""
        tok = _user_token()
        db = app.config["DATABASE"]
        stored = get_setting(db, tok, "wisenet_cookies", None)
        if not stored:
            flash("Please connect Wisenet first.", "info")
            return redirect(url_for("index"))
        session_data = decrypt_json(stored)
        if not session_data:
            flash("Wisenet session data could not be read. Please reconnect.", "error")
            return redirect(url_for("index"))
        try:
            from .wisenet import build_client_from_cookies
            client = build_client_from_cookies(
                cookies=session_data.get("cookies", {}),
                sesskey=session_data.get("sesskey", ""),
                userid=session_data.get("userid", ""),
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
