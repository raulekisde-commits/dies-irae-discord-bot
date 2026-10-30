"""Ruleta europea de casino (lógica y persistencia, sin Discord).

Dinero — todo reutiliza la economía existente (eco_accounts / eco_movements), sin otra moneda:
  * Apostar = se descuenta del balance del jugador (movimiento `roulette_bet`) y la apuesta queda «en custodia» de la ronda.
  * Resolver = se calculan los premios (incluyen la devolución de la apuesta) y se acreditan (`roulette_win`). La tesorería de la casa
    (independiente de la guild) cambia en:  apuestas totales − premios totales pagados.
  * Cancelar = se devuelven íntegras las apuestas (`roulette_refund`); la casa no cambia.
Cada acción es UNA transacción (BEGIN IMMEDIATE): o se hace todo o nada, y las transiciones de estado (`betting` → `done`/`cancelled`)
ocurren dentro de la misma transacción, por lo que ni una apuesta ni un premio pueden aplicarse dos veces.

Riesgo — antes de aceptar una apuesta se calcula, para cada uno de los 37 resultados posibles, cuánto pagaría la casa con TODAS las
apuestas de la ronda (incluida la nueva). La casa solo acepta si, aun en el peor resultado, su balance (+ las apuestas en custodia) queda
por encima de la reserva de seguridad. La reserva nunca se usa para autorizar apuestas.

Una sola ruleta: un índice único parcial sobre `roulette_panels(status) WHERE status='open'` impide dos ruletas abiertas a la vez.
"""
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import economy_db as eco
from dbutil import connect, utcnow_iso, write_tx

MOV_BET, MOV_WIN, MOV_REFUND = "roulette_bet", "roulette_win", "roulette_refund"
MAX_BETS_PER_USER_ROUND = 30
MIN_ROUND_SECONDS, MAX_ROUND_SECONDS = 15, 600

REDS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
# Orden físico de la ruleta europea (para mostrar los vecinos del número ganador)
WHEEL = (0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26)

DEFAULT_PAYOUTS = {"color": 2, "parity": 2, "lowhigh": 2, "dozen": 3, "column": 3, "number": 36}
PAYOUT_GROUP = {"red": "color", "black": "color", "even": "parity", "odd": "parity", "low": "lowhigh", "high": "lowhigh",
                "dozen": "dozen", "column": "column", "number": "number"}
KINDS = tuple(PAYOUT_GROUP)

DEFAULTS = {"initial_capital": 50_000_000, "house_balance": 50_000_000, "reserve": 20_000_000, "min_bet": 10_000, "max_bet": 500_000,
            "round_seconds": 60}

SCHEMA = """
CREATE TABLE IF NOT EXISTS roulette_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital INTEGER NOT NULL,
    house_balance   INTEGER NOT NULL CHECK (house_balance >= 0),
    reserve         INTEGER NOT NULL,
    min_bet         INTEGER NOT NULL,
    max_bet         INTEGER NOT NULL,
    round_seconds   INTEGER NOT NULL,
    payouts_enabled INTEGER NOT NULL DEFAULT 0,
    payouts_json    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roulette_panels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    thread_id   INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',          -- open | closed
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    closed_by   INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_roulette_active ON roulette_panels(status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_roulette_panel_thread ON roulette_panels(thread_id);
CREATE TABLE IF NOT EXISTS roulette_rounds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL UNIQUE,
    panel_id     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'betting',      -- betting | done | cancelled
    created_at   TEXT NOT NULL,
    ends_at      REAL,                                  -- se fija con la primera apuesta
    resolved_at  TEXT,
    number       INTEGER,
    total_staked INTEGER NOT NULL DEFAULT 0,
    total_paid   INTEGER NOT NULL DEFAULT 0,
    house_delta  INTEGER NOT NULL DEFAULT 0,
    participants INTEGER NOT NULL DEFAULT 0,
    payouts_json TEXT NOT NULL,
    FOREIGN KEY (panel_id) REFERENCES roulette_panels(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_roulette_round_live ON roulette_rounds(panel_id) WHERE status = 'betting';
CREATE TABLE IF NOT EXISTS roulette_bets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    selection   INTEGER NOT NULL DEFAULT 0,
    amount      INTEGER NOT NULL,
    payout      INTEGER NOT NULL DEFAULT 0,
    won         INTEGER NOT NULL DEFAULT 0,
    request_key TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (round_id) REFERENCES roulette_rounds(id)
);
CREATE INDEX IF NOT EXISTS idx_roulette_bets_round ON roulette_bets(round_id, user_id);
CREATE TABLE IF NOT EXISTS roulette_treasury (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,                        -- initial | settle | admin_adjust
    round_id      INTEGER,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    actor_id      INTEGER,
    note          TEXT,
    idem_key      TEXT NOT NULL UNIQUE
);
"""


class RouletteError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


# ============================================================
# Reglas de la ruleta (puras)
# ============================================================
def color_of(n: int) -> str:
    return "green" if n == 0 else ("red" if n in REDS else "black")


COLOR_EMOJI = {"green": "🟢", "red": "🔴", "black": "⚫"}
COLOR_NAME = {"green": "Verde", "red": "Rojo", "black": "Negro"}


def wins(kind: str, selection: int, n: int) -> bool:
    """¿Gana esta apuesta si sale `n`? El 0 solo gana a número exacto (pierde externas, docenas y columnas)."""
    if kind == "number":
        return n == selection
    if n == 0:
        return False
    if kind == "red":
        return n in REDS
    if kind == "black":
        return n not in REDS
    if kind == "even":
        return n % 2 == 0
    if kind == "odd":
        return n % 2 == 1
    if kind == "low":
        return 1 <= n <= 18
    if kind == "high":
        return 19 <= n <= 36
    if kind == "dozen":
        return (n - 1) // 12 + 1 == selection
    if kind == "column":
        return (n - 1) % 3 + 1 == selection
    return False


def payout_multiplier(kind: str, payouts: Dict[str, int]) -> int:
    return int(payouts[PAYOUT_GROUP[kind]])


def gross_payout(kind: str, selection: int, amount: int, n: int, payouts: Dict[str, int]) -> int:
    """Lo que se le acredita al jugador si gana (incluye la devolución de su apuesta); 0 si pierde."""
    return amount * payout_multiplier(kind, payouts) if wins(kind, selection, n) else 0


def label_of(kind: str, selection: int) -> str:
    return {"red": "🔴 Rojo", "black": "⚫ Negro", "even": "Par", "odd": "Impar", "low": "Falta (1-18)", "high": "Pasa (19-36)",
            "dozen": f"{['1ª', '2ª', '3ª'][selection - 1]} docena ({selection * 12 - 11}-{selection * 12})" if kind == "dozen" and 1 <= selection <= 3 else "Docena",
            "column": f"{['1ª', '2ª', '3ª'][selection - 1]} columna" if kind == "column" and 1 <= selection <= 3 else "Columna",
            "number": f"Número {selection}"}[kind]


_ALIASES = {"rojo": "red", "red": "red", "negro": "black", "black": "black", "par": "even", "even": "even", "impar": "odd", "odd": "odd",
            "falta": "low", "low": "low", "manque": "low", "pasa": "high", "high": "high", "passe": "high"}


def parse_selection(text: str) -> Tuple[str, int]:
    """'rojo' → ('red', 0) · 'docena 2' → ('dozen', 2) · 'columna 3' → ('column', 3) · 'numero 17' / 'n 17' / '17' → ('number', 17)."""
    words = (text or "").lower().replace("º", "").replace("ª", "").replace("°", "").split()
    if not words:
        raise RouletteError("Elegí a qué apostás: `rojo`, `negro`, `par`, `impar`, `falta`, `pasa`, `docena 1-3`, `columna 1-3` o `numero 0-36`.")
    head = words[0]
    if head in _ALIASES and len(words) == 1:
        return _ALIASES[head], 0
    if head in ("1-18", "1a18"):
        return "low", 0
    if head in ("19-36", "19a36"):
        return "high", 0
    if head in ("docena", "doc", "d", "columna", "col", "c") and len(words) == 2 and words[1].isdigit():
        sel = int(words[1])
        if not 1 <= sel <= 3:
            raise RouletteError("La docena o columna va de 1 a 3 (ej: `docena 2`).")
        return ("dozen" if head.startswith("d") else "column"), sel
    if head in ("numero", "número", "num", "n", "pleno") and len(words) == 2 and words[1].isdigit():
        words = [words[1]]
        head = words[0]
    if len(words) == 1 and head.isdigit():
        sel = int(head)
        if not 0 <= sel <= 36:
            raise RouletteError("Los números van de 0 a 36.")
        return "number", sel
    raise RouletteError("No entendí la apuesta. Ejemplos: `rojo`, `par`, `docena 2`, `columna 1`, `numero 17`.")


def worst_case(bets: List[Tuple[str, int, int]], payouts: Dict[str, int]) -> Tuple[int, int, int]:
    """(peor resultado, premios totales en ese resultado, pérdida neta de la casa en él) para una lista de (tipo, selección, monto).
    Pérdida neta = premios − apuestas en custodia (las apuestas ya están descontadas a los jugadores)."""
    staked = sum(b[2] for b in bets)
    worst_n, worst_paid = 0, -1
    for n in range(37):
        paid = sum(gross_payout(k, s, a, n, payouts) for k, s, a in bets)
        if paid > worst_paid:
            worst_n, worst_paid = n, paid
    return worst_n, worst_paid, worst_paid - staked


# ============================================================
# Modelo
# ============================================================
@dataclass
class Config:
    initial_capital: int
    house_balance: int
    reserve: int
    min_bet: int
    max_bet: int
    round_seconds: int
    payouts_enabled: bool
    payouts: Dict[str, int]


@dataclass
class Panel:
    id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    thread_id: Optional[int]
    status: str
    created_by: int
    created_at: str


@dataclass
class Round:
    id: int
    code: str
    panel_id: int
    status: str
    created_at: str
    ends_at: Optional[float]
    number: Optional[int]
    total_staked: int
    total_paid: int
    house_delta: int
    participants: int
    payouts: Dict[str, int]


@dataclass
class Bet:
    id: int
    round_id: int
    user_id: int
    kind: str
    selection: int
    amount: int
    payout: int
    won: bool


def _cfg(r) -> Config:
    return Config(int(r["initial_capital"]), int(r["house_balance"]), int(r["reserve"]), int(r["min_bet"]), int(r["max_bet"]),
                  int(r["round_seconds"]), bool(r["payouts_enabled"]), {k: int(v) for k, v in json.loads(r["payouts_json"]).items()})


def _panel(r) -> Optional[Panel]:
    if r is None:
        return None
    return Panel(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), r["message_id"], r["thread_id"], r["status"], int(r["created_by"]), r["created_at"])


def _round(r, conn=None) -> Optional[Round]:
    if r is None:
        return None
    return Round(int(r["id"]), r["code"], int(r["panel_id"]), r["status"], r["created_at"], r["ends_at"], r["number"], int(r["total_staked"]),
                 int(r["total_paid"]), int(r["house_delta"]), int(r["participants"]), {k: int(v) for k, v in json.loads(r["payouts_json"]).items()})


def _bet(r) -> Bet:
    return Bet(int(r["id"]), int(r["round_id"]), int(r["user_id"]), r["kind"], int(r["selection"]), int(r["amount"]), int(r["payout"]), bool(r["won"]))


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM roulette_config WHERE id = 1").fetchone() is None:
                now = utcnow_iso()
                conn.execute("INSERT INTO roulette_config(id, initial_capital, house_balance, reserve, min_bet, max_bet, round_seconds, payouts_enabled, "
                             "payouts_json, updated_at) VALUES (1,?,?,?,?,?,?,0,?,?)",
                             (DEFAULTS["initial_capital"], DEFAULTS["house_balance"], DEFAULTS["reserve"], DEFAULTS["min_bet"], DEFAULTS["max_bet"],
                              DEFAULTS["round_seconds"], json.dumps(DEFAULT_PAYOUTS), now))
                conn.execute("INSERT OR IGNORE INTO roulette_treasury(at, kind, delta, balance_after, actor_id, note, idem_key) VALUES (?,?,?,?,?,?,?)",
                             (now, "initial", DEFAULTS["house_balance"], DEFAULTS["house_balance"], None, "Capital inicial de la casa", "initial"))
    finally:
        conn.close()


def get_config(db_path: str) -> Config:
    conn = connect(db_path)
    try:
        return _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
    finally:
        conn.close()


# ============================================================
# Configuración (/ruleta-editar)
# ============================================================
def _exposure_now(conn, cfg: Config) -> int:
    """Pérdida máxima de la casa con las apuestas de las rondas abiertas (≥ 0)."""
    worst = 0
    for rr in conn.execute("SELECT * FROM roulette_rounds WHERE status = 'betting'").fetchall():
        rnd = _round(rr)
        bets = [(b["kind"], int(b["selection"]), int(b["amount"])) for b in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ?", (rnd.id,))]
        if bets:
            worst += max(0, worst_case(bets, rnd.payouts)[2])
    return worst


def update_config(db_path: str, actor_id: int, *, initial_capital: Optional[int] = None, min_bet: Optional[int] = None, max_bet: Optional[int] = None,
                  reserve: Optional[int] = None, round_seconds: Optional[int] = None, payouts_enabled: Optional[bool] = None,
                  payouts: Optional[Dict[str, int]] = None) -> Config:
    """Cambia parámetros uno por uno. El capital inicial NO toca el balance actual. Los pagos solo se editan si están habilitados y rigen desde la
    ronda siguiente (cada ronda guarda los pagos con los que se abrió)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
            vals = {"initial_capital": cfg.initial_capital, "min_bet": cfg.min_bet, "max_bet": cfg.max_bet, "reserve": cfg.reserve,
                    "round_seconds": cfg.round_seconds}
            for key, v in (("initial_capital", initial_capital), ("min_bet", min_bet), ("max_bet", max_bet), ("reserve", reserve), ("round_seconds", round_seconds)):
                if v is not None:
                    vals[key] = int(v)
            if vals["initial_capital"] <= 0:
                raise RouletteError("El capital inicial tiene que ser mayor a 0.")
            if vals["min_bet"] < 1:
                raise RouletteError("La apuesta mínima tiene que ser al menos 1.")
            if vals["max_bet"] < vals["min_bet"]:
                raise RouletteError("La apuesta máxima no puede ser menor que la mínima.")
            if vals["reserve"] < 0:
                raise RouletteError("La reserva no puede ser negativa.")
            if not MIN_ROUND_SECONDS <= vals["round_seconds"] <= MAX_ROUND_SECONDS:
                raise RouletteError(f"La duración de la ronda va de {MIN_ROUND_SECONDS} a {MAX_ROUND_SECONDS} segundos.")
            enabled = cfg.payouts_enabled if payouts_enabled is None else bool(payouts_enabled)
            new_payouts = dict(cfg.payouts)
            if payouts:
                if not enabled:
                    raise RouletteError("Los pagos no se pueden modificar: primero hay que habilitar expresamente `pagos_editables`.")
                for k, v in payouts.items():
                    if k not in DEFAULT_PAYOUTS:
                        raise RouletteError("Tipo de pago inválido.")
                    if not 1 <= int(v) <= 1000:
                        raise RouletteError("Un pago tiene que ser entre 1x y 1000x.")
                    new_payouts[k] = int(v)
            conn.execute("UPDATE roulette_config SET initial_capital=?, min_bet=?, max_bet=?, reserve=?, round_seconds=?, payouts_enabled=?, payouts_json=?, "
                         "updated_at=? WHERE id = 1",
                         (vals["initial_capital"], vals["min_bet"], vals["max_bet"], vals["reserve"], vals["round_seconds"], 1 if enabled else 0,
                          json.dumps(new_payouts), utcnow_iso()))
            changes = {k: v for k, v in (("capital_inicial", initial_capital), ("min", min_bet), ("max", max_bet), ("reserva", reserve),
                                          ("segundos", round_seconds), ("pagos_editables", payouts_enabled), ("pagos", payouts)) if v is not None}
            conn.execute("INSERT INTO roulette_treasury(at, kind, delta, balance_after, actor_id, note, idem_key) VALUES (?,?,?,?,?,?,?)",
                         (utcnow_iso(), "config", 0, cfg.house_balance, actor_id, f"Config: {json.dumps(changes, ensure_ascii=False)}"[:500], f"cfg:{uuid.uuid4().hex}"))
            return _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
    finally:
        conn.close()


def adjust_house_balance(db_path: str, actor_id: int, new_balance: int, note: str) -> Tuple[int, int]:
    """Ajuste administrativo auditado del balance de la casa. Devuelve (antes, después). No puede dejarlo negativo ni por debajo de lo que
    la casa necesita para cubrir las apuestas ya aceptadas."""
    note = " ".join((note or "").split())[:200]
    if not note:
        raise RouletteError("Un ajuste del balance de la casa necesita un motivo (queda en la auditoría).")
    if new_balance < 0:
        raise RouletteError("El balance de la casa no puede ser negativo.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
            exposure = _exposure_now(conn, cfg)
            if new_balance < exposure:
                raise RouletteError(f"Hay apuestas aceptadas que la casa tendría que poder cubrir (hasta {exposure:,}). Ese balance no alcanza.")
            conn.execute("UPDATE roulette_config SET house_balance = ?, updated_at = ? WHERE id = 1", (new_balance, utcnow_iso()))
            conn.execute("INSERT INTO roulette_treasury(at, kind, delta, balance_after, actor_id, note, idem_key) VALUES (?,?,?,?,?,?,?)",
                         (utcnow_iso(), "admin_adjust", new_balance - cfg.house_balance, new_balance, actor_id, note, f"adj:{uuid.uuid4().hex}"))
            return cfg.house_balance, new_balance
    finally:
        conn.close()


# ============================================================
# Ruleta global única y rondas
# ============================================================
def _new_round(conn, panel_id: int) -> int:
    cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
    cur = conn.execute("INSERT INTO roulette_rounds(code, panel_id, created_at, payouts_json) VALUES (?,?,?,?)",
                       (uuid.uuid4().hex[:12], panel_id, utcnow_iso(), json.dumps(cfg.payouts)))
    return int(cur.lastrowid)


def create_panel(db_path: str, guild_id: int, channel_id: int, created_by: int) -> Tuple[Panel, bool]:
    """(panel, creado). Si ya había una ruleta abierta devuelve ESA con creado=False. La unicidad la garantiza la base."""
    conn = connect(db_path)
    try:
        try:
            with write_tx(conn):
                cur = conn.execute("INSERT INTO roulette_panels(guild_id, channel_id, created_by, created_at) VALUES (?,?,?,?)",
                                   (guild_id, channel_id, created_by, utcnow_iso()))
                _new_round(conn, int(cur.lastrowid))
                return _panel(conn.execute("SELECT * FROM roulette_panels WHERE id = ?", (cur.lastrowid,)).fetchone()), True
        except sqlite3.IntegrityError:
            existing = conn.execute("SELECT * FROM roulette_panels WHERE status = 'open'").fetchone()
            if existing is None:
                raise
            return _panel(existing), False
    finally:
        conn.close()


def set_panel_message(db_path: str, panel_id: int, message_id: int, thread_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE roulette_panels SET message_id = ?, thread_id = ? WHERE id = ?", (message_id, thread_id, panel_id))
    finally:
        conn.close()


def abort_panel(db_path: str, panel_id: int) -> None:
    """La publicación del panel falló: se libera el estado global (sin apuestas todavía)."""
    close_panel(db_path, panel_id, None)


def get_active_panel(db_path: str) -> Optional[Panel]:
    conn = connect(db_path)
    try:
        return _panel(conn.execute("SELECT * FROM roulette_panels WHERE status = 'open'").fetchone())
    finally:
        conn.close()


def get_panel(db_path: str, panel_id: int) -> Optional[Panel]:
    conn = connect(db_path)
    try:
        return _panel(conn.execute("SELECT * FROM roulette_panels WHERE id = ?", (panel_id,)).fetchone())
    finally:
        conn.close()


def find_panel_by_thread(db_path: str, thread_id: int) -> Optional[Panel]:
    conn = connect(db_path)
    try:
        return _panel(conn.execute("SELECT * FROM roulette_panels WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone())
    finally:
        conn.close()


def find_panel_by_message(db_path: str, message_id: int) -> Optional[Panel]:
    conn = connect(db_path)
    try:
        return _panel(conn.execute("SELECT * FROM roulette_panels WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def current_round(db_path: str, panel_id: int) -> Optional[Round]:
    conn = connect(db_path)
    try:
        return _round(conn.execute("SELECT * FROM roulette_rounds WHERE panel_id = ? AND status = 'betting'", (panel_id,)).fetchone())
    finally:
        conn.close()


def last_finished_round(db_path: str, panel_id: Optional[int] = None) -> Optional[Round]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM roulette_rounds WHERE status = 'done'" + (" AND panel_id = ?" if panel_id else "") + " ORDER BY id DESC LIMIT 1"
        return _round(conn.execute(sql, (panel_id,) if panel_id else ()).fetchone())
    finally:
        conn.close()


def round_summary(db_path: str, round_id: int) -> Tuple[int, int, int]:
    """(participantes, apuestas, total apostado) de una ronda."""
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT COUNT(DISTINCT user_id) AS p, COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM roulette_bets WHERE round_id = ?", (round_id,)).fetchone()
        return int(r["p"]), int(r["n"]), int(r["s"])
    finally:
        conn.close()


def user_bets(db_path: str, round_id: int, user_id: int) -> List[Bet]:
    conn = connect(db_path)
    try:
        return [_bet(r) for r in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ? AND user_id = ? ORDER BY id", (round_id, user_id))]
    finally:
        conn.close()


def round_bets(db_path: str, round_id: int) -> List[Bet]:
    conn = connect(db_path)
    try:
        return [_bet(r) for r in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ? ORDER BY id", (round_id,))]
    finally:
        conn.close()


@dataclass
class BetResult:
    status: str        # ok | closed | min | max | funds | frozen | exposure | too_many | duplicate | notfound
    bet: Optional[Bet] = None
    round: Optional[Round] = None
    balance: int = 0
    detail: int = 0    # min/max/saldo según el caso


def place_bet(db_path: str, panel_id: int, user_id: int, kind: str, selection: int, amount: int, request_key: str,
              now: Optional[float] = None) -> BetResult:
    now = time.time() if now is None else now
    if kind not in KINDS:
        raise RouletteError("Tipo de apuesta inválido.")
    if kind == "number" and not 0 <= selection <= 36:
        raise RouletteError("Los números van de 0 a 36.")
    if kind in ("dozen", "column") and not 1 <= selection <= 3:
        raise RouletteError("La docena o columna va de 1 a 3.")
    if amount <= 0:
        raise RouletteError("La cantidad tiene que ser mayor a 0.")
    conn = connect(db_path)
    try:
        try:
            with write_tx(conn):
                p = conn.execute("SELECT * FROM roulette_panels WHERE id = ?", (panel_id,)).fetchone()
                if p is None:
                    return BetResult("notfound")
                rr = conn.execute("SELECT * FROM roulette_rounds WHERE panel_id = ? AND status = 'betting'", (panel_id,)).fetchone()
                if p["status"] != "open" or rr is None:
                    return BetResult("closed")
                rnd = _round(rr)
                if rnd.ends_at is not None and now >= rnd.ends_at:
                    return BetResult("closed", round=rnd)
                if conn.execute("SELECT 1 FROM roulette_bets WHERE request_key = ?", (request_key,)).fetchone():
                    return BetResult("duplicate", round=rnd)
                cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
                if amount < cfg.min_bet:
                    return BetResult("min", round=rnd, detail=cfg.min_bet)
                if amount > cfg.max_bet:
                    return BetResult("max", round=rnd, detail=cfg.max_bet)
                if conn.execute("SELECT COUNT(*) FROM roulette_bets WHERE round_id = ? AND user_id = ?", (rnd.id, user_id)).fetchone()[0] >= MAX_BETS_PER_USER_ROUND:
                    return BetResult("too_many", round=rnd, detail=MAX_BETS_PER_USER_ROUND)
                # Control de riesgo: peor resultado posible con TODAS las apuestas (incluida esta)
                existing = [(b["kind"], int(b["selection"]), int(b["amount"])) for b in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ?", (rnd.id,))]
                _, _, net_loss = worst_case(existing + [(kind, selection, amount)], rnd.payouts)
                if cfg.house_balance - max(0, net_loss) < cfg.reserve:
                    return BetResult("exposure", round=rnd)
                related = f"roulette:{rnd.id}"
                reason = f"Ruleta ronda #{rnd.id}: {label_of(kind, selection)}"
                op = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"roulette:{rnd.id}:bet:{request_key}:{uuid.uuid4().hex}",
                                          total_amount=amount, target_count=1, reason=reason, related_id=related)
                _, after = eco.tx_move(conn, op_id=op, user_id=user_id, delta=-amount, mov_type=MOV_BET, reason=reason, actor_id=user_id,
                                       related_id=related, require_funds=True)
                cur = conn.execute("INSERT INTO roulette_bets(round_id, user_id, kind, selection, amount, request_key, created_at) VALUES (?,?,?,?,?,?,?)",
                                   (rnd.id, user_id, kind, selection, amount, request_key, utcnow_iso()))
                if rnd.ends_at is None:                         # la primera apuesta arranca el reloj de la ronda
                    conn.execute("UPDATE roulette_rounds SET ends_at = ? WHERE id = ?", (now + cfg.round_seconds, rnd.id))
                rnd = _round(conn.execute("SELECT * FROM roulette_rounds WHERE id = ?", (rnd.id,)).fetchone())
                return BetResult("ok", _bet(conn.execute("SELECT * FROM roulette_bets WHERE id = ?", (cur.lastrowid,)).fetchone()), rnd, after)
        except eco.InsufficientFunds as exc:
            return BetResult("funds", balance=int(exc.args[0]))
        except eco.BalanceFrozen:
            return BetResult("frozen")
    finally:
        conn.close()


@dataclass
class Settlement:
    status: str                                   # done | cancelled
    round: Round
    number: Optional[int] = None
    bets: List[Bet] = field(default_factory=list)
    payouts_by_user: Dict[int, int] = field(default_factory=dict)
    stake_by_user: Dict[int, int] = field(default_factory=dict)
    next_round_id: Optional[int] = None
    note: str = ""

    @property
    def winners(self) -> List[Bet]:
        return [b for b in self.bets if b.won]

    @property
    def losers(self) -> List[Bet]:
        return [b for b in self.bets if not b.won]


def due_rounds(db_path: str, now: Optional[float] = None) -> List[int]:
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute(
            "SELECT r.id FROM roulette_rounds r JOIN roulette_panels p ON p.id = r.panel_id "
            "WHERE r.status = 'betting' AND r.ends_at IS NOT NULL AND r.ends_at <= ? AND p.status = 'open' ORDER BY r.id", (now,))]
    finally:
        conn.close()


def _credit(conn, rnd_id: int, user_id: int, amount: int, mov: str, reason: str, key: str) -> None:
    related = f"roulette:{rnd_id}"
    op = eco.tx_new_operation(conn, kind="add", actor_id=0, request_key=f"roulette:{rnd_id}:{key}:{user_id}", total_amount=amount, target_count=1,
                              reason=reason, related_id=related)
    eco.tx_move(conn, op_id=op, user_id=user_id, delta=amount, mov_type=mov, reason=reason, actor_id=0, related_id=related)


def _refund_all(conn, rnd: Round, bets: List[Bet], reason: str) -> Dict[int, int]:
    per_user: Dict[int, int] = {}
    for b in bets:
        per_user[b.user_id] = per_user.get(b.user_id, 0) + b.amount
    for uid, amount in sorted(per_user.items()):
        _credit(conn, rnd.id, uid, amount, MOV_REFUND, reason, "refund")
    return per_user


def resolve_round(db_path: str, round_id: int, now: Optional[float] = None, force: bool = False, number: Optional[int] = None) -> Optional[Settlement]:
    """Gira y liquida una ronda. None si no corresponde (ya resuelta, todavía no vence, sin apuestas). `number` solo para pruebas."""
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        with write_tx(conn):
            rr = conn.execute("SELECT * FROM roulette_rounds WHERE id = ?", (round_id,)).fetchone()
            if rr is None or rr["status"] != "betting":
                return None
            rnd = _round(rr)
            if not force and (rnd.ends_at is None or now < rnd.ends_at):
                return None
            bets = [_bet(b) for b in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ? ORDER BY id", (round_id,))]
            if not bets:
                return None
            cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
            n = secrets.randbelow(37) if number is None else int(number)          # generador criptográfico del sistema operativo
            staked = sum(b.amount for b in bets)
            payouts_by_user: Dict[int, int] = {}
            paid_total = 0
            settled: List[Bet] = []
            for b in bets:
                gross = gross_payout(b.kind, b.selection, b.amount, n, rnd.payouts)
                b.payout, b.won = gross, gross > 0
                paid_total += gross
                if gross:
                    payouts_by_user[b.user_id] = payouts_by_user.get(b.user_id, 0) + gross
                settled.append(b)
            house_delta = staked - paid_total
            new_balance = cfg.house_balance + house_delta
            panel = conn.execute("SELECT * FROM roulette_panels WHERE id = ?", (rnd.panel_id,)).fetchone()
            if new_balance < 0:                                                  # no debería ocurrir (control de riesgo); si pasa, se devuelve todo
                stake_by_user = _refund_all(conn, rnd, bets, f"Ruleta ronda #{rnd.id}: cancelada (la casa no podía cubrir los premios)")
                conn.execute("UPDATE roulette_rounds SET status = 'cancelled', resolved_at = ? WHERE id = ?", (utcnow_iso(), rnd.id))
                nxt = _new_round(conn, rnd.panel_id) if panel["status"] == "open" else None
                return Settlement("cancelled", _round(conn.execute("SELECT * FROM roulette_rounds WHERE id = ?", (rnd.id,)).fetchone()), None, bets,
                                  {}, stake_by_user, nxt, "La casa no podía cubrir los premios: se devolvieron las apuestas.")
            for uid, amount in sorted(payouts_by_user.items()):
                _credit(conn, rnd.id, uid, amount, MOV_WIN, f"Ruleta ronda #{rnd.id}: premio (número {n})", "win")
            for b in settled:
                conn.execute("UPDATE roulette_bets SET payout = ?, won = ? WHERE id = ?", (b.payout, 1 if b.won else 0, b.id))
            conn.execute("UPDATE roulette_config SET house_balance = ?, updated_at = ? WHERE id = 1", (new_balance, utcnow_iso()))
            conn.execute("INSERT INTO roulette_treasury(at, kind, round_id, delta, balance_after, note, idem_key) VALUES (?,?,?,?,?,?,?)",
                         (utcnow_iso(), "settle", rnd.id, house_delta, new_balance, f"Ronda #{rnd.id}: número {n}", f"round:{rnd.id}:settle"))
            participants = len({b.user_id for b in bets})
            conn.execute("UPDATE roulette_rounds SET status = 'done', resolved_at = ?, number = ?, total_staked = ?, total_paid = ?, house_delta = ?, "
                         "participants = ? WHERE id = ?", (utcnow_iso(), n, staked, paid_total, house_delta, participants, rnd.id))
            nxt = _new_round(conn, rnd.panel_id) if panel["status"] == "open" else None
            stake_by_user: Dict[int, int] = {}
            for b in bets:
                stake_by_user[b.user_id] = stake_by_user.get(b.user_id, 0) + b.amount
            return Settlement("done", _round(conn.execute("SELECT * FROM roulette_rounds WHERE id = ?", (rnd.id,)).fetchone()), n, settled,
                              payouts_by_user, stake_by_user, nxt)
    finally:
        conn.close()


def close_panel(db_path: str, panel_id: int, actor_id: Optional[int]) -> Optional[Settlement]:
    """Cierra la ruleta: cancela la ronda en curso devolviendo íntegras las apuestas y libera el estado global.
    Devuelve el resultado de la cancelación (o un Settlement vacío si no había apuestas); None si ya estaba cerrada."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            p = conn.execute("SELECT * FROM roulette_panels WHERE id = ?", (panel_id,)).fetchone()
            if p is None or p["status"] != "open":
                return None
            rr = conn.execute("SELECT * FROM roulette_rounds WHERE panel_id = ? AND status = 'betting'", (panel_id,)).fetchone()
            settlement = None
            if rr is not None:
                rnd = _round(rr)
                bets = [_bet(b) for b in conn.execute("SELECT * FROM roulette_bets WHERE round_id = ? ORDER BY id", (rnd.id,))]
                stake_by_user = _refund_all(conn, rnd, bets, f"Ruleta ronda #{rnd.id}: cancelada por cierre de la ruleta") if bets else {}
                conn.execute("UPDATE roulette_rounds SET status = 'cancelled', resolved_at = ?, total_staked = ?, participants = ? WHERE id = ?",
                             (utcnow_iso(), sum(b.amount for b in bets), len({b.user_id for b in bets}), rnd.id))
                settlement = Settlement("cancelled", _round(conn.execute("SELECT * FROM roulette_rounds WHERE id = ?", (rnd.id,)).fetchone()), None, bets,
                                        {}, stake_by_user)
            conn.execute("UPDATE roulette_panels SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?", (utcnow_iso(), actor_id, panel_id))
            return settlement or Settlement("cancelled", Round(0, "", panel_id, "cancelled", "", None, None, 0, 0, 0, 0, {}))
    finally:
        conn.close()


# ============================================================
# Estadísticas
# ============================================================
def stats(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        cfg = _cfg(conn.execute("SELECT * FROM roulette_config WHERE id = 1").fetchone())
        done = "status = 'done'"
        agg = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(total_staked),0) AS staked, COALESCE(SUM(total_paid),0) AS paid, "
                           f"COALESCE(SUM(CASE WHEN house_delta > 0 THEN house_delta ELSE 0 END),0) AS gains, "
                           f"COALESCE(SUM(CASE WHEN house_delta < 0 THEN -house_delta ELSE 0 END),0) AS losses, "
                           f"COALESCE(MIN(house_delta),0) AS worst FROM roulette_rounds WHERE {done}").fetchone()
        players = conn.execute(f"SELECT COUNT(DISTINCT b.user_id) FROM roulette_bets b JOIN roulette_rounds r ON r.id = b.round_id WHERE r.{done}").fetchone()[0]
        biggest = conn.execute(f"SELECT COALESCE(MAX(s),0) FROM (SELECT SUM(b.payout) AS s FROM roulette_bets b JOIN roulette_rounds r ON r.id = b.round_id "
                               f"WHERE r.{done} GROUP BY b.round_id, b.user_id)").fetchone()[0]
        adjustments = [dict(r) for r in conn.execute("SELECT at, kind, delta, balance_after, actor_id, note FROM roulette_treasury "
                                                      "WHERE kind IN ('admin_adjust','config') ORDER BY id DESC LIMIT 10")]
        return {"config": cfg, "rounds": int(agg["n"]), "staked": int(agg["staked"]), "paid": int(agg["paid"]), "gains": int(agg["gains"]),
                "losses": int(agg["losses"]), "biggest_house_loss": max(0, -int(agg["worst"])), "players": int(players),
                "biggest_payout": int(biggest), "adjustments": adjustments}
    finally:
        conn.close()
