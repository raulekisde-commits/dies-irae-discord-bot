"""Piezas puras (sin Discord) del sistema de CTA: título por hora redonda, rol único por posición y envíos programados."""
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from dbutil import connect, write_tx, utcnow_iso

CTA_CHANNEL_ID = 1559574169593716776
CTA_VOICE_ID = 1257910727843250177
STATS_CHANNEL_ID = 1565980248133345300
SCHEDULE_GRACE_SECONDS = 120          # si el bot estuvo caído más que esto a la hora de publicar, la CTA programada se descarta

# Los 11 roles de juego: cada posición pide exactamente uno de ellos.
PLAYED_ROLES = {
    1260755129754189854: "Tank", 1560213580928913408: "Clump Maker", 1260755151296266331: "Healer", 1260755289062248458: "Dps",
    1560213502524657774: "Shaper", 1559711933702086727: "Galatinas", 1560212286075830272: "Bloodletter", 1260755342472646656: "Supp",
    1560341674046136511: "Pierce", 1469369363739181087: "Battle Mount", 1565982191945519194: "Bomb",
}


def round_hour(dt: datetime) -> datetime:
    """Hora más cercana (a las :30 sube): 21:50 -> 22:00, 22:15 -> 22:00, 22:30 -> 23:00."""
    dt = dt.astimezone(timezone.utc)
    return (dt + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)


def base_title(close_at: datetime) -> str:
    r = round_hour(close_at)
    return f"CTA {r.day:02d}/{r.month:02d} {r.hour:02d} UTC"


def unique_title(base: str, existing: Iterable[str]) -> str:
    taken = {(t or "").casefold() for t in existing}
    if base.casefold() not in taken:
        return base
    n = 2
    while f"{base} #{n}".casefold() in taken:
        n += 1
    return f"{base} #{n}"


def parse_hhmm_utc(text: str, now: Optional[datetime] = None) -> datetime:
    """HH:MM (UTC) -> próxima vez que ocurre esa hora. ValueError con mensaje listo si no sirve."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    t = (text or "").strip()
    try:
        h, m = t.split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("Hora inválida. Usá `HH:MM` en UTC (ej: `21:30`).")
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at


def required_role(slot_role_ids) -> Optional[int]:
    """El único rol que pide una posición (el primero si hubiera varios guardados). None = sin rol cargado."""
    for r in slot_role_ids or []:
        try:
            return int(r)
        except (TypeError, ValueError):
            continue
    return None


def migrate_role_ids(role_ids_json: str) -> Tuple[str, bool]:
    """Varios roles -> el primero. Devuelve (json nuevo, si cambió)."""
    try:
        ids = json.loads(role_ids_json or "[]")
    except ValueError:
        ids = []
    first = required_role(ids)
    new = json.dumps([str(first)] if first is not None else [])
    return new, new != (role_ids_json or "[]") and len(ids) > 1


def access_missing_role(member_role_ids: Iterable[int], slot_role_ids, flex_role_id: int) -> Optional[int]:
    """None si puede anotarse; si no, el id del rol que le falta. Flex ignora el permiso."""
    have = {int(r) for r in member_role_ids}
    if flex_role_id in have:
        return None
    need = required_role(slot_role_ids)
    if need is None or need in have:
        return None
    return need


# ---------- CTA programadas ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS cta_scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    composition_name TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    title TEXT,
    publish_at TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
"""


def init_schedule(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def schedule(db_path: str, *, guild_id: int, composition_name: str, minutes: int, title: Optional[str], publish_at: datetime,
             created_by: int) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO cta_scheduled(guild_id, composition_name, minutes, title, publish_at, created_by) VALUES (?,?,?,?,?,?)",
                               (guild_id, composition_name, minutes, title, publish_at.astimezone(timezone.utc).isoformat(), created_by))
            return int(cur.lastrowid)
    finally:
        conn.close()


def take_due(db_path: str, now: Optional[datetime] = None) -> Tuple[List[dict], List[dict]]:
    """Reclama las programadas vencidas. Devuelve (para publicar, descartadas por llegar tarde). Cada una se entrega una sola vez."""
    now = (now or datetime.now(timezone.utc))
    conn = connect(db_path)
    go, dropped = [], []
    try:
        with write_tx(conn):
            for r in conn.execute("SELECT * FROM cta_scheduled WHERE status='pending' ORDER BY publish_at").fetchall():
                at = datetime.fromisoformat(r["publish_at"])
                if at > now:
                    continue
                late = (now - at).total_seconds() > SCHEDULE_GRACE_SECONDS
                conn.execute("UPDATE cta_scheduled SET status=? WHERE id=?", ("dropped" if late else "taken", r["id"]))
                (dropped if late else go).append(dict(r))
    finally:
        conn.close()
    return go, dropped


def pending_list(db_path: str, guild_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM cta_scheduled WHERE status='pending' AND guild_id=? ORDER BY publish_at", (guild_id,))]
    finally:
        conn.close()


def cancel_scheduled(db_path: str, sched_id: int, guild_id: int) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE cta_scheduled SET status='cancelled' WHERE id=? AND guild_id=? AND status='pending'",
                                (sched_id, guild_id)).rowcount > 0
    finally:
        conn.close()
