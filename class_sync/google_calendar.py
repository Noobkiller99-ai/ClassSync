from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import CALENDAR_NAME


SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


@dataclass
class SyncResult:
    calendar_id: str
    created_calendar: bool
    imported: int
    dry_run: bool
    event_ids: dict[str, str]


class GoogleCalendarClient:
    def __init__(self, credentials: Any | None = None, dry_run: bool = False):
        self.credentials = credentials
        self.dry_run = dry_run

    def authorization_url(self) -> str:
        """Return the Google OAuth authorisation URL, or the dry-run callback URL."""
        if self.dry_run:
            return "/google/callback?dry_run=1"
        from google_auth_oauthlib.flow import Flow

        flow = self._flow()
        url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return url

    def authorization_url_with_state(self, reauth: bool = False) -> tuple[str, str]:
        """Return (url, state) for CSRF-safe OAuth. Use with real Google accounts.

        Args:
            reauth: If True (re-connecting an already-linked account), omit the
                    ``consent`` prompt so the user can simply click their saved
                    account instead of seeing the full re-consent screen.
                    Set False (default) on first-time auth to guarantee a
                    refresh_token is returned.
        """
        if self.dry_run:
            return "/google/callback?dry_run=1", ""
        from google_auth_oauthlib.flow import Flow

        flow = self._flow()
        prompt = "select_account" if reauth else "select_account consent"
        url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt=prompt,
        )
        return url, state

    def fetch_token(self, authorization_response: str, state: str | None = None) -> dict:
        """Exchange the OAuth callback URL for a token dict."""
        if self.dry_run:
            return {"dry_run": True}
        from google_auth_oauthlib.flow import Flow

        # Extract the authorisation code directly so we avoid cross-request
        # state-mismatch issues when the flow object is recreated per request.
        parsed = urlparse(authorization_response)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            raise RuntimeError(
                "Google OAuth callback did not contain an authorisation code. "
                "Ensure the redirect URI is registered in Google Cloud Console."
            )

        flow = self._flow(state=state)
        # Allow the library to exchange the code even over plain HTTP (local dev).
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Try to get the user's email from the ID token (if present)
        email = ""
        try:
            import json, base64
            if creds.id_token:
                # JWT payload is the second part, base64-encoded
                payload_b64 = creds.id_token.split(".")[1]
                padding = 4 - len(payload_b64) % 4
                payload = json.loads(base64.b64decode(payload_b64 + "=" * padding))
                email = payload.get("email", "")
        except Exception:
            pass

        return {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
            "email": email,
        }

    def sync(self, event_payloads: list[dict]) -> SyncResult:
        event_ids: dict[str, str] = {}
        if self.dry_run:
            for event in event_payloads:
                event_ids[event["uid"]] = (
                    event.get("synced_event_id") or f"dry-run-{abs(hash(event['uid']))}"
                )
            return SyncResult("dry-run-spjimr-timetable", True, len(event_payloads), True, event_ids)

        # Short-circuit: nothing to sync, skip all API calls
        if not event_payloads:
            return SyncResult("", False, 0, False, {})

        import logging
        logger = logging.getLogger(__name__)

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = self.credentials
        if isinstance(credentials, dict):
            credentials = Credentials(**{
                k: v for k, v in credentials.items()
                if k in {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes"}
            })
        service = build("calendar", "v3", credentials=credentials)
        calendar_id, created = self._ensure_calendar(service)

        # 1. Build a map of expected incoming times for each (courseCode, sessionNumber)
        incoming_times_by_code_session: dict[tuple[str, str], set[str]] = {}
        for event in event_payloads:
            private_props = event.get("extendedProperties", {}).get("private", {})
            p_code = private_props.get("courseCode", "").strip().upper()
            p_sess = private_props.get("sessionNumber", "").strip()
            start_dt = event.get("start", {}).get("dateTime")
            if p_code and p_sess and start_dt:
                key = (p_code, p_sess)
                if key not in incoming_times_by_code_session:
                    incoming_times_by_code_session[key] = set()
                incoming_times_by_code_session[key].add(start_dt[:19])

        # Retrieve existing calendar events to avoid creating duplicates.
        # timeMin restricts to events starting from yesterday, cutting API payload significantly.
        import datetime as _dt
        time_min = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        existing_by_uid = {}
        existing_by_time_title = {}
        existing_by_code_session: dict[tuple[str, str], str] = {}
        existing_by_code_session_time: dict[tuple[str, str, str], str] = {}
        try:
            page_token = None
            while True:
                events_result = service.events().list(
                    calendarId=calendar_id,
                    pageToken=page_token,
                    singleEvents=True,
                    maxResults=250,
                    timeMin=time_min,
                ).execute()
                for item in events_result.get("items", []):
                    private = item.get("extendedProperties", {}).get("private", {})
                    code = private.get("courseCode", "").strip().upper()
                    sess = private.get("sessionNumber", "").strip()

                    # Fallback: parse code & session from description if private properties are missing
                    desc = item.get("description", "")
                    if desc and (not code or not sess):
                        import re as _re
                        code_match = _re.search(r"Course Code:\s*(\S+)", desc)
                        sess_match = _re.search(r"Session No:\s*(\d+)", desc)
                        if code_match and not code:
                            code = code_match.group(1).split("-")[0].strip().upper()
                        if sess_match and not sess:
                            sess = sess_match.group(1).strip()

                    start_dt = item.get("start", {}).get("dateTime")
                    summary = item.get("summary", "")

                    # 2. Strict checks for duplicate and stale/rescheduled events
                    is_dup_or_stale = False
                    if code and sess and start_dt:
                        norm_start = start_dt[:19]
                        key = (code, sess)
                        expected_times = incoming_times_by_code_session.get(key, set())
                        
                        # Rescheduled Slot: If the time of the existing event is NOT in the incoming timetable,
                        # delete it immediately from Google Calendar.
                        if norm_start not in expected_times:
                            is_dup_or_stale = True
                            duplicate_id = item["id"]
                            try:
                                service.events().delete(
                                    calendarId=calendar_id, eventId=duplicate_id
                                ).execute()
                                logger.info("Deleted rescheduled calendar event %s (code=%s, session=%s, start=%s)", duplicate_id, code, sess, norm_start)
                            except Exception as e:
                                logger.warning("Failed to delete rescheduled event %s: %s", duplicate_id, e)
                        else:
                            # If it matches expected time, check for duplicate at the same slot
                            time_key = (code, sess, norm_start)
                            if time_key in existing_by_code_session_time:
                                is_dup_or_stale = True
                                duplicate_id = item["id"]
                                try:
                                    service.events().delete(
                                        calendarId=calendar_id, eventId=duplicate_id
                                    ).execute()
                                    logger.info("Deleted duplicate calendar event %s (code=%s, session=%s, start=%s)", duplicate_id, code, sess, norm_start)
                                except Exception as e:
                                    logger.warning("Failed to delete duplicate event %s: %s", duplicate_id, e)
                            else:
                                existing_by_code_session_time[time_key] = item["id"]

                    # Fallback check for duplicates by time and title if not already handled
                    if not is_dup_or_stale and start_dt and summary:
                        norm_start = start_dt[:19]
                        norm_title = summary.replace("🔴 MANDATORY: ", "").replace("📝 EVALUATION: ", "").replace("🔴 MANDATORY EVALUATION: ", "").strip()
                        time_title_key = (norm_title, norm_start)
                        if time_title_key in existing_by_time_title:
                            is_dup_or_stale = True
                            duplicate_id = item["id"]
                            try:
                                service.events().delete(
                                    calendarId=calendar_id, eventId=duplicate_id
                                ).execute()
                                logger.info("Deleted duplicate calendar event by time/title %s (%s at %s)", duplicate_id, norm_title, norm_start)
                            except Exception as e:
                                logger.warning("Failed to delete duplicate event %s: %s", duplicate_id, e)
                        else:
                            existing_by_time_title[time_title_key] = item["id"]

                    # Do not index deleted events
                    if is_dup_or_stale:
                        continue

                    # 3. Index remaining active events
                    uid = private.get("classSyncUid")
                    if uid:
                        existing_by_uid[uid] = item["id"]

                    if code and sess:
                        existing_by_code_session[(code, sess)] = item["id"]
                page_token = events_result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            logger.warning("Failed to list existing calendar events: %s", e)

        imported = 0
        for event in event_payloads:
            body = {key: value for key, value in event.items() if key not in {"uid", "synced_event_id"}}
            uid = event["uid"]
            google_id = event.get("synced_event_id")

            # Determine the normalised code+session key for this payload
            private_props = event.get("extendedProperties", {}).get("private", {})
            payload_code = private_props.get("courseCode", "").strip().upper()
            payload_sess = private_props.get("sessionNumber", "").strip()
            starts_at_iso = event["start"]["dateTime"][:19]

            # Rule: If the existing event matches course code, session number, and time,
            # SKIP and do not create or update any event.
            if payload_code and payload_sess:
                time_key = (payload_code, payload_sess, starts_at_iso)
                if time_key in existing_by_code_session_time:
                    event_ids[uid] = existing_by_code_session_time[time_key]
                    # Skip completely (no write API call)
                    continue

            # Fallback duplicate prevention (using title/time)
            fallback_id = None
            norm_title = event["summary"].replace("🔴 MANDATORY: ", "").replace("📝 EVALUATION: ", "").replace("🔴 MANDATORY EVALUATION: ", "").strip()
            if (norm_title, starts_at_iso) in existing_by_time_title:
                fallback_id = existing_by_time_title[(norm_title, starts_at_iso)]
            elif uid in existing_by_uid:
                fallback_id = existing_by_uid[uid]

            if fallback_id:
                event_ids[uid] = fallback_id
                # Skip completely (no write API call)
                continue

            if google_id:
                try:
                    service.events().update(
                        calendarId=calendar_id, eventId=google_id, body=body
                    ).execute()
                except Exception:
                    # Event may have been deleted manually on Google Calendar; re-insert it.
                    created_event = service.events().insert(
                        calendarId=calendar_id, body=body
                    ).execute()
                    google_id = created_event["id"]
            else:
                created_event = service.events().insert(
                    calendarId=calendar_id, body=body
                ).execute()
                google_id = created_event["id"]
            event_ids[uid] = google_id
            imported += 1
        return SyncResult(calendar_id, created, imported, False, event_ids)

    def _flow(self, state: str | None = None):
        from google_auth_oauthlib.flow import Flow

        redirect_uri = os.environ.get(
            "GOOGLE_REDIRECT_URI", "http://127.0.0.1:5002/google/callback"
        )
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        if state:
            flow.oauth2session.state = state
        return flow

    def _ensure_calendar(self, service: Any) -> tuple[str, bool]:
        page_token = None
        while True:
            result = service.calendarList().list(pageToken=page_token).execute()
            for calendar in result.get("items", []):
                if calendar.get("summary") == CALENDAR_NAME:
                    return calendar["id"], False
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        calendar = service.calendars().insert(
            body={"summary": CALENDAR_NAME, "timeZone": "Asia/Kolkata"}
        ).execute()
        return calendar["id"], True
