"""Contadores de apuestas por persona (sin tablas nuevas: se calculan desde el historial de movimientos).

Cuentan SOLO juegos terminados con resultado (apuestas resueltas, dados, coinflip y duelos 1v1 finalizados). Lo cancelado o devuelto
no cuenta, y lo que todavía está en juego tampoco.
- apostado: suma de todas las entradas/apuestas puestas.
- ganado:   ganancia NETA de los juegos en que salió ganando (premio − lo que puso).
- perdido:  pérdida NETA de los juegos en que salió perdiendo (lo que puso − lo que recuperó).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import sqlite3

import bets_db as bdb
import economy_db as eco
import duels_db as ddl
import games_db as gdb
from dbutil import connect, utcnow_iso, write_tx

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


RESET_SCHEMA = """
CREATE TABLE IF NOT EXISTS stats_reset (
    user_id  INTEGER PRIMARY KEY,
    reset_at TEXT NOT NULL
);
"""


def init_schema(db_path: str) -> None:
    bdb.init_schema(db_path)
    gdb.init_schema(db_path)
    ddl.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(RESET_SCHEMA)
    finally:
        conn.close()


def reset_user(db_path: str, user_id: int) -> None:
    """Reinicia las estadísticas de una persona (cuando sale del servidor): lo jugado hasta ahora deja de contar en tops y
    winrate. El historial de movimientos NO se borra (queda para auditoría); solo se ignora lo anterior a este momento."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO stats_reset(user_id, reset_at) VALUES (?, ?)", (user_id, utcnow_iso()))
    finally:
        conn.close()


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
          LEFT JOIN stats_reset r ON r.user_id = m.user_id
         WHERE m.kind IN ({results})
           AND (b.status = 'resolved' OR g.status = 'done' OR d.status = 'done')
           AND (r.reset_at IS NULL OR m.created_at > r.reset_at)
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
    - 'gain' / 'loss': Top ganado (solo positivos) y Top perdido (solo negativos, el que más perdió primero).
    - 'won' / 'lost': solo quienes tienen algo en ese campo.
    """
    if field not in ("won", "lost", "wagered", "net", "gain", "loss"):
        raise ValueError("campo inválido")
    if field == "gain":                    # Top ganado: solo quienes están en positivo (ganado − perdido > 0), de mayor a menor
        rows = [s for s in all_stats(db_path) if s.net > 0]
        rows.sort(key=lambda s: (-s.net, s.user_id))
        return rows
    if field == "loss":                    # Top perdido: solo quienes están en negativo, del que más perdió al que menos
        rows = [s for s in all_stats(db_path) if s.net < 0]
        rows.sort(key=lambda s: (s.net, s.user_id))
        return rows
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
              LEFT JOIN stats_reset r ON r.user_id = p.user_id
              WHERE g.kind = ? AND g.status = 'done' AND g.winner_id IS NOT NULL
                AND (r.reset_at IS NULL OR g.resolved_at > r.reset_at) {flt}
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


def top_winrate_combined(db_path: str, min_games: int = MIN_GAMES_FOR_TOP) -> List[WinRate]:
    """Winrate general (dados + coinflip juntos)."""
    acc = {}
    for kind in ("dice", "coinflip"):
        for w in _winrates(db_path, kind, None):
            cur = acc.setdefault(w.user_id, WinRate(w.user_id, "all"))
            cur.played += w.played
            cur.wins += w.wins
    rows = [w for w in acc.values() if w.played >= min_games]
    rows.sort(key=lambda w: (-w.rate, -w.played, w.user_id))
    return rows


# ============================================================
# Total apostado en todo el servidor
# ============================================================
_REFUNDS = {"bet_stake": "bet_refund", "dice_stake": "dice_refund", "coinflip_stake": "coinflip_refund", "duel_stake": "duel_refund"}


@dataclass
class ServerWagered:
    by_kind: dict            # tipo de juego → (monto, cantidad de entradas)
    players: int = 0

    @property
    def total(self) -> int:
        return sum(a for a, _n in self.by_kind.values())

    @property
    def entries(self) -> int:
        return sum(n for _a, n in self.by_kind.values())


def server_wagered(db_path: str) -> ServerWagered:
    """TODO lo que se puso en juego desde siempre (apuestas, dados, coinflip y duelos), también lo que sigue en juego, descontando lo
    que se devolvió (juegos cancelados o sin rival). No depende de los reinicios individuales de estadísticas."""
    kinds = tuple(_REFUNDS) + tuple(_REFUNDS.values())
    sql = f"""SELECT kind, SUM(delta) AS total, COUNT(*) AS n, COUNT(DISTINCT user_id) AS u
                FROM eco_movements WHERE kind IN ({",".join("?" * len(kinds))}) GROUP BY kind"""

    def once():
        conn = connect(db_path)
        try:
            rows = {r["kind"]: r for r in conn.execute(sql, kinds)}
            users = conn.execute(
                f"SELECT COUNT(DISTINCT user_id) FROM eco_movements WHERE kind IN ({','.join('?' * len(_REFUNDS))})", tuple(_REFUNDS)).fetchone()[0]
            return rows, int(users or 0)
        finally:
            conn.close()

    try:
        rows, users = once()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        init_schema(db_path)
        rows, users = once()
    out = {}
    for stake, refund in _REFUNDS.items():
        st, rf = rows.get(stake), rows.get(refund)
        amount = -(int(st["total"]) if st else 0) - (int(rf["total"]) if rf else 0)
        out[stake] = (max(amount, 0), int(st["n"]) if st else 0)
    return ServerWagered(out, users)


# ============================================================
# Niveles de ludópata (según lo apostado en total) — se calculan al consultar, no se guardan: lo apostado ya es dato guardado
# ============================================================
@dataclass(frozen=True)
class Level:
    min_wagered: int
    icon: str
    name: str


# Rangos pensados con el top real a los 3-4 días (el 1.º ~1.900 M, el 10.º ~295 M): el top 10 cae entre «Ludópata» y «Rey».
# Para ajustarlos alcanza con tocar esta lista (de menor a mayor).
LEVELS: Tuple[Level, ...] = (
    Level(1_000_000, "🎲", "Curioso"),
    Level(5_000_000, "🎰", "Aficionado"),
    Level(25_000_000, "🃏", "Apostador"),
    Level(100_000_000, "💸", "Viciado"),
    Level(250_000_000, "🔥", "Ludópata"),
    Level(600_000_000, "💀", "Ludópata terminal"),
    Level(1_000_000_000, "👑", "Rey de la ludopatía"),
)


def gambler_level(wagered: int) -> Tuple[Optional[Level], Optional[Level]]:
    """(nivel actual, nivel siguiente) según lo apostado. El actual es None si todavía no llegó al primero; el siguiente es None en el último."""
    current = None
    for lv in LEVELS:
        if wagered >= lv.min_wagered:
            current = lv
    nxt = next((lv for lv in LEVELS if lv.min_wagered > wagered), None)
    return current, nxt


# ============================================================
# Rachas de coinflip (victorias seguidas). Se leen del historial de partidas, que no se borra: no hace falta otra tabla.
# ============================================================
@dataclass
class Streak:
    user_id: int
    current: int = 0
    best: int = 0
    played: int = 0


MIN_STREAK_FOR_TOP = 2


def _streaks(db_path: str, user_id: Optional[int]) -> Dict[int, Streak]:
    sql = """SELECT p.user_id AS u, CASE WHEN g.winner_id = p.user_id THEN 1 ELSE 0 END AS won
               FROM game_players p JOIN games g ON g.id = p.game_id
               LEFT JOIN stats_reset r ON r.user_id = p.user_id
              WHERE g.kind = 'coinflip' AND g.status = 'done' AND g.winner_id IS NOT NULL
                AND (r.reset_at IS NULL OR g.resolved_at > r.reset_at) {flt}
              ORDER BY g.id"""
    sql = sql.format(flt="AND p.user_id = ?" if user_id is not None else "")
    params = [user_id] if user_id is not None else []

    def once():
        conn = connect(db_path)
        try:
            return [(int(r["u"]), int(r["won"])) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    try:
        rows = once()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        init_schema(db_path)
        rows = once()
    out: Dict[int, Streak] = {}
    for uid, won in rows:
        st = out.setdefault(uid, Streak(uid))
        st.played += 1
        st.current = st.current + 1 if won else 0
        st.best = max(st.best, st.current)
    return out


def get_streak(db_path: str, user_id: int) -> Streak:
    """Racha de victorias seguidas en coinflip: la actual (se corta con cada derrota) y la mejor de siempre."""
    return _streaks(db_path, user_id).get(user_id, Streak(user_id))


def top_streaks(db_path: str, min_best: int = MIN_STREAK_FOR_TOP) -> List[Streak]:
    """Ranking por mejor racha (desempata por racha actual, luego por menos partidas jugadas y por id)."""
    rows = [s for s in _streaks(db_path, None).values() if s.best >= min_best]
    rows.sort(key=lambda s: (-s.best, -s.current, s.played, s.user_id))
    return rows


# ============================================================
# Historial del casino (/casino ver-historial)
# ============================================================
HISTORY_LIMIT = 200                                   # partidas más recientes que se muestran como máximo
DAILY_LIMIT = 10                                      # en el historial del día se listan las últimas 10 (el resumen cuenta todas)


def day_start_utc() -> str:
    """Comienzo del día actual en UTC (00:00 UTC), en ISO: el historial diario cuenta desde ahí."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass
class HistoryEntry:
    when: str                                         # ISO UTC de cuando se resolvió
    game: str                                         # coinflip | dice | duel | roulette
    ref_id: int                                       # número de partida / duelo / ronda
    stake: int                                        # lo que apostó la persona
    net: int                                          # + ganó / - perdió (neto)
    rivals: List[int]                                 # contra quién jugó (vacío en la ruleta: es contra la casa)


@dataclass
class HeadToHead:
    a: int
    b: int
    entries: List[HistoryEntry]                       # desde el punto de vista de `a`
    a_wins: int = 0
    b_wins: int = 0
    a_net: int = 0                                    # lo que `a` le ganó (o perdió, si es negativo) a `b`


def _history_rows(conn, user_id: int) -> List[HistoryEntry]:
    out: List[HistoryEntry] = []
    sql = """SELECT g.id, g.kind, g.stake, g.payout, g.winner_id, g.resolved_at,
                    (SELECT group_concat(p2.user_id) FROM game_players p2 WHERE p2.game_id = g.id AND p2.user_id != ?) AS others
               FROM game_players p JOIN games g ON g.id = p.game_id
              WHERE p.user_id = ? AND g.status = 'done' AND g.winner_id IS NOT NULL"""
    for r in conn.execute(sql, (user_id, user_id)):
        won = int(r["winner_id"]) == user_id
        net = int(r["payout"] or 0) - int(r["stake"]) if won else -int(r["stake"])
        others = [int(x) for x in (r["others"] or "").split(",") if x]
        out.append(HistoryEntry(r["resolved_at"], r["kind"], int(r["id"]), int(r["stake"]), net, others))
    sql = """SELECT id, challenger_id, challenged_id, stake, winner_id, resolved_at FROM duels
              WHERE status = 'done' AND winner_id IS NOT NULL AND ? IN (challenger_id, challenged_id)"""
    for r in conn.execute(sql, (user_id,)):
        rival = int(r["challenged_id"]) if int(r["challenger_id"]) == user_id else int(r["challenger_id"])
        stake = int(r["stake"])
        out.append(HistoryEntry(r["resolved_at"], "duel", int(r["id"]), stake, stake if int(r["winner_id"]) == user_id else -stake, [rival]))
    try:
        sql = """SELECT r.id, r.resolved_at, SUM(b.amount) AS staked, SUM(b.payout) AS paid
                   FROM roulette_bets b JOIN roulette_rounds r ON r.id = b.round_id
                  WHERE b.user_id = ? AND r.status = 'done' GROUP BY r.id"""
        for r in conn.execute(sql, (user_id,)):
            out.append(HistoryEntry(r["resolved_at"], "roulette", int(r["id"]), int(r["staked"]), int(r["paid"]) - int(r["staked"]), []))
    except sqlite3.OperationalError as exc:              # base sin ruleta todavía
        if "no such table" not in str(exc):
            raise
    out.sort(key=lambda e: (e.when or "", e.ref_id), reverse=True)
    return out


def history(db_path: str, user_id: int, limit: Optional[int] = HISTORY_LIMIT, since: Optional[str] = None) -> List[HistoryEntry]:
    """Partidas terminadas de una persona (coinflip, dados, 1v1 y ruleta), de la más nueva a la más vieja.
    Las devueltas o canceladas no aparecen: no hubo ganador ni perdedor. `since` (ISO UTC) filtra desde ese momento."""
    conn = connect(db_path)
    try:
        rows = [e for e in _history_rows(conn, user_id) if since is None or (e.when or "") >= since]
        return rows[:limit] if limit else rows
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    finally:
        conn.close()


def head_to_head(db_path: str, a: int, b: int, limit: int = HISTORY_LIMIT, since: Optional[str] = None) -> HeadToHead:
    """Partidas donde jugaron SOLO esas dos personas (coinflip, 1v1 y dados de a dos), vistas desde `a`."""
    conn = connect(db_path)
    try:
        rows = _history_rows(conn, a)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        rows = []
    finally:
        conn.close()
    mine = [e for e in rows if e.game != "roulette" and e.rivals == [b] and (since is None or (e.when or "") >= since)]
    h = HeadToHead(a, b, mine[:limit])
    for e in mine:                                    # el resumen cuenta todas, no solo las que se listan
        if e.net > 0:
            h.a_wins += 1
        else:
            h.b_wins += 1
        h.a_net += e.net
    return h
