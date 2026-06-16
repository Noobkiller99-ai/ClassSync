"""
wisenet.py — Wisenet (Moodle LMS) client for SPJIMR.

Handles:
  1. Login via Google SSO (SAML2) — two modes:
       a. Server-side token flow: uses the stored Google OAuth access_token +
          OAuthLogin endpoint to establish a Google session, then completes
          the SAML2 handshake via requests (no browser, no display, works on Render)
       b. Browser popup fallback (local only): Playwright headed browser where
          the user clicks their SPJIMR Google account
  2. Fetching enrolled courses via Moodle AJAX API
  3. Downloading Course Outline PDFs from each course
  4. Parsing mandatory sessions from those PDFs
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

import requests

logger = logging.getLogger(__name__)

WISENET_BASE = "https://wisenet.spjimr.org"
LOGIN_URL = f"{WISENET_BASE}/login/index.php"
AJAX_URL = f"{WISENET_BASE}/lib/ajax/service.php"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class WisenetCourse:
    id: int
    fullname: str
    shortname: str          # e.g. "FIN521-PDM-46"
    course_code: str = ""   # extracted from shortname, e.g. "FIN521"

    def __post_init__(self):
        if not self.course_code:
            # Course code is the part before the first "-" in shortname
            # e.g. "FIN521-PDM-46" → "FIN521"
            self.course_code = self.shortname.split("-")[0].strip().upper()


@dataclass
class MandatorySessionInfo:
    course_code: str
    course_shortname: str
    mandatory_sessions: list[int]  # list of session numbers (ints)


# ── Moodle AJAX helpers ───────────────────────────────────────────────────────

def _ajax_post(session: requests.Session, sesskey: str, calls: list[dict]) -> list:
    """POST to Moodle AJAX endpoint and return parsed JSON results."""
    resp = session.post(
        AJAX_URL,
        params={"sesskey": sesskey, "info": ",".join(c["methodname"] for c in calls)},
        json=[{"index": i, "methodname": c["methodname"], "args": c["args"]}
              for i, c in enumerate(calls)],
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data:
        if item.get("error"):
            raise RuntimeError(f"Moodle AJAX error: {item}")
        results.append(item.get("data"))
    return results


# ── Session / sesskey extraction ──────────────────────────────────────────────

def _extract_sesskey(html: str) -> str:
    """Extract Moodle sesskey from page HTML."""
    match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'sesskey=([A-Za-z0-9]+)', html)
    if match:
        return match.group(1)
    raise RuntimeError("Could not extract Moodle sesskey from page HTML.")


def _extract_userid(html: str) -> str:
    """Extract Moodle userid from page HTML."""
    match = re.search(r'"userid"\s*:\s*(\d+)', html)
    if match:
        return match.group(1)
    match = re.search(r'"id"\s*:\s*(\d+).*?"userpicture"', html, re.S)
    if match:
        return match.group(1)
    return ""


# ── Google-token → Wisenet session (server-side SAML2) ───────────────────────

def _ensure_fresh_token(google_creds: dict) -> str:
    """
    Return a valid Google access token, refreshing it if expired.
    google_creds is the dict stored in the DB (token, refresh_token, client_id, etc.)
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials(
        token=google_creds.get("token"),
        refresh_token=google_creds.get("refresh_token"),
        token_uri=google_creds.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=google_creds.get("client_id"),
        client_secret=google_creds.get("client_secret"),
        scopes=google_creds.get("scopes"),
    )
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def login_via_google_token(google_creds: dict) -> tuple[dict, str, str]:
    """
    Log into Wisenet silently using a stored Google OAuth credential dict.

    This is a fully server-side flow — no browser popup, no display, no
    cookie pasting. Works on Render and any headless environment.

    Flow:
      1. Refresh the Google access token if needed
      2. Call Google's OAuthLogin endpoint to convert the OAuth token
         into browser-style Google session cookies (SID, HSID, etc.)
      3. Use those Google cookies to navigate to Wisenet's SAML2 SSO URL
      4. Google auto-authenticates (no user interaction needed because we
         already have a valid Google session) and returns an HTML form
         containing a SAMLResponse assertion
      5. POST the SAMLResponse to Wisenet's ACS (Assertion Consumer Service)
      6. Wisenet validates and sets MoodleSession cookie — we're in!

    Returns:
        (cookies_dict, sesskey, userid)
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })

    # ── Step 1: get a fresh access token ─────────────────────────────────────
    try:
        access_token = _ensure_fresh_token(google_creds)
    except Exception as exc:
        raise RuntimeError(
            f"Could not refresh Google token: {exc}. "
            "Please reconnect Google Calendar first."
        ) from exc

    # ── Step 2: OAuthLogin — convert OAuth token → Google session cookies ─────
    # This endpoint is used by Chrome browser sync, oauth2l, etc.
    try:
        uber_r = sess.get(
            "https://accounts.google.com/OAuthLogin",
            params={"source": "ChromiumBrowser", "issueuberauth": "1"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        uber_r.raise_for_status()
        uberauth = uber_r.text.strip()
        if not uberauth:
            raise RuntimeError("OAuthLogin returned empty uber-auth token")
    except Exception as exc:
        raise RuntimeError(f"Google OAuthLogin step failed: {exc}") from exc

    try:
        sess.get(
            "https://accounts.google.com/MergeSession",
            params={
                "uberauth": uberauth,
                "continue": "https://accounts.google.com/",
                "source": "ChromiumBrowser",
            },
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"Google MergeSession step failed: {exc}") from exc
    # sess.cookies now holds SID, HSID, SSID, APISID, SAPISID, SIDCC...

    # ── Step 3: navigate Wisenet login → find SSO URL ─────────────────────────
    try:
        login_r = sess.get(LOGIN_URL, allow_redirects=True, timeout=20)
        login_r.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Could not reach Wisenet login page: {exc}") from exc

    # Find the SAML2/SSO button URL in the page
    saml_url: str | None = None
    for pattern in [
        r'href="([^"]*auth/saml2[^"]*)"',
        r'href="([^"]*sso[^"]*)"',
        r'href="([^"]*google[^"]*)"',
    ]:
        m = re.search(pattern, login_r.text, re.I)
        if m:
            href = m.group(1)
            saml_url = WISENET_BASE + href if href.startswith("/") else href
            break

    # Also try grabbing any Google SSO link from anchors
    if not saml_url:
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>', login_r.text, re.I):
            href = m.group(1)
            if any(kw in href.lower() for kw in ("saml", "sso", "google", "oauth")):
                saml_url = WISENET_BASE + href if href.startswith("/") else href
                break

    if not saml_url:
        raise RuntimeError(
            "Could not find the Google SSO button on the Wisenet login page. "
            "The page structure may have changed."
        )

    logger.info("Wisenet SSO URL: %s", saml_url)

    # ── Step 4: follow SAML2 flow — Google auto-authenticates ─────────────────
    try:
        saml_r = sess.get(saml_url, allow_redirects=True, timeout=30)
    except Exception as exc:
        raise RuntimeError(f"SAML2 redirect failed: {exc}") from exc

    # Google returns an auto-submitting HTML form with SAMLResponse + RelayState.
    # We need to parse it and POST it manually (no JS engine in requests).
    saml_response: str | None = None
    relay_state: str | None = None
    acs_url: str | None = None

    def _parse_saml_form(html: str) -> tuple[str | None, str | None, str | None]:
        """
        Parse a SAML response form from HTML.
        Returns (acs_url, saml_response, relay_state) or (None, None, None).
        Handles:
          - HTML-encoded values (Google encodes + as &#43;, = as &#61;, etc.)
          - Both double- and single-quoted attribute values
          - name/value in either order in <input>
        """
        from html import unescape

        form_m = re.search(
            r'<form[^>]+action=["\']([^"\']+)["\'][^>]*>(.*?)</form>',
            html, re.S | re.I,
        )
        if not form_m:
            return None, None, None

        found_acs = form_m.group(1)
        form_body = form_m.group(2)

        # Match <input ...> tags — attributes may appear in any order, may use ' or "
        inputs: dict[str, str] = {}
        for inp in re.finditer(r'<input\b([^>]+)>', form_body, re.I):
            attrs_str = inp.group(1)
            name_m = re.search(r'\bname=["\']([^"\']*)["\']', attrs_str, re.I)
            val_m  = re.search(r'\bvalue=["\']([^"\']*)["\']', attrs_str, re.I)
            if name_m and val_m:
                inputs[name_m.group(1)] = unescape(val_m.group(1))

        return (
            found_acs,
            inputs.get("SAMLResponse"),
            inputs.get("RelayState"),
        )

    # Scan final response + all redirect history for the SAMLResponse form
    pages_to_scan = [saml_r] + list(saml_r.history)
    for page in pages_to_scan:
        acs_url, saml_response, relay_state = _parse_saml_form(page.text)
        if saml_response:
            logger.debug("SAMLResponse found (len=%d) at %s", len(saml_response), page.url)
            break

    if not saml_response or not acs_url:
        # Check if we're already on Wisenet (sometimes SSO skips the SAML form
        # because the session is already established by the Google cookies)
        wisenet_cookies_now = {
            c.name: c.value
            for c in sess.cookies
            if "wisenet.spjimr.org" in (c.domain or "")
        }
        if wisenet_cookies_now.get("MoodleSession"):
            logger.info("SSO completed without explicit SAMLResponse POST (session already set)")
        else:
            # Log a snippet of the last response to aid debugging
            snippet = saml_r.text[:800].replace("\n", " ")
            logger.warning("SAML2 last page URL=%s snippet=%s", saml_r.url, snippet)
            raise RuntimeError(
                "Wisenet SSO flow did not produce a SAMLResponse. "
                "This usually means Google showed an account picker or a consent screen "
                "that requires user interaction. "
                "Reconnect Google Calendar (Disconnect → re-authorise with your SPJIMR "
                "account) to refresh permissions, then try again."
            )
    else:
        # ── Step 5: POST SAMLResponse to Wisenet ACS ──────────────────────────
        post_data: dict[str, str] = {"SAMLResponse": saml_response}
        if relay_state:
            post_data["RelayState"] = relay_state

        try:
            final_r = sess.post(acs_url, data=post_data, allow_redirects=True, timeout=20)
            final_r.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"SAML2 ACS POST failed: {exc}") from exc

    # ── Step 6: collect Wisenet session cookies + sesskey + userid ────────────
    wisenet_cookies = {
        c.name: c.value
        for c in sess.cookies
        if "wisenet.spjimr.org" in (c.domain or "")
    }
    if not wisenet_cookies.get("MoodleSession"):
        raise RuntimeError(
            "Wisenet login did not produce a MoodleSession cookie. "
            "The SSO flow may have failed silently."
        )

    # Navigate to /my/ to get sesskey and userid reliably
    try:
        my_r = sess.get(f"{WISENET_BASE}/my/", allow_redirects=True, timeout=20)
        html = my_r.text
    except Exception:
        html = ""

    try:
        sesskey = _extract_sesskey(html)
    except RuntimeError:
        sesskey = ""
    userid = _extract_userid(html)

    logger.info(
        "Wisenet login via Google token: userid=%s, sesskey=%s…, cookies=%s",
        userid, sesskey[:8] if sesskey else "", list(wisenet_cookies.keys()),
    )
    return wisenet_cookies, sesskey, userid


def parse_mandatory_sessions_from_pdf(pdf_bytes: bytes, course_shortname: str) -> MandatorySessionInfo:
    """
    Parse a Course Outline PDF and extract session numbers marked as mandatory.

    The session plan table (spanning multiple pages) has columns:
        Session No & Faculty | ... | Mandatory Sessions

    The "Mandatory Sessions" column contains:
        - "Yes"              → all sessions in that row are mandatory
        - "Session N - Yes"  → only session N is mandatory
        - ""                 → not mandatory

    Because the table spans pages, we use two strategies:
    1. On any page that has the "Mandatory Sessions" header, we identify the
       exact column indices.
    2. On continuation pages (no header visible), we assume the table has the
       same structure: first column = session numbers, last column = mandatory flag.
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is not installed. Run: pip install pdfplumber")

    course_code = course_shortname.split("-")[0].strip().upper()
    mandatory: list[int] = []

    # Global column config discovered from header row
    mandatory_col_idx: int | None = None
    session_col_idx: int | None = None
    in_session_table = False  # True once we've found the session plan table

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue

                # ── Step 1: look for the header row in this table ──────────
                local_mandatory_col = mandatory_col_idx
                local_session_col = session_col_idx
                data_start_row = 0

                for row_idx, row in enumerate(table):
                    # Merge cells vertically across next 3 rows to handle split headers
                    col_count = len(row)
                    merged_cells = [""] * col_count
                    for next_row_idx in range(row_idx, min(row_idx + 3, len(table))):
                        next_row = table[next_row_idx]
                        if not next_row or len(next_row) != col_count:
                            continue
                        for ci, cell in enumerate(next_row):
                            val = str(cell).replace("\n", " ").strip() if cell else ""
                            if val:
                                merged_cells[ci] += " " + val
                    
                    merged_cells = [c.strip().lower() for c in merged_cells]
                    
                    # Detect header row: a merged column must contain "mandatory" AND "session"
                    if any("mandatory" in c and "session" in c for c in merged_cells):
                        for ci, cell in enumerate(merged_cells):
                            if "mandatory" in cell and "session" in cell:
                                local_mandatory_col = ci
                                in_session_table = True
                                break
                        # Session numbers always appear in col 0 in SPJIMR course outlines
                        # (the header for that col is empty or has "Session No &")
                        local_session_col = 0
                        # Store globally for subsequent pages
                        mandatory_col_idx = local_mandatory_col
                        session_col_idx = local_session_col
                        data_start_row = row_idx + 1
                        # Skip over sub-header/merged header rows (rows with no digit in col 0)
                        while data_start_row < len(table):
                            first_cell = str(table[data_start_row][0] or "").strip()
                            if re.search(r"\d", first_cell):
                                break
                            data_start_row += 1
                        break
                else:
                    # No header found — check if this looks like a continuation table
                    # (i.e., we already found the session table on a previous page)
                    if not in_session_table:
                        continue
                    # For continuation pages: use first col as session, last col as mandatory
                    local_session_col = 0
                    local_mandatory_col = len(table[0]) - 1
                    data_start_row = 0

                # ── Step 2: parse data rows ────────────────────────────────
                for row in table[data_start_row:]:
                    cells = [str(c).replace("\n", " ").strip() if c else "" for c in row]
                    if not cells:
                        continue

                    # Bounds check
                    if local_session_col >= len(cells) or local_mandatory_col >= len(cells):
                        # Try last column for mandatory
                        if len(cells) < 2:
                            continue
                        local_mandatory_col = len(cells) - 1
                        local_session_col = 0

                    session_cell = cells[local_session_col].strip()
                    mandatory_cell = cells[local_mandatory_col].strip().lower()

                    # Skip rows with no session number
                    if not re.search(r"\d", session_cell):
                        continue
                    # Skip rows with no mandatory indicator
                    if not mandatory_cell or mandatory_cell in {"-", "no", "n/a", "na"}:
                        continue
                    # Skip rows where mandatory cell has no "yes"
                    if "yes" not in mandatory_cell:
                        continue

                    # Parse session numbers from the session cell
                    session_nums = _parse_session_nums(session_cell)

                    # Determine which sessions are mandatory
                    if mandatory_cell == "yes":
                        # All sessions in this row are mandatory
                        mandatory.extend(session_nums)
                    else:
                        # e.g. "session 9 - yes" → only session 9
                        specific = _parse_session_nums(mandatory_cell)
                        if specific:
                            mandatory.extend(specific)
                        else:
                            mandatory.extend(session_nums)

    # Deduplicate and sort
    mandatory = sorted(set(mandatory))
    return MandatorySessionInfo(
        course_code=course_code,
        course_shortname=course_shortname,
        mandatory_sessions=mandatory,
    )


def _parse_session_nums(text: str) -> list[int]:
    """Extract all integers from a string like '1, 2' or '9, 10, 11' or 'Session 9 - Yes'."""
    return [int(m) for m in re.findall(r"\d+", text)]



# ── Wisenet HTTP client (post-login, uses cookies) ────────────────────────────

class WisenetClient:
    """
    Wisenet Moodle client that uses an authenticated requests.Session.
    The session is obtained by logging in via Playwright (Google SSO).
    """

    def __init__(self, session: requests.Session, sesskey: str, userid: str):
        self.session = session
        self.sesskey = sesskey
        self.userid = userid

    def get_enrolled_courses(self) -> list[WisenetCourse]:
        """Return all in-progress enrolled courses."""
        results = _ajax_post(
            self.session,
            self.sesskey,
            [
                {
                    "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                    "args": {
                        "offset": 0,
                        "limit": 0,
                        "classification": "inprogress",
                        "sort": "fullname",
                        "customfieldname": "",
                        "customfieldvalue": "",
                    },
                }
            ],
        )
        data = results[0] if results else {}
        courses_raw = data.get("courses", []) if isinstance(data, dict) else []
        courses = []
        for c in courses_raw:
            courses.append(
                WisenetCourse(
                    id=int(c.get("id", 0)),
                    fullname=c.get("fullname", ""),
                    shortname=c.get("shortname", ""),
                )
            )
        return courses

    def get_course_page_html(self, course_id: int) -> str:
        """Fetch the HTML of a course's main page."""
        resp = self.session.get(
            f"{WISENET_BASE}/course/view.php",
            params={"id": course_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text

    def find_course_outline_pdf_url(self, course_html: str) -> str | None:
        """
        Find the URL of a resource labelled 'Course Outline' in the course HTML.
        Returns the mod/resource view URL or the pluginfile URL directly.
        """
        # Look for links containing "course outline" in the anchor text or href
        # Pattern: <a href="/mod/resource/view.php?id=XXXX">Course Outline</a>
        # or any PDF link with "course" and "outline" in its name
        patterns = [
            # Resource view links
            r'href="([^"]*mod/resource/view\.php\?id=(\d+))[^"]*"[^>]*>([^<]*(?:course\s*outline|co\s*[-–])[^<]*)</a',
            # Direct pluginfile PDF links with outline/CO in name
            r'href="([^"]*pluginfile\.php[^"]*(?:course.?outline|PGDM.CO)[^"]*\.pdf[^"]*)"',
        ]
        html_lower = course_html.lower()

        # Method 1: Find anchor text containing "course outline"
        for match in re.finditer(
            r'<a[^>]+href="([^"]*mod/resource/view\.php\?id=(\d+))[^"]*"[^>]*>(.*?)</a',
            course_html,
            re.I | re.S,
        ):
            href, res_id, anchor_text = match.group(1), match.group(2), match.group(3)
            clean_anchor = re.sub(r"<[^>]+>", "", anchor_text).strip().lower()
            if "course outline" in clean_anchor or re.match(r"pgdm\s+co\b", clean_anchor):
                return f"{WISENET_BASE}{href}" if href.startswith("/") else href

        # Method 2: Find any PDF file link with "outline" or "CO" in name
        for match in re.finditer(
            r'href="([^"]*pluginfile[^"]*(?:outline|PGDM[^"]*CO|course[^"]*outline)[^"]*\.pdf[^"]*)"',
            course_html,
            re.I,
        ):
            return match.group(1)

        # Method 3: Find any resource that has PDF icon and contains "outline"
        # Look for section around "course outline" text
        for match in re.finditer(
            r'(mod/resource/view\.php\?id=\d+)',
            course_html,
            re.I,
        ):
            # Check the surrounding context (~200 chars) for "outline"
            start = max(0, match.start() - 200)
            end = min(len(course_html), match.end() + 200)
            context = course_html[start:end].lower()
            if "outline" in context or "pgdm co" in context:
                url = match.group(1)
                return f"{WISENET_BASE}/{url}"

        return None

    def download_pdf(self, resource_url: str) -> bytes | None:
        """
        Download a PDF via a mod/resource/view.php URL (which redirects to the actual file).
        Returns raw bytes of the PDF, or None if not a PDF.
        """
        resp = self.session.get(resource_url, allow_redirects=True, timeout=60)
        if resp.status_code != 200:
            logger.warning("PDF download failed: HTTP %s for %s", resp.status_code, resource_url)
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type and not resource_url.lower().endswith(".pdf"):
            # May have ended up at a non-PDF resource; try one more redirect
            logger.warning("Expected PDF but got %s for %s", content_type, resource_url)
            return None
        return resp.content

    def get_mandatory_sessions_for_course(self, course: WisenetCourse) -> MandatorySessionInfo | None:
        """Full pipeline: fetch course page → find PDF → download → parse."""
        try:
            html = self.get_course_page_html(course.id)
        except Exception as exc:
            logger.warning("Failed to fetch course page for %s: %s", course.shortname, exc)
            return None

        pdf_url = self.find_course_outline_pdf_url(html)
        if not pdf_url:
            logger.info("No Course Outline PDF found for %s", course.shortname)
            return None

        try:
            pdf_bytes = self.download_pdf(pdf_url)
        except Exception as exc:
            logger.warning("Failed to download PDF for %s: %s", course.shortname, exc)
            return None

        if not pdf_bytes:
            return None

        try:
            return parse_mandatory_sessions_from_pdf(pdf_bytes, course.shortname)
        except Exception as exc:
            logger.warning("Failed to parse PDF for %s: %s", course.shortname, exc)
            return None

    def get_all_mandatory_sessions(self) -> dict[str, list[int]]:
        """
        Return a dict mapping course_code → list of mandatory session numbers,
        for all enrolled in-progress courses.
        """
        courses = self.get_enrolled_courses()
        result: dict[str, list[int]] = {}
        for course in courses:
            info = self.get_mandatory_sessions_for_course(course)
            if info and info.mandatory_sessions:
                result[info.course_code] = info.mandatory_sessions
                logger.info(
                    "Course %s: mandatory sessions = %s",
                    info.course_code,
                    info.mandatory_sessions,
                )
        return result


# ── Playwright login — browser popup (no credentials required) ────────────────

def login_with_browser_popup(hint_email: str = "") -> tuple[dict, str, str]:
    """
    Log into Wisenet by opening a VISIBLE browser window where the user simply
    clicks their SPJIMR Google account. No email or password is ever typed by
    the automation — Google SSO handles authentication.

    Args:
        hint_email: optional email to pre-populate the Google account hint
                    (e.g. from the existing Google Calendar OAuth token).

    Returns:
        (cookies_dict, sesskey, userid)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. "
            "Run: pip install playwright && python -m playwright install chromium"
        )

    with sync_playwright() as pw:
        # ── Headed mode: a real browser window opens on the user's desktop ──
        browser = pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = browser.new_context(
            viewport=None,  # use the window size
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            # ── Step 1: navigate to Wisenet login ────────────────────────────
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            # ── Step 2: click the Google SSO / SAML2 button ──────────────────
            sso_selectors = [
                "a[href*='saml2']",
                "a[href*='sso']",
                "a:has-text('Google')",
                "a:has-text('Sign in with Google')",
                "a:has-text('Log in with Google')",
                "button:has-text('Google')",
                ".btn-google",
                "[data-logintype='sso']",
            ]
            sso_clicked = False
            for sel in sso_selectors:
                try:
                    elem = page.locator(sel).first
                    if elem.is_visible(timeout=2_000):
                        elem.click()
                        sso_clicked = True
                        break
                except Exception:
                    continue

            if not sso_clicked:
                hrefs = page.eval_on_selector_all("a", "els => els.map(e => e.href)")
                for href in hrefs:
                    if "google" in href.lower() or "saml" in href.lower():
                        page.goto(href, wait_until="networkidle", timeout=30_000)
                        sso_clicked = True
                        break

            # ── Step 3: optionally pre-fill the Google account hint ──────────
            # If an email hint is provided, try to fill the email field so the
            # user just has to click "Next" / confirm — no typing needed.
            if hint_email:
                try:
                    page.wait_for_selector('input[type="email"]', timeout=5_000)
                    page.fill('input[type="email"]', hint_email)
                    # Don't press Enter — let the user review and confirm
                except Exception:
                    pass  # Google may have skipped straight to account picker

            # ── Step 4: wait for user to complete login (up to 3 minutes) ────
            # The browser is visible; the user clicks their account and any
            # 2FA / consent prompts. We just wait for the redirect back.
            logger.info(
                "Wisenet: waiting for user to select Google account in browser window…"
            )
            page.wait_for_url(f"{WISENET_BASE}/**", timeout=180_000)
            page.wait_for_load_state("networkidle", timeout=20_000)

            # ── Step 5: collect cookies ───────────────────────────────────────
            cookies = context.cookies()
            cookies_dict = {
                c["name"]: c["value"]
                for c in cookies
                if "wisenet.spjimr.org" in c.get("domain", "")
            }

            # ── Step 6: extract sesskey + userid ──────────────────────────────
            html = page.content()
            try:
                sesskey = _extract_sesskey(html)
            except RuntimeError:
                sesskey = ""
            userid = _extract_userid(html)

            if not sesskey:
                page.goto(f"{WISENET_BASE}/my/", wait_until="networkidle", timeout=20_000)
                html = page.content()
                sesskey = _extract_sesskey(html)
                userid = _extract_userid(html) or userid

            if not cookies_dict:
                raise RuntimeError(
                    "Login did not produce Wisenet session cookies. "
                    "Please make sure you selected the correct SPJIMR account."
                )

            return cookies_dict, sesskey, userid

        except Exception as exc:
            raise RuntimeError(f"Wisenet browser login failed: {exc}") from exc
        finally:
            browser.close()


def build_requests_session(cookies: dict) -> requests.Session:
    """Build a requests.Session pre-loaded with Wisenet cookies."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": WISENET_BASE,
        "X-Requested-With": "XMLHttpRequest",
    })
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain="wisenet.spjimr.org")
    return sess


def build_client_from_cookies(cookies: dict, sesskey: str, userid: str) -> WisenetClient:
    """Build a WisenetClient from already-captured cookies (no login needed)."""
    session = build_requests_session(cookies)
    return WisenetClient(session=session, sesskey=sesskey, userid=userid)


def login_and_build_client(hint_email: str = "") -> WisenetClient:
    """
    Open a browser popup for Google SSO, capture cookies, return WisenetClient.
    No credentials required — the user just clicks their SPJIMR account.
    """
    cookies, sesskey, userid = login_with_browser_popup(hint_email=hint_email)
    session = build_requests_session(cookies)
    return WisenetClient(session=session, sesskey=sesskey, userid=userid)

