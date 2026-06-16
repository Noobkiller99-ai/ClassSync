from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import TimetableEvent


def database_path(instance_path: str | Path) -> Path:
    path = Path(instance_path)
    path.mkdir(parents=True, exist_ok=True)
    return path / "class_sync.sqlite3"


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: str | Path) -> None:
    """Initialise the database, migrating from single-user schema if needed."""
    with connect(path) as conn:
        # Detect old single-user schema (no user_token column) and drop it so
        # the new multi-user schema is applied cleanly.
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()]
            if cols and "user_token" not in cols:
                conn.executescript(
                    "DROP TABLE IF EXISTS settings; DROP TABLE IF EXISTS timetable_events;"
                )
        except Exception:
            pass

        # Migrate mandatory_sessions to global central store schema if it has old format
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(mandatory_sessions)").fetchall()]
            if cols and "user_token" in cols:
                conn.execute("DROP TABLE IF EXISTS mandatory_sessions")
        except Exception:
            pass

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_token TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                PRIMARY KEY (user_token, key)
            );
            CREATE TABLE IF NOT EXISTS timetable_events (
                user_token      TEXT NOT NULL,
                uid             TEXT NOT NULL,
                payload         TEXT NOT NULL,
                synced_event_id TEXT,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (user_token, uid)
            );
            CREATE TABLE IF NOT EXISTS mandatory_sessions (
                course_code  TEXT PRIMARY KEY,
                session_nums TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            """
        )


# ── Settings ──────────────────────────────────────────────────────────────────

def set_setting(path: str | Path, user_token: str, key: str, value: object) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO settings(user_token, key, value) VALUES(?, ?, ?) "
            "ON CONFLICT(user_token, key) DO UPDATE SET value = excluded.value",
            (user_token, key, json.dumps(value)),
        )


def get_setting(path: str | Path, user_token: str, key: str, default: object = None) -> object:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_token = ? AND key = ?",
            (user_token, key),
        ).fetchone()
    return json.loads(row["value"]) if row else default


def delete_setting(path: str | Path, user_token: str, key: str) -> None:
    with connect(path) as conn:
        conn.execute(
            "DELETE FROM settings WHERE user_token = ? AND key = ?",
            (user_token, key),
        )


def get_all_users_with_credentials(path: str | Path) -> list[str]:
    """Return user_tokens that have both TCS and Google credentials saved."""
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT user_token
            FROM settings
            WHERE key IN ('tcs_credentials_encrypted', 'google_credentials')
            GROUP BY user_token
            HAVING COUNT(DISTINCT key) = 2
            """
        ).fetchall()
    return [row[0] for row in rows]


# ── Events ─────────────────────────────────────────────────────────────────────

def clear_events(path: str | Path, user_token: str) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM timetable_events WHERE user_token = ?", (user_token,))


def save_events(path: str | Path, user_token: str, events: list[TimetableEvent]) -> None:
    now = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        for event in events:
            conn.execute(
                "INSERT INTO timetable_events(user_token, uid, payload, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_token, uid) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (user_token, event.uid, json.dumps(event.google_payload()), now),
            )


def list_event_payloads(path: str | Path, user_token: str) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT uid, payload, synced_event_id FROM timetable_events "
            "WHERE user_token = ? ORDER BY uid",
            (user_token,),
        ).fetchall()
    payloads = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload["uid"] = row["uid"]
        payload["synced_event_id"] = row["synced_event_id"]
        payloads.append(payload)
    return payloads


def mark_synced(path: str | Path, user_token: str, uid: str, google_event_id: str) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE timetable_events SET synced_event_id = ? WHERE user_token = ? AND uid = ?",
            (google_event_id, user_token, uid),
        )


def mark_many_synced(path: str | Path, user_token: str, event_ids: dict[str, str]) -> None:
    with connect(path) as conn:
        conn.executemany(
            "UPDATE timetable_events SET synced_event_id = ? WHERE user_token = ? AND uid = ?",
            [(event_id, user_token, uid) for uid, event_id in event_ids.items()],
        )


# ── Mandatory sessions ─────────────────────────────────────────────────────────

def save_mandatory_sessions(
    path: str | Path, mandatory_sessions: dict[str, list[int]]
) -> None:
    """Persist mandatory session data centrally: course_code → list of session numbers."""
    now = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        for course_code, session_nums in mandatory_sessions.items():
            conn.execute(
                "INSERT INTO mandatory_sessions(course_code, session_nums, updated_at) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(course_code) DO UPDATE SET "
                "session_nums = excluded.session_nums, updated_at = excluded.updated_at",
                (course_code, json.dumps(session_nums), now),
            )


def get_mandatory_sessions(path: str | Path) -> dict[str, list[int]]:
    """Load mandatory session data centrally as course_code → list[int]."""
    res: dict[str, list[int]] = {}
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT course_code, session_nums FROM mandatory_sessions ORDER BY updated_at ASC"
        ).fetchall()
        for row in rows:
            res[row["course_code"]] = json.loads(row["session_nums"])
    return res


def clear_mandatory_sessions(path: str | Path) -> None:
    """Remove all central mandatory session records."""
    with connect(path) as conn:
        conn.execute("DELETE FROM mandatory_sessions")


def get_all_user_tokens(path: str | Path) -> list[str]:
    """Get all unique user tokens stored in the settings table."""
    with connect(path) as conn:
        rows = conn.execute("SELECT DISTINCT user_token FROM settings").fetchall()
    return [row[0] for row in rows]
