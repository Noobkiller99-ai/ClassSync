from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


TIMEZONE = "Asia/Kolkata"
CALENDAR_NAME = "SPJIMR Timetable"
DEFAULT_REMINDER_MINUTES = 15
SOURCE = "TCS iON"
SYNC_WINDOW_DAYS = 14

# Google Calendar color ID for Tomato/Red
MANDATORY_COLOR_ID = "11"


@dataclass(frozen=True)
class TimetableEvent:
    uid: str
    subject_name: str
    course_code: str
    faculty: str
    classroom: str
    starts_at: datetime
    ends_at: datetime
    status: str = ""
    mandatory: bool = False
    session_number: str = ""   # e.g. "9" matching "Remarks" in TCS iON

    @property
    def title(self) -> str:
        if self.mandatory:
            return f"🔴 MANDATORY: {self.subject_name}"
        return self.subject_name

    @property
    def description(self) -> str:
        lines = [
            f"Faculty: {self.faculty or '-'}",
            f"Course Code: {self.course_code or '-'}",
            f"Classroom: {self.classroom or '-'}",
            f"Source: {SOURCE}",
        ]
        if self.mandatory:
            lines.insert(0, "⚠️ MANDATORY SESSION — Attendance compulsory")
        if self.session_number:
            lines.append(f"Session No: {self.session_number}")
        return "\n".join(lines)

    def google_payload(self) -> dict:
        payload: dict = {
            "summary": self.title,
            "description": self.description,
            "start": {"dateTime": self.starts_at.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": self.ends_at.isoformat(), "timeZone": TIMEZONE},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": DEFAULT_REMINDER_MINUTES},
                ],
            },
            "extendedProperties": {
                "private": {
                    "classSyncUid": self.uid,
                    "mandatory": "true" if self.mandatory else "false",
                    "courseCode": self.course_code.split("-")[0].strip().upper() if self.course_code else "",
                    "sessionNumber": self.session_number or "",
                }
            },
        }
        if self.mandatory:
            payload["colorId"] = MANDATORY_COLOR_ID
        return payload


def in_sync_window(event: TimetableEvent, now: datetime | None = None, days: int = SYNC_WINDOW_DAYS) -> bool:
    current = now or datetime.now()
    window_end = current + timedelta(days=days)
    return current.date() <= event.starts_at.date() <= window_end.date()

