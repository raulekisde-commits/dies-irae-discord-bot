"""Balance de embo (embotelladas de Albion): solo contabilidad, separada de los Wuju Coins.

Una cuenta por persona y un historial de movimientos. Cada movimiento lleva `request_key` única, así un reintento o un
doble evento no aplica dos veces la misma operación. El balance puede quedar negativo (quitar o fijar desde /eco embo,
cartas malas del Wujupón), igual que los Wuju Coins.
"""
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

NAME = "Embo"
EMOJI = "🍾"
MAX_EMBO = 10 ** 9

SCHEMA = """
CREATE TABLE IF NOT EXISTS embo_accounts (
    user_id    INTEGER PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embo_movements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key    TEXT NOT NULL UNIQUE,
    user_id        INTEGER NOT NULL,
    delta          INTEGER NOT NULL,
    balance_before INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    kind           TEXT NOT NULL,          -- add | remove | set | wujupon_prize
    reason         TEXT,
    actor_id       INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embo_movements_user ON embo_movements(user_id);
"""


class EmboError(ValueError):
    """Operación inválida (texto apto para mostrar)."""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _balance(conn: sqlite3.Connection, user_id: int) -> int:
    r = conn.execute("SELECT balance FROM embo_accounts WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["balance"]) if r else 0


def get_balance(db_path: str, user_id: int) -> int:
    conn = connect(db_path)
    try:
        try:
            return _balance(conn, user_id)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return 0
            raise
    finally:
        conn.close()


@dataclass
class Change:
    before: int
    after: int
    duplicate: bool = False


def tx_apply(conn: sqlite3.Connection, *, user_id: int, mode: str, amount: int, actor_id: int,
             request_key: str, reason: str, kind: Optional[str] = None) -> Change:
    """Aplica add/remove/set dentro de una transacción ya abierta (la usa también Wujupón)."""
    if mode not in ("add", "remove", "set"):
        raise ValueError("modo inválido")
    amount = int(amount)
    if mode != "set" and (amount <= 0 or amount > MAX_EMBO):
        raise EmboError("La cantidad tiene que ser un número positivo.")
    if mode == "set" and abs(amount) > MAX_EMBO:
        raise EmboError("La cantidad excede el límite.")
    prev = conn.execute("SELECT balance_before, balance_after FROM embo_movements WHERE request_key = ?", (request_key,)).fetchone()
    if prev is not None:
        return Change(int(prev["balance_before"]), int(prev["balance_after"]), duplicate=True)
    before = _balance(conn, user_id)
    after = before + amount if mode == "add" else before - amount if mode == "remove" else amount
    if abs(after) > MAX_EMBO:
        raise EmboError("El balance resultante excede el límite.")
    now = utcnow_iso()
    conn.execute("INSERT INTO embo_accounts(user_id, balance, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at",
                 (user_id, after, now))
    conn.execute("INSERT INTO embo_movements(request_key, user_id, delta, balance_before, balance_after, kind, reason, actor_id, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (request_key, user_id, after - before, before, after, kind or mode, reason, actor_id, now))
    return Change(before, after)


def apply(db_path: str, *, user_id: int, mode: str, amount: int, actor_id: int, request_key: str, reason: str) -> Change:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return tx_apply(conn, user_id=user_id, mode=mode, amount=amount, actor_id=actor_id,
                            request_key=request_key, reason=reason)
    finally:
        conn.close()


def history(db_path: str, user_id: int, limit: int = 15) -> List[Tuple[str, int, int, str, str]]:
    """(fecha, delta, balance_after, kind, reason) más recientes primero."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT created_at, delta, balance_after, kind, reason FROM embo_movements WHERE user_id = ? "
                            "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [(r["created_at"], int(r["delta"]), int(r["balance_after"]), r["kind"], r["reason"] or "") for r in rows]
    finally:
        conn.close()
