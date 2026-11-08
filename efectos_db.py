"""Efectos automáticos de las cartas de Wujupón (sin Discord).

Efectos con vencimiento por persona: mute de chat, mute de voice, congelar balance, congelar Wujupón, escudo y bloqueo
de casino. Más la lista de inmunes permanentes (la administra un admin).

Reglas:
- Una carta que afecta a otro no se puede usar contra un inmune ni contra alguien con escudo: la carta NO se gasta.
- Las cartas malas (☠️) tampoco afectan a quien tiene escudo o es inmune: se consumen sin efecto.
- Cada uso es una sola transacción: la carta se consume y el efecto se guarda juntos (o nada).
- Si el mismo efecto ya está activo, se extiende hasta el vencimiento más lejano (no se suman tiempos).
"""
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import economy_db as eco
import embo_db
from dbutil import connect, utcnow_iso, write_tx

KINDS = ("mute_chat", "mute_voice", "freeze_balance", "freeze_wujupon", "shield", "casino_block")
KIND_LABELS = {
    "mute_chat": "🔇 Mute de chat", "mute_voice": "🔈 Mute de voice", "freeze_balance": "🧊 Balance congelado",
    "freeze_wujupon": "❄️ Wujupón congelado", "shield": "🛡️ Escudo", "casino_block": "🚫 Bloqueado del casino",
}
CLEANS = {                                  # carta de limpieza → efectos que saca
    "clean_chat": ("mute_chat",),
    "clean_voice": ("mute_voice",),
    "clean_freeze": ("freeze_balance",),
    "clean_all": ("mute_chat", "mute_voice", "freeze_balance", "freeze_wujupon"),
}
TARGETED = ("mute_chat", "mute_voice", "freeze_balance", "freeze_wujupon", "casino_block", "purge", "voice_kick")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wjp_effects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    until      REAL,                          -- epoch; NULL = sin vencimiento (bloqueo de casino)
    armed      INTEGER NOT NULL DEFAULT 0,    -- bloqueo de casino: 1 = la próxima apertura lo termina
    card_id    INTEGER,
    actor_id   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    ended_at   TEXT,
    ended_by   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wjp_effects_user ON wjp_effects(user_id, kind, active);
CREATE TABLE IF NOT EXISTS wjp_immune (
    user_id    INTEGER PRIMARY KEY,
    reason     TEXT,
    actor_id   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass
class Effect:
    id: int
    user_id: int
    kind: str
    until: Optional[float]
    actor_id: int


def _effect(r) -> Effect:
    return Effect(int(r["id"]), int(r["user_id"]), r["kind"], float(r["until"]) if r["until"] is not None else None, int(r["actor_id"]))


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _now() -> float:
    return time.time()


def _active(conn, user_id: int, kind: str, now: Optional[float] = None) -> Optional[Effect]:
    now = now or _now()
    r = conn.execute("SELECT * FROM wjp_effects WHERE user_id = ? AND kind = ? AND active = 1 AND (until IS NULL OR until > ?) "
                     "ORDER BY until DESC LIMIT 1", (user_id, kind, now)).fetchone()
    return _effect(r) if r else None


def _safe(fn, default):
    """Las tablas pueden no existir todavía (otros módulos consultan antes de que Wujupón arranque)."""
    try:
        return fn()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return default
        raise


def tx_has(conn, user_id: int, kind: str) -> bool:
    return _safe(lambda: _active(conn, user_id, kind) is not None, False)


def active_until(db_path: str, user_id: int, kind: str) -> Optional[float]:
    """Vencimiento del efecto activo (epoch), 0.0 si no vence, o None si no lo tiene."""
    conn = connect(db_path)
    try:
        e = _safe(lambda: _active(conn, user_id, kind), None)
        return None if e is None else (e.until or 0.0)
    finally:
        conn.close()


def _immune(conn, user_id: int) -> bool:
    return _safe(lambda: conn.execute("SELECT 1 FROM wjp_immune WHERE user_id = ?", (user_id,)).fetchone() is not None, False)


def is_immune(db_path: str, user_id: int) -> bool:
    conn = connect(db_path)
    try:
        return _immune(conn, user_id)
    finally:
        conn.close()


def _protected(conn, user_id: int) -> Optional[str]:
    """'immune' | 'shield' | None."""
    if _immune(conn, user_id):
        return "immune"
    if tx_has(conn, user_id, "shield"):
        return "shield"
    return None


def _add_effect(conn, user_id: int, kind: str, minutes: Optional[float], card_id: Optional[int], actor_id: int,
                armed: int = 0) -> Effect:
    now = _now()
    cur = _active(conn, user_id, kind, now)
    until = None if minutes is None else now + float(minutes) * 60
    if cur is not None:
        new_until = None if (cur.until is None or until is None) else max(cur.until, until)
        conn.execute("UPDATE wjp_effects SET until = ?, card_id = ?, actor_id = ? WHERE id = ?", (new_until, card_id, actor_id, cur.id))
        return _effect(conn.execute("SELECT * FROM wjp_effects WHERE id = ?", (cur.id,)).fetchone())
    eid = conn.execute("INSERT INTO wjp_effects(user_id, kind, until, armed, card_id, actor_id, created_at) VALUES (?,?,?,?,?,?,?)",
                       (user_id, kind, until, armed, card_id, actor_id, utcnow_iso())).lastrowid
    return _effect(conn.execute("SELECT * FROM wjp_effects WHERE id = ?", (eid,)).fetchone())


def _end(conn, user_id: int, kinds, actor_id: Optional[int]) -> List[Effect]:
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(f"SELECT * FROM wjp_effects WHERE user_id = ? AND active = 1 AND kind IN ({marks}) "
                        f"AND (until IS NULL OR until > ?)", (user_id, *kinds, _now())).fetchall()
    if rows:
        conn.execute(f"UPDATE wjp_effects SET active = 0, ended_at = ?, ended_by = ? WHERE id IN ({','.join('?' * len(rows))})",
                     (utcnow_iso(), actor_id, *[r["id"] for r in rows]))
    return [_effect(r) for r in rows]


def _consume(conn, card_id: int, owner_id: int, from_status: str = "available") -> bool:
    return conn.execute("UPDATE wjp_cards SET status = 'consumed', updated_at = ? WHERE id = ? AND user_id = ? AND status = ?",
                        (utcnow_iso(), card_id, owner_id, from_status)).rowcount == 1


def _log(conn, actor_id: int, action: str, user_id: Optional[int], ref: str, detail: str) -> None:
    conn.execute("INSERT INTO wjp_log(actor_id, action, user_id, ref, detail, created_at) VALUES (?,?,?,?,?,?)",
                 (actor_id, action, user_id, ref, detail[:1000], utcnow_iso()))


def _record(conn, card_id: int, user_id: int, prize_key: str, note: str) -> None:
    now = utcnow_iso()
    conn.execute("INSERT OR IGNORE INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,'auto',?,?,?)", (f"efx:{card_id}", card_id, user_id, prize_key, "", note[:500], now, now))


# ============================================================
# Usar una carta
# ============================================================
@dataclass
class UseResult:
    status: str                   # ok | not_available | immune | shield | no_effect | casino_state
    message: str = ""
    effect: Optional[Effect] = None
    ended: Tuple[Effect, ...] = ()


def use_card(db_path: str, *, card_id: int, owner_id: int, prize_key: str, action: str, minutes: Optional[float],
             target_id: Optional[int], casino_open: Optional[bool] = None) -> UseResult:
    """Consume la carta y aplica el efecto en UNA transacción. `action`: un KIND, 'purge', 'clean_*' o 'casino_block'.
    `casino_open` (para el bloqueo de casino): estado del casino en este momento."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            card = conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone()
            if card is None or int(card["user_id"]) != owner_id or card["status"] != "available":
                return UseResult("not_available", "Esa carta ya no está disponible.")
            if action in TARGETED:
                prot = _protected(conn, target_id)
                if prot == "immune":
                    return UseResult("immune", "Esa persona es **inmune** a las cartas. Tu carta no se gastó.")
                if prot == "shield" and action != "purge":
                    e = _active(conn, target_id, "shield")
                    return UseResult("shield", f"Esa persona tiene **🛡️ escudo** hasta <t:{int(e.until)}:t>. Tu carta no se gastó.")
            if action == "purge":
                ended = _end(conn, target_id, ("shield",), owner_id)
                if not ended:
                    return UseResult("no_effect", "Esa persona no tiene escudo. Tu carta no se gastó.")
                _consume(conn, card_id, owner_id)
                _record(conn, card_id, owner_id, prize_key, f"Purgó el escudo de {target_id}")
                _log(conn, owner_id, "efx_purge", target_id, f"card:{card_id}", "")
                return UseResult("ok", ended=tuple(ended))
            if action in CLEANS:
                ended = _end(conn, owner_id, CLEANS[action], owner_id)
                if not ended:
                    return UseResult("no_effect", "No tenés nada que limpiar. Tu carta no se gastó.")
                _consume(conn, card_id, owner_id)
                _record(conn, card_id, owner_id, prize_key, "Limpió: " + ", ".join(e.kind for e in ended))
                _log(conn, owner_id, "efx_clean", owner_id, f"card:{card_id}", ",".join(e.kind for e in ended))
                return UseResult("ok", ended=tuple(ended))
            if action == "voice_kick":
                # sin efecto que dure: la capa de Discord desconecta (y devuelve la carta con undo_use si no puede)
                _consume(conn, card_id, owner_id)
                _record(conn, card_id, owner_id, prize_key, f"Kick de voice → {target_id}")
                _log(conn, owner_id, "efx_voice_kick", target_id, f"card:{card_id}", "")
                return UseResult("ok")
            if action == "casino_block" and not minutes:
                # (bloqueos viejos sin duración) armed=1 si el casino ya está cerrado: lo termina la próxima apertura; si está abierto, primero tiene que cerrarse
                eff = _add_effect(conn, target_id, "casino_block", None, card_id, owner_id, armed=0 if casino_open else 1)
            elif action in KINDS:
                who = owner_id if action == "shield" else target_id
                eff = _add_effect(conn, who, action, minutes, card_id, owner_id)
            else:
                raise ValueError(action)
            _consume(conn, card_id, owner_id)
            _record(conn, card_id, owner_id, prize_key, f"{action} → {eff.user_id}")
            _log(conn, owner_id, f"efx_{action}", eff.user_id, f"card:{card_id}", f"{minutes} min" if minutes else "")
            return UseResult("ok", effect=eff)
    finally:
        conn.close()


def undo_use(db_path: str, card_id: int, effect_id: Optional[int], actor_id: int) -> None:
    """Si Discord no deja aplicar el efecto (falta un rol, permisos), se devuelve la carta y se corta el efecto."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_cards SET status = 'available', updated_at = ? WHERE id = ? AND status = 'consumed'",
                         (utcnow_iso(), card_id))
            conn.execute("DELETE FROM wjp_requests WHERE request_key = ?", (f"efx:{card_id}",))
            if effect_id:
                conn.execute("UPDATE wjp_effects SET active = 0, ended_at = ?, ended_by = ? WHERE id = ?", (utcnow_iso(), actor_id, effect_id))
    finally:
        conn.close()


def consume_simple(db_path: str, *, card_id: int, owner_id: int, prize_key: str, note: str) -> bool:
    """Para cartas cuyo efecto lo hace otro módulo (abrir/cerrar casino): solo consume y registra."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if not _consume(conn, card_id, owner_id):
                return False
            _record(conn, card_id, owner_id, prize_key, note)
            _log(conn, owner_id, f"efx_{prize_key}", owner_id, f"card:{card_id}", note)
            return True
    finally:
        conn.close()


# ============================================================
# Cartas malas (☠️): se aplican solas al salir
# ============================================================
@dataclass
class ForcedResult:
    status: str                   # ok | blocked | gone
    message: str = ""
    effect: Optional[Effect] = None
    cooldown_hours: int = 0


def apply_forced(db_path: str, *, card_id: int, user_id: int, prize_key: str, action: str, amount: int,
                 minutes: Optional[float]) -> ForcedResult:
    """`action`: wuju | embo | mute_chat | mute_voice | cooldown | voice_dc. La carta (reservada al salir) pasa a usada."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            card = conn.execute("SELECT status FROM wjp_cards WHERE id = ? AND user_id = ?", (card_id, user_id)).fetchone()
            if card is None or card["status"] not in ("reserved", "available"):
                return ForcedResult("gone")
            _consume(conn, card_id, user_id, card["status"])
            prot = _protected(conn, user_id)
            if prot:
                why = "inmune" if prot == "immune" else "con escudo"
                _record(conn, card_id, user_id, prize_key, f"Bloqueada ({why})")
                return ForcedResult("blocked", "🛡️ Tenías escudo: la carta mala no te afectó." if prot == "shield"
                                    else "🛡️ Sos inmune: la carta mala no te afectó.")
            eff = None
            hours = 0
            if action == "wuju":
                op = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"wjp:forced:{card_id}",
                                          total_amount=amount, target_count=1, reason="Wujupón: carta mala",
                                          related_id=f"wjp:card:{card_id}")
                _, after = eco.tx_move(conn, op_id=op, user_id=user_id, delta=-amount, mov_type="wujupon_penalty",
                                       reason="Wujupón: carta mala", actor_id=user_id, related_id=f"wjp:card:{card_id}")
                msg = f"Se te descontaron **{eco.format_amount(amount)} Wuju Coins**. Balance: {eco.format_amount(after)}."
            elif action == "embo":
                ch = embo_db.tx_apply(conn, user_id=user_id, mode="remove", amount=amount, actor_id=user_id,
                                      request_key=f"wjp:forced:{card_id}", reason="Wujupón: carta mala", kind="wujupon_penalty")
                msg = f"Se te descontaron **{amount} de embo**. Balance de embo: {eco.format_amount(ch.after)}."
            elif action in ("mute_chat", "mute_voice"):
                eff = _add_effect(conn, user_id, action, minutes, card_id, user_id)
                msg = f"{KIND_LABELS[action]} por {int(minutes)} min."
            elif action == "voice_dc":
                msg = "Si estabas en un voice, te desconectó."      # la desconexión la hace la capa de Discord
            elif action == "cooldown":
                hours = int(amount)
                msg = f"Se sumaron **{hours} h** de cooldown para cobrar balance."
            else:
                raise ValueError(action)
            _record(conn, card_id, user_id, prize_key, msg)
            _log(conn, user_id, f"efx_forced_{action}", user_id, f"card:{card_id}", msg)
            return ForcedResult("ok", msg, eff, hours)
    finally:
        conn.close()


def pending_forced_cards(db_path: str) -> List[Tuple[int, int, str]]:
    """Cartas malas reservadas sin aplicar (por ejemplo, si el bot se reinició justo al salir): (carta, dueño, premio)."""
    conn = connect(db_path)
    try:
        return _safe(lambda: [(int(r["id"]), int(r["user_id"]), r["prize_key"]) for r in conn.execute(
            "SELECT c.id, c.user_id, c.prize_key FROM wjp_cards c JOIN wjp_prizes p ON p.key = c.prize_key "
            "WHERE c.status = 'reserved' AND p.forced = 1 AND p.handler LIKE 'efx:%' "
            "AND NOT EXISTS (SELECT 1 FROM wjp_requests r WHERE r.card_id = c.id)").fetchall()], [])
    finally:
        conn.close()


# ============================================================
# Vencimientos, consultas y Council
# ============================================================
def expire_due(db_path: str) -> List[Effect]:
    """Termina los efectos vencidos y los devuelve (para sacar roles)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            rows = conn.execute("SELECT * FROM wjp_effects WHERE active = 1 AND until IS NOT NULL AND until <= ?", (_now(),)).fetchall()
            if rows:
                conn.execute(f"UPDATE wjp_effects SET active = 0, ended_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                             (utcnow_iso(), *[r["id"] for r in rows]))
            return [_effect(r) for r in rows]
    finally:
        conn.close()


def list_active(db_path: str, user_id: Optional[int] = None) -> List[Effect]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        q = "SELECT * FROM wjp_effects WHERE active = 1 AND (until IS NULL OR until > ?)"
        args = [_now()]
        if user_id is not None:
            q += " AND user_id = ?"
            args.append(user_id)
        return [_effect(r) for r in conn.execute(q + " ORDER BY until", args).fetchall()]
    finally:
        conn.close()


def end_all(db_path: str, user_id: int, actor_id: int) -> List[Effect]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            ended = _end(conn, user_id, KINDS, actor_id)
            if ended:
                _log(conn, actor_id, "efx_end_all", user_id, "", ",".join(e.kind for e in ended))
            return ended
    finally:
        conn.close()


def set_immune(db_path: str, user_id: int, actor_id: int, reason: str, add: bool) -> bool:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if add:
                n = conn.execute("INSERT OR IGNORE INTO wjp_immune(user_id, reason, actor_id, created_at) VALUES (?,?,?,?)",
                                 (user_id, reason[:300], actor_id, utcnow_iso())).rowcount
            else:
                n = conn.execute("DELETE FROM wjp_immune WHERE user_id = ?", (user_id,)).rowcount
            if n:
                _log(conn, actor_id, "immune_add" if add else "immune_remove", user_id, "", reason[:300])
            return n > 0
    finally:
        conn.close()


def list_immune(db_path: str) -> List[Tuple[int, str]]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        return [(int(r["user_id"]), r["reason"] or "") for r in conn.execute("SELECT * FROM wjp_immune ORDER BY created_at")]
    finally:
        conn.close()


# ============================================================
# Ganchos para otros módulos (sin abrir transacciones propias)
# ============================================================
def tx_casino_changed(conn, opened: bool) -> None:
    """Llamado por casino_db al cambiar el estado: cerrar «arma» los bloqueos; abrir termina los armados."""
    def go():
        if opened:
            conn.execute("UPDATE wjp_effects SET active = 0, ended_at = ? WHERE kind = 'casino_block' AND active = 1 AND armed = 1 "
                         "AND until IS NULL",
                         (utcnow_iso(),))
        else:
            conn.execute("UPDATE wjp_effects SET armed = 1 WHERE kind = 'casino_block' AND active = 1 AND until IS NULL")
    _safe(go, None)


def tx_balance_frozen(conn, user_id: int) -> bool:
    return tx_has(conn, user_id, "freeze_balance")


# ============================================================
# Coinflip con tope y duelo especial (grupo C)
# ============================================================
GAMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS wjp_card_games (
    card_id  INTEGER PRIMARY KEY,
    game_id  INTEGER NOT NULL,
    owner_id INTEGER NOT NULL
);
"""


def reserve_card(db_path: str, card_id: int, owner_id: int) -> bool:
    """La carta queda reservada mientras se arma el juego (nadie la puede usar dos veces a la vez)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE wjp_cards SET status = 'reserved', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'available'",
                                (utcnow_iso(), card_id, owner_id)).rowcount == 1
    finally:
        conn.close()


def release_card(db_path: str, card_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_cards SET status = 'available', updated_at = ? WHERE id = ? AND status = 'reserved'",
                         (utcnow_iso(), card_id))
            _safe(lambda: conn.execute("DELETE FROM wjp_card_games WHERE card_id = ?", (card_id,)), None)
    finally:
        conn.close()


def finish_reserved(db_path: str, card_id: int, owner_id: int, prize_key: str, note: str) -> None:
    """Carta reservada → usada (duelo creado / coinflip jugado), con registro."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("UPDATE wjp_cards SET status = 'consumed', updated_at = ? WHERE id = ? AND status = 'reserved'",
                            (utcnow_iso(), card_id)).rowcount:
                _record(conn, card_id, owner_id, prize_key, note)
                _log(conn, owner_id, f"efx_{prize_key}", owner_id, f"card:{card_id}", note)
    finally:
        conn.close()


def bind_game(db_path: str, card_id: int, game_id: int, owner_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(GAMES_SCHEMA)
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO wjp_card_games(card_id, game_id, owner_id) VALUES (?,?,?)", (card_id, game_id, owner_id))
    finally:
        conn.close()


def settle_games(db_path: str) -> List[Tuple[int, str]]:
    """Coinflips de cartas ya terminados: si se jugó, la carta se consume; si nadie aceptó, vuelve al inventario."""
    conn = connect(db_path)
    out = []
    try:
        conn.executescript(GAMES_SCHEMA)
        with write_tx(conn):
            rows = _safe(lambda: conn.execute(
                "SELECT b.card_id, b.owner_id, g.status, c.prize_key FROM wjp_card_games b JOIN games g ON g.id = b.game_id "
                "JOIN wjp_cards c ON c.id = b.card_id WHERE g.status != 'open'").fetchall(), [])
            now = utcnow_iso()
            for r in rows:
                played = r["status"] == "done"
                conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ? AND status = 'reserved'",
                             ("consumed" if played else "available", now, r["card_id"]))
                if played:
                    _record(conn, int(r["card_id"]), int(r["owner_id"]), r["prize_key"], "Coinflip jugado")
                conn.execute("DELETE FROM wjp_card_games WHERE card_id = ?", (r["card_id"],))
                out.append((int(r["card_id"]), "consumed" if played else "released"))
        return out
    finally:
        conn.close()
