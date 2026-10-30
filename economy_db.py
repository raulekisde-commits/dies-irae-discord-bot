"""Wuju Coins: lógica y persistencia (sin dependencias de Discord).

Todo el dinero son enteros. El balance de una cuenta y su movimiento se escriben en la
misma transacción, así el historial siempre puede reconstruir los balances.
"""
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from dbutil import connect, utcnow_iso, write_tx

CURRENCY_NAME = "Wuju Coins"
MAX_AMOUNT = 10 ** 15          # tope por operación (por usuario)
MAX_BALANCE_ABS = 10 ** 18     # protección contra desbordar el entero de 64 bits de SQLite

ECON_COMMANDS = ("bal", "addmoney", "addmoney-role", "remove-money", "top")
# Comandos de administración de saldo: funcionan en cualquier canal (solo se exige el rol/permiso).
ANY_CHANNEL_COMMANDS = ("addmoney", "addmoney-role", "remove-money")
# Permisos que se otorgan con /perms-comando pero no son comandos de prefijo restringidos por canal
GAME_COMMANDS = ("dados", "coinflip", "1v1")          # juegos con apuesta: se habilitan con /perms-canal y /perms-comando
EXTRA_PERMISSION_COMMANDS = ("apuesta", "apuestas", "verhistorial", "set-balance", "eco-stats", "give-money") + GAME_COMMANDS
GRANTABLE_COMMANDS = ECON_COMMANDS + EXTRA_PERMISSION_COMMANDS
CHANNEL_COMMANDS = tuple(c for c in ECON_COMMANDS if c not in ANY_CHANNEL_COMMANDS) + GAME_COMMANDS   # bal, top, dados, coinflip, 1v1


# ============================================================
# Números: UNA sola función para leer y UNA para mostrar
# ============================================================
class AmountError(ValueError):
    """Cantidad mal formada. El mensaje es apto para mostrarse al usuario."""


_PLAIN_RE = re.compile(r"^(0|[1-9]\d*)$")
_GROUPED_RE = re.compile(r"^[1-9]\d{0,2}(?:[.,]\d{3})+$")


def parse_amount(text: str) -> int:
    """Lee una cantidad entera positiva.

    Válidos: 150000, 150.000, 150,000, 1.500.000, 1,500,000
    Inválidos (se rechazan, nunca se "adivinan"): 1.5, 150.00, 1,50, 1.500,000 (mezcla), -5, 0, 1e5, 12 345
    """
    s = (text or "").strip()
    if not s:
        raise AmountError("Falta la cantidad.")
    if s.startswith("-"):
        raise AmountError("La cantidad tiene que ser positiva (sin signo `-`).")
    if s.startswith("+"):
        s = s[1:]

    if _PLAIN_RE.match(s):
        value = int(s)
    elif _GROUPED_RE.match(s):
        if len(set(re.findall(r"[.,]", s))) > 1:
            raise AmountError("No mezcles puntos y comas en la misma cantidad (ej: `1.500.000` o `1,500,000`).")
        value = int(re.sub(r"[.,]", "", s))
    else:
        raise AmountError(
            "Cantidad inválida. Usá solo números enteros, con puntos o comas únicamente como separador de miles "
            "de a 3 dígitos (ej: `150000`, `150.000`, `1,500,000`)."
        )

    if value <= 0:
        raise AmountError("La cantidad tiene que ser mayor a 0.")
    if value > MAX_AMOUNT:
        raise AmountError(f"La cantidad es demasiado grande (máximo {format_amount(MAX_AMOUNT)}).")
    return value


_SHORT_RE = re.compile(r"^([0-9]+)(?:[.,]([0-9]{1,2}))?([kKmMbB])$")
_SHORT_MULT = {"k": 10 ** 3, "m": 10 ** 6, "b": 10 ** 9}


def parse_amount_short(text: str) -> int:
    """Igual que parse_amount, pero además acepta abreviaturas SOLO al ingresar: `1k` = 1.000, `1m` = 1.000.000, `10m` = 10.000.000,
    `2b` = 2.000.000.000 y hasta dos decimales (`1.5m` = 1.500.000). Lo que se muestra no cambia (format_amount)."""
    s = (text or "").strip()
    m = _SHORT_RE.match(s)
    if m is None:
        return parse_amount(s)
    whole, dec, suffix = m.group(1), m.group(2), m.group(3).lower()
    mult = _SHORT_MULT[suffix]
    value = int(whole) * mult + (int(dec.ljust(2, "0")) * mult // 100 if dec else 0)
    if value <= 0:
        raise AmountError("La cantidad tiene que ser mayor a 0.")
    if value > MAX_AMOUNT:
        raise AmountError(f"La cantidad es demasiado grande (máximo {format_amount(MAX_AMOUNT)}).")
    return value


def format_amount(n: int) -> str:
    """150000 -> '150,000' | -150000 -> '-150,000' (los miles se separan con comas en lo que se muestra).

    Al escribir una cantidad se siguen aceptando `150000`, `150,000` y `150.000` (ver parse_amount)."""
    return f"{int(n):,}"


# ============================================================
# Motivos de las operaciones
# ============================================================
REASON_ADD_DEFAULT = "Sin motivo especificado"
REASON_REMOVE_DEFAULT = "cobro el bal"
MAX_REASON = 200

MOVEMENT_LABELS = {
    "addmoney": "Agregado",
    "addmoney-role": "Agregado por rol",
    "remove-money": "Retiro",
    "bet_stake": "Apuesta realizada",
    "bet_payout": "Premio de apuesta",
    "bet_refund": "Devolución de apuesta",
    "balance_set": "Saldo ajustado",
    "balance_reset": "Saldo restablecido",
    "guildshop_purchase": "Compra en Guild Shop",
    "transfer_out": "Transferencia enviada",
    "transfer_in": "Transferencia recibida",
    "duel_stake": "Apuesta de duelo 1v1",
    "duel_win": "Premio de duelo 1v1",
    "duel_refund": "Devolución de duelo 1v1",
    "dice_stake": "Entrada a dados",
    "dice_win": "Premio de dados",
    "dice_refund": "Devolución de dados",
    "coinflip_stake": "Apuesta de coinflip",
    "coinflip_win": "Premio de coinflip",
    "coinflip_refund": "Devolución de coinflip",
    "raffle_ticket": "Ticket de rifa",
    "raffle_refund": "Devolución de rifa",
    "roulette_bet": "Apuesta de ruleta",
    "roulette_win": "Premio de ruleta",
    "roulette_refund": "Devolución de ruleta",
}


_NUMBERISH_RE = re.compile(r"^[\d.,]+$")


def reason_from_tokens(tokens: List[str], default: str) -> str:
    """tokens = [objetivo, cantidad, motivo...]. Si el motivo empieza con un número suelto se rechaza,
    porque probablemente la cantidad estaba escrita con espacios (`12 345`) y se leería mal."""
    rest = tokens[2] if len(tokens) > 2 else ""
    first = rest.split(None, 1)[0] if rest.strip() else ""
    if first and _NUMBERISH_RE.match(first):
        raise AmountError("La cantidad es ambigua: no uses espacios dentro del número (ej: `12345` o `12,345`). "
                          "Si querés un motivo que empiece con un número, escribí primero una palabra.")
    return clean_reason(rest, default)


def clean_reason(text: Optional[str], default: str) -> str:
    """Motivo en una línea, sin espacios sobrantes y con tope de largo. Vacío => `default`."""
    value = " ".join((text or "").split())
    if not value:
        return default
    return value[:MAX_REASON]


# ============================================================
# Importación de saldos desde una lista "apodo + cantidad"
# ============================================================
IMPORT_REASON = "Importación inicial de saldos"


def parse_balance_value(text: str) -> int:
    """Saldo objetivo: entero con el mismo formato de miles que parse_amount, pero admite 0 y negativos."""
    s = (text or "").strip()
    if s in ("0", "-0", "+0"):
        return 0
    negative = s.startswith("-")
    value = parse_amount(s[1:] if negative else s)
    return -value if negative else value
MAX_IMPORT_LINES = 500


def parse_import_text(text: str):
    """Lee líneas `apodo;cantidad`, `apodo,cantidad`, `apodo<TAB>cantidad` o `apodo cantidad`.

    Devuelve (entradas, errores): entradas = [(linea, apodo, cantidad)], errores = [(linea, texto, mensaje)].
    """
    entries, errors = [], []
    lines = [(n, raw.strip()) for n, raw in enumerate((text or "").splitlines(), start=1)]
    lines = [(n, raw) for n, raw in lines if raw and not raw.startswith("#")]
    if len(lines) > MAX_IMPORT_LINES:
        raise AmountError(f"La lista tiene demasiadas líneas (máximo {MAX_IMPORT_LINES}). Dividila en partes.")
    for n, raw in lines:
        name = amount_text = None
        if ";" in raw or "\t" in raw:
            sep = ";" if ";" in raw else "\t"
            name, _, amount_text = raw.rpartition(sep)
        elif "," in raw:
            head, _, tail = raw.rpartition(",")
            if re.fullmatch(r"\d{3}", tail.strip()) and head.rstrip()[-1:].isdigit():
                name, _, amount_text = raw.rpartition(" ")       # "apodo 1,500": la coma es separador de miles
            else:
                name, amount_text = head, tail
        else:
            name, _, amount_text = raw.rpartition(" ")
        name, amount_text = (name or "").strip(), (amount_text or "").strip()
        if not name:
            errors.append((n, raw, "Falta el apodo o la cantidad."))
            continue
        try:
            entries.append((n, name, parse_balance_value(amount_text)))
        except AmountError as exc:
            errors.append((n, raw, str(exc)))
    return entries, errors


# ============================================================
# Esquema (migración segura: solo CREATE ... IF NOT EXISTS)
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS eco_accounts (
    user_id    INTEGER PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_operations (
    op_id           TEXT PRIMARY KEY,
    request_key     TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('add','remove')),
    scope           TEXT NOT NULL CHECK (scope IN ('user','role')),
    actor_id        INTEGER NOT NULL,
    role_id         INTEGER,
    per_user_amount INTEGER NOT NULL,
    target_count    INTEGER NOT NULL,
    total_amount    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed',
    created_at      TEXT NOT NULL,
    log_status      TEXT NOT NULL DEFAULT 'pending',
    log_pages_sent  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS eco_movements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id          TEXT NOT NULL REFERENCES eco_operations(op_id),
    user_id        INTEGER NOT NULL,
    delta          INTEGER NOT NULL,
    balance_before INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eco_movements_user ON eco_movements(user_id);
CREATE INDEX IF NOT EXISTS idx_eco_movements_op   ON eco_movements(op_id);
CREATE TABLE IF NOT EXISTS eco_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_command_roles (
    command TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (command, role_id)
);
CREATE TABLE IF NOT EXISTS eco_command_channels (
    command    TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL
);
"""


# Columnas agregadas después (migración aditiva: nunca borra ni reescribe datos existentes)
_ADDED_COLUMNS = (
    ("eco_operations", "reason", "TEXT"),
    ("eco_operations", "related_id", "TEXT"),
    ("eco_movements", "kind", "TEXT"),
    ("eco_movements", "reason", "TEXT"),
    ("eco_movements", "actor_id", "INTEGER"),
    ("eco_movements", "related_id", "TEXT"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                # otro proceso la agregó en el medio
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    raise


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eco_movements_related ON eco_movements(related_id)")
    finally:
        conn.close()


# ============================================================
# Balances
# ============================================================
def get_balance(db_path: str, user_id: int) -> int:
    """Balance de un usuario; 0 si todavía no tiene cuenta (no la crea)."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (int(user_id),)).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        conn.close()


# Movimientos que cuentan como "depositados a la persona" en el Balance histórico.
# NO cuentan: remove-money, apuestas (apostar / premio / devolución), que son movimientos internos.
LIFETIME_DEPOSIT_TYPES = ("addmoney", "addmoney-role", "balance_set")


def get_lifetime_deposits(db_path: str, user_id: int) -> int:
    """Balance histórico: suma de todo lo depositado a la persona. Solo suma, nunca resta.

    Se calcula desde el historial de movimientos (por eso incluye también lo cargado antes de existir el contador).
    Un ajuste de saldo (importación) suma únicamente si subió el saldo.
    """
    marks = ",".join("?" for _ in LIFETIME_DEPOSIT_TYPES)
    conn = connect(db_path)
    try:
        after_id = _lifetime_reset_id(conn)
        row = conn.execute(
            f"SELECT COALESCE(SUM(delta), 0) AS total FROM eco_movements "
            f"WHERE user_id = ? AND id > ? AND delta > 0 AND COALESCE(kind, 'addmoney') IN ({marks})",
            (int(user_id), after_id, *LIFETIME_DEPOSIT_TYPES)).fetchone()
        return int(row["total"])
    finally:
        conn.close()


def _lifetime_reset_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM eco_settings WHERE key = 'lifetime_reset_movement_id'").fetchone()
    return int(row["value"]) if row else 0


def reset_lifetime(db_path: str, actor_id: int) -> int:
    """Pone en cero el Balance histórico de TODOS. No borra movimientos ni toca balances: solo guarda una marca
    y, desde ese momento, el contador cuenta únicamente lo depositado después. Devuelve cuántas personas tenían histórico."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            before_id = _lifetime_reset_id(conn)
            marks = ",".join("?" for _ in LIFETIME_DEPOSIT_TYPES)
            affected = int(conn.execute(
                f"SELECT COUNT(DISTINCT user_id) c FROM eco_movements WHERE id > ? AND delta > 0 "
                f"AND COALESCE(kind, 'addmoney') IN ({marks})", (before_id, *LIFETIME_DEPOSIT_TYPES)).fetchone()["c"])
            last = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM eco_movements").fetchone()["m"]
            now = utcnow_iso()
            for key, value in (("lifetime_reset_movement_id", str(int(last))), ("lifetime_reset_at", now), ("lifetime_reset_by", str(int(actor_id)))):
                conn.execute("INSERT INTO eco_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            return affected
    finally:
        conn.close()


@dataclass
class EcoStats:
    total: int            # suma de todos los balances cargados
    positive: int         # suma de balances positivos
    negative: int         # suma de balances negativos (valor negativo)
    accounts: int         # cuentas con balance distinto de 0
    holders_positive: int
    holders_negative: int
    lifetime_total: int   # suma del Balance histórico de todos (desde el último reset)


def get_stats(db_path: str) -> EcoStats:
    marks = ",".join("?" for _ in LIFETIME_DEPOSIT_TYPES)
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(balance),0) t,
                      COALESCE(SUM(CASE WHEN balance > 0 THEN balance END),0) p,
                      COALESCE(SUM(CASE WHEN balance < 0 THEN balance END),0) n,
                      COALESCE(SUM(CASE WHEN balance != 0 THEN 1 END),0) a,
                      COALESCE(SUM(CASE WHEN balance > 0 THEN 1 END),0) hp,
                      COALESCE(SUM(CASE WHEN balance < 0 THEN 1 END),0) hn
               FROM eco_accounts""").fetchone()
        after_id = _lifetime_reset_id(conn)
        life = conn.execute(
            f"SELECT COALESCE(SUM(delta),0) t FROM eco_movements WHERE id > ? AND delta > 0 "
            f"AND COALESCE(kind, 'addmoney') IN ({marks})", (after_id, *LIFETIME_DEPOSIT_TYPES)).fetchone()["t"]
        return EcoStats(int(row["t"]), int(row["p"]), int(row["n"]), int(row["a"]), int(row["hp"]), int(row["hn"]), int(life))
    finally:
        conn.close()


def get_all_balances(db_path: str) -> Dict[int, int]:
    conn = connect(db_path)
    try:
        return {int(r["user_id"]): int(r["balance"]) for r in conn.execute("SELECT user_id, balance FROM eco_accounts")}
    finally:
        conn.close()


def rank_top(balances: Dict[int, int], eligible_ids: Iterable[int], limit: int = 10) -> List[Tuple[int, int, int]]:
    """Ranking determinista: balance desc y, en empate, user_id asc.

    Los elegibles sin cuenta cuentan como 0. Sin duplicados. Devuelve [(posición, user_id, balance)].
    """
    seen: Set[int] = set()
    rows: List[Tuple[int, int]] = []
    for uid in eligible_ids:
        uid = int(uid)
        if uid in seen:
            continue
        seen.add(uid)
        rows.append((uid, int(balances.get(uid, 0))))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [(i + 1, uid, bal) for i, (uid, bal) in enumerate(rows[:limit])]


# ============================================================
# Operaciones (agregar / retirar) — atómicas e idempotentes
# ============================================================
@dataclass
class Movement:
    user_id: int
    delta: int
    before: int
    after: int


@dataclass
class OperationResult:
    op_id: str
    request_key: str
    kind: str                 # 'add' | 'remove'
    scope: str                # 'user' | 'role'
    actor_id: int
    role_id: Optional[int]
    per_user_amount: int
    total_amount: int
    created_at: str
    created: bool = True      # False = esta solicitud ya se había aplicado antes (no se repitió)
    log_status: str = "pending"
    log_pages_sent: int = 0
    movements: List[Movement] = field(default_factory=list)
    reason: str = ""


def _load_result(conn: sqlite3.Connection, row: sqlite3.Row, created: bool) -> OperationResult:
    movements = [
        Movement(int(m["user_id"]), int(m["delta"]), int(m["balance_before"]), int(m["balance_after"]))
        for m in conn.execute("SELECT * FROM eco_movements WHERE op_id = ? ORDER BY id", (row["op_id"],))
    ]
    return OperationResult(
        op_id=row["op_id"], request_key=row["request_key"], kind=row["kind"], scope=row["scope"],
        actor_id=int(row["actor_id"]), role_id=int(row["role_id"]) if row["role_id"] is not None else None,
        per_user_amount=int(row["per_user_amount"]), total_amount=int(row["total_amount"]),
        created_at=row["created_at"], created=created, log_status=row["log_status"],
        log_pages_sent=int(row["log_pages_sent"]), movements=movements,
        reason=(row["reason"] if "reason" in row.keys() and row["reason"] else ""),
    )


def apply_operation(
    db_path: str,
    *,
    kind: str,
    actor_id: int,
    user_ids: Iterable[int],
    amount: int,
    request_key: str,
    role_id: Optional[int] = None,
    reason: Optional[str] = None,
    require_funds: bool = False,
    _test_hook: Optional[Callable[[int, int], None]] = None,
) -> OperationResult:
    """Suma o resta `amount` a CADA usuario de `user_ids`, todo en UNA transacción.

    - Si algo falla a la mitad se revierte todo (no quedan balances parciales).
    - `request_key` es único: repetir la misma solicitud (reintento, doble evento) devuelve la operación
      ya aplicada con created=False en lugar de aplicarla otra vez.
    - Los balances pueden quedar negativos (salvo con require_funds=True: si alguien no alcanza se lanza
      InsufficientFunds y no se mueve nada).
    """
    if kind not in ("add", "remove"):
        raise ValueError("kind inválido")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > MAX_AMOUNT:
        raise ValueError("amount inválido")
    ids: List[int] = []
    seen: Set[int] = set()
    for uid in user_ids:
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise ValueError("user_id inválido")
        if uid not in seen:
            seen.add(uid)
            ids.append(uid)
    if not ids:
        raise ValueError("sin destinatarios")

    sign = 1 if kind == "add" else -1
    reason = clean_reason(reason, REASON_ADD_DEFAULT if kind == "add" else REASON_REMOVE_DEFAULT)
    mov_type = ("addmoney-role" if role_id is not None else "addmoney") if kind == "add" else "remove-money"
    now = utcnow_iso()
    conn = connect(db_path)
    try:
        with write_tx(conn):
            existing = conn.execute("SELECT * FROM eco_operations WHERE request_key = ?", (request_key,)).fetchone()
            if existing is not None:
                return _load_result(conn, existing, created=False)

            op_id = uuid.uuid4().hex
            scope = "role" if role_id is not None else "user"
            conn.execute(
                """INSERT INTO eco_operations(op_id, request_key, kind, scope, actor_id, role_id,
                       per_user_amount, target_count, total_amount, status, created_at, reason, related_id)
                   VALUES (?,?,?,?,?,?,?,?,?, 'completed', ?, ?, ?)""",
                (op_id, request_key, kind, scope, actor_id, role_id, amount, len(ids), amount * len(ids), now, reason, op_id),
            )
            for index, uid in enumerate(ids):
                conn.execute(
                    "INSERT OR IGNORE INTO eco_accounts(user_id, balance, created_at, updated_at) VALUES (?,0,?,?)",
                    (uid, now, now),
                )
                before = int(conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (uid,)).fetchone()["balance"])
                after = before + sign * amount
                if require_funds and sign < 0 and after < 0:
                    raise InsufficientFunds(before)
                if abs(after) > MAX_BALANCE_ABS:
                    raise OverflowError("El balance resultante excede el límite permitido.")
                conn.execute("UPDATE eco_accounts SET balance = ?, updated_at = ? WHERE user_id = ?", (after, now, uid))
                conn.execute(
                    """INSERT INTO eco_movements(op_id, user_id, delta, balance_before, balance_after, created_at,
                           kind, reason, actor_id, related_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (op_id, uid, sign * amount, before, after, now, mov_type, reason, actor_id, op_id),
                )
                if _test_hook is not None:
                    _test_hook(index, uid)
            row = conn.execute("SELECT * FROM eco_operations WHERE op_id = ?", (op_id,)).fetchone()
            return _load_result(conn, row, created=True)
    finally:
        conn.close()


# ------------------------------------------------------------
# Piezas para otros módulos (apuestas): operan DENTRO de una transacción abierta por quien las llama,
# así el saldo, el historial y el registro del otro módulo se confirman o se revierten juntos.
# ------------------------------------------------------------
class InsufficientFunds(Exception):
    pass


# ------------------------------------------------------------
# Congelamiento del balance: con un ticket de «Cobrar balance» abierto, la persona no puede gastar su balance
# (apostar, anotarse a juegos, comprar tickets de rifa, dar coins, comprar en la Guild Shop). Lo que ya estaba en juego sigue su curso y
# los premios/devoluciones se acreditan igual; el retiro que hace el Council en el ticket tampoco se bloquea.
# ------------------------------------------------------------
FROZEN_TEXT = ("🧊 Tu balance está **congelado** mientras tengas abierto un ticket de **Cobrar balance**: no podés apostar ni mover "
               "tu balance hasta que se cierre el ticket.")
FROZEN_BLOCKED_TYPES = frozenset({"bet_stake", "duel_stake", "dice_stake", "coinflip_stake", "raffle_ticket", "roulette_bet", "transfer_out", "guildshop_purchase"})


class BalanceFrozen(Exception):
    """Se intentó gastar un balance congelado (red de seguridad: las pantallas avisan antes con un mensaje claro)."""


def _frozen_conn(conn, user_id: int) -> bool:
    try:
        return conn.execute("SELECT 1 FROM cm_tickets WHERE user_id = ? AND kind = 'sell' AND status = 'open' LIMIT 1", (user_id,)).fetchone() is not None
    except sqlite3.OperationalError as exc:              # todavía no existe la tabla de tickets: nadie está congelado
        if "no such table" in str(exc):
            return False
        raise


def is_frozen(db_path: str, user_id: int) -> bool:
    conn = connect(db_path)
    try:
        return _frozen_conn(conn, user_id)
    finally:
        conn.close()


def tx_new_operation(conn: sqlite3.Connection, *, kind: str, actor_id: int, request_key: str,
                     total_amount: int, target_count: int, reason: str, related_id: str) -> str:
    """Crea la operación que agrupa varios movimientos. No genera registro de auditoría público."""
    if kind not in ("add", "remove"):
        raise ValueError("kind inválido")
    op_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO eco_operations(op_id, request_key, kind, scope, actor_id, role_id, per_user_amount,
               target_count, total_amount, status, created_at, log_status, reason, related_id)
           VALUES (?,?,?,'user',?,NULL,0,?,?, 'completed', ?, 'sent', ?, ?)""",
        (op_id, request_key, kind, actor_id, target_count, total_amount, utcnow_iso(), reason, related_id),
    )
    return op_id


def tx_move(conn: sqlite3.Connection, *, op_id: str, user_id: int, delta: int, mov_type: str, reason: str,
            actor_id: int, related_id: str, require_funds: bool = False) -> Tuple[int, int]:
    """Aplica un movimiento a un balance dentro de la transacción en curso. Devuelve (antes, después)."""
    if delta < 0 and mov_type in FROZEN_BLOCKED_TYPES and _frozen_conn(conn, user_id):
        raise BalanceFrozen(user_id)
    now = utcnow_iso()
    conn.execute("INSERT OR IGNORE INTO eco_accounts(user_id, balance, created_at, updated_at) VALUES (?,0,?,?)",
                 (user_id, now, now))
    before = int(conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()["balance"])
    after = before + delta
    if require_funds and after < 0:
        raise InsufficientFunds(before)
    if abs(after) > MAX_BALANCE_ABS:
        raise OverflowError("El balance resultante excede el límite permitido.")
    conn.execute("UPDATE eco_accounts SET balance = ?, updated_at = ? WHERE user_id = ?", (after, now, user_id))
    conn.execute(
        """INSERT INTO eco_movements(op_id, user_id, delta, balance_before, balance_after, created_at,
               kind, reason, actor_id, related_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (op_id, user_id, delta, before, after, now, mov_type, reason, actor_id, related_id),
    )
    return before, after


@dataclass
class HistoryRow:
    created_at: str
    mov_type: str
    delta: int
    reason: str
    balance_after: int
    actor_id: Optional[int]
    related_id: Optional[str]

    @property
    def label(self) -> str:
        return MOVEMENT_LABELS.get(self.mov_type, self.mov_type)


def get_history(db_path: str, user_id: int, limit: int = 20) -> List[HistoryRow]:
    """Últimos movimientos de un usuario (más reciente primero). Los registros anteriores a la migración
    se completan con los datos de su operación."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT m.id, m.created_at, m.delta, m.balance_after, m.related_id AS m_related, m.kind AS m_kind,
                      m.reason AS m_reason, m.actor_id AS m_actor,
                      o.kind AS o_kind, o.scope AS o_scope, o.actor_id AS o_actor, o.reason AS o_reason, o.op_id AS o_id
               FROM eco_movements m JOIN eco_operations o ON o.op_id = m.op_id
               WHERE m.user_id = ? ORDER BY m.id DESC LIMIT ?""", (int(user_id), int(limit))).fetchall()
        result = []
        for r in rows:
            mov_type = r["m_kind"] or (("addmoney-role" if r["o_scope"] == "role" else "addmoney") if r["o_kind"] == "add" else "remove-money")
            reason = r["m_reason"] or r["o_reason"] or "—"
            result.append(HistoryRow(
                created_at=r["created_at"], mov_type=mov_type, delta=int(r["delta"]), reason=reason,
                balance_after=int(r["balance_after"]),
                actor_id=int(r["m_actor"] if r["m_actor"] is not None else r["o_actor"]),
                related_id=r["m_related"] or r["o_id"],
            ))
        return result
    finally:
        conn.close()


GIVE_DEFAULT_REASON = "Transferencia entre usuarios"


def transfer(db_path: str, *, from_id: int, to_id: int, amount: int, request_key: str,
             reason: str = GIVE_DEFAULT_REASON) -> Tuple[str, int, int]:
    """Pasa `amount` del balance de `from_id` al de `to_id` en UNA transacción (o se hace todo o no se hace nada).

    Devuelve (estado, saldo_origen, saldo_destino): estado "applied" o "repeated" (misma solicitud ya procesada).
    Lanza InsufficientFunds si el origen no alcanza y OverflowError si el destino superaría el límite.
    """
    if from_id == to_id:
        raise ValueError("no se puede transferir a uno mismo")
    if amount <= 0 or amount > MAX_AMOUNT:
        raise ValueError("cantidad inválida")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM eco_operations WHERE request_key = ?", (f"{request_key}:out",)).fetchone():
                return "repeated", get_balance(db_path, from_id), get_balance(db_path, to_id)
            related = f"give:{from_id}:{to_id}"
            op_out = tx_new_operation(conn, kind="remove", actor_id=from_id, request_key=f"{request_key}:out",
                                      total_amount=amount, target_count=1, reason=reason, related_id=related)
            _, after_from = tx_move(conn, op_id=op_out, user_id=from_id, delta=-amount, mov_type="transfer_out", reason=reason,
                                    actor_id=from_id, related_id=related, require_funds=True)
            op_in = tx_new_operation(conn, kind="add", actor_id=from_id, request_key=f"{request_key}:in",
                                     total_amount=amount, target_count=1, reason=reason, related_id=related)
            _, after_to = tx_move(conn, op_id=op_in, user_id=to_id, delta=amount, mov_type="transfer_in", reason=reason,
                                  actor_id=from_id, related_id=related)
            return "applied", after_from, after_to
    finally:
        conn.close()


def set_balance(db_path: str, *, user_id: int, target: int, actor_id: int, request_key: str, reason: str,
                mov_type: str = "balance_set"):
    """Deja el balance EXACTAMENTE en `target` (calcula la diferencia dentro de la transacción).

    Devuelve (estado, antes, después): estado = 'applied' | 'unchanged' (ya estaba igual) | 'repeated' (misma solicitud ya aplicada).
    """
    if abs(target) > MAX_BALANCE_ABS:
        raise OverflowError("El balance objetivo excede el límite permitido.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM eco_operations WHERE request_key = ?", (request_key,)).fetchone():
                row = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()
                bal = int(row["balance"]) if row else 0
                return "repeated", bal, bal
            row = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()
            before = int(row["balance"]) if row else 0
            delta = target - before
            if delta == 0:
                return "unchanged", before, before
            op_id = tx_new_operation(conn, kind="add" if delta > 0 else "remove", actor_id=actor_id, request_key=request_key,
                                     total_amount=abs(delta), target_count=1, reason=reason, related_id=request_key)
            _, after = tx_move(conn, op_id=op_id, user_id=user_id, delta=delta, mov_type=mov_type, reason=reason,
                               actor_id=actor_id, related_id=request_key)
            return "applied", before, after
    finally:
        conn.close()


def get_operation(db_path: str, op_id: str) -> Optional[OperationResult]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM eco_operations WHERE op_id = ?", (op_id,)).fetchone()
        return _load_result(conn, row, created=False) if row else None
    finally:
        conn.close()


def pending_log_operations(db_path: str, limit: int = 20) -> List[str]:
    """Operaciones confirmadas cuyo registro de auditoría todavía no se publicó completo."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT op_id FROM eco_operations WHERE log_status != 'sent' ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()
        return [r["op_id"] for r in rows]
    finally:
        conn.close()


def set_log_progress(db_path: str, op_id: str, pages_sent: int, done: bool, failed: bool = False) -> None:
    status = "sent" if done else ("failed" if failed else "pending")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute(
                "UPDATE eco_operations SET log_pages_sent = ?, log_status = ? WHERE op_id = ?",
                (pages_sent, status, op_id),
            )
    finally:
        conn.close()


# ============================================================
# Configuración, roles y canales por comando
# ============================================================
def get_setting(db_path: str, key: str) -> Optional[str]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM eco_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def get_bet_limit(db_path: str, game: str) -> Optional[int]:
    """Apuesta máxima configurada con /limite para dados, coinflip o 1v1 (None = sin límite propio)."""
    raw = get_setting(db_path, f"bet_limit:{game}")
    try:
        v = int(raw) if raw else 0
    except ValueError:
        return None
    return v if v > 0 else None


def set_bet_limit(db_path: str, game: str, amount: Optional[int]) -> None:
    set_setting(db_path, f"bet_limit:{game}", None if not amount else str(int(amount)))


def check_bet_limit(db_path: str, game: str, amount: int) -> Optional[str]:
    """Texto de error si la apuesta supera el límite del juego, o None."""
    limit = get_bet_limit(db_path, game)
    if limit is not None and amount > limit:
        return f"La apuesta máxima en {game} es {format_amount(limit)}."
    return None


def set_setting(db_path: str, key: str, value: Optional[str]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if value is None:
                conn.execute("DELETE FROM eco_settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO eco_settings(key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
    finally:
        conn.close()


_CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>$")


def parse_custom_emoji(text: str) -> Optional[Tuple[str, int]]:
    """'<:wuju:123456789012345678>' -> ('<:wuju:123456789012345678>', 123456789012345678) o None."""
    m = _CUSTOM_EMOJI_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(0), int(m.group(3))


def grant_command_role(db_path: str, command: str, role_id: int) -> bool:
    """True si el permiso se agregó, False si ya existía."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT OR IGNORE INTO eco_command_roles(command, role_id) VALUES (?,?)", (command, role_id))
            return cur.rowcount > 0
    finally:
        conn.close()


def revoke_command_role(db_path: str, command: str, role_id: int) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("DELETE FROM eco_command_roles WHERE command = ? AND role_id = ?", (command, role_id))
            return cur.rowcount > 0
    finally:
        conn.close()


def get_command_roles(db_path: str, command: str) -> Set[int]:
    conn = connect(db_path)
    try:
        return {int(r["role_id"]) for r in conn.execute("SELECT role_id FROM eco_command_roles WHERE command = ?", (command,))}
    finally:
        conn.close()


def set_command_channel(db_path: str, command: str, channel_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO eco_command_channels(command, channel_id) VALUES (?,?) "
                "ON CONFLICT(command) DO UPDATE SET channel_id = excluded.channel_id",
                (command, channel_id),
            )
    finally:
        conn.close()


def clear_command_channel(db_path: str, command: str) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("DELETE FROM eco_command_channels WHERE command = ?", (command,))
            return cur.rowcount > 0
    finally:
        conn.close()


def get_command_channel(db_path: str, command: str) -> Optional[int]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT channel_id FROM eco_command_channels WHERE command = ?", (command,)).fetchone()
        return int(row["channel_id"]) if row else None
    finally:
        conn.close()


def get_permissions_overview(db_path: str) -> Dict[str, Dict[str, object]]:
    """{comando: {'roles': set[int], 'channel': Optional[int]}} para todos los comandos económicos."""
    return {
        cmd: {"roles": get_command_roles(db_path, cmd), "channel": get_command_channel(db_path, cmd)}
        for cmd in ECON_COMMANDS
    }


# ============================================================
# Autorización (función pura: se prueba sin Discord)
# ============================================================
@dataclass
class AuthResult:
    ok: bool
    reason: str = ""


def authorize(
    *,
    command: str,
    is_admin: bool,
    member_role_ids: Set[int],
    allowed_role_ids: Set[int],
    channel_id: int,
    configured_channel_id: Optional[int],
) -> AuthResult:
    """Regla: (rol autorizado o Administrator/propietario) Y canal configurado para ese comando.
    Excepción: addmoney, addmoney-role y remove-money se pueden usar en cualquier canal.

    El canal se exige SIEMPRE, también a los administradores. Si el comando no tiene canal configurado
    queda deshabilitado para todos hasta que un administrador use /perms-canal.
    """
    if not (is_admin or (member_role_ids & allowed_role_ids)):
        return AuthResult(False, "No tenés permiso para usar este comando.")
    if command in ANY_CHANNEL_COMMANDS:
        return AuthResult(True)
    if configured_channel_id is None:
        return AuthResult(False, f"`{command}` todavía no está habilitado en ningún canal. Un administrador debe usar `/perms-canal`.")
    if channel_id != configured_channel_id:
        return AuthResult(False, f"Este comando solo se puede usar en <#{configured_channel_id}>.")
    return AuthResult(True)
