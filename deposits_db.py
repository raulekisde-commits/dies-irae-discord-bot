"""Pasar silver a Wuju Coins — tickets de depósito (lógica y persistencia, sin Discord).

El ticket solo registra QUIÉN pidió pasar CUÁNTO silver y si sigue abierto; no mueve Wuju Coins:
un Council revisa la captura del depósito y acredita aparte (por ejemplo con /addmoney).
"""
from dataclasses import dataclass
from typing import Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS dep_tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL,
    silver     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',      -- open | closed
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    closed_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dep_tickets_user ON dep_tickets(guild_id, user_id, status);

CREATE TABLE IF NOT EXISTS dep_panels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
"""


class DepositError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


@dataclass
class DepositTicket:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    silver: int
    status: str


def _row(r) -> Optional[DepositTicket]:
    if r is None:
        return None
    return DepositTicket(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), int(r["user_id"]), int(r["silver"]), r["status"])


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def parse_silver(text: str) -> int:
    """Cantidad entera positiva (acepta 1.500.000 o 1,500,000)."""
    try:
        return eco.parse_amount(text)
    except eco.AmountError as exc:
        raise DepositError(str(exc))


def create_ticket(db_path: str, guild_id: int, channel_id: int, user_id: int, silver: int) -> int:
    if silver <= 0:
        raise DepositError("La cantidad tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO dep_tickets(guild_id, channel_id, user_id, silver, status, created_at) VALUES (?,?,?,?, 'open', ?)",
                               (guild_id, channel_id, user_id, silver, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def get_by_channel(db_path: str, channel_id: int) -> Optional[DepositTicket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM dep_tickets WHERE channel_id = ?", (channel_id,)).fetchone())
    finally:
        conn.close()


def open_ticket_of(db_path: str, guild_id: int, user_id: int) -> Optional[DepositTicket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute(
            "SELECT * FROM dep_tickets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (guild_id, user_id)).fetchone())
    finally:
        conn.close()


def close_ticket(db_path: str, ticket_id: int, actor_id: int) -> bool:
    """False si ya estaba cerrado (dos Council a la vez: solo uno lo cierra)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE dep_tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ? AND status = 'open'",
                               (utcnow_iso(), actor_id, ticket_id))
            return cur.rowcount == 1
    finally:
        conn.close()


def discard_ticket(db_path: str, ticket_id: int) -> None:
    """Deshace un ticket cuyo canal no se pudo armar."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM dep_tickets WHERE id = ? AND status = 'open'", (ticket_id,))
    finally:
        conn.close()


def get_panel(db_path: str, guild_id: int) -> Optional[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id, message_id FROM dep_panels WHERE guild_id = ?", (guild_id,)).fetchone()
        return (int(r["channel_id"]), int(r["message_id"])) if r else None
    finally:
        conn.close()


def set_panel(db_path: str, guild_id: int, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO dep_panels(guild_id, channel_id, message_id) VALUES (?,?,?)", (guild_id, channel_id, message_id))
    finally:
        conn.close()
