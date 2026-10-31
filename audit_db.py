"""Auditoría del casino (solo lectura, sin Discord): busca INDICIOS de trampa en lo ya jugado.

Qué mira:
1. Suerte anómala: por persona, cuántas partidas de dados/coinflip/duelos ganó contra las que se esperaba por azar (cada jugador de una
   partida de n personas tiene 1/n de ganar) y cuán improbable es la diferencia (z-score), más la racha de victorias más improbable.
2. Parejas que juegan casi solo entre sí en partidas de dos (coinflip, duelos, dados 1 contra 1) y hacia dónde fluye el dinero:
   así se ve el pasar coins entre cuentas «perdiendo a propósito».
3. Transferencias (/give-money): quién le pasa mucho a quién en un solo sentido y quién recibe de muchas cuentas distintas.

Son indicios para revisar a mano, NO pruebas: con muchas personas, algunas siempre salen raras por pura casualidad, y dos amigos
pueden jugar siempre entre ellos. Por eso se muestran los números y no se castiga a nadie automáticamente.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import sqlite3

from dbutil import connect

MIN_GAMES_LUCK = 20             # partidas mínimas para evaluar la suerte de alguien
Z_FLAG = 3.0                    # |z| a partir del cual se marca (≈ 1 en 370 por azar)
STREAK_PROB_FLAG = 0.0005       # racha de victorias con probabilidad menor a esto (≈ 11 seguidas en un coinflip)
MIN_PAIR_GAMES = 10
PAIR_CONCENTRATION = 0.6        # una de las dos personas juega ≥60 % de sus partidas de a dos contra la misma persona
PAIR_Z_FLAG = 3.0
MIN_TRANSFER_COUNT = 3
ONE_WAY_RATIO = 0.9
COLLECTOR_SENDERS = 4           # recibe de al menos tantas cuentas distintas


@dataclass
class Result:
    kind: str                   # dice | coinflip | duel
    ref: int
    at: str                     # ISO UTC
    stake: int
    players: List[int]
    winner: int


@dataclass
class LuckRow:
    user_id: int
    games: int
    wins: int
    expected: float
    z: float
    p_value: float


@dataclass
class StreakRow:
    user_id: int
    length: int
    probability: float


@dataclass
class PairRow:
    a: int
    b: int
    games: int
    wins_a: int
    wins_b: int
    flow_to_a: int              # monto neto que pasó de b hacia a por los resultados (negativo: de a hacia b)
    share_a: float
    share_b: float
    z: float


@dataclass
class TransferRow:
    sender: int
    receiver: int
    total: int
    count: int
    back_total: int             # lo que el receptor le devolvió al emisor


@dataclass
class CollectorRow:
    user_id: int
    senders: int
    total: int


@dataclass
class Report:
    since: Optional[str]
    games: int = 0
    luck: List[LuckRow] = field(default_factory=list)
    streaks: List[StreakRow] = field(default_factory=list)
    pairs: List[PairRow] = field(default_factory=list)
    transfers: List[TransferRow] = field(default_factory=list)
    collectors: List[CollectorRow] = field(default_factory=list)


# ------------------------------------------------------------
# Datos
# ------------------------------------------------------------
def collect_results(db_path: str, since_iso: Optional[str] = None) -> List[Result]:
    flt = " AND g.resolved_at >= ?" if since_iso else ""
    dflt = " AND d.resolved_at >= ?" if since_iso else ""
    out: List[Result] = []
    conn = connect(db_path)
    try:
        try:
            games = conn.execute(f"SELECT g.id, g.kind, g.stake, g.winner_id, g.resolved_at FROM games g "
                                 f"WHERE g.status = 'done' AND g.winner_id IS NOT NULL{flt} ORDER BY g.resolved_at, g.id",
                                 (since_iso,) if since_iso else ()).fetchall()
            for g in games:
                players = [int(r["user_id"]) for r in conn.execute("SELECT user_id FROM game_players WHERE game_id = ? ORDER BY joined_at", (g["id"],))]
                if len(players) >= 2:
                    out.append(Result(g["kind"], int(g["id"]), g["resolved_at"], int(g["stake"]), players, int(g["winner_id"])))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
        try:
            for d in conn.execute(f"SELECT d.id, d.stake, d.challenger_id, d.challenged_id, d.winner_id, d.resolved_at FROM duels d "
                                  f"WHERE d.status = 'done' AND d.winner_id IS NOT NULL{dflt} ORDER BY d.resolved_at, d.id",
                                  (since_iso,) if since_iso else ()):
                out.append(Result("duel", int(d["id"]), d["resolved_at"], int(d["stake"]),
                                  [int(d["challenger_id"]), int(d["challenged_id"])], int(d["winner_id"])))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
    finally:
        conn.close()
    out.sort(key=lambda r: (r.at or "", r.kind, r.ref))
    return out


def collect_transfers(db_path: str, since_iso: Optional[str] = None) -> List[Tuple[int, int, int]]:
    """(emisor, receptor, monto) de cada /give-money."""
    sql = "SELECT related_id, -delta AS amount FROM eco_movements WHERE kind = 'transfer_out' AND related_id LIKE 'give:%'"
    params: tuple = ()
    if since_iso:
        sql += " AND created_at >= ?"
        params = (since_iso,)
    conn = connect(db_path)
    try:
        rows = []
        for r in conn.execute(sql, params):
            parts = (r["related_id"] or "").split(":")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                rows.append((int(parts[1]), int(parts[2]), int(r["amount"])))
        return rows
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise
    finally:
        conn.close()


# ------------------------------------------------------------
# Análisis (puro)
# ------------------------------------------------------------
def _p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def luck_report(results: List[Result], min_games: int = MIN_GAMES_LUCK, z_flag: float = Z_FLAG) -> Tuple[List[LuckRow], List[StreakRow]]:
    games: Dict[int, int] = defaultdict(int)
    wins: Dict[int, int] = defaultdict(int)
    exp: Dict[int, float] = defaultdict(float)
    var: Dict[int, float] = defaultdict(float)
    seq: Dict[int, List[Tuple[bool, float]]] = defaultdict(list)
    for r in results:
        p = 1.0 / len(r.players)
        for u in r.players:
            won = u == r.winner
            games[u] += 1
            wins[u] += 1 if won else 0
            exp[u] += p
            var[u] += p * (1 - p)
            seq[u].append((won, p))
    luck: List[LuckRow] = []
    for u, n in games.items():
        if n < min_games or var[u] <= 0:
            continue
        z = (wins[u] - exp[u]) / math.sqrt(var[u])
        if abs(z) >= z_flag:
            luck.append(LuckRow(u, n, wins[u], exp[u], z, _p_value(z)))
    luck.sort(key=lambda r: -abs(r.z))
    streaks: List[StreakRow] = []
    for u, s in seq.items():
        best_len, best_p, cur_len, cur_p = 0, 1.0, 0, 1.0
        for won, p in s:
            if won:
                cur_len += 1
                cur_p *= p
                if cur_p < best_p:
                    best_len, best_p = cur_len, cur_p
            else:
                cur_len, cur_p = 0, 1.0
        if best_len >= 5 and best_p < STREAK_PROB_FLAG:
            streaks.append(StreakRow(u, best_len, best_p))
    streaks.sort(key=lambda r: r.probability)
    return luck, streaks


def pair_report(results: List[Result], min_games: int = MIN_PAIR_GAMES) -> List[PairRow]:
    two = [r for r in results if len(r.players) == 2]
    total_two: Dict[int, int] = defaultdict(int)
    together: Dict[Tuple[int, int], List[Result]] = defaultdict(list)
    for r in two:
        a, b = sorted(r.players)
        total_two[a] += 1
        total_two[b] += 1
        together[(a, b)].append(r)
    rows: List[PairRow] = []
    for (a, b), rs in together.items():
        g = len(rs)
        if g < min_games:
            continue
        wa = sum(1 for r in rs if r.winner == a)
        wb = g - wa
        flow_to_a = sum(r.stake if r.winner == a else -r.stake for r in rs)
        z = (wa - g / 2) / math.sqrt(g / 4)
        share_a, share_b = g / total_two[a], g / total_two[b]
        if max(share_a, share_b) >= PAIR_CONCENTRATION or abs(z) >= PAIR_Z_FLAG:
            rows.append(PairRow(a, b, g, wa, wb, flow_to_a, share_a, share_b, z))
    rows.sort(key=lambda r: (-r.games, -abs(r.flow_to_a)))
    return rows


def transfer_report(transfers: List[Tuple[int, int, int]]) -> Tuple[List[TransferRow], List[CollectorRow]]:
    out: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for s, r, amount in transfers:
        out[(s, r)].append(amount)
    rows: List[TransferRow] = []
    for (s, r), amounts in out.items():
        total = sum(amounts)
        back = sum(out.get((r, s), []))
        if len(amounts) >= MIN_TRANSFER_COUNT and total / max(total + back, 1) >= ONE_WAY_RATIO:
            rows.append(TransferRow(s, r, total, len(amounts), back))
    rows.sort(key=lambda r: -r.total)
    senders: Dict[int, set] = defaultdict(set)
    received: Dict[int, int] = defaultdict(int)
    for s, r, amount in transfers:
        senders[r].add(s)
        received[r] += amount
    collectors = [CollectorRow(u, len(ss), received[u]) for u, ss in senders.items() if len(ss) >= COLLECTOR_SENDERS]
    collectors.sort(key=lambda c: (-c.senders, -c.total))
    return rows, collectors


def build_report(db_path: str, days: Optional[int] = 30, now: Optional[datetime] = None) -> Report:
    since = None
    if days:
        since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = collect_results(db_path, since)
    rep = Report(since=since, games=len(results))
    rep.luck, rep.streaks = luck_report(results)
    rep.pairs = pair_report(results)
    rep.transfers, rep.collectors = transfer_report(collect_transfers(db_path, since))
    return rep


# ------------------------------------------------------------
# Texto (sin Discord): cada sección es una lista de líneas
# ------------------------------------------------------------
def format_sections(rep: Report, money, limit: int = 10) -> Dict[str, List[str]]:
    def cut(lines: List[str]) -> List[str]:
        return lines[:limit] + ([f"… y {len(lines) - limit} más"] if len(lines) > limit else [])

    luck = [f"<@{r.user_id}> — ganó {r.wins} de {r.games} (esperado {r.expected:.1f}) · z = {r.z:+.1f} · 1 en {int(1 / max(r.p_value, 1e-12)):,} por azar"
            for r in rep.luck]
    luck += [f"<@{s.user_id}> — racha de {s.length} victorias seguidas (probabilidad 1 en {int(1 / s.probability):,})" for s in rep.streaks]
    pairs = []
    for p in rep.pairs:
        who, to = (p.a, p.b) if p.flow_to_a >= 0 else (p.b, p.a)
        pairs.append(f"<@{p.a}> vs <@{p.b}> — {p.games} partidas ({p.wins_a}-{p.wins_b}) · juegan entre sí el {p.share_a * 100:.0f}% / {p.share_b * 100:.0f}% de sus partidas "
                     f"· neto: {money(abs(p.flow_to_a))} de <@{to}> hacia <@{who}>")
    trans = [f"<@{t.sender}> → <@{t.receiver}> — {money(t.total)} en {t.count} envíos" + (f" (le devolvió {money(t.back_total)})" if t.back_total else " (nunca le devolvió)")
             for t in rep.transfers]
    trans += [f"<@{c.user_id}> recibió de {c.senders} cuentas distintas ({money(c.total)} en total)" for c in rep.collectors]
    return {"luck": cut(luck), "pairs": cut(pairs), "transfers": cut(trans)}
