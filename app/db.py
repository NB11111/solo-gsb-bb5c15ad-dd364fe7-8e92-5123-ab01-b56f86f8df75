"""SQLite persistence. Schema is created on application startup."""

from __future__ import annotations

import os
import sqlite3
import threading

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./merges.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS merges (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    revision        INTEGER NOT NULL,
    base_doc        TEXT NOT NULL,
    left_doc        TEXT NOT NULL,
    right_doc       TEXT NOT NULL,
    document        TEXT NOT NULL,
    conflicts       TEXT NOT NULL,
    result          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        directory = os.path.dirname(os.path.abspath(DATABASE_PATH))
        os.makedirs(directory, exist_ok=True)
        _conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_schema() -> None:
    with _lock:
        connect().executescript(SCHEMA)
        connect().commit()


def execute(query: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = connect().execute(query, params)
        connect().commit()
        return cur


def query_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(query, params).fetchone()
