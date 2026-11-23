"""Registro y vinculación Discord ↔ personajes de Albion (lógica y persistencia, sin Discord).

Reglas:
  * Un personaje de Albion solo puede estar vinculado a UNA cuenta de Discord, y una cuenta de Discord solo puede tener UN personaje
    (índices únicos por cuenta, por ID y por nombre).
  * Registrar a otra persona exige que ella misma acepte la solicitud desde su cuenta; hasta entonces no se guarda nada.
  * No hay verificación de propiedad del personaje (la API pública de Albion no la permite): ver registration.py.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

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
CREATE TABLE IF NOT EXISTS left_members (               -- últimos miembros registrados que se fueron del servidor (se guardan 5)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL UNIQUE,
    name       TEXT,
    nick       TEXT,
    left_at    TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '[]',                 -- [[id, nombre], ...]
    regs_json  TEXT NOT NULL DEFAULT '[]'                  -- registros de Albion que tenía
);
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
CREATE TABLE IF NOT EXISTS guild_transfers (              -- /miembros cambiar-guild: pase entre la guild principal y una hermana
    discord_id INTEGER PRIMARY KEY,
    target     TEXT NOT NULL,                              -- guild de destino (ej. Smogged)
    actor_id   INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_periods (               -- tiempo en cada guild (Smogg / Smogged) POR PERSONAJE DE ALBION: no se borra
    id         INTEGER PRIMARY KEY AUTOINCREMENT,         -- aunque la persona se desvincule o se vaya del Discord
    char_key   TEXT NOT NULL,                             -- char_id de Albion, o 'name:<nombre en minúsculas>' si no hay ID
    char_name  TEXT NOT NULL,
    discord_id INTEGER,                                   -- cuenta de Discord que tenía el personaje en ese tramo (referencia)
    guild_name TEXT NOT NULL,
    start_at   TEXT NOT NULL,
    end_at     TEXT                                       -- NULL = tramo actual
);
CREATE INDEX IF NOT EXISTS ix_guild_periods_char ON guild_periods(char_key);
CREATE TABLE IF NOT EXISTS albion_name_history (         -- nombres anteriores de un personaje renombrado (killboards viejos, asistencia)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    char_key   TEXT NOT NULL,                             -- misma clave que guild_periods (ID de Albion, o 'name:<nombre>')
    old_name   TEXT NOT NULL,
    new_name   TEXT,
    actor_id   INTEGER,
    changed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_albion_name_history ON albion_name_history(char_key, lower(old_name));
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


MAIN_GUILD = os.getenv("ALBION_GUILD_NAME", "Smogg")
SEASON_NUMBER = int(os.getenv("ALBION_SEASON", "35"))
SEASON_START = os.getenv("ALBION_SEASON_START", "2026-11-21T11:35:00Z")   # inicio de la season actual (UTC)


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if "extra_role_ids" not in {r["name"] for r in conn.execute("PRAGMA table_info(recruit_pending)")}:
            conn.execute("ALTER TABLE recruit_pending ADD COLUMN extra_role_ids TEXT")
        # registros previos a guild_periods: un tramo desde que se registraron, en la guild que tienen hoy
        for r in conn.execute("SELECT * FROM albion_links").fetchall():
            key = char_key(r["char_id"], r["char_name"])
            if not conn.execute("SELECT 1 FROM guild_periods WHERE char_key=?", (key,)).fetchone():
                conn.execute("INSERT INTO guild_periods(char_key, char_name, discord_id, guild_name, start_at) VALUES (?,?,?,?,?)",
                             (key, r["char_name"], r["discord_id"], r["guild_name"] or MAIN_GUILD, r["registered_at"]))
        conn.commit()
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
            _period_switch(conn, r.char_id, r.char_name, user_id, r.guild_name, now)
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
                _period_close(conn, char_key(r["char_id"], r["char_name"]), utcnow_iso())   # el tramo queda guardado
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
            _period_switch(conn, char_id, char_name, discord_id, guild_name, now)
            conn.execute("UPDATE albion_link_requests SET status='cancelled', updated_at=? WHERE lower(char_name)=lower(?) AND status='pending_target'",
                         (_iso(_now()), char_name))
            _audit(conn, actor_id, "recruit_linked" if method == "recruitment" else "admin_linked", discord_id, None,
                   f"personaje={char_name} " + ("(aceptado en reclutamiento)" if method == "recruitment" else "(sin que la persona lo aceptara)"))
            return _reg(conn.execute("SELECT * FROM albion_links WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def admin_relink(db_path: str, discord_id: int, char_name: str, actor_id: int, *, char_id: Optional[str] = None,
                 region: Optional[str] = None, guild_name: Optional[str] = None) -> tuple:
    """/admin_vincular: vincula pisando el registro que ya tuviera la persona. Devuelve (registro, modo, nombre anterior):
    'new' = no tenía registro; 'updated' = mismo personaje (mismo ID o nombre): se actualizan nombre, ID, servidor y guild
    sin perder la fecha de registro; 'replaced' = tenía otro personaje: se desvincula y se vincula el nuevo.
    Si el personaje es de OTRA cuenta de Discord, no se toca nada (RegError)."""
    char_name = (char_name or "").strip()
    if not char_name or len(char_name) > 40:
        raise RegError("Nombre de personaje inválido.")
    conn = connect(db_path)
    try:
        with _tx(conn):
            other = conn.execute("SELECT char_name FROM albion_links WHERE discord_id<>? AND (lower(char_name)=lower(?) OR (? IS NOT NULL AND char_id=?))",
                                 (discord_id, char_name, char_id, char_id)).fetchone()
            if other is not None:
                raise RegError(f"Ese personaje ya está vinculado a otra cuenta (**{other['char_name']}**). Desvinculalo primero con `/admin_desvincular`.")
            mine = conn.execute("SELECT * FROM albion_links WHERE discord_id=?", (discord_id,)).fetchone()
            now = utcnow_iso()
            same = mine is not None and ((char_id and mine["char_id"] == char_id) or                       # mismo ID (aunque se haya renombrado)
                                         (mine["char_name"].casefold() == char_name.casefold() and          # mismo nombre, salvo que los IDs digan
                                          not (char_id and mine["char_id"] and mine["char_id"] != char_id)))  # que son personajes distintos
            if same:
                old_key, new_key = char_key(mine["char_id"], mine["char_name"]), char_key(char_id or mine["char_id"], char_name)
                conn.execute("UPDATE albion_links SET char_name=?, char_id=COALESCE(?, char_id), region=COALESCE(?, region), "
                             "guild_name=COALESCE(?, guild_name), updated_at=? WHERE id=?", (char_name, char_id, region, guild_name, now, mine["id"]))
                if old_key != new_key:                    # tramos guardados por nombre (o con el nombre viejo) pasan a la clave nueva
                    conn.execute("UPDATE guild_periods SET char_key=? WHERE char_key=?", (new_key, old_key))
                conn.execute("UPDATE guild_periods SET char_name=? WHERE char_key=?", (char_name, new_key))
                if guild_name:
                    _period_switch(conn, char_id or mine["char_id"], char_name, discord_id, guild_name, now)
                if mine["char_name"].casefold() != char_name.casefold():          # se renombró: el nombre viejo sigue contando
                    _add_old_name(conn, new_key, mine["char_name"], char_name, actor_id, now)
                _audit(conn, actor_id, "admin_updated", discord_id, None, f"personaje={mine['char_name']} -> {char_name} id={char_id or mine['char_id']}")
                return _reg(conn.execute("SELECT * FROM albion_links WHERE id=?", (mine["id"],)).fetchone()), "updated", mine["char_name"]
            previous = None
            if mine is not None:                          # tenía otro personaje: se lo saca (su tiempo en la guild queda guardado)
                previous = mine["char_name"]
                conn.execute("DELETE FROM albion_links WHERE id=?", (mine["id"],))
                _period_close(conn, char_key(mine["char_id"], mine["char_name"]), now)
                _audit(conn, actor_id, "unlinked", discord_id, None, f"personaje={previous} (reemplazado con /admin_vincular por {char_name})")
            try:
                cur = conn.execute(
                    "INSERT INTO albion_links(discord_id, char_id, char_name, region, guild_name, method, linked_by, registered_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'admin_override', ?, ?, ?)", (discord_id, char_id, char_name, region, guild_name, actor_id, now, now))
            except sqlite3.IntegrityError:
                raise RegError("Ese personaje ya está vinculado a otra cuenta.")
            _period_switch(conn, char_id, char_name, discord_id, guild_name, now)
            conn.execute("UPDATE albion_link_requests SET status='cancelled', updated_at=? WHERE lower(char_name)=lower(?) AND status='pending_target'",
                         (_iso(_now()), char_name))
            _audit(conn, actor_id, "admin_linked", discord_id, None, f"personaje={char_name} (sin que la persona lo aceptara)")
            return _reg(conn.execute("SELECT * FROM albion_links WHERE id=?", (cur.lastrowid,)).fetchone()), ("replaced" if previous else "new"), previous
    finally:
        conn.close()


def incomplete_registrations(db_path: str) -> List[Registration]:
    """Registros guardados sin ID de personaje (p. ej. vínculos hechos mientras la API de Albion no respondía)."""
    conn = connect(db_path)
    try:
        return [_reg(r) for r in conn.execute("SELECT * FROM albion_links WHERE char_id IS NULL OR char_id='' ORDER BY id")]
    finally:
        conn.close()


def fill_registration(db_path: str, reg_id: int, char_id: str, region: Optional[str], guild_name: Optional[str], actor_id: int) -> bool:
    """Completa ID, servidor y guild de un registro que solo tenía el nombre. No cambia nombre ni dueño.
    False si ya no hacía falta; RegError si ese ID ya pertenece a otra cuenta."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            row = conn.execute("SELECT * FROM albion_links WHERE id=? AND (char_id IS NULL OR char_id='')", (reg_id,)).fetchone()
            if row is None:
                return False
            if conn.execute("SELECT 1 FROM albion_links WHERE char_id=? AND id<>?", (char_id, reg_id)).fetchone():
                raise RegError("Ese personaje ya está vinculado a otra cuenta.")
            try:
                conn.execute("UPDATE albion_links SET char_id=?, region=?, guild_name=?, updated_at=? WHERE id=?",
                             (char_id, region, guild_name, utcnow_iso(), reg_id))
            except sqlite3.IntegrityError:
                raise RegError("Ese personaje ya está vinculado a otra cuenta.")
            old_key = char_key(None, row["char_name"])   # los tramos guardados por nombre pasan al ID de Albion
            conn.execute("UPDATE guild_periods SET char_key=? WHERE char_key=?", (char_key(char_id, row["char_name"]), old_key))
            if guild_name:                               # se completó un dato que faltaba: corrige el tramo actual, no es un pase
                conn.execute("UPDATE guild_periods SET guild_name=? WHERE char_key=? AND end_at IS NULL",
                             (guild_name, char_key(char_id, row["char_name"])))
            _audit(conn, actor_id, "admin_completed", int(row["discord_id"]), None, f"personaje={row['char_name']} id={char_id}")
            return True
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
    extra_role_ids: tuple = ()


def _pend(r) -> PendingRecruit:
    try:
        extra = tuple(int(x) for x in json.loads(r["extra_role_ids"] or "[]"))
    except Exception:
        extra = ()
    return PendingRecruit(int(r["discord_id"]), r["char_name"], r["char_id"], int(r["role_id"]), r["role_name"], r["recruiter_id"], r["created_at"], extra)


def add_pending_recruit(db_path: str, discord_id: int, char_name: str, char_id: Optional[str], role_id: int, role_name: str,
                        recruiter_id: Optional[int], extra_role_ids=()) -> None:
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("INSERT OR REPLACE INTO recruit_pending(discord_id, char_name, char_id, role_id, role_name, recruiter_id, created_at, extra_role_ids) "
                         "VALUES (?,?,?,?,?,?,?,?)", (discord_id, char_name, char_id, role_id, role_name, recruiter_id, utcnow_iso(),
                                                      json.dumps([int(x) for x in extra_role_ids])))
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
MEMBER_COUNT_CHANNEL_KEY = "registro:canal_miembros"


# ---------- últimos miembros registrados que se fueron del servidor ----------
LEFT_KEEP = 5


def save_left_member(db_path: str, discord_id: int, *, name: str, nick: Optional[str], roles: list, regs: List[Registration]) -> None:
    """Guarda los datos de quien se fue (solo si estaba registrado). Se conservan los últimos LEFT_KEEP: el siguiente pisa al más viejo."""
    if not regs:
        return
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("DELETE FROM left_members WHERE discord_id=?", (discord_id,))
            conn.execute("INSERT INTO left_members(discord_id, name, nick, left_at, roles_json, regs_json) VALUES (?,?,?,?,?,?)",
                         (discord_id, name, nick, utcnow_iso(), json.dumps([[int(i), n] for i, n in roles]),
                          json.dumps([dict(char_id=r.char_id, char_name=r.char_name, region=r.region, guild_name=r.guild_name,
                                           method=r.method, registered_at=r.registered_at) for r in regs])))
            conn.execute("DELETE FROM left_members WHERE id NOT IN (SELECT id FROM left_members ORDER BY id DESC LIMIT ?)", (LEFT_KEEP,))
    finally:
        conn.close()


def _left(r) -> dict:
    return dict(discord_id=int(r["discord_id"]), name=r["name"], nick=r["nick"], left_at=r["left_at"],
                roles=[(int(i), n) for i, n in json.loads(r["roles_json"] or "[]")], regs=json.loads(r["regs_json"] or "[]"))


def list_left_members(db_path: str) -> List[dict]:
    conn = connect(db_path)
    try:
        return [_left(r) for r in conn.execute("SELECT * FROM left_members ORDER BY id DESC")]
    finally:
        conn.close()


def delete_left_member(db_path: str, discord_id: int) -> None:
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("DELETE FROM left_members WHERE discord_id=?", (discord_id,))
    finally:
        conn.close()


# ---------- pases entre guilds (Smogg ↔ Smogged) ----------
TRANSFER_HOURS = 72                                        # cuánto se espera a que el personaje aparezca en la guild nueva


def set_guild(db_path: str, discord_id: int, guild_name: str, actor_id: int) -> List[Registration]:
    """Pasa los personajes registrados de la persona a `guild_name` y anota el pase (pisa uno anterior). Devuelve los registros."""
    conn = connect(db_path)
    try:
        with _tx(conn):
            rows = conn.execute("SELECT * FROM albion_links WHERE discord_id=?", (discord_id,)).fetchall()
            if not rows:
                raise RegError("Esa persona no tiene personaje registrado.")
            now = _iso(_now())
            conn.execute("UPDATE albion_links SET guild_name=?, updated_at=? WHERE discord_id=?", (guild_name, now, discord_id))
            for r in rows:
                _period_switch(conn, r["char_id"], r["char_name"], discord_id, guild_name, now)
            conn.execute("INSERT INTO guild_transfers(discord_id, target, actor_id, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(discord_id) DO UPDATE SET target=excluded.target, actor_id=excluded.actor_id, created_at=excluded.created_at",
                         (discord_id, guild_name, actor_id, now))
            _audit(conn, actor_id, "guild_transfer", discord_id, None, f"a {guild_name}")
            return [_reg(r) for r in conn.execute("SELECT * FROM albion_links WHERE discord_id=? ORDER BY id", (discord_id,))]
    finally:
        conn.close()


def active_transfers(db_path: str) -> Dict[int, str]:
    """{discord_id: guild de destino} de los pases de las últimas TRANSFER_HOURS (los vencidos se borran)."""
    cutoff = _iso(_now() - timedelta(hours=TRANSFER_HOURS))
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("DELETE FROM guild_transfers WHERE created_at < ?", (cutoff,))
            return {int(r["discord_id"]): r["target"] for r in conn.execute("SELECT discord_id, target FROM guild_transfers")}
    finally:
        conn.close()


def end_transfer(db_path: str, discord_id: int) -> None:
    conn = connect(db_path)
    try:
        with _tx(conn):
            conn.execute("DELETE FROM guild_transfers WHERE discord_id=?", (discord_id,))
    finally:
        conn.close()


# ---------- tiempo en cada guild (antigüedad y season), por personaje de Albion ----------
def char_key(char_id: Optional[str], char_name: str) -> str:
    return char_id or f"name:{(char_name or '').strip().lower()}"


def _period_close(conn, key: str, at: str) -> None:
    conn.execute("UPDATE guild_periods SET end_at=? WHERE char_key=? AND end_at IS NULL", (at, key))


def _period_switch(conn, char_id: Optional[str], char_name: str, discord_id: Optional[int], guild_name: Optional[str], at: str) -> None:
    """Cierra el tramo abierto del personaje y abre uno en `guild_name` (si ya está en esa guild, no hace nada)."""
    key, guild_name = char_key(char_id, char_name), guild_name or MAIN_GUILD
    cur = conn.execute("SELECT guild_name FROM guild_periods WHERE char_key=? AND end_at IS NULL", (key,)).fetchone()
    if cur is not None and cur["guild_name"].casefold() == guild_name.casefold():
        return
    _period_close(conn, key, at)
    conn.execute("INSERT INTO guild_periods(char_key, char_name, discord_id, guild_name, start_at) VALUES (?,?,?,?,?)",
                 (key, char_name, discord_id, guild_name, at))


def _parse(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def season_start() -> datetime:
    return _parse(SEASON_START)


def guild_seconds(db_path: str, discord_id: int, since: Optional[datetime] = None, now: Optional[datetime] = None) -> Dict[str, int]:
    """{guild: segundos} de los personajes que hoy tiene registrados la persona, sumando todos sus tramos
    (también los de antes de desvincularse o irse del Discord). Con `since`, solo lo que cae desde esa fecha."""
    now = now or _now()
    out: Dict[str, int] = {}
    conn = connect(db_path)
    try:
        keys = [char_key(r["char_id"], r["char_name"]) for r in conn.execute("SELECT char_id, char_name FROM albion_links WHERE discord_id=?", (discord_id,))]
        for key in keys:
            for r in conn.execute("SELECT guild_name, start_at, end_at FROM guild_periods WHERE char_key=?", (key,)):
                a, b = _parse(r["start_at"]), (_parse(r["end_at"]) if r["end_at"] else now)
                if since is not None:
                    a = max(a, since)
                secs = int((min(b, now) - a).total_seconds())
                if secs > 0:
                    out[r["guild_name"]] = out.get(r["guild_name"], 0) + secs
    finally:
        conn.close()
    return out


def fmt_span(seconds: int) -> str:
    """'1d 8h', '5h', '0h'."""
    h = max(0, int(seconds)) // 3600
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if d else f"{h}h"


# ---------- nombres anteriores (personajes renombrados) ----------
def _add_old_name(conn, key: str, old_name: str, new_name: Optional[str], actor_id: Optional[int], at: str) -> None:
    old_name = (old_name or "").strip()
    if old_name and (not new_name or old_name.casefold() != new_name.casefold()):
        conn.execute("INSERT OR IGNORE INTO albion_name_history(char_key, old_name, new_name, actor_id, changed_at) VALUES (?,?,?,?,?)",
                     (key, old_name, new_name, actor_id, at))


def add_old_name(db_path: str, discord_id: int, old_name: str, actor_id: int) -> str:
    """Anota a mano un nombre anterior del personaje registrado de la persona. Devuelve el nombre actual."""
    old_name = (old_name or "").strip()
    if not old_name or len(old_name) > 40:
        raise RegError("Nombre anterior inválido.")
    conn = connect(db_path)
    try:
        with _tx(conn):
            mine = conn.execute("SELECT * FROM albion_links WHERE discord_id=?", (discord_id,)).fetchone()
            if mine is None:
                raise RegError("Esa persona no tiene personaje registrado.")
            other = conn.execute("SELECT 1 FROM albion_links WHERE lower(char_name)=lower(?) AND discord_id<>?", (old_name, discord_id)).fetchone()
            if other is not None:
                raise RegError(f"**{old_name}** es el personaje registrado de otra cuenta: no puede ser un nombre anterior.")
            if old_name.casefold() != mine["char_name"].casefold():
                _add_old_name(conn, char_key(mine["char_id"], mine["char_name"]), old_name, mine["char_name"], actor_id, utcnow_iso())
                _audit(conn, actor_id, "old_name_added", discord_id, None, f"personaje={mine['char_name']} antes={old_name}")
            return mine["char_name"]
    finally:
        conn.close()


def old_names(db_path: str, discord_id: int) -> List[str]:
    """Nombres anteriores de los personajes que hoy tiene registrados la persona."""
    conn = connect(db_path)
    try:
        keys = [char_key(r["char_id"], r["char_name"]) for r in conn.execute("SELECT char_id, char_name FROM albion_links WHERE discord_id=?", (discord_id,))]
        out: List[str] = []
        for key in keys:
            out += [r["old_name"] for r in conn.execute("SELECT old_name FROM albion_name_history WHERE char_key=? ORDER BY id", (key,))]
        return out
    finally:
        conn.close()


def rename_character(db_path: str, char_id: str, new_name: str) -> Optional[tuple]:
    """El personaje con ese ID se renombró en Albion: actualiza el registro y deja el nombre viejo como anterior.
    Devuelve (discord_id, nombre viejo) o None si no había nada que cambiar (o el nombre nuevo lo tiene otra cuenta)."""
    new_name = (new_name or "").strip()
    if not char_id or not new_name:
        return None
    conn = connect(db_path)
    try:
        with _tx(conn):
            r = conn.execute("SELECT * FROM albion_links WHERE char_id=?", (char_id,)).fetchone()
            if r is None or r["char_name"] == new_name:
                return None
            if conn.execute("SELECT 1 FROM albion_links WHERE lower(char_name)=lower(?) AND id<>?", (new_name, r["id"])).fetchone():
                return None
            now = utcnow_iso()
            conn.execute("UPDATE albion_links SET char_name=?, updated_at=? WHERE id=?", (new_name, now, r["id"]))
            conn.execute("UPDATE guild_periods SET char_name=? WHERE char_key=?", (new_name, char_id))
            if r["char_name"].casefold() != new_name.casefold():
                _add_old_name(conn, char_id, r["char_name"], new_name, None, now)
            _audit(conn, None, "renamed", int(r["discord_id"]), None, f"personaje={r['char_name']} -> {new_name} id={char_id}")
            return int(r["discord_id"]), r["char_name"]
    finally:
        conn.close()
