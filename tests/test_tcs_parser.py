from pathlib import Path

from class_sync.models import DEFAULT_REMINDER_MINUTES, SOURCE, TIMEZONE
from class_sync.tcs import encrypt_password, parse_tcs_attendance


ROOT = Path(__file__).resolve().parents[1]


def test_parse_tcs_attendance_sample():
    events = parse_tcs_attendance((ROOT / "scripts" / "attendance_sample.json").read_text(encoding="utf-8"))

    assert len(events) == 9
    first = events[0]
    assert first.title == "Management of Change"
    assert first.course_code == "OLS541-PBM"
    assert first.faculty == "Tanvi Mankodi"
    assert first.classroom == "NCR5"
    assert first.starts_at.isoformat() == "2026-06-09T10:40:00"
    assert first.ends_at.isoformat() == "2026-06-09T11:50:00"
    assert first.session_number == "3"
    assert events[1].session_number == "1"
    assert events[2].session_number == "2"
    assert events[3].session_number == "3"


def test_google_payload_contract():
    event = parse_tcs_attendance((ROOT / "scripts" / "attendance_sample.json").read_text(encoding="utf-8"))[0]
    payload = event.google_payload()

    # summary must equal event.title (which adds "🔴 MANDATORY: " prefix for mandatory events)
    assert payload["summary"] == event.title
    assert f"Source: {SOURCE}" in payload["description"]
    assert payload["start"]["timeZone"] == TIMEZONE
    assert payload["reminders"]["overrides"][0]["minutes"] == DEFAULT_REMINDER_MINUTES


def test_tcs_password_transform_shape():
    encrypted = encrypt_password("JohnNolan@2000")

    assert encrypted != "JohnNolan@2000"
    assert len(encrypted) == len("JohnNolan@2000") + len("fdledje4p2aga6gtfgq2ce")
    assert encrypted.endswith("Ns")
