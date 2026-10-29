"""Splits (/transparencia): datos, cálculo y cierre. Sin Discord: solo SQLite y matemática.

Cálculo (todo en enteros de silver, redondeando siempre hacia abajo):
    bruto           = total + Σ looteado de los looters
    reparación tot. = reparación + Σ reparación de los looters
    neto            = bruto − reparación tot.            (si da negativo se toma 0)
    tax de venta    = neto × tax_venta %
    tax de guild    = neto × tax_guild %
    tax looter (c/u)= (looteado − reparación del looter, mínimo 0) × tax_looter %
    a splitear      = neto − tax de venta − tax de guild − Σ tax looters
    por persona     = a splitear ÷ cantidad de personas listadas
Al cerrar: cada persona listada recibe `por persona` y cada looter recibe además su tax looter.
"""
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

DEFAULT_TAX_SALE = 20.0
DEFAULT_TAX_GUILD = 5.0
DEFAULT_TAX_LOOTER = 10.0
MAX_PEOPLE = 100
MAX_LOOTERS = 25
MAX_TITLE, MAX_TEXT = 100, 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS splits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    title            TEXT NOT NULL,
    tab              TEXT,
    city             TEXT,
    role_id          INTEGER,
    total            INTEGER NOT NULL DEFAULT 0,
    repair           INTEGER NOT NULL DEFAULT 0,
    tax_sale         REAL NOT NULL DEFAULT 20,
    tax_guild        REAL NOT NULL DEFAULT 5,
    tax_looter       REAL NOT NULL DEFAULT 10,
    channel_id       INTEGER,
    message_id       INTEGER,
    thread_id        INTEGER,
    list_message_id  INTEGER,
    data_message_id  INTEGER,
    creator_id       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',    -- open | closing | closed | cancelled
    created_at       TEXT NOT NULL,
    closed_at        TEXT,
    closed_by        INTEGER,
    source_cta_id    INTEGER                           -- CTA de la que salió este split (NULL = creado a mano)
);
CREATE TABLE IF NOT EXISTS split_requests (            -- anotados después del cierre de la CTA: el organizador decide
    split_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    slot_no    INTEGER,
    status     TEXT NOT NULL DEFAULT 'pending',        -- pending | accepted | rejected
    message_id INTEGER,
    created_at TEXT NOT NULL,
    decided_by INTEGER,
    PRIMARY KEY (split_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_split_requests_msg ON split_requests(message_id);
CREATE TABLE IF NOT EXISTS split_people (
    split_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    pos      INTEGER NOT NULL,
    PRIMARY KEY (split_id, user_id)
);
CREATE TABLE IF NOT EXISTS split_looters (
    split_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    looted   INTEGER NOT NULL DEFAULT 0,
    repair   INTEGER NOT NULL DEFAULT 0,
    tax_pct  REAL,                                    -- NULL = usa el tax looter por defecto del split
    pos      INTEGER NOT NULL,
    PRIMARY KEY (split_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_splits_thread ON splits(thread_id);
CREATE INDEX IF NOT EXISTS idx_splits_message ON splits(message_id);
"""


class SplitError(ValueError):
    """Mensaje que se le puede mostrar tal cual a quien lo causó."""


@dataclass
class Looter:
    user_id: int
    looted: int = 0
    repair: int = 0
    tax_pct: Optional[float] = None


@dataclass
class Split:
    id: int
    guild_id: int
    title: str
    tab: Optional[str]
    city: Optional[str]
    role_id: Optional[int]
    total: int
    repair: int
    tax_sale: float
    tax_guild: float
    tax_looter: float
    channel_id: Optional[int]
    message_id: Optional[int]
    thread_id: Optional[int]
    list_message_id: Optional[int]
    data_message_id: Optional[int]
    creator_id: int
    status: str
    source_cta_id: Optional[int] = None
    people: List[int] = field(default_factory=list)
    looters: List[Looter] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def looter_pct(self, lt: Looter) -> float:
        return self.tax_looter if lt.tax_pct is None else lt.tax_pct


@dataclass
class Calc:
    looted_total: int
    repair_total: int
    gross: int
    net: int
    sale_tax: int
    guild_tax: int
    looter_taxes: Dict[int, int]
    to_split: int
    people: int
    per_person: int
    leftover: int

    @property
    def looter_tax_total(self) -> int:
        return sum(self.looter_taxes.values())


def _floor(x: Decimal) -> int:
    return int(math.floor(x))


def compute(s: Split) -> Calc:
    looted_total = sum(l.looted for l in s.looters)
    repair_total = s.repair + sum(l.repair for l in s.looters)
    gross = s.total + looted_total
    net = max(gross - repair_total, 0)
    sale = _floor(Decimal(net) * Decimal(str(s.tax_sale)) / 100)
    guild = _floor(Decimal(net) * Decimal(str(s.tax_guild)) / 100)
    lt = {l.user_id: _floor(Decimal(max(l.looted - l.repair, 0)) * Decimal(str(s.looter_pct(l))) / 100) for l in s.looters}
    to_split = max(net - sale - guild - sum(lt.values()), 0)
    n = len(s.people)
    per = to_split // n if n else 0
    return Calc(looted_total, repair_total, gross, net, sale, guild, lt, to_split, n, per, to_split - per * n)


# ---------- validaciones ----------
def parse_money(text, *, field_name: str, allow_zero: bool = True) -> int:
    t = (text or "").strip()
    if t in ("", "0") and allow_zero:
        return 0
    try:
        v = eco.parse_amount(t)
    except Exception as exc:
        raise SplitError(f"{field_name}: {exc}") from None
    if v > eco.MAX_AMOUNT:
        raise SplitError(f"{field_name}: el valor es demasiado grande.")
    return v


def parse_pct(text, *, field_name: str) -> float:
    t = (text or "").strip().replace("%", "").replace(",", ".")
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError):
        raise SplitError(f"{field_name}: poné un porcentaje válido (ej: 20 o 7.5).") from None
    if not d.is_finite() or d < 0 or d > 100:
        raise SplitError(f"{field_name}: tiene que estar entre 0 y 100.")
    return float(d)


def _clean(text: Optional[str], limit: int, name: str, required: bool = False) -> Optional[str]:
    t = " ".join((text or "").split())
    if not t:
        if required:
            raise SplitError(f"{name}: no puede estar vacío.")
        return None
    if len(t) > limit:
        raise SplitError(f"{name}: máximo {limit} caracteres.")
    return t


# ---------- carga ----------
def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(splits)")}
        if "source_cta_id" not in cols:                      # base creada antes de esta columna
            conn.execute("ALTER TABLE splits ADD COLUMN source_cta_id INTEGER")
            conn.commit()
    finally:
        conn.close()


_COLS = ("id, guild_id, title, tab, city, role_id, total, repair, tax_sale, tax_guild, tax_looter, channel_id, message_id, "
         "thread_id, list_message_id, data_message_id, creator_id, status, source_cta_id")


def _load(conn: sqlite3.Connection, split_id: int) -> Optional[Split]:
    r = conn.execute(f"SELECT {_COLS} FROM splits WHERE id = ?", (split_id,)).fetchone()
    if r is None:
        return None
    s = Split(*tuple(r))
    s.people = [int(x["user_id"]) for x in conn.execute("SELECT user_id FROM split_people WHERE split_id=? ORDER BY pos", (split_id,))]
    s.looters = [Looter(int(x["user_id"]), int(x["looted"]), int(x["repair"]), x["tax_pct"])
                 for x in conn.execute("SELECT user_id, looted, repair, tax_pct FROM split_looters WHERE split_id=? ORDER BY pos", (split_id,))]
    return s


def get_split(db_path: str, split_id: int) -> Optional[Split]:
    conn = connect(db_path)
    try:
        return _load(conn, split_id)
    finally:
        conn.close()


def _find(db_path: str, col: str, value: int) -> Optional[Split]:
    conn = connect(db_path)
    try:
        r = conn.execute(f"SELECT id FROM splits WHERE {col} = ? ORDER BY id DESC LIMIT 1", (value,)).fetchone()
        return _load(conn, int(r["id"])) if r else None
    finally:
        conn.close()


def find_by_thread(db_path: str, thread_id: int) -> Optional[Split]:
    return _find(db_path, "thread_id", thread_id)


def find_by_cta(db_path: str, cta_id: int) -> Optional[Split]:
    return _find(db_path, "source_cta_id", cta_id)


def find_by_message(db_path: str, message_id: int) -> Optional[Split]:
    return _find(db_path, "message_id", message_id)


def _next_pos(conn, table: str, split_id: int) -> int:
    r = conn.execute(f"SELECT COALESCE(MAX(pos), 0) + 1 AS p FROM {table} WHERE split_id=?", (split_id,)).fetchone()
    return int(r["p"])


def _require_open(conn, split_id: int) -> None:
    r = conn.execute("SELECT status FROM splits WHERE id=?", (split_id,)).fetchone()
    if r is None:
        raise SplitError("Ese split no existe.")
    if r["status"] != "open":
        raise SplitError("Ese split ya está cerrado: no se puede modificar.")


# ---------- alta y edición ----------
def create_split(db_path: str, *, guild_id: int, creator_id: int, title: str, tab: Optional[str] = None, city: Optional[str] = None,
                 role_id: Optional[int] = None, total: int = 0, repair: int = 0,
                 people: Iterable[int] = (), looters: Iterable[int] = (), source_cta_id: Optional[int] = None) -> int:
    title = _clean(title, MAX_TITLE, "Título", required=True)
    tab, city = _clean(tab, MAX_TEXT, "Tab"), _clean(city, MAX_TEXT, "City")
    people_l = list(dict.fromkeys(int(p) for p in people))
    looters_l = list(dict.fromkeys(int(p) for p in looters))
    for l in looters_l:
        if l not in people_l:
            people_l.append(l)
    if len(people_l) > MAX_PEOPLE:
        raise SplitError(f"Máximo {MAX_PEOPLE} personas en un split (hay {len(people_l)}).")
    if len(looters_l) > MAX_LOOTERS:
        raise SplitError(f"Máximo {MAX_LOOTERS} looters.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "INSERT INTO splits(guild_id, title, tab, city, role_id, total, repair, tax_sale, tax_guild, tax_looter, creator_id, status, created_at, source_cta_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
                (guild_id, title, tab, city, role_id, total, repair, DEFAULT_TAX_SALE, DEFAULT_TAX_GUILD, DEFAULT_TAX_LOOTER, creator_id, utcnow_iso(), source_cta_id))
            sid = int(cur.lastrowid)
            for i, uid in enumerate(people_l, 1):
                conn.execute("INSERT INTO split_people(split_id, user_id, pos) VALUES (?,?,?)", (sid, uid, i))
            for i, uid in enumerate(looters_l, 1):
                conn.execute("INSERT INTO split_looters(split_id, user_id, pos) VALUES (?,?,?)", (sid, uid, i))
            return sid
    finally:
        conn.close()


def set_location(db_path: str, split_id: int, **cols) -> None:
    allowed = {"channel_id", "message_id", "thread_id", "list_message_id", "data_message_id"}
    if not cols or set(cols) - allowed:
        raise ValueError("columnas inválidas")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute(f"UPDATE splits SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), split_id))
    finally:
        conn.close()


def abort_unpublished(db_path: str, split_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            for t in ("split_people", "split_looters"):
                conn.execute(f"DELETE FROM {t} WHERE split_id=?", (split_id,))
            conn.execute("DELETE FROM splits WHERE id=? AND message_id IS NULL", (split_id,))
    finally:
        conn.close()


def update_info(db_path: str, split_id: int, *, title=None, tab=None, city=None, clear_tab=False, clear_city=False) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            if title is not None:
                conn.execute("UPDATE splits SET title=? WHERE id=?", (_clean(title, MAX_TITLE, "Título", required=True), split_id))
            if tab is not None or clear_tab:
                conn.execute("UPDATE splits SET tab=? WHERE id=?", (_clean(tab, MAX_TEXT, "Tab"), split_id))
            if city is not None or clear_city:
                conn.execute("UPDATE splits SET city=? WHERE id=?", (_clean(city, MAX_TEXT, "City"), split_id))
    finally:
        conn.close()


def update_amounts(db_path: str, split_id: int, *, total=None, repair=None, tax_sale=None, tax_guild=None, tax_looter=None) -> None:
    sets = {k: v for k, v in dict(total=total, repair=repair, tax_sale=tax_sale, tax_guild=tax_guild, tax_looter=tax_looter).items() if v is not None}
    if not sets:
        return
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            conn.execute(f"UPDATE splits SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), split_id))
    finally:
        conn.close()


def add_people(db_path: str, split_id: int, user_ids: Iterable[int], *, role_id: Optional[int] = None) -> int:
    """Agrega personas a la lista general. Devuelve cuántas eran nuevas."""
    new = 0
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            have = {int(r["user_id"]) for r in conn.execute("SELECT user_id FROM split_people WHERE split_id=?", (split_id,))}
            for uid in dict.fromkeys(int(u) for u in user_ids):
                if uid in have:
                    continue
                if len(have) >= MAX_PEOPLE:
                    raise SplitError(f"Máximo {MAX_PEOPLE} personas en un split.")
                conn.execute("INSERT INTO split_people(split_id, user_id, pos) VALUES (?,?,?)", (split_id, uid, _next_pos(conn, "split_people", split_id)))
                have.add(uid)
                new += 1
            if role_id is not None:
                conn.execute("UPDATE splits SET role_id=? WHERE id=?", (role_id, split_id))
        return new
    finally:
        conn.close()


def remove_people(db_path: str, split_id: int, user_ids: Iterable[int]) -> Tuple[int, List[int]]:
    """Quita personas de la lista. Si eran looters, también salen de los looters. Devuelve (quitadas, looters_quitados)."""
    removed, lost_looters = 0, []
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            for uid in dict.fromkeys(int(u) for u in user_ids):
                removed += conn.execute("DELETE FROM split_people WHERE split_id=? AND user_id=?", (split_id, uid)).rowcount
                if conn.execute("DELETE FROM split_looters WHERE split_id=? AND user_id=?", (split_id, uid)).rowcount:
                    lost_looters.append(uid)
        return removed, lost_looters
    finally:
        conn.close()


def add_looters(db_path: str, split_id: int, user_ids: Iterable[int]) -> int:
    """Agrega looters (y los suma a la lista general). Devuelve cuántos eran nuevos."""
    new = 0
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            have_l = {int(r["user_id"]) for r in conn.execute("SELECT user_id FROM split_looters WHERE split_id=?", (split_id,))}
            have_p = {int(r["user_id"]) for r in conn.execute("SELECT user_id FROM split_people WHERE split_id=?", (split_id,))}
            for uid in dict.fromkeys(int(u) for u in user_ids):
                if uid not in have_l:
                    if len(have_l) >= MAX_LOOTERS:
                        raise SplitError(f"Máximo {MAX_LOOTERS} looters.")
                    conn.execute("INSERT INTO split_looters(split_id, user_id, pos) VALUES (?,?,?)", (split_id, uid, _next_pos(conn, "split_looters", split_id)))
                    have_l.add(uid)
                    new += 1
                if uid not in have_p:
                    if len(have_p) >= MAX_PEOPLE:
                        raise SplitError(f"Máximo {MAX_PEOPLE} personas en un split.")
                    conn.execute("INSERT INTO split_people(split_id, user_id, pos) VALUES (?,?,?)", (split_id, uid, _next_pos(conn, "split_people", split_id)))
                    have_p.add(uid)
        return new
    finally:
        conn.close()


def set_looter(db_path: str, split_id: int, user_id: int, *, looted=None, repair=None, tax_pct=None, reset_tax: bool = False) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            if not conn.execute("SELECT 1 FROM split_looters WHERE split_id=? AND user_id=?", (split_id, user_id)).fetchone():
                raise SplitError("Esa persona no es looter de este split.")
            if looted is not None:
                conn.execute("UPDATE split_looters SET looted=? WHERE split_id=? AND user_id=?", (looted, split_id, user_id))
            if repair is not None:
                conn.execute("UPDATE split_looters SET repair=? WHERE split_id=? AND user_id=?", (repair, split_id, user_id))
            if reset_tax:
                conn.execute("UPDATE split_looters SET tax_pct=NULL WHERE split_id=? AND user_id=?", (split_id, user_id))
            elif tax_pct is not None:
                conn.execute("UPDATE split_looters SET tax_pct=? WHERE split_id=? AND user_id=?", (tax_pct, split_id, user_id))
    finally:
        conn.close()


def remove_looters(db_path: str, split_id: int, user_ids: Iterable[int]) -> int:
    """Quita el apartado de looter (la persona sigue en la lista general)."""
    n = 0
    conn = connect(db_path)
    try:
        with write_tx(conn):
            _require_open(conn, split_id)
            for uid in dict.fromkeys(int(u) for u in user_ids):
                n += conn.execute("DELETE FROM split_looters WHERE split_id=? AND user_id=?", (split_id, uid)).rowcount
        return n
    finally:
        conn.close()


# ---------- cancelar ----------
def cancel_split(db_path: str, split_id: int, actor_id: int) -> str:
    """Cierra el split SIN pagar nada. Devuelve 'cancelled' | 'closed' (ya estaba cerrado) | 'cancelled_before' | 'closing' | 'missing'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT status FROM splits WHERE id=?", (split_id,)).fetchone()
            if r is None:
                return "missing"
            if r["status"] == "closed":
                return "closed"
            if r["status"] == "cancelled":
                return "cancelled_before"
            if r["status"] == "closing":
                return "closing"                      # ya empezó a pagar: no se puede cancelar a mitad
            conn.execute("UPDATE splits SET status='cancelled', closed_at=?, closed_by=? WHERE id=? AND status='open'",
                         (utcnow_iso(), actor_id, split_id))
            conn.execute("UPDATE split_requests SET status='rejected', decided_by=? WHERE split_id=? AND status='pending'", (actor_id, split_id))
            return "cancelled"
    finally:
        conn.close()


# ---------- solicitudes tardías ----------
@dataclass
class Request:
    split_id: int
    user_id: int
    slot_no: Optional[int]
    status: str
    message_id: Optional[int]


def _req(r) -> Optional[Request]:
    return Request(int(r["split_id"]), int(r["user_id"]), r["slot_no"], r["status"], r["message_id"]) if r else None


def add_request(db_path: str, split_id: int, user_id: int, slot_no: Optional[int]) -> Tuple[str, int]:
    """Registra a alguien que se anotó después del cierre. Devuelve (estado, cantidad_total_de_solicitudes):
    'new' · 'listed' (ya está en la lista) · 'pending' / 'accepted' / 'rejected' (ya había pedido antes) · 'closed' (el split ya cerró)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            st = conn.execute("SELECT status FROM splits WHERE id=?", (split_id,)).fetchone()
            if st is None or st["status"] != "open":
                return "closed", 0
            if conn.execute("SELECT 1 FROM split_people WHERE split_id=? AND user_id=?", (split_id, user_id)).fetchone():
                return "listed", 0
            old = conn.execute("SELECT status FROM split_requests WHERE split_id=? AND user_id=?", (split_id, user_id)).fetchone()
            if old is not None:
                return old["status"], 0
            conn.execute("INSERT INTO split_requests(split_id, user_id, slot_no, status, created_at) VALUES (?,?,?, 'pending', ?)",
                         (split_id, user_id, slot_no, utcnow_iso()))
            n = conn.execute("SELECT COUNT(*) AS n FROM split_requests WHERE split_id=?", (split_id,)).fetchone()["n"]
            return "new", int(n)
    finally:
        conn.close()


def set_request_message(db_path: str, split_id: int, user_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE split_requests SET message_id=? WHERE split_id=? AND user_id=?", (message_id, split_id, user_id))
    finally:
        conn.close()


def get_request_by_message(db_path: str, message_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute("SELECT * FROM split_requests WHERE message_id=?", (message_id,)).fetchone())
    finally:
        conn.close()


def decide_request(db_path: str, split_id: int, user_id: int, accept: bool, actor_id: int) -> str:
    """Acepta (la persona entra a la lista) o rechaza. Devuelve 'accepted' | 'rejected' | 'decided' (ya estaba decidida) | 'closed' | 'missing'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT status FROM split_requests WHERE split_id=? AND user_id=?", (split_id, user_id)).fetchone()
            if r is None:
                return "missing"
            if r["status"] != "pending":
                return "decided"
            st = conn.execute("SELECT status FROM splits WHERE id=?", (split_id,)).fetchone()
            if st is None or st["status"] != "open":
                return "closed"
            if accept:
                if not conn.execute("SELECT 1 FROM split_people WHERE split_id=? AND user_id=?", (split_id, user_id)).fetchone():
                    n = conn.execute("SELECT COUNT(*) AS n FROM split_people WHERE split_id=?", (split_id,)).fetchone()["n"]
                    if n >= MAX_PEOPLE:
                        raise SplitError(f"La lista ya tiene el máximo de {MAX_PEOPLE} personas.")
                    conn.execute("INSERT INTO split_people(split_id, user_id, pos) VALUES (?,?,?)", (split_id, user_id, _next_pos(conn, "split_people", split_id)))
            conn.execute("UPDATE split_requests SET status=?, decided_by=? WHERE split_id=? AND user_id=?",
                         ("accepted" if accept else "rejected", actor_id, split_id, user_id))
            return "accepted" if accept else "rejected"
    finally:
        conn.close()


def pending_requests(db_path: str, split_id: int) -> List[int]:
    conn = connect(db_path)
    try:
        return [int(r["user_id"]) for r in conn.execute("SELECT user_id FROM split_requests WHERE split_id=? AND status='pending' ORDER BY created_at", (split_id,))]
    finally:
        conn.close()


# ---------- cierre ----------
def begin_close(db_path: str, split_id: int, actor_id: int) -> Tuple[str, Optional[Split]]:
    """Pasa el split de 'open' a 'closing' (una sola vez). Devuelve (estado, split):
    'started' = lo cerrás vos ahora · 'resume' = quedó a medias antes (se reintenta; los pagos son idempotentes) ·
    'closed' = ya estaba cerrado · 'missing' / 'cancelled'.
    """
    conn = connect(db_path)
    try:
        with write_tx(conn):
            s = _load(conn, split_id)
            if s is None:
                return "missing", None
            if s.status == "closed":
                return "closed", s
            if s.status == "cancelled":
                return "cancelled", s
            if s.status == "closing":
                return "resume", s
            conn.execute("UPDATE splits SET status='closing', closed_by=? WHERE id=? AND status='open'", (actor_id, split_id))
            return "started", _load(conn, split_id)
    finally:
        conn.close()


def finish_close(db_path: str, split_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE splits SET status='closed', closed_at=? WHERE id=? AND status='closing'", (utcnow_iso(), split_id))
    finally:
        conn.close()


def share_key(split_id: int) -> str:
    return f"split:{split_id}:share"


def tax_key(split_id: int, user_id: int) -> str:
    return f"split:{split_id}:tax:{user_id}"


def pay_out(db_path: str, s: Split, actor_id: int) -> Tuple[Calc, Dict[int, int], List[str]]:
    """Acredita los pagos (idempotente por request_key). Devuelve (cálculo, {usuario: total acreditado}, [op_id nuevos])."""
    calc = compute(s)
    paid: Dict[int, int] = {}
    ops: List[str] = []
    if calc.per_person > 0 and s.people:
        res = eco.apply_operation(db_path, kind="add", actor_id=actor_id, user_ids=s.people, amount=calc.per_person,
                                  request_key=share_key(s.id), reason=f"Split {s.title} (#{s.id}): parte por persona")
        if res.created:
            ops.append(res.op_id)
        for u in s.people:
            paid[u] = paid.get(u, 0) + calc.per_person
    for uid, tax in calc.looter_taxes.items():
        if tax > 0:
            res = eco.apply_operation(db_path, kind="add", actor_id=actor_id, user_ids=[uid], amount=tax,
                                      request_key=tax_key(s.id, uid), reason=f"Split {s.title} (#{s.id}): tax looter")
            if res.created:
                ops.append(res.op_id)
            paid[uid] = paid.get(uid, 0) + tax
    return calc, paid, ops
