"""Dónde están publicados los paneles de /apuestas panel (los números salen de stats_db: no se duplican)."""
import sqlite3
from typing import Optional, Tuple

from dbutil import connect, write_tx

KINDS = ("wagered", "net", "lost_total", "wr_dice", "wr_coinflip", "blacklist")                 # Top apostado · Top ganado (ganado − perdido, puede ser negativo)
LEGACY_KINDS = ("won", "lost")             # paneles de la versión anterior (se reemplazan al publicar de nuevo)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rank_panels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,                    -- wagered | net (y los viejos won | lost)
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_panel(db_path: str, guild_id: int, kind: str) -> Optional[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id, message_id FROM rank_panels WHERE guild_id = ? AND kind = ?", (guild_id, kind)).fetchone()
        return (int(r["channel_id"]), int(r["message_id"])) if r else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def set_panel(db_path: str, guild_id: int, kind: str, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO rank_panels(guild_id, kind, channel_id, message_id) VALUES (?,?,?,?)",
                         (guild_id, kind, channel_id, message_id))
    finally:
        conn.close()


def clear_panel(db_path: str, guild_id: int, kind: str) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM rank_panels WHERE guild_id = ? AND kind = ?", (guild_id, kind))
    finally:
        conn.close()
