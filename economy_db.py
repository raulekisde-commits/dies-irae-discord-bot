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


def format_amount(n: int) -> str:
    """150000 -> '150.000' | -150000 -> '-150.000' (siempre puntos, nunca comas)."""
    return f"{int(n):,}".replace(",", ".")


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


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
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
    _test_hook: Optional[Callable[[int, int], None]] = None,
) -> OperationResult:
    """Suma o resta `amount` a CADA usuario de `user_ids`, todo en UNA transacción.

    - Si algo falla a la mitad se revierte todo (no quedan balances parciales).
    - `request_key` es único: repetir la misma solicitud (reintento, doble evento) devuelve la operación
      ya aplicada con created=False en lugar de aplicarla otra vez.
    - Los balances pueden quedar negativos.
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
                       per_user_amount, target_count, total_amount, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'completed', ?)""",
                (op_id, request_key, kind, scope, actor_id, role_id, amount, len(ids), amount * len(ids), now),
            )
            for index, uid in enumerate(ids):
                conn.execute(
                    "INSERT OR IGNORE INTO eco_accounts(user_id, balance, created_at, updated_at) VALUES (?,0,?,?)",
                    (uid, now, now),
                )
                before = int(conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (uid,)).fetchone()["balance"])
                after = before + sign * amount
                if abs(after) > MAX_BALANCE_ABS:
                    raise OverflowError("El balance resultante excede el límite permitido.")
                conn.execute("UPDATE eco_accounts SET balance = ?, updated_at = ? WHERE user_id = ?", (after, now, uid))
                conn.execute(
                    """INSERT INTO eco_movements(op_id, user_id, delta, balance_before, balance_after, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (op_id, uid, sign * amount, before, after, now),
                )
                if _test_hook is not None:
                    _test_hook(index, uid)
            row = conn.execute("SELECT * FROM eco_operations WHERE op_id = ?", (op_id,)).fetchone()
            return _load_result(conn, row, created=True)
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

    El canal se exige SIEMPRE, también a los administradores. Si el comando no tiene canal configurado
    queda deshabilitado para todos hasta que un administrador use /perms-canal.
    """
    if not (is_admin or (member_role_ids & allowed_role_ids)):
        return AuthResult(False, "No tenés permiso para usar este comando.")
    if configured_channel_id is None:
        return AuthResult(False, f"`{command}` todavía no está habilitado en ningún canal. Un administrador debe usar `/perms-canal`.")
    if channel_id != configured_channel_id:
        return AuthResult(False, f"Este comando solo se puede usar en <#{configured_channel_id}>.")
    return AuthResult(True)
