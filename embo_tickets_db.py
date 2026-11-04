"""/panel embo — tickets para comprar embo o saldar la deuda de embo pagando con Wuju Coins (datos, sin Discord).

El Council confirma cantidad y precio por unidad: en UNA transacción se restan los Wuju Coins (sin dejar el balance en negativo)
y se suman las embo. Idempotente por ticket: un doble clic o un reintento no cobra dos veces.
"""
from dataclasses import dataclass
from typing import Optional

import economy_db as eco
import embo_db
from dbutil import connect, utcnow_iso, write_tx

KINDS = ("buy", "debt")           # buy = quiere comprar embo · debt = quiere saldar su deuda (balance de embo negativo)
MOV_TYPE = "embo_purchase"

SCHEMA = """
CREATE TABLE IF NOT EXISTS embo_tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,                 -- buy | debt
    amount       INTEGER NOT NULL,              -- buy: embo pedidas · debt: deuda al abrir el ticket
    status       TEXT NOT NULL DEFAULT 'open',  -- open | closed
    processed    INTEGER NOT NULL DEFAULT 0,    -- 1 = ya se cobraron los Wuju Coins y se sumaron las embo
    qty          INTEGER,
    unit_price   INTEGER,
    total        INTEGER,
    processed_by INTEGER,
    message_id   INTEGER,
    created_at   TEXT NOT NULL,
    closed_at    TEXT,
    closed_by    INTEGER,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_embo_tickets_user ON embo_tickets(guild_id, user_id, status);
"""


class EmboTicketError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


@dataclass
class Ticket:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    kind: str
    amount: int
    status: str
    processed: bool
    message_id: Optional[int]


def _row(r) -> Optional[Ticket]:
    if r is None:
        return None
    return Ticket(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), int(r["user_id"]), r["kind"], int(r["amount"]),
                  r["status"], bool(r["processed"]), int(r["message_id"]) if r["message_id"] is not None else None)


def init_schema(db_path: str) -> None:
    embo_db.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def parse_amount(text: str) -> int:
    """Entero positivo (acepta 1.500 o 1,500)."""
    try:
        return eco.parse_amount(text)
    except eco.AmountError as exc:
        raise EmboTicketError(str(exc))


def create_ticket(db_path: str, guild_id: int, channel_id: int, user_id: int, kind: str, amount: int) -> int:
    if kind not in KINDS:
        raise EmboTicketError("Tipo de ticket inválido.")
    if amount <= 0:
        raise EmboTicketError("La cantidad tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO embo_tickets(guild_id, channel_id, user_id, kind, amount, status, created_at) "
                               "VALUES (?,?,?,?,?, 'open', ?)", (guild_id, channel_id, user_id, kind, amount, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def _one(db_path: str, sql: str, args) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute(sql, args).fetchone())
    finally:
        conn.close()


def get_ticket(db_path: str, ticket_id: int) -> Optional[Ticket]:
    return _one(db_path, "SELECT * FROM embo_tickets WHERE id = ?", (ticket_id,))


def get_by_channel(db_path: str, channel_id: int) -> Optional[Ticket]:
    return _one(db_path, "SELECT * FROM embo_tickets WHERE channel_id = ?", (channel_id,))


def open_ticket_of(db_path: str, guild_id: int, user_id: int) -> Optional[Ticket]:
    return _one(db_path, "SELECT * FROM embo_tickets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                (guild_id, user_id))


def set_message(db_path: str, ticket_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE embo_tickets SET message_id = ? WHERE id = ?", (message_id, ticket_id))
    finally:
        conn.close()


def discard_ticket(db_path: str, ticket_id: int) -> None:
    """Borra un ticket que no se llegó a abrir bien (falló el mensaje del canal)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM embo_tickets WHERE id = ? AND processed = 0", (ticket_id,))
    finally:
        conn.close()


def close_ticket(db_path: str, ticket_id: int, actor_id: int, reason: str) -> bool:
    """Cierra el ticket. True si lo cerró ahora; False si ya estaba cerrado."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE embo_tickets SET status = 'closed', closed_at = ?, closed_by = ?, close_reason = ? "
                                "WHERE id = ? AND status = 'open'", (utcnow_iso(), actor_id, reason, ticket_id)).rowcount > 0
    finally:
        conn.close()


@dataclass
class SettleResult:
    status: str                    # ok | funds | closed | done | invalid
    total: int = 0
    wuju_before: int = 0
    wuju_after: int = 0
    embo_before: int = 0
    embo_after: int = 0
    message: str = ""


class _Abort(Exception):
    def __init__(self, result: SettleResult):
        super().__init__()
        self.result = result


def settle(db_path: str, ticket_id: int, actor_id: int, qty: int, unit_price: int) -> SettleResult:
    """Cobra qty × unit_price Wuju Coins y suma qty embo, todo junto. Si no alcanza el balance no se mueve nada ('funds').
    Al terminar bien el ticket queda procesado y cerrado."""
    if qty <= 0 or unit_price <= 0:
        return SettleResult("invalid", message="La cantidad y el precio tienen que ser mayores a 0.")
    total = qty * unit_price
    if qty > embo_db.MAX_EMBO or total > eco.MAX_BALANCE_ABS:
        return SettleResult("invalid", total=total, message="La cantidad o el total exceden el límite.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = _row(conn.execute("SELECT * FROM embo_tickets WHERE id = ?", (ticket_id,)).fetchone())
            if t is None or t.status != "open":
                raise _Abort(SettleResult("closed"))
            if t.processed:
                raise _Abort(SettleResult("done"))
            key = f"emboticket:{t.id}"
            what = "Saldar deuda de embo" if t.kind == "debt" else "Compra de embo"
            reason = f"{what}: {qty} × {unit_price} (ticket #{t.id})"
            op_id = eco.tx_new_operation(conn, kind="remove", actor_id=actor_id, request_key=key, total_amount=total,
                                         target_count=1, reason=reason, related_id=key)
            try:
                wb, wa = eco.tx_move(conn, op_id=op_id, user_id=t.user_id, delta=-total, mov_type=MOV_TYPE, reason=reason,
                                     actor_id=actor_id, related_id=key, require_funds=True)
            except eco.InsufficientFunds as exc:
                raise _Abort(SettleResult("funds", total=total, wuju_before=int(exc.args[0]) if exc.args else 0))
            ch = embo_db.tx_apply(conn, user_id=t.user_id, mode="add", amount=qty, actor_id=actor_id, request_key=key,
                                  reason=reason, kind=MOV_TYPE)
            now = utcnow_iso()
            conn.execute("UPDATE embo_tickets SET processed = 1, qty = ?, unit_price = ?, total = ?, processed_by = ?, "
                         "status = 'closed', closed_at = ?, closed_by = ?, close_reason = 'accepted' WHERE id = ?",
                         (qty, unit_price, total, actor_id, now, actor_id, t.id))
            return SettleResult("ok", total, wb, wa, ch.before, ch.after)
    except _Abort as a:
        return a.result
    finally:
        conn.close()
