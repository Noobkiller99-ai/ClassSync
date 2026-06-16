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


def test_wisenet_ingest_flow(tmp_path):
    from unittest.mock import patch
    from class_sync.wisenet import MandatorySessionInfo
    import base64
    from urllib.parse import urlparse, parse_qs

    app = make_app(tmp_path)
    client = app.test_client()

    # Get index to generate user_token
    client.get("/")

    # Initiate connect to get state
    response = client.post("/wisenet/connect", follow_redirects=False)
    assert response.status_code == 302
    
    parsed = urlparse(response.location)
    wants = parse_qs(parsed.query).get("wants", [None])[0]
    assert wants is not None
    parsed_wants = urlparse(wants)
    state = parse_qs(parsed_wants.query).get("state", [None])[0]
    assert state is not None

    dummy_pdf = base64.b64encode(b"%PDF-1.4...").decode("utf-8")
    
    mock_info = MandatorySessionInfo(
        course_code="FIN521",
        course_shortname="FIN521-PDM-46",
        mandatory_sessions=[1, 3, 5]
    )

    with patch("class_sync.wisenet.parse_mandatory_sessions_from_pdf", return_value=mock_info):
        res = client.post(
            "/wisenet/ingest",
            json={
                "state": state,
                "sesskey": "test_sesskey",
                "course_code": "FIN521-PDM-46",
                "pdf_base64": dummy_pdf
            }
        )
        assert res.status_code == 200
        assert res.json["ok"] is True
        assert res.json["count"] == 3

    # Now check /wisenet/sync_done
    response = client.get(f"/wisenet/sync_done", follow_redirects=True)
    assert response.status_code == 200
    assert b"Wisenet sync done" in response.data


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
    sessions = get_mandatory_sessions(db, "different-user-token")
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

        # Verify update was called for existing-google-id-1 and existing-google-id-2
        # Verify event payload bodies exclude non-google fields like uid and synced_event_id
        body_1 = {k: v for k, v in payloads[0].items() if k not in {"uid", "synced_event_id"}}
        body_2 = {k: v for k, v in payloads[1].items() if k not in {"uid", "synced_event_id"}}
        body_3 = {k: v for k, v in payloads[2].items() if k not in {"uid", "synced_event_id"}}

        mock_service.events().update.assert_any_call(
            calendarId="primary", eventId="existing-google-id-1", body=body_1
        )
        mock_service.events().update.assert_any_call(
            calendarId="primary", eventId="existing-google-id-2", body=body_2
        )
        # Verify insert was called for new event
        mock_service.events().insert.assert_any_call(
            calendarId="primary", body=body_3
        )



