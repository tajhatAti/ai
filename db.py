"""
প্রতিটা /edit রান-এর audit log রাখার জন্য একটা ছোট SQLite ডাটাবেস।
/history আর /undo কমান্ড এখান থেকেই ডেটা নেয়।
"""

import json
import sqlite3
import threading
import time

from config import CFG

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(CFG.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                created_at REAL,
                prompt TEXT,
                branch TEXT,
                status TEXT,
                files_json TEXT,
                detail TEXT
            )
            """
        )
        conn.commit()


def create_run(chat_id, user_id, prompt):
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (chat_id, user_id, created_at, prompt, branch, status, files_json, detail) "
            "VALUES (?, ?, ?, ?, '', 'running', '[]', '')",
            (chat_id, user_id, time.time(), prompt),
        )
        conn.commit()
        return cur.lastrowid


def update_run(run_id, **fields):
    if not fields:
        return
    allowed = {"branch", "status", "files_json", "detail"}
    sets = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        values.append(v)
    if not sets:
        return
    values.append(run_id)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", values)
        conn.commit()


def set_run_files(run_id, files_list):
    update_run(run_id, files_json=json.dumps(files_list, ensure_ascii=False))


def get_last_pushed_run(chat_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE chat_id = ? AND status = 'pushed' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def list_runs(chat_id, limit=10):
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
