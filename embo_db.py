"""Balance de embo (embotelladas de Albion): solo contabilidad, separada de los Wuju Coins.

Una cuenta por persona y un historial de movimientos. Cada movimiento lleva `request_key` única, así un reintento o un
doble evento no aplica dos veces la misma operación. El balance puede quedar negativo (quitar o fijar desde /eco embo,
cartas malas del Wujupón), igual que los Wuju Coins.
"""
import sqlite3
from dataclasses import dataclass, field
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


def debtors(db_path: str, limit: int = 1000) -> List[Tuple[int, int]]:
    """[(discord_id, balance)] de quienes tienen el balance de embo en negativo, de la mayor deuda a la menor."""
    conn = connect(db_path)
    try:
        return [(int(r["user_id"]), int(r["balance"])) for r in
                conn.execute("SELECT user_id, balance FROM embo_accounts WHERE balance < 0 ORDER BY balance ASC, user_id LIMIT ?", (limit,))]
    finally:
        conn.close()


def last_loaded(db_path: str) -> Optional[Tuple[str, str, int]]:
    """Movimiento del log de embotelladas más reciente que se cargó: (fecha y hora del log UTC, cuándo se cargó, quién lo cargó).
    None si todavía no se cargó ningún archivo. Solo cuenta movimientos de jugadores registrados (los demás no se guardan)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT substr(request_key, 9, 19) AS mv, created_at, actor_id FROM embo_movements "
                         "WHERE request_key LIKE 'embomov:%' ORDER BY mv DESC, id DESC LIMIT 1").fetchone()
        return (r["mv"], r["created_at"], int(r["actor_id"])) if r else None
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


# ============================================================
# Importar movimientos del log de la guild (embotelladas depositadas / retiradas)
# ============================================================
@dataclass
class Movement:
    when: str            # "YYYY-MM-DD HH:MM:SS" (UTC, tal cual viene del log)
    player: str
    reason: str
    amount: int          # con signo: Deposit +, Withdrawal −

    @property
    def day(self) -> str:
        return self.when[:10]


@dataclass
class ImportResult:
    rows: int = 0                                   # movimientos considerados (después del filtro por día)
    applied: int = 0                                # cargados ahora
    duplicates: int = 0                             # ya estaban cargados (mismo archivo subido otra vez)
    users: dict = field(default_factory=dict)       # discord_id -> suma cargada
    not_found: dict = field(default_factory=dict)   # nick -> [cantidad de movimientos, suma]
    skipped_days: int = 0                           # filas de otros días (ignoradas)


HEADER_ALIASES = {
    "when": ("fecha y hora", "date", "fecha"),
    "player": ("jugador", "player"),
    "reason": ("motivo", "reason"),
    "amount": ("cantidad", "amount"),
}


def parse_movements(data: bytes) -> List[Movement]:
    """Lee el CSV/TSV del log de embotelladas. Acepta los dos formatos:
    `Fecha y hora | Jugador | Motivo | Cantidad | ...` (export propio) y `"Date" "Player" "Reason" "Amount"` (log de Albion).
    Separado por tabs, comas o punto y coma; UTF-8 o UTF-16. Lanza EmboError si no reconoce el encabezado."""
    import csv
    import io
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise EmboError("El archivo está vacío.")
    delim = "\t" if "\t" in lines[0] else ("," if lines[0].count(",") >= 3 else ";")
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    head = [(c or "").strip().strip('"').strip().casefold() for c in rows[0]]
    cols = {}
    for key, names in HEADER_ALIASES.items():
        idx = next((i for i, c in enumerate(head) if c in names), None)
        if idx is None:
            raise EmboError("No reconozco el encabezado: tiene que tener las columnas Fecha y hora / Jugador / Motivo / Cantidad "
                            "(o Date / Player / Reason / Amount).")
        cols[key] = idx
    out: List[Movement] = []
    for r in rows[1:]:
        cell = lambda k: (r[cols[k]] if len(r) > cols[k] else "").strip().strip('"').strip()
        when, player, reason, raw = cell("when"), cell("player"), cell("reason"), cell("amount")
        if not player or not when:
            continue
        try:
            amount = int(raw.replace(".", "").replace(",", "").replace(" ", ""))
        except ValueError:
            continue
        if amount == 0 or len(when) < 10:
            continue
        out.append(Movement(when=when[:19], player=player, reason=reason, amount=amount))
    return out


def import_movements(db_path: str, movements: List[Movement], name_to_user: dict, *, actor_id: int, kind: str,
                     only_day: Optional[str] = None) -> ImportResult:
    """Carga cada movimiento al balance de embo de quien tiene ese personaje registrado (name_to_user: nick casefold -> discord_id).
    Cada movimiento se identifica por fecha+jugador+motivo+cantidad (+ cuántas veces se repite igual en el archivo), así subir
    el mismo archivo dos veces, o el mismo día por el import histórico y por el diario, no lo carga de nuevo.
    Con only_day ("YYYY-MM-DD") se ignoran las filas de otros días. Todo en una sola transacción."""
    res = ImportResult()
    seen: dict = {}
    conn = connect(db_path)
    try:
        with write_tx(conn):
            for m in movements:
                if only_day and m.day != only_day:
                    res.skipped_days += 1
                    continue
                res.rows += 1
                base = f"{m.when}|{m.player.casefold()}|{m.reason.casefold()}|{m.amount}"
                seen[base] = seen.get(base, 0) + 1
                uid = name_to_user.get(m.player.casefold())
                if uid is None:
                    nf = res.not_found.setdefault(m.player, [0, 0])
                    nf[0] += 1
                    nf[1] += m.amount
                    continue
                ch = tx_apply(conn, user_id=uid, mode="add" if m.amount > 0 else "remove", amount=abs(m.amount), actor_id=actor_id,
                              request_key=f"embomov:{base}|{seen[base]}", reason=f"{m.reason or 'Movimiento'} {m.when} ({m.player})",
                              kind=kind)
                if ch.duplicate:
                    res.duplicates += 1
                    continue
                res.applied += 1
                res.users[uid] = res.users.get(uid, 0) + m.amount
        return res
    finally:
        conn.close()
