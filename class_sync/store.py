from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import TimetableEvent

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


def database_path(instance_path: str | Path) -> Path:
    if os.getenv("VERCEL"):
        path = Path("/tmp")
    else:
        path = Path(instance_path)
    path.mkdir(parents=True, exist_ok=True)
    return path / "class_sync.sqlite3"


@contextmanager
def connect(path: str | Path) -> Iterator[object]:
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import DictCursor
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _execute(conn: object, query: str, params: tuple = ()) -> object:
    if DATABASE_URL:
        query = query.replace("?", "%s")
        cur = conn.cursor() # type: ignore[attr-defined]
        cur.execute(query, params)
        return cur
    else:
        return conn.execute(query, params) # type: ignore[attr-defined]


def _executemany(conn: object, query: str, params_list: list[tuple]) -> object:
    if DATABASE_URL:
        query = query.replace("?", "%s")
        cur = conn.cursor() # type: ignore[attr-defined]
        cur.executemany(query, params_list)
        return cur
    else:
        return conn.executemany(query, params_list) # type: ignore[attr-defined]


def _executescript(conn: object, sql: str) -> None:
    if DATABASE_URL:
        with conn.cursor() as cur: # type: ignore[attr-defined]
            cur.execute(sql)
    else:
        conn.executescript(sql) # type: ignore[attr-defined]


def init_db(path: str | Path) -> None:
    """Initialise the database, migrating from single-user schema if needed."""
    with connect(path) as conn:
        if not DATABASE_URL:
            # Detect old single-user schema (no user_token column) and drop it so
            # the new multi-user schema is applied cleanly.
            try:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()] # type: ignore[attr-defined]
                if cols and "user_token" not in cols:
                    conn.executescript( # type: ignore[attr-defined]
                        "DROP TABLE IF EXISTS settings; DROP TABLE IF EXISTS timetable_events;"
                    )
            except Exception:
                pass

        # Migrate mandatory_sessions to batch-specific schema if it has old format (no batch column)
        try:
            if DATABASE_URL:
                with conn.cursor() as cur: # type: ignore[attr-defined]
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'mandatory_sessions' AND column_name = 'batch'"
                    )
                    if not cur.fetchone():
                        cur.execute("DROP TABLE IF EXISTS mandatory_sessions CASCADE")
            else:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(mandatory_sessions)").fetchall()] # type: ignore[attr-defined]
                if cols and "batch" not in cols:
                    conn.execute("DROP TABLE IF EXISTS mandatory_sessions") # type: ignore[attr-defined]
        except Exception:
            pass

        _executescript(
            conn,
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
                batch        TEXT NOT NULL,
                course_code  TEXT NOT NULL,
                session_nums TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (batch, course_code)
            );
            """,
        )


# ── Settings ───────────────────────────────────────────────────────────────

def set_setting(path: str | Path, user_token: str, key: str, value: object) -> None:
    with connect(path) as conn:
        _execute(
            conn,
            "INSERT INTO settings(user_token, key, value) VALUES(?, ?, ?) "
            "ON CONFLICT(user_token, key) DO UPDATE SET value = excluded.value",
            (user_token, key, json.dumps(value)),
        )


def get_setting(path: str | Path, user_token: str, key: str, default: object = None) -> object:
    with connect(path) as conn:
        row = _execute(
            conn,
            "SELECT value FROM settings WHERE user_token = ? AND key = ?",
            (user_token, key),
        ).fetchone() # type: ignore[attr-defined]
    return json.loads(row["value"]) if row else default


def delete_setting(path: str | Path, user_token: str, key: str) -> None:
    with connect(path) as conn:
        _execute(
            conn,
            "DELETE FROM settings WHERE user_token = ? AND key = ?",
            (user_token, key),
        )


# ── Events ─────────────────────────────────────────────────────────────────────

def clear_events(path: str | Path, user_token: str) -> None:
    with connect(path) as conn:
        _execute(conn, "DELETE FROM timetable_events WHERE user_token = ?", (user_token,))


def save_events(path: str | Path, user_token: str, events: list[TimetableEvent]) -> None:
    now = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        for event in events:
            _execute(
                conn,
                "INSERT INTO timetable_events(user_token, uid, payload, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_token, uid) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (user_token, event.uid, json.dumps(event.google_payload()), now),
            )


def list_event_payloads(path: str | Path, user_token: str) -> list[dict]:
    with connect(path) as conn:
        rows = _execute(
            conn,
            "SELECT uid, payload, synced_event_id FROM timetable_events "
            "WHERE user_token = ? ORDER BY uid",
            (user_token,),
        ).fetchall() # type: ignore[attr-defined]
    payloads = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload["uid"] = row["uid"]
        payload["synced_event_id"] = row["synced_event_id"]
        payloads.append(payload)
    return payloads


def mark_synced(path: str | Path, user_token: str, uid: str, google_event_id: str) -> None:
    with connect(path) as conn:
        _execute(
            conn,
            "UPDATE timetable_events SET synced_event_id = ? WHERE user_token = ? AND uid = ?",
            (google_event_id, user_token, uid),
        )


def mark_many_synced(path: str | Path, user_token: str, event_ids: dict[str, str]) -> None:
    with connect(path) as conn:
        _executemany(
            conn,
            "UPDATE timetable_events SET synced_event_id = ? WHERE user_token = ? AND uid = ?",
            [(event_id, user_token, uid) for uid, event_id in event_ids.items()],
        )


# ── Mandatory sessions ─────────────────────────────────────────────────────────

def save_mandatory_sessions(
    path: str | Path, batch: str, mandatory_sessions: dict[str, list[int]]
) -> None:
    """Persist mandatory session data centrally for a batch: (batch, course_code) → list of session numbers."""
    now = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        for course_code, session_nums in mandatory_sessions.items():
            _execute(
                conn,
                "INSERT INTO mandatory_sessions(batch, course_code, session_nums, updated_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(batch, course_code) DO UPDATE SET "
                "session_nums = excluded.session_nums, updated_at = excluded.updated_at",
                (batch, course_code, json.dumps(session_nums), now),
            )


def get_mandatory_sessions(path: str | Path, batch: str) -> dict[str, list[int]]:
    """Load mandatory session data centrally for a batch as course_code → list[int]."""
    res: dict[str, list[int]] = {}
    with connect(path) as conn:
        rows = _execute(
            conn,
            "SELECT course_code, session_nums FROM mandatory_sessions WHERE batch = ? ORDER BY updated_at ASC",
            (batch,),
        ).fetchall() # type: ignore[attr-defined]
        for row in rows:
            res[row["course_code"]] = json.loads(row["session_nums"])
    return res


def clear_mandatory_sessions(path: str | Path, batch: str) -> None:
    """Remove all central mandatory session records for a batch."""
    with connect(path) as conn:
        _execute(conn, "DELETE FROM mandatory_sessions WHERE batch = ?", (batch,))


def get_all_user_tokens(path: str | Path) -> list[str]:
    """Get all unique user tokens stored in the settings table."""
    with connect(path) as conn:
        rows = _execute(conn, "SELECT DISTINCT user_token FROM settings").fetchall() # type: ignore[attr-defined]
    return [row[0] for row in rows]


def reapply_mandatory_flags_to_batch(
    path: str | Path,
    batch: str,
    mandatory_data: dict[str, list[int]],
) -> None:
    """Re-flag stored timetable events as mandatory for all users in the batch and save in bulk."""
    from datetime import datetime as _DT
    
    now = datetime.now(UTC).isoformat()
    
    with connect(path) as conn:
        # Find all user tokens in the batch
        batch_serialized = json.dumps(batch)
        rows = _execute(
            conn,
            "SELECT user_token FROM settings WHERE key = 'batch' AND value = ?",
            (batch_serialized,),
        ).fetchall() # type: ignore[attr-defined]
        user_tokens = [r["user_token"] for r in rows]
        
        if batch == "general":
            all_tokens_rows = _execute(conn, "SELECT DISTINCT user_token FROM settings").fetchall() # type: ignore[attr-defined]
            all_tokens = {r["user_token"] for r in all_tokens_rows}
            has_batch_rows = _execute(conn, "SELECT user_token FROM settings WHERE key = 'batch'").fetchall() # type: ignore[attr-defined]
            has_batch_tokens = {r["user_token"] for r in has_batch_rows}
            user_tokens.extend(list(all_tokens - has_batch_tokens))
            user_tokens = list(set(user_tokens))
            
        for user_token in user_tokens:
            row = _execute(
                conn,
                "SELECT value FROM settings WHERE user_token = ? AND key = 'preview_events'",
                (user_token,),
            ).fetchone() # type: ignore[attr-defined]
            if not row:
                continue
                
            try:
                events_raw = json.loads(row["value"])
            except Exception:
                continue
            if not events_raw:
                continue
                
            updated: list[dict] = []
            timetable_events_list: list[tuple] = []
            
            for e in events_raw:
                code = (e.get("course_code") or "").split("-")[0].strip().upper()
                sess = e.get("session_number", "")
                is_mandatory = False
                if code in mandatory_data and sess:
                    try:
                        is_mandatory = int(sess) in mandatory_data[code]
                    except ValueError:
                        pass
                        
                e_updated = dict(e)
                e_updated["mandatory"] = is_mandatory
                subject = e.get("title", "").replace("🔴 MANDATORY: ", "")
                if is_mandatory:
                    e_updated["title"] = f"🔴 MANDATORY: {subject}"
                else:
                    e_updated["title"] = subject
                updated.append(e_updated)
                
                starts_at = _DT.fromisoformat(e["starts_at"])
                ends_at = _DT.fromisoformat(e["ends_at"])
                
                ev_obj = TimetableEvent(
                    uid=e["uid"],
                    subject_name=subject,
                    course_code=e.get("course_code", ""),
                    faculty=e.get("faculty", ""),
                    classroom=e.get("classroom", ""),
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status=e.get("status", ""),
                    mandatory=is_mandatory,
                    session_number=sess,
                    activity_name=e.get("activity_name", ""),
                )
                timetable_events_list.append(
                    (user_token, e["uid"], json.dumps(ev_obj.google_payload()), now)
                )
                
            # Write updated settings
            _execute(
                conn,
                "INSERT INTO settings(user_token, key, value) VALUES(?, 'preview_events', ?) "
                "ON CONFLICT(user_token, key) DO UPDATE SET value = excluded.value",
                (user_token, json.dumps(updated)),
            )
            
            # Write updated timetable_events
            if timetable_events_list:
                _executemany(
                    conn,
                    "INSERT INTO timetable_events(user_token, uid, payload, updated_at) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(user_token, uid) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                    timetable_events_list,
                )


