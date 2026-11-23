"""Servidor de rentas (Albion): datos, sin Discord.

Rentas de HO / territorio / permisos de crafteo por mapa, con cobro semanal los domingos (00:00 UTC).
- Modalidad «adelantado»: al aprobar se cobra el prorrateo del día de inicio al sábado; cada domingo, la semana que empieza.
- Modalidad «vencido»: cada domingo se cobra la semana que terminó (la primera, prorrateada desde el día de inicio).
- Cargos fijos (subir a nivel 3, «lo ponemos nosotros»): se cobran al aprobar.
Estado de cada cargo: pagado / no pagado. Lo impago se acumula como deuda del canal mientras la renta esté activa;
al dar de baja, la deuda queda registrada a nombre del titular y solo se consulta aparte (no entra en las estadísticas).
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from dbutil import connect, utcnow_iso, write_tx

TIPOS = ("ho", "terri", "permiso")
TITULARES = ("personaje", "guild", "alianza")
TITULARES_POR_TIPO = {"ho": ("guild", "alianza"), "terri": ("guild", "alianza"), "permiso": ("personaje", "guild", "alianza")}
MODALIDADES = ("adelantado", "vencido")
CONCEPTOS_PRECIO = ("ho", "terri", "permiso", "nivel3", "ponemos")     # semanales + cargos fijos
IDIOMAS = ("es", "pt", "en")
CALIDADES = ("Q3", "Q4", "Q5", "Q6")
BIOMAS = ("Thetford", "Lymhurst", "Martlock", "Bridgewatch", "Fort Sterling")
# Tarifas únicas para todos los mapas (rent_config), editables con /renta tarifas
TARIFAS_FIJAS = {
    "defensa": "tarifa_defensa",              # precio por fase de defensa del HO (fase 1 y/o 2, opcional)
    "nivel3": "tarifa_nivel3",                # pago único de subir el HO a nivel 3
    "recargo_alianza": "tarifa_n3_alianza",   # HO nivel 3: recargo SEMANAL si la guild tiene alianza
    "recargo_sin": "tarifa_n3_sin",           # HO nivel 3: recargo SEMANAL si la guild no tiene alianza
    "pack_q5": "tarifa_pack_q5",              # pack HO Q5: defensa de las 2 fases + 1 semana gratis
    "pack_q6": "tarifa_pack_q6",              # pack HO Q6: ídem
    "acceso_no_renter": "tarifa_acceso_no",   # acceso a HO: precio SEMANAL para quien no renta con nosotros
    "acceso_renter": "tarifa_acceso_si",      # acceso a HO: precio SEMANAL para quien tiene una renta activa de HO / territorio
}
ACCESO_DEFAULTS = {"acceso_no_renter": 50_000_000, "acceso_renter": 35_000_000}
PACK_CALIDADES = ("Q5", "Q6")
GRATIS_DIAS_PACK = 7
CONCEPTOS_FIJOS = ("nivel3", "ponemos", "defensa_ant", "defensa_resto", "pack_ant", "pack_resto")
CATEGORY_LIMIT = 50                                                    # Discord: máx. 50 canales por categoría

SCHEMA = """
CREATE TABLE IF NOT EXISTS rent_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_acceso_mapas (
    nombre     TEXT PRIMARY KEY COLLATE NOCASE,   -- mapas donde hay HO con acceso a la venta
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_mapas (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    activo  INTEGER NOT NULL DEFAULT 1,
    calidad TEXT,                           -- Q3..Q6
    bioma   TEXT                            -- Thetford / Lymhurst / Martlock / Bridgewatch / Fort Sterling
);
CREATE TABLE IF NOT EXISTS rent_categorias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mapa_id     INTEGER,                         -- NULL = categoría «Archivo»
    idx         INTEGER NOT NULL,                -- 1, 2, 3... (overflow «Mapa (2)»)
    category_id INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS rent_info (
    idioma TEXT PRIMARY KEY,
    texto  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_precios (
    concepto     TEXT NOT NULL,
    mapa_id      INTEGER NOT NULL,
    titular_tipo TEXT NOT NULL,
    monto        INTEGER NOT NULL,
    PRIMARY KEY (concepto, mapa_id, titular_tipo)
);
CREATE TABLE IF NOT EXISTS rent_tickets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id     INTEGER NOT NULL UNIQUE,
    user_id        INTEGER NOT NULL,
    idioma         TEXT NOT NULL,
    tipo           TEXT NOT NULL,               -- ho | terri | permiso
    mapa_id        INTEGER NOT NULL,
    nivel3         INTEGER NOT NULL DEFAULT 0,
    ponemos        INTEGER NOT NULL DEFAULT 0,
    titular_tipo   TEXT NOT NULL,
    titular_nombre TEXT NOT NULL,
    guild_nombre   TEXT,                        -- guild de Albion de quien pidió (para el rol «[TAG] Guild»)
    alianza_tag    TEXT,
    status         TEXT NOT NULL DEFAULT 'open', -- open | approved | rejected
    message_id     INTEGER,
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    closed_by      INTEGER,
    motivo         TEXT
);
CREATE TABLE IF NOT EXISTS rent_canales (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       INTEGER NOT NULL UNIQUE,
    mapa_id          INTEGER NOT NULL,
    titular_tipo     TEXT NOT NULL,
    titular_nombre   TEXT NOT NULL,
    idioma           TEXT NOT NULL,
    panel_message_id INTEGER,
    archivado        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_miembros (
    canal_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (canal_id, user_id)
);
CREATE TABLE IF NOT EXISTS rent_rentas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canal_id       INTEGER NOT NULL,
    ticket_id      INTEGER,
    tipo           TEXT NOT NULL,
    mapa_id        INTEGER NOT NULL,
    titular_tipo   TEXT NOT NULL,
    titular_nombre TEXT NOT NULL,
    precio         INTEGER NOT NULL,
    modalidad      TEXT NOT NULL,
    fecha_inicio   TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    activa         INTEGER NOT NULL DEFAULT 1,
    fecha_baja     TEXT,
    created_by     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_cargos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    renta_id   INTEGER NOT NULL,
    concepto   TEXT NOT NULL,                   -- prorrateo | semana | nivel3 | ponemos | baja | extra | ajuste (pagó otro monto)
    periodo    TEXT NOT NULL,                   -- semana cobrada (domingo) o fecha del cargo
    lote       TEXT,                            -- domingo del cobro semanal que lo generó (NULL = cargo al aprobar/baja)
    monto      INTEGER NOT NULL,
    pagado     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paid_at    TEXT,
    paid_by    INTEGER,
    UNIQUE (renta_id, concepto, periodo)
);
CREATE TABLE IF NOT EXISTS rent_guild_roles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_nombre TEXT NOT NULL UNIQUE COLLATE NOCASE,
    tag          TEXT,                          -- tag de la alianza (hasta 5), NULL = sin alianza
    role_id      INTEGER NOT NULL,
    color        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_usuarios (
    user_id       INTEGER PRIMARY KEY,
    guild_role_id INTEGER NOT NULL              -- rent_guild_roles.id
);
CREATE TABLE IF NOT EXISTS rent_jobs (
    key     TEXT PRIMARY KEY,
    done_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rent_rentas_canal ON rent_rentas(canal_id, activa);
CREATE INDEX IF NOT EXISTS idx_rent_cargos_renta ON rent_cargos(renta_id, pagado);
"""


class RentaError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


_MIGRATIONS = {
    "rent_mapas": (("calidad", "TEXT"), ("bioma", "TEXT")),
    "rent_tickets": (("defensas", "INTEGER NOT NULL DEFAULT 0"), ("pack", "INTEGER NOT NULL DEFAULT 0"),
                     ("con_terri", "INTEGER NOT NULL DEFAULT 0")),           # HO + territorio del mapa
    "rent_rentas": (("defensa_total", "INTEGER NOT NULL DEFAULT 0"), ("pack_total", "INTEGER NOT NULL DEFAULT 0"),
                    ("gratis_hasta", "TEXT"),           # pack: días gratis hasta esta fecha (exclusive)
                    ("pago_ok", "INTEGER NOT NULL DEFAULT 0"),      # etapa: el Council confirmó el pago inicial
                    ("colocado", "INTEGER NOT NULL DEFAULT 1")),    # etapa: HO colocado (la renta corre desde ahí); 0 = esperando
    "rent_cargos": (("detalle", "TEXT"),),              # extra: motivo que escribió el Council
}


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for table, cols in _MIGRATIONS.items():               # bases creadas antes de agregar estas columnas
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    finally:
        conn.close()


# ============================================================
# Montos (silver) y fechas
# ============================================================
_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_silver(text: str) -> int:
    """'1.5M' / '750k' / '1500000' / '1.500.000' / '1,500,000' -> int. Error si no es un número positivo."""
    s = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([kmb])", s)
    if m:
        value = float(m.group(1).replace(",", ".")) * _SUFFIX[m.group(2)]
    elif re.fullmatch(r"\d+(?:[.,]\d{3})*", s):
        value = int(re.sub(r"[.,]", "", s))
    else:
        raise RentaError(f"«{text}» no es un monto válido. Ej: 1.5M, 750K o 1500000.")
    value = int(round(value))
    if value < 0:
        raise RentaError("El monto no puede ser negativo.")
    return value


def fmt_silver(n: int) -> str:
    """1500000 -> '1.5M' · 750000 -> '750K' · 1250000 -> '1.25M' · 999 -> '999'."""
    n = int(n)
    sign = "-" if n < 0 else ""
    a = abs(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if a >= div:
            txt = f"{a / div:.2f}".rstrip("0").rstrip(".")
            return f"{sign}{txt}{suf}"
    return f"{sign}{a}"


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def week_start(d: date) -> date:
    """Domingo de la semana de `d` (la semana va de domingo a sábado)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def days_to_saturday(d: date) -> int:
    """Días desde `d` (inclusive) hasta el sábado (inclusive). Miércoles = 4, sábado = 1, domingo = 7."""
    return (5 - d.weekday()) % 7 + 1


def prorate(precio: int, days: int) -> int:
    return int(round(precio * max(0, min(days, 7)) / 7))


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _sunday_iso_start(sunday: date) -> str:
    return datetime(sunday.year, sunday.month, sunday.day, tzinfo=timezone.utc).isoformat()


# ============================================================
# Config / info
# ============================================================
def get_config(db_path: str, key: str) -> Optional[str]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT value FROM rent_config WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_config(db_path: str, key: str, value) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO rent_config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, str(value)))
    finally:
        conn.close()


def get_info(db_path: str, idioma: str) -> Optional[str]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT texto FROM rent_info WHERE idioma = ?", (idioma,)).fetchone()
        return r["texto"] if r else None
    finally:
        conn.close()


def set_info(db_path: str, idioma: str, texto: str) -> None:
    if idioma not in IDIOMAS:
        raise RentaError("Idioma inválido.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO rent_info(idioma, texto) VALUES(?, ?) ON CONFLICT(idioma) DO UPDATE SET texto = excluded.texto",
                         (idioma, texto))
    finally:
        conn.close()


# ============================================================
# Mapas y categorías
# ============================================================
@dataclass
class Mapa:
    id: int
    nombre: str
    calidad: Optional[str] = None
    bioma: Optional[str] = None

    @property
    def detalle(self) -> str:
        return " · ".join(x for x in (self.calidad, self.bioma) if x)


def _mapa(r) -> Optional[Mapa]:
    return Mapa(int(r["id"]), r["nombre"], r["calidad"], r["bioma"]) if r else None


def list_mapas(db_path: str) -> List[Mapa]:
    conn = connect(db_path)
    try:
        return [_mapa(r) for r in conn.execute("SELECT * FROM rent_mapas WHERE activo = 1 ORDER BY nombre COLLATE NOCASE")]
    finally:
        conn.close()


def get_mapa(db_path: str, mapa_id: int) -> Optional[Mapa]:
    conn = connect(db_path)
    try:
        return _mapa(conn.execute("SELECT * FROM rent_mapas WHERE id = ?", (mapa_id,)).fetchone())
    finally:
        conn.close()


def find_mapa(db_path: str, nombre: str) -> Optional[Mapa]:
    conn = connect(db_path)
    try:
        return _mapa(conn.execute("SELECT * FROM rent_mapas WHERE activo = 1 AND nombre = ? COLLATE NOCASE",
                                  ((nombre or "").strip(),)).fetchone())
    finally:
        conn.close()


def _check_calidad_bioma(calidad: Optional[str], bioma: Optional[str]) -> None:
    if calidad is not None and calidad not in CALIDADES:
        raise RentaError(f"Calidad inválida: {calidad} (Q3, Q4, Q5 o Q6).")
    if bioma is not None and bioma not in BIOMAS:
        raise RentaError(f"Bioma inválido: {bioma}.")


def add_mapa(db_path: str, nombre: str, calidad: Optional[str] = None, bioma: Optional[str] = None) -> Mapa:
    _check_calidad_bioma(calidad, bioma)
    nombre = (nombre or "").strip()
    if not nombre or len(nombre) > 90:
        raise RentaError("Nombre de mapa inválido (1 a 90 caracteres).")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT id, activo FROM rent_mapas WHERE nombre = ? COLLATE NOCASE", (nombre,)).fetchone()
            if r is not None:
                if int(r["activo"]):
                    raise RentaError(f"El mapa «{nombre}» ya está en la lista.")
                conn.execute("UPDATE rent_mapas SET activo = 1, nombre = ?, calidad = ?, bioma = ? WHERE id = ?",
                             (nombre, calidad, bioma, r["id"]))
                return Mapa(int(r["id"]), nombre, calidad, bioma)
            cur = conn.execute("INSERT INTO rent_mapas(nombre, calidad, bioma) VALUES(?, ?, ?)", (nombre, calidad, bioma))
            return Mapa(int(cur.lastrowid), nombre, calidad, bioma)
    finally:
        conn.close()


def count_active_rentas_mapa(db_path: str, mapa_id: int) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM rent_rentas WHERE mapa_id = ? AND activa = 1", (mapa_id,)).fetchone()[0])
    finally:
        conn.close()


def remove_mapa(db_path: str, nombre: str) -> Mapa:
    m = find_mapa(db_path, nombre)
    if m is None:
        raise RentaError(f"No existe el mapa «{nombre}».")
    n = count_active_rentas_mapa(db_path, m.id)
    if n:
        raise RentaError(f"El mapa «{m.nombre}» tiene {n} renta(s) activa(s): no se puede quitar. Podés renombrarlo con /renta mapa editar.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_mapas SET activo = 0 WHERE id = ?", (m.id,))
    finally:
        conn.close()
    return m


def set_mapa_detalle(db_path: str, mapa_id: int, calidad: Optional[str] = None, bioma: Optional[str] = None) -> None:
    """Cambia calidad y/o bioma (None = no se toca)."""
    _check_calidad_bioma(calidad, bioma)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if calidad is not None:
                conn.execute("UPDATE rent_mapas SET calidad = ? WHERE id = ?", (calidad, mapa_id))
            if bioma is not None:
                conn.execute("UPDATE rent_mapas SET bioma = ? WHERE id = ?", (bioma, mapa_id))
    finally:
        conn.close()


def get_tarifa_fija(db_path: str, concepto: str) -> Optional[int]:
    v = get_config(db_path, TARIFAS_FIJAS[concepto])
    return int(v) if v not in (None, "") else None


def set_tarifa_fija(db_path: str, concepto: str, monto: int) -> None:
    set_config(db_path, TARIFAS_FIJAS[concepto], int(monto))


def lista_tarifas(db_path: str) -> List[tuple]:
    """[(Mapa, precio semanal de HO para guild o None)] de los mapas activos: por calidad (Q6 primero, sin calidad al final),
    después bioma y nombre."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT m.*, p.monto AS precio FROM rent_mapas m LEFT JOIN rent_precios p ON p.mapa_id = m.id "
                            "AND p.concepto = 'ho' AND p.titular_tipo = 'guild' WHERE m.activo = 1").fetchall()
    finally:
        conn.close()
    out = [(_mapa(r), int(r["precio"]) if r["precio"] is not None else None) for r in rows]
    border = {b: i for i, b in enumerate(BIOMAS)}
    qrank = lambda q: -int(q[1:]) if q and q[1:].isdigit() else 99
    out.sort(key=lambda x: (qrank(x[0].calidad), border.get(x[0].bioma, 99), x[0].nombre.lower()))
    return out


# ============================================================
# Acceso a HO (solo informativo: lista de mapas y precio semanal renter / no renter)
# ============================================================
def acceso_precios(db_path: str) -> tuple:
    """(precio no renter, precio renter), con 50M / 35M si no se cambiaron."""
    return tuple(get_tarifa_fija(db_path, k) or ACCESO_DEFAULTS[k] for k in ("acceso_no_renter", "acceso_renter"))


def acceso_mapas(db_path: str) -> List[str]:
    conn = connect(db_path)
    try:
        return [r["nombre"] for r in conn.execute("SELECT nombre FROM rent_acceso_mapas ORDER BY nombre COLLATE NOCASE")]
    finally:
        conn.close()


def add_acceso_mapa(db_path: str, nombre: str) -> str:
    nombre = " ".join((nombre or "").split())
    if not nombre or len(nombre) > 100:
        raise RentaError("Nombre de mapa inválido.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM rent_acceso_mapas WHERE nombre = ?", (nombre,)).fetchone():
                raise RentaError(f"«{nombre}» ya está en la lista de acceso a HO.")
            conn.execute("INSERT INTO rent_acceso_mapas(nombre, created_at) VALUES(?, ?)", (nombre, utcnow_iso()))
        return nombre
    finally:
        conn.close()


def remove_acceso_mapa(db_path: str, nombre: str) -> str:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT nombre FROM rent_acceso_mapas WHERE nombre = ?", (" ".join((nombre or "").split()),)).fetchone()
            if r is None:
                raise RentaError(f"«{nombre}» no está en la lista de acceso a HO.")
            conn.execute("DELETE FROM rent_acceso_mapas WHERE nombre = ?", (r["nombre"],))
            return r["nombre"]
    finally:
        conn.close()


def is_renter(db_path: str, user_id: int) -> bool:
    """Renter = está en el canal de un cliente (no archivado) con una renta activa de HO o territorio."""
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM rent_miembros m JOIN rent_canales c ON c.id = m.canal_id AND c.archivado = 0 "
                            "JOIN rent_rentas r ON r.canal_id = c.id AND r.activa = 1 AND r.tipo IN ('ho', 'terri') "
                            "WHERE m.user_id = ? LIMIT 1", (user_id,)).fetchone() is not None
    finally:
        conn.close()


def rename_mapa(db_path: str, actual: str, nuevo: str) -> Mapa:
    m = find_mapa(db_path, actual)
    if m is None:
        raise RentaError(f"No existe el mapa «{actual}».")
    nuevo = (nuevo or "").strip()
    if not nuevo or len(nuevo) > 90:
        raise RentaError("Nombre nuevo inválido (1 a 90 caracteres).")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            other = conn.execute("SELECT id FROM rent_mapas WHERE nombre = ? COLLATE NOCASE AND id <> ?", (nuevo, m.id)).fetchone()
            if other is not None:
                raise RentaError(f"Ya existe otro mapa llamado «{nuevo}».")
            conn.execute("UPDATE rent_mapas SET nombre = ? WHERE id = ?", (nuevo, m.id))
    finally:
        conn.close()
    return get_mapa(db_path, m.id)


def categorias(db_path: str, mapa_id: Optional[int]) -> List[tuple]:
    """[(idx, category_id), ...] ordenadas. mapa_id None = Archivo."""
    conn = connect(db_path)
    try:
        if mapa_id is None:
            rows = conn.execute("SELECT idx, category_id FROM rent_categorias WHERE mapa_id IS NULL ORDER BY idx").fetchall()
        else:
            rows = conn.execute("SELECT idx, category_id FROM rent_categorias WHERE mapa_id = ? ORDER BY idx", (mapa_id,)).fetchall()
        return [(int(r["idx"]), int(r["category_id"])) for r in rows]
    finally:
        conn.close()


def add_categoria(db_path: str, mapa_id: Optional[int], idx: int, category_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO rent_categorias(mapa_id, idx, category_id) VALUES(?, ?, ?)", (mapa_id, idx, category_id))
    finally:
        conn.close()


def forget_categoria(db_path: str, category_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM rent_categorias WHERE category_id = ?", (category_id,))
    finally:
        conn.close()


def category_name(base: str, idx: int) -> str:
    return base if idx <= 1 else f"{base} ({idx})"


# ============================================================
# Precios
# ============================================================
def set_precio(db_path: str, concepto: str, mapa_id: int, titular_tipo: str, monto: int) -> None:
    if concepto not in CONCEPTOS_PRECIO or titular_tipo not in TITULARES:
        raise RentaError("Concepto o titular inválido.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO rent_precios(concepto, mapa_id, titular_tipo, monto) VALUES(?, ?, ?, ?) "
                         "ON CONFLICT(concepto, mapa_id, titular_tipo) DO UPDATE SET monto = excluded.monto",
                         (concepto, mapa_id, titular_tipo, int(monto)))
    finally:
        conn.close()


def get_precio(db_path: str, concepto: str, mapa_id: int, titular_tipo: str) -> Optional[int]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT monto FROM rent_precios WHERE concepto = ? AND mapa_id = ? AND titular_tipo = ?",
                         (concepto, mapa_id, titular_tipo)).fetchone()
        return int(r["monto"]) if r else None
    finally:
        conn.close()


def terri_precio(db_path: str, mapa_id: int, titular_tipo: str = "guild") -> int:
    """Precio semanal del territorio del mapa (el del titular o, si no hay, el de guild). 0 = ese territorio no se alquila."""
    p = get_precio(db_path, "terri", mapa_id, titular_tipo)
    if p is None:
        p = get_precio(db_path, "terri", mapa_id, "guild")
    return int(p or 0)


def mapas_con_terri(db_path: str) -> List[int]:
    """Ids de los mapas activos cuyo territorio se alquila (precio de terri > 0)."""
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute(
            "SELECT DISTINCT m.id FROM rent_mapas m JOIN rent_precios p ON p.mapa_id = m.id AND p.concepto = 'terri' "
            "WHERE m.activo = 1 AND p.monto > 0")]
    finally:
        conn.close()


def list_precios(db_path: str) -> List[tuple]:
    """[(mapa, concepto, titular_tipo, monto)] de los mapas activos."""
    conn = connect(db_path)
    try:
        return [(r["nombre"], r["concepto"], r["titular_tipo"], int(r["monto"])) for r in conn.execute(
            "SELECT m.nombre, p.concepto, p.titular_tipo, p.monto FROM rent_precios p JOIN rent_mapas m ON m.id = p.mapa_id "
            "WHERE m.activo = 1 ORDER BY m.nombre COLLATE NOCASE, p.concepto, p.titular_tipo")]
    finally:
        conn.close()


# ============================================================
# Tickets
# ============================================================
@dataclass
class Ticket:
    id: int
    channel_id: int
    user_id: int
    idioma: str
    tipo: str
    mapa_id: int
    nivel3: bool
    ponemos: bool
    titular_tipo: str
    titular_nombre: str
    status: str
    message_id: Optional[int]
    guild_nombre: Optional[str] = None
    alianza_tag: Optional[str] = None
    defensas: int = 0                 # 0, 1 o 2 fases de defensa
    pack: bool = False                # pack Q5/Q6: defensa de las 2 fases + 1 semana gratis
    con_terri: bool = False           # HO: también renta el territorio del mapa


def _ticket(r) -> Optional[Ticket]:
    if r is None:
        return None
    return Ticket(int(r["id"]), int(r["channel_id"]), int(r["user_id"]), r["idioma"], r["tipo"], int(r["mapa_id"]),
                  bool(r["nivel3"]), bool(r["ponemos"]), r["titular_tipo"], r["titular_nombre"], r["status"],
                  int(r["message_id"]) if r["message_id"] is not None else None, r["guild_nombre"], r["alianza_tag"],
                  int(r["defensas"] or 0), bool(r["pack"]), bool(r["con_terri"]) if "con_terri" in r.keys() else False)


TAG_MAX = 5


def parse_guild_label(text: str) -> tuple:
    """'[Timba] Smogg' -> ('Timba', 'Smogg') · 'Smogg' -> (None, 'Smogg') · '[Timba]' -> ('Timba', None) · '' -> (None, None)."""
    s = " ".join((text or "").split())
    m = re.fullmatch(r"(?:\[([^\[\]]*)\])?\s*(.*)", s)
    tag = (m.group(1) or "").strip() or None
    guild = (m.group(2) or "").strip() or None
    if tag is not None and len(tag) > TAG_MAX:
        raise RentaError(f"El tag de alianza tiene que tener hasta {TAG_MAX} caracteres (ej: [Timba] Smogg).")
    if guild is not None and ("[" in guild or "]" in guild or len(guild) > 60):
        raise RentaError("Formato inválido. Ej: [Timba] Smogg, o solo Smogg si la guild no tiene alianza.")
    return tag, guild


def guild_role_name(guild: str, tag: Optional[str]) -> str:
    return (f"[{tag}] {guild}" if tag else guild)[:100]


def validate_request(tipo: str, titular_tipo: str, titular_nombre: str) -> str:
    if tipo not in TIPOS:
        raise RentaError("Tipo de renta inválido.")
    if titular_tipo not in TITULARES_POR_TIPO[tipo]:
        raise RentaError("Tipo de titular inválido para esta renta.")
    nombre = " ".join((titular_nombre or "").split())
    if not nombre or len(nombre) > 60:
        raise RentaError("Nombre inválido (1 a 60 caracteres).")
    return nombre


def create_ticket(db_path: str, channel_id: int, user_id: int, idioma: str, tipo: str, mapa_id: int, nivel3: bool, ponemos: bool,
                  titular_tipo: str, titular_nombre: str, guild_nombre: Optional[str] = None, alianza_tag: Optional[str] = None,
                  defensas: int = 0, pack: bool = False, con_terri: bool = False) -> int:
    nombre = validate_request(tipo, titular_tipo, titular_nombre)
    if tipo != "ho":
        nivel3 = ponemos = pack = con_terri = False
        defensas = 0
    if pack:
        defensas = 0                                   # el pack ya incluye la defensa de las 2 fases
    defensas = max(0, min(2, int(defensas)))
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "INSERT INTO rent_tickets(channel_id, user_id, idioma, tipo, mapa_id, nivel3, ponemos, titular_tipo, titular_nombre, "
                "guild_nombre, alianza_tag, defensas, pack, con_terri, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (channel_id, user_id, idioma, tipo, mapa_id, int(bool(nivel3)), int(bool(ponemos)), titular_tipo, nombre,
                 guild_nombre, alianza_tag, defensas, int(bool(pack)), int(bool(con_terri)), utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def set_ticket_message(db_path: str, ticket_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_tickets SET message_id = ? WHERE id = ?", (message_id, ticket_id))
    finally:
        conn.close()


def open_ticket_of(db_path: str, user_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute("SELECT * FROM rent_tickets WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                                    (user_id,)).fetchone())
    finally:
        conn.close()


def ticket_by_channel(db_path: str, channel_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute("SELECT * FROM rent_tickets WHERE channel_id = ?", (channel_id,)).fetchone())
    finally:
        conn.close()


def close_ticket(db_path: str, ticket_id: int, status: str, actor_id: int, motivo: Optional[str] = None) -> bool:
    """Cierra el ticket si seguía abierto (idempotente: el segundo clic devuelve False)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE rent_tickets SET status = ?, closed_at = ?, closed_by = ?, motivo = ? WHERE id = ? AND status = 'open'",
                               (status, utcnow_iso(), actor_id, motivo, ticket_id))
            return cur.rowcount == 1
    finally:
        conn.close()


def reopen_ticket(db_path: str, ticket_id: int) -> None:
    """Si falló la parte de Discord al aprobar, el ticket vuelve a quedar abierto para reintentar."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_tickets SET status = 'open', closed_at = NULL, closed_by = NULL WHERE id = ?", (ticket_id,))
    finally:
        conn.close()


# ============================================================
# Canales de clientes
# ============================================================
@dataclass
class Canal:
    id: int
    channel_id: int
    mapa_id: int
    titular_tipo: str
    titular_nombre: str
    idioma: str
    panel_message_id: Optional[int]
    archivado: bool


def _canal(r) -> Optional[Canal]:
    if r is None:
        return None
    return Canal(int(r["id"]), int(r["channel_id"]), int(r["mapa_id"]), r["titular_tipo"], r["titular_nombre"], r["idioma"],
                 int(r["panel_message_id"]) if r["panel_message_id"] is not None else None, bool(r["archivado"]))


def find_canal(db_path: str, mapa_id: int, titular_nombre: str) -> Optional[Canal]:
    """Canal activo (no archivado) del titular en ese mapa: un canal por titular por mapa."""
    conn = connect(db_path)
    try:
        return _canal(conn.execute("SELECT * FROM rent_canales WHERE mapa_id = ? AND titular_nombre = ? COLLATE NOCASE AND archivado = 0",
                                   (mapa_id, titular_nombre)).fetchone())
    finally:
        conn.close()


def canal_by_channel(db_path: str, channel_id: int) -> Optional[Canal]:
    conn = connect(db_path)
    try:
        return _canal(conn.execute("SELECT * FROM rent_canales WHERE channel_id = ?", (channel_id,)).fetchone())
    finally:
        conn.close()


def get_canal(db_path: str, canal_id: int) -> Optional[Canal]:
    conn = connect(db_path)
    try:
        return _canal(conn.execute("SELECT * FROM rent_canales WHERE id = ?", (canal_id,)).fetchone())
    finally:
        conn.close()


def create_canal(db_path: str, channel_id: int, mapa_id: int, titular_tipo: str, titular_nombre: str, idioma: str) -> Canal:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO rent_canales(channel_id, mapa_id, titular_tipo, titular_nombre, idioma, created_at) "
                               "VALUES(?, ?, ?, ?, ?, ?)", (channel_id, mapa_id, titular_tipo, titular_nombre, idioma, utcnow_iso()))
            cid = int(cur.lastrowid)
    finally:
        conn.close()
    return get_canal(db_path, cid)


def set_canal_panel(db_path: str, canal_id: int, message_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_canales SET panel_message_id = ? WHERE id = ?", (message_id, canal_id))
    finally:
        conn.close()


def set_canal_archivado(db_path: str, canal_id: int, archivado: bool) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_canales SET archivado = ? WHERE id = ?", (int(archivado), canal_id))
    finally:
        conn.close()


def active_canales(db_path: str) -> List[Canal]:
    conn = connect(db_path)
    try:
        return [_canal(r) for r in conn.execute(
            "SELECT * FROM rent_canales c WHERE archivado = 0 AND EXISTS "
            "(SELECT 1 FROM rent_rentas r WHERE r.canal_id = c.id AND r.activa = 1) ORDER BY id")]
    finally:
        conn.close()


def add_miembro(db_path: str, canal_id: int, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR IGNORE INTO rent_miembros(canal_id, user_id) VALUES(?, ?)", (canal_id, user_id))
    finally:
        conn.close()


def miembros(db_path: str, canal_id: int) -> List[int]:
    conn = connect(db_path)
    try:
        return [int(r["user_id"]) for r in conn.execute("SELECT user_id FROM rent_miembros WHERE canal_id = ? ORDER BY rowid", (canal_id,))]
    finally:
        conn.close()


# ============================================================
# Rentas y cargos
# ============================================================
@dataclass
class Renta:
    id: int
    canal_id: int
    tipo: str
    mapa_id: int
    titular_tipo: str
    titular_nombre: str
    precio: int
    modalidad: str
    fecha_inicio: date
    activa: bool
    fecha_baja: Optional[date]
    defensa_total: int = 0
    pack_total: int = 0
    gratis_hasta: Optional[date] = None
    pago_ok: bool = False
    colocado: bool = True


def _renta(r) -> Optional[Renta]:
    if r is None:
        return None
    return Renta(int(r["id"]), int(r["canal_id"]), r["tipo"], int(r["mapa_id"]), r["titular_tipo"], r["titular_nombre"], int(r["precio"]),
                 r["modalidad"], _d(r["fecha_inicio"]), bool(r["activa"]), _d(r["fecha_baja"]) if r["fecha_baja"] else None,
                 int(r["defensa_total"] or 0), int(r["pack_total"] or 0), _d(r["gratis_hasta"]) if r["gratis_hasta"] else None,
                 bool(r["pago_ok"]), bool(r["colocado"]))


def billable_days(r: Renta, start: date, end: date) -> int:
    """Días cobrables entre start y end (inclusive): desde el inicio de la renta y fuera de los días gratis del pack."""
    s = max(start, r.fecha_inicio, r.gratis_hasta or start)
    return max(0, (end - s).days + 1)


def half(total: int) -> tuple:
    """(anticipo, resto): la mitad se paga por adelantado."""
    ant = int(total) // 2
    return ant, int(total) - ant


@dataclass
class Cargo:
    id: int
    renta_id: int
    concepto: str
    periodo: str
    lote: Optional[str]
    monto: int
    pagado: bool
    detalle: Optional[str] = None


def _cargo(r) -> Cargo:
    return Cargo(int(r["id"]), int(r["renta_id"]), r["concepto"], r["periodo"], r["lote"], int(r["monto"]), bool(r["pagado"]),
                 r["detalle"] if "detalle" in r.keys() else None)


def _insert_cargo(conn, renta_id: int, concepto: str, periodo: str, monto: int, lote: Optional[str] = None) -> bool:
    if monto <= 0:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO rent_cargos(renta_id, concepto, periodo, lote, monto, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                       (renta_id, concepto, periodo, lote, int(monto), utcnow_iso()))
    return cur.rowcount == 1


def create_renta(db_path: str, *, canal_id: int, ticket: Ticket, precio: int, modalidad: str, inicio: date, actor_id: int,
                 nivel3_monto: int = 0, ponemos_monto: int = 0, defensa_monto: int = 0, pack_monto: int = 0) -> int:
    """Alta de la renta + cargos iniciales (prorrateo si es adelantado, nivel 3, y el 50% por adelantado de defensa / pack).
    HO: queda «esperando colocación»: la renta semanal (y el prorrateo, los días gratis del pack y el 50% restante) arranca con colocar().
    Resto de tipos: corre desde `inicio`. Devuelve el id de la renta."""
    if modalidad not in MODALIDADES:
        raise RentaError("Modalidad inválida.")
    if precio <= 0:
        raise RentaError("El precio semanal tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            is_ho = ticket.tipo == "ho"
            def_total = int(defensa_monto) if is_ho and ticket.defensas and not ticket.pack else 0
            pack_total = int(pack_monto) if is_ho and ticket.pack else 0
            cur = conn.execute(
                "INSERT INTO rent_rentas(canal_id, ticket_id, tipo, mapa_id, titular_tipo, titular_nombre, precio, modalidad, fecha_inicio, "
                "created_by, created_at, defensa_total, pack_total, colocado) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (canal_id, ticket.id, ticket.tipo, ticket.mapa_id, ticket.titular_tipo, ticket.titular_nombre, int(precio), modalidad,
                 inicio.isoformat(), actor_id, utcnow_iso(), def_total, pack_total, 0 if is_ho else 1))
            rid = int(cur.lastrowid)
            if not is_ho:
                _start_rent(conn, rid, inicio)
            if def_total:
                _insert_cargo(conn, rid, "defensa_ant", inicio.isoformat(), half(def_total)[0])
            if pack_total:
                _insert_cargo(conn, rid, "pack_ant", inicio.isoformat(), half(pack_total)[0])
            if ticket.tipo == "ho" and ticket.nivel3:
                _insert_cargo(conn, rid, "nivel3", inicio.isoformat(), nivel3_monto)
            if ticket.tipo == "ho" and ticket.ponemos:
                _insert_cargo(conn, rid, "ponemos", inicio.isoformat(), ponemos_monto)
            return rid
    finally:
        conn.close()


def _start_rent(conn, renta_id: int, inicio: date) -> List[int]:
    """La renta empieza a correr: fecha de inicio, días gratis del pack, prorrateo (adelantado) y 50% restante. Devuelve ids de cargos."""
    r = _renta(conn.execute("SELECT * FROM rent_rentas WHERE id = ?", (renta_id,)).fetchone())
    gratis = (inicio + timedelta(days=GRATIS_DIAS_PACK)).isoformat() if r.pack_total else None
    conn.execute("UPDATE rent_rentas SET fecha_inicio = ?, gratis_hasta = ?, colocado = 1 WHERE id = ?", (inicio.isoformat(), gratis, renta_id))
    r = _renta(conn.execute("SELECT * FROM rent_rentas WHERE id = ?", (renta_id,)).fetchone())
    ids = []

    def add(concepto, monto):                  # mismo periodo que cargar_resto: nunca se carga dos veces
        if _insert_cargo(conn, renta_id, concepto, inicio.isoformat(), monto):
            ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    if r.modalidad == "adelantado":
        end = inicio + timedelta(days=days_to_saturday(inicio) - 1)
        add("prorrateo", prorate(r.precio, billable_days(r, inicio, end)))
    if r.tipo == "ho":
        if r.defensa_total:
            add("defensa_resto", half(r.defensa_total)[1])
        if r.pack_total:
            add("pack_resto", half(r.pack_total)[1])
    return ids


def colocar(db_path: str, renta_id: int, hoy: date) -> Optional[List["Cargo"]]:
    """Etapa «HO colocado»: la renta empieza a correr hoy y se carga el 50% restante de defensa / pack.
    Devuelve los cargos creados, o None si ya estaba colocado / dado de baja."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT colocado, activa FROM rent_rentas WHERE id = ?", (renta_id,)).fetchone()
            if r is None or int(r["colocado"]) or not int(r["activa"]):
                return None
            ids = _start_rent(conn, renta_id, hoy)
            return [_cargo(x) for x in conn.execute(f"SELECT * FROM rent_cargos WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    finally:
        conn.close()


def confirmar_pago(db_path: str, renta_id: int, actor_id: int) -> Optional[int]:
    """Etapa «Pago confirmado»: marca pagado lo pendiente de esta renta. Devuelve el total marcado, o None si ya estaba confirmado."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE rent_rentas SET pago_ok = 1 WHERE id = ? AND pago_ok = 0", (renta_id,))
            if cur.rowcount != 1:
                return None
            total = int(conn.execute("SELECT COALESCE(SUM(monto), 0) FROM rent_cargos WHERE renta_id = ? AND pagado = 0",
                                     (renta_id,)).fetchone()[0])
            conn.execute("UPDATE rent_cargos SET pagado = 1, paid_at = ?, paid_by = ? WHERE renta_id = ? AND pagado = 0",
                         (utcnow_iso(), actor_id, renta_id))
            return total
    finally:
        conn.close()


def renta_by_ticket(db_path: str, ticket_id: int) -> Optional[Renta]:
    conn = connect(db_path)
    try:
        return _renta(conn.execute("SELECT r.* FROM rent_rentas r JOIN rent_tickets t ON t.id = r.ticket_id "      # la del tipo del ticket
                                   "WHERE r.ticket_id = ? AND r.tipo = t.tipo ORDER BY r.id DESC LIMIT 1", (ticket_id,)).fetchone())
    finally:
        conn.close()


def ticket_by_message(db_path: str, message_id: int) -> Optional[Ticket]:
    conn = connect(db_path)
    try:
        return _ticket(conn.execute("SELECT * FROM rent_tickets WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def get_renta(db_path: str, renta_id: int) -> Optional[Renta]:
    conn = connect(db_path)
    try:
        return _renta(conn.execute("SELECT * FROM rent_rentas WHERE id = ?", (renta_id,)).fetchone())
    finally:
        conn.close()


def rentas_canal(db_path: str, canal_id: int, only_active: bool = True) -> List[Renta]:
    conn = connect(db_path)
    try:
        q = "SELECT * FROM rent_rentas WHERE canal_id = ?" + (" AND activa = 1" if only_active else "") + " ORDER BY id"
        return [_renta(r) for r in conn.execute(q, (canal_id,))]
    finally:
        conn.close()


def cargos_renta(db_path: str, renta_id: int) -> List[Cargo]:
    conn = connect(db_path)
    try:
        return [_cargo(r) for r in conn.execute("SELECT * FROM rent_cargos WHERE renta_id = ? ORDER BY id", (renta_id,))]
    finally:
        conn.close()


def deuda_canal(db_path: str, canal_id: int) -> int:
    """Suma de los cargos impagos de las rentas ACTIVAS del canal."""
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COALESCE(SUM(c.monto), 0) FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id "
                                "WHERE r.canal_id = ? AND r.activa = 1 AND c.pagado = 0", (canal_id,)).fetchone()[0])
    finally:
        conn.close()


def cargos_pendientes_canal(db_path: str, canal_id: int) -> List[Cargo]:
    conn = connect(db_path)
    try:
        return [_cargo(r) for r in conn.execute(
            "SELECT c.* FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id WHERE r.canal_id = ? AND r.activa = 1 AND c.pagado = 0 "
            "ORDER BY c.id", (canal_id,))]
    finally:
        conn.close()


def marcar_pagado_canal(db_path: str, canal_id: int, actor_id: int) -> int:
    """Marca pagados todos los cargos impagos de las rentas activas del canal. Devuelve el total marcado (0 = no había nada)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            total = int(conn.execute("SELECT COALESCE(SUM(c.monto), 0) FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id "
                                     "WHERE r.canal_id = ? AND r.activa = 1 AND c.pagado = 0", (canal_id,)).fetchone()[0])
            if total:
                conn.execute("UPDATE rent_cargos SET pagado = 1, paid_at = ?, paid_by = ? WHERE pagado = 0 AND renta_id IN "
                             "(SELECT id FROM rent_rentas WHERE canal_id = ? AND activa = 1)", (utcnow_iso(), actor_id, canal_id))
                conn.execute("UPDATE rent_rentas SET pago_ok = 1 WHERE canal_id = ? AND activa = 1", (canal_id,))
            return total
    finally:
        conn.close()


def set_precio_renta(db_path: str, renta_id: int, precio: int) -> Optional[int]:
    """Cambia el precio semanal de una renta activa (rige desde el próximo cobro). Devuelve el precio anterior, o None si no está activa."""
    if precio <= 0:
        raise RentaError("El precio semanal tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT precio FROM rent_rentas WHERE id = ? AND activa = 1", (renta_id,)).fetchone()
            if r is None:
                return None
            conn.execute("UPDATE rent_rentas SET precio = ? WHERE id = ?", (int(precio), renta_id))
            return int(r["precio"])
    finally:
        conn.close()


def confirmar_semana_canal(db_path: str, canal_id: int, hoy: date, actor_id: int) -> int:
    """Marca pagados los cargos impagos de ESTA semana (cobro del domingo `week_start(hoy)` y cargos sueltos creados desde ese domingo).
    La deuda de semanas anteriores queda pendiente. Devuelve el total marcado (0 = no había nada)."""
    sunday = week_start(hoy)
    cond = ("pagado = 0 AND (lote = ? OR (lote IS NULL AND created_at >= ?)) AND renta_id IN "
            "(SELECT id FROM rent_rentas WHERE canal_id = ? AND activa = 1)")
    args = (sunday.isoformat(), _sunday_iso_start(sunday), canal_id)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            total = int(conn.execute(f"SELECT COALESCE(SUM(monto), 0) FROM rent_cargos WHERE {cond}", args).fetchone()[0])
            if total:
                conn.execute(f"UPDATE rent_cargos SET pagado = 1, paid_at = ?, paid_by = ? WHERE {cond}", (utcnow_iso(), actor_id) + args)
            return total
    finally:
        conn.close()


def confirmar_semana_monto(db_path: str, canal_id: int, hoy: date, monto: int, actor_id: int) -> tuple:
    """Como confirmar_semana_canal, pero el cliente pagó `monto` en vez de lo cargado: marca pagado lo de ESTA semana y deja un
    cargo 'ajuste' (pagado, del mismo lote) por la diferencia, así lo cobrado suma exactamente `monto`. La deuda de semanas
    anteriores no se toca. Devuelve (total cargado de la semana, monto pagado); (0, 0) si no había nada pendiente."""
    if monto <= 0:
        raise RentaError("El monto tiene que ser mayor a 0.")
    sunday = week_start(hoy)
    cond = ("pagado = 0 AND (lote = ? OR (lote IS NULL AND created_at >= ?)) AND renta_id IN "
            "(SELECT id FROM rent_rentas WHERE canal_id = ? AND activa = 1)")
    args = (sunday.isoformat(), _sunday_iso_start(sunday), canal_id)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            rows = conn.execute(f"SELECT renta_id, monto FROM rent_cargos WHERE {cond} ORDER BY id", args).fetchall()
            total = sum(int(r["monto"]) for r in rows)
            if not rows:
                return 0, 0
            now = utcnow_iso()
            conn.execute(f"UPDATE rent_cargos SET pagado = 1, paid_at = ?, paid_by = ? WHERE {cond}", (now, actor_id) + args)
            diff = int(monto) - total
            if diff:
                conn.execute("INSERT INTO rent_cargos(renta_id, concepto, periodo, lote, monto, pagado, created_at, paid_at, paid_by, detalle) "
                             "VALUES(?, 'ajuste', ?, ?, ?, 1, ?, ?, ?, ?)",
                             (int(rows[0]["renta_id"]), now, sunday.isoformat(), diff, now, now, actor_id,
                              f"Pagó {int(monto)} de {total}"[:100]))
            return total, int(monto)
    finally:
        conn.close()


def agregar_extra(db_path: str, canal_id: int, detalle: str, monto: int, actor_id: int, renta_id: Optional[int] = None) -> Cargo:
    """Registra un pago extra (ya cobrado: queda pagado). Va a la renta indicada o a la primera renta activa del canal."""
    detalle = (detalle or "").strip()
    if not detalle:
        raise RentaError("Falta el concepto del extra.")
    if monto <= 0:
        raise RentaError("El monto tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            q = "SELECT id FROM rent_rentas WHERE canal_id = ? AND activa = 1" + (" AND id = ?" if renta_id else "") + " ORDER BY id LIMIT 1"
            r = conn.execute(q, (canal_id, renta_id) if renta_id else (canal_id,)).fetchone()
            if r is None:
                raise RentaError("Este canal no tiene rentas activas.")
            now = utcnow_iso()
            cur = conn.execute("INSERT INTO rent_cargos(renta_id, concepto, periodo, monto, pagado, created_at, paid_at, paid_by, detalle) "
                               "VALUES(?, 'extra', ?, ?, 1, ?, ?, ?, ?)", (int(r["id"]), now, int(monto), now, now, actor_id, detalle[:100]))
            return _cargo(conn.execute("SELECT * FROM rent_cargos WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def extras_canal(db_path: str, canal_id: int) -> List[Cargo]:
    """Extras de las rentas activas del canal, del más reciente al más viejo."""
    conn = connect(db_path)
    try:
        return [_cargo(r) for r in conn.execute(
            "SELECT c.* FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id WHERE r.canal_id = ? AND r.activa = 1 "
            "AND c.concepto = 'extra' ORDER BY c.id DESC", (canal_id,))]
    finally:
        conn.close()


def dar_baja(db_path: str, renta_id: int, hoy: date) -> Optional[Renta]:
    """Baja de la renta. Vencido: se cobran los días usados de la semana en curso (hasta hoy, inclusive).
    La deuda que quede impaga queda registrada al titular (no cuenta para estadísticas). None si ya estaba dada de baja."""
    r = get_renta(db_path, renta_id)
    if r is None or not r.activa:
        return None
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if r.modalidad == "vencido" and r.colocado:
                days = billable_days(r, week_start(hoy), hoy)
                _insert_cargo(conn, r.id, "baja", week_start(hoy).isoformat(), prorate(r.precio, days))
            conn.execute("UPDATE rent_rentas SET activa = 0, fecha_baja = ? WHERE id = ? AND activa = 1", (hoy.isoformat(), r.id))
    finally:
        conn.close()
    return get_renta(db_path, renta_id)


# ============================================================
# Cobro semanal
# ============================================================
def job_done(db_path: str, key: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM rent_jobs WHERE key = ?", (key,)).fetchone() is not None
    finally:
        conn.close()


def mark_job(db_path: str, key: str) -> bool:
    """True si lo marcó ahora (False = ya estaba hecho)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT OR IGNORE INTO rent_jobs(key, done_at) VALUES(?, ?)", (key, utcnow_iso()))
            return cur.rowcount == 1
    finally:
        conn.close()


def weekly_charge_for(r: Renta, sunday: date) -> Optional[tuple]:
    """(concepto, periodo, monto) que le toca a la renta en el cobro del domingo `sunday`, o None."""
    if not r.colocado or r.fecha_inicio >= sunday:
        return None                                   # HO sin colocar, o empezó ese mismo domingo (o después): ya lo cubre el prorrateo / el próximo cobro
    if r.modalidad == "adelantado":
        days = billable_days(r, sunday, sunday + timedelta(days=6))
        periodo = sunday
    else:
        periodo = sunday - timedelta(days=7)
        days = billable_days(r, periodo, sunday - timedelta(days=1))
    if days <= 0:
        return None
    return ("semana", periodo.isoformat(), prorate(r.precio, days))


def cargar_resto(db_path: str, canal_id: int) -> List[Cargo]:
    """Carga el 50% restante de defensa / pack de las rentas activas del canal (una sola vez). Devuelve los cargos creados."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            ids = []
            for r in [_renta(x) for x in conn.execute("SELECT * FROM rent_rentas WHERE canal_id = ? AND activa = 1 AND colocado = 1",
                                                       (canal_id,))]:
                for total, concepto in ((r.defensa_total, "defensa_resto"), (r.pack_total, "pack_resto")):
                    if total and _insert_cargo(conn, r.id, concepto, r.fecha_inicio.isoformat(), half(total)[1]):
                        ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
            return [_cargo(x) for x in conn.execute(f"SELECT * FROM rent_cargos WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    finally:
        conn.close()


def run_weekly(db_path: str, sunday: date) -> List[int]:
    """Genera los cargos del domingo `sunday` (idempotente). Devuelve los canal_id con rentas activas."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            rentas = [_renta(x) for x in conn.execute("SELECT * FROM rent_rentas WHERE activa = 1")]
            for r in rentas:
                ch = weekly_charge_for(r, sunday)
                if ch:
                    _insert_cargo(conn, r.id, ch[0], ch[1], ch[2], lote=sunday.isoformat())
            return sorted({r.canal_id for r in rentas})
    finally:
        conn.close()


def cargos_lote_canal(db_path: str, canal_id: int, sunday: date) -> List[Cargo]:
    conn = connect(db_path)
    try:
        return [_cargo(r) for r in conn.execute(
            "SELECT c.* FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id WHERE r.canal_id = ? AND c.lote = ? ORDER BY c.id",
            (canal_id, sunday.isoformat()))]
    finally:
        conn.close()


# ============================================================
# Estadísticas
# ============================================================
@dataclass
class Resumen:
    ultimo_domingo: date
    proximo_domingo: date
    cobrado_historico: int        # pagado, sin la semana en curso
    ultimo_generado: int
    ultimo_pagado: int
    proximo_estimado: int
    deuda_activa: int


def resumen(db_path: str, hoy: date) -> Resumen:
    """Las deudas de rentas dadas de baja no entran en nada de esto."""
    last = week_start(hoy)
    nxt = last + timedelta(days=7)
    conn = connect(db_path)
    try:
        hist = int(conn.execute("SELECT COALESCE(SUM(monto), 0) FROM rent_cargos WHERE pagado = 1 AND created_at < ?",
                                (_sunday_iso_start(last),)).fetchone()[0])
        gen = int(conn.execute("SELECT COALESCE(SUM(c.monto), 0) FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id "
                               "WHERE c.lote = ? AND (r.activa = 1 OR c.pagado = 1)", (last.isoformat(),)).fetchone()[0])
        pag = int(conn.execute("SELECT COALESCE(SUM(monto), 0) FROM rent_cargos WHERE lote = ? AND pagado = 1",
                               (last.isoformat(),)).fetchone()[0])
        deuda = int(conn.execute("SELECT COALESCE(SUM(c.monto), 0) FROM rent_cargos c JOIN rent_rentas r ON r.id = c.renta_id "
                                 "WHERE r.activa = 1 AND c.pagado = 0").fetchone()[0])
        rentas = [_renta(x) for x in conn.execute("SELECT * FROM rent_rentas WHERE activa = 1")]
    finally:
        conn.close()
    est = sum(ch[2] for ch in (weekly_charge_for(r, nxt) for r in rentas) if ch)
    return Resumen(last, nxt, hist, gen, pag, est, deuda)


def deudas_baja(db_path: str, titular: Optional[str] = None) -> List[tuple]:
    """[(titular_tipo, titular_nombre, mapa, total impago)] de rentas dadas de baja con deuda."""
    conn = connect(db_path)
    try:
        q = ("SELECT r.titular_tipo, r.titular_nombre, m.nombre AS mapa, SUM(c.monto) AS total FROM rent_cargos c "
             "JOIN rent_rentas r ON r.id = c.renta_id JOIN rent_mapas m ON m.id = r.mapa_id WHERE r.activa = 0 AND c.pagado = 0")
        args: list = []
        if titular:
            q += " AND r.titular_nombre LIKE ? COLLATE NOCASE"
            args.append(f"%{titular.strip()}%")
        q += " GROUP BY r.titular_tipo, r.titular_nombre, m.nombre ORDER BY total DESC"
        return [(r["titular_tipo"], r["titular_nombre"], r["mapa"], int(r["total"])) for r in conn.execute(q, args)]
    finally:
        conn.close()


def mapa_names(db_path: str) -> Dict[int, str]:
    conn = connect(db_path)
    try:
        return {int(r["id"]): r["nombre"] for r in conn.execute("SELECT id, nombre FROM rent_mapas")}
    finally:
        conn.close()


# ============================================================
# Roles de guild «[TAG] Guild»
# ============================================================
@dataclass
class GuildRole:
    id: int
    guild_nombre: str
    tag: Optional[str]
    role_id: int
    color: int

    @property
    def name(self) -> str:
        return guild_role_name(self.guild_nombre, self.tag)


def _grole(r) -> Optional[GuildRole]:
    if r is None:
        return None
    return GuildRole(int(r["id"]), r["guild_nombre"], r["tag"], int(r["role_id"]), int(r["color"]))


def get_guild_role(db_path: str, guild_nombre: str) -> Optional[GuildRole]:
    conn = connect(db_path)
    try:
        return _grole(conn.execute("SELECT * FROM rent_guild_roles WHERE guild_nombre = ? COLLATE NOCASE",
                                   ((guild_nombre or "").strip(),)).fetchone())
    finally:
        conn.close()


def get_guild_role_by_id(db_path: str, grole_id: int) -> Optional[GuildRole]:
    conn = connect(db_path)
    try:
        return _grole(conn.execute("SELECT * FROM rent_guild_roles WHERE id = ?", (grole_id,)).fetchone())
    finally:
        conn.close()


def list_guild_roles(db_path: str) -> List[GuildRole]:
    conn = connect(db_path)
    try:
        return [_grole(r) for r in conn.execute("SELECT * FROM rent_guild_roles ORDER BY guild_nombre COLLATE NOCASE")]
    finally:
        conn.close()


def _hsv_to_int(h: float, s: float, v: float) -> int:
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def next_color(db_path: str) -> int:
    """Color distinto para cada guild: tono por razón áurea (se reparten bien) y nunca uno ya usado."""
    used = {g.color for g in list_guild_roles(db_path)}
    i = len(used)
    while True:
        c = _hsv_to_int(0.11 + i * 0.618033988749895, 0.70 if (i // 10) % 2 == 0 else 0.50, 0.95)
        if c not in used and c != 0:
            return c
        i += 1


def save_guild_role(db_path: str, guild_nombre: str, tag: Optional[str], role_id: int, color: int) -> GuildRole:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO rent_guild_roles(guild_nombre, tag, role_id, color) VALUES(?, ?, ?, ?) "
                         "ON CONFLICT(guild_nombre) DO UPDATE SET tag = excluded.tag, role_id = excluded.role_id",
                         (guild_nombre.strip(), tag, role_id, color))
    finally:
        conn.close()
    return get_guild_role(db_path, guild_nombre)


def set_guild_tag(db_path: str, guild_nombre: str, tag: Optional[str]) -> GuildRole:
    if tag is not None and len(tag) > TAG_MAX:
        raise RentaError(f"El tag de alianza tiene que tener hasta {TAG_MAX} caracteres.")
    g = get_guild_role(db_path, guild_nombre)
    if g is None:
        raise RentaError(f"No hay rol para la guild «{guild_nombre}».")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE rent_guild_roles SET tag = ? WHERE id = ?", (tag or None, g.id))
    finally:
        conn.close()
    return get_guild_role_by_id(db_path, g.id)


def set_user_guild(db_path: str, user_id: int, grole_id: int) -> Optional[int]:
    """Asigna la guild del usuario. Devuelve el id de la guild anterior (o None)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT guild_role_id FROM rent_usuarios WHERE user_id = ?", (user_id,)).fetchone()
            conn.execute("INSERT INTO rent_usuarios(user_id, guild_role_id) VALUES(?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET guild_role_id = excluded.guild_role_id", (user_id, grole_id))
            return int(r["guild_role_id"]) if r else None
    finally:
        conn.close()


def user_guild(db_path: str, user_id: int) -> Optional[GuildRole]:
    conn = connect(db_path)
    try:
        return _grole(conn.execute("SELECT g.* FROM rent_usuarios u JOIN rent_guild_roles g ON g.id = u.guild_role_id WHERE u.user_id = ?",
                                   (user_id,)).fetchone())
    finally:
        conn.close()
