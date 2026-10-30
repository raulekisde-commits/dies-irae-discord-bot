"""Rifas con Wuju Coins (lógica y persistencia, sin Discord).

Reglas de dinero (cada acción es UNA transacción sobre `eco_accounts`):
- Comprar tickets = se descuenta (precio × cantidad) en el mismo momento en que se anotan; sin saldo suficiente no se anota nada.
- Cada pulsación lleva una clave única (el id de la interacción de Discord): si se reenvía, no se cobra dos veces.
- El sorteo se hace una sola vez (la transición `open` → `done` ocurre dentro de la transacción); cada ticket es una chance.
- Cancelar devuelve todo lo cobrado. Lo cobrado en una rifa terminada queda en la guild (no se paga a nadie).
"""
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

MAX_TICKETS_PER_USER = 100
MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 7 * 24 * 3600
MAX_PRIZE_LEN = 100
MAX_PRICE = 10 ** 12
MOV_BUY, MOV_REFUND = "raffle_ticket", "raffle_refund"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raffles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    creator_id  INTEGER NOT NULL,
    prize       TEXT NOT NULL,
    price       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',      -- open | done | cancelled
    created_at  TEXT NOT NULL,
    ends_at     REAL NOT NULL,
    resolved_at TEXT,
    winner_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raffles_message ON raffles(message_id);
CREATE TABLE IF NOT EXISTS raffle_tickets (
    raffle_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    qty       INTEGER NOT NULL,
    PRIMARY KEY (raffle_id, user_id),
    FOREIGN KEY (raffle_id) REFERENCES raffles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS raffle_purchases (
    purchase_key TEXT PRIMARY KEY,
    raffle_id    INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    qty          INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
"""


class RaffleError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


@dataclass
class Raffle:
    id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    creator_id: int
    prize: str
    price: int
    status: str
    created_at: str
    ends_at: float
    winner_id: Optional[int]
    tickets: Dict[int, int] = field(default_factory=dict)      # persona → cantidad

    @property
    def total_tickets(self) -> int:
        return sum(self.tickets.values())

    @property
    def pot(self) -> int:
        return self.total_tickets * self.price


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def parse_duration(text: str) -> int:
    """'hh:mm' → segundos (por ejemplo '02:30' = 2 h 30 min; '0:45' = 45 min)."""
    s = (text or "").strip()
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or len(parts[1]) != 2:
        raise RaffleError("El tiempo se escribe `hh:mm` (por ejemplo `01:30` = 1 hora y media).")
    hours, minutes = int(parts[0]), int(parts[1])
    if minutes > 59:
        raise RaffleError("Los minutos van de 00 a 59.")
    seconds = hours * 3600 + minutes * 60
    if seconds < MIN_DURATION_SECONDS:
        raise RaffleError("La rifa tiene que durar al menos 1 minuto.")
    if seconds > MAX_DURATION_SECONDS:
        raise RaffleError("La rifa puede durar como máximo 7 días (`168:00`).")
    return seconds


def _load(conn, raffle_id: int) -> Optional[Raffle]:
    r = conn.execute("SELECT * FROM raffles WHERE id = ?", (raffle_id,)).fetchone()
    if r is None:
        return None
    tickets = {int(t["user_id"]): int(t["qty"]) for t in conn.execute(
        "SELECT user_id, qty FROM raffle_tickets WHERE raffle_id = ? ORDER BY qty DESC, user_id", (raffle_id,))}
    return Raffle(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), r["message_id"], int(r["creator_id"]), r["prize"], int(r["price"]),
                  r["status"], r["created_at"], float(r["ends_at"]), r["winner_id"], tickets)


def create_raffle(db_path: str, *, guild_id: int, channel_id: int, creator_id: int, prize: str, price: int, duration_seconds: int,
                  now: Optional[float] = None) -> Raffle:
    now = time.time() if now is None else now
    prize = " ".join((prize or "").split())
    if not prize:
        raise RaffleError("Falta el premio.")
    if len(prize) > MAX_PRIZE_LEN:
        raise RaffleError(f"El premio es muy largo (máximo {MAX_PRIZE_LEN} caracteres).")
    if not (0 < price <= MAX_PRICE):
        raise RaffleError("El precio de entrada tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO raffles(guild_id, channel_id, creator_id, prize, price, created_at, ends_at) VALUES (?,?,?,?,?,?,?)",
                               (guild_id, channel_id, creator_id, prize, price, utcnow_iso(), now + duration_seconds))
            return _load(conn, cur.lastrowid)
    finally:
        conn.close()


def get_raffle(db_path: str, raffle_id: int) -> Optional[Raffle]:
    conn = connect(db_path)
    try:
        return _load(conn, raffle_id)
    finally:
        conn.close()


def find_by_message(db_path: str, message_id: int) -> Optional[Raffle]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT id FROM raffles WHERE message_id = ?", (message_id,)).fetchone()
        return _load(conn, int(r["id"])) if r else None
    finally:
        conn.close()


def set_message(db_path: str, raffle_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE raffles SET message_id = ? WHERE id = ?", (message_id, raffle_id))
    finally:
        conn.close()


def due_raffles(db_path: str, now: Optional[float] = None) -> List[int]:
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute("SELECT id FROM raffles WHERE status = 'open' AND ends_at <= ? ORDER BY id", (now,))]
    finally:
        conn.close()


def open_raffles(db_path: str) -> List[Raffle]:
    conn = connect(db_path)
    try:
        return [_load(conn, int(r["id"])) for r in conn.execute("SELECT id FROM raffles WHERE status = 'open' ORDER BY id")]
    finally:
        conn.close()


@dataclass
class BuyResult:
    status: str                    # ok | closed | funds | limit | duplicate | notfound
    raffle: Optional[Raffle] = None
    balance: int = 0
    mine: int = 0                  # tickets totales de la persona en esta rifa
    spent: int = 0


def buy_tickets(db_path: str, raffle_id: int, user_id: int, qty: int, purchase_key: str, now: Optional[float] = None) -> BuyResult:
    now = time.time() if now is None else now
    if qty < 1:
        raise RaffleError("La cantidad de tickets tiene que ser al menos 1.")
    conn = connect(db_path)
    try:
        try:
            with write_tx(conn):
                r = _load(conn, raffle_id)
                if r is None:
                    return BuyResult("notfound")
                if r.status != "open" or now >= r.ends_at:
                    return BuyResult("closed", r)
                try:
                    conn.execute("INSERT INTO raffle_purchases(purchase_key, raffle_id, user_id, qty, created_at) VALUES (?,?,?,?,?)",
                                 (purchase_key, raffle_id, user_id, qty, utcnow_iso()))
                except sqlite3.IntegrityError:
                    return BuyResult("duplicate", r, mine=r.tickets.get(user_id, 0))
                have = r.tickets.get(user_id, 0)
                if have + qty > MAX_TICKETS_PER_USER:
                    raise _Limit(have)
                cost = r.price * qty
                if cost > eco.MAX_AMOUNT:
                    raise _Limit(have)
                related = f"raffle:{raffle_id}"
                reason = f"Rifa #{raffle_id}: {qty} ticket(s)"
                op = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"raffle:{raffle_id}:buy:{purchase_key}:{uuid.uuid4().hex}",
                                          total_amount=cost, target_count=1, reason=reason, related_id=related)
                _, after = eco.tx_move(conn, op_id=op, user_id=user_id, delta=-cost, mov_type=MOV_BUY, reason=reason, actor_id=user_id,
                                       related_id=related, require_funds=True)
                conn.execute("INSERT INTO raffle_tickets(raffle_id, user_id, qty) VALUES (?,?,?) "
                             "ON CONFLICT(raffle_id, user_id) DO UPDATE SET qty = qty + excluded.qty", (raffle_id, user_id, qty))
                return BuyResult("ok", _load(conn, raffle_id), after, have + qty, cost)
        except eco.InsufficientFunds as exc:
            return BuyResult("funds", get_raffle(db_path, raffle_id), balance=int(exc.args[0]))
        except _Limit as exc:
            return BuyResult("limit", get_raffle(db_path, raffle_id), mine=exc.have)
    finally:
        conn.close()


class _Limit(Exception):
    def __init__(self, have: int):
        super().__init__(have)
        self.have = have


def draw(db_path: str, raffle_id: int, now: Optional[float] = None, rng: Optional[random.Random] = None) -> Optional[Raffle]:
    """Sortea una rifa vencida (cada ticket es una chance). Devuelve la rifa ya cerrada, o None si no estaba abierta."""
    now = time.time() if now is None else now
    rng = rng or random.SystemRandom()
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = _load(conn, raffle_id)
            if r is None or r.status != "open" or now < r.ends_at:
                return None
            winner = None
            total = r.total_tickets
            if total:
                pick = rng.randrange(total)
                for uid in sorted(r.tickets):
                    pick -= r.tickets[uid]
                    if pick < 0:
                        winner = uid
                        break
            conn.execute("UPDATE raffles SET status = 'done', winner_id = ?, resolved_at = ? WHERE id = ?", (winner, utcnow_iso(), raffle_id))
            return _load(conn, raffle_id)
    finally:
        conn.close()


def cancel(db_path: str, raffle_id: int) -> Optional[Raffle]:
    """Cancela una rifa abierta y devuelve a cada persona todo lo que pagó. None si no estaba abierta."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = _load(conn, raffle_id)
            if r is None or r.status != "open":
                return None
            related = f"raffle:{raffle_id}"
            for uid, qty in r.tickets.items():
                amount = qty * r.price
                reason = f"Rifa #{raffle_id} cancelada"
                op = eco.tx_new_operation(conn, kind="add", actor_id=0, request_key=f"raffle:{raffle_id}:refund:{uid}:{uuid.uuid4().hex}",
                                          total_amount=amount, target_count=1, reason=reason, related_id=related)
                eco.tx_move(conn, op_id=op, user_id=uid, delta=amount, mov_type=MOV_REFUND, reason=reason, actor_id=0, related_id=related)
            conn.execute("UPDATE raffles SET status = 'cancelled', resolved_at = ? WHERE id = ?", (utcnow_iso(), raffle_id))
            return _load(conn, raffle_id)
    finally:
        conn.close()
