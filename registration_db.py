"""Registro y vinculación Discord ↔ personajes de Albion (lógica y persistencia, sin Discord).

Reglas:
  * Un personaje de Albion solo puede estar vinculado a UNA cuenta de Discord, y una cuenta de Discord solo puede tener UN personaje
    (índices únicos por cuenta, por ID y por nombre).
  * Registrar a otra persona exige que ella misma acepte la solicitud desde su cuenta; hasta entonces no se guarda nada.
  * No hay verificación de propiedad del personaje (la API pública de Albion no la permite): ver registration.py.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dbutil import connect, utcnow_iso

PENDING_TTL_MINUTES = 15                                 # el dueño de la cuenta tiene 15 min para aceptar
MAX_REQUESTS_PER_HOUR = 5                                # por persona que inicia solicitudes (anti-spam / suplantación repetida)

SCHEMA = """
CREATE TABLE IF NOT EXISTS albion_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    INTEGER NOT NULL,
    char_id       TEXT,                                   -- ID único de gameinfo (NULL solo en vínculos administrativos sin API)
    char_name     TEXT NOT NULL,
    region        TEXT,
    guild_name    TEXT,
    method        TEXT NOT NULL DEFAULT 'confirmed',      -- confirmed (aceptó la persona) | admin_override | recruitment
    linked_by     INTEGER,
    registered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_links_discord ON albion_links(discord_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_links_char_id ON albion_links(char_id) WHERE char_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_links_char_name ON albion_links(lower(char_name));
CREATE TABLE IF NOT EXISTS albion_link_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     INTEGER NOT NULL,
    initiator_id  INTEGER NOT NULL,
    char_id       TEXT,
    char_name     TEXT NOT NULL,
    region        TEXT,
    guild_name    TEXT,
    info_json     TEXT,
    status        TEXT NOT NULL,                          -- pending_target | completed | rejected | expired | cancelled
    channel_id    INTEGER,
    prompt_msg_id INTEGER,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_linkreq_char_active ON albion_link_requests(lower(char_name)) WHERE status = 'pending_target';
CREATE TABLE IF NOT EXISTS recruit_pending (
    discord_id   INTEGER PRIMARY KEY,
    char_name    TEXT NOT NULL,
    char_id      TEXT,
    role_id      INTEGER NOT NULL,                       -- rol principal (Tank, Healer…) que se da junto con Miembro
    role_name    TEXT NOT NULL,
    recruiter_id INTEGER,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS albion_link_audit (
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
    """Igual, pero la transacción se CONFIRMA antes de propagar el error: sirve para dejar asentados vencimientos y auditoría."""


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
    channel_id: Optional[int]
    prompt_msg_id: Optional[int]
    expires_at: str

    @property
    def is_active(self) -> bool:
        return self.status == "pending_target"


@dataclass
class Registration:
    id: int
    discord_id: int
    char_id: Optional[str]
    char_name: str
    region: Optional[str]
    guild_name: Optional[str]
    method: str
    linked_by: Optional[int]
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
    return Request(int(r["id"]), int(r["target_id"]), int(r["initiator_id"]), r["char_id"], r["char_name"], r["region"], r["guild_name"],
                   info, r["status"], r["channel_id"], r["prompt_msg_id"], r["expires_at"])


def _reg(r) -> Optional[Registration]:
    if r is None:
        return None
    return Registration(int(r["id"]), int(r["discord_id"]), r["char_id"], r["char_name"], r["region"], r["guild_name"], r["method"],
                        r["linked_by"], r["registered_at"], r["updated_at"])


def _audit(conn, actor_id, action: str, target_id=None, request_id=None, detail: str = "") -> None:
    conn.execute("INSERT INTO albion_link_audit(at, actor_id, action, target_id, request_id, detail) VALUES (?,?,?,?,?,?)",
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
        return list(conn.execute("SELECT * FROM albion_link_audit WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id, limit)))
    finally:
        conn.close()


# ---------- consultas ----------
def get_registrations(db_path: str, discord_id: int) -> List[Registration]:
    conn = connect(db_path)
    try:
        return [_reg(r) for r in conn.execute("SELECT * FROM albion_links WHERE discord_id=? ORDER BY id", (discord_id,))]
    finally:
        conn.close()


def all_registrations(db_path: str) -> List[Registration]:
    conn = connect(db_path)
    try:
        return [_reg(r) for r in conn.execute("SELECT * FROM albion_links ORDER BY id")]
    finally:
        conn.close()


def get_registration(db_path: str, discord_id: int) -> Optional[Registration]:
    regs = get_registrations(db_path, discord_id)
    return regs[0] if regs else None


def registration_required(db_path: str) -> bool:
    """Por defecto SÍ se exige estar registrado; un administrador puede apagarlo con /registro_obligatorio desactivar."""
    return get_flag(db_path, REQUIRED_KEY) != "0"


def is_registered(db_path: str, discord_id: int) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM albion_links WHERE discord_id=? LIMIT 1", (discord_id,)).fetchone() is not None
    finally:
        conn.close()


def get_request(db_path: str, request_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute("SELECT * FROM albion_link_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def find_request_by_message(db_path: str, message_id: int) -> Optional[Request]:
    conn = connect(db_path)
    try:
        return _req(conn.execute("SELECT * FROM albion_link_requests WHERE prompt_msg_id=? ORDER BY id DESC LIMIT 1", (message_id,)).fetchone())
    finally:
        conn.close()


def pending_requests_for(db_path: str, target_id: int) -> List[Request]:
    conn = connect(db_path)
    try:
        return [_req(r) for r in conn.execute("SELECT * FROM albion_link_requests WHERE target_id=? AND status='pending_target'", (target_id,))]
    finally:
        conn.close()


def set_messages(db_path: str, request_id: int, **cols) -> None:
    allowed = {"channel_id", "prompt_msg_id"}
    cols = {k: v for k, v in cols.items() if k in allowed}
    if not cols:
        return
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute(f"UPDATE albion_link_requests SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), request_id))
    finally:
        conn.close()


# ---------- solicitudes ----------
def _expire(conn) -> List[int]:
    now = _iso(_now())
    rows = conn.execute("SELECT id, target_id FROM albion_link_requests WHERE status='pending_target' AND expires_at<?", (now,)).fetchall()
    for r in rows:
        conn.execute("UPDATE albion_link_requests SET status='expired', updated_at=? WHERE id=?", (now, r["id"]))
        _audit(conn, None, "request_expired", int(r["target_id"]), int(r["id"]))
    return [int(r["id"]) for r in rows]


def expire_old(db_path: str) -> List[Request]:
    conn = connect(db_path)
    try:
        with _tx(conn):
            ids = _expire(conn)
            return [_req(conn.execute("SELECT * FROM albion_link_requests WHERE id=?", (i,)).fetchone()) for i in ids]
    finally:
        conn.close()


def create_request(db_path: str, *, target_id: int, initiator_id: int, char_id: Optional[str], char_name: str,
                   region: Optional[str], guild_name: Optional[str], info: Optional[dict] = None,
                   channel_id: Optional[int] = None) -> Request:
    """Abre una solicitud. Falla con RegError (mensaje seguro, sin revelar quién tiene el personaje) si ese personaje ya está vinculado
    a otra cuenta, si ya hay una solicitud en curso para él, o si quien inicia superó el límite por hora."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            _expire(conn)
            since = _iso(_now() - timedelta(hours=1))
            if conn.execute("SELECT COUNT(*) FROM albion_link_requests WHERE initiator_id=? AND created_at>=?", (initiator_id, since)).fetchone()[0] >= MAX_REQUESTS_PER_HOUR:
                _audit(conn, initiator_id, "rate_limited", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Hiciste demasiadas solicitudes en la última hora. Esperá un rato e intentá de nuevo.")
            owner = conn.execute("SELECT discord_id FROM albion_links WHERE lower(char_name)=lower(?) OR (char_id IS NOT NULL AND char_id=?)",
                                 (char_name, char_id)).fetchone()
            if owner is not None:
                if int(owner["discord_id"]) == target_id:
                    raise RegError("Esa cuenta ya tiene vinculado ese personaje.")
                _audit(conn, initiator_id, "dup_character_attempt", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Ese personaje ya está vinculado a otra cuenta de Discord. Si es un error, pedile a un administrador que lo revise.")
            mine = conn.execute("SELECT char_name FROM albion_links WHERE discord_id=?", (target_id,)).fetchone()
            if mine is not None:
                raise RegError(f"Esa cuenta ya tiene un personaje vinculado (**{mine['char_name']}**). Una cuenta de Discord solo puede tener uno: "
                               "primero hay que desvincularlo (`/desvincular`).")
            if conn.execute("SELECT 1 FROM albion_link_requests WHERE lower(char_name)=lower(?) AND status='pending_target'", (char_name,)).fetchone():
                _audit(conn, initiator_id, "dup_request_attempt", target_id, None, f"personaje={char_name}")
                raise PersistedRegError("Ya hay una solicitud en curso para ese personaje.")
            now = _now()
            try:
                cur = conn.execute(
                    "INSERT INTO albion_link_requests(target_id, initiator_id, char_id, char_name, region, guild_name, info_json, status, channel_id, "
                    "expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'pending_target', ?, ?, ?, ?)",
                    (target_id, initiator_id, char_id, char_name, region, guild_name, json.dumps(info or {}), channel_id,
                     _iso(now + timedelta(minutes=PENDING_TTL_MINUTES)), _iso(now), _iso(now)))
            except sqlite3.IntegrityError:
                raise RegError("Ya hay una solicitud en curso para ese personaje.")
            rid = int(cur.lastrowid)
            _audit(conn, initiator_id, "request_created", target_id, rid, f"personaje={char_name} region={region}")
            return _req(conn.execute("SELECT * FROM albion_link_requests WHERE id=?", (rid,)).fetchone())
    finally:
        conn.close()


def _load_active(conn, request_id: int) -> Request:
    _expire(conn)
    r = _req(conn.execute("SELECT * FROM albion_link_requests WHERE id=?", (request_id,)).fetchone())
    if r is None:
        raise RegError("No encuentro esa solicitud.")
    if r.status == "expired":
        raise PersistedRegError("La solicitud venció. Empezá de nuevo con `/registrar`.")
    if not r.is_active:
        raise RegError("Esa solicitud ya no está activa.")
    return r


def accept(db_path: str, request_id: int, user_id: int) -> Registration:
    """El dueño de la cuenta de Discord acepta: recién acá se guarda la vinculación. Nadie más puede aceptar por él."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            if user_id != r.target_id:
                _audit(conn, user_id, "impersonation_attempt", r.target_id, request_id, "intentó aceptar una solicitud ajena")
                raise PersistedRegError("Esta solicitud no es para vos.")
            now = utcnow_iso()
            try:
                cur = conn.execute(
                    "INSERT INTO albion_links(discord_id, char_id, char_name, region, guild_name, method, linked_by, registered_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'confirmed', ?, ?, ?)", (user_id, r.char_id, r.char_name, r.region, r.guild_name, r.initiator_id, now, now))
            except sqlite3.IntegrityError:               # carrera: se vinculó antes ese personaje, o esta cuenta ya quedó con otro
                if conn.execute("SELECT 1 FROM albion_links WHERE discord_id=?", (user_id,)).fetchone():
                    raise RegError("Esa cuenta ya tiene un personaje vinculado: una cuenta de Discord solo puede tener uno. No se guardó nada.")
                raise RegError("Ese personaje acaba de quedar vinculado a otra cuenta. No se guardó nada.")
            conn.execute("UPDATE albion_link_requests SET status='completed', updated_at=? WHERE id=?", (_iso(_now()), request_id))
            _audit(conn, user_id, "registered", user_id, request_id, f"personaje={r.char_name} iniciado_por={r.initiator_id}")
            return _reg(conn.execute("SELECT * FROM albion_links WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def close_request(db_path: str, request_id: int, actor_id: int, status: str, reason: str = "") -> Optional[Request]:
    assert status in ("rejected", "cancelled")
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = _load_active(conn, request_id)
            conn.execute("UPDATE albion_link_requests SET status=?, updated_at=? WHERE id=?", (status, _iso(_now()), request_id))
            _audit(conn, actor_id, f"request_{status}", r.target_id, request_id, reason or f"personaje={r.char_name}")
            return _req(conn.execute("SELECT * FROM albion_link_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def cancel_active_for(db_path: str, target_id: int, actor_id: Optional[int], reason: str) -> int:
    conn = connect(db_path)
    try:
        with _tx(conn):
            rows = conn.execute("SELECT id FROM albion_link_requests WHERE target_id=? AND status='pending_target'", (target_id,)).fetchall()
            for r in rows:
                conn.execute("UPDATE albion_link_requests SET status='cancelled', updated_at=? WHERE id=?", (_iso(_now()), r["id"]))
                _audit(conn, actor_id, "request_cancelled", target_id, int(r["id"]), reason)
            return len(rows)
    finally:
        conn.close()


# ---------- vínculos ----------
def unlink(db_path: str, discord_id: int, actor_id: Optional[int], char_name: Optional[str] = None, reason: str = "") -> List[Registration]:
    """Quita un personaje (char_name) o todos los de la cuenta (char_name=None). Devuelve lo que se quitó."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            if char_name is None:
                rows = conn.execute("SELECT * FROM albion_links WHERE discord_id=?", (discord_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM albion_links WHERE discord_id=? AND lower(char_name)=lower(?)", (discord_id, char_name)).fetchall()
            for r in rows:
                conn.execute("DELETE FROM albion_links WHERE id=?", (r["id"],))
                _audit(conn, actor_id, "unlinked", discord_id, None, f"personaje={r['char_name']} {reason}".strip())
            return [_reg(r) for r in rows]
    finally:
        conn.close()


def admin_link(db_path: str, discord_id: int, char_name: str, actor_id: int, *, char_id: Optional[str] = None,
               region: Optional[str] = None, guild_name: Optional[str] = None, method: str = "admin_override") -> Registration:
    """Vínculo administrativo excepcional: la persona no tuvo que aceptar. Queda marcado como 'admin_override' y auditado."""
    char_name = (char_name or "").strip()
    if not char_name or len(char_name) > 40:
        raise RegError("Nombre de personaje inválido.")
    conn = connect(db_path)
    try:
        with _tx(conn):
            owner = conn.execute("SELECT discord_id FROM albion_links WHERE lower(char_name)=lower(?) OR (char_id IS NOT NULL AND char_id=?)",
                                 (char_name, char_id)).fetchone()
            if owner is not None:
                if int(owner["discord_id"]) == discord_id:
                    raise RegError("Esa cuenta ya tiene vinculado ese personaje.")
                raise RegError("Ese personaje ya está vinculado a otra cuenta. Desvinculalo primero con `/admin_desvincular`.")
            mine = conn.execute("SELECT char_name FROM albion_links WHERE discord_id=?", (discord_id,)).fetchone()
            if mine is not None:
                raise RegError(f"Esa cuenta ya tiene vinculado **{mine['char_name']}** (solo se permite un personaje por cuenta). "
                               "Desvinculalo primero con `/admin_desvincular`.")
            now = utcnow_iso()
            try:
                cur = conn.execute(
                    "INSERT INTO albion_links(discord_id, char_id, char_name, region, guild_name, method, linked_by, registered_at, updated_at) "
                    "VALUES (?,?,?,?,?, ?, ?, ?, ?)", (discord_id, char_id, char_name, region, guild_name, method, actor_id, now, now))
            except sqlite3.IntegrityError:
                raise RegError("Ese personaje ya está vinculado a otra cuenta.")
            conn.execute("UPDATE albion_link_requests SET status='cancelled', updated_at=? WHERE lower(char_name)=lower(?) AND status='pending_target'",
                         (_iso(_now()), char_name))
            _audit(conn, actor_id, "recruit_linked" if method == "recruitment" else "admin_linked", discord_id, None,
                   f"personaje={char_name} " + ("(aceptado en reclutamiento)" if method == "recruitment" else "(sin que la persona lo aceptara)"))
            return _reg(conn.execute("SELECT * FROM albion_links WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


# ---------- reclutas aceptados que todavía no entraron a la guild ----------
@dataclass
class PendingRecruit:
    discord_id: int
    char_name: str
    char_id: Optional[str]
    role_id: int
    role_name: str
    recruiter_id: Optional[int]
    created_at: str


def _pend(r) -> PendingRecruit:
    return PendingRecruit(int(r["discord_id"]), r["char_name"], r["char_id"], int(r["role_id"]), r["role_name"], r["recruiter_id"], r["created_at"])


def add_pending_recruit(db_path: str, discord_id: int, char_name: str, char_id: Optional[str], role_id: int, role_name: str,
                        recruiter_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("INSERT OR REPLACE INTO recruit_pending(discord_id, char_name, char_id, role_id, role_name, recruiter_id, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (discord_id, char_name, char_id, role_id, role_name, recruiter_id, utcnow_iso()))
            _audit(conn, recruiter_id, "recruit_pending", discord_id, None, f"personaje={char_name} rol={role_name}")
    finally:
        conn.close()


def list_pending_recruits(db_path: str) -> List[PendingRecruit]:
    conn = connect(db_path)
    try:
        return [_pend(r) for r in conn.execute("SELECT * FROM recruit_pending ORDER BY created_at")]
    finally:
        conn.close()


def remove_pending_recruit(db_path: str, discord_id: int, actor_id: Optional[int] = None, reason: str = "") -> bool:
    conn = connect(db_path)
    try:
        with _tx(conn):
            n = conn.execute("DELETE FROM recruit_pending WHERE discord_id=?", (discord_id,)).rowcount
            if n:
                _audit(conn, actor_id, "recruit_pending_removed", discord_id, None, reason)
            return bool(n)
    finally:
        conn.close()


# ---------- ajustes (obligatoriedad y canal de avisos) ----------
def get_flag(db_path: str, key: str, default: str = "") -> str:
    import economy_db as eco
    return eco.get_setting(db_path, key) or default


def set_flag(db_path: str, key: str, value: Optional[str]) -> None:
    import economy_db as eco
    eco.set_setting(db_path, key, value)


REQUIRED_KEY = "registro:obligatorio"
REVIEW_CHANNEL_KEY = "registro:canal"
ROSTER_KEY = "registro:roster"
