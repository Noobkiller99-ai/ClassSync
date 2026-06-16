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

    def authorization_url_with_state(self) -> tuple[str, str]:
        """Return (url, state) for CSRF-safe OAuth. Use with real Google accounts."""
        if self.dry_run:
            return "/google/callback?dry_run=1", ""
        from google_auth_oauthlib.flow import Flow

        flow = self._flow()
        url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="select_account consent",  # force account picker + re-consent for new scopes
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
        imported = 0
        for event in event_payloads:
            body = {key: value for key, value in event.items() if key not in {"uid", "synced_event_id"}}
            google_id = event.get("synced_event_id")
            if google_id:
                try:
                    service.events().update(
                        calendarId=calendar_id, eventId=google_id, body=body
                    ).execute()
                except Exception:
                    # Event may have been deleted manually; re-insert it.
                    created_event = service.events().insert(
                        calendarId=calendar_id, body=body
                    ).execute()
                    google_id = created_event["id"]
            else:
                created_event = service.events().insert(
                    calendarId=calendar_id, body=body
                ).execute()
                google_id = created_event["id"]
            event_ids[event["uid"]] = google_id
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
