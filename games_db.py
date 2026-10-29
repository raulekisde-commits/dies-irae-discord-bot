"""Juegos con Wuju Coins — /dados y /coinflip (lógica y persistencia, sin Discord).

Reglas de dinero (todas dentro de UNA transacción por acción, sobre `eco_accounts`):
- Entrar/crear = se descuenta la entrada en el mismo momento en que se registra la persona.
- Resolver = el pozo completo va al ganador; si no hubo rival, se devuelve cada entrada.
- Un juego se resuelve una sola vez (la transición de estado `open` → `done/refunded` ocurre dentro de la transacción).
- Solo puede haber un juego vivo de cada tipo por servidor (índice único parcial en la base).
"""
import json
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

MAX_STAKE = 5_000_000
DURATION_SECONDS = 300            # 5 minutos
KINDS = ("dice", "coinflip")
COMMAND_OF = {"dice": "dados", "coinflip": "coinflip"}
MOV = {"dice": ("dice_stake", "dice_win", "dice_refund"), "coinflip": ("coinflip_stake", "coinflip_win", "coinflip_refund")}
MAX_TIE_ROUNDS = 50               # tope de seguridad (la probabilidad de llegar es astronómicamente baja)

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                  -- dice | coinflip
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    creator_id  INTEGER NOT NULL,
    stake       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | done | refunded | cancelled
    created_at  TEXT NOT NULL,
    ends_at     REAL NOT NULL,
    resolved_at TEXT,
    winner_id   INTEGER,
    payout      INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_games_live ON games(guild_id, kind) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_games_message ON games(message_id);

CREATE TABLE IF NOT EXISTS game_players (
    game_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (game_id, user_id),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
);
"""


class GameError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


def init_schema(db_path: str) -> None:
    eco.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def validate_stake(amount: int) -> int:
    if amount < 1:
        raise GameError("La cantidad tiene que ser mayor a 0.")
    if amount > MAX_STAKE:
        raise GameError(f"El máximo por entrada es {eco.format_amount(MAX_STAKE)}.")
    return amount


# ============================================================
# Modelo
# ============================================================
@dataclass
class Game:
    id: int
    kind: str
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    creator_id: int
    stake: int
    status: str
    ends_at: float
    winner_id: Optional[int]
    payout: int
    outcome: Optional[dict]
    players: List[int] = field(default_factory=list)

    @property
    def pool(self) -> int:
        return self.stake * len(self.players)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


def _load(conn: sqlite3.Connection, game_id: int) -> Optional[Game]:
    r = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return _build(conn, r)


def _build(conn: sqlite3.Connection, r) -> Optional[Game]:
    if r is None:
        return None
    players = [int(x["user_id"]) for x in conn.execute(
        "SELECT user_id FROM game_players WHERE game_id = ? ORDER BY rowid", (r["id"],))]
    return Game(int(r["id"]), r["kind"], int(r["guild_id"]), int(r["channel_id"]),
                int(r["message_id"]) if r["message_id"] is not None else None, int(r["creator_id"]), int(r["stake"]),
                r["status"], float(r["ends_at"]), int(r["winner_id"]) if r["winner_id"] is not None else None,
                int(r["payout"]), json.loads(r["outcome"]) if r["outcome"] else None, players)


def get_game(db_path: str, game_id: int) -> Optional[Game]:
    conn = connect(db_path)
    try:
        return _load(conn, game_id)
    finally:
        conn.close()


def find_by_message(db_path: str, message_id: int) -> Optional[Game]:
    conn = connect(db_path)
    try:
        return _build(conn, conn.execute("SELECT * FROM games WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def live_game(db_path: str, guild_id: int, kind: str) -> Optional[Game]:
    conn = connect(db_path)
    try:
        return _build(conn, conn.execute(
            "SELECT * FROM games WHERE guild_id = ? AND kind = ? AND status = 'open'", (guild_id, kind)).fetchone())
    finally:
        conn.close()


def due_games(db_path: str, now: Optional[float] = None) -> List[int]:
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM games WHERE status = 'open' AND ends_at <= ? ORDER BY id", (now,))]
    finally:
        conn.close()


# ============================================================
# Dinero (dentro de la transacción en curso)
# ============================================================
def _debit(conn, game_id: int, kind: str, user_id: int, stake: int, label: str) -> int:
    related = f"game:{game_id}"
    reason = f"{label} #{game_id}"
    op = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"game:{game_id}:stake:{user_id}:{uuid.uuid4().hex}",
                              total_amount=stake, target_count=1, reason=reason, related_id=related)
    _, after = eco.tx_move(conn, op_id=op, user_id=user_id, delta=-stake, mov_type=MOV[kind][0], reason=reason,
                           actor_id=user_id, related_id=related, require_funds=True)
    return after


def _credit(conn, game_id: int, kind: str, user_ids: List[int], amount: int, which: int, label: str) -> None:
    related = f"game:{game_id}"
    reason = f"{label} #{game_id}"
    op = eco.tx_new_operation(conn, kind="add", actor_id=0, request_key=f"game:{game_id}:pay:{uuid.uuid4().hex}",
                              total_amount=amount * len(user_ids), target_count=len(user_ids), reason=reason, related_id=related)
    for uid in user_ids:
        eco.tx_move(conn, op_id=op, user_id=uid, delta=amount, mov_type=MOV[kind][which], reason=reason, actor_id=0, related_id=related)


LABEL = {"dice": "Dados", "coinflip": "Coinflip"}


# ============================================================
# Crear / anotarse
# ============================================================
@dataclass
class CreateResult:
    ok: bool
    code: str = ""                  # live | funds
    game: Optional[Game] = None
    balance: int = 0


def create_game(db_path: str, *, kind: str, guild_id: int, channel_id: int, creator_id: int, stake: int,
                now: Optional[float] = None) -> CreateResult:
    if kind not in KINDS:
        raise ValueError("tipo inválido")
    validate_stake(stake)
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        try:
            with write_tx(conn):
                live = _build(conn, conn.execute(
                    "SELECT * FROM games WHERE guild_id = ? AND kind = ? AND status = 'open'", (guild_id, kind)).fetchone())
                if live is not None:
                    return CreateResult(False, "live", live)
                cur = conn.execute(
                    "INSERT INTO games(kind, guild_id, channel_id, creator_id, stake, status, created_at, ends_at) "
                    "VALUES (?,?,?,?,?, 'open', ?, ?)", (kind, guild_id, channel_id, creator_id, stake, utcnow_iso(), now + DURATION_SECONDS))
                gid = int(cur.lastrowid)
                _debit(conn, gid, kind, creator_id, stake, LABEL[kind])
                conn.execute("INSERT INTO game_players(game_id, user_id, joined_at) VALUES (?,?,?)", (gid, creator_id, utcnow_iso()))
                return CreateResult(True, game=_load(conn, gid))
        except eco.InsufficientFunds as exc:
            return CreateResult(False, "funds", balance=int(exc.args[0]))
        except sqlite3.IntegrityError:                  # carrera: otro juego del mismo tipo se creó justo antes
            return CreateResult(False, "live", live_game(db_path, guild_id, kind))
    finally:
        conn.close()


def set_message(db_path: str, game_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE games SET message_id = ? WHERE id = ?", (message_id, game_id))
    finally:
        conn.close()


def abort_unpublished(db_path: str, game_id: int) -> bool:
    """Si el panel no se pudo publicar: devuelve la entrada y libera el cupo del juego."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            g = _load(conn, game_id)
            if g is None or g.status != "open" or g.message_id is not None:
                return False
            _credit(conn, g.id, g.kind, g.players, g.stake, 2, LABEL[g.kind])
            conn.execute("UPDATE games SET status = 'cancelled', resolved_at = ? WHERE id = ?", (utcnow_iso(), g.id))
            return True
    finally:
        conn.close()


@dataclass
class ResolveResult:
    status: str                      # done | refunded
    game: Game
    winner_id: Optional[int] = None
    pool: int = 0
    rounds: List[List[Tuple[int, int]]] = field(default_factory=list)     # dados: [(usuario, tirada)] por ronda
    coin_side: Optional[str] = None                                       # coinflip: "cara" | "cruz"
    note: str = ""


@dataclass
class JoinResult:
    status: str                      # ok | notfound | closed | already | own | full | funds
    game: Optional[Game] = None
    balance: int = 0
    resolved: Optional[ResolveResult] = None


def join_game(db_path: str, game_id: int, user_id: int, now: Optional[float] = None,
              rng: Optional[random.Random] = None) -> JoinResult:
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        try:
            with write_tx(conn):
                g = _load(conn, game_id)
                if g is None:
                    return JoinResult("notfound")
                if g.status != "open" or now >= g.ends_at:
                    return JoinResult("closed", g)
                if user_id in g.players:
                    return JoinResult("own" if (g.kind == "coinflip" and user_id == g.creator_id) else "already", g)
                if g.kind == "coinflip" and len(g.players) >= 2:
                    return JoinResult("full", g)
                after = _debit(conn, g.id, g.kind, user_id, g.stake, LABEL[g.kind])
                conn.execute("INSERT INTO game_players(game_id, user_id, joined_at) VALUES (?,?,?)", (g.id, user_id, utcnow_iso()))
                g = _load(conn, g.id)
                resolved = _resolve(conn, g, rng or random.SystemRandom()) if g.kind == "coinflip" else None
                return JoinResult("ok", _load(conn, g.id), after, resolved)
        except eco.InsufficientFunds as exc:
            return JoinResult("funds", balance=int(exc.args[0]))
    finally:
        conn.close()


# ============================================================
# Resolver
# ============================================================
def _roll_dice(players: List[int], rng: random.Random) -> Tuple[int, List[List[Tuple[int, int]]], str]:
    contenders = list(players)
    rounds: List[List[Tuple[int, int]]] = []
    for _ in range(MAX_TIE_ROUNDS):
        rolls = [(uid, rng.randint(1, 6)) for uid in contenders]
        rounds.append(rolls)
        top = max(r for _, r in rolls)
        tied = [uid for uid, r in rolls if r == top]
        if len(tied) == 1:
            return tied[0], rounds, ""
        contenders = tied
    return rng.choice(contenders), rounds, "Se llegó al máximo de desempates: se sorteó entre los empatados."


def _resolve(conn, g: Game, rng: random.Random) -> ResolveResult:
    """Resuelve un juego abierto dentro de la transacción en curso."""
    if len(g.players) < 2:
        _credit(conn, g.id, g.kind, g.players, g.stake, 2, LABEL[g.kind])
        conn.execute("UPDATE games SET status = 'refunded', resolved_at = ?, payout = 0 WHERE id = ?", (utcnow_iso(), g.id))
        return ResolveResult("refunded", _load(conn, g.id))
    pool = g.stake * len(g.players)
    rounds, note, side = [], "", None
    if g.kind == "dice":
        winner, rounds, note = _roll_dice(g.players, rng)
        outcome = {"rounds": [[[u, r] for u, r in rnd] for rnd in rounds], "note": note}
    else:
        side = "cara" if rng.randint(0, 1) == 0 else "cruz"                  # el creador es "cara"; quien acepta, "cruz"
        winner = g.players[0] if side == "cara" else g.players[1]
        outcome = {"side": side}
    _credit(conn, g.id, g.kind, [winner], pool, 1, LABEL[g.kind])
    conn.execute("UPDATE games SET status = 'done', resolved_at = ?, winner_id = ?, payout = ?, outcome = ? WHERE id = ?",
                 (utcnow_iso(), winner, pool, json.dumps(outcome), g.id))
    return ResolveResult("done", _load(conn, g.id), winner, pool, rounds, side, note)


def resolve_game(db_path: str, game_id: int, now: Optional[float] = None, rng: Optional[random.Random] = None) -> Optional[ResolveResult]:
    """Resuelve un juego vencido. None si ya estaba resuelto o todavía no vence (idempotente)."""
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        with write_tx(conn):
            g = _load(conn, game_id)
            if g is None or g.status != "open" or now < g.ends_at:
                return None
            return _resolve(conn, g, rng or random.SystemRandom())
    finally:
        conn.close()
