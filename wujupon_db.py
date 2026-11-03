"""Wujupón (gachapon): catálogo, tiradas, cartas y canjes. Sin dependencias de Discord.

Reglas de integridad:
- Cada cambio de estado es una sola transacción (BEGIN IMMEDIATE) condicionada al estado actual: una carta no se puede
  canjear dos veces y dos clics no cobran dos veces.
- Compras, tiradas y canjes llevan `request_key` única (el id de la interacción): un reintento devuelve el mismo resultado.
- Comprar tiradas descuenta Wuju Coins con `economy_db.tx_move` en la MISMA transacción que suma las tiradas.
- Si el bot se reinicia a mitad de algo, la transacción se revierte completa: no quedan medias operaciones.

Probabilidades: pesos enteros sobre 100.000 (1 = 0,001 %). Son probabilidades individuales sobre el total de tiradas.
"""
import random
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import economy_db as eco
import embo_db
from dbutil import connect, utcnow_iso, write_tx

TOTAL_WEIGHT = 100_000
DEFAULT_PRICE = 500_000
MAX_BUY = 50
EMBO_SILVER_VALUE = 10_000          # 1 embo = 10.000 (Wuju = silver 1:1)

RARITIES: Dict[str, Tuple[str, str, int]] = {      # clave → (nombre, emoji, color)
    "common": ("Común", "⚪", 0xB0B3B8),
    "rare": ("Rara", "🔵", 0x3498DB),
    "epic": ("Épica", "🟣", 0x9B59B6),
    "legendary": ("Legendaria", "🟠", 0xE67E22),
    "mythic": ("Mítica", "🔴", 0xE74C3C),
}
RARITY_ORDER = list(RARITIES)

# Handlers automáticos implementados. Las cartas «Bot» sin handler todavía se canjean por encargo al Council.
AUTO_HANDLERS = ("wuju", "embo", "sell_cooldown")

# (clave, nombre, rareza, peso/100.000, origen Bot|Staff, handler, cantidad, costo, moneda del costo, afecta a terceros)
CATALOG = [
    # ⚪ Comunes — 45 %
    ("casino_block", "Bloquear del casino hasta su próximo cierre y reapertura", "common", 8000, "Bot", None, 0, 0, "", 1),
    ("flex_cta", "Rol Flex para la próxima CTA", "common", 11000, "Bot", None, 0, 0, "", 0),
    ("sell_cooldown", "Cancelar cooldown de cobro", "common", 8500, "Bot", "sell_cooldown", 0, 0, "", 0),
    ("gendarme_photo", "Foto de la cola de Gendarme", "common", 9000, "Staff", None, 0, 0, "", 0),
    ("wuju_100k", "100.000 Wuju Coins", "common", 2500, "Bot", "wuju", 100_000, 100_000, "wuju", 0),
    ("embo_50", "50 de balance de embo", "common", 1500, "Bot", "embo", 50, 50, "embo", 0),
    ("party_switch", "Switchear roles en una party (vendiéndole el set al jugador afectado)", "common", 4500, "Staff", None, 0, 0, "", 1),
    # 🔵 Raras — 35 %
    ("mute_15", "Silenciar miembro de Discord durante 15 minutos", "rare", 4500, "Bot", None, 0, 0, "", 1),
    ("voice_mute_15", "Silenciar del voice durante 15 minutos", "rare", 2500, "Bot", None, 0, 0, "", 1),
    ("undo_coinflip", "Deshacer Coinflip", "rare", 4500, "Bot", None, 0, 0, "", 1),
    ("casino_open", "Abrir el casino", "rare", 3500, "Bot", None, 0, 0, "", 0),
    ("casino_close", "Cerrar el casino", "rare", 3500, "Bot", None, 0, 0, "", 0),
    ("plot_tax0", "Tax 0 en plots durante 24 horas", "rare", 2500, "Staff", None, 0, 0, "", 0),
    ("free_build_p1", "Build libre en Party PRIO 1", "rare", 3500, "Staff", None, 0, 0, "", 0),
    ("craft_alter_7d", "Alter de crafteo durante 7 días", "rare", 2000, "Staff", None, 0, 0, "", 0),
    ("party1_slot", "Ocupar un slot de Party 1", "rare", 3500, "Staff", None, 0, 0, "", 0),
    ("wuju_500k", "500.000 Wuju Coins", "rare", 2000, "Bot", "wuju", 500_000, 500_000, "wuju", 0),
    ("embo_100", "100 de balance de embo", "rare", 2000, "Bot", "embo", 100, 100, "embo", 0),
    ("freeze_30m", "Congelar balance de otro jugador durante 30 minutos", "rare", 1000, "Bot", None, 0, 0, "", 1),
    # 🟣 Épicas — 15,2 %
    ("regear_4m", "Reequipo de próxima CTA (presupuesto 4M)", "epic", 600, "Staff", None, 0, 4_000_000, "silver", 0),
    ("mount_escort", "Porte con montura libre y escolta", "epic", 1700, "Staff", None, 0, 0, "", 0),
    ("loot_3_items", "Tres ítems de loot (valor estimado 7M)", "epic", 500, "Staff", None, 0, 7_000_000, "silver", 0),
    ("repair_4m", "Ticket reparación hasta 4M", "epic", 700, "Staff", None, 0, 4_000_000, "silver", 0),
    ("ava_replace", "Reemplazar a alguien en una Avalonian antes de salir", "epic", 1700, "Staff", None, 0, 0, "", 1),
    ("caerleon_port", "Porte a Caerleon", "epic", 1100, "Staff", None, 0, 0, "", 0),
    ("duel_1v1", "Duelo 1v1 especial", "epic", 2300, "Bot", None, 0, 0, "", 1),
    ("ho_house", "Reclamar una casa del HO", "epic", 700, "Staff", None, 0, 0, "", 0),
    ("lootsplit_remove", "Sacar a un jugador del lootsplit de la última CTA", "epic", 1300, "Staff", None, 0, 0, "", 1),
    ("weapon_73", "Arma 7.3 de Guild Shop (valor 8M)", "epic", 600, "Staff", None, 0, 8_000_000, "silver", 0),
    ("wuju_2m", "2.000.000 Wuju Coins", "epic", 300, "Bot", "wuju", 2_000_000, 2_000_000, "wuju", 0),
    ("embo_500", "500 de balance de embo", "epic", 650, "Bot", "embo", 500, 500, "embo", 0),
    ("freeze_2h", "Congelar balance de otro jugador durante 2 horas", "epic", 750, "Bot", None, 0, 0, "", 1),
    ("split_no_join", "Recibir split de una CTA del día sin participar", "epic", 1000, "Staff", None, 0, 0, "", 0),
    ("cta_free_mount", "CTA con montura libre (aportada por el jugador)", "epic", 1300, "Staff", None, 0, 0, "", 0),
    # 🟠 Legendarias — 4,6 %
    ("guild_kick", "Kick de un miembro de la guild", "legendary", 100, "Staff", None, 0, 0, "", 1),
    ("weapon_83", "Arma 8.3 hasta 12M", "legendary", 250, "Staff", None, 0, 12_000_000, "silver", 0),
    ("wuju_15m", "Jackpot de 15M Wuju Coins", "legendary", 250, "Bot", "wuju", 15_000_000, 15_000_000, "wuju", 0),
    ("regear_full_5m", "Reequipo completo de CTA (presupuesto 5M)", "legendary", 400, "Staff", None, 0, 5_000_000, "silver", 0),
    ("custom_bomb", "Bomb personalizada con 5 personas a elección", "legendary", 1200, "Staff", None, 0, 0, "", 0),
    ("embo_1000", "1.000 de balance de embo", "legendary", 300, "Bot", "embo", 1000, 1000, "embo", 0),
    ("freeze_12h", "Congelar balance de otro jugador durante 12 horas", "legendary", 400, "Bot", None, 0, 0, "", 1),
    ("deny_cta", "Denegar una CTA a un jugador", "legendary", 600, "Staff", None, 0, 0, "", 1),
    ("guild_desc_1d", "Cambiar la descripción de la guild durante 1 día", "legendary", 500, "Staff", None, 0, 0, "", 0),
    ("staff_build", "Elegirle la build a un miembro del staff", "legendary", 600, "Staff", None, 0, 0, "", 1),
    # 🔴 Míticas — 0,2 %
    ("premium_30m", "Premio equivalente a Premium de Albion (30M VAL)", "mythic", 75, "Staff", None, 0, 30_000_000, "val", 0),
    ("freeze_24h", "Congelar balance de otro jugador durante 24 horas", "mythic", 125, "Bot", None, 0, 0, "", 1),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS wjp_prizes (
    key           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    rarity        TEXT NOT NULL,
    weight        INTEGER NOT NULL,
    origin        TEXT NOT NULL,              -- Bot | Staff (como figura en el catálogo)
    handler       TEXT,                       -- canje automático (NULL = encargo al Council)
    amount        INTEGER NOT NULL DEFAULT 0,
    cost          INTEGER NOT NULL DEFAULT 0,
    cost_currency TEXT NOT NULL DEFAULT '',   -- wuju | embo | silver | val | ''
    third_party   INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    sort          INTEGER NOT NULL DEFAULT 0,
    description   TEXT NOT NULL DEFAULT '',
    custom        INTEGER NOT NULL DEFAULT 0,   -- 1 = carta creada desde el bot (/wujupon carta-agregar)
    name_edited   INTEGER NOT NULL DEFAULT 0    -- 1 = el Council cambió el nombre: el catálogo del código ya no lo pisa
);
CREATE TABLE IF NOT EXISTS wjp_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wjp_spins (
    user_id    INTEGER PRIMARY KEY,
    spins      INTEGER NOT NULL DEFAULT 0 CHECK (spins >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_ops (              -- compras y regalos de tiradas (idempotencia)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,                     -- buy | gift
    user_id     INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    spins       INTEGER NOT NULL,
    cost        INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_pulls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key  TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL,
    prize_key    TEXT NOT NULL,
    rarity       TEXT NOT NULL,
    weight       INTEGER NOT NULL,
    total_weight INTEGER NOT NULL,
    card_id      INTEGER NOT NULL,
    announced    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    prize_key  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'available',  -- available | reserved | consumed
    source     TEXT NOT NULL,                       -- pull | gift
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wjp_cards_user ON wjp_cards(user_id, status);
CREATE TABLE IF NOT EXISTS wjp_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key  TEXT NOT NULL UNIQUE,
    card_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    prize_key    TEXT NOT NULL,
    details      TEXT,
    status       TEXT NOT NULL,       -- auto | pending | taken | approved | delivered | rejected | returned
    taker_id     INTEGER,
    approved_by  INTEGER,
    rejected_by  INTEGER,
    delivered_by INTEGER,
    refunded     INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    channel_id   INTEGER,
    message_id   INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wjp_requests_user ON wjp_requests(user_id);
CREATE TABLE IF NOT EXISTS wjp_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER NOT NULL,
    action     TEXT NOT NULL,
    user_id    INTEGER,
    ref        TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
"""

OPEN_STATUSES = ("pending", "taken", "approved")
STATUS_LABELS = {
    "auto": "✅ Entregado automáticamente", "pending": "🕓 Pendiente", "taken": "✋ En proceso",
    "approved": "👍 Aprobado", "delivered": "✅ Entregado", "rejected": "❌ Rechazado", "returned": "↩️ Carta devuelta",
}


class WujuponError(ValueError):
    """Error apto para mostrar al usuario."""


@dataclass
class Prize:
    key: str
    name: str
    rarity: str
    weight: int
    origin: str
    handler: Optional[str]
    amount: int
    cost: int
    cost_currency: str
    third_party: bool
    enabled: bool
    description: str = ""
    custom: bool = False

    @property
    def automatic(self) -> bool:
        return self.handler in AUTO_HANDLERS

    @property
    def rarity_label(self) -> str:
        name, emoji, _ = RARITIES[self.rarity]
        return f"{emoji} {name}"

    @property
    def color(self) -> int:
        return RARITIES[self.rarity][2]


@dataclass
class Card:
    id: int
    user_id: int
    prize_key: str
    status: str
    source: str


@dataclass
class Request:
    id: int
    card_id: int
    user_id: int
    prize_key: str
    details: str
    status: str
    taker_id: Optional[int]
    approved_by: Optional[int]
    rejected_by: Optional[int]
    delivered_by: Optional[int]
    refunded: bool
    note: str
    channel_id: Optional[int]
    message_id: Optional[int]
    created_at: str
    updated_at: str


def chance_text(weight: int, total: int = TOTAL_WEIGHT) -> str:
    """Porcentaje con coma decimal, sin ceros de más (0,075 %, 11 %, 15,2 %)."""
    if total <= 0:
        return "0 %"
    pct = weight * 100 / total
    text = f"{pct:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " %"


# ============================================================
# Esquema y catálogo
# ============================================================
_ready: set = set()


def validate_catalog(catalog=CATALOG) -> None:
    keys = [c[0] for c in catalog]
    if len(keys) != len(set(keys)):
        raise ValueError("Claves de premio duplicadas")
    total = sum(c[3] for c in catalog)
    if total != TOTAL_WEIGHT:
        raise ValueError(f"Las probabilidades suman {total}/{TOTAL_WEIGHT}")


def init_schema(db_path: str) -> None:
    if db_path in _ready:
        return
    validate_catalog()
    eco.init_schema(db_path)
    embo_db.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {c["name"] for c in conn.execute("PRAGMA table_info(wjp_prizes)").fetchall()}
        for col, ddl in (("description", "TEXT NOT NULL DEFAULT ''"), ("custom", "INTEGER NOT NULL DEFAULT 0"),
                         ("name_edited", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE wjp_prizes ADD COLUMN {col} {ddl}")
        with write_tx(conn):
            for i, (key, name, rarity, weight, origin, handler, amount, cost, cur, third) in enumerate(CATALOG):
                # INSERT OR IGNORE: lo que el Council habilitó/deshabilitó se conserva; nombre, peso y handler siguen al código.
                conn.execute("INSERT OR IGNORE INTO wjp_prizes(key, name, rarity, weight, origin, handler, amount, cost, cost_currency, "
                             "third_party, enabled, sort) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                             (key, name, rarity, weight, origin, handler, amount, cost, cur, third, i))
                conn.execute("UPDATE wjp_prizes SET name = CASE WHEN name_edited = 1 THEN name ELSE ? END, rarity=?, weight=?, origin=?, "
                             "handler=?, amount=?, cost=?, cost_currency=?, third_party=?, sort=? WHERE key=? AND custom = 0",
                             (name, rarity, weight, origin, handler, amount, cost, cur, third, i, key))
    finally:
        conn.close()
    _ready.add(db_path)


def _prize(r) -> Prize:
    keys = r.keys()
    return Prize(r["key"], r["name"], r["rarity"], int(r["weight"]), r["origin"], r["handler"], int(r["amount"]), int(r["cost"]),
                 r["cost_currency"], bool(r["third_party"]), bool(r["enabled"]),
                 (r["description"] or "") if "description" in keys else "", bool(r["custom"]) if "custom" in keys else False)


def _open(db_path: str) -> sqlite3.Connection:
    init_schema(db_path)
    return connect(db_path)


def list_prizes(db_path: str, enabled_only: bool = False) -> List[Prize]:
    conn = _open(db_path)
    try:
        sql = "SELECT * FROM wjp_prizes" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY sort"
        return [_prize(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_prize(db_path: str, key: str) -> Optional[Prize]:
    conn = _open(db_path)
    try:
        r = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone()
        return _prize(r) if r else None
    finally:
        conn.close()


def _log(conn, actor_id: int, action: str, user_id: Optional[int] = None, ref: str = "", detail: str = "") -> None:
    conn.execute("INSERT INTO wjp_log(actor_id, action, user_id, ref, detail, created_at) VALUES (?,?,?,?,?,?)",
                 (actor_id, action, user_id, ref, detail[:1000], utcnow_iso()))


def set_prize_enabled(db_path: str, key: str, enabled: bool, actor_id: int) -> bool:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE wjp_prizes SET enabled = ? WHERE key = ? AND enabled != ?", (int(enabled), key, int(enabled)))
            if cur.rowcount:
                _log(conn, actor_id, "prize_enabled" if enabled else "prize_disabled", ref=key)
            return cur.rowcount > 0
    finally:
        conn.close()


MAX_NAME = 90
MAX_DESC = 500
CUSTOM_SORT_BASE = 1000


def _clean_text(text: Optional[str], limit: int, what: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        raise WujuponError(f"{what} puede tener hasta {limit} caracteres.")
    return text


def parse_chance(text: str) -> int:
    """'0,5' / '0.5%' / '11' → peso sobre 100.000 (mínimo 0,001 %)."""
    raw = (text or "").strip().replace("%", "").replace(" ", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise WujuponError("Escribí la probabilidad como porcentaje (ej: 0,5 o 2).")
    weight = round(value * TOTAL_WEIGHT / 100)
    if abs(weight * 100 / TOTAL_WEIGHT - value) > 1e-9 or not 1 <= weight <= TOTAL_WEIGHT:
        raise WujuponError("La probabilidad tiene que estar entre 0,001 % y 100 %, con hasta 3 decimales.")
    return weight


def enabled_total(db_path: str) -> int:
    conn = _open(db_path)
    try:
        return int(conn.execute("SELECT COALESCE(SUM(weight), 0) FROM wjp_prizes WHERE enabled = 1").fetchone()[0])
    finally:
        conn.close()


def add_prize(db_path: str, *, actor_id: int, name: str, description: str, rarity: str, weight: int,
              third_party: bool = False) -> Prize:
    """Carta nueva creada desde el bot. Siempre se canjea por encargo al Council (no tiene canje automático)."""
    if rarity not in RARITIES:
        raise WujuponError("Rareza inválida.")
    name = _clean_text(name, MAX_NAME, "El nombre")
    if not name:
        raise WujuponError("El nombre no puede estar vacío.")
    description = _clean_text(description, MAX_DESC, "La descripción")
    if not 1 <= int(weight) <= TOTAL_WEIGHT:
        raise WujuponError("Probabilidad inválida.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_prizes WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise WujuponError("Ya hay una carta con ese nombre.")
            n = int(conn.execute("SELECT COUNT(*) FROM wjp_prizes WHERE custom = 1").fetchone()[0]) + 1
            key = f"custom_{n}"
            while conn.execute("SELECT 1 FROM wjp_prizes WHERE key = ?", (key,)).fetchone():
                n += 1
                key = f"custom_{n}"
            conn.execute("INSERT INTO wjp_prizes(key, name, rarity, weight, origin, handler, amount, cost, cost_currency, third_party, "
                         "enabled, sort, description, custom, name_edited) VALUES (?,?,?,?,'Staff',NULL,0,0,'',?,1,?,?,1,1)",
                         (key, name, rarity, int(weight), int(bool(third_party)), CUSTOM_SORT_BASE + n, description))
            _log(conn, actor_id, "prize_added", ref=key, detail=f"{name} · {rarity} · {chance_text(int(weight))}")
            return _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone())
    finally:
        conn.close()


def edit_prize(db_path: str, key: str, *, actor_id: int, name: Optional[str] = None,
               description: Optional[str] = None) -> Tuple[Prize, Prize]:
    """Cambia nombre y/o descripción. Las cartas ya obtenidas muestran el texto nuevo. Devuelve (antes, después)."""
    if name is None and description is None:
        raise WujuponError("Indicá un nombre nuevo, una descripción nueva o ambos.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise WujuponError("Esa carta no existe.")
            before = _prize(row)
            changes = []
            if name is not None:
                new_name = _clean_text(name, MAX_NAME, "El nombre")
                if not new_name:
                    raise WujuponError("El nombre no puede estar vacío.")
                if conn.execute("SELECT 1 FROM wjp_prizes WHERE lower(name) = lower(?) AND key != ?", (new_name, key)).fetchone():
                    raise WujuponError("Ya hay otra carta con ese nombre.")
                conn.execute("UPDATE wjp_prizes SET name = ?, name_edited = 1 WHERE key = ?", (new_name, key))
                changes.append(f"nombre: {before.name} → {new_name}")
            if description is not None:
                new_desc = "" if description.strip().lower() in ("-", "quitar", "borrar") else _clean_text(description, MAX_DESC, "La descripción")
                conn.execute("UPDATE wjp_prizes SET description = ? WHERE key = ?", (new_desc, key))
                changes.append("descripción actualizada" if new_desc else "descripción quitada")
            _log(conn, actor_id, "prize_edited", ref=key, detail=" · ".join(changes))
            return before, _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone())
    finally:
        conn.close()


def get_setting(db_path: str, key: str) -> Optional[str]:
    conn = _open(db_path)
    try:
        r = conn.execute("SELECT value FROM wjp_settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_setting(db_path: str, key: str, value: Optional[str]) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if value is None:
                conn.execute("DELETE FROM wjp_settings WHERE key = ?", (key,))
            else:
                conn.execute("INSERT OR REPLACE INTO wjp_settings(key, value) VALUES (?,?)", (key, str(value)))
    finally:
        conn.close()


def get_price(db_path: str) -> int:
    v = get_setting(db_path, "price")
    try:
        return int(v) if v else DEFAULT_PRICE
    except ValueError:
        return DEFAULT_PRICE


# ============================================================
# Tiradas
# ============================================================
def _spins(conn, user_id: int) -> int:
    r = conn.execute("SELECT spins FROM wjp_spins WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["spins"]) if r else 0


def _add_spins(conn, user_id: int, delta: int) -> int:
    now = utcnow_iso()
    conn.execute("INSERT OR IGNORE INTO wjp_spins(user_id, spins, updated_at) VALUES (?,0,?)", (user_id, now))
    after = _spins(conn, user_id) + delta
    if after < 0:
        raise WujuponError("No tiene tantas tiradas.")
    conn.execute("UPDATE wjp_spins SET spins = ?, updated_at = ? WHERE user_id = ?", (after, now, user_id))
    return after


def get_spins(db_path: str, user_id: int) -> int:
    conn = _open(db_path)
    try:
        return _spins(conn, user_id)
    finally:
        conn.close()


@dataclass
class BuyResult:
    status: str               # ok | duplicate | funds | frozen | invalid
    spins: int = 0
    cost: int = 0
    balance: int = 0


def buy_spins(db_path: str, user_id: int, count: int, request_key: str) -> BuyResult:
    if not 1 <= count <= MAX_BUY:
        return BuyResult("invalid")
    price = get_price(db_path)
    cost = price * count
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_ops WHERE request_key = ?", (request_key,)).fetchone():
                return BuyResult("duplicate", _spins(conn, user_id), cost, eco_balance(conn, user_id))
            op_id = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"wjp:buy:{request_key}",
                                         total_amount=cost, target_count=1, reason=f"Wujupón: {count} tirada(s)",
                                         related_id=f"wjp:buy:{request_key}")
            try:
                _, after = eco.tx_move(conn, op_id=op_id, user_id=user_id, delta=-cost, mov_type="wujupon_purchase",
                                       reason=f"Wujupón: {count} tirada(s)", actor_id=user_id,
                                       related_id=f"wjp:buy:{request_key}", require_funds=True)
            except eco.InsufficientFunds as exc:
                raise _Abort(BuyResult("funds", cost=cost, balance=int(exc.args[0]) if exc.args else 0))
            except eco.BalanceFrozen:
                raise _Abort(BuyResult("frozen", cost=cost))
            spins = _add_spins(conn, user_id, count)
            conn.execute("INSERT INTO wjp_ops(request_key, kind, user_id, actor_id, spins, cost, created_at) VALUES (?,?,?,?,?,?,?)",
                         (request_key, "buy", user_id, user_id, count, cost, utcnow_iso()))
            return BuyResult("ok", spins, cost, after)
    except _Abort as a:
        return a.result
    finally:
        conn.close()


class _Abort(Exception):
    """Revierte la transacción y devuelve un resultado (sin propagar error)."""
    def __init__(self, result):
        super().__init__()
        self.result = result


def eco_balance(conn, user_id: int) -> int:
    r = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["balance"]) if r else 0


def gift_spins(db_path: str, *, actor_id: int, user_id: int, count: int, request_key: str, reason: str) -> int:
    """Regala (count > 0) o quita (count < 0) tiradas. Devuelve las tiradas resultantes."""
    if count == 0 or abs(count) > 1000:
        raise WujuponError("La cantidad de tiradas tiene que estar entre 1 y 1000.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_ops WHERE request_key = ?", (request_key,)).fetchone():
                return _spins(conn, user_id)
            after = _add_spins(conn, user_id, count)
            conn.execute("INSERT INTO wjp_ops(request_key, kind, user_id, actor_id, spins, cost, reason, created_at) "
                         "VALUES (?,?,?,?,?,0,?,?)", (request_key, "gift", user_id, actor_id, count, reason, utcnow_iso()))
            _log(conn, actor_id, "gift_spins" if count > 0 else "remove_spins", user_id, request_key, f"{count} · {reason}")
            return after
    finally:
        conn.close()


@dataclass
class PullResult:
    status: str                       # ok | duplicate | no_spins | empty
    prize: Optional[Prize] = None
    card_id: int = 0
    pull_id: int = 0
    weight: int = 0
    total_weight: int = 0
    spins_left: int = 0


def draw(prizes: List[Prize], rng=None) -> Tuple[Prize, int]:
    """Elige un premio según su peso. Devuelve (premio, peso total)."""
    total = sum(p.weight for p in prizes)
    if total <= 0:
        raise WujuponError("No hay premios habilitados.")
    roll = rng.randrange(total) if rng is not None else secrets.randbelow(total)
    acc = 0
    for p in prizes:
        acc += p.weight
        if roll < acc:
            return p, total
    return prizes[-1], total


def pull(db_path: str, user_id: int, request_key: str, rng: Optional[random.Random] = None) -> PullResult:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            prev = conn.execute("SELECT * FROM wjp_pulls WHERE request_key = ?", (request_key,)).fetchone()
            if prev is not None:
                pr = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (prev["prize_key"],)).fetchone()
                return PullResult("duplicate", _prize(pr), int(prev["card_id"]), int(prev["id"]), int(prev["weight"]),
                                  int(prev["total_weight"]), _spins(conn, user_id))
            if _spins(conn, user_id) <= 0:
                return PullResult("no_spins")
            prizes = [_prize(r) for r in conn.execute("SELECT * FROM wjp_prizes WHERE enabled = 1 ORDER BY sort").fetchall()]
            if not prizes:
                return PullResult("empty")
            prize, total = draw(prizes, rng)
            left = _add_spins(conn, user_id, -1)
            now = utcnow_iso()
            card_id = conn.execute("INSERT INTO wjp_cards(user_id, prize_key, status, source, created_at, updated_at) "
                                   "VALUES (?,?,'available','pull',?,?)", (user_id, prize.key, now, now)).lastrowid
            pull_id = conn.execute("INSERT INTO wjp_pulls(request_key, user_id, prize_key, rarity, weight, total_weight, card_id, created_at) "
                                   "VALUES (?,?,?,?,?,?,?,?)",
                                   (request_key, user_id, prize.key, prize.rarity, prize.weight, total, card_id, now)).lastrowid
            return PullResult("ok", prize, int(card_id), int(pull_id), prize.weight, total, left)
    finally:
        conn.close()


def unannounced_pulls(db_path: str, limit: int = 20) -> List[Tuple[int, int, str, int, int]]:
    """(pull_id, user_id, prize_key, weight, total_weight) sin anunciar, más viejas primero."""
    conn = _open(db_path)
    try:
        rows = conn.execute("SELECT id, user_id, prize_key, weight, total_weight FROM wjp_pulls WHERE announced = 0 "
                            "ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(int(r["id"]), int(r["user_id"]), r["prize_key"], int(r["weight"]), int(r["total_weight"])) for r in rows]
    finally:
        conn.close()


def mark_all_announced(db_path: str) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_pulls SET announced = 1 WHERE announced = 0")
    finally:
        conn.close()


def mark_announced(db_path: str, pull_ids: List[int]) -> None:
    if not pull_ids:
        return
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.executemany("UPDATE wjp_pulls SET announced = 1 WHERE id = ?", [(i,) for i in pull_ids])
    finally:
        conn.close()


# ============================================================
# Cartas e inventario
# ============================================================
def _card(r) -> Optional[Card]:
    return Card(int(r["id"]), int(r["user_id"]), r["prize_key"], r["status"], r["source"]) if r else None


def get_card(db_path: str, card_id: int) -> Optional[Card]:
    conn = _open(db_path)
    try:
        return _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
    finally:
        conn.close()


@dataclass
class InventoryRow:
    prize: Prize
    available: int
    reserved: int
    first_card_id: Optional[int]      # carta disponible más vieja (la que se canjea)


def inventory(db_path: str, user_id: int) -> List[InventoryRow]:
    conn = _open(db_path)
    try:
        rows = conn.execute(
            """SELECT p.*, SUM(c.status = 'available') AS av, SUM(c.status = 'reserved') AS rs,
                      MIN(CASE WHEN c.status = 'available' THEN c.id END) AS first_id
                 FROM wjp_cards c JOIN wjp_prizes p ON p.key = c.prize_key
                WHERE c.user_id = ? AND c.status IN ('available', 'reserved')
                GROUP BY p.key ORDER BY p.sort""", (user_id,)).fetchall()
        return [InventoryRow(_prize(r), int(r["av"] or 0), int(r["rs"] or 0), int(r["first_id"]) if r["first_id"] else None)
                for r in rows]
    finally:
        conn.close()


def give_card(db_path: str, *, actor_id: int, user_id: int, prize_key: str, reason: str) -> int:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_prizes WHERE key = ?", (prize_key,)).fetchone() is None:
                raise WujuponError("Ese premio no existe.")
            now = utcnow_iso()
            card_id = conn.execute("INSERT INTO wjp_cards(user_id, prize_key, status, source, created_at, updated_at) "
                                   "VALUES (?,?,'available','gift',?,?)", (user_id, prize_key, now, now)).lastrowid
            _log(conn, actor_id, "give_card", user_id, f"card:{card_id}", f"{prize_key} · {reason}")
            return int(card_id)
    finally:
        conn.close()


def return_card(db_path: str, *, card_id: int, actor_id: int, reason: str) -> Tuple[Card, Optional[int]]:
    """Devuelve una carta usada (o reservada) al inventario de su dueño. Si tenía un encargo abierto, lo cierra como
    «carta devuelta». Devuelve (carta, id del encargo cerrado o None)."""
    conn = _open(db_path)
    try:
        with write_tx(conn):
            card = _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
            if card is None:
                raise WujuponError("No existe esa carta.")
            if card.status == "available":
                raise WujuponError("Esa carta ya está disponible en el inventario: no hay nada que devolver.")
            now = utcnow_iso()
            conn.execute("UPDATE wjp_cards SET status = 'available', updated_at = ? WHERE id = ?", (now, card_id))
            req = conn.execute(f"SELECT id FROM wjp_requests WHERE card_id = ? AND status IN ({','.join('?' * len(OPEN_STATUSES))}) "
                               "ORDER BY id DESC LIMIT 1", (card_id, *OPEN_STATUSES)).fetchone()
            req_id = int(req["id"]) if req else None
            if req_id:
                conn.execute("UPDATE wjp_requests SET status = 'returned', refunded = 1, note = ?, updated_at = ? WHERE id = ?",
                             (reason[:500], now, req_id))
            _log(conn, actor_id, "return_card", card.user_id, f"card:{card_id}", reason)
            card.status = "available"
            return card, req_id
    finally:
        conn.close()


# ============================================================
# Canjes
# ============================================================
def _request(r) -> Optional[Request]:
    if r is None:
        return None
    return Request(int(r["id"]), int(r["card_id"]), int(r["user_id"]), r["prize_key"], r["details"] or "", r["status"],
                   r["taker_id"], r["approved_by"], r["rejected_by"], r["delivered_by"], bool(r["refunded"]), r["note"] or "",
                   r["channel_id"], r["message_id"], r["created_at"], r["updated_at"])


def get_request(db_path: str, req_id: int) -> Optional[Request]:
    conn = _open(db_path)
    try:
        return _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
    finally:
        conn.close()


def find_request_by_message(db_path: str, message_id: int) -> Optional[Request]:
    conn = _open(db_path)
    try:
        return _request(conn.execute("SELECT * FROM wjp_requests WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def user_requests(db_path: str, user_id: int, limit: int = 15) -> List[Request]:
    conn = _open(db_path)
    try:
        return [_request(r) for r in conn.execute("SELECT * FROM wjp_requests WHERE user_id = ? AND status != 'auto' "
                                                  "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()]
    finally:
        conn.close()


def open_requests(db_path: str, limit: int = 25) -> List[Request]:
    conn = _open(db_path)
    try:
        q = f"SELECT * FROM wjp_requests WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id LIMIT ?"
        return [_request(r) for r in conn.execute(q, (*OPEN_STATUSES, limit)).fetchall()]
    finally:
        conn.close()


def unpublished_requests(db_path: str) -> List[int]:
    conn = _open(db_path)
    try:
        q = f"SELECT id FROM wjp_requests WHERE message_id IS NULL AND status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id"
        return [int(r["id"]) for r in conn.execute(q, OPEN_STATUSES).fetchall()]
    finally:
        conn.close()


def set_request_message(db_path: str, req_id: int, channel_id: int, message_id: int) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_requests SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, req_id))
    finally:
        conn.close()


@dataclass
class RedeemResult:
    status: str               # auto | pending | duplicate | not_found | not_yours | not_available | disabled | no_effect
    message: str = ""
    request: Optional[Request] = None
    prize: Optional[Prize] = None


def _run_handler(conn, prize: Prize, card: Card, *, guild_id: int, req_key: str) -> str:
    """Ejecuta un canje automático dentro de la transacción. Devuelve el texto del resultado o lanza _Abort."""
    if prize.handler == "wuju":
        op_id = eco.tx_new_operation(conn, kind="add", actor_id=card.user_id, request_key=f"wjp:card:{card.id}",
                                     total_amount=prize.amount, target_count=1, reason=f"Wujupón: {prize.name}",
                                     related_id=f"wjp:card:{card.id}")
        _, after = eco.tx_move(conn, op_id=op_id, user_id=card.user_id, delta=prize.amount, mov_type="wujupon_prize",
                               reason=f"Wujupón: {prize.name}", actor_id=card.user_id, related_id=f"wjp:card:{card.id}")
        return f"Se acreditaron **{eco.format_amount(prize.amount)} {eco.CURRENCY_NAME}**. Balance: {eco.format_amount(after)}."
    if prize.handler == "embo":
        ch = embo_db.tx_apply(conn, user_id=card.user_id, mode="add", amount=prize.amount, actor_id=card.user_id,
                              request_key=f"wjp:card:{card.id}", reason=f"Wujupón: {prize.name}", kind="wujupon_prize")
        return f"Se acreditaron **{eco.format_amount(prize.amount)} de embo**. Balance de embo: {eco.format_amount(ch.after)}."
    if prize.handler == "sell_cooldown":
        import deposits_db
        if not deposits_db.tx_reset_sell_cooldown(conn, guild_id, card.user_id):
            raise _Abort(RedeemResult("no_effect", "No tenés el cooldown de «Cobrar balance» activo: guardá la carta para cuando lo tengas.",
                                      prize=prize))
        return "Listo: ya podés volver a abrir un ticket de **Cobrar balance**."
    raise ValueError(f"handler desconocido: {prize.handler}")


def redeem(db_path: str, *, card_id: int, user_id: int, details: str, request_key: str, guild_id: int) -> RedeemResult:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            prev = _request(conn.execute("SELECT * FROM wjp_requests WHERE request_key = ?", (request_key,)).fetchone())
            if prev is not None:
                return RedeemResult("duplicate", request=prev)
            card = _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
            if card is None:
                return RedeemResult("not_found", "No existe esa carta.")
            if card.user_id != user_id:
                return RedeemResult("not_yours", "Esa carta no es tuya.")
            prize = _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (card.prize_key,)).fetchone())
            if not prize.enabled:
                return RedeemResult("disabled", "Ese premio está deshabilitado por el Council por ahora. La carta sigue en tu inventario.",
                                    prize=prize)
            now = utcnow_iso()
            moved = conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ? AND status = 'available'",
                                 ("consumed" if prize.automatic else "reserved", now, card_id)).rowcount
            if not moved:
                return RedeemResult("not_available", "Esa carta ya fue usada o tiene un canje en curso.", prize=prize)
            message = _run_handler(conn, prize, card, guild_id=guild_id, req_key=request_key) if prize.automatic else ""
            status = "auto" if prize.automatic else "pending"
            req_id = conn.execute("INSERT INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, "
                                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (request_key, card_id, user_id, prize.key, (details or "")[:1000], status, message, now, now)).lastrowid
            req = _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
            return RedeemResult(status, message, req, prize)
    except _Abort as a:
        return a.result
    finally:
        conn.close()


@dataclass
class Change:
    ok: bool
    message: str = ""
    request: Optional[Request] = None


def _transition(db_path: str, req_id: int, actor_id: int, action: str, *, refund: bool = False, note: str = "") -> Change:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            req = _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
            if req is None:
                return Change(False, "No existe ese encargo.")
            if req.status not in OPEN_STATUSES:
                return Change(False, f"El encargo ya está cerrado ({STATUS_LABELS.get(req.status, req.status)}).", req)
            now = utcnow_iso()
            if action == "take":
                if req.taker_id == actor_id:
                    return Change(False, "Ya lo tenés tomado vos.", req)
                conn.execute("UPDATE wjp_requests SET taker_id = ?, status = CASE WHEN status = 'pending' THEN 'taken' ELSE status END, "
                             "updated_at = ? WHERE id = ?", (actor_id, now, req_id))
            elif action == "approve":
                if req.status == "approved":
                    return Change(False, "Ya estaba aprobado.", req)
                conn.execute("UPDATE wjp_requests SET status = 'approved', approved_by = ?, taker_id = COALESCE(taker_id, ?), "
                             "updated_at = ? WHERE id = ?", (actor_id, actor_id, now, req_id))
            elif action == "deliver":
                conn.execute("UPDATE wjp_requests SET status = 'delivered', delivered_by = ?, taker_id = COALESCE(taker_id, ?), "
                             "updated_at = ? WHERE id = ?", (actor_id, actor_id, now, req_id))
                conn.execute("UPDATE wjp_cards SET status = 'consumed', updated_at = ? WHERE id = ?", (now, req.card_id))
            elif action == "reject":
                conn.execute("UPDATE wjp_requests SET status = 'rejected', rejected_by = ?, refunded = ?, note = ?, updated_at = ? "
                             "WHERE id = ?", (actor_id, int(refund), note[:500], now, req_id))
                conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ?",
                             ("available" if refund else "consumed", now, req.card_id))
            else:
                raise ValueError(action)
            _log(conn, actor_id, f"request_{action}", req.user_id, f"req:{req_id}", note if action == "reject" else "")
            return Change(True, "", _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone()))
    finally:
        conn.close()


def take_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "take")


def approve_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "approve")


def deliver_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "deliver")


def reject_request(db_path: str, req_id: int, actor_id: int, *, refund: bool, note: str = "") -> Change:
    return _transition(db_path, req_id, actor_id, "reject", refund=refund, note=note)


# ============================================================
# Estadísticas y registro
# ============================================================
@dataclass
class Stats:
    pulls: int
    by_rarity: Dict[str, int]
    spins_bought: int
    wuju_spent: int
    spins_gifted: int
    spins_unused: int
    paid: Dict[str, int]          # moneda → entregado (canjes automáticos + encargos entregados)
    pending: Dict[str, int]       # moneda → pasivo de cartas sin canjear o con encargo abierto
    open_requests: int


def _cost_by_currency(rows) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        if r["cost_currency"]:
            out[r["cost_currency"]] = out.get(r["cost_currency"], 0) + int(r["total"] or 0)
    return out


def get_stats(db_path: str) -> Stats:
    conn = _open(db_path)
    try:
        pulls = int(conn.execute("SELECT COUNT(*) FROM wjp_pulls").fetchone()[0])
        by_rarity = {r["rarity"]: int(r["n"]) for r in conn.execute("SELECT rarity, COUNT(*) AS n FROM wjp_pulls GROUP BY rarity")}
        b = conn.execute("SELECT COALESCE(SUM(spins),0) AS s, COALESCE(SUM(cost),0) AS c FROM wjp_ops WHERE kind = 'buy'").fetchone()
        gifted = int(conn.execute("SELECT COALESCE(SUM(spins),0) FROM wjp_ops WHERE kind = 'gift'").fetchone()[0])
        unused = int(conn.execute("SELECT COALESCE(SUM(spins),0) FROM wjp_spins").fetchone()[0])
        paid = _cost_by_currency(conn.execute(
            "SELECT p.cost_currency, SUM(p.cost) AS total FROM wjp_requests r JOIN wjp_prizes p ON p.key = r.prize_key "
            "WHERE r.status IN ('auto', 'delivered') GROUP BY p.cost_currency").fetchall())
        pending = _cost_by_currency(conn.execute(
            "SELECT p.cost_currency, SUM(p.cost) AS total FROM wjp_cards c JOIN wjp_prizes p ON p.key = c.prize_key "
            "WHERE c.status IN ('available', 'reserved') GROUP BY p.cost_currency").fetchall())
        q = f"SELECT COUNT(*) FROM wjp_requests WHERE status IN ({','.join('?' * len(OPEN_STATUSES))})"
        open_n = int(conn.execute(q, OPEN_STATUSES).fetchone()[0])
        return Stats(pulls, by_rarity, int(b["s"]), int(b["c"]), gifted, unused, paid, pending, open_n)
    finally:
        conn.close()


def silver_equivalent(by_currency: Dict[str, int]) -> int:
    """Wuju = silver 1:1, embo = 10.000. VAL no se convierte (se informa aparte)."""
    return (by_currency.get("wuju", 0) + by_currency.get("silver", 0) + by_currency.get("embo", 0) * EMBO_SILVER_VALUE)


def expected_cost(prizes: List[Prize]) -> Dict[str, float]:
    """Costo esperado por tirada, por moneda, según los pesos habilitados."""
    total = sum(p.weight for p in prizes) or 1
    out: Dict[str, float] = {}
    for p in prizes:
        if p.cost_currency:
            out[p.cost_currency] = out.get(p.cost_currency, 0.0) + p.cost * p.weight / total
    return out


def recent_log(db_path: str, limit: int = 20) -> List[sqlite3.Row]:
    conn = _open(db_path)
    try:
        return conn.execute("SELECT * FROM wjp_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()


def recent_pulls(db_path: str, user_id: Optional[int] = None, limit: int = 20) -> List[sqlite3.Row]:
    conn = _open(db_path)
    try:
        if user_id is None:
            return conn.execute("SELECT * FROM wjp_pulls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return conn.execute("SELECT * FROM wjp_pulls WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    finally:
        conn.close()
