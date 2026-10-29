"""Dónde están publicados los paneles de /apuestas panel (los números salen de stats_db: no se duplican)."""
import sqlite3
from typing import Optional, Tuple

from dbutil import connect, write_tx

KINDS = ("won", "lost", "wagered")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rank_panels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,                    -- won | lost | wagered
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
