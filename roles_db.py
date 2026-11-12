"""Registro de cambios de roles hechos con /rol dar y /rol quitar (quién, a quién, qué rol, cuándo)."""
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
CREATE TABLE IF NOT EXISTS obs_votes (            -- votaciones (!votacion) para sacar el rol Observación
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL,                 -- post del foro de VODs donde se abrió
    user_id     INTEGER NOT NULL,                 -- a quién se le votaría sacar Observación
    started_by  INTEGER NOT NULL,
    message_id  INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',     -- 'open' | 'aprobada' | 'rechazada'
    closed_by   INTEGER,                          -- Council que aprobó (o NULL si fue por mayoría / rechazo)
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_votes_thread ON obs_votes(thread_id, status);
CREATE TABLE IF NOT EXISTS obs_vote_ballots (
    vote_id   INTEGER NOT NULL REFERENCES obs_votes(id),
    voter_id  INTEGER NOT NULL,
    choice    INTEGER NOT NULL,                   -- 1 = sí (sacar), 0 = no
    PRIMARY KEY (vote_id, voter_id)
);
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


# ---------- votaciones para sacar Observación ----------
def open_vote(db_path: str, thread_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM obs_votes WHERE thread_id=? AND status='open' ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def create_vote(db_path: str, thread_id: int, user_id: int, started_by: int) -> Optional[int]:
    """Crea la votación; None si ya hay una abierta en ese post."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM obs_votes WHERE thread_id=? AND status='open'", (thread_id,)).fetchone():
                return None
            cur = conn.execute("INSERT INTO obs_votes(thread_id, user_id, started_by, created_at) VALUES (?,?,?,?)",
                               (thread_id, user_id, started_by, utcnow_iso()))
            return cur.lastrowid
    finally:
        conn.close()


def set_vote_message(db_path: str, vote_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE obs_votes SET message_id=? WHERE id=?", (message_id, vote_id))
    finally:
        conn.close()


def vote_by_message(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM obs_votes WHERE message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def cast_ballot(db_path: str, vote_id: int, voter_id: int, yes: bool) -> bool:
    """Registra (o cambia) el voto. False si la votación ya no está abierta."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if not conn.execute("SELECT 1 FROM obs_votes WHERE id=? AND status='open'", (vote_id,)).fetchone():
                return False
            conn.execute("INSERT INTO obs_vote_ballots(vote_id, voter_id, choice) VALUES (?,?,?) "
                         "ON CONFLICT(vote_id, voter_id) DO UPDATE SET choice=excluded.choice", (vote_id, voter_id, 1 if yes else 0))
            return True
    finally:
        conn.close()


def ballots(db_path: str, vote_id: int) -> dict:
    """{voter_id: True (sí) / False (no)}"""
    conn = connect(db_path)
    try:
        return {r["voter_id"]: bool(r["choice"]) for r in conn.execute("SELECT voter_id, choice FROM obs_vote_ballots WHERE vote_id=?", (vote_id,))}
    finally:
        conn.close()


def close_vote(db_path: str, vote_id: int, status: str, closed_by: Optional[int] = None) -> bool:
    """Cierra la votación una sola vez (True solo para quien la cerró)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE obs_votes SET status=?, closed_by=?, closed_at=? WHERE id=? AND status='open'",
                               (status, closed_by, utcnow_iso(), vote_id))
            return cur.rowcount == 1
    finally:
        conn.close()


def reopen_vote(db_path: str, vote_id: int) -> None:
    """Vuelve a abrir una votación que se cerró pero no se pudo aplicar (p. ej. Discord no dejó sacar el rol)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE obs_votes SET status='open', closed_by=NULL, closed_at=NULL WHERE id=?", (vote_id,))
    finally:
        conn.close()
