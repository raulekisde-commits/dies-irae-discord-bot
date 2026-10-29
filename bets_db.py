"""Apuestas (predicciones) sobre la economía de Wuju Coins — lógica y persistencia, sin Discord.

Reglas de oro:
- UN solo balance: el de economy_db (eco_accounts). Acá no existe ningún saldo propio.
- Cada operación que mueve dinero (apostar, pagar, devolver) ocurre dentro de UNA transacción
  BEGIN IMMEDIATE junto con el registro de la apuesta y el historial de movimientos: o se confirma todo o nada.
- El estado de la apuesta se cambia con una comprobación dentro de la misma transacción
  (`status` actual), por eso un pago o una devolución no pueden duplicarse aunque lleguen dos comandos a la vez.
- El pozo son exclusivamente las apuestas descontadas; los pagos se redondean hacia abajo con enteros.
"""
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_STATEMENT = 300
MAX_OPTION = 60
STATUSES = ("open", "closed", "resolved", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER,
    message_id       INTEGER,
    thread_id        INTEGER,
    statement        TEXT NOT NULL,
    creator_id       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','resolved','cancelled')),
    winner_idx       INTEGER,
    outcome          TEXT,
    distributed      INTEGER NOT NULL DEFAULT 0,
    remainder        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    closed_at        TEXT,
    resolved_at      TEXT,
    panel_rev        INTEGER NOT NULL DEFAULT 1,
    panel_synced_rev INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bets_thread  ON bets(thread_id);
CREATE INDEX IF NOT EXISTS idx_bets_message ON bets(message_id);
CREATE INDEX IF NOT EXISTS idx_bets_status  ON bets(status);
CREATE TABLE IF NOT EXISTS bet_options (
    bet_id    INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    label     TEXT NOT NULL,
    label_key TEXT NOT NULL,
    PRIMARY KEY (bet_id, idx),
    UNIQUE (bet_id, label_key)
);
CREATE TABLE IF NOT EXISTS bet_wagers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id     INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    option_idx INTEGER NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bet_wagers_bet ON bet_wagers(bet_id);
CREATE TABLE IF NOT EXISTS bet_payouts (
    bet_id  INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    amount  INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('payout','refund')),
    PRIMARY KEY (bet_id, user_id)
);
"""


class BetError(Exception):
    """Error de validación con mensaje apto para mostrar al usuario."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def init_schema(db_path: str) -> None:
    eco.init_schema(db_path)          # el historial y los balances viven en la economía
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


# ============================================================
# Parseo
# ============================================================
def normalize(text: str) -> str:
    """Compara ignorando mayúsculas y espacios sobrantes."""
    return " ".join((text or "").split()).casefold()


def parse_options(text: str) -> List[str]:
    parts = [" ".join(p.split()) for p in (text or "").split(",")]
    parts = [p for p in parts if p]
    seen: Set[str] = set()
    result: List[str] = []
    for p in parts:
        key = normalize(p)
        if key in seen:
            raise BetError(f"La opción **{p}** está repetida.")
        if len(p) > MAX_OPTION:
            raise BetError(f"Cada opción puede tener hasta {MAX_OPTION} caracteres.")
        seen.add(key)
        result.append(p)
    if len(result) < MIN_OPTIONS:
        raise BetError("Necesito al menos 2 opciones separadas por comas (ej: `Gana Smogg, Pierde Smogg, Empate`).")
    if len(result) > MAX_OPTIONS:
        raise BetError(f"Máximo {MAX_OPTIONS} opciones.")
    return result


def split_option_and_amount(text: str) -> Tuple[str, str]:
    """'Gana Smogg 5000' -> ('Gana Smogg', '5000'). La cantidad es siempre la última palabra."""
    parts = (text or "").split()
    if len(parts) < 2:
        raise BetError("Uso: `!apostar [opción] [cantidad]` (ej: `!apostar Gana Smogg 5000`).")
    return " ".join(parts[:-1]), parts[-1]


# ============================================================
# Modelo
# ============================================================
@dataclass
class Wager:
    user_id: int
    option_idx: int
    amount: int
    created_at: str


@dataclass
class Bet:
    id: int
    guild_id: int
    channel_id: Optional[int]
    message_id: Optional[int]
    thread_id: Optional[int]
    statement: str
    creator_id: int
    status: str
    winner_idx: Optional[int]
    outcome: Optional[str]
    distributed: int
    remainder: int
    created_at: str
    closed_at: Optional[str]
    resolved_at: Optional[str]
    panel_rev: int
    panel_synced_rev: int
    options: List[str] = field(default_factory=list)          # posición = idx
    wagers: List[Wager] = field(default_factory=list)
    payouts: Dict[int, int] = field(default_factory=dict)
    payout_kind: Optional[str] = None

    @property
    def pool(self) -> int:
        return sum(w.amount for w in self.wagers)

    @property
    def participants(self) -> Set[int]:
        return {w.user_id for w in self.wagers}

    def option_total(self, idx: int) -> int:
        return sum(w.amount for w in self.wagers if w.option_idx == idx)

    def option_participants(self, idx: int) -> Set[int]:
        return {w.user_id for w in self.wagers if w.option_idx == idx}

    def user_totals(self) -> Dict[int, Dict[int, int]]:
        """{user_id: {option_idx: monto acumulado}} (las apuestas sucesivas se acumulan)."""
        out: Dict[int, Dict[int, int]] = {}
        for w in self.wagers:
            out.setdefault(w.user_id, {})
            out[w.user_id][w.option_idx] = out[w.user_id].get(w.option_idx, 0) + w.amount
        return out

    def find_option(self, text: str) -> Optional[int]:
        key = normalize(text)
        for i, label in enumerate(self.options):
            if normalize(label) == key:
                return i
        return None

    @property
    def is_final(self) -> bool:
        return self.status in ("resolved", "cancelled")


def _load(conn: sqlite3.Connection, bet_id: int) -> Optional[Bet]:
    row = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if row is None:
        return None
    bet = Bet(
        id=int(row["id"]), guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        thread_id=int(row["thread_id"]) if row["thread_id"] is not None else None,
        statement=row["statement"], creator_id=int(row["creator_id"]), status=row["status"],
        winner_idx=int(row["winner_idx"]) if row["winner_idx"] is not None else None,
        outcome=row["outcome"], distributed=int(row["distributed"]), remainder=int(row["remainder"]),
        created_at=row["created_at"], closed_at=row["closed_at"], resolved_at=row["resolved_at"],
        panel_rev=int(row["panel_rev"]), panel_synced_rev=int(row["panel_synced_rev"]),
    )
    bet.options = [r["label"] for r in conn.execute("SELECT label FROM bet_options WHERE bet_id = ? ORDER BY idx", (bet_id,))]
    bet.wagers = [Wager(int(r["user_id"]), int(r["option_idx"]), int(r["amount"]), r["created_at"])
                  for r in conn.execute("SELECT * FROM bet_wagers WHERE bet_id = ? ORDER BY id", (bet_id,))]
    kinds = set()
    for r in conn.execute("SELECT user_id, amount, kind FROM bet_payouts WHERE bet_id = ?", (bet_id,)):
        bet.payouts[int(r["user_id"])] = int(r["amount"])
        kinds.add(r["kind"])
    bet.payout_kind = next(iter(kinds)) if len(kinds) == 1 else None
    return bet


def get_bet(db_path: str, bet_id: int) -> Optional[Bet]:
    conn = connect(db_path)
    try:
        return _load(conn, bet_id)
    finally:
        conn.close()


def find_by_thread(db_path: str, thread_id: int) -> Optional[Bet]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT id FROM bets WHERE thread_id = ?", (thread_id,)).fetchone()
        return _load(conn, int(row["id"])) if row else None
    finally:
        conn.close()


def find_by_message(db_path: str, message_id: int) -> Optional[Bet]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT id FROM bets WHERE message_id = ?", (message_id,)).fetchone()
        return _load(conn, int(row["id"])) if row else None
    finally:
        conn.close()


def list_bets(db_path: str, statuses: Tuple[str, ...] = ("open", "closed"), limit: int = 25) -> List[Bet]:
    conn = connect(db_path)
    try:
        marks = ",".join("?" for _ in statuses)
        ids = [int(r["id"]) for r in conn.execute(
            f"SELECT id FROM bets WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?", (*statuses, limit))]
        return [b for b in (_load(conn, i) for i in ids) if b]
    finally:
        conn.close()


# ============================================================
# Creación
# ============================================================
def create_bet(db_path: str, *, guild_id: int, channel_id: int, creator_id: int, statement: str, options: List[str]) -> int:
    statement = " ".join((statement or "").split())
    if not statement:
        raise BetError("El enunciado no puede estar vacío.")
    if len(statement) > MAX_STATEMENT:
        raise BetError(f"El enunciado puede tener hasta {MAX_STATEMENT} caracteres.")
    if not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
        raise BetError(f"Tiene que haber entre {MIN_OPTIONS} y {MAX_OPTIONS} opciones.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "INSERT INTO bets(guild_id, channel_id, statement, creator_id, created_at) VALUES (?,?,?,?,?)",
                (guild_id, channel_id, statement, creator_id, utcnow_iso()))
            bet_id = int(cur.lastrowid)
            for i, label in enumerate(options):
                conn.execute("INSERT INTO bet_options(bet_id, idx, label, label_key) VALUES (?,?,?,?)",
                             (bet_id, i, label, normalize(label)))
            return bet_id
    finally:
        conn.close()


def set_location(db_path: str, bet_id: int, *, message_id: Optional[int] = None, thread_id: Optional[int] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if message_id is not None:
                conn.execute("UPDATE bets SET message_id = ? WHERE id = ?", (message_id, bet_id))
            if thread_id is not None:
                conn.execute("UPDATE bets SET thread_id = ? WHERE id = ?", (thread_id, bet_id))
            conn.execute("UPDATE bets SET panel_rev = panel_rev + 1 WHERE id = ?", (bet_id,))
    finally:
        conn.close()


def abort_unpublished(db_path: str, bet_id: int) -> None:
    """Si el panel o el hilo no se pudieron crear, se cancela la apuesta (todavía no hay dinero movido)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            n = conn.execute("SELECT COUNT(*) c FROM bet_wagers WHERE bet_id = ?", (bet_id,)).fetchone()["c"]
            if n == 0:
                conn.execute("UPDATE bets SET status='cancelled', outcome='no_publicada', resolved_at=? WHERE id=? AND status='open'",
                             (utcnow_iso(), bet_id))
    finally:
        conn.close()


# ============================================================
# Resultados de las acciones
# ============================================================
@dataclass
class Result:
    ok: bool
    code: str = "ok"           # ok | not_found | state | permission | option | thread | funds | invalid | no_winner
    message: str = ""
    balance: Optional[int] = None
    payouts: Dict[int, int] = field(default_factory=dict)
    remainder: int = 0
    option_idx: Optional[int] = None


def _bump(conn: sqlite3.Connection, bet_id: int) -> None:
    conn.execute("UPDATE bets SET panel_rev = panel_rev + 1 WHERE id = ?", (bet_id,))


# ============================================================
# Apostar
# ============================================================
def place_wager(db_path: str, bet_id: int, user_id: int, option_text: str, amount: int, *, thread_id: Optional[int] = None) -> Result:
    """Descuenta `amount` del balance y registra la apuesta, TODO en una transacción."""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > eco.MAX_AMOUNT:
        return Result(False, "invalid", "La cantidad tiene que ser un entero positivo.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            bet = _load(conn, bet_id)
            if bet is None:
                return Result(False, "not_found", "Esa apuesta no existe.")
            if thread_id is not None and bet.thread_id != thread_id:
                return Result(False, "thread", "Solo podés apostar dentro del hilo de la apuesta.")
            if bet.status != "open":
                msg = {"closed": "La apuesta ya está cerrada: no se aceptan más apuestas.",
                       "resolved": "Esa apuesta ya fue resuelta.",
                       "cancelled": "Esa apuesta fue cancelada."}[bet.status]
                return Result(False, "state", msg)
            idx = bet.find_option(option_text)
            if idx is None:
                listing = " · ".join(f"`{o}`" for o in bet.options)
                return Result(False, "option", f"Esa opción no existe. Opciones: {listing}")

            label = bet.options[idx]
            related = f"bet:{bet_id}"
            reason = f"Apuesta #{bet_id}: {label}"
            op_id = eco.tx_new_operation(conn, kind="remove", actor_id=user_id,
                                         request_key=f"bet:{bet_id}:stake:{uuid.uuid4().hex}",
                                         total_amount=amount, target_count=1, reason=reason, related_id=related)
            try:
                _, after = eco.tx_move(conn, op_id=op_id, user_id=user_id, delta=-amount, mov_type="bet_stake",
                                       reason=reason, actor_id=user_id, related_id=related, require_funds=True)
            except eco.InsufficientFunds as exc:
                raise _Rollback(Result(False, "funds", "No tenés saldo suficiente.", balance=int(exc.args[0])))
            conn.execute("INSERT INTO bet_wagers(bet_id, user_id, option_idx, amount, created_at) VALUES (?,?,?,?,?)",
                         (bet_id, user_id, idx, amount, utcnow_iso()))
            _bump(conn, bet_id)
            return Result(True, balance=after, option_idx=idx)
    except _Rollback as rb:
        return rb.result
    finally:
        conn.close()


class _Rollback(Exception):
    """Fuerza ROLLBACK de la transacción y devuelve un resultado de rechazo."""

    def __init__(self, result: Result):
        super().__init__("rollback")
        self.result = result


# ============================================================
# Cerrar / resolver / cancelar
# ============================================================
def close_bet(db_path: str, bet_id: int, actor_id: int) -> Result:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            bet = _load(conn, bet_id)
            if bet is None:
                return Result(False, "not_found", "Esa apuesta no existe.")
            if actor_id != bet.creator_id:
                return Result(False, "permission", "Solo quien creó la apuesta puede cerrarla.")
            if bet.status != "open":
                return Result(False, "state", "La apuesta ya no está abierta.")
            conn.execute("UPDATE bets SET status='closed', closed_at=? WHERE id=? AND status='open'", (utcnow_iso(), bet_id))
            _bump(conn, bet_id)
            return Result(True)
    finally:
        conn.close()


def _refund_all(conn: sqlite3.Connection, bet: Bet, actor_id: int, reason: str) -> Dict[int, int]:
    """Devuelve a cada usuario TODO lo que apostó (una sola operación, un movimiento por usuario)."""
    totals: Dict[int, int] = {}
    for w in bet.wagers:
        totals[w.user_id] = totals.get(w.user_id, 0) + w.amount
    if not totals:
        return {}
    related = f"bet:{bet.id}"
    op_id = eco.tx_new_operation(conn, kind="add", actor_id=actor_id, request_key=f"bet:{bet.id}:refund",
                                 total_amount=sum(totals.values()), target_count=len(totals), reason=reason, related_id=related)
    for uid in sorted(totals):
        eco.tx_move(conn, op_id=op_id, user_id=uid, delta=totals[uid], mov_type="bet_refund", reason=reason,
                    actor_id=actor_id, related_id=related)
        conn.execute("INSERT INTO bet_payouts(bet_id, user_id, amount, kind) VALUES (?,?,?, 'refund')", (bet.id, uid, totals[uid]))
    return totals


def compute_payouts(pool: int, winner_stakes: Dict[int, int]) -> Tuple[Dict[int, int], int]:
    """Pago = apuesta_ganadora_individual * pozo // total_apostado_a_la_ganadora (enteros, hacia abajo).
    Devuelve ({usuario: pago}, sobrante). El sobrante nunca es negativo y la suma nunca supera el pozo."""
    total = sum(winner_stakes.values())
    if total <= 0:
        raise ValueError("sin apuestas ganadoras")
    payouts = {uid: (stake * pool) // total for uid, stake in winner_stakes.items()}
    remainder = pool - sum(payouts.values())
    assert remainder >= 0
    return payouts, remainder


def resolve_bet(db_path: str, bet_id: int, actor_id: int, option_idx: int) -> Result:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            bet = _load(conn, bet_id)
            if bet is None:
                return Result(False, "not_found", "Esa apuesta no existe.")
            if actor_id != bet.creator_id:
                return Result(False, "permission", "Solo quien creó la apuesta puede resolverla.")
            if bet.status == "resolved":
                return Result(False, "state", "Esa apuesta ya fue resuelta; no se paga dos veces.")
            if bet.status == "cancelled":
                return Result(False, "state", "Esa apuesta fue cancelada.")
            if bet.status == "open":
                return Result(False, "state", "Primero tenés que cerrar la apuesta (`/cerrarapuesta`).")
            if not (0 <= option_idx < len(bet.options)):
                return Result(False, "option", "Esa opción no existe.")

            now = utcnow_iso()
            pool = bet.pool
            winner_stakes = {uid: opts[option_idx] for uid, opts in bet.user_totals().items() if opts.get(option_idx)}
            if not winner_stakes:
                # Nadie apostó a la ganadora: se cancela y se devuelve todo.
                refunded = _refund_all(conn, bet, actor_id, f"Devolución apuesta #{bet_id}: sin ganadores")
                conn.execute("UPDATE bets SET status='cancelled', outcome='no_winner', winner_idx=?, resolved_at=? WHERE id=? AND status='closed'",
                             (option_idx, now, bet_id))
                _bump(conn, bet_id)
                return Result(False, "no_winner", "Nadie apostó a esa opción: la apuesta se canceló y se devolvió todo.",
                              payouts=refunded, option_idx=option_idx)

            payouts, remainder = compute_payouts(pool, winner_stakes)
            related = f"bet:{bet_id}"
            label = bet.options[option_idx]
            reason = f"Premio apuesta #{bet_id}: {label}"
            op_id = eco.tx_new_operation(conn, kind="add", actor_id=actor_id, request_key=f"bet:{bet_id}:payout",
                                         total_amount=sum(payouts.values()), target_count=len(payouts), reason=reason, related_id=related)
            for uid in sorted(payouts):
                if payouts[uid] > 0:
                    eco.tx_move(conn, op_id=op_id, user_id=uid, delta=payouts[uid], mov_type="bet_payout",
                                reason=reason, actor_id=actor_id, related_id=related)
                conn.execute("INSERT INTO bet_payouts(bet_id, user_id, amount, kind) VALUES (?,?,?, 'payout')", (bet_id, uid, payouts[uid]))
            cur = conn.execute(
                """UPDATE bets SET status='resolved', winner_idx=?, outcome='resolved', distributed=?, remainder=?, resolved_at=?
                   WHERE id=? AND status='closed'""", (option_idx, sum(payouts.values()), remainder, now, bet_id))
            if cur.rowcount != 1:
                raise RuntimeError("estado inesperado")          # revierte todo
            _bump(conn, bet_id)
            return Result(True, payouts=payouts, remainder=remainder, option_idx=option_idx)
    finally:
        conn.close()


def cancel_bet(db_path: str, bet_id: int, actor_id: int, *, is_admin: bool = False) -> Result:
    """Cancela una apuesta abierta o cerrada y devuelve a cada usuario el total apostado (una sola vez)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            bet = _load(conn, bet_id)
            if bet is None:
                return Result(False, "not_found", "Esa apuesta no existe.")
            if actor_id != bet.creator_id and not is_admin:
                return Result(False, "permission", "Solo quien creó la apuesta (o un administrador) puede cancelarla.")
            if bet.status == "resolved":
                return Result(False, "state", "Esa apuesta ya fue resuelta; no se puede cancelar.")
            if bet.status == "cancelled":
                return Result(False, "state", "Esa apuesta ya estaba cancelada; no se repiten las devoluciones.")
            refunded = _refund_all(conn, bet, actor_id, f"Devolución apuesta #{bet_id}: cancelada")
            cur = conn.execute("UPDATE bets SET status='cancelled', outcome='cancelled', resolved_at=? WHERE id=? AND status IN ('open','closed')",
                               (utcnow_iso(), bet_id))
            if cur.rowcount != 1:
                raise RuntimeError("estado inesperado")
            _bump(conn, bet_id)
            return Result(True, payouts=refunded)
    finally:
        conn.close()


# ============================================================
# Sincronización del panel (la base de datos es la fuente de verdad)
# ============================================================
def mark_synced(db_path: str, bet_id: int, rev: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE bets SET panel_synced_rev = MAX(panel_synced_rev, ?) WHERE id = ?", (rev, bet_id))
    finally:
        conn.close()


def mark_all_dirty(db_path: str) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE bets SET panel_rev = panel_rev + 1 WHERE status IN ('open','closed')")
    finally:
        conn.close()


def needing_sync(db_path: str) -> List[int]:
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM bets WHERE panel_rev != panel_synced_rev AND message_id IS NOT NULL")]
    finally:
        conn.close()
