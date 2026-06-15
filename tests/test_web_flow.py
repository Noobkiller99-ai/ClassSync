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
