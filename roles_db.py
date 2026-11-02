"""Registro de cambios de roles hechos con /rol-dar y /rol-quitar (quién, a quién, qué rol, cuándo)."""
from typing import List, Optional

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,      -- a quién se le cambió el rol
    role_id    INTEGER NOT NULL,
    role_name  TEXT NOT NULL,
    action     TEXT NOT NULL,         -- 'dar' | 'quitar'
    actor_id   INTEGER NOT NULL,      -- quien ejecutó el comando
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_role_changes_user ON role_changes(user_id, id);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def log_change(db_path: str, user_id: int, role_id: int, role_name: str, action: str, actor_id: int, reason: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO role_changes(user_id, role_id, role_name, action, actor_id, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                         (user_id, role_id, role_name, action, actor_id, (reason or "").strip()[:200] or None, utcnow_iso()))
    finally:
        conn.close()


def history(db_path: str, user_id: Optional[int] = None, limit: int = 15) -> List[dict]:
    conn = connect(db_path)
    try:
        if user_id is None:
            rows = conn.execute("SELECT * FROM role_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM role_changes WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
