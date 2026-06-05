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
            callback_fired  INTEGER DEFAULT 0,  -- 完成回调是否已派发（防重复触发）
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
    # 分组任务（type='batch'，如一本漫画/一个 ASMR）的子文件。父 job 一行、
    # 这里 N 行；父 job 的进度/状态由对账协程聚合后写回 jobs 行。
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          TEXT NOT NULL,
            url             TEXT NOT NULL,
            rel_path        TEXT,
            headers         TEXT,
            optional        INTEGER DEFAULT 0,
            gid             TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            total_bytes     INTEGER DEFAULT 0,
            completed_bytes INTEGER DEFAULT 0,
            download_speed  INTEGER DEFAULT 0,
            error           TEXT
        )
        """
    )
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_job_files_job ON job_files(job_id)")
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    # 老库迁移：早于回调功能建的库没有 callback_fired 列，补上。
    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(jobs)")}
    if "callback_fired" not in cols:
        _conn.execute("ALTER TABLE jobs ADD COLUMN callback_fired INTEGER DEFAULT 0")
    file_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(job_files)")}
    if "optional" not in file_cols:
        _conn.execute("ALTER TABLE job_files ADD COLUMN optional INTEGER DEFAULT 0")
    _conn.commit()


def get_setting(key: str, default: str = "") -> str:
    assert _conn is not None
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    assert _conn is not None
    with _lock:
        now = time.time()
        _conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
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


def claim_callback(job_id: str) -> bool:
    """原子地认领完成回调：把 callback_fired 从 0 置 1，返回是否抢到。

    用 UPDATE ... WHERE callback_fired=0 保证多次对账只派发一次回调（即使并发）。
    """
    with _lock:
        cur = _conn.execute(
            "UPDATE jobs SET callback_fired = 1 WHERE id = ? AND callback_fired = 0",
            (job_id,),
        )
        _conn.commit()
        return cur.rowcount > 0


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
        _conn.execute("DELETE FROM job_files WHERE job_id = ?", (job_id,))
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


# --- 分组任务（batch） ------------------------------------------------------

def create_batch(
    *,
    name: str,
    dest_dir: str,
    callback_url: Optional[str],
    files: list[dict],
) -> str:
    """files: [{url, rel_path, headers(dict|None), optional(bool)}]。父 job type='batch'。"""
    job_id = uuid.uuid4().hex
    now = _now()
    with _lock:
        _conn.execute(
            """INSERT INTO jobs (id, gid, type, source, dest_dir, filename, headers,
                                 callback_url, status, name, created_at, updated_at)
               VALUES (?, NULL, 'batch', ?, ?, NULL, NULL, ?, 'pending', ?, ?, ?)""",
            (job_id, name, dest_dir, callback_url, name, now, now),
        )
        for f in files:
            _conn.execute(
                """INSERT INTO job_files (job_id, url, rel_path, headers, optional, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (job_id, f["url"], f.get("rel_path"),
                 json.dumps(f["headers"]) if f.get("headers") else None,
                 1 if f.get("optional") else 0),
            )
        _conn.commit()
    return job_id


def batch_files(job_id: str) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM job_files WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def file_set_gid(file_id: int, gid: Optional[str]) -> None:
    with _lock:
        _conn.execute("UPDATE job_files SET gid = ? WHERE id = ?", (gid, file_id))
        _conn.commit()


def file_set_status(file_id: int, status: str) -> None:
    with _lock:
        _conn.execute("UPDATE job_files SET status = ? WHERE id = ?", (status, file_id))
        _conn.commit()


def file_update(
    file_id: int, *, status: str, total_bytes: int, completed_bytes: int,
    download_speed: int = 0, error: Optional[str] = None,
) -> None:
    with _lock:
        _conn.execute(
            """UPDATE job_files SET status = ?, total_bytes = ?, completed_bytes = ?,
                                    download_speed = ?, error = ? WHERE id = ?""",
            (status, total_bytes, completed_bytes, download_speed, error, file_id),
        )
        _conn.commit()
