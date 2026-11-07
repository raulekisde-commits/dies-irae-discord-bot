"""Duelos 1v1 (lógica y persistencia, sin Discord).

Funciona como un coinflip entre dos personas ya elegidas: quien reta pone su apuesta al crear el duelo; la persona retada
puede aceptar (pone lo mismo y se tira la moneda al instante) o rechazar (se devuelve todo). Sin límite de monto: el tope es
el balance de quien reta, y la persona retada tiene que tener al menos lo apostado.
"""
import random
import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional

import economy_db as eco
import provably_fair as pf
from dbutil import connect, utcnow_iso, write_tx

DURATION_SECONDS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS duels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER,
    challenger_id INTEGER NOT NULL,
    challenged_id INTEGER NOT NULL,
    stake         INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | done | declined | expired | cancelled
    created_at    TEXT NOT NULL,
    ends_at       REAL NOT NULL,
    resolved_at   TEXT,
    winner_id     INTEGER,
    side          TEXT
);
CREATE INDEX IF NOT EXISTS idx_duels_open ON duels(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_duels_msg ON duels(message_id);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(duels)")}
        if "penalty" not in cols:                 # duelo especial de Wujupón: lo que pierde el retado si rechaza o no responde
            conn.execute("ALTER TABLE duels ADD COLUMN penalty INTEGER NOT NULL DEFAULT 0")
        if "penalty_paid" not in cols:
            conn.execute("ALTER TABLE duels ADD COLUMN penalty_paid INTEGER NOT NULL DEFAULT 0")
    finally:
        conn.close()
    pf.init_schema(db_path)


class DuelError(ValueError):
    pass


@dataclass
class Duel:
    id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    challenger_id: int
    challenged_id: int
    stake: int
    status: str
    ends_at: float
    winner_id: Optional[int]
    side: Optional[str]
    penalty: int = 0
    penalty_paid: int = 0
    pf_hash: Optional[str] = None            # hash SHA-256 de la semilla secreta (se publica en el panel)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


def _row(r) -> Optional[Duel]:
    if r is None:
        return None
    return Duel(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), int(r["message_id"]) if r["message_id"] is not None else None,
                int(r["challenger_id"]), int(r["challenged_id"]), int(r["stake"]), r["status"], float(r["ends_at"]),
                int(r["winner_id"]) if r["winner_id"] is not None else None, r["side"],
                int(r["penalty"]) if "penalty" in r.keys() else 0, int(r["penalty_paid"]) if "penalty_paid" in r.keys() else 0)


def get_duel(db_path: str, duel_id: int) -> Optional[Duel]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone())
    finally:
        conn.close()


def find_by_message(db_path: str, message_id: int) -> Optional[Duel]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM duels WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def set_message(db_path: str, duel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE duels SET message_id = ? WHERE id = ?", (message_id, duel_id))
    finally:
        conn.close()


def _balance(conn: sqlite3.Connection, user_id: int) -> int:
    r = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["balance"]) if r else 0


@dataclass
class CreateResult:
    ok: bool
    code: str = "ok"                 # ok | funds | target_funds
    duel: Optional[Duel] = None
    balance: int = 0                 # balance de quien no alcanza (code funds → retador; target_funds → retado)


def create_duel(db_path: str, *, guild_id: int, channel_id: int, challenger_id: int, challenged_id: int, stake: int,
                now: Optional[float] = None, duration: Optional[float] = None, penalty: int = 0) -> CreateResult:
    """`duration` y `penalty`: duelo especial de Wujupón (24 h para responder; si rechaza o no responde, el retado
    le paga `penalty` al retador)."""
    import time
    now = time.time() if now is None else now
    if challenger_id == challenged_id:
        raise DuelError("No podés retarte a vos mismo.")
    if not isinstance(stake, int) or stake <= 0 or stake > eco.MAX_AMOUNT:
        raise DuelError("La cantidad tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            mine, theirs = _balance(conn, challenger_id), _balance(conn, challenged_id)
            if mine < stake:
                return CreateResult(False, "funds", None, mine)
            if theirs < stake:
                return CreateResult(False, "target_funds", None, theirs)
            cur = conn.execute(
                "INSERT INTO duels(guild_id, channel_id, challenger_id, challenged_id, stake, status, created_at, ends_at, penalty) "
                "VALUES (?,?,?,?,?, 'open', ?, ?, ?)",
                (guild_id, channel_id, challenger_id, challenged_id, stake, utcnow_iso(), now + (duration or DURATION_SECONDS),
                 max(0, int(penalty))))
            did = int(cur.lastrowid)
            rel = f"duel:{did}"
            op = eco.tx_new_operation(conn, kind="remove", actor_id=challenger_id, request_key=f"duel:{did}:stake:{challenger_id}",
                                      total_amount=stake, target_count=1, reason=f"Duelo 1v1 #{did}: apuesta", related_id=rel)
            eco.tx_move(conn, op_id=op, user_id=challenger_id, delta=-stake, mov_type="duel_stake", reason=f"Duelo 1v1 #{did}: apuesta",
                        actor_id=challenger_id, related_id=rel, require_funds=True)
            duel = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (did,)).fetchone())
            duel.pf_hash = pf.tx_commit(conn, "duel", did)                 # el resultado queda fijado (y su hash publicado) desde ya
            return CreateResult(True, "ok", duel, mine - stake)
    finally:
        conn.close()


def abort_unpublished(db_path: str, duel_id: int) -> None:
    """Si no se pudo publicar el panel: devuelve la apuesta y deja el duelo cancelado."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            d = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone())
            if d is None or d.status != "open" or d.message_id is not None:
                return
            _refund(conn, d, "cancelled")
    finally:
        conn.close()


def _refund(conn: sqlite3.Connection, d: Duel, new_status: str) -> None:
    rel = f"duel:{d.id}"
    op = eco.tx_new_operation(conn, kind="add", actor_id=d.challenger_id, request_key=f"duel:{d.id}:refund",
                              total_amount=d.stake, target_count=1, reason=f"Duelo 1v1 #{d.id}: devolución", related_id=rel)
    eco.tx_move(conn, op_id=op, user_id=d.challenger_id, delta=d.stake, mov_type="duel_refund", reason=f"Duelo 1v1 #{d.id}: devolución",
                actor_id=d.challenger_id, related_id=rel)
    conn.execute("UPDATE duels SET status = ?, resolved_at = ? WHERE id = ?", (new_status, utcnow_iso(), d.id))
    if d.penalty > 0 and new_status in ("declined", "expired"):
        # duelo especial: el retado le paga la penalidad al retador (no lo deja en negativo)
        have = _balance(conn, d.challenged_id)
        pay = max(0, min(d.penalty, have))
        if pay:
            op_out = eco.tx_new_operation(conn, kind="remove", actor_id=d.challenged_id, request_key=f"duel:{d.id}:penalty:out",
                                          total_amount=pay, target_count=1, reason=f"Duelo 1v1 #{d.id}: penalidad por no aceptar",
                                          related_id=rel)
            eco.tx_move(conn, op_id=op_out, user_id=d.challenged_id, delta=-pay, mov_type="duel_penalty",
                        reason=f"Duelo 1v1 #{d.id}: penalidad por no aceptar", actor_id=d.challenged_id, related_id=rel)
            op_in = eco.tx_new_operation(conn, kind="add", actor_id=d.challenger_id, request_key=f"duel:{d.id}:penalty:in",
                                         total_amount=pay, target_count=1, reason=f"Duelo 1v1 #{d.id}: penalidad cobrada",
                                         related_id=rel)
            eco.tx_move(conn, op_id=op_in, user_id=d.challenger_id, delta=pay, mov_type="duel_penalty_in",
                        reason=f"Duelo 1v1 #{d.id}: penalidad cobrada", actor_id=d.challenger_id, related_id=rel)
        conn.execute("UPDATE duels SET penalty_paid = ? WHERE id = ?", (pay, d.id))


@dataclass
class ActionResult:
    status: str                      # ok | notfound | closed | notyou | funds
    duel: Optional[Duel] = None
    balance: int = 0
    winner_id: Optional[int] = None
    loser_id: Optional[int] = None
    side: Optional[str] = None       # lado que cayó: cara (retador) | cruz (retado)
    pool: int = 0
    proof: Optional[dict] = None     # semillas reveladas para verificar (provably fair)


def accept_duel(db_path: str, duel_id: int, user_id: int, now: Optional[float] = None,
                rng: Optional[random.Random] = None) -> ActionResult:
    import time
    now = time.time() if now is None else now
    fair = rng is None
    conn = connect(db_path)
    try:
        with write_tx(conn):
            d = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone())
            if d is None:
                return ActionResult("notfound")
            if d.status != "open" or now >= d.ends_at:
                return ActionResult("closed", d)
            if user_id != d.challenged_id:
                return ActionResult("notyou", d)
            have = _balance(conn, user_id)
            if have < d.stake:
                return ActionResult("funds", d, have)
            rel = f"duel:{d.id}"
            op = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"duel:{d.id}:stake:{user_id}",
                                      total_amount=d.stake, target_count=1, reason=f"Duelo 1v1 #{d.id}: apuesta", related_id=rel)
            eco.tx_move(conn, op_id=op, user_id=user_id, delta=-d.stake, mov_type="duel_stake", reason=f"Duelo 1v1 #{d.id}: apuesta",
                        actor_id=user_id, related_id=rel, require_funds=True)
            if fair:
                rng = pf.tx_rng(conn, "duel", d.id, [d.challenger_id, d.challenged_id])
            side = pf.coin(rng)
            winner = d.challenger_id if side == "cara" else d.challenged_id
            loser = d.challenged_id if winner == d.challenger_id else d.challenger_id
            pool = d.stake * 2
            op2 = eco.tx_new_operation(conn, kind="add", actor_id=winner, request_key=f"duel:{d.id}:win", total_amount=pool,
                                       target_count=1, reason=f"Duelo 1v1 #{d.id}: premio", related_id=rel)
            _, after = eco.tx_move(conn, op_id=op2, user_id=winner, delta=pool, mov_type="duel_win", reason=f"Duelo 1v1 #{d.id}: premio",
                                   actor_id=winner, related_id=rel)
            conn.execute("UPDATE duels SET status = 'done', resolved_at = ?, winner_id = ?, side = ? WHERE id = ?",
                         (utcnow_iso(), winner, side, d.id))
            proof = pf.tx_finish(conn, "duel", d.id, {"side": side}) if fair else None
            d2 = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (d.id,)).fetchone())
            return ActionResult("ok", d2, after, winner, loser, side, pool, proof)
    except eco.InsufficientFunds:
        return ActionResult("funds", get_duel(db_path, duel_id))
    finally:
        conn.close()


def decline_duel(db_path: str, duel_id: int, user_id: int) -> ActionResult:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            d = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone())
            if d is None:
                return ActionResult("notfound")
            if d.status != "open":
                return ActionResult("closed", d)
            if user_id != d.challenged_id:
                return ActionResult("notyou", d)
            _refund(conn, d, "declined")
            return ActionResult("ok", _row(conn.execute("SELECT * FROM duels WHERE id = ?", (d.id,)).fetchone()))
    finally:
        conn.close()


def due_duels(db_path: str, now: Optional[float] = None) -> List[int]:
    import time
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute("SELECT id FROM duels WHERE status = 'open' AND ends_at <= ? ORDER BY id", (now,))]
    finally:
        conn.close()


def expire_duel(db_path: str, duel_id: int, now: Optional[float] = None) -> Optional[Duel]:
    """Devuelve la apuesta del retador si nadie respondió. None si ya estaba resuelto o todavía no vence."""
    import time
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        with write_tx(conn):
            d = _row(conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone())
            if d is None or d.status != "open" or now < d.ends_at:
                return None
            _refund(conn, d, "expired")
            return _row(conn.execute("SELECT * FROM duels WHERE id = ?", (d.id,)).fetchone())
    finally:
        conn.close()
