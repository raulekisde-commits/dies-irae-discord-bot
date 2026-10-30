"""Casino: abierto/cerrado y lista de bloqueados (lógica y persistencia, sin Discord)."""
from typing import List, Optional

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS casino_state (
    guild_id INTEGER PRIMARY KEY,
    is_open  INTEGER NOT NULL DEFAULT 1,
    changed_by INTEGER,
    changed_at TEXT
);
CREATE TABLE IF NOT EXISTS casino_ban (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    added_by   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

CLOSED_TEXT = "🔒 El casino está cerrado por ahora."
BANNED_TEXT = "🚫 Estás bloqueado del casino: no podés participar."


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def is_open(db_path: str, guild_id: int) -> bool:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT is_open FROM casino_state WHERE guild_id = ?", (guild_id,)).fetchone()
        return True if r is None else bool(r["is_open"])
    finally:
        conn.close()


def set_open(db_path: str, guild_id: int, open_: bool, actor_id: int) -> bool:
    """Devuelve True si el estado cambió (False si ya estaba así)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT is_open FROM casino_state WHERE guild_id = ?", (guild_id,)).fetchone()
            current = True if r is None else bool(r["is_open"])
            conn.execute("INSERT OR REPLACE INTO casino_state(guild_id, is_open, changed_by, changed_at) VALUES (?,?,?,?)",
                         (guild_id, 1 if open_ else 0, actor_id, utcnow_iso()))
            return current != open_
    finally:
        conn.close()


def is_banned(db_path: str, guild_id: int, user_id: int) -> bool:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM casino_ban WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone() is not None
    finally:
        conn.close()


def toggle_ban(db_path: str, guild_id: int, user_id: int, actor_id: int) -> bool:
    """Alterna: si no estaba en la lista lo agrega (devuelve True = ahora bloqueado); si estaba, lo saca (False)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM casino_ban WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone():
                conn.execute("DELETE FROM casino_ban WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                return False
            conn.execute("INSERT INTO casino_ban(guild_id, user_id, added_by, created_at) VALUES (?,?,?,?)",
                         (guild_id, user_id, actor_id, utcnow_iso()))
            return True
    finally:
        conn.close()


def list_banned(db_path: str, guild_id: int) -> List[int]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        return [int(r["user_id"]) for r in conn.execute(
            "SELECT user_id FROM casino_ban WHERE guild_id = ? ORDER BY created_at, user_id", (guild_id,))]
    finally:
        conn.close()


def block_reason(db_path: str, guild_id: int, *user_ids: int, check_open: bool = True) -> Optional[str]:
    """Texto para mostrar si el casino está cerrado o alguna de las personas está bloqueada (None = puede jugar).

    `check_open=False` se usa al ANOTARSE en un juego que ya estaba en marcha (dados, coinflip, 1v1): con el casino cerrado no se
    puede crear nada nuevo, pero lo que ya empezó se puede terminar. La lista negra se aplica siempre."""
    if check_open and not is_open(db_path, guild_id):
        return CLOSED_TEXT
    for uid in user_ids:
        if is_banned(db_path, guild_id, uid):
            return BANNED_TEXT if len(user_ids) == 1 else f"🚫 <@{uid}> está bloqueado del casino: no puede participar."
    for uid in user_ids:
        if eco.is_frozen(db_path, uid):
            return eco.FROZEN_TEXT if len(user_ids) == 1 else f"🧊 <@{uid}> tiene el balance congelado (ticket de Cobrar balance abierto): no puede participar."
    return None
