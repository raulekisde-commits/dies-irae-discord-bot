"""Registro y vinculación Discord ↔ personaje de Albion (lógica y persistencia, sin Discord).

Modelo de seguridad (ver registration.py para el flujo completo):
  * Consultar la API de Albion solo demuestra que el personaje EXISTE (y a qué guild pertenece), no quién lo controla.
  * La propiedad se comprueba con un código aleatorio de un solo uso que la persona le envía a un oficial DENTRO del juego
    (susurro o chat de guild) desde ese personaje. El oficial lo escribe en Discord; acá solo se guarda un hash con sal.
  * La vinculación definitiva solo se guarda después de: aceptación del dueño de la cuenta de Discord → código verificado
    por alguien distinto del dueño → confirmación final del dueño.
"""
import hashlib
from contextlib import contextmanager
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # sin 0/O/1/I/L para que se pueda dictar sin errores
CODE_TTL_MINUTES = 60 * 24                               # el código vence a las 24 h
PENDING_TTL_MINUTES = 15                                 # el dueño tiene 15 min para aceptar la solicitud
VERIFIED_TTL_MINUTES = 60                                # y 1 h para confirmar una vez verificado
MAX_CODE_ATTEMPTS = 5
MAX_REQUESTS_PER_HOUR = 5                                # por persona que inicia solicitudes (anti-spam / suplantación repetida)

ACTIVE = ("pending_target", "awaiting_code", "verified")

SCHEMA = """
CREATE TABLE IF NOT EXISTS albion_registrations (
    discord_id    INTEGER PRIMARY KEY,
    char_id       TEXT,                                   -- ID único de gameinfo (NULL solo en vínculos administrativos sin API)
    char_name     TEXT NOT NULL,
    region        TEXT,
    guild_name    TEXT,
    status        TEXT NOT NULL DEFAULT 'verified',       -- verified | admin_linked
    method        TEXT NOT NULL DEFAULT 'ingame_code',    -- ingame_code | admin_override
    verified_by   INTEGER,
    registered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_reg_char_id ON albion_registrations(char_id) WHERE char_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_reg_char_name ON albion_registrations(lower(char_name));
CREATE TABLE IF NOT EXISTS albion_reg_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     INTEGER NOT NULL,
    initiator_id  INTEGER NOT NULL,
    char_id       TEXT,
    char_name     TEXT NOT NULL,
    region        TEXT,
    guild_name    TEXT,
    info_json     TEXT,
    status        TEXT NOT NULL,                          -- pending_target | awaiting_code | verified | completed | rejected | expired | cancelled
    code_salt     TEXT,
    code_hash     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    verified_by   INTEGER,
    channel_id    INTEGER,
    prompt_msg_id INTEGER,
    review_msg_id INTEGER,
    confirm_msg_id INTEGER,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_req_target_active ON albion_reg_requests(target_id)
    WHERE status IN ('pending_target','awaiting_code','verified');
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_req_char_active ON albion_reg_requests(lower(char_name))
    WHERE status IN ('pending_target','awaiting_code','verified');
CREATE TABLE IF NOT EXISTS albion_reg_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    target_id  INTEGER,
    request_id INTEGER,
    detail     TEXT
);
"""


class RegError(ValueError):
    """Mensaje que se le puede mostrar tal cual a quien lo causó. La transacción se revierte."""


class PersistedRegError(RegError):
    """Igual que RegError, pero la transacción se CONFIRMA antes de propagar el error: sirve para dejar asentados
    los intentos fallidos, los vencimientos y la auditoría (si no, el rollback los borraría y el límite de intentos no serviría)."""


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except PersistedRegError:
        conn.execute("COMMIT")
        raise
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


@dataclass
class Request:
    id: int
    target_id: int
    initiator_id: int
    char_id: Optional[str]
    char_name: str
    region: Optional[str]
    guild_name: Optional[str]
    info: dict
    status: str
    attempts: int
    verified_by: Optional[int]
    channel_id: Optional[int]
    prompt_msg_id: Optional[int]
    review_msg_id: Optional[int]
    confirm_msg_id: Optional[int]
    expires_at: str

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE


@dataclass
class Registration:
    discord_id: int
    char_id: Optional[str]
    char_name: str
    region: Optional[str]
    guild_name: Optional[str]
    status: str
    method: str
    verified_by: Optional[int]
    registered_at: str
    updated_at: str


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _req(r) -> Optional[Request]:
    if r is None:
        return None
    try:
        info = json.loads(r["info_json"] or "{}")
    except ValueError:
        info = {}
    return Request(int(r["id"]), int(r["target_id"]), int(r["initiator_id"]), r["char_id"], r["char_name"], r["region"],
                   r["guild_name"], info, r["status"], int(r["attempts"]), r["verified_by"], r["channel_id"], r["prompt_msg_id"],
                   r["review_msg_id"], r["confirm_msg_id"], r["expires_at"])


def _reg(r) -> Optional[Registration]:
    if r is None:
        return None
    return Registration(int(r["discord_id"]), r["char_id"], r["char_name"], r["region"], r["guild_name"], r["status"], r["method"],
                        r["verified_by"], r["registered_at"], r["updated_at"])


def _audit(conn, actor_id, action: str, target_id=None, request_id=None, detail: str = "") -> None:
    # Nunca se guardan códigos ni secretos: solo hechos (quién, qué, sobre quién).
    conn.execute("INSERT INTO albion_reg_audit(at, actor_id, action, target_id, request_id, detail) VALUES (?,?,?,?,?,?)",
                 (utcnow_iso(), actor_id, action, target_id, request_id, (detail or "")[:500]))


def audit(db_path: str, actor_id, action: str, target_id=None, request_id=None, detail: str = "") -> None:
    conn = connect(db_path)
    try:
        with _tx(conn):
            _audit(conn, actor_id, action, target_id, request_id, detail)
    finally:
        conn.close()


def recent_audit(db_path: str, target_id: int, limit: int = 8) -> List[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return list(conn.execute("SELECT * FROM albion_reg_audit WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id, limit)))
    finally:
        conn.close()


# ---------- consultas ----------
def get_registration(db_path: str, discord_id: int) -> Optional[Registration]:
    conn = connect(db_path)
    try:
        return _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (discord_id,)).fetchone())
    finally:
        conn.close()


def is_registered(db_path: str, discord_id: int) -> bool:
    return get_registration(db_path, discord_id) is not None


def owner_of_character(db_path: str, char_id: Optional[str], char_name: str) -> Optional[int]:
    """ID de Discord que tiene vinculado ese personaje (solo para uso interno: nunca se muestra a terceros)."""
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT discord_id FROM albion_registrations WHERE lower(char_name)=lower(?) OR (char_id IS NOT NULL AND char_id=?)",
                         (char_name, char_id)).fetchone()
        return int(r["discord_id"]) if r else None
    finally:
        conn.close()


def get_request(db_path: str, request_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def find_request_by_message(db_path: str, message_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute(
            "SELECT * FROM albion_reg_requests WHERE prompt_msg_id=? OR review_msg_id=? OR confirm_msg_id=? ORDER BY id DESC LIMIT 1",
            (message_id, message_id, message_id)).fetchone())
    finally:
        conn.close()


def active_request_for(db_path: str, target_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute(
            "SELECT * FROM albion_reg_requests WHERE target_id=? AND status IN ('pending_target','awaiting_code','verified')",
            (target_id,)).fetchone())
    finally:
        conn.close()


def set_messages(db_path: str, request_id: int, **cols) -> None:
    allowed = {"channel_id", "prompt_msg_id", "review_msg_id", "confirm_msg_id"}
    cols = {k: v for k, v in cols.items() if k in allowed}
    if not cols:
        return
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute(f"UPDATE albion_reg_requests SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), request_id))
    finally:
        conn.close()


# ---------- solicitudes ----------
def create_request(db_path: str, *, target_id: int, initiator_id: int, char_id: Optional[str], char_name: str,
                   region: Optional[str], guild_name: Optional[str], info: Optional[dict] = None,
                   channel_id: Optional[int] = None) -> Request:
    """Abre una solicitud. Falla con RegError (mensaje seguro) si el personaje ya está vinculado, si hay otra solicitud
    en curso para esa persona o ese personaje, o si quien inicia superó el límite por hora."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            _expire(conn)
            since = _iso(_now() - timedelta(hours=1))
            n = conn.execute("SELECT COUNT(*) FROM albion_reg_requests WHERE initiator_id=? AND created_at>=?", (initiator_id, since)).fetchone()[0]
            if n >= MAX_REQUESTS_PER_HOUR:
                _audit(conn, initiator_id, "rate_limited", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Hiciste demasiadas solicitudes en la última hora. Esperá un rato e intentá de nuevo.")
            owner = conn.execute("SELECT discord_id FROM albion_registrations WHERE lower(char_name)=lower(?) OR (char_id IS NOT NULL AND char_id=?)",
                                 (char_name, char_id)).fetchone()
            if owner is not None:
                if int(owner["discord_id"]) == target_id:
                    raise RegError("Esa cuenta ya está vinculada a ese personaje.")
                _audit(conn, initiator_id, "dup_character_attempt", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Ese personaje ya está vinculado a otra cuenta de Discord. Si es un error, pedile a un administrador que lo revise.")
            if conn.execute("SELECT 1 FROM albion_reg_requests WHERE target_id=? AND status IN ('pending_target','awaiting_code','verified')",
                            (target_id,)).fetchone():
                raise RegError("Ya hay una solicitud de registro en curso para esa persona. Terminala o esperá a que venza.")
            if conn.execute("SELECT 1 FROM albion_reg_requests WHERE lower(char_name)=lower(?) AND status IN ('pending_target','awaiting_code','verified')",
                            (char_name,)).fetchone():
                _audit(conn, initiator_id, "dup_request_attempt", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Ya hay una solicitud en curso para ese personaje.")
            now = _now()
            try:
                cur = conn.execute(
                    "INSERT INTO albion_reg_requests(target_id, initiator_id, char_id, char_name, region, guild_name, info_json, status, channel_id, "
                    "expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'pending_target', ?, ?, ?, ?)",
                    (target_id, initiator_id, char_id, char_name, region, guild_name, json.dumps(info or {}), channel_id,
                     _iso(now + timedelta(minutes=PENDING_TTL_MINUTES)), _iso(now), _iso(now)))
            except sqlite3.IntegrityError:
                raise RegError("Ya hay una solicitud en curso para esa persona o ese personaje.")
            rid = int(cur.lastrowid)
            _audit(conn, initiator_id, "request_created", target_id, rid, f"personaje={char_name} region={region}")
            return _req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (rid,)).fetchone())
    finally:
        conn.close()


def _expire(conn) -> List[int]:
    now = _iso(_now())
    rows = conn.execute("SELECT id, target_id FROM albion_reg_requests WHERE status IN ('pending_target','awaiting_code','verified') AND expires_at<?",
                        (now,)).fetchall()
    for r in rows:
        conn.execute("UPDATE albion_reg_requests SET status='expired', code_hash=NULL, code_salt=NULL, updated_at=? WHERE id=?", (now, r["id"]))
        _audit(conn, None, "request_expired", int(r["target_id"]), int(r["id"]))
    return [int(r["id"]) for r in rows]


def expire_old(db_path: str) -> List[Request]:
    """Marca como vencidas las solicitudes cuyo plazo pasó y las devuelve (para avisar). Los códigos vencidos se borran."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            ids = _expire(conn)
            return [_req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (i,)).fetchone()) for i in ids]
    finally:
        conn.close()


def _load_active(conn, request_id: int) -> Request:
    _expire(conn)
    r = _req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (request_id,)).fetchone())
    if r is None:
        raise RegError("No encuentro esa solicitud.")
    if r.status == "expired":
        raise PersistedRegError("La solicitud venció. Empezá de nuevo con `/registrar`.")
    if not r.is_active:
        raise RegError("Esa solicitud ya no está activa.")
    return r


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256((salt + normalize_code(code)).encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def target_accept(db_path: str, request_id: int, user_id: int) -> str:
    """El dueño de la cuenta de Discord acepta. Devuelve el código en texto plano UNA sola vez (solo queda su hash)."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            if user_id != r.target_id:
                _audit(conn, user_id, "impersonation_attempt", r.target_id, request_id, "intentó aceptar una solicitud ajena")
                raise PersistedRegError("Esta solicitud no es para vos.")
            if r.status != "pending_target":
                raise RegError("Esta solicitud ya fue aceptada.")
            code = new_code()
            salt = secrets.token_hex(8)
            conn.execute("UPDATE albion_reg_requests SET status='awaiting_code', code_salt=?, code_hash=?, attempts=0, expires_at=?, updated_at=? WHERE id=?",
                         (salt, _hash(salt, code), _iso(_now() + timedelta(minutes=CODE_TTL_MINUTES)), _iso(_now()), request_id))
            _audit(conn, user_id, "target_accepted", r.target_id, request_id, f"personaje={r.char_name}")
            return code
    finally:
        conn.close()


def verify_code(db_path: str, request_id: int, reviewer_id: int, code: str) -> Request:
    """Un oficial (distinto del dueño y de quien inició) escribe el código que vio en el juego. Un solo uso, con límite de intentos."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            if reviewer_id in (r.target_id, r.initiator_id):
                _audit(conn, reviewer_id, "self_review_attempt", r.target_id, request_id, "intentó verificar su propia solicitud")
                raise PersistedRegError("No podés verificar una solicitud en la que participás: tiene que hacerlo otra persona autorizada.")
            if r.status != "awaiting_code":
                raise RegError("Esta solicitud no está esperando un código.")
            row = conn.execute("SELECT code_salt, code_hash FROM albion_reg_requests WHERE id=?", (request_id,)).fetchone()
            ok = hmac.compare_digest(_hash(row["code_salt"], code), row["code_hash"])
            if not ok:
                att = r.attempts + 1
                if att >= MAX_CODE_ATTEMPTS:
                    conn.execute("UPDATE albion_reg_requests SET status='cancelled', attempts=?, code_hash=NULL, code_salt=NULL, updated_at=? WHERE id=?",
                                 (att, _iso(_now()), request_id))
                    _audit(conn, reviewer_id, "code_failed_locked", r.target_id, request_id, "demasiados intentos: solicitud cancelada")
                    raise PersistedRegError("Código incorrecto. Se agotaron los intentos: la solicitud se canceló y hay que empezar de nuevo.")
                conn.execute("UPDATE albion_reg_requests SET attempts=?, updated_at=? WHERE id=?", (att, _iso(_now()), request_id))
                _audit(conn, reviewer_id, "code_failed", r.target_id, request_id, f"intento {att}/{MAX_CODE_ATTEMPTS}")
                raise PersistedRegError(f"Código incorrecto ({att}/{MAX_CODE_ATTEMPTS} intentos).")
            conn.execute("UPDATE albion_reg_requests SET status='verified', verified_by=?, code_hash=NULL, code_salt=NULL, expires_at=?, updated_at=? WHERE id=?",
                         (reviewer_id, _iso(_now() + timedelta(minutes=VERIFIED_TTL_MINUTES)), _iso(_now()), request_id))
            _audit(conn, reviewer_id, "code_verified", r.target_id, request_id, f"personaje={r.char_name}")
            return _req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def complete(db_path: str, request_id: int, user_id: int) -> Tuple[Registration, Optional[Registration]]:
    """Confirmación final del dueño: recién acá se guarda la vinculación. Devuelve (nueva, anterior si la había)."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            if user_id != r.target_id:
                _audit(conn, user_id, "impersonation_attempt", r.target_id, request_id, "intentó confirmar un registro ajeno")
                raise PersistedRegError("Esta confirmación no es tuya.")
            if r.status != "verified":
                raise RegError("Todavía falta la verificación del personaje.")
            now = utcnow_iso()
            old = _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (user_id,)).fetchone())
            try:
                if old is not None:
                    conn.execute("DELETE FROM albion_registrations WHERE discord_id=?", (user_id,))
                conn.execute(
                    "INSERT INTO albion_registrations(discord_id, char_id, char_name, region, guild_name, status, method, verified_by, registered_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'verified', 'ingame_code', ?, ?, ?)",
                    (user_id, r.char_id, r.char_name, r.region, r.guild_name, r.verified_by, now, now))
            except sqlite3.IntegrityError:               # otra solicitud ganó la carrera por el mismo personaje
                raise RegError("Ese personaje acaba de quedar vinculado a otra cuenta. No se guardó nada.")
            conn.execute("UPDATE albion_reg_requests SET status='completed', updated_at=? WHERE id=?", (_iso(_now()), request_id))
            _audit(conn, user_id, "registered", user_id, request_id,
                   f"personaje={r.char_name} verificado_por={r.verified_by}" + (f" reemplaza={old.char_name}" if old else ""))
            return _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (user_id,)).fetchone()), old
    finally:
        conn.close()


def close_request(db_path: str, request_id: int, actor_id: int, status: str, reason: str = "") -> Optional[Request]:
    """Rechaza o cancela una solicitud activa (status: 'rejected' | 'cancelled')."""
    assert status in ("rejected", "cancelled")
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            conn.execute("UPDATE albion_reg_requests SET status=?, code_hash=NULL, code_salt=NULL, updated_at=? WHERE id=?", (status, _iso(_now()), request_id))
            _audit(conn, actor_id, f"request_{status}", r.target_id, request_id, reason or f"personaje={r.char_name}")
            return _req(conn.execute("SELECT * FROM albion_reg_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def cancel_active_for(db_path: str, target_id: int, actor_id: Optional[int], reason: str) -> int:
    conn = connect(db_path)
    try:
        with _tx(conn):
            rows = conn.execute("SELECT id FROM albion_reg_requests WHERE target_id=? AND status IN ('pending_target','awaiting_code','verified')",
                                (target_id,)).fetchall()
            for r in rows:
                conn.execute("UPDATE albion_reg_requests SET status='cancelled', code_hash=NULL, code_salt=NULL, updated_at=? WHERE id=?",
                             (_iso(_now()), r["id"]))
                _audit(conn, actor_id, "request_cancelled", target_id, int(r["id"]), reason)
            return len(rows)
    finally:
        conn.close()


# ---------- vínculos ----------
def unlink(db_path: str, discord_id: int, actor_id: Optional[int], reason: str = "") -> Optional[Registration]:
    conn = connect(db_path)
    try:
        with _tx(conn):
            old = _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (discord_id,)).fetchone())
            if old is None:
                return None
            conn.execute("DELETE FROM albion_registrations WHERE discord_id=?", (discord_id,))
            _audit(conn, actor_id, "unlinked", discord_id, None, f"personaje={old.char_name} {reason}".strip())
            return old
    finally:
        conn.close()


def admin_link(db_path: str, discord_id: int, char_name: str, actor_id: int, *, char_id: Optional[str] = None,
               region: Optional[str] = None, guild_name: Optional[str] = None, replace: bool = False) -> Tuple[Registration, Optional[Registration]]:
    """Vínculo administrativo excepcional: NO verifica propiedad. Queda marcado como 'admin_linked' y auditado."""
    char_name = (char_name or "").strip()
    if not char_name or len(char_name) > 40:
        raise RegError("Nombre de personaje inválido.")
    conn = connect(db_path)
    try:
        with _tx(conn):
            owner = conn.execute("SELECT discord_id FROM albion_registrations WHERE lower(char_name)=lower(?) OR (char_id IS NOT NULL AND char_id=?)",
                                 (char_name, char_id)).fetchone()
            if owner is not None and int(owner["discord_id"]) != discord_id:
                raise RegError("Ese personaje ya está vinculado a otra cuenta. Desvinculalo primero con `/admin_desvincular`.")
            old = _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (discord_id,)).fetchone())
            if old is not None and not replace and old.char_name.lower() != char_name.lower():
                raise RegError(f"Esa cuenta ya está vinculada a **{old.char_name}**. Desvinculala primero o confirmá el reemplazo.")
            now = utcnow_iso()
            if old is not None:
                conn.execute("DELETE FROM albion_registrations WHERE discord_id=?", (discord_id,))
            try:
                conn.execute(
                    "INSERT INTO albion_registrations(discord_id, char_id, char_name, region, guild_name, status, method, verified_by, registered_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'admin_linked', 'admin_override', ?, ?, ?)",
                    (discord_id, char_id, char_name, region, guild_name, actor_id, now, now))
            except sqlite3.IntegrityError:
                raise RegError("Ese personaje ya está vinculado a otra cuenta.")
            conn.execute("UPDATE albion_reg_requests SET status='cancelled', code_hash=NULL, code_salt=NULL, updated_at=? WHERE target_id=? "
                         "AND status IN ('pending_target','awaiting_code','verified')", (_iso(_now()), discord_id))
            _audit(conn, actor_id, "admin_linked", discord_id, None,
                   f"personaje={char_name} SIN verificación de propiedad" + (f" reemplaza={old.char_name}" if old else ""))
            return _reg(conn.execute("SELECT * FROM albion_registrations WHERE discord_id=?", (discord_id,)).fetchone()), old
    finally:
        conn.close()


# ---------- ajustes (obligatoriedad y canal de revisión) ----------
def get_flag(db_path: str, key: str, default: str = "") -> str:
    import economy_db as eco
    return eco.get_setting(db_path, key) or default


def set_flag(db_path: str, key: str, value: Optional[str]) -> None:
    import economy_db as eco
    eco.set_setting(db_path, key, value)


REQUIRED_KEY = "registro:obligatorio"
REVIEW_CHANNEL_KEY = "registro:canal"
