"""Contadores de apuestas por persona (sin tablas nuevas: se calculan desde el historial de movimientos).

Cuentan SOLO juegos terminados con resultado (apuestas resueltas, dados, coinflip y duelos 1v1 finalizados). Lo cancelado o devuelto
no cuenta, y lo que todavía está en juego tampoco.
- apostado: suma de todas las entradas/apuestas puestas.
- ganado:   ganancia NETA de los juegos en que salió ganando (premio − lo que puso).
- perdido:  pérdida NETA de los juegos en que salió perdiendo (lo que puso − lo que recuperó).
"""
from dataclasses import dataclass
from typing import List, Optional

import sqlite3

import bets_db as bdb
import economy_db as eco
import duels_db as ddl
import games_db as gdb
from dbutil import connect

STAKE_TYPES = ("bet_stake", "dice_stake", "coinflip_stake", "duel_stake")
RESULT_TYPES = STAKE_TYPES + ("bet_payout", "dice_win", "coinflip_win", "duel_win")


@dataclass
class GamblerStats:
    user_id: int
    wagered: int = 0
    won: int = 0
    lost: int = 0
    games: int = 0

    @property
    def net(self) -> int:
        return self.won - self.lost


def init_schema(db_path: str) -> None:
    bdb.init_schema(db_path)
    gdb.init_schema(db_path)
    ddl.init_schema(db_path)


_SQL = """
SELECT user_id,
       SUM(staked) AS wagered,
       SUM(CASE WHEN net > 0 THEN net ELSE 0 END) AS won,
       SUM(CASE WHEN net < 0 THEN -net ELSE 0 END) AS lost,
       COUNT(*) AS games
  FROM (
        SELECT m.user_id AS user_id, m.related_id AS rel,
               SUM(CASE WHEN m.kind IN ({stakes}) THEN -m.delta ELSE 0 END) AS staked,
               SUM(m.delta) AS net
          FROM eco_movements m
          LEFT JOIN bets  b ON m.related_id = 'bet:'  || b.id
          LEFT JOIN games g ON m.related_id = 'game:' || g.id
          LEFT JOIN duels d ON m.related_id = 'duel:' || d.id
         WHERE m.kind IN ({results})
           AND (b.status = 'resolved' OR g.status = 'done' OR d.status = 'done')
           {user_filter}
         GROUP BY m.user_id, m.related_id
       )
 GROUP BY user_id
"""


def _query(db_path: str, user_id: Optional[int]) -> List[GamblerStats]:
    sql = _SQL.format(stakes=",".join("?" * len(STAKE_TYPES)), results=",".join("?" * len(RESULT_TYPES)),
                      user_filter="AND m.user_id = ?" if user_id is not None else "")
    params = list(STAKE_TYPES) + list(RESULT_TYPES) + ([user_id] if user_id is not None else [])
    def run_once() -> List[GamblerStats]:
        conn = connect(db_path)
        try:
            return [GamblerStats(int(r["user_id"]), int(r["wagered"]), int(r["won"]), int(r["lost"]), int(r["games"]))
                    for r in conn.execute(sql, params)]
        finally:
            conn.close()

    try:
        return run_once()
    except sqlite3.OperationalError as exc:            # base recién creada: todavía no existen las tablas de apuestas/juegos
        if "no such table" not in str(exc):
            raise
        init_schema(db_path)
        return run_once()


def get_user_stats(db_path: str, user_id: int) -> GamblerStats:
    rows = _query(db_path, user_id)
    return rows[0] if rows else GamblerStats(user_id)


def all_stats(db_path: str) -> List[GamblerStats]:
    return _query(db_path, None)


def top_by(db_path: str, field: str) -> List[GamblerStats]:
    """Ranking completo (mayor a menor).

    - 'wagered': lo apostado (quien tiene 0 no aparece).
    - 'net':     ganado − perdido; puede ser negativo. Aparece quien jugó al menos una partida terminada
                 (los negativos van al final).
    - 'won' / 'lost': solo quienes tienen algo en ese campo.
    """
    if field not in ("won", "lost", "wagered", "net"):
        raise ValueError("campo inválido")
    if field == "net":
        rows = [s for s in all_stats(db_path) if s.games > 0]
    else:
        rows = [s for s in all_stats(db_path) if getattr(s, field) > 0]
    rows.sort(key=lambda s: (-getattr(s, field), s.user_id))
    return rows


# ============================================================
# Winrate en dados y coinflip (solo partidas terminadas con ganador; las devueltas por falta de rival no cuentan)
# ============================================================
MIN_GAMES_FOR_TOP = 5            # para entrar a un top de winrate (evita que 1 de 1 = 100 %)


@dataclass
class WinRate:
    user_id: int
    kind: str
    played: int = 0
    wins: int = 0

    @property
    def rate(self) -> float:
        return self.wins / self.played if self.played else 0.0

    @property
    def percent(self) -> str:
        return f"{self.rate * 100:.1f}".replace(".0", "") + "%" if self.played else "—"


def _winrates(db_path: str, kind: str, user_id: Optional[int]) -> List[WinRate]:
    sql = """SELECT p.user_id AS u, COUNT(*) AS played, SUM(CASE WHEN g.winner_id = p.user_id THEN 1 ELSE 0 END) AS wins
               FROM game_players p JOIN games g ON g.id = p.game_id
              WHERE g.kind = ? AND g.status = 'done' AND g.winner_id IS NOT NULL {flt}
              GROUP BY p.user_id"""
    sql = sql.format(flt="AND p.user_id = ?" if user_id is not None else "")
    params = [kind] + ([user_id] if user_id is not None else [])

    def once():
        conn = connect(db_path)
        try:
            return [WinRate(int(r["u"]), kind, int(r["played"]), int(r["wins"] or 0)) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    try:
        return once()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        init_schema(db_path)
        return once()


def get_winrate(db_path: str, kind: str, user_id: int) -> WinRate:
    """kind: 'dice' | 'coinflip'."""
    rows = _winrates(db_path, kind, user_id)
    return rows[0] if rows else WinRate(user_id, kind)


def top_winrate(db_path: str, kind: str, min_games: int = MIN_GAMES_FOR_TOP) -> List[WinRate]:
    """Ranking completo por winrate (desempata por más partidas ganadas y luego por id)."""
    if kind not in ("dice", "coinflip"):
        raise ValueError("juego inválido")
    rows = [w for w in _winrates(db_path, kind, None) if w.played >= min_games]
    rows.sort(key=lambda w: (-w.rate, -w.played, w.user_id))
    return rows
