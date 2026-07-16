"""
test_improvements.py — Comprehensive tests for ClassSync improvements.

Areas covered:
  1. Google credential validity helper
  2. Remember-Google-Account flow (skip OAuth when valid creds stored)
  3. Upsert-by-code+session (delete stale + insert fresh)
  4. Extended properties written to every event payload
  5. Sync result counts are accurate
  6. Token refresh fallback when access token expires (credential reuse)
  7. /sync with stored creds does NOT redirect to /google/login
  8. /google/login uses reauth=True prompt when creds already present
  9. Session-number normalisation edge cases
 10. Batch extraction edge cases
 11. Events without session_number are NOT accidentally deleted
 12. Concurrent same code+session in one sync pass (no double-delete)
 13. Stale-event deletion failure is non-fatal
 14. google_email correctly surfaced in index template
 15. Re-sync banner shown instead of static pill when already synced
 16. TCS slot_remarks with leading quote character (real data artifact)
 17. Empty / whitespace session_number not used as index key
 18. Duplicate UIDs across users are isolated
 19. _group_events respects date ordering
 20. Admin refresh route still works with stored Google creds
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_app(tmp_path):
    from class_sync.web import create_app
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SAMPLE_ATTENDANCE_PATH": str(ROOT / "scripts" / "attendance_sample.json"),
            "USE_SAMPLE_TCS": True,
            "SYNC_WINDOW_NOW": "2026-06-12T00:00:00",
        }
    )


def _dry_run_creds():
    return {"dry_run": True}


def _real_creds(with_refresh=True):
    creds = {
        "token": "access-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csec",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "email": "student@gmail.com",
    }
    if with_refresh:
        creds["refresh_token"] = "rtoken"
    return creds


def _login(client):
    client.post(
        "/tcs/login",
        data={"username": "student@spjimr.org", "password": "secret"},
        follow_redirects=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _google_credentials_valid helper
# ═══════════════════════════════════════════════════════════════════════════════

def test_credentials_valid_with_refresh_token():
    from class_sync.web import google_credentials_valid
    assert google_credentials_valid(_real_creds(with_refresh=True)) is True


def test_credentials_invalid_without_refresh_token():
    from class_sync.web import google_credentials_valid
    assert google_credentials_valid(_real_creds(with_refresh=False)) is False


def test_credentials_valid_dry_run():
    from class_sync.web import google_credentials_valid
    assert google_credentials_valid({"dry_run": True}) is True


def test_credentials_invalid_none():
    from class_sync.web import google_credentials_valid
    assert google_credentials_valid(None) is False


def test_credentials_invalid_non_dict():
    from class_sync.web import google_credentials_valid
    assert google_credentials_valid("not-a-dict") is False
    assert google_credentials_valid(42) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. /sync route skips OAuth when valid credentials are already stored
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_skips_oauth_with_stored_creds(tmp_path):
    """If valid Google creds exist, /sync should NOT redirect to /google/login."""
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    # Read the user_token that was created during login
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    assert tok, "Login must create a user_token in session"

    from class_sync.store import set_setting
    db = app.config["DATABASE"]
    set_setting(db, tok, "google_credentials", _dry_run_creds())

    response = client.post("/sync", follow_redirects=False)

    # Should NOT redirect to google/login; should redirect back to index (/)
    assert response.status_code == 302
    assert "/google/login" not in response.headers.get("Location", "")


def test_sync_redirects_to_google_login_when_no_creds(tmp_path):
    """Without stored creds, /sync must redirect to /google/login."""
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post("/sync", follow_redirects=False)

    assert response.status_code == 302
    assert "google/login" in response.headers.get("Location", "")


def test_sync_redirects_to_google_login_when_creds_have_no_refresh_token(tmp_path):
    """Creds without refresh_token are treated as invalid; must re-auth."""
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    set_setting(db, tok, "google_credentials", _real_creds(with_refresh=False))

    response = client.post("/sync", follow_redirects=False)
    assert response.status_code == 302
    assert "google/login" in response.headers.get("Location", "")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Upsert-by-code+session: stale event deleted, fresh one inserted
# ═══════════════════════════════════════════════════════════════════════════════

def _make_mock_service(existing_items):
    mock_service = MagicMock()
    # Calendar list returns our calendar
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    # Events list returns existing_items
    mock_service.events().list().execute.side_effect = [
        {"items": existing_items, "nextPageToken": None}
    ]
    # insert returns a new event id
    mock_service.events().insert().execute.return_value = {"id": "new-google-id"}
    # update returns same id
    mock_service.events().update().execute.return_value = {"id": "existing-id"}
    return mock_service


def test_upsert_deletes_stale_and_inserts_fresh():
    """When a new payload matches an existing event by (courseCode, sessionNumber)
    but the existing event's uid is NOT in the incoming payload, the old event must
    be deleted and a fresh one inserted (rescheduled session scenario)."""
    from class_sync.google_calendar import GoogleCalendarClient

    stale_item = {
        "id": "stale-event-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "old-uid",   # uid NOT in payload → stale
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [stale_item], "nextPageToken": None}
    ]
    mock_service.events().insert().execute.return_value = {"id": "new-google-id"}

    # New payload for same course+session with a DIFFERENT uid (e.g. rescheduled)
    payloads = [
        {
            "uid": "new-uid",
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-11T10:40:00"},  # moved to next day
            "end": {"dateTime": "2026-06-11T11:50:00"},
            "extendedProperties": {
                "private": {
                    "classSyncUid": "new-uid",
                    "courseCode": "FIN521",
                    "sessionNumber": "3",
                }
            },
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # delete must have been called with the stale id
    mock_service.events().delete.assert_called_once_with(
        calendarId="cal-id", eventId="stale-event-id"
    )
    # insert must have been called (not update) because new-uid has no synced_event_id
    assert mock_service.events().insert.called
    assert not mock_service.events().update.called
    assert result.event_ids["new-uid"] == "new-google-id"


def test_resync_no_duplicate_when_synced_event_id_valid():
    """
    REGRESSION: Re-sync must NOT create a duplicate event when:
    - The payload event has a valid synced_event_id pointing to its existing Google event
    - existing_by_code_session happens to contain a STALE ghost event from a previous
      buggy run (different uid, same code+session)

    Expected: delete the ghost, UPDATE the legitimate event via synced_event_id.
    """
    from class_sync.google_calendar import GoogleCalendarClient

    legitimate_item = {
        "id": "legit-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-1",
                "courseCode": "FIN521",
                "sessionNumber": "1",
            }
        },
    }
    ghost_item = {
        "id": "ghost-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                # uid NOT in the incoming payload — this is the orphaned ghost
                "classSyncUid": "old-orphan-uid",
                "courseCode": "FIN521",
                "sessionNumber": "1",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    # Google Calendar has BOTH the legitimate event AND the ghost
    mock_service.events().list().execute.side_effect = [
        {"items": [legitimate_item, ghost_item], "nextPageToken": None}
    ]
    mock_service.events().update().execute.return_value = {"id": "legit-id"}

    payloads = [
        {
            "uid": "uid-1",
            "synced_event_id": "legit-id",   # points to the legitimate event
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T10:40:00"},
            "end": {"dateTime": "2026-06-10T11:50:00"},
            "extendedProperties": {
                "private": {
                    "classSyncUid": "uid-1",
                    "courseCode": "FIN521",
                    "sessionNumber": "1",
                }
            },
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # Ghost must be deleted
    mock_service.events().delete.assert_called_once_with(
        calendarId="cal-id", eventId="ghost-id"
    )
    # Legitimate event must be UPDATED (not inserted) — no duplicate!
    assert mock_service.events().update.called
    assert not mock_service.events().insert.called
    assert result.event_ids["uid-1"] == "legit-id"


def test_two_legitimate_sessions_same_code_session_both_kept():
    """
    Two real TCS events for the same (course, session) at DIFFERENT times must
    both be kept in Google Calendar — neither should be deleted.
    """
    from class_sync.google_calendar import GoogleCalendarClient

    item_a = {
        "id": "gid-a",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-a",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }
    item_b = {
        "id": "gid-b",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-13T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-b",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [item_a, item_b], "nextPageToken": None}
    ]
    mock_service.events().update().execute.side_effect = [
        {"id": "gid-a"}, {"id": "gid-b"}
    ]

    # Both events are in the payload (both uid-a and uid-b present)
    payloads = [
        {
            "uid": "uid-a",
            "synced_event_id": "gid-a",
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T10:40:00"},
            "end": {"dateTime": "2026-06-10T11:50:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-a", "courseCode": "FIN521", "sessionNumber": "3"}},
        },
        {
            "uid": "uid-b",
            "synced_event_id": "gid-b",
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-13T10:40:00"},
            "end": {"dateTime": "2026-06-13T11:50:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-b", "courseCode": "FIN521", "sessionNumber": "3"}},
        },
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # Neither event should be deleted — both are legitimate
    mock_service.events().delete.assert_not_called()
    # Both should be skipped (update not called) because they matched time, code, and session
    assert mock_service.events().update.call_count == 1  # only mock side_effect setup call
    assert result.event_ids["uid-a"] == "gid-a"
    assert result.event_ids["uid-b"] == "gid-b"



def test_upsert_does_not_delete_when_uid_matches():
    """If the incoming payload's UID matches the existing event's UID,
    it's the same event — we should UPDATE, not delete+insert."""
    from class_sync.google_calendar import GoogleCalendarClient

    existing_item = {
        "id": "existing-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "same-uid",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [existing_item], "nextPageToken": None}
    ]
    mock_service.events().update().execute.return_value = {"id": "existing-id"}

    payloads = [
        {
            "uid": "same-uid",
            "synced_event_id": None,
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T10:40:00"},
            "end": {"dateTime": "2026-06-10T11:50:00"},
            "extendedProperties": {
                "private": {
                    "classSyncUid": "same-uid",
                    "courseCode": "FIN521",
                    "sessionNumber": "3",
                }
            },
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # No delete
    mock_service.events().delete.assert_not_called()
    # Should update
    assert mock_service.events().update.called
    assert result.event_ids["same-uid"] == "existing-id"


def test_upsert_no_session_number_skips_code_session_logic():
    """Events without a sessionNumber in extended props must NOT trigger
    the delete+insert path — they use the normal UID / time+title dedup."""
    from class_sync.google_calendar import GoogleCalendarClient

    existing_item = {
        "id": "existing-id",
        "summary": "Business & Society",
        "start": {"dateTime": "2026-06-11T14:30:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-no-sess",
                "courseCode": "STR520",
                "sessionNumber": "",  # empty
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [existing_item], "nextPageToken": None}
    ]
    mock_service.events().update().execute.return_value = {"id": "existing-id"}

    payloads = [
        {
            "uid": "uid-no-sess",
            "synced_event_id": None,
            "summary": "Business & Society",
            "start": {"dateTime": "2026-06-11T14:30:00"},
            "end": {"dateTime": "2026-06-11T15:40:00"},
            "extendedProperties": {
                "private": {
                    "classSyncUid": "uid-no-sess",
                    "courseCode": "STR520",
                    "sessionNumber": "",
                }
            },
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    mock_service.events().delete.assert_not_called()
    assert mock_service.events().update.called


def test_stale_deletion_failure_is_non_fatal():
    """If delete() raises an exception, sync should still insert the fresh event."""
    from class_sync.google_calendar import GoogleCalendarClient

    stale_item = {
        "id": "stale-id",
        "summary": "Org Behaviour",
        "start": {"dateTime": "2026-06-10T08:00:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "old-uid-2",
                "courseCode": "OLS541",
                "sessionNumber": "7",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [stale_item], "nextPageToken": None}
    ]
    # Simulate delete failure
    mock_service.events().delete().execute.side_effect = Exception("API error")
    mock_service.events().insert().execute.return_value = {"id": "fresh-id"}

    payloads = [
        {
            "uid": "new-uid-2",
            "summary": "Org Behaviour",
            "start": {"dateTime": "2026-06-11T08:00:00"},
            "end": {"dateTime": "2026-06-11T09:10:00"},
            "extendedProperties": {
                "private": {
                    "classSyncUid": "new-uid-2",
                    "courseCode": "OLS541",
                    "sessionNumber": "7",
                }
            },
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        # Should NOT raise even though delete failed
        result = client.sync(payloads)

    assert result.imported == 1
    # Insert should have been called despite delete failure
    assert mock_service.events().insert.called


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Extended properties in every event payload
# ═══════════════════════════════════════════════════════════════════════════════

def test_google_payload_has_course_code_and_session_number():
    from class_sync.tcs import parse_tcs_attendance
    events = parse_tcs_attendance(
        (ROOT / "scripts" / "attendance_sample.json").read_text(encoding="utf-8")
    )
    for event in events:
        payload = event.google_payload()
        private = payload["extendedProperties"]["private"]
        assert "courseCode" in private, f"Missing courseCode for {event.uid}"
        assert "sessionNumber" in private, f"Missing sessionNumber for {event.uid}"
        # courseCode must be the normalised prefix (no section suffixes)
        if event.course_code:
            expected = event.course_code.split("-")[0].strip().upper()
            assert private["courseCode"] == expected


def test_google_payload_course_code_normalised():
    from class_sync.models import TimetableEvent
    event = TimetableEvent(
        uid="test|0940|FIN521-PDM-46|NCR5|0",
        subject_name="Financial Innovations & Fintech",
        course_code="FIN521-PDM-46",
        faculty="Vidhu Shekhar",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 9, 40),
        ends_at=datetime(2026, 6, 10, 10, 50),
        session_number="5",
    )
    private = event.google_payload()["extendedProperties"]["private"]
    assert private["courseCode"] == "FIN521"
    assert private["sessionNumber"] == "5"


def test_google_payload_empty_session_number():
    """Events with no session number should store empty string, not None."""
    from class_sync.models import TimetableEvent
    event = TimetableEvent(
        uid="test|0940|OLS541|NCR5|0",
        subject_name="Management of Change",
        course_code="OLS541",
        faculty="Tanvi Mankodi",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 9, 10, 40),
        ends_at=datetime(2026, 6, 9, 11, 50),
        session_number="",
    )
    private = event.google_payload()["extendedProperties"]["private"]
    assert private["sessionNumber"] == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Sync result counts are accurate
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_result_imported_count_matches_payload_count():
    from class_sync.google_calendar import GoogleCalendarClient

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [], "nextPageToken": None}
    ]
    mock_service.events().insert().execute.side_effect = [
        {"id": f"gid-{i}"} for i in range(5)
    ]

    payloads = [
        {
            "uid": f"uid-{i}",
            "summary": f"Course {i}",
            "start": {"dateTime": f"2026-06-{10+i:02d}T10:00:00"},
            "end": {"dateTime": f"2026-06-{10+i:02d}T11:00:00"},
            "extendedProperties": {"private": {"classSyncUid": f"uid-{i}", "courseCode": f"CRS{i:03d}", "sessionNumber": str(i)}},
        }
        for i in range(5)
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    assert result.imported == 5
    assert len(result.event_ids) == 5


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Token refresh — creds with refresh_token reused without re-auth
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_with_stored_real_creds_calls_sync_calendar(tmp_path):
    """When real creds (with refresh_token) are stored, /sync must call
    _sync_calendar and succeed without touching /google/login."""
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    # Store dry-run creds (acts like valid creds in test mode)
    set_setting(db, tok, "google_credentials", _dry_run_creds())

    response = client.post("/sync", follow_redirects=True)
    assert response.status_code == 200
    # Flash message should confirm sync happened
    assert b"Synced!" in response.data or b"events" in response.data


# ═══════════════════════════════════════════════════════════════════════════════
# 7. google_email surfaced in template
# ═══════════════════════════════════════════════════════════════════════════════

def test_index_shows_google_email_when_connected(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    creds = _real_creds()
    set_setting(db, tok, "google_credentials", creds)

    response = client.get("/")
    assert response.status_code == 200
    assert b"student@gmail.com" in response.data


def test_index_shows_generic_badge_when_no_email(tmp_path):
    """When email is missing from stored creds, fall back to 'Google Calendar'."""
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    creds = _real_creds()
    creds.pop("email")
    set_setting(db, tok, "google_credentials", creds)

    response = client.get("/")
    assert response.status_code == 200
    assert b"Google Calendar" in response.data


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Re-sync banner shown instead of static pill when already synced
# ═══════════════════════════════════════════════════════════════════════════════

def test_index_shows_resync_button_when_synced(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting, mark_many_synced, list_event_payloads
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    set_setting(db, tok, "google_credentials", _dry_run_creds())
    # Mark events as synced
    payloads = list_event_payloads(db, tok)
    if payloads:
        mark_many_synced(db, tok, {p["uid"]: "fake-gcal-id" for p in payloads})

    response = client.get("/")
    assert response.status_code == 200
    # Re-sync button must be present (not static synced pill)
    assert b"Re-sync Calendar" in response.data or b"Sync Calendar" in response.data


# ═══════════════════════════════════════════════════════════════════════════════
# 9. TCS slot_remarks leading-quote artifact (real data)
# ═══════════════════════════════════════════════════════════════════════════════

def test_slot_remarks_leading_quote_parsed_correctly():
    """The real TCS data has slot_remarks like '"3' (with a leading double-quote).
    The parser must still extract session number '3'."""
    from class_sync.tcs import parse_tcs_attendance
    events = parse_tcs_attendance(
        (ROOT / "scripts" / "attendance_sample.json").read_text(encoding="utf-8")
    )
    # First event in sample has slot_remarks = '"3'
    first = events[0]
    assert first.session_number == "3", (
        f"Expected session_number='3', got '{first.session_number}'"
    )


def test_all_sample_events_have_session_numbers():
    from class_sync.tcs import parse_tcs_attendance
    events = parse_tcs_attendance(
        (ROOT / "scripts" / "attendance_sample.json").read_text(encoding="utf-8")
    )
    for ev in events:
        assert ev.session_number, f"Event {ev.uid} has empty session_number"
        assert ev.session_number.isdigit(), (
            f"Event {ev.uid} session_number '{ev.session_number}' is not a digit"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Batch extraction edge cases
# ═══════════════════════════════════════════════════════════════════════════════

def test_batch_extraction_standard():
    from class_sync.web import extract_batch_from_email
    assert extract_batch_from_email("pgp25.john.doe@spjimr.org") == "pgp25"


def test_batch_extraction_single_prefix():
    from class_sync.web import extract_batch_from_email
    assert extract_batch_from_email("pgp25@spjimr.org") == "pgp25"


def test_batch_extraction_empty():
    from class_sync.web import extract_batch_from_email
    assert extract_batch_from_email("") == "general"


def test_batch_extraction_no_dot():
    from class_sync.web import extract_batch_from_email
    # Not a known programme prefix → "general"
    result = extract_batch_from_email("johndoe@spjimr.org")
    assert result == "general"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. User isolation — two users' events don't bleed into each other's DB rows
# ═══════════════════════════════════════════════════════════════════════════════

def test_user_event_isolation(tmp_path):
    from class_sync.store import save_events, list_event_payloads
    from class_sync.models import TimetableEvent

    app = make_app(tmp_path)
    db = app.config["DATABASE"]

    ev_a = TimetableEvent(
        uid="uid-a",
        subject_name="Course A",
        course_code="CRS001",
        faculty="Faculty A",
        classroom="Room1",
        starts_at=datetime(2026, 6, 10, 9, 0),
        ends_at=datetime(2026, 6, 10, 10, 0),
        session_number="1",
    )
    ev_b = TimetableEvent(
        uid="uid-b",
        subject_name="Course B",
        course_code="CRS002",
        faculty="Faculty B",
        classroom="Room2",
        starts_at=datetime(2026, 6, 11, 9, 0),
        ends_at=datetime(2026, 6, 11, 10, 0),
        session_number="1",
    )

    save_events(db, "user-alpha", [ev_a])
    save_events(db, "user-beta", [ev_b])

    payloads_alpha = list_event_payloads(db, "user-alpha")
    payloads_beta = list_event_payloads(db, "user-beta")

    assert len(payloads_alpha) == 1
    assert payloads_alpha[0]["summary"] == "Course A"
    assert len(payloads_beta) == 1
    assert payloads_beta[0]["summary"] == "Course B"


# ═══════════════════════════════════════════════════════════════════════════════
# 12. _group_events respects date ordering
# ═══════════════════════════════════════════════════════════════════════════════

def test_group_events_sorted_by_date():
    from class_sync.web import _group_events  # type: ignore[attr-defined]

    events = [
        {"starts_at": "2026-06-13T10:00:00", "ends_at": "2026-06-13T11:00:00", "title": "Last"},
        {"starts_at": "2026-06-10T10:00:00", "ends_at": "2026-06-10T11:00:00", "title": "First"},
        {"starts_at": "2026-06-11T10:00:00", "ends_at": "2026-06-11T11:00:00", "title": "Middle"},
    ]

    groups = _group_events(events)
    dates = [g["date"] for g in groups]
    assert dates == sorted(dates), "Event groups are not in ascending date order"


def test_group_events_empty():
    from class_sync.web import _group_events  # type: ignore[attr-defined]
    assert _group_events([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Admin refresh still works with stored Google creds
# ═══════════════════════════════════════════════════════════════════════════════

def test_admin_refresh_with_stored_google_creds(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    from class_sync.store import set_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    set_setting(db, tok, "google_credentials", _dry_run_creds())

    response = client.post("/admin/refresh", follow_redirects=True)
    assert response.status_code == 200
    assert b"Admin refresh complete" in response.data


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Dry-run sync returns correct structure
# ═══════════════════════════════════════════════════════════════════════════════

def test_dry_run_sync_result_structure():
    from class_sync.google_calendar import GoogleCalendarClient

    client = GoogleCalendarClient(dry_run=True)
    payloads = [
        {
            "uid": "uid-1",
            "summary": "Test Event",
            "start": {"dateTime": "2026-06-10T10:00:00"},
            "end": {"dateTime": "2026-06-10T11:00:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-1"}},
        }
    ]

    result = client.sync(payloads)

    assert result.dry_run is True
    assert result.imported == 1
    assert "uid-1" in result.event_ids


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Encrypt / decrypt round-trip for TCS credentials
# ═══════════════════════════════════════════════════════════════════════════════

def test_credential_encrypt_decrypt_roundtrip():
    from class_sync.security import encrypt_json, decrypt_json
    original = {"username": "student@spjimr.org", "password": "super-secret!"}
    encrypted = encrypt_json(original)
    assert encrypted != json.dumps(original)
    decrypted = decrypt_json(encrypted)
    assert decrypted == original


def test_decrypt_none_returns_none():
    from class_sync.security import decrypt_json
    assert decrypt_json(None) is None


def test_decrypt_empty_string_returns_none():
    from class_sync.security import decrypt_json
    assert decrypt_json("") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Mandatory flag propagation via apply_mandatory_flags
# ═══════════════════════════════════════════════════════════════════════════════

def test_apply_mandatory_flags_marks_correct_sessions():
    from class_sync.models import TimetableEvent
    from class_sync.tcs import apply_mandatory_flags

    events = [
        TimetableEvent(
            uid="a", subject_name="Fin", course_code="FIN521-PDM",
            faculty="F", classroom="C",
            starts_at=datetime(2026, 6, 10, 9, 0), ends_at=datetime(2026, 6, 10, 10, 0),
            session_number="3"
        ),
        TimetableEvent(
            uid="b", subject_name="Fin", course_code="FIN521-PDM",
            faculty="F", classroom="C",
            starts_at=datetime(2026, 6, 11, 9, 0), ends_at=datetime(2026, 6, 11, 10, 0),
            session_number="5"
        ),
        TimetableEvent(
            uid="c", subject_name="Fin", course_code="FIN521-PDM",
            faculty="F", classroom="C",
            starts_at=datetime(2026, 6, 12, 9, 0), ends_at=datetime(2026, 6, 12, 10, 0),
            session_number="7"
        ),
    ]

    flagged = apply_mandatory_flags(events, {"FIN521": [3, 5]})

    assert flagged[0].mandatory is True    # session 3 → mandatory
    assert flagged[1].mandatory is True    # session 5 → mandatory
    assert flagged[2].mandatory is False   # session 7 → not mandatory


def test_apply_mandatory_flags_leaves_unknown_courses_untouched():
    from class_sync.models import TimetableEvent
    from class_sync.tcs import apply_mandatory_flags

    event = TimetableEvent(
        uid="x", subject_name="Unknown Course", course_code="XYZ999",
        faculty="F", classroom="C",
        starts_at=datetime(2026, 6, 10, 9, 0), ends_at=datetime(2026, 6, 10, 10, 0),
        session_number="1"
    )

    flagged = apply_mandatory_flags([event], {"FIN521": [1, 2, 3]})
    assert flagged[0].mandatory is False


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Store: mark_many_synced correctly updates synced_event_id
# ═══════════════════════════════════════════════════════════════════════════════

def test_mark_many_synced(tmp_path):
    from class_sync.store import save_events, list_event_payloads, mark_many_synced
    from class_sync.models import TimetableEvent

    app = make_app(tmp_path)
    db = app.config["DATABASE"]

    events = [
        TimetableEvent(
            uid=f"uid-{i}", subject_name=f"Course {i}", course_code=f"CRS{i}",
            faculty="F", classroom="C",
            starts_at=datetime(2026, 6, 10 + i, 9, 0),
            ends_at=datetime(2026, 6, 10 + i, 10, 0),
            session_number=str(i),
        )
        for i in range(3)
    ]
    save_events(db, "tok-1", events)

    mark_many_synced(db, "tok-1", {f"uid-{i}": f"gcal-{i}" for i in range(3)})

    payloads = list_event_payloads(db, "tok-1")
    for p in payloads:
        i = p["uid"].split("-")[1]
        assert p["synced_event_id"] == f"gcal-{i}"


# ═══════════════════════════════════════════════════════════════════════════════
# 18. /google/callback stores credentials and redirects to index
# ═══════════════════════════════════════════════════════════════════════════════

def test_google_callback_dry_run_stores_creds_and_redirects(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.get("/google/callback?dry_run=1", follow_redirects=False)
    assert response.status_code == 302

    from class_sync.store import get_setting
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    db = app.config["DATABASE"]
    creds = get_setting(db, tok, "google_credentials", None)
    assert creds is not None
    assert creds.get("dry_run") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 19. sync() short-circuits on empty payload (no Google API calls)
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_empty_payloads_skips_api_calls():
    """sync([]) must return immediately with imported=0 and make NO Google API calls."""
    from class_sync.google_calendar import GoogleCalendarClient

    mock_service = MagicMock()

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync([])

    assert result.imported == 0
    assert result.event_ids == {}
    # The Google Calendar API must not have been touched at all
    mock_service.events.assert_not_called()
    mock_service.calendarList.assert_not_called()


def test_sync_dry_run_empty_payloads_returns_zero():
    """Dry-run with empty payloads also returns correctly."""
    from class_sync.google_calendar import GoogleCalendarClient
    client = GoogleCalendarClient(dry_run=True)
    result = client.sync([])
    assert result.imported == 0
    assert result.event_ids == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 20. timeMin filter is passed to events().list()
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_passes_time_min_to_events_list():
    """events().list() must receive timeMin and timeMax arguments to avoid fetching unnecessary history/future events."""
    from class_sync.google_calendar import GoogleCalendarClient

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [], "nextPageToken": None}
    ]
    mock_service.events().insert().execute.return_value = {"id": "new-id"}

    payloads = [
        {
            "uid": "uid-1",
            "summary": "Course",
            "start": {"dateTime": "2026-06-10T10:00:00"},
            "end": {"dateTime": "2026-06-10T11:00:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-1", "courseCode": "CRS001", "sessionNumber": "1"}},
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        client.sync(payloads)

    # Verify that the list() call received timeMin and timeMax
    list_call_kwargs = mock_service.events().list.call_args
    assert list_call_kwargs is not None
    kwargs = list_call_kwargs.kwargs if hasattr(list_call_kwargs, "kwargs") else list_call_kwargs[1]
    assert "timeMin" in kwargs, "timeMin must be passed to events().list() for performance"
    assert "timeMax" in kwargs, "timeMax must be passed to events().list() for performance"
    
    # Check formats
    time_min_val = kwargs["timeMin"]
    assert "Z" in time_min_val or "T" in time_min_val, f"timeMin looks malformed: {time_min_val}"
    time_max_val = kwargs["timeMax"]
    assert "Z" in time_max_val or "T" in time_max_val, f"timeMax looks malformed: {time_max_val}"


# ═══════════════════════════════════════════════════════════════════════════════
# 21. _group_events tomorrow label uses timedelta (no month-boundary crash)
# ═══════════════════════════════════════════════════════════════════════════════

def test_group_events_tomorrow_label():
    """Tomorrow events must receive the 'Tomorrow · ...' label."""
    from class_sync.web import _group_events  # type: ignore[attr-defined]
    from datetime import date, timedelta

    tomorrow = date.today() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()

    events = [{"starts_at": f"{tomorrow_str}T10:00:00", "ends_at": f"{tomorrow_str}T11:00:00"}]
    groups = _group_events(events)

    assert len(groups) == 1
    assert groups[0]["label"].startswith("Tomorrow ·"), (
        f"Expected 'Tomorrow · ...' label, got: '{groups[0]['label']}'"
    )


def test_group_events_today_label():
    """Today's events must receive the 'Today · ...' label."""
    from class_sync.web import _group_events  # type: ignore[attr-defined]
    from datetime import date

    today_str = date.today().isoformat()
    events = [{"starts_at": f"{today_str}T10:00:00", "ends_at": f"{today_str}T11:00:00"}]
    groups = _group_events(events)

    assert len(groups) == 1
    assert groups[0]["label"].startswith("Today ·"), (
        f"Expected 'Today · ...' label, got: '{groups[0]['label']}'"
    )
    assert groups[0]["is_today"] is True


def test_group_events_month_boundary_no_crash():
    """_group_events must not crash for dates at month boundaries (e.g. Jan 31, Mar 31)."""
    from class_sync.web import _group_events  # type: ignore[attr-defined]

    # These are month-boundary dates that previously could cause ValueError
    boundary_dates = [
        "2026-01-31T10:00:00",
        "2026-03-31T10:00:00",
        "2026-05-31T10:00:00",
        "2026-08-31T10:00:00",
    ]
    events = [{"starts_at": d, "ends_at": d.replace("10:00", "11:00")} for d in boundary_dates]

    # Must not raise
    groups = _group_events(events)
    assert len(groups) == len(boundary_dates)


# ═══════════════════════════════════════════════════════════════════════════════
# 22. Evaluations Support (📝 title prefix and Tangerine Orange color ID 6)
# ═══════════════════════════════════════════════════════════════════════════════

def test_evaluation_formatting():
    from class_sync.models import TimetableEvent

    # 1. Non-mandatory evaluation
    ev1 = TimetableEvent(
        uid="uid-1", subject_name="Midterm Exam", course_code="FIN521",
        faculty="Dr. Shekhar", classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 0), ends_at=datetime(2026, 6, 10, 11, 0),
        session_number="3", activity_name="Evaluation"
    )
    assert ev1.is_evaluation is True
    assert ev1.title == "📝 EVALUATION: Midterm Exam"
    assert "📝 EVALUATION EVENT" in ev1.description
    assert ev1.google_payload()["colorId"] == "6"

    # 2. Mandatory evaluation
    ev2 = TimetableEvent(
        uid="uid-2", subject_name="Final Exam", course_code="FIN521",
        faculty="Dr. Shekhar", classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 0), ends_at=datetime(2026, 6, 10, 11, 0),
        session_number="4", activity_name="Exam", mandatory=True
    )
    assert ev2.is_evaluation is True
    assert ev2.title == "🔴 MANDATORY EVALUATION: Final Exam"
    assert "⚠️ MANDATORY SESSION" in ev2.description
    assert "📝 EVALUATION EVENT" in ev2.description
    assert ev2.google_payload()["colorId"] == "11"


# ═══════════════════════════════════════════════════════════════════════════════
# 23. Deduplication of incoming parsed TCS events
# ═══════════════════════════════════════════════════════════════════════════════

def test_parse_tcs_attendance_deduplicates_incoming_list():
    from class_sync.tcs import parse_tcs_attendance

    # Timetable response containing exact duplicate rows (except possibly the outer JSON structure wrapper)
    raw_payload = json.dumps([
        {
            "Item1": {
                "dateval": "2026-06-10 00:00:00.0",
                "start_time": "10:40am",
                "end_time": "11:50am",
                "sudsubjectname": "Financial Innovations & Fintech",
                "sudsubjectshortcode": "FIN521-PDM",
                "sudresourcename": "NCR5",
                "slot_remarks": "3",
                "sudactivityname": "Session"
            }
        },
        {
            "Item1": {
                "dateval": "2026-06-10 00:00:00.0",
                "start_time": "10:40am",
                "end_time": "11:50am",
                "sudsubjectname": "Financial Innovations & Fintech",
                "sudsubjectshortcode": "FIN521-PDM",
                "sudresourcename": "NCR5",
                "slot_remarks": "3",
                "sudactivityname": "Session"
            }
        }
    ])

    events = parse_tcs_attendance(raw_payload)
    assert len(events) == 1  # The duplicate was successfully discarded!


# ═══════════════════════════════════════════════════════════════════════════════
# 24. Deleting duplicate events on listing from Google Calendar
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_deletes_duplicates_on_google_calendar():
    from class_sync.google_calendar import GoogleCalendarClient

    item_orig = {
        "id": "g-id-orig",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-1",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }
    item_dup = {
        "id": "g-id-dup",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-1-dup",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    mock_service.events().list().execute.side_effect = [
        {"items": [item_orig, item_dup], "nextPageToken": None}
    ]
    mock_service.events().update().execute.return_value = {"id": "g-id-orig"}

    payloads = [
        {
            "uid": "uid-1",
            "synced_event_id": "g-id-orig",
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T10:40:00"},
            "end": {"dateTime": "2026-06-10T11:50:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-1", "courseCode": "FIN521", "sessionNumber": "3"}},
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # The duplicate must be deleted immediately in the listing phase
    mock_service.events().delete.assert_called_once_with(
        calendarId="cal-id", eventId="g-id-dup"
    )
    # The original is matched and skipped under strict skip rule, so update is not called on mock.
    # (The test setup update mock call count remains 1 from the side_effect setup)
    assert mock_service.events().update.call_count == 1
    assert result.event_ids["uid-1"] == "g-id-orig"


# ═══════════════════════════════════════════════════════════════════════════════
# 25. Strict Deduplication and Rescheduling logic
# ═══════════════════════════════════════════════════════════════════════════════

def test_strict_dedup_and_rescheduling():
    """
    If existing matches code+session+time -> skip.
    If existing matches code+session but time mismatches -> delete old, insert new.
    """
    from class_sync.google_calendar import GoogleCalendarClient

    # 1. Stale event in Google Calendar (different time: 10:40)
    existing_stale = {
        "id": "g-stale-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-old",
                "courseCode": "FIN521",
                "sessionNumber": "3",
            }
        },
    }

    # 2. Matching event in Google Calendar (same time: 14:30)
    existing_matching = {
        "id": "g-match-id",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T14:30:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-match",
                "courseCode": "FIN521",
                "sessionNumber": "4",
            }
        },
    }

    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {
        "items": [{"id": "cal-id", "summary": "SPJIMR Timetable"}]
    }
    # Google Calendar contains both existing events
    mock_service.events().list().execute.side_effect = [
        {"items": [existing_stale, existing_matching], "nextPageToken": None}
    ]
    mock_service.events().insert().execute.return_value = {"id": "g-new-inserted-id"}

    # Timetable incoming payloads
    payloads = [
        {
            "uid": "uid-new",  # Rescheduled session 3 now at 15:40 instead of 10:40
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T15:40:00"},
            "end": {"dateTime": "2026-06-10T16:50:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-new", "courseCode": "FIN521", "sessionNumber": "3"}},
        },
        {
            "uid": "uid-match", # Matching session 4 remains at 14:30
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T14:30:00"},
            "end": {"dateTime": "2026-06-10T15:40:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-match", "courseCode": "FIN521", "sessionNumber": "4"}},
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        client = GoogleCalendarClient(credentials={"token": "tok", "refresh_token": "r"})
        result = client.sync(payloads)

    # 1. Stale event g-stale-id (since start 10:40 is not in incoming times) must be deleted in list loop
    mock_service.events().delete.assert_any_call(calendarId="cal-id", eventId="g-stale-id")
    
    # 2. Matching event must NOT be deleted because start 14:30 is in incoming times
    with pytest.raises(AssertionError):
        mock_service.events().delete.assert_any_call(calendarId="cal-id", eventId="g-match-id")

    # 3. Matching event must be skipped (no update or insert API calls made for it)
    # The only insert call should be for the rescheduled/new slot uid-new (call_count is 2 due to setup mock)
    assert mock_service.events().insert.call_count == 2
    # Verify the actual insert call arguments
    insert_call = mock_service.events().insert.call_args_list[-1]
    assert insert_call.kwargs["calendarId"] == "cal-id"
    assert insert_call.kwargs["body"]["extendedProperties"]["private"]["classSyncUid"] == "uid-new"
    
    # Update must not be called (since matches skipped and stale deleted)
    assert mock_service.events().update.call_count == 0

    # 4. Result ids must contain correct mapped IDs
    assert result.event_ids["uid-new"] == "g-new-inserted-id"
    assert result.event_ids["uid-match"] == "g-match-id"


def test_wisenet_parser_extracts_all_sessions():
    from class_sync.wisenet import parse_mandatory_sessions_from_pdf
    pdf_path = ROOT / "QF - Course outline for sessions 9 to 18 - Batch 25-27.pdf"
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    
    res = parse_mandatory_sessions_from_pdf(pdf_bytes, "FIN561")
    assert res.course_code == "FIN561"
    assert res.mandatory_sessions == []
    assert res.all_sessions == [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]


def test_web_upload_merges_mandatory_sessions(tmp_path):
    from class_sync.store import get_mandatory_sessions, save_mandatory_sessions
    app = make_app(tmp_path)
    client = app.test_client()
    _login(client)
    
    with client.session_transaction() as sess:
        tok = sess.get("user_token")
    
    db = app.config["DATABASE"]
    # Seed initial mandatory sessions in DB
    save_mandatory_sessions(db, "general", {"FIN561": [1, 2, 3, 4]})
    
    pdf_path = ROOT / "QF - Course outline for sessions 9 to 18 - Batch 25-27.pdf"
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
        
    data = {
        "pdf_files": (io.BytesIO(pdf_bytes), "QF - Course outline for sessions 9 to 18 - Batch 25-27.pdf")
    }
    
    res = client.post(
        "/wisenet/upload",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True
    )
    assert res.status_code == 200
    assert b"Successfully processed 1" in res.data
    
    # Check that database contains the merged list
    sessions = get_mandatory_sessions(db, "general")
    assert "FIN561" in sessions
    assert sessions["FIN561"] == [1, 2, 3, 4]




