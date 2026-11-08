"""/tarea — tareas para el staff (datos y transiciones, sin Discord).

Estados: scheduled (esperando su hora) → open (publicada, sin dueño) ⇄ taken (alguien la tiene) → done.
Cada transición es una sola transacción y condicionada al estado actual, así dos toques simultáneos no pueden pisarse.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

MAX_TEXT = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    text        TEXT NOT NULL,
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    publish_at  REAL,
    status      TEXT NOT NULL,                 -- scheduled | publishing | open | taken | done | cancelled
    taker_id    INTEGER,
    done_by     INTEGER,
    done_at     TEXT,
    source      TEXT,
    link        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_message ON tasks(message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source);
CREATE TABLE IF NOT EXISTS task_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,                    -- created | taken | released | done | cancelled
    actor_id INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""


class TaskError(ValueError):
    """Dato inválido que se le puede mostrar tal cual a quien lo ingresó."""


@dataclass
class Task:
    id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    text: str
    created_by: int
    created_at: str
    publish_at: Optional[float]
    status: str
    taker_id: Optional[int]
    done_by: Optional[int]
    done_at: Optional[str]
    source: Optional[str]
    link: Optional[str]
    events: List[Tuple[str, str, Optional[int]]] = field(default_factory=list)      # (cuándo, tipo, quién)
    ping_roles: List[int] = field(default_factory=list)       # roles a etiquetar además (o en vez) del Staff
    ping_users: List[int] = field(default_factory=list)       # personas a etiquetar
    ping_staff: bool = True                                    # False: no se etiqueta al Staff (p. ej. la tarea del split va al Council)
    thread_id: Optional[int] = None                            # hilo para que quien tomó la tarea se coordine
    taken_at: Optional[float] = None
    last_reminder: Optional[float] = None
    for_role: Optional[int] = None                             # tarea «para» un rol: solo ese rol (y el Council) la puede tomar y terminar
    require_photo: bool = False                                # para terminarla hay que subir una foto (sale en el log)


def _ids(text) -> List[int]:
    return [int(x) for x in str(text or "").split(",") if x.strip().isdigit()]


def _csv(ids) -> str:
    return ",".join(str(int(i)) for i in dict.fromkeys(ids or []))


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("ping_roles", "TEXT"), ("ping_users", "TEXT"), ("ping_staff", "INTEGER"),
                         ("thread_id", "INTEGER"), ("taken_at", "REAL"), ("last_reminder", "REAL"), ("for_role", "INTEGER"),
                         ("require_photo", "INTEGER")):
            if col not in have:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        with write_tx(conn):
            conn.execute("UPDATE tasks SET taken_at = ? WHERE status = 'taken' AND taken_at IS NULL", (time.time(),))    # tareas tomadas antes del recordatorio
        with write_tx(conn):                    # si el bot se cayó justo mientras publicaba, se reintenta
            conn.execute("UPDATE tasks SET status = 'scheduled' WHERE status = 'publishing' AND message_id IS NULL")
    finally:
        conn.close()


def _load(conn, task_id: int) -> Optional[Task]:
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if r is None:
        return None
    ev = [(e["at"], e["kind"], e["actor_id"]) for e in conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]
    return Task(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]), r["message_id"], r["text"], int(r["created_by"]), r["created_at"],
                r["publish_at"], r["status"], r["taker_id"], r["done_by"], r["done_at"], r["source"], r["link"], ev,
                _ids(r["ping_roles"]), _ids(r["ping_users"]), r["ping_staff"] is None or bool(r["ping_staff"]),
                r["thread_id"], r["taken_at"], r["last_reminder"], int(r["for_role"]) if r["for_role"] else None, bool(r["require_photo"]))


def get_task(db_path: str, task_id: int) -> Optional[Task]:
    conn = connect(db_path)
    try:
        return _load(conn, task_id)
    finally:
        conn.close()


def find_by_message(db_path: str, message_id: int) -> Optional[Task]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT id FROM tasks WHERE message_id = ?", (int(message_id),)).fetchone()
        return _load(conn, int(r["id"])) if r else None
    finally:
        conn.close()


def find_by_source(db_path: str, source: str) -> Optional[Task]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT id FROM tasks WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return _load(conn, int(r["id"])) if r else None
    finally:
        conn.close()


_REL_RE = re.compile(r"^\+?\s*(?:(\d{1,3})\s*h)?\s*(?:(\d{1,4})\s*m?)?$", re.I)


def parse_when(text: str, now: Optional[datetime] = None) -> float:
    """'HH:MM' (hora UTC, la próxima que llegue) o '+90m' / '+2h' / '+1h30' (desde ahora). Devuelve un timestamp."""
    now = now or datetime.now(timezone.utc)
    t = (text or "").strip().lower().replace("utc", "").strip()
    if not t:
        raise TaskError("Indicá cuándo: `HH:MM` en UTC (ej: `22:30`) o `+90m` / `+2h`.")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise TaskError("Hora inválida. Usá `HH:MM` en UTC, por ejemplo `22:30`.")
        at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return at.timestamp()
    if t.startswith("+"):
        rel = _REL_RE.match(t)
        if rel and (rel.group(1) or rel.group(2)):
            minutes = int(rel.group(1) or 0) * 60 + int(rel.group(2) or 0)
            if minutes < 1 or minutes > 7 * 24 * 60:
                raise TaskError("El tiempo relativo va de 1 minuto a 7 días.")
            return (now + timedelta(minutes=minutes)).timestamp()
    raise TaskError("No entendí el horario. Usá `HH:MM` en UTC (ej: `22:30`) o `+90m` / `+2h`.")


def create_task(db_path: str, *, guild_id: int, channel_id: int, text: str, created_by: int, publish_at: Optional[float] = None,
                source: Optional[str] = None, link: Optional[str] = None, now: Optional[float] = None,
                ping_roles=(), ping_users=(), ping_staff: bool = True, for_role: Optional[int] = None, require_photo: bool = False) -> Task:
    text = " ".join((text or "").split())
    if not text:
        raise TaskError("La tarea no puede estar vacía.")
    if len(text) > MAX_TEXT:
        raise TaskError(f"La tarea es demasiado larga (máximo {MAX_TEXT} caracteres).")
    now = time.time() if now is None else now
    status = "scheduled"                                    # siempre pasa por el publicador (inmediata o en su hora)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if source:
                dup = conn.execute("SELECT id FROM tasks WHERE source = ?", (source,)).fetchone()
                if dup:                                                    # tareas automáticas: una sola por origen
                    return _load(conn, int(dup["id"]))
            cur = conn.execute("INSERT INTO tasks(guild_id, channel_id, text, created_by, created_at, publish_at, status, source, link, "
                               "ping_roles, ping_users, ping_staff, for_role, require_photo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (guild_id, channel_id, text, created_by, utcnow_iso(), publish_at if publish_at else now, status, source, link,
                                _csv(ping_roles), _csv(ping_users), 1 if ping_staff else 0, int(for_role) if for_role else None,
                                1 if require_photo else 0))
            tid = int(cur.lastrowid)
            conn.execute("INSERT INTO task_events(task_id, at, kind, actor_id) VALUES (?,?,?,?)", (tid, utcnow_iso(), "created", created_by))
            return _load(conn, tid)
    finally:
        conn.close()


def due_for_publish(db_path: str, now: Optional[float] = None) -> List[int]:
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute("SELECT id FROM tasks WHERE status = 'scheduled' AND publish_at <= ? ORDER BY publish_at, id", (now,))]
    finally:
        conn.close()


def claim_publish(db_path: str, task_id: int) -> bool:
    """Reclama la publicación (una sola vez). False si otro ya la reclamó."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE tasks SET status = 'publishing' WHERE id = ? AND status = 'scheduled'", (task_id,)).rowcount == 1
    finally:
        conn.close()


def mark_published(db_path: str, task_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE tasks SET status = 'open', message_id = ? WHERE id = ? AND status = 'publishing'", (message_id, task_id))
    finally:
        conn.close()


def unclaim_publish(db_path: str, task_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ? AND status = 'publishing' AND message_id IS NULL", (task_id,))
    finally:
        conn.close()


@dataclass
class Change:
    status: str            # ok | notyou | closed | taken | notopen | notfound
    task: Optional[Task] = None


def _event(conn, task_id: int, kind: str, actor: int) -> None:
    conn.execute("INSERT INTO task_events(task_id, at, kind, actor_id) VALUES (?,?,?,?)", (task_id, utcnow_iso(), kind, actor))


def take(db_path: str, task_id: int, user_id: int) -> Change:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = _load(conn, task_id)
            if t is None:
                return Change("notfound")
            if t.status == "done":
                return Change("closed", t)
            if t.status == "taken":
                return Change("taken", t)
            if t.status != "open":
                return Change("notopen", t)
            conn.execute("UPDATE tasks SET status = 'taken', taker_id = ?, taken_at = ?, last_reminder = NULL WHERE id = ? AND status = 'open'",
                         (user_id, time.time(), task_id))
            _event(conn, task_id, "taken", user_id)
            return Change("ok", _load(conn, task_id))
    finally:
        conn.close()


def release(db_path: str, task_id: int, user_id: int) -> Change:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = _load(conn, task_id)
            if t is None:
                return Change("notfound")
            if t.status == "done":
                return Change("closed", t)
            if t.status != "taken":
                return Change("notopen", t)
            if t.taker_id != user_id:
                return Change("notyou", t)
            conn.execute("UPDATE tasks SET status = 'open', taker_id = NULL WHERE id = ? AND status = 'taken'", (task_id,))
            _event(conn, task_id, "released", user_id)
            return Change("ok", _load(conn, task_id))
    finally:
        conn.close()


def finish(db_path: str, task_id: int, user_id: int, override: bool) -> Change:
    """La termina quien la tomó, o alguien con `override` (staff/council) aunque nadie la haya tomado."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = _load(conn, task_id)
            if t is None:
                return Change("notfound")
            if t.status == "done":
                return Change("closed", t)
            if t.status not in ("open", "taken"):
                return Change("notopen", t)
            if not (override or (t.status == "taken" and t.taker_id == user_id)):
                return Change("notyou", t)
            conn.execute("UPDATE tasks SET status = 'done', done_by = ?, done_at = ? WHERE id = ? AND status IN ('open','taken')",
                         (user_id, utcnow_iso(), task_id))
            _event(conn, task_id, "done", user_id)
            return Change("ok", _load(conn, task_id))
    finally:
        conn.close()


def record_done(db_path: str, *, guild_id: int, channel_id: int, text: str, actor_id: int, source: str) -> bool:
    """Anota como tarea hecha algo que el Staff resolvió fuera del panel de tareas (cerrar un ticket, pagar un split…),
    para que cuente en /top tareas. Una sola vez por `source`. True si se anotó ahora; False si ya estaba."""
    text = " ".join((text or "").split())[:MAX_TEXT] or "Tarea"
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM tasks WHERE source = ?", (source,)).fetchone():
                return False
            now = utcnow_iso()
            cur = conn.execute("INSERT INTO tasks(guild_id, channel_id, text, created_by, created_at, status, taker_id, done_by, done_at, source) "
                               "VALUES (?,?,?,?,?, 'done', ?, ?, ?, ?)",
                               (guild_id, channel_id, text, actor_id, now, actor_id, actor_id, now, source))
            conn.execute("INSERT INTO task_events(task_id, at, kind, actor_id) VALUES (?,?,?,?)", (int(cur.lastrowid), now, "done", actor_id))
            return True
    finally:
        conn.close()


def cancel(db_path: str, task_id: int, actor_id: int) -> Change:
    """Cancela una tarea que no se hizo (botón «Cancelar tarea», o se canceló la CTA que la generó). No cuenta como terminada.
    'ok' · 'closed' (ya estaba terminada o cancelada) · 'notfound'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = _load(conn, task_id)
            if t is None:
                return Change("notfound")
            if t.status in ("done", "cancelled"):
                return Change("closed", t)
            conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ? AND status NOT IN ('done','cancelled')", (task_id,))
            _event(conn, task_id, "cancelled", actor_id)
            return Change("ok", _load(conn, task_id))
    finally:
        conn.close()


def open_by_source_prefix(db_path: str, prefix: str) -> List[int]:
    """Ids de las tareas sin terminar ni cancelar cuyo `source` empieza con `prefix` (p. ej. «cta:12:»)."""
    conn = connect(db_path)
    try:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM tasks WHERE source LIKE ? ESCAPE '\\' AND status NOT IN ('done','cancelled') ORDER BY id", (like,))]
    finally:
        conn.close()


def top_coaches(db_path: str, size: int = 10, since: Optional[str] = None):
    """[(user_id, devoluciones de coaching cerradas)] de más a menos. `since` (ISO UTC) limita a lo cerrado desde esa fecha."""
    sql = "SELECT done_by, COUNT(*) AS n FROM tasks WHERE status='done' AND done_by IS NOT NULL AND source LIKE 'coach:%'"
    args: list = []
    if since:
        sql += " AND done_at >= ?"
        args.append(since)
    conn = connect(db_path)
    try:
        return [(int(r[0]), int(r[1])) for r in conn.execute(sql + " GROUP BY done_by ORDER BY n DESC, done_by LIMIT ?", (*args, size))]
    finally:
        conn.close()


def top_users(db_path: str, size: int = 10):
    """(más tareas aceptadas, más tareas cerradas): listas de (user_id, cantidad). «Aceptadas» = veces que alguien tomó una tarea."""
    conn = connect(db_path)
    try:
        taken = [(int(r[0]), int(r[1])) for r in conn.execute(
            "SELECT actor_id, COUNT(*) AS n FROM task_events WHERE kind='taken' GROUP BY actor_id ORDER BY n DESC, actor_id LIMIT ?", (size,))]
        done = [(int(r[0]), int(r[1])) for r in conn.execute(
            "SELECT done_by, COUNT(*) AS n FROM tasks WHERE status='done' AND done_by IS NOT NULL GROUP BY done_by ORDER BY n DESC, done_by LIMIT ?", (size,))]
        return taken, done
    finally:
        conn.close()


# ---------- hilo de coordinación y recordatorio horario ----------
REMIND_EVERY = 3600          # segundos entre avisos mientras la tarea siga tomada y sin cerrar
REMIND_EVERY_COACH = 12 * 3600       # las devoluciones de coaching (source «coach:…») llevan más tiempo: aviso cada 12 h


def set_text(db_path: str, task_id: int, text: str) -> None:
    text = " ".join((text or "").split())[:MAX_TEXT]
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE tasks SET text = ? WHERE id = ? AND status <> 'done'", (text, task_id))
    finally:
        conn.close()


def set_thread(db_path: str, task_id: int, thread_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE tasks SET thread_id = ? WHERE id = ?", (thread_id, task_id))
    finally:
        conn.close()


def due_reminders(db_path: str, now: Optional[float] = None) -> List[int]:
    """Tareas tomadas y sin cerrar a las que ya les toca el aviso (pasó 1 h desde que se tomó o desde el último aviso)."""
    now = time.time() if now is None else now
    conn = connect(db_path)
    try:
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM tasks WHERE status = 'taken' AND taker_id IS NOT NULL AND COALESCE(last_reminder, taken_at, 0) <= "
            "? - CASE WHEN source LIKE 'coach:%' THEN ? ELSE ? END ORDER BY id",
            (now, REMIND_EVERY_COACH, REMIND_EVERY))]
    finally:
        conn.close()


def mark_reminded(db_path: str, task_id: int, now: Optional[float] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE tasks SET last_reminder = ? WHERE id = ?", (time.time() if now is None else now, task_id))
    finally:
        conn.close()
