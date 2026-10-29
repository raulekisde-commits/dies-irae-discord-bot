"""Canal-reloj UTC: qué canal de voz muestra la hora de cada servidor."""
from datetime import datetime, timezone
from typing import Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS utc_clock (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

STEP_MINUTES = 10          # Discord permite renombrar un canal 2 veces cada 10 minutos: se actualiza 1 vez cada 10


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def label_for(now: datetime) -> str:
    """'🕒 14:30 UTC' — la hora UTC redondeada hacia abajo a bloques de 10 minutos."""
    now = now.astimezone(timezone.utc)
    return f"🕒 {now.hour:02d}:{now.minute - now.minute % STEP_MINUTES:02d} UTC"


def get_clock(db_path: str, guild_id: int) -> Optional[int]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id FROM utc_clock WHERE guild_id=?", (guild_id,)).fetchone()
        return int(r["channel_id"]) if r else None
    finally:
        conn.close()


def set_clock(db_path: str, guild_id: int, channel_id: int, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO utc_clock(guild_id, channel_id, created_by, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, created_by=excluded.created_by, created_at=excluded.created_at",
                         (guild_id, channel_id, user_id, utcnow_iso()))
    finally:
        conn.close()


def clear_clock(db_path: str, guild_id: int, channel_id: Optional[int] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if channel_id is None:
                conn.execute("DELETE FROM utc_clock WHERE guild_id=?", (guild_id,))
            else:
                conn.execute("DELETE FROM utc_clock WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
    finally:
        conn.close()
