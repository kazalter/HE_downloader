"""SQLite 任务库 —— 下载中心的唯一真相源。

不依赖 aria2 的内存态：每个 job 一行，记录原始请求（source/dest/headers）+ 当前
aria2 gid + 进度快照。aria2 或 gateway 重启后，对账协程能据此把未完成任务重新入队，
所以"重启不丢"。纯 stdlib sqlite3，不加依赖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

DB_PATH = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")

TERMINAL = {"complete", "error", "canceled"}

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def init() -> None:
    global _conn
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id              TEXT PRIMARY KEY,
            gid             TEXT,
            type            TEXT NOT NULL,
            source          TEXT NOT NULL,
            dest_dir        TEXT,
            filename        TEXT,
            headers         TEXT,           -- JSON object or NULL
            callback_url    TEXT,
            status          TEXT NOT NULL,  -- pending|active|waiting|paused|complete|error|canceled
            name            TEXT DEFAULT '',
            total_bytes     INTEGER DEFAULT 0,
            completed_bytes INTEGER DEFAULT 0,
            download_speed  INTEGER DEFAULT 0,
            error           TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        )
        """
    )
    _conn.commit()


def _now() -> float:
    return time.time()


def create(
    *,
    type: str,
    source: str,
    dest_dir: Optional[str],
    filename: Optional[str],
    headers: Optional[dict],
    callback_url: Optional[str],
) -> str:
    job_id = uuid.uuid4().hex
    now = _now()
    with _lock:
        _conn.execute(
            """INSERT INTO jobs (id, gid, type, source, dest_dir, filename, headers,
                                 callback_url, status, created_at, updated_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                job_id, type, source, dest_dir, filename,
                json.dumps(headers) if headers else None,
                callback_url, now, now,
            ),
        )
        _conn.commit()
    return job_id


def set_gid(job_id: str, gid: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE jobs SET gid = ?, updated_at = ? WHERE id = ?",
            (gid, _now(), job_id),
        )
        _conn.commit()


def update_progress(
    job_id: str,
    *,
    status: str,
    name: str,
    total_bytes: int,
    completed_bytes: int,
    download_speed: int,
    error: Optional[str],
) -> None:
    with _lock:
        _conn.execute(
            """UPDATE jobs SET status = ?, name = ?, total_bytes = ?, completed_bytes = ?,
                               download_speed = ?, error = ?, updated_at = ?
               WHERE id = ?""",
            (status, name, total_bytes, completed_bytes, download_speed, error, _now(), job_id),
        )
        _conn.commit()


def set_status(job_id: str, status: str, error: Optional[str] = None) -> None:
    with _lock:
        _conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, _now(), job_id),
        )
        _conn.commit()


def get(job_id: str) -> Optional[dict]:
    with _lock:
        row = _conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_all() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def non_terminal() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM jobs WHERE status NOT IN ('complete','error','canceled')"
        ).fetchall()
    return [dict(r) for r in rows]


def delete(job_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()


def headers_of(row: dict) -> Optional[dict]:
    raw = row.get("headers")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
