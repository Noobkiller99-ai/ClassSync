from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


TIMEZONE = "Asia/Kolkata"
CALENDAR_NAME = "SPJIMR Timetable"
DEFAULT_REMINDER_MINUTES = 15
SOURCE = "TCS iON"
SYNC_WINDOW_DAYS = 14


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

    @property
    def title(self) -> str:
        return self.subject_name

    @property
    def description(self) -> str:
        lines = [
            f"Faculty: {self.faculty or '-'}",
            f"Course Code: {self.course_code or '-'}",
            f"Classroom: {self.classroom or '-'}",
            f"Source: {SOURCE}",
        ]
        return "\n".join(lines)

    def google_payload(self) -> dict:
        return {
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
            "extendedProperties": {"private": {"classSyncUid": self.uid}},
        }


def in_sync_window(event: TimetableEvent, now: datetime | None = None, days: int = SYNC_WINDOW_DAYS) -> bool:
    current = now or datetime.now()
    window_end = current + timedelta(days=days)
    return current.date() <= event.starts_at.date() <= window_end.date()
