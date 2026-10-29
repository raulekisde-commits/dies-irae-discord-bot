"""Guild Shop — lógica y persistencia (sin Discord).

Tickets de compra, ventas confirmadas y paneles. El cobro en Wuju Coins se hace en UNA transacción junto con
el registro de la venta (si el saldo no alcanza o algo falla, no queda nada a medias) y reutiliza la economía
(`eco_accounts` / `eco_movements`), que sigue siendo la única fuente de verdad de los balances.
"""
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

DEFAULT_DISCOUNT_BP = 1000          # 10,00 % expresado en puntos básicos (1 % = 100)
MAX_ITEMS_LEN = 1500
LOG_CHANNEL_KEY = "guildshop_log_channel_id"
MOV_PURCHASE = "guildshop_purchase"
CURRENCIES = ("wuju", "silver")

SCHEMA = """
CREATE TABLE IF NOT EXISTS gshop_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL UNIQUE,
    buyer_id    INTEGER NOT NULL,
    items       TEXT NOT NULL,
    payment     TEXT,                              -- wuju | silver (método que eligió el comprador)
    status      TEXT NOT NULL DEFAULT 'open',      -- open | closed | sold
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    closed_by   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gshop_tickets_buyer ON gshop_tickets(guild_id, buyer_id, status);

CREATE TABLE IF NOT EXISTS gshop_sales (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id    INTEGER NOT NULL UNIQUE,
    guild_id     INTEGER NOT NULL,
    seller_id    INTEGER NOT NULL,
    buyer_id     INTEGER NOT NULL,
    currency     TEXT NOT NULL,                     -- wuju | silver
    gross        INTEGER NOT NULL,
    discount_bp  INTEGER NOT NULL,
    net          INTEGER NOT NULL,
    confirmed_by INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gshop_sales_seller ON gshop_sales(guild_id, seller_id);

CREATE TABLE IF NOT EXISTS gshop_pending (
    message_id   INTEGER PRIMARY KEY,
    ticket_id    INTEGER NOT NULL,
    seller_id    INTEGER NOT NULL,
    currency     TEXT NOT NULL,
    gross        INTEGER NOT NULL,
    discount_bp  INTEGER NOT NULL,
    net          INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gshop_panels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,                       -- top | shop
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);
"""


class ShopError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


def init_schema(db_path: str) -> None:
    eco.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(gshop_tickets)")}
        if "payment" not in cols:                     # bases creadas antes de esta columna
            conn.execute("ALTER TABLE gshop_tickets ADD COLUMN payment TEXT")
    finally:
        conn.close()


# ============================================================
# Parseo y cálculo
# ============================================================
_PCT_RE = re.compile(r"^\d{1,3}(?:[.,]\d{1,2})?$")


def parse_discount(text: Optional[str]) -> int:
    """'' -> 10 % por defecto. Acepta '10', '10%', '7,5'. Devuelve puntos básicos (0..10000)."""
    s = (text or "").strip().replace(" ", "")
    if s.endswith("%"):
        s = s[:-1]
    if not s:
        return DEFAULT_DISCOUNT_BP
    if not _PCT_RE.match(s):
        raise ShopError("El descuento tiene que ser un porcentaje entre 0 y 100 (ej: `10` o `7,5`).")
    whole, _, frac = s.replace(",", ".").partition(".")
    bp = int(whole) * 100 + int((frac + "00")[:2]) if frac else int(whole) * 100
    if bp > 10000:
        raise ShopError("El descuento no puede ser mayor a 100%.")
    return bp


def compute_net(gross: int, discount_bp: int) -> int:
    """gross menos el descuento, redondeado al entero más cercano (mitades hacia arriba)."""
    return (int(gross) * (10000 - int(discount_bp)) + 5000) // 10000


def build_offer(total_text: str, discount_text: Optional[str]) -> Tuple[int, int, int]:
    """(bruto, descuento en bp, neto a cobrar). Lanza ShopError con un mensaje claro."""
    try:
        gross = eco.parse_amount(total_text)
    except eco.AmountError as exc:
        raise ShopError(str(exc))
    bp = parse_discount(discount_text)
    net = compute_net(gross, bp)
    if net < 1:
        raise ShopError("Con ese descuento el total a cobrar sería 0. Revisá los valores.")
    return gross, bp, net


def format_pct(bp: int) -> str:
    if bp % 100 == 0:
        return f"{bp // 100}%"
    return f"{bp / 100:.2f}".rstrip("0").replace(".", ",") + "%"


# ============================================================
# Tickets
# ============================================================
@dataclass
class Ticket:
    id: int
    guild_id: int
    channel_id: int
    buyer_id: int
    items: str
    status: str
    payment: Optional[str] = None


def _ticket(row) -> Optional[Ticket]:
    if row is None:
        return None
    return Ticket(int(row["id"]), int(row["guild_id"]), int(row["channel_id"]), int(row["buyer_id"]), row["items"], row["status"], row["payment"])


def create_ticket(db_path: str, guild_id: int, channel_id: int, buyer_id: int, items: str, payment: Optional[str] = None) -> int:
    items = (items or "").strip()
    if not items:
        raise ShopError("La lista de compra está vacía.")
    if payment is not None and payment not in CURRENCIES:
        raise ShopError("Método de pago inválido.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "INSERT INTO gshop_tickets(guild_id, channel_id, buyer_id, items, payment, status, created_at) VALUES (?,?,?,?,?, 'open', ?)",
                (guild_id, channel_id, buyer_id, items[:MAX_ITEMS_LEN], payment, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def get_ticket_by_channel(db_path: str, channel_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute("SELECT * FROM gshop_tickets WHERE channel_id = ?", (channel_id,)).fetchone())
    finally:
        conn.close()


def get_ticket(db_path: str, ticket_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute("SELECT * FROM gshop_tickets WHERE id = ?", (ticket_id,)).fetchone())
    finally:
        conn.close()


def open_ticket_of(db_path: str, guild_id: int, buyer_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute(
            "SELECT * FROM gshop_tickets WHERE guild_id = ? AND buyer_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (guild_id, buyer_id)).fetchone())
    finally:
        conn.close()


def close_ticket(db_path: str, ticket_id: int, actor_id: int) -> bool:
    """Cierra sin transacción. False si ya no estaba abierto (otro vendedor llegó primero)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "UPDATE gshop_tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ? AND status = 'open'",
                (utcnow_iso(), actor_id, ticket_id))
            if cur.rowcount != 1:
                return False
            conn.execute("DELETE FROM gshop_pending WHERE ticket_id = ?", (ticket_id,))
            return True
    finally:
        conn.close()


def discard_ticket(db_path: str, ticket_id: int) -> None:
    """Deshace un ticket cuyo canal no se pudo armar (no pasó nada con dinero)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM gshop_tickets WHERE id = ? AND status = 'open'", (ticket_id,))
    finally:
        conn.close()


# ============================================================
# Confirmaciones pendientes y venta
# ============================================================
@dataclass
class Pending:
    message_id: int
    ticket_id: int
    seller_id: int
    currency: str
    gross: int
    discount_bp: int
    net: int


def _pending(row) -> Optional[Pending]:
    if row is None:
        return None
    return Pending(int(row["message_id"]), int(row["ticket_id"]), int(row["seller_id"]), row["currency"],
                   int(row["gross"]), int(row["discount_bp"]), int(row["net"]))


def save_pending(db_path: str, message_id: int, ticket_id: int, seller_id: int, currency: str,
                 gross: int, discount_bp: int, net: int) -> None:
    if currency not in CURRENCIES:
        raise ValueError("moneda inválida")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT OR REPLACE INTO gshop_pending(message_id, ticket_id, seller_id, currency, gross, discount_bp, net, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (message_id, ticket_id, seller_id, currency, gross, discount_bp, net, utcnow_iso()))
    finally:
        conn.close()


def get_pending(db_path: str, message_id: int) -> Optional[Pending]:
    conn = connect(db_path)
    try:
        return _pending(conn.execute("SELECT * FROM gshop_pending WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def cancel_pending(db_path: str, message_id: int) -> Optional[Pending]:
    """Descarta una confirmación. Devuelve lo descartado, o None si ya no existía (doble clic / ya confirmada)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            p = _pending(conn.execute("SELECT * FROM gshop_pending WHERE message_id = ?", (message_id,)).fetchone())
            if p is None:
                return None
            conn.execute("DELETE FROM gshop_pending WHERE message_id = ?", (message_id,))
            return p
    finally:
        conn.close()


@dataclass
class SaleResult:
    status: str                       # ok | gone | closed | funds | self
    pending: Optional[Pending] = None
    ticket: Optional[Ticket] = None
    sale_id: int = 0
    balance_after: int = 0
    balance_before: int = 0


def confirm_sale(db_path: str, message_id: int, confirmer_id: int) -> SaleResult:
    """Confirma una venta pendiente en UNA transacción:
    - consume la confirmación (un segundo clic o dos vendedores a la vez no pueden duplicarla),
    - exige que el ticket siga abierto,
    - si es en Wuju Coins, descuenta el neto al comprador (sin saldo suficiente no se toca nada),
    - registra la venta a nombre de quien la inició y marca el ticket como vendido.
    """
    conn = connect(db_path)
    p: Optional[Pending] = None
    try:
        try:
            with write_tx(conn):
                p = _pending(conn.execute("SELECT * FROM gshop_pending WHERE message_id = ?", (message_id,)).fetchone())
                if p is None:
                    return SaleResult("gone")
                conn.execute("DELETE FROM gshop_pending WHERE message_id = ?", (message_id,))
                t = _ticket(conn.execute("SELECT * FROM gshop_tickets WHERE id = ?", (p.ticket_id,)).fetchone())
                if t is None or t.status != "open":
                    return SaleResult("closed", pending=p, ticket=t)
                if t.buyer_id == p.seller_id:
                    return SaleResult("self", pending=p, ticket=t)
                before = after = 0
                if p.currency == "wuju":
                    related = f"guildshop:{t.id}"
                    reason = f"Compra en Guild Shop (ticket #{t.id})"
                    op_id = eco.tx_new_operation(conn, kind="remove", actor_id=p.seller_id,
                                                 request_key=f"guildshop:{t.id}:{uuid.uuid4().hex}",
                                                 total_amount=p.net, target_count=1, reason=reason, related_id=related)
                    before, after = eco.tx_move(conn, op_id=op_id, user_id=t.buyer_id, delta=-p.net, mov_type=MOV_PURCHASE,
                                                reason=reason, actor_id=p.seller_id, related_id=related, require_funds=True)
                cur = conn.execute(
                    "INSERT INTO gshop_sales(ticket_id, guild_id, seller_id, buyer_id, currency, gross, discount_bp, net, confirmed_by, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t.id, t.guild_id, p.seller_id, t.buyer_id, p.currency, p.gross, p.discount_bp, p.net, confirmer_id, utcnow_iso()))
                conn.execute("UPDATE gshop_tickets SET status = 'sold', closed_at = ?, closed_by = ? WHERE id = ?",
                             (utcnow_iso(), confirmer_id, t.id))
                conn.execute("DELETE FROM gshop_pending WHERE ticket_id = ?", (t.id,))
                return SaleResult("ok", pending=p, ticket=t, sale_id=int(cur.lastrowid), balance_before=before, balance_after=after)
        except eco.InsufficientFunds as exc:
            # el ROLLBACK ya devolvió la confirmación a la tabla: la descartamos aparte para que no quede colgada
            cancel_pending(db_path, message_id)
            return SaleResult("funds", pending=p, balance_before=int(exc.args[0]))
    finally:
        conn.close()


# ============================================================
# Top vendedores
# ============================================================
@dataclass
class SellerRow:
    seller_id: int
    total: int
    wuju: int
    silver: int
    sales: int


def top_sellers(db_path: str, guild_id: int, limit: int = 25) -> List[SellerRow]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT seller_id,
                      SUM(net) AS total,
                      SUM(CASE WHEN currency = 'wuju' THEN net ELSE 0 END) AS wuju,
                      SUM(CASE WHEN currency = 'silver' THEN net ELSE 0 END) AS silver,
                      COUNT(*) AS n
                 FROM gshop_sales WHERE guild_id = ?
                GROUP BY seller_id
                ORDER BY total DESC, MIN(id) ASC
                LIMIT ?""", (guild_id, limit)).fetchall()
        return [SellerRow(int(r["seller_id"]), int(r["total"]), int(r["wuju"]), int(r["silver"]), int(r["n"])) for r in rows]
    finally:
        conn.close()


# ============================================================
# Paneles y canal de registro
# ============================================================
def get_panel(db_path: str, guild_id: int, kind: str) -> Optional[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id, message_id FROM gshop_panels WHERE guild_id = ? AND kind = ?", (guild_id, kind)).fetchone()
        return (int(r["channel_id"]), int(r["message_id"])) if r else None
    finally:
        conn.close()


def set_panel(db_path: str, guild_id: int, kind: str, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO gshop_panels(guild_id, kind, channel_id, message_id) VALUES (?,?,?,?)",
                         (guild_id, kind, channel_id, message_id))
    finally:
        conn.close()


def get_log_channel_id(db_path: str) -> Optional[int]:
    v = eco.get_setting(db_path, LOG_CHANNEL_KEY)
    return int(v) if v and v.isdigit() else None


def set_log_channel_id(db_path: str, channel_id: int) -> None:
    eco.set_setting(db_path, LOG_CHANNEL_KEY, str(int(channel_id)))
