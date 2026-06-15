from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import unescape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

from .models import TimetableEvent, in_sync_window, SYNC_WINDOW_DAYS


TCS_ENTRY_URL = "https://g21.tcsion.com/SelfServices/"
TCS_DEFAULT_BASE_URL = "https://g21.tcsion.com"
ORG_ID = "2782"
DASHBOARD_APP_ID = "9517"
TIMETABLE_APP_ID = "9520"
TIMETABLE_COMPONENT_ID = "4700253"
TIMETABLE_ENTITY_TYPE_ID = "101782"


class TcsError(RuntimeError):
    pass


def clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"-", "null", "None"}:
        return ""
    return re.sub(r"\s+", " ", text)


def parse_tcs_datetime(date_value: str, time_value: str) -> datetime:
    date_part = date_value.split(" ")[0]
    year, month, day = [int(part) for part in date_part.split("-")]
    match = re.match(r"^(\d{1,2}):(\d{2})\s*([ap]m)$", time_value.strip(), re.I)
    if not match:
        raise ValueError(f"Unsupported TCS time: {time_value}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    suffix = match.group(3).lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return datetime(year, month, day, hour, minute)


def parse_tcs_attendance(payload: str | list[dict]) -> list[TimetableEvent]:
    rows = json.loads(payload) if isinstance(payload, str) else payload
    events: list[TimetableEvent] = []
    for index, row in enumerate(rows):
        item = row.get("Item1", row)
        date_value = clean(item.get("dateval"))
        start_time = clean(item.get("start_time"))
        end_time = clean(item.get("end_time"))
        subject = clean(item.get("sudsubjectname")) or clean(item.get("sudsubjectshortcode"))
        if not (date_value and start_time and end_time and subject):
            continue
        starts_at = parse_tcs_datetime(date_value, start_time)
        ends_at = parse_tcs_datetime(date_value, end_time)
        course_code = clean(item.get("sudsubjectshortcode")) or clean(item.get("sudsubjectcode"))
        faculty = clean(item.get("sudfacultyname"))
        classroom = clean(item.get("sudresourcename")) or clean(item.get("venue"))
        # Extract session number from the "Remarks" field in TCS iON
        # TCS iON typically stores session number as remarks, sessionno, or sessno
        session_number = clean(
            item.get("remarks")
            or item.get("sessionno")
            or item.get("sessno")
            or item.get("sessionNumber")
            or item.get("session_no")
            or item.get("SessionNo")
        )
        # Normalise: extract just the digits if it looks like "Session 9" or "9"
        if session_number:
            num_match = re.search(r"(\d+)", session_number)
            session_number = num_match.group(1) if num_match else session_number
        uid_bits = [
            clean(item.get("dateindex")) or starts_at.strftime("%Y%m%d"),
            starts_at.strftime("%H%M"),
            course_code or subject,
            classroom,
            str(index),
        ]
        uid = "|".join(uid_bits)
        events.append(
            TimetableEvent(
                uid=uid,
                subject_name=subject,
                course_code=course_code,
                faculty=faculty,
                classroom=classroom,
                starts_at=starts_at,
                ends_at=ends_at,
                status=clean(item.get("attendanceStatus") or item.get("status")),
                session_number=session_number,
            )
        )
    return events


def apply_mandatory_flags(
    events: list[TimetableEvent],
    mandatory_sessions: dict[str, list[int]],
) -> list[TimetableEvent]:
    """
    Return a new list of events with the mandatory flag set where appropriate.

    Args:
        events: list of TimetableEvent (from TCS iON)
        mandatory_sessions: dict mapping course_code → list of mandatory session numbers
                            (from Wisenet PDF parsing)
    """
    result = []
    for event in events:
        # Normalise course code for lookup
        # TCS code may be "FIN521-PDM-46" or "FIN521"; Wisenet key is "FIN521"
        code = event.course_code.split("-")[0].strip().upper()
        mandatory_nums = mandatory_sessions.get(code, [])
        is_mandatory = False
        if mandatory_nums and event.session_number:
            try:
                sess_int = int(event.session_number)
                is_mandatory = sess_int in mandatory_nums
            except ValueError:
                pass
        if is_mandatory and not event.mandatory:
            # Create a new frozen event with mandatory=True
            event = TimetableEvent(
                uid=event.uid,
                subject_name=event.subject_name,
                course_code=event.course_code,
                faculty=event.faculty,
                classroom=event.classroom,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                status=event.status,
                mandatory=True,
                session_number=event.session_number,
            )
        result.append(event)
    return result


class TcsClient:
    def __init__(self, base_url: str = TCS_DEFAULT_BASE_URL, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.lk = "Wux_9qd2mnIqiEeG9O0VzH3W_VITnxR8MXU07uY-"

    def login(self, username: str, password: str) -> None:
        if not username or not password:
            raise TcsError("TCS iON username and password are required.")
        self.session.headers.update(
            {
                "User-Agent": "ClassSync/1.0",
                "Referer": TCS_ENTRY_URL,
            }
        )
        self.session.get(f"{self.base_url}/SelfServices/", timeout=30)
        self.session.post(f"{self.base_url}/SMBPortal/deviceExist?loginType=5", timeout=30)
        response = self.session.post(
            f"{self.base_url}/Login/Login",
            data={
                "accountname": username,
                "password": encrypt_password(password),
                "loginType": "5",
                "remember_Me": "0",
                "device": "login",
                "isPasswordEncrypted": "1",
                "ssPageid": "",
            },
            allow_redirects=False,
            timeout=30,
        )
        if response.status_code != 302:
            if is_login_failure(response):
                raise TcsError("TCS iON rejected the credentials. Please verify the user ID and password by logging into TCS iON manually.")
            raise TcsError(f"TCS iON login returned HTTP {response.status_code} instead of a redirect.")
        next_url = response.headers.get("Location", "")
        if not next_url:
            raise TcsError("TCS iON login did not return an intermediate redirect.")
        response = self.session.get(next_url, allow_redirects=False, timeout=30)
        if response.status_code == 302 and "PrivacyPolicyCapturePage" in response.headers.get("Location", ""):
            privacy_url = response.headers["Location"]
            self.session.get(privacy_url, timeout=30)
            self.session.get(f"{self.base_url}/Login/getVersion", headers={"Content-Type": "application/json;charset=UTF-8"}, timeout=30)
            self.session.post(f"{self.base_url}/Login/getPrivacyPolicyDetails", json={}, timeout=30)
            self.session.post(
                f"{self.base_url}/ConsentManagement/getPolicyDetails.do?",
                json={
                    "paramOrgId": ORG_ID,
                    "paramAppId": DASHBOARD_APP_ID,
                    "paramUserId": self.user_id(),
                    "isLink": 0,
                    "isInsideSolution": 1,
                    "isRedirect": "true",
                    "applicabilityArr": [],
                    "solutionSpecificConsentDB": "0",
                    "isNewPortal": "Yes",
                },
                timeout=30,
            )
            response = self.session.get(f"{self.base_url}/Login/intermediatePage", allow_redirects=False, timeout=30)
        if response.status_code == 302:
            response = self.session.get(response.headers["Location"], allow_redirects=True, timeout=30)
            for hop in [*response.history, response]:
                parsed_hop = urlparse(hop.url)
                query = parsed_hop.query
                match = re.search(r"(?:^|&)LK=([^&]+)", query)
                if match:
                    self.lk = match.group(1)
        if is_login_failure(response):
            raise TcsError("TCS iON rejected the credentials. Please verify the user ID and password by logging into TCS iON manually.")
        if "SelfServices/home" not in response.url and "SelfServices" not in response.text:
            raise TcsError("TCS iON login did not reach the student portal. A fresh login HAR including the credential submit step is required.")
        parsed = urlparse(response.url)
        if parsed.scheme and parsed.netloc:
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"

    def fetch_attendance_payload(self, now: datetime | None = None) -> str:
        self.bootstrap_dashboard()
        self.open_timetable_app()
        # Warm-up steps — failures here don't block timetable retrieval.
        try:
            self.post_attendance(
                {
                    "REFERENCE_ID": "cms_03621",
                    "orgId": ORG_ID,
                    "permissionId": "106429",
                    "entityTypeId": "101762",
                    "userId": self.user_id(),
                    "appid": TIMETABLE_APP_ID,
                    "operation": "syncmessages",
                    "formId": TIMETABLE_ENTITY_TYPE_ID,
                }
            )
        except TcsError:
            pass
        self.post_attendance({"REFERENCE_ID": "cms_05340", "permissionId": "100733", "entityTypeId": "100138"})
        sgm_response = self.post_attendance(
            {
                "className": "com.tcs.cmstimetable.action.timetable.StudentTimetable.StudentTimetableNew",
                "methodName": "getAllSGMs",
                "permissionId": "106554",
                "entityTypeId": TIMETABLE_ENTITY_TYPE_ID,
                "userID": self.user_id(),
            }
        )
        # Build a date window: today through today + SYNC_WINDOW_DAYS, formatted as YYYYMMDD
        start_dt = now or datetime.now()
        end_dt = start_dt + timedelta(days=SYNC_WINDOW_DAYS)
        startdate = start_dt.strftime("%Y%m%d")
        enddate = end_dt.strftime("%Y%m%d")
        startdate_ts = start_dt.strftime("%Y-%m-%d")
        enddate_ts = end_dt.strftime("%Y-%m-%d")
        # Resolve the correct exam session ID.
        exam_session_id = self._pick_sgm_id(sgm_response.text, self, startdate, enddate, startdate_ts, enddate_ts)
        # Fetch the actual timetable JSON with date range and session ID
        response = self.session.post(
            f"{self.base_url}/cms/AttendancePeriodWiseServlet",
            params={
                "REFERENCE_ID": "cms_03954",
                "permissionId": "106554",
                "entityTypeId": TIMETABLE_ENTITY_TYPE_ID,
                "startdate": startdate,
                "enddate": enddate,
                "startdateTS": startdate_ts,
                "enddateTS": enddate_ts,
                "showAcademicSlots": "no",
                "screen": "c",
                "examSessionId": exam_session_id,
                "isOpen": "N",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise TcsError(f"TCS timetable fetch failed with HTTP {response.status_code}.")
        if not response.text.lstrip().startswith("["):
            raise TcsError(
                f"TCS timetable response was not JSON (examSessionId={exam_session_id}). "
                f"Got: {response.text[:120]}"
            )
        return response.text

    @staticmethod
    def _pick_sgm_id(
        sgm_response_text: str,
        client: "TcsClient",
        startdate: str,
        enddate: str,
        startdate_ts: str,
        enddate_ts: str,
    ) -> str:
        """Resolve the exam session ID to use.

        Strategy:
        1. If getAllSGMs returned a valid JSON dict, pick the highest numeric key.
        2. Otherwise probe each candidate ID (known from HAR: 379, 378, 377, 374)
           until one returns a JSON array from AttendancePeriodWiseServlet.
        """
        # --- Strategy 1: parse getAllSGMs response ---
        try:
            sgms = json.loads(sgm_response_text)
            if sgms and isinstance(sgms, dict):
                ids = sorted(sgms.keys(), key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
                return ids[0]
        except (json.JSONDecodeError, ValueError):
            pass

        # --- Strategy 2: probe known candidate IDs ---
        candidates = ["379", "378", "377", "374", "380", "376", "375"]
        for cid in candidates:
            try:
                r = client.session.post(
                    f"{client.base_url}/cms/AttendancePeriodWiseServlet",
                    params={
                        "REFERENCE_ID": "cms_03954",
                        "permissionId": "106554",
                        "entityTypeId": TIMETABLE_ENTITY_TYPE_ID,
                        "startdate": startdate,
                        "enddate": enddate,
                        "startdateTS": startdate_ts,
                        "enddateTS": enddate_ts,
                        "showAcademicSlots": "no",
                        "screen": "c",
                        "examSessionId": cid,
                        "isOpen": "N",
                    },
                    timeout=30,
                )
                if r.status_code == 200 and r.text.lstrip().startswith("["):
                    return cid
            except Exception:
                continue
        raise TcsError(
            "Could not determine a valid TCS exam session ID. "
            "The timetable may not be available for the current date range."
        )

    def bootstrap_dashboard(self) -> None:
        urn = self.onload_token()
        launch_key = self.insert_launch_key("9505", urn)
        urn = self.onload_token()
        dashboard_launch_key = self.insert_launch_key(DASHBOARD_APP_ID, urn)
        urn = self.onload_token()
        response = self.session.post(
            f"{self.base_url}/DICEDataform/DiceSession.ddf",
            data={"AppID": DASHBOARD_APP_ID, "launchKey": dashboard_launch_key, "urn": urn},
            timeout=30,
        )
        if "<status>success</status>" not in response.text:
            raise TcsError("TCS dashboard session did not start successfully.")
        self.onload_token()
        self.session.post(
            f"{self.base_url}/SelfServices/EssOnLoadServlet",
            data={"SubAction": "listCategory", "defaultFlag": "1", "urn": urn},
            timeout=30,
        )
        self.session.post(
            f"{self.base_url}/UCP/UcpUtilAction.do?method=getOrgUserPushFlagCheck",
            data={"launchKey": launch_key},
            timeout=30,
        )

    def open_timetable_app(self) -> None:
        urn = self.urn_token()
        response = self.session.post(
            f"{self.base_url}/SelfServices/LandingPageDetailsServlet",
            data={
                "subAction": "validateComponentAccess",
                "compType": "Quicklink",
                "compId": TIMETABLE_COMPONENT_ID,
                "urn": urn,
            },
            timeout=30,
        )
        if TIMETABLE_APP_ID not in response.text:
            raise TcsError("TCS timetable component access was not validated.")
        urn = self.urn_token()
        launch_key = self.session.post(
            f"{self.base_url}/SelfServices/LandingPageDetailsServlet",
            data={"subAction": "insertLaunchKey", "targetSolutionId": TIMETABLE_APP_ID, "urn": urn},
            timeout=30,
        ).text.strip()
        if not launch_key:
            raise TcsError("TCS did not return a timetable launch key.")
        app_login_url = (
            f"{self.base_url}/DICEDataform/ApplicationLogin.ddf"
            f"?solname=SS&AppID={TIMETABLE_APP_ID}&SsTabId=8984264&entityid={TIMETABLE_ENTITY_TYPE_ID}"
            f"&screentype=search&launchKey={launch_key}&AppID={TIMETABLE_APP_ID}"
            f"&LK={self.lk}&timezoneId=Asia/Kolkata"
        )
        response = self.session.get(app_login_url, allow_redirects=True, timeout=30)
        if "cmsStudentTimetableNewUI" not in response.url and "cmsStudentTimetableNewUI" not in response.text:
            raise TcsError("TCS timetable application session did not open.")

    def onload_token(self) -> str:
        response = self.session.post(
            f"{self.base_url}/SelfServices/EssOnLoadServlet",
            data={"SubAction": "onloadtoken"},
            timeout=30,
        )
        token = response.text.strip()
        if not token:
            raise TcsError("TCS did not return an onload URN token.")
        return token

    def urn_token(self) -> str:
        response = self.session.post(
            f"{self.base_url}/SelfServices/getURNToken.do",
            data={"urnFlag": "false"},
            timeout=30,
        )
        token = response.text.strip()
        if not token:
            raise TcsError("TCS did not return a URN token.")
        return token

    def insert_launch_key(self, solution_id: str, urn: str) -> str:
        response = self.session.post(
            f"{self.base_url}/SelfServices/EssOnLoadServlet",
            data={"SubAction": "insertLaunchKey", "WidgetSolutionId": solution_id, "urn": urn},
            timeout=30,
        )
        match = re.search(r"<launchkeyID>(.*?)</launchkeyID>", response.text)
        if not match:
            raise TcsError(f"TCS did not return a launch key for solution {solution_id}.")
        return match.group(1)

    def post_attendance(self, data: dict[str, str]) -> requests.Response:
        response = self.session.post(f"{self.base_url}/cms/AttendancePeriodWiseServlet", data=data, timeout=30)
        if response.status_code != 200:
            raise TcsError(f"TCS timetable fetch failed with HTTP {response.status_code}.")
        return response

    def user_id(self) -> str:
        for cookie in self.session.cookies:
            if cookie.name.lower() in {"userid", "user_id", "uid", "loginid"} and cookie.value:
                return cookie.value
        # Fallback: read from the session cookie jar by a broader scan
        for cookie in self.session.cookies:
            if "user" in cookie.name.lower() and cookie.value and cookie.value.isdigit():
                return cookie.value
        return ""

    def fetch_timetable(self, username: str, password: str, now: datetime | None = None) -> list[TimetableEvent]:
        self.login(username, password)
        return parse_tcs_attendance(self.fetch_attendance_payload(now=now))


def next_two_weeks(events: Iterable[TimetableEvent], now: datetime | None = None) -> list[TimetableEvent]:
    return [event for event in events if in_sync_window(event, now=now)]


def find_login_form(html: str) -> str:
    match = re.search(r"<form[^>]+name=['\"]login['\"][\s\S]*?</form>", html, flags=re.I)
    if not match:
        raise TcsError("TCS iON login form was not found.")
    return match.group(0)


def html_inputs(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]+>", html, flags=re.I):
        name = html_attr(tag, "name")
        if name:
            fields[name] = html_attr(tag, "value")
    return fields


def html_attr(tag: str, name: str) -> str:
    match = re.search(rf"""{name}\s*=\s*(['"])(.*?)\1|{name}\s*=\s*([^\s>]+)""", tag, flags=re.I)
    if not match:
        return ""
    return unescape(match.group(2) or match.group(3) or "").strip()


def is_login_failure(response: requests.Response) -> bool:
    text = response.text.lower()
    return "loginfailure" in response.url.lower() or "invalid id/ password" in text or "login failed" in text


def encrypt_password(password: str) -> str:
    text = password + "fdledje4p2aga6gtfgq2ce"
    encoded = "".join(chr(ord(char) + 4) for char in text)
    return encoded[-2:] + encoded[2:-2] + encoded[:2]


def serialize_events(events: Iterable[TimetableEvent]) -> list[dict]:
    return [
        {
            "uid": event.uid,
            "title": event.title,
            "course_code": event.course_code,
            "faculty": event.faculty,
            "classroom": event.classroom,
            "starts_at": event.starts_at.isoformat(),
            "ends_at": event.ends_at.isoformat(),
            "status": event.status,
            "description": event.description,
            "mandatory": event.mandatory,
            "session_number": event.session_number,
        }
        for event in events
    ]
