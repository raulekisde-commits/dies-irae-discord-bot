"""Control de miembros guild ↔ Discord (lógica y persistencia, sin Discord): lo usa guildcheck.py (/miembros ...).

• Última conexión: se toma del archivo que exporta Albion con la lista de la guild ("Character Name" / "Last Seen" / "Roles").
  Cada vez que se sube (en /miembros o como lista de conectados de una CTA) se reemplaza la foto completa.
  "Last Seen" viene en UTC con formato MM/DD/YYYY HH:MM:SS; "Online" = la hora en que se subió el archivo.
• Asistencia a CTA: cuenta si la persona aparece en el killboard de esa CTA (personaje registrado o alter)
  o si estaba ANOTADA en la composición y estuvo más de 20 minutos en el canal de voz.
• Avisos de salida de la guild: uno por persona; se borra cuando el personaje vuelve a figurar en la guild."""
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from dbutil import connect, utcnow_iso, write_tx

INACTIVE_DAYS = 7
CTA_WEEKLY_MIN = 5
CHANNEL_KEY = "leave_alert_channel"
SNAPSHOT_KEY = "last_seen_at"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_last_seen (
    name_norm TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    last_seen TEXT NOT NULL,             -- ISO UTC
    online    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS guildcheck_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_leave_alerts (
    discord_id INTEGER PRIMARY KEY,
    char_name  TEXT NOT NULL,
    status     TEXT NOT NULL,            -- pending | kicked | ignored
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL,
    handled_by INTEGER,
    handled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leave_alerts_msg ON guild_leave_alerts(message_id);
"""


def norm(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- ajustes ----------
def get_setting(db_path: str, key: str) -> Optional[str]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT value FROM guildcheck_settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_setting(db_path: str, key: str, value: Optional[str]) -> None:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if value is None:
                conn.execute("DELETE FROM guildcheck_settings WHERE key=?", (key,))
            else:
                conn.execute("INSERT INTO guildcheck_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
    finally:
        conn.close()


# ---------- última conexión (archivo exportado de la guild) ----------
def parse_last_seen(raw: bytes, now: Optional[datetime] = None) -> List[Tuple[str, datetime, bool]]:
    """[(personaje, última conexión UTC, online)] del export de la guild. Filas sin fecha legible se descartan."""
    now = now or datetime.now(timezone.utc)
    text = None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        for enc in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    delim = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else ";")
    out, seen = [], set()
    for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=delim, quotechar='"'):
        if len(row) < 2:
            continue
        name, when = row[0].strip().strip('"').strip(), row[1].strip().strip('"').strip()
        if not name or name.casefold() == "character name" or norm(name) in seen:
            continue
        if when.casefold() == "online":
            out.append((name, now, True))
        else:
            try:
                out.append((name, datetime.strptime(when, "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc), False))
            except ValueError:
                continue
        seen.add(norm(name))
    return out


def save_snapshot(db_path: str, rows: Iterable[Tuple[str, datetime, bool]], now: Optional[datetime] = None) -> int:
    """Reemplaza la foto de la guild (quien ya no figura en el archivo, ya no está en la guild)."""
    rows = list(rows)
    if not rows:
        return 0
    init_schema(db_path)
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM guild_last_seen")
            for name, when, online in rows:
                conn.execute("INSERT OR REPLACE INTO guild_last_seen(name_norm, name, last_seen, online) VALUES (?,?,?,?)",
                             (norm(name), name, when.astimezone(timezone.utc).isoformat(), 1 if online else 0))
            conn.execute("INSERT INTO guildcheck_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (SNAPSHOT_KEY, now.astimezone(timezone.utc).isoformat()))
        return len(rows)
    finally:
        conn.close()


def save_from_export(db_path: str, raw: bytes, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    return save_snapshot(db_path, parse_last_seen(raw, now), now)


def snapshot(db_path: str) -> Tuple[Dict[str, Tuple[str, datetime]], Optional[datetime]]:
    """({name_norm: (nombre, última conexión)}, cuándo se subió el archivo)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        data = {r["name_norm"]: (r["name"], _dt(r["last_seen"])) for r in conn.execute("SELECT * FROM guild_last_seen")}
        at = conn.execute("SELECT value FROM guildcheck_settings WHERE key=?", (SNAPSHOT_KEY,)).fetchone()
        return data, (_dt(at["value"]) if at else None)
    finally:
        conn.close()


def chars_by_user(db_path: str) -> Dict[int, Set[str]]:
    """{discord_id: {name_norm}} con los personajes registrados y los alters de /alter."""
    out: Dict[int, Set[str]] = {}
    conn = connect(db_path)
    try:
        if _has_table(conn, "albion_links"):
            for r in conn.execute("SELECT discord_id, char_name FROM albion_links"):
                out.setdefault(int(r["discord_id"]), set()).add(norm(r["char_name"]))
        if _has_table(conn, "alters"):
            for r in conn.execute("SELECT user_id, alter_name FROM alters"):
                out.setdefault(int(r["user_id"]), set()).add(norm(r["alter_name"]))
        return out
    finally:
        conn.close()


def last_seen_by_user(snap: Dict[str, Tuple[str, datetime]], chars: Dict[int, Set[str]]) -> Dict[int, Tuple[str, datetime]]:
    """{discord_id: (personaje, conexión más reciente)} entre todos sus personajes que figuran en el archivo."""
    out: Dict[int, Tuple[str, datetime]] = {}
    for uid, names in chars.items():
        best = None
        for n in names:
            hit = snap.get(n)
            if hit and (best is None or hit[1] > best[1]):
                best = hit
        if best:
            out[uid] = best
    return out


def owner_of_names(chars: Dict[int, Set[str]]) -> Dict[str, int]:
    return {n: uid for uid, names in chars.items() for n in names}


# ---------- asistencia a CTA ----------
ATTEND_VOICE_SECS = 20 * 60          # anotado + más de 20 min en el canal de voz de la CTA


def cta_attendance(db_path: str, guild_id: int, start: Optional[datetime], end: Optional[datetime], chars: Dict[int, Set[str]],
                   min_voice_secs: int = ATTEND_VOICE_SECS, user_id: Optional[int] = None) -> Tuple[int, Dict[int, int]]:
    """(CTAs del período, {usuario: CTAs con asistencia}). start/end None = desde siempre / hasta ahora.
    Asistencia = aparece en el killboard de la CTA (personaje registrado o alter) o se anotó y estuvo más de 20 min en el canal de voz.
    Solo CTAs normales cerradas (no /content ni canceladas). user_id: calcula solo para esa persona (más rápido)."""
    owner = owner_of_names(chars)
    conn = connect(db_path)
    try:
        if not _has_table(conn, "cta_runs"):
            return 0, {}
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cta_runs)")}
        sql, args = "SELECT id FROM cta_runs WHERE guild_id=? AND status='closed'", [guild_id]
        if start is not None:
            sql += " AND closes_at>=?"
            args.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            sql += " AND closes_at<?"
            args.append(end.astimezone(timezone.utc).isoformat())
        if "kind" in cols:
            sql += " AND kind='cta'"
        runs = [int(r["id"]) for r in conn.execute(sql, args)]
        has_pos, has_voice = _has_table(conn, "cta_positions"), _has_table(conn, "cta_voice_sessions")
        has_kb = _has_table(conn, "kb_players") and _has_table(conn, "kb_battles")
        uf, ua = (" AND user_id=?", (user_id,)) if user_id is not None else ("", ())
        counts: Dict[int, int] = {}
        now = datetime.now(timezone.utc)
        for cta_id in runs:
            present: Set[int] = set()
            if has_kb:
                for r in conn.execute("SELECT p.name_norm FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id WHERE b.cta_id=?", (cta_id,)):
                    uid = owner.get(norm(r["name_norm"]))
                    if uid is not None:
                        present.add(uid)
            if has_pos and has_voice:
                signed = {int(r["user_id"]) for r in conn.execute(f"SELECT user_id FROM cta_positions WHERE cta_id=?{uf}", (cta_id, *ua))}
                if signed:
                    secs: Dict[int, float] = {}
                    for r in conn.execute(f"SELECT user_id, joined_at, left_at FROM cta_voice_sessions WHERE cta_id=?{uf}", (cta_id, *ua)):
                        stop = _dt(r["left_at"]) if r["left_at"] else now
                        secs[int(r["user_id"])] = secs.get(int(r["user_id"]), 0.0) + max(0.0, (stop - _dt(r["joined_at"])).total_seconds())
                    present |= {u for u in signed if secs.get(u, 0.0) > min_voice_secs}
            if user_id is not None:
                present &= {user_id}
            for uid in present:
                counts[uid] = counts.get(uid, 0) + 1
        return len(runs), counts
    finally:
        conn.close()


# ---------- poca asistencia desde que entró (solo killboards cargados) ----------
def kb_cta_presence(db_path: str, guild_id: int, chars: Dict[int, Set[str]]) -> List[Tuple[datetime, Set[int]]]:
    """[(cierre de la CTA, {usuarios en su killboard})] de las CTA normales cerradas que tienen al menos un killboard cargado,
    ordenadas por fecha. Solo cuenta el killboard (personaje registrado o alter), no la voz."""
    owner = owner_of_names(chars)
    conn = connect(db_path)
    try:
        if not (_has_table(conn, "cta_runs") and _has_table(conn, "kb_battles") and _has_table(conn, "kb_players")):
            return []
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cta_runs)")}
        sql = ("SELECT r.id, r.closes_at FROM cta_runs r WHERE r.guild_id=? AND r.status='closed'"
               + (" AND r.kind='cta'" if "kind" in cols else "")
               + " AND EXISTS (SELECT 1 FROM kb_battles b WHERE b.cta_id=r.id) ORDER BY r.closes_at")
        out: List[Tuple[datetime, Set[int]]] = []
        for r in conn.execute(sql, (guild_id,)).fetchall():
            present: Set[int] = set()
            for p in conn.execute("SELECT p.name_norm FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id WHERE b.cta_id=?", (r["id"],)):
                uid = owner.get(norm(p["name_norm"]))
                if uid is not None:
                    present.add(uid)
            out.append((_dt(r["closes_at"]), present))
        return out
    finally:
        conn.close()


def cta_signups(db_path: str, guild_id: int) -> List[Tuple[datetime, Set[int]]]:
    """[(cierre de la CTA, {anotados})] de todas las CTA normales cerradas (con o sin killboard), ordenadas por fecha."""
    conn = connect(db_path)
    try:
        if not (_has_table(conn, "cta_runs") and _has_table(conn, "cta_positions")):
            return []
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cta_runs)")}
        sql = ("SELECT id, closes_at FROM cta_runs WHERE guild_id=? AND status='closed'"
               + (" AND kind='cta'" if "kind" in cols else "") + " ORDER BY closes_at")
        out: List[Tuple[datetime, Set[int]]] = []
        for r in conn.execute(sql, (guild_id,)).fetchall():
            signed = {int(p["user_id"]) for p in conn.execute("SELECT user_id FROM cta_positions WHERE cta_id=?", (r["id"],))}
            out.append((_dt(r["closes_at"]), signed))
        return out
    finally:
        conn.close()


def registered_at_by_user(db_path: str) -> Dict[int, Tuple[datetime, str]]:
    """{discord_id: (fecha en que lo reclutaron o vinculó su personaje principal, nombre del personaje)} (albion_links)."""
    conn = connect(db_path)
    try:
        if not _has_table(conn, "albion_links"):
            return {}
        return {int(r["discord_id"]): (_dt(r["registered_at"]), r["char_name"])
                for r in conn.execute("SELECT discord_id, registered_at, char_name FROM albion_links")}
    finally:
        conn.close()


def attendance_since(ctas: List[Tuple[datetime, Set[int]]], user_id: int, since: datetime, now: datetime,
                     weekly: int = CTA_WEEKLY_MIN) -> Optional[Tuple[int, int, int]]:
    """(asistencias, pedidas, CTA con killboard) desde `since` (o desde el primer killboard cargado, si es posterior).
    Pedidas = `weekly` por semana, proporcional a los días, y nunca más que las CTA con killboard del período.
    None = todavía no hay nada que evaluar (sin CTA con killboard o menos de un día)."""
    if not ctas:
        return None
    start = max(since, ctas[0][0])
    after = [present for when, present in ctas if when >= start]
    days = (now - start).total_seconds() / 86400
    need = min(int(weekly * days / 7), len(after))
    if not after or need <= 0:
        return None
    return sum(1 for present in after if user_id in present), need, len(after)


# ---------- ausencias (/ausencia) ----------
def ausencias_en(db_path: str, guild_id: int, start: datetime, end: datetime, now: Optional[datetime] = None) -> Dict[int, dict]:
    """{usuario: ausencia} vigente ahora o que se cruza con la semana evaluada (la que termina más tarde)."""
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        if not _has_table(conn, "ausencias"):
            return {}
        out: Dict[int, dict] = {}
        rows = conn.execute("SELECT * FROM ausencias WHERE guild_id=? AND (hasta>? OR (hasta>? AND created_at<?)) ORDER BY hasta",
                            (guild_id, now.isoformat(), start.isoformat(), end.isoformat()))
        for r in rows:
            out[int(r["user_id"])] = dict(r)
        return out
    finally:
        conn.close()


# ---------- avisos de salida de la guild ----------
def get_alert(db_path: str, discord_id: int) -> Optional[dict]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM guild_leave_alerts WHERE discord_id=?", (discord_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def alert_by_message(db_path: str, message_id: int) -> Optional[dict]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM guild_leave_alerts WHERE message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def add_alert(db_path: str, discord_id: int, char_name: str, channel_id: int, message_id: int) -> None:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO guild_leave_alerts(discord_id, char_name, status, channel_id, message_id, created_at) "
                         "VALUES (?,?,?,?,?,?)", (discord_id, char_name, "pending", channel_id, message_id, utcnow_iso()))
    finally:
        conn.close()


def close_alert(db_path: str, message_id: int, status: str, actor_id: int) -> bool:
    """Marca el aviso como resuelto. False si ya lo había resuelto otro (dos clics a la vez)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE guild_leave_alerts SET status=?, handled_by=?, handled_at=? WHERE message_id=? AND status='pending'",
                                (status, actor_id, utcnow_iso(), message_id)).rowcount > 0
    finally:
        conn.close()


def reopen_alert(db_path: str, message_id: int) -> None:
    """Vuelve a dejar pendiente un aviso (la expulsión falló): los botones siguen sirviendo."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE guild_leave_alerts SET status='pending', handled_by=NULL, handled_at=NULL WHERE message_id=?", (message_id,))
    finally:
        conn.close()


def clear_alerts(db_path: str, discord_ids: Iterable[int]) -> int:
    """Borra los avisos de quienes volvieron a la guild (si se van de nuevo, se avisa de nuevo)."""
    ids = list(discord_ids)
    if not ids:
        return 0
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return sum(conn.execute("DELETE FROM guild_leave_alerts WHERE discord_id=?", (i,)).rowcount for i in ids)
    finally:
        conn.close()


def all_alert_ids(db_path: str) -> Set[int]:
    init_schema(db_path)
    conn = connect(db_path)
    try:
        return {int(r["discord_id"]) for r in conn.execute("SELECT discord_id FROM guild_leave_alerts")}
    finally:
        conn.close()


def week_bounds(offset: int = -1, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Semana lunes 00:00 → lunes 00:00 UTC (igual que coaching). offset=-1: la anterior, 0: la actual."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(weeks=offset)
    return start, start + timedelta(weeks=1)
