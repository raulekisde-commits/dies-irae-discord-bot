"""Sorteos (/sorteo): datos y transiciones, sin Discord.

Estados de un sorteo (columna `state`):  open → drawing → announcing → (borrado)
  * open:       acepta anotados hasta `end_at`.
  * drawing:    alguien reclamó el sorteo (atómico, una sola vez). Todavía no se eligió nada.
  * announcing: el resultado YA está elegido y guardado (en la misma transacción); solo falta mostrarlo.
                Si el anuncio falla se reintenta desde lo guardado: NUNCA se vuelve a sortear.
Usa las mismas tablas que antes (giveaways, giveaway_entrants, giveaway_history), así los sorteos en curso siguen valiendo.
"""
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY,
    prize TEXT NOT NULL,
    end_at TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    winners_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS giveaway_entrants (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id),
    FOREIGN KEY (message_id) REFERENCES giveaways(message_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS giveaway_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    prize TEXT NOT NULL,
    winners_requested INTEGER NOT NULL,
    entrants_count INTEGER NOT NULL,
    valid_count INTEGER NOT NULL,
    winners_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
"""

MAX_WINNERS = 25


@dataclass
class Giveaway:
    message_id: int
    prize: str
    end_at: datetime
    channel_id: int
    creator_id: int
    guild_id: int
    winners_count: int
    state: str
    outcome: Optional[str] = None
    valid_count: int = 0
    winners: List[int] = field(default_factory=list)
    entrants: Set[int] = field(default_factory=set)


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "giveaways", "winners_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "giveaways", "state", "TEXT NOT NULL DEFAULT 'open'")
        _ensure_column(conn, "giveaways", "outcome", "TEXT")
        _ensure_column(conn, "giveaways", "valid_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "giveaways", "winners_json", "TEXT")
        with write_tx(conn):               # un reclamo sin resultado guardado no dejó nada hecho: se puede reclamar de nuevo
            conn.execute("UPDATE giveaways SET state = 'open' WHERE state = 'drawing'")
    finally:
        conn.close()


def _dt(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _build(conn, r) -> Giveaway:
    entrants = {int(x["user_id"]) for x in conn.execute("SELECT user_id FROM giveaway_entrants WHERE message_id = ?", (r["message_id"],))}
    return Giveaway(int(r["message_id"]), r["prize"], _dt(r["end_at"]).astimezone(timezone.utc), int(r["channel_id"]), int(r["creator_id"]),
                    int(r["guild_id"]), max(1, int(r["winners_count"] or 1)), r["state"] or "open", r["outcome"], int(r["valid_count"] or 0),
                    [int(w) for w in json.loads(r["winners_json"] or "[]")], entrants)


def create(db_path: str, *, message_id: int, prize: str, end_at: datetime, channel_id: int, creator_id: int, guild_id: int,
           winners_count: int = 1) -> Giveaway:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO giveaways(message_id, prize, end_at, channel_id, creator_id, guild_id, winners_count, state) "
                         "VALUES (?,?,?,?,?,?,?, 'open')",
                         (message_id, prize, end_at.astimezone(timezone.utc).isoformat(), channel_id, creator_id, guild_id,
                          max(1, min(MAX_WINNERS, int(winners_count)))))
            return _build(conn, conn.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def get(db_path: str, message_id: int) -> Optional[Giveaway]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()
        return _build(conn, r) if r else None
    finally:
        conn.close()


def count_entrants(db_path: str, message_id: int) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM giveaway_entrants WHERE message_id = ?", (message_id,)).fetchone()[0])
    finally:
        conn.close()


def add_entrant(db_path: str, message_id: int, user_id: int, now: Optional[datetime] = None) -> str:
    """'ok' | 'already' | 'closed' (terminó o ya se está sorteando) | 'notfound'. Atómico respecto del sorteo."""
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT state, end_at FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()
            if r is None:
                return "notfound"
            if (r["state"] or "open") != "open" or now >= _dt(r["end_at"]):
                return "closed"
            cur = conn.execute("INSERT OR IGNORE INTO giveaway_entrants(message_id, user_id) VALUES (?, ?)", (message_id, int(user_id)))
            return "ok" if cur.rowcount == 1 else "already"
    finally:
        conn.close()


def pending(db_path: str, now: Optional[datetime] = None) -> List[int]:
    """Sorteos que hay que atender ya: vencidos todavía abiertos, y los que quedaron a medio anunciar."""
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        out = []
        for r in conn.execute("SELECT message_id, state, end_at FROM giveaways ORDER BY end_at"):
            st = r["state"] or "open"
            if st == "announcing" or (st == "open" and _dt(r["end_at"]) <= now):
                out.append(int(r["message_id"]))
        return out
    finally:
        conn.close()


def claim_draw(db_path: str, message_id: int, now: Optional[datetime] = None) -> bool:
    """Reclama el sorteo (open → drawing) una sola vez, y solo si ya venció."""
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT state, end_at FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()
            if r is None or (r["state"] or "open") != "open" or _dt(r["end_at"]) > now:
                return False
            return conn.execute("UPDATE giveaways SET state = 'drawing' WHERE message_id = ? AND state = 'open'", (message_id,)).rowcount == 1
    finally:
        conn.close()


def release_claim(db_path: str, message_id: int) -> None:
    """No se pudo ni empezar (p. ej. el servidor no está disponible): vuelve a abierto para reintentar."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE giveaways SET state = 'open' WHERE message_id = ? AND state = 'drawing'", (message_id,))
    finally:
        conn.close()


def pick_winners(eligible, count: int, rng=None) -> List[int]:
    """`count` ganadores distintos, solo entre `eligible`. [] si no hay suficientes."""
    unique = list(dict.fromkeys(int(u) for u in eligible))
    if count < 1 or len(unique) < count:
        return []
    return (rng or random.SystemRandom()).sample(unique, count)


def record_result(db_path: str, message_id: int, valid: List[int], rng=None, allow_partial: bool = False) -> Optional[Giveaway]:
    """Elige y GUARDA el resultado en una transacción (drawing → announcing). None si no estaba en 'drawing'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()
            if r is None or r["state"] != "drawing":
                return None
            g = _build(conn, r)
            wanted = g.winners_count
            valid = list(dict.fromkeys(int(u) for u in valid))
            if not valid:
                outcome, winners = "no_entrants", []
            elif len(valid) < wanted and not allow_partial:
                outcome, winners = "insufficient", []
            else:
                count = min(wanted, len(valid))
                winners = pick_winners(valid, count, rng)
                outcome = "ok" if count == wanted else "partial"
            conn.execute("INSERT INTO giveaway_history(message_id, guild_id, channel_id, prize, winners_requested, entrants_count, valid_count, "
                         "winners_json, outcome, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (g.message_id, g.guild_id, g.channel_id, g.prize, wanted, len(g.entrants), len(valid),
                          json.dumps([str(w) for w in winners]), outcome, utcnow_iso()))
            conn.execute("UPDATE giveaways SET state = 'announcing', outcome = ?, valid_count = ?, winners_json = ? WHERE message_id = ?",
                         (outcome, len(valid), json.dumps([str(w) for w in winners]), message_id))
            return _build(conn, conn.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def finish(db_path: str, message_id: int) -> None:
    """Anunciado: se borra el sorteo en curso (el resultado queda en giveaway_history)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM giveaway_entrants WHERE message_id = ?", (message_id,))
            conn.execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))
    finally:
        conn.close()
