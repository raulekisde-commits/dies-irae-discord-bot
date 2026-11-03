"""Cambio de moneda: tickets para comprar Wuju Coins (con silver) y para venderlos (por silver).

Los tickets solo registran quién, de qué tipo y (si es compra) cuántos Wuju Coins pidió. El movimiento de Wuju Coins lo hace
el Council con un botón y se aplica con `economy_db.apply_operation`, que es idempotente por `request_key`.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

KINDS = ("buy", "sell")           # buy = el usuario compra Wuju Coins · sell = el usuario vende sus Wuju Coins por silver

SCHEMA = """
CREATE TABLE IF NOT EXISTS cm_tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,                     -- buy | sell
    amount     INTEGER NOT NULL DEFAULT 0,        -- buy: Wuju Coins pedidos · sell: no se usa
    status     TEXT NOT NULL DEFAULT 'open',      -- open | closed
    processed  INTEGER NOT NULL DEFAULT 0,        -- 1 = ya se agregaron/retiraron los Wuju Coins
    processed_amount INTEGER,
    processed_by INTEGER,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    closed_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cm_tickets_user ON cm_tickets(guild_id, user_id, kind, status);

CREATE TABLE IF NOT EXISTS cm_panels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);

-- Carta de Wujupón «Cancelar cooldown de cobro»: anula el cooldown del último ticket de cobro abierto antes de reset_at
CREATE TABLE IF NOT EXISTS cm_cooldown_resets (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    reset_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


class DepositError(ValueError):
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
    processed_amount: Optional[int]


def _row(r) -> Optional[Ticket]:
    if r is None:
        return None
    return Ticket(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), int(r["user_id"]), r["kind"], int(r["amount"]), r["status"],
                  bool(r["processed"]), int(r["processed_amount"]) if r["processed_amount"] is not None else None)


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cm_tickets)")}
        if "message_id" not in cols:                                  # migración aditiva: mensaje del ticket y balance que muestra
            conn.execute("ALTER TABLE cm_tickets ADD COLUMN message_id INTEGER")
        if "shown_balance" not in cols:
            conn.execute("ALTER TABLE cm_tickets ADD COLUMN shown_balance INTEGER")
    finally:
        conn.close()


def parse_amount(text: str) -> int:
    """Cantidad entera positiva (acepta 1.500.000 o 1,500,000)."""
    try:
        return eco.parse_amount(text)
    except eco.AmountError as exc:
        raise DepositError(str(exc))


def create_ticket(db_path: str, guild_id: int, channel_id: int, user_id: int, kind: str, amount: int = 0) -> int:
    if kind not in KINDS:
        raise DepositError("Tipo de ticket inválido.")
    if kind == "buy" and amount <= 0:
        raise DepositError("La cantidad tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO cm_tickets(guild_id, channel_id, user_id, kind, amount, status, created_at) VALUES (?,?,?,?,?, 'open', ?)",
                               (guild_id, channel_id, user_id, kind, amount, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def get_ticket(db_path: str, ticket_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM cm_tickets WHERE id = ?", (ticket_id,)).fetchone())
    finally:
        conn.close()


def get_by_channel(db_path: str, channel_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM cm_tickets WHERE channel_id = ?", (channel_id,)).fetchone())
    finally:
        conn.close()


SELL_COOLDOWN_SECONDS = 8 * 3600       # quien abre un ticket de «Cobrar balance» no puede abrir otro hasta pasadas 8 horas


def sell_cooldown_remaining(db_path: str, guild_id: int, user_id: int, now=None) -> int:
    """Segundos que faltan para que pueda volver a abrir un ticket de cobro (0 = puede). Cuenta desde que abrió el último,
    aunque ya esté cerrado o cancelado (salvo que se haya cancelado con una carta de Wujupón)."""
    conn = connect(db_path)
    try:
        return _sell_cooldown_conn(conn, guild_id, user_id, now)
    finally:
        conn.close()


def _sell_cooldown_conn(conn, guild_id: int, user_id: int, now=None) -> int:
    """Lo que falte entre el cooldown del último ticket de cobro y el cooldown extra (cartas ☠️ o el Council)."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    return max(_ticket_cooldown(conn, guild_id, user_id, now), _extra_cooldown(conn, guild_id, user_id, now))


def _parse(ts):
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ticket_cooldown(conn, guild_id: int, user_id: int, now) -> int:
    try:
        r = conn.execute("SELECT created_at FROM cm_tickets WHERE guild_id = ? AND user_id = ? AND kind = 'sell' ORDER BY id DESC LIMIT 1",
                         (guild_id, user_id)).fetchone()
    except Exception as exc:     # tabla todavía no creada
        if "no such table" in str(exc):
            return 0
        raise
    if r is None:
        return 0
    try:
        reset = conn.execute("SELECT reset_at FROM cm_cooldown_resets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
    except Exception:            # tabla todavía no creada
        reset = None
    if reset is not None and str(reset["reset_at"]) >= str(r["created_at"]):
        return 0                 # cooldown cancelado con una carta de Wujupón o por el Council
    opened = _parse(r["created_at"])
    if opened is None:
        return 0
    return max(0, int(SELL_COOLDOWN_SECONDS - (now - opened).total_seconds()))


def _extra_cooldown(conn, guild_id: int, user_id: int, now) -> int:
    try:
        r = conn.execute("SELECT until_at FROM cm_cooldown_extra WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
    except Exception:            # tabla todavía no creada
        return 0
    until = _parse(r["until_at"]) if r else None
    return max(0, int((until - now).total_seconds())) if until else 0


_EXTRA_SQL = ("CREATE TABLE IF NOT EXISTS cm_cooldown_extra (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
              "until_at TEXT NOT NULL, PRIMARY KEY (guild_id, user_id))")
_RESETS_SQL = ("CREATE TABLE IF NOT EXISTS cm_cooldown_resets (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
               "reset_at TEXT NOT NULL, PRIMARY KEY (guild_id, user_id))")
MAX_EXTRA_HOURS = 24 * 30


def tx_reset_sell_cooldown(conn, guild_id: int, user_id: int) -> bool:
    """Dentro de una transacción: cancela el cooldown de «Cobrar balance» (el del ticket y el extra).
    False si no había cooldown activo."""
    conn.execute(_RESETS_SQL)
    conn.execute(_EXTRA_SQL)
    if _sell_cooldown_conn(conn, guild_id, user_id) <= 0:
        return False
    conn.execute("INSERT OR REPLACE INTO cm_cooldown_resets(guild_id, user_id, reset_at) VALUES (?,?,?)",
                 (guild_id, user_id, utcnow_iso()))
    conn.execute("DELETE FROM cm_cooldown_extra WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return True


def add_sell_cooldown(db_path: str, guild_id: int, user_id: int, hours: float, now=None) -> int:
    """Suma `hours` al cooldown de «Cobrar balance» (sobre lo que ya le falte; si no tenía, empieza uno).
    Devuelve los segundos que le quedan."""
    from datetime import datetime, timedelta, timezone
    if not 0 < hours <= MAX_EXTRA_HOURS:
        raise DepositError(f"Las horas tienen que estar entre 1 y {MAX_EXTRA_HOURS}.")
    now = now or datetime.now(timezone.utc)
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute(_EXTRA_SQL)
            left = _sell_cooldown_conn(conn, guild_id, user_id, now)
            until = now + timedelta(seconds=left) + timedelta(hours=hours)
            conn.execute("INSERT OR REPLACE INTO cm_cooldown_extra(guild_id, user_id, until_at) VALUES (?,?,?)",
                         (guild_id, user_id, until.isoformat()))
            return _sell_cooldown_conn(conn, guild_id, user_id, now)
    finally:
        conn.close()


def clear_sell_cooldown(db_path: str, guild_id: int, user_id: int) -> bool:
    """Saca todo el cooldown de «Cobrar balance». False si no tenía."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return tx_reset_sell_cooldown(conn, guild_id, user_id)
    finally:
        conn.close()


def open_ticket_of(db_path: str, guild_id: int, user_id: int, kind: str) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _row(conn.execute(
            "SELECT * FROM cm_tickets WHERE guild_id = ? AND user_id = ? AND kind = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (guild_id, user_id, kind)).fetchone())
    finally:
        conn.close()


def mark_processed(db_path: str, ticket_id: int, amount: int, actor_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE cm_tickets SET processed = 1, processed_amount = ?, processed_by = ? WHERE id = ?", (amount, actor_id, ticket_id))
    finally:
        conn.close()


def close_ticket(db_path: str, ticket_id: int, actor_id: int) -> bool:
    """False si ya estaba cerrado (dos Council a la vez: solo uno lo cierra)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE cm_tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ? AND status = 'open'",
                               (utcnow_iso(), actor_id, ticket_id))
            return cur.rowcount == 1
    finally:
        conn.close()


def discard_ticket(db_path: str, ticket_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM cm_tickets WHERE id = ?", (ticket_id,))
    finally:
        conn.close()


def get_panel(db_path: str, guild_id: int, kind: str) -> Optional[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id, message_id FROM cm_panels WHERE guild_id = ? AND kind = ?", (guild_id, kind)).fetchone()
        return (int(r["channel_id"]), int(r["message_id"])) if r else None
    finally:
        conn.close()


def set_panel(db_path: str, guild_id: int, kind: str, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO cm_panels(guild_id, kind, channel_id, message_id) VALUES (?,?,?,?)",
                         (guild_id, kind, channel_id, message_id))
    finally:
        conn.close()


# ---------- mantener al día el balance que muestran los tickets de cobro ----------
def set_ticket_message(db_path: str, ticket_id: int, message_id: int, balance: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE cm_tickets SET message_id = ?, shown_balance = ? WHERE id = ?", (message_id, balance, ticket_id))
    finally:
        conn.close()


def set_shown_balance(db_path: str, ticket_id: int, balance: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE cm_tickets SET shown_balance = ? WHERE id = ?", (balance, ticket_id))
    finally:
        conn.close()


def live_sell_tickets(db_path: str) -> List[Tuple[Ticket, int, Optional[int]]]:
    """Tickets de cobro abiertos y sin procesar: (ticket, id del mensaje, balance que muestran)."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM cm_tickets WHERE kind = 'sell' AND status = 'open' AND processed = 0 AND message_id IS NOT NULL").fetchall()
        return [(_row(r), int(r["message_id"]), None if r["shown_balance"] is None else int(r["shown_balance"])) for r in rows]
    finally:
        conn.close()
