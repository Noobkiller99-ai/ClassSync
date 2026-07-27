"""
class_sync/spp.py
~~~~~~~~~~~~~~~~~
Salesforce Experience Cloud (SPP) client for spp.spjimr.org.

The portal runs on Salesforce LWR (Lightning Web Runtime). All data is
exposed through a single Apex REST endpoint:

    POST/GET /student/webruntime/api/apex/execute

Authentication uses a cookie-based session obtained by:
  1. Calling the Apex ``login`` method (guest mode) with SPJIMR email + password.
  2. Following a redirect to ``/vforcesite/secur/frontdoor.jsp?sid=...``
  3. Session cookies are then set and all subsequent requests are authenticated.

Credentials are NEVER stored server-side — the session is held entirely in
the user's browser cookies (``requests.Session`` here acts as the in-memory
browser for the duration of the request only).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

import requests

from .models import TimetableEvent, SYNC_WINDOW_DAYS


# ── Constants ──────────────────────────────────────────────────────────────────

SPP_BASE_URL       = "https://spp.spjimr.org"
SPP_APEX_PATH      = "/student/webruntime/api/apex/execute"
SPP_FRONTDOOR_PATH = "/vforcesite/secur/frontdoor.jsp"

# Salesforce Apex classname IDs (from HAR analysis)
CLS_LOGIN   = "@udd/01pOS00000rnWDr"   # login (guest)
CLS_AUTH    = "@udd/01pOS00000rnWER"   # getUserInfo, getMenuItems, fetchEmergencyDetails
CLS_SCHED   = "@udd/01pOS00000rnWET"   # getEnrolledSessions, getSessionDetails, getSessionTypeOptions
CLS_ATTEND  = "@udd/01pOS00000rnWEP"   # getData (attendance history)
CLS_NOTIF   = "@udd/01pOS00000rnWDv"   # getPortalNotifications
CLS_LEAVES  = "@udd/01pOS00000rnWDl"   # getStudentLeaves

# Google Calendar colour for SPJIMR SPP events (hex #531f75)
SPP_CALENDAR_NAME  = "SPJIMR Timetable"
SPP_CALENDAR_COLOR = "#531f75"          # Used when creating the calendar via the API

# Source label embedded in event descriptions
SOURCE_SPP = "SPJIMR SPP"


# ── Exceptions ─────────────────────────────────────────────────────────────────

class SppError(RuntimeError):
    """Raised for any SPP / Salesforce portal error."""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_12h_time(date_str: str, time_str: str) -> datetime:
    """
    Parse a combined date + 12-hour time string into a datetime.

    ``date_str`` is ISO-format (``2026-07-28``).
    ``time_str`` is like ``9:00AM`` or ``12:10PM``.
    """
    year, month, day = [int(p) for p in date_str.split("-")]
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", time_str.strip(), re.I)
    if not m:
        raise ValueError(f"Unsupported SPP time format: {time_str!r}")
    hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    if period == "AM" and hour == 12:
        hour = 0
    return datetime(year, month, day, hour, minute)


def parse_spp_sessions(
    sessions: list[dict],
    details_map: dict[str, dict] | None = None,
) -> list[TimetableEvent]:
    """
    Convert a list of raw SPP ``getEnrolledSessions`` dicts into
    :class:`~class_sync.models.TimetableEvent` objects.

    ``details_map`` is an optional ``{session_id: detail_dict}`` map from
    ``getSessionDetails`` calls. When provided, it enriches events with the
    classroom/location and full description.
    """
    events: list[TimetableEvent] = []
    seen: set[tuple] = set()

    for item in sessions:
        session_id   = item.get("id", "")
        course_name  = (item.get("courseName") or "").strip()
        session_date = (item.get("sessionDate") or "").strip()   # "2026-07-28"
        start_time   = (item.get("startTime") or "").strip()     # "9:00AM"
        end_time     = (item.get("endTime") or "").strip()       # "10:10AM"
        activity     = (item.get("courseActivity") or "").strip()
        instructor   = (item.get("instructorNames") or "").strip()
        title_raw    = (item.get("title") or "").strip()         # session number like "17"

        if not (course_name and session_date and start_time and end_time):
            continue

        try:
            starts_at = _parse_12h_time(session_date, start_time)
            ends_at   = _parse_12h_time(session_date, end_time)
        except ValueError:
            continue

        # Enrich with session detail if available
        location    = ""
        description = ""
        if details_map and session_id in details_map:
            det = details_map[session_id]
            location    = (det.get("location") or "").strip()
            description = (det.get("description") or "").strip()
            if description.lower() in {"not given", "n/a", "-"}:
                description = ""

        # Deduplicate
        dedup_key = (starts_at, ends_at, course_name.lower(), title_raw.lower())
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Build a stable UID using SPP session ID (most reliable) or fallback
        uid = session_id or f"{session_date}|{start_time}|{course_name}"

        events.append(
            TimetableEvent(
                uid=uid,
                subject_name=course_name,
                course_code="",          # SPP doesn't expose course codes in schedule view
                faculty=instructor,
                classroom=location,
                starts_at=starts_at,
                ends_at=ends_at,
                status="",               # Attendance status not available in schedule view
                mandatory=False,
                session_number=title_raw,
                activity_name=activity,
            )
        )

    return events


def serialize_spp_events(events: list[TimetableEvent]) -> list[dict]:
    """Serialise SPP events to the same flat dict shape used by the TCS serialiser."""
    return [
        {
            "uid": e.uid,
            "title": e.title,
            "course_code": e.course_code,
            "faculty": e.faculty,
            "classroom": e.classroom,
            "starts_at": e.starts_at.isoformat(),
            "ends_at": e.ends_at.isoformat(),
            "status": e.status,
            "description": _spp_description(e),
            "mandatory": e.mandatory,
            "session_number": e.session_number,
            "activity_name": e.activity_name,
            "source": SOURCE_SPP,
        }
        for e in events
    ]


def _spp_description(event: TimetableEvent) -> str:
    """Build a rich description string for SPP events (used in preview dict, NOT in Google Calendar payload)."""
    lines = [
        f"Faculty: {event.faculty or '-'}",
        f"Room: {event.classroom or '-'}",
        f"Activity: {event.activity_name or '-'}",
        f"Source: {SOURCE_SPP}",
    ]
    if event.session_number:
        lines.append(f"Session: {event.session_number}")
    return "\n".join(lines)


def next_two_weeks_spp(
    events: list[TimetableEvent], now: datetime | None = None
) -> list[TimetableEvent]:
    """Filter events to the rolling 14-day window (same as TCS)."""
    current = now or datetime.now()
    window_end = current + timedelta(days=SYNC_WINDOW_DAYS)
    return [e for e in events if current.date() <= e.starts_at.date() <= window_end.date()]


# ── SPP Client ─────────────────────────────────────────────────────────────────

class SppClient:
    """
    Session-based HTTP client for the SPJIMR SPP Salesforce portal.

    Credentials are used transiently — the resulting session cookies are the
    only persistent state (stored in the user's Flask session / browser).
    """

    # ── Internal state ──────────────────────────────────────────────────────

    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Referer": f"{SPP_BASE_URL}/student/login",
            "Origin": SPP_BASE_URL,
        })
        self._csrf_token: str = ""
        self._user_id: str = ""
        self._user_info: dict = {}
    # ── Authentication ──────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> None:
        """
        Authenticate against the SPP portal.

        Step 1: POST the Apex ``login`` method (guest mode) with email + password.
                The response ``returnValue`` is the full frontdoor URL string.
        Step 2: GET the frontdoor URL so the session receives its authentication
                cookies (sid, sfdc-stream, etc.).
        Step 3: Fetch the student home page to obtain a CSRF token.
        Step 4: Verify authentication by calling getUserInfo.
        """
        if not email or not password:
            raise SppError("SPJIMR email and password are required.")

        # ── Step 1: Guest Apex login → returns frontdoor URL ─────────────
        login_payload = {
            "namespace": "",
            "classname": CLS_LOGIN,
            "method": "login",
            "isContinuation": False,
            "params": {"username": email, "password": password},
            "cacheable": False,
        }
        resp_data = self._apex_post_guest(login_payload)

        # returnValue is the full frontdoor URL string, e.g.:
        # "https://spp.spjimr.org/vforcesite/secur/frontdoor.jsp?...&sid=TOKEN..."
        frontdoor_url = resp_data.get("returnValue", "")
        if not frontdoor_url or not isinstance(frontdoor_url, str):
            raise SppError(
                "SPP login did not return a session URL. "
                "Please check your SPJIMR email and password."
            )

        # ── Step 2: Follow frontdoor URL to set session cookies ───────────
        self._establish_session_via_frontdoor(frontdoor_url)

        # ── Step 3: Obtain CSRF token from the authenticated home page ────
        self._fetch_csrf_token()

        # ── Step 4: Verify session works via a GET call (no CSRF needed) ──────
        # getUserInfo fails as POST from server-side (Salesforce requires browser
        # signed JWT for POST). Use getMenuItems GET instead to verify the session.
        try:
            menu = self._apex_get(
                classname=CLS_AUTH,
                method="getMenuItems",
                params={"menuName": "Main_Navigation"},
            )
            if menu is None:
                raise SppError("Session established but menu returned no data. Check credentials.")
            # Extract user info from page HTML (embedded in LWR JS bootstrap)
            self._user_info = self._get_user_info_from_page()
        except SppError:
            raise
        except Exception as exc:
            raise SppError(f"SPP session verification failed: {exc}") from exc

    def _establish_session_via_frontdoor(self, frontdoor_url: str) -> None:
        """GET the full frontdoor URL (or build one from a bare sid) to receive session cookies."""
        # If caller passed a full URL, use it directly.
        # If they passed a bare sid token, build the standard frontdoor URL.
        if frontdoor_url.startswith("http"):
            url = frontdoor_url
        else:
            url = (
                f"{SPP_BASE_URL}{SPP_FRONTDOOR_PATH}"
                f"?sid={frontdoor_url}&retURL=%2Fstudent%2F"
            )
        try:
            self._session.get(url, allow_redirects=True, timeout=30)
        except Exception as exc:
            raise SppError(f"Failed to establish SPP session via frontdoor: {exc}") from exc

    def _fetch_csrf_token(self) -> None:
        """
        Load the student home page and extract the CSRF token from the HTML
        or from a ``<meta name="csrf-token">`` tag (Salesforce LWR pattern).
        The token is also present as a cookie in some deployments.
        """
        try:
            resp = self._session.get(
                f"{SPP_BASE_URL}/student/",
                allow_redirects=True,
                timeout=30,
            )
            html = resp.text

            # Pattern 1: <meta name="csrf-token" content="...">
            m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if m:
                self._csrf_token = m.group(1)
                return

            # Pattern 2: JSON blob with csrfToken key
            m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
            if m:
                self._csrf_token = m.group(1)
                return

            # Pattern 3: cookie named csrf or csrfToken
            for cookie in self._session.cookies:
                if "csrf" in cookie.name.lower():
                    self._csrf_token = cookie.value
                    return

        except Exception:
            pass  # CSRF token is best-effort; some endpoints work without it

    # ── Low-level Apex callers ──────────────────────────────────────────────

    def _apex_post_guest(self, body: dict) -> dict:
        """POST to Apex endpoint in guest mode (no CSRF, no auth cookies)."""
        url = f"{SPP_BASE_URL}{SPP_APEX_PATH}?language=en-US&asGuest=true&htmlEncode=false"
        try:
            resp = self._session.post(url, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            raise SppError(f"SPP guest Apex call failed (HTTP {exc.response.status_code}).") from exc
        except Exception as exc:
            raise SppError(f"SPP guest Apex call error: {exc}") from exc

    def _apex_post(self, body: dict) -> Any:
        """POST to Apex endpoint in authenticated mode."""
        url = f"{SPP_BASE_URL}{SPP_APEX_PATH}?language=en-US&asGuest=false&htmlEncode=false"
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        if self._csrf_token:
            headers["csrf-token"] = self._csrf_token
        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("returnValue") if isinstance(data, dict) else data
        except requests.HTTPError as exc:
            raise SppError(f"SPP authenticated Apex call failed (HTTP {exc.response.status_code}).") from exc
        except Exception as exc:
            raise SppError(f"SPP authenticated Apex call error: {exc}") from exc

    def _apex_get(self, classname: str, method: str, params: dict | None = None) -> Any:
        """GET to Apex endpoint (cacheable=true calls are GET in Salesforce LWR)."""
        import urllib.parse
        query: dict[str, str] = {
            "cacheable": "true",
            "classname": classname,
            "isContinuation": "false",
            "method": method,
            "namespace": "",
            "language": "en-US",
            "asGuest": "false",
            "htmlEncode": "false",
        }
        if params:
            query["params"] = json.dumps(params)
        qs = urllib.parse.urlencode(query)
        url = f"{SPP_BASE_URL}{SPP_APEX_PATH}?{qs}"
        headers: dict[str, str] = {}
        if self._csrf_token:
            headers["csrf-token"] = self._csrf_token
        try:
            resp = self._session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("returnValue") if isinstance(data, dict) else data
        except requests.HTTPError as exc:
            raise SppError(f"SPP GET Apex call {method} failed (HTTP {exc.response.status_code}).") from exc
        except Exception as exc:
            raise SppError(f"SPP GET Apex call {method} error: {exc}") from exc

    # ── High-level data methods ─────────────────────────────────────────────

    def fetch_user_info(self) -> dict:
        """
        Return user info dict.

        getUserInfo is not callable as a server-side POST (Salesforce rejects it
        with 401 unless the CSRF JWT is browser-signed). We instead extract
        what we need from the student home page HTML + the cached user_info
        populated during login().
        """
        if self._user_info:
            return self._user_info
        self._user_info = self._get_user_info_from_page()
        return self._user_info

    def _get_user_info_from_page(self) -> dict:
        """
        Scrape user info from the LWR bootstrap embedded in the /student/ HTML.

        Salesforce LWR embeds a JSON context object in a <script> tag that
        contains the current user's name, email, account ID, etc.
        Returns a best-effort dict — callers must handle missing keys.
        """
        info: dict = {}
        try:
            resp = self._session.get(
                f"{SPP_BASE_URL}/student/",
                allow_redirects=True,
                timeout=30,
            )
            html = resp.text

            # Extract individual fields from JSON blobs in the LWR boot script
            field_patterns: list[tuple[str, str]] = [
                (r'"fullName"\s*:\s*"([^"]+)"', "fullName"),
                (r'"email"\s*:\s*"([^"]+@[^"]+)"', "email"),
                (r'"accountId"\s*:\s*"([^"]+)"', "accountId"),
                (r'"userId"\s*:\s*"([^"]+)"', "userId"),
                (r'"batchName"\s*:\s*"([^"]+)"', "batchName"),
                (r'"programCode"\s*:\s*"([^"]+)"', "programCode"),
                (r'"termId"\s*:\s*"([^"]+)"', "termId"),
            ]
            for pattern, key in field_patterns:
                m = re.search(pattern, html)
                if m:
                    info[key] = m.group(1)

            # Also extract CSRF token while we have the page
            if not self._csrf_token:
                m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
                if m:
                    self._csrf_token = m.group(1)

        except Exception:
            pass  # Best-effort — callers handle missing fields
        return info

    def fetch_enrolled_sessions(
        self, start_date: str, end_date: str, session_type: str | None = None
    ) -> list[dict]:
        """
        Fetch enrolled sessions for a date range.

        ``start_date`` / ``end_date`` are ISO strings like ``"2026-07-27"``.
        Returns the raw list from ``getEnrolledSessions``.
        """
        filter_json = json.dumps({
            "startDate": start_date,
            "endDate": end_date,
            "sessionType": session_type,
        })
        result = self._apex_get(
            classname=CLS_SCHED,
            method="getEnrolledSessions",
            params={"filterJson": filter_json},
        )
        return result if isinstance(result, list) else []

    def fetch_session_detail(self, session_id: str) -> dict:
        """
        Fetch detailed information for a single session (including location).

        Returns raw dict from ``getSessionDetails``.
        """
        result = self._apex_get(
            classname=CLS_SCHED,
            method="getSessionDetails",
            params={"sessionId": session_id},
        )
        return result if isinstance(result, dict) else {}

    def fetch_attendance_data(self, account_id: str, term_id: str) -> list[dict]:
        """
        Fetch attendance history for the given account and academic term.

        Returns list of attendance records from ``getData``.
        """
        result = self._apex_post({
            "namespace": "",
            "classname": CLS_ATTEND,
            "method": "getData",
            "isContinuation": False,
            "params": {"accId": account_id, "academicTermId": term_id},
            "cacheable": False,
        })
        return result if isinstance(result, list) else []

    def fetch_student_leaves(self, user_id: str) -> list[dict]:
        """
        Fetch the student's leave records.
        """
        result = self._apex_get(
            classname=CLS_LEAVES,
            method="getStudentLeaves",
            params={"userId": user_id},
        )
        return result if isinstance(result, list) else []

    def fetch_notifications(self) -> dict:
        """
        Fetch portal notifications.
        """
        result = self._apex_post({
            "namespace": "",
            "classname": CLS_NOTIF,
            "method": "getPortalNotifications",
            "isContinuation": False,
            "cacheable": False,
        })
        return result if isinstance(result, dict) else {}

    def fetch_menu_items(self) -> list[dict]:
        """
        Fetch the main navigation menu items.
        """
        result = self._apex_get(
            classname=CLS_AUTH,
            method="getMenuItems",
            params={"menuName": "Main_Navigation"},
        )
        return result if isinstance(result, list) else []

    def fetch_session_type_options(self) -> list[dict]:
        """
        Fetch available session type filter values (Session, Quizzes, End Term, etc.)
        """
        result = self._apex_post({
            "namespace": "",
            "classname": CLS_SCHED,
            "method": "getSessionTypeOptions",
            "isContinuation": False,
            "cacheable": False,
        })
        return result if isinstance(result, list) else []

    # ── Composite fetch ─────────────────────────────────────────────────────

    def fetch_timetable(
        self,
        email: str,
        password: str,
        now: datetime | None = None,
        enrich_details: bool = False,
    ) -> tuple[list[TimetableEvent], dict]:
        """
        High-level entry point: login → fetch sessions → enrich → parse.

        Returns ``(events, user_info)`` where user_info is the raw dict from
        ``getUserInfo``.

        ``enrich_details`` controls whether we make per-session
        ``getSessionDetails`` calls to get classroom/location.
        """
        self.login(email, password)

        user_info = self.fetch_user_info()

        # Build a 14-day date window identical to TCS behaviour
        start = now or datetime.now()
        end   = start + timedelta(days=SYNC_WINDOW_DAYS)
        start_str = start.strftime("%Y-%m-%d")
        end_str   = end.strftime("%Y-%m-%d")

        raw_sessions = self.fetch_enrolled_sessions(start_str, end_str)

        # Optionally enrich each session with classroom/location detail
        details_map: dict[str, dict] = {}
        if enrich_details and raw_sessions:
            for sess in raw_sessions:
                sid = sess.get("id", "")
                if sid:
                    try:
                        details_map[sid] = self.fetch_session_detail(sid)
                    except SppError:
                        pass  # Best-effort; location will just be empty

        events = parse_spp_sessions(raw_sessions, details_map=details_map)
        return events, user_info


# ── Google Calendar payload override for SPP ───────────────────────────────────

def spp_google_payload(event: TimetableEvent) -> dict:
    """
    Build a Google Calendar event payload for an SPP event.

    Identical shape to TimetableEvent.google_payload() but uses the SPJIMR
    SPP source label and includes the session colour from the SPP portal.
    """
    # Determine title prefix based on activity type
    activity = (event.activity_name or "").lower()
    is_exam = any(kw in activity for kw in ("end term", "mid term", "quiz", "exam", "retest", "make up"))

    if is_exam:
        prefix = "📝 EXAM"
        if event.mandatory:
            prefix = "🔴 MANDATORY EXAM"
    elif event.mandatory:
        prefix = "🔴 MANDATORY"
    else:
        prefix = None

    title = f"{prefix}: {event.subject_name}" if prefix else event.subject_name

    description_lines = [
        f"Faculty: {event.faculty or '-'}",
        f"Room: {event.classroom or '-'}",
        f"Activity: {event.activity_name or '-'}",
        f"Source: {SOURCE_SPP}",
    ]
    if event.mandatory:
        description_lines.insert(0, "⚠️ MANDATORY SESSION — Attendance compulsory")
    if event.session_number:
        description_lines.append(f"Session No: {event.session_number}")

    payload: dict = {
        "summary": title,
        "description": "\n".join(description_lines),
        "start": {"dateTime": event.starts_at.isoformat(), "timeZone": "Asia/Kolkata"},
        "end":   {"dateTime": event.ends_at.isoformat(),   "timeZone": "Asia/Kolkata"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 15}],
        },
        "extendedProperties": {
            "private": {
                "classSyncUid": event.uid,
                "mandatory": "true" if event.mandatory else "false",
                "courseCode": event.course_code or "",
                "sessionNumber": event.session_number or "",
                "activityName": event.activity_name or "",
                "isEvaluation": "true" if is_exam else "false",
                "source": SOURCE_SPP,
            }
        },
    }
    # Colour: mandatory → red (11), exam → orange (6), else no colorId
    # (the calendar itself has the SPJIMR purple colour set at creation time)
    if event.mandatory:
        payload["colorId"] = "11"   # Tomato / Red
    elif is_exam:
        payload["colorId"] = "6"    # Tangerine / Orange
    return payload
