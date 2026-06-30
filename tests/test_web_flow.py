from pathlib import Path

from class_sync.web import create_app


ROOT = Path(__file__).resolve().parents[1]


def make_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SAMPLE_ATTENDANCE_PATH": str(ROOT / "scripts" / "attendance_sample.json"),
            "USE_SAMPLE_TCS": True,
            "SYNC_WINDOW_NOW": "2026-06-12T00:00:00",
        }
    )


def test_health(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json["status"] == "ok"


def test_preview_google_dry_run_sync_and_admin_refresh(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/tcs/login",
        data={"username": "student@spjimr.org", "password": "secret"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"TCS iON connected" in response.data
    assert b"Management of Change" in response.data

    response = client.post("/sync")

    assert response.status_code in (302, 200)
    # May redirect to Google OAuth — that's expected when Google not yet configured

    response = client.post("/admin/refresh", follow_redirects=True)

    assert response.status_code == 200
    assert b"Admin refresh complete" in response.data


def test_rejects_non_spjimr_accounts(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/tcs/login",
        data={"username": "student@example.com", "password": "secret"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"limited to SPJIMR student accounts" in response.data


def test_reset_clears_tcs_state(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/tcs/login",
        data={"username": "student@spjimr.org", "password": "secret"},
        follow_redirects=True,
    )

    response = client.post("/tcs/reset", follow_redirects=True)

    assert response.status_code == 200
    assert b"Session cleared" in response.data
    assert b"Sync with Google Calendar" not in response.data


def test_wisenet_upload_and_global_sharing(tmp_path):
    from unittest.mock import patch
    from class_sync.wisenet import MandatorySessionInfo
    from class_sync.store import get_mandatory_sessions
    import io

    app = make_app(tmp_path)
    client = app.test_client()

    client.get("/")  # create session token

    mock_info = MandatorySessionInfo(
        course_code="FIN521",
        course_shortname="FIN521-PDM-46",
        mandatory_sessions=[1, 3, 5]
    )

    with patch("class_sync.wisenet.parse_mandatory_sessions_from_pdf", return_value=mock_info):
        data = {
            "pdf_files": (io.BytesIO(b"%PDF-1.4 dummy"), "FIN521-Outline.pdf")
        }
        res = client.post(
            "/wisenet/upload",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True
        )
        assert res.status_code == 200
        assert b"Successfully processed 1 course outline" in res.data

    # Verify that a different user query gets the shared course outline
    db = app.config["DATABASE"]
    sessions = get_mandatory_sessions(db, "general")
    assert "FIN521" in sessions
    assert sessions["FIN521"] == [1, 3, 5]


def test_google_calendar_duplicate_prevention_and_color():
    from unittest.mock import MagicMock, patch
    from class_sync.google_calendar import GoogleCalendarClient

    # Set up mock events list response from Google Calendar list() call
    mock_item_1 = {
        "id": "existing-google-id-1",
        "summary": "🔴 MANDATORY: Management of Change",
        "start": {"dateTime": "2026-06-09T10:40:00+05:30"},
        "extendedProperties": {
            "private": {
                "classSyncUid": "uid-1"
            }
        }
    }
    # Item 2 matched by start time and normalized summary, no classSyncUid in private properties
    mock_item_2 = {
        "id": "existing-google-id-2",
        "summary": "Financial Innovations & Fintech",
        "start": {"dateTime": "2026-06-10T10:40:00+05:30"}
    }
    
    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {"items": [{"id": "primary", "summary": "SPJIMR Timetable"}]}
    
    # Mock listing call returns mock_item_1 and mock_item_2
    mock_service.events().list().execute.side_effect = [
        {"items": [mock_item_1, mock_item_2], "nextPageToken": None}
    ]

    client = GoogleCalendarClient(credentials={"token": "dummy"})

    # Payloads to sync
    payloads = [
        {
            "uid": "uid-1",
            "summary": "🔴 MANDATORY: Management of Change",
            "start": {"dateTime": "2026-06-09T10:40:00"},
            "end": {"dateTime": "2026-06-09T11:50:00"},
            "colorId": "11",
            "extendedProperties": {"private": {"classSyncUid": "uid-1"}}
        },
        {
            "uid": "uid-2",
            "summary": "Financial Innovations & Fintech",
            "start": {"dateTime": "2026-06-10T10:40:00"},
            "end": {"dateTime": "2026-06-10T11:50:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-2"}}
        },
        {
            "uid": "uid-3",
            "summary": "Business & Society",
            "start": {"dateTime": "2026-06-11T14:30:00"},
            "end": {"dateTime": "2026-06-11T15:40:00"},
            "extendedProperties": {"private": {"classSyncUid": "uid-3"}}
        }
    ]

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        result = client.sync(payloads)
        
        # Verify result mappings
        assert result.event_ids["uid-1"] == "existing-google-id-1"  # Matched by classSyncUid
        assert result.event_ids["uid-2"] == "existing-google-id-2"  # Matched by normalized summary & start time
        assert result.event_ids["uid-3"] != "existing-google-id-1"  # New event inserted
        assert result.event_ids["uid-3"] != "existing-google-id-2"

        # Verify that update is NOT called because the events matched time, course code, and session number (skipped)
        mock_service.events().update.assert_not_called()
        # Verify insert was called for new event
        body_3 = {k: v for k, v in payloads[2].items() if k not in {"uid", "synced_event_id"}}
        mock_service.events().insert.assert_any_call(
            calendarId="primary", body=body_3
        )


def test_central_distribution_on_upload(tmp_path):
    from unittest.mock import patch
    from class_sync.wisenet import MandatorySessionInfo
    from class_sync.store import save_events, list_event_payloads, set_setting
    from class_sync.models import TimetableEvent
    from datetime import datetime
    import io

    app = make_app(tmp_path)
    db = app.config["DATABASE"]
    client = app.test_client()

    # Create two users with stored settings and events in the database
    user1_tok = "user-1-token"
    user2_tok = "user-2-token"

    ev1 = TimetableEvent(
        uid="uid-user1-1",
        subject_name="Financial Innovations & Fintech",
        course_code="FIN521-PDM",
        faculty="Vidhu Shekhar",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 40),
        ends_at=datetime(2026, 6, 10, 11, 50),
        session_number="1"
    )
    ev2 = TimetableEvent(
        uid="uid-user2-1",
        subject_name="Financial Innovations & Fintech",
        course_code="FIN521-PDM",
        faculty="Vidhu Shekhar",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 40),
        ends_at=datetime(2026, 6, 10, 11, 50),
        session_number="1"
    )

    # Initialize setting & save event for user 1
    from class_sync.tcs import serialize_events
    set_setting(db, user1_tok, "preview_events", serialize_events([ev1]))
    save_events(db, user1_tok, [ev1])

    # Initialize setting & save event for user 2
    set_setting(db, user2_tok, "preview_events", serialize_events([ev2]))
    save_events(db, user2_tok, [ev2])

    mock_info = MandatorySessionInfo(
        course_code="FIN521",
        course_shortname="FIN521-PDM-46",
        mandatory_sessions=[1, 3]  # session 1 is mandatory
    )

    with patch("class_sync.wisenet.parse_mandatory_sessions_from_pdf", return_value=mock_info):
        # We perform upload on user1 session context
        data = {
            "pdf_files": (io.BytesIO(b"%PDF-1.4 dummy"), "FIN521-Outline.pdf")
        }
        res = client.post(
            "/wisenet/upload",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True
        )
        assert res.status_code == 200

    # Verify that BOTH user 1 and user 2 got their stored database events updated to mandatory!
    payloads_user1 = list_event_payloads(db, user1_tok)
    payloads_user2 = list_event_payloads(db, user2_tok)

    assert len(payloads_user1) == 1
    assert payloads_user1[0]["extendedProperties"]["private"]["mandatory"] == "true"
    assert payloads_user1[0]["colorId"] == "11"
    assert "🔴 MANDATORY:" in payloads_user1[0]["summary"]

    assert len(payloads_user2) == 1
    assert payloads_user2[0]["extendedProperties"]["private"]["mandatory"] == "true"
    assert payloads_user2[0]["colorId"] == "11"
    assert "🔴 MANDATORY:" in payloads_user2[0]["summary"]


def test_batch_bifurcation_on_upload(tmp_path):
    from unittest.mock import patch
    from class_sync.wisenet import MandatorySessionInfo
    from class_sync.store import save_events, list_event_payloads, set_setting, get_mandatory_sessions
    from class_sync.models import TimetableEvent
    from class_sync.security import encrypt_json
    from class_sync.tcs import serialize_events
    from datetime import datetime
    import io

    app = make_app(tmp_path)
    db = app.config["DATABASE"]
    client = app.test_client()

    user1_tok = "user-1-pgp25"
    user2_tok = "user-2-pgp26"

    ev1 = TimetableEvent(
        uid="uid-user1-1",
        subject_name="Financial Innovations & Fintech",
        course_code="FIN521-PDM",
        faculty="Vidhu Shekhar",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 40),
        ends_at=datetime(2026, 6, 10, 11, 50),
        session_number="1"
    )
    ev2 = TimetableEvent(
        uid="uid-user2-1",
        subject_name="Financial Innovations & Fintech",
        course_code="FIN521-PDM",
        faculty="Vidhu Shekhar",
        classroom="NCR5",
        starts_at=datetime(2026, 6, 10, 10, 40),
        ends_at=datetime(2026, 6, 10, 11, 50),
        session_number="1"
    )

    # Set up user 1 (pgp25)
    set_setting(db, user1_tok, "tcs_credentials_encrypted", encrypt_json({"username": "pgp25.student1@spjimr.org", "password": "abc"}))
    set_setting(db, user1_tok, "preview_events", serialize_events([ev1]))
    save_events(db, user1_tok, [ev1])

    # Set up user 2 (pgp26)
    set_setting(db, user2_tok, "tcs_credentials_encrypted", encrypt_json({"username": "pgp26.student2@spjimr.org", "password": "xyz"}))
    set_setting(db, user2_tok, "preview_events", serialize_events([ev2]))
    save_events(db, user2_tok, [ev2])

    mock_info = MandatorySessionInfo(
        course_code="FIN521",
        course_shortname="FIN521-PDM-46",
        mandatory_sessions=[1, 3]  # session 1 is mandatory
    )

    # Perform upload as User 1 (pgp25)
    with client.session_transaction() as sess:
        sess["user_token"] = user1_tok

    with patch("class_sync.wisenet.parse_mandatory_sessions_from_pdf", return_value=mock_info):
        data = {
            "pdf_files": (io.BytesIO(b"%PDF-1.4 dummy"), "FIN521-Outline.pdf")
        }
        res = client.post(
            "/wisenet/upload",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True
        )
        assert res.status_code == 200

    # Verify database lists course under pgp25 but NOT under pgp26
    pgp25_sessions = get_mandatory_sessions(db, "pgp25")
    pgp26_sessions = get_mandatory_sessions(db, "pgp26")
    assert "FIN521" in pgp25_sessions
    assert "FIN521" not in pgp26_sessions

    # Verify that only User 1 (pgp25) has events flagged as mandatory
    payloads_user1 = list_event_payloads(db, user1_tok)
    payloads_user2 = list_event_payloads(db, user2_tok)

    assert len(payloads_user1) == 1
    assert payloads_user1[0]["extendedProperties"]["private"]["mandatory"] == "true"
    assert "🔴 MANDATORY:" in payloads_user1[0]["summary"]

    assert len(payloads_user2) == 1
    assert payloads_user2[0]["extendedProperties"]["private"]["mandatory"] == "false"
    assert "🔴 MANDATORY:" not in payloads_user2[0]["summary"]




