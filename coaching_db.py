"""Coaching: VODs que suben los jugadores a su post del foro (datos y cuentas, sin Discord).

- Cada persona tiene UN post (coaching_posts).
- Cada VOD (link o video adjunto) se guarda una sola vez por persona (coaching_vods). Los contadores se calculan de ahí,
  así que si borran el mensaje el VOD deja de contar.
- Para el mínimo semanal solo cuentan los VODs de quien tenía el rol Observación al subirlos (is_obs = 1).
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from dbutil import connect, utcnow_iso, write_tx

WEEKLY_MIN = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS coaching_posts (
    user_id    INTEGER PRIMARY KEY,
    thread_id  INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_msgs (
    message_id INTEGER PRIMARY KEY,
    thread_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    is_obs     INTEGER NOT NULL,
    role_label TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_vods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,
    url        TEXT,
    is_obs     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_coaching_vods_msg ON coaching_vods(message_id);
CREATE INDEX IF NOT EXISTS idx_coaching_vods_time ON coaching_vods(created_at);
CREATE TABLE IF NOT EXISTS coaching_reviews (
    review_msg_id INTEGER PRIMARY KEY,
    reply_msg_id  INTEGER NOT NULL,
    thread_id     INTEGER NOT NULL,
    coach_id      INTEGER NOT NULL,
    vod_msgs      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    voted_by      INTEGER,
    kind          TEXT NOT NULL DEFAULT 'live',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_review_votes (
    review_msg_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    bad           INTEGER NOT NULL,
    PRIMARY KEY (review_msg_id, user_id)
);
CREATE TABLE IF NOT EXISTS coaching_penalties (
    user_id    INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_flags (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if "done" not in {r["name"] for r in conn.execute("PRAGMA table_info(coaching_msgs)")}:
            conn.execute("ALTER TABLE coaching_msgs ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
    finally:
        conn.close()


# ---------- detección de VODs ----------
URL_RE = re.compile(r"https?://[^\s<>\[\]()]+", re.I)
VIDEO_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "medal.tv", "streamable.com", "vimeo.com", "kick.com", "outplayed.tv", "clips.twitch.tv")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _is_video_url(url: str) -> bool:
    h = _host(url)
    if any(h == v or h.endswith("." + v) for v in VIDEO_HOSTS):
        return True
    return urlparse(url).path.lower().endswith(VIDEO_EXT)


def _key(url: str) -> str:
    """Clave para no contar dos veces el mismo video aunque cambie el link (youtu.be/x = youtube.com/watch?v=x)."""
    p = urlparse(url)
    h, path = _host(url), p.path.rstrip("/")
    if h == "youtu.be" and path:
        return "yt:" + path.split("/")[-1]
    if h.endswith("youtube.com"):
        v = parse_qs(p.query).get("v")
        if v:
            return "yt:" + v[0]
        parts = [x for x in path.split("/") if x]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
            return "yt:" + parts[1]
    return f"{h}{path}".lower()


def extract_vods(content: str, attachments=()) -> List[Tuple[str, str]]:
    """[(clave, url)] de los videos del mensaje: archivos de video adjuntos y links de sitios de video. Sin repetidos."""
    out: Dict[str, str] = {}
    for a in attachments or []:
        ctype = (getattr(a, "content_type", None) or "").lower()
        name = (getattr(a, "filename", None) or "").lower()
        if ctype.startswith("video/") or name.endswith(VIDEO_EXT):
            out.setdefault(f"att:{getattr(a, 'id', name)}", getattr(a, "url", "") or name)
    for m in URL_RE.finditer(content or ""):
        url = m.group(0).rstrip(".,;:!?'\"")
        if _is_video_url(url):
            out.setdefault(_key(url), url)
    return list(out.items())


ROLE_WORDS = {
    "Tank": ("tank", "tanque"), "Healer": ("healer", "heal", "curador", "sanador"), "Support": ("support", "supp", "soporte"),
    "DPS": ("dps", "damage"), "Battle Mount": ("battle mount", "battlemount", "bm"), "Clump Maker": ("clump", "clump maker", "clumpmaker"),
    "Shaper": ("shaper",), "Galatinas": ("galatina", "galatinas"), "Bloodletter": ("bloodletter",), "Pierce": ("pierce",),
}


def guess_role(text: str) -> Optional[str]:
    """Rol de juego que menciona el texto del mensaje (sin contar los links). None si no dice ninguno o dice varios."""
    low = URL_RE.sub(" ", text or "").lower()
    words = set(re.findall(r"[a-záéíóúñ]+", low))
    found = [label for label, keys in ROLE_WORDS.items() if any((k in low) if " " in k else (k in words) for k in keys)]
    return found[0] if len(found) == 1 else None


# ---------- semanas (lunes 00:00 UTC a domingo 23:59 UTC) ----------
def week_start(dt: Optional[datetime] = None, offset: int = 0) -> datetime:
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(weeks=offset)


# ---------- posts ----------
def get_post(db_path: str, user_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_posts WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def post_by_thread(db_path: str, thread_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_posts WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_post(db_path: str, user_id: int, thread_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO coaching_posts(user_id, thread_id, created_at) VALUES (?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET thread_id=excluded.thread_id", (user_id, thread_id, utcnow_iso()))
    finally:
        conn.close()


# ---------- VODs ----------
def add_vods(db_path: str, *, message_id: int, thread_id: int, user_id: int, is_obs: bool, vods: List[Tuple[str, str]],
             created_at: Optional[datetime] = None, role_label: Optional[str] = None) -> int:
    """Guarda los VODs nuevos del mensaje (los que esa persona ya había subido no se repiten). Devuelve cuántos eran nuevos."""
    when = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    new = 0
    conn = connect(db_path)
    try:
        with write_tx(conn):
            for key, url in vods:
                cur = conn.execute("INSERT OR IGNORE INTO coaching_vods(message_id, user_id, key, url, is_obs, created_at) VALUES (?,?,?,?,?,?)",
                                   (message_id, user_id, key, url, 1 if is_obs else 0, when))
                new += cur.rowcount
            if new:
                conn.execute("INSERT OR IGNORE INTO coaching_msgs(message_id, thread_id, user_id, is_obs, role_label, created_at) VALUES (?,?,?,?,?,?)",
                             (message_id, thread_id, user_id, 1 if is_obs else 0, role_label, when))
        return new
    finally:
        conn.close()


def get_msg(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT m.*, (SELECT COUNT(*) FROM coaching_vods v WHERE v.message_id=m.message_id) AS vods "
                         "FROM coaching_msgs m WHERE m.message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_role(db_path: str, message_id: int, label: str) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE coaching_msgs SET role_label=? WHERE message_id=?", (label, message_id))
    finally:
        conn.close()


def pending_msgs(db_path: str, thread_id: int) -> List[int]:
    """Mensajes con VODs de un post cuya devolución todavía no se dio (del más viejo al más nuevo)."""
    conn = connect(db_path)
    try:
        return [int(r["message_id"]) for r in conn.execute(
            "SELECT message_id FROM coaching_msgs WHERE thread_id=? AND done=0 ORDER BY message_id", (thread_id,))]
    finally:
        conn.close()


def mark_done(db_path: str, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE coaching_msgs SET done=1 WHERE message_id=?", (message_id,))
    finally:
        conn.close()


# ---------- revisión de devoluciones cortas ----------
def add_review(db_path: str, *, review_msg_id: int, reply_msg_id: int, thread_id: int, coach_id: int, vod_msgs: List[int],
               kind: str = "live") -> None:
    """kind: 'live' (devolución corta, la tirada espera el voto) o 'retro' (ya cobrada; si está mal, solo penaliza)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR IGNORE INTO coaching_reviews(review_msg_id,reply_msg_id,thread_id,coach_id,vod_msgs,kind,created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (review_msg_id, reply_msg_id, thread_id, coach_id,
                                                    ",".join(str(int(m)) for m in vod_msgs), kind, utcnow_iso()))
    finally:
        conn.close()


def get_review(db_path: str, review_msg_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_reviews WHERE review_msg_id=?", (review_msg_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["vod_msgs"] = [int(x) for x in d["vod_msgs"].split(",") if x]
        return d
    finally:
        conn.close()


REVIEW_VOTES_NEEDED = 1                                    # decide el primer voto del Council


def vote_review(db_path: str, review_msg_id: int, user_id: int, bad: bool, needed: int = REVIEW_VOTES_NEEDED) -> Tuple[str, int, int]:
    """Voto de un miembro del Council (puede cambiarlo mientras siga abierta). Se decide cuando una opción llega a `needed`.
    Devuelve (estado, votos 'mal', votos 'bien'): estado 'closed' si ya estaba decidida, 'pending', 'bad' u 'ok'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT status FROM coaching_reviews WHERE review_msg_id=?", (review_msg_id,)).fetchone()
            if r is None or r["status"] != "pending":
                return "closed", 0, 0
            conn.execute("INSERT INTO coaching_review_votes(review_msg_id,user_id,bad) VALUES (?,?,?) "
                         "ON CONFLICT(review_msg_id,user_id) DO UPDATE SET bad=excluded.bad", (review_msg_id, user_id, 1 if bad else 0))
            c = conn.execute("SELECT COALESCE(SUM(bad),0) AS b, COUNT(*) - COALESCE(SUM(bad),0) AS g FROM coaching_review_votes "
                             "WHERE review_msg_id=?", (review_msg_id,)).fetchone()
            b, g = int(c["b"]), int(c["g"])
            status = "bad" if b >= needed else "ok" if g >= needed else "pending"
            if status != "pending":
                conn.execute("UPDATE coaching_reviews SET status=?, voted_by=? WHERE review_msg_id=?", (status, user_id, review_msg_id))
            return status, b, g
    finally:
        conn.close()


def has_review_for_reply(db_path: str, reply_msg_id: int) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM coaching_reviews WHERE reply_msg_id=?", (reply_msg_id,)).fetchone() is not None
    finally:
        conn.close()


def add_penalty(db_path: str, user_id: int) -> bool:
    """Penaliza al coach (no acumulable: si ya tenía una, queda una). True si es nueva."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("INSERT OR IGNORE INTO coaching_penalties(user_id,created_at) VALUES (?,?)",
                                (user_id, utcnow_iso())).rowcount == 1
    finally:
        conn.close()


def has_penalty(db_path: str, user_id: int) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM coaching_penalties WHERE user_id=?", (user_id,)).fetchone() is not None
    finally:
        conn.close()


def clear_penalty(db_path: str, user_id: int) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("DELETE FROM coaching_penalties WHERE user_id=?", (user_id,)).rowcount == 1
    finally:
        conn.close()


def done_msgs(db_path: str, thread_id: int) -> List[int]:
    """Mensajes con VODs de un post cuya devolución ya se dio."""
    conn = connect(db_path)
    try:
        return [int(r["message_id"]) for r in conn.execute(
            "SELECT message_id FROM coaching_msgs WHERE thread_id=? AND done=1 ORDER BY message_id", (thread_id,))]
    finally:
        conn.close()


def all_threads(db_path: str) -> List[Tuple[int, int]]:
    """(thread_id, user_id) de todos los posts."""
    conn = connect(db_path)
    try:
        return [(int(r["thread_id"]), int(r["user_id"])) for r in conn.execute("SELECT thread_id, user_id FROM coaching_posts")]
    finally:
        conn.close()


def take_flag(db_path: str, key: str) -> bool:
    """Marca una acción de una sola vez. True la primera vez; False si ya se había hecho."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("INSERT OR IGNORE INTO coaching_flags(key,created_at) VALUES (?,?)", (key, utcnow_iso())).rowcount == 1
    finally:
        conn.close()


def remove_message(db_path: str, message_id: int) -> int:
    """El mensaje se borró: sus VODs dejan de contar. Devuelve cuántos se quitaron."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            n = conn.execute("DELETE FROM coaching_vods WHERE message_id=?", (message_id,)).rowcount
            conn.execute("DELETE FROM coaching_msgs WHERE message_id=?", (message_id,))
            return n
    finally:
        conn.close()


def count_between(db_path: str, start: datetime, end: datetime, *, only_obs: Optional[bool] = None) -> Dict[int, int]:
    """{usuario: VODs} entre start (incluido) y end (excluido). only_obs=True: solo los subidos con el rol Observación; False: solo los demás."""
    sql = "SELECT user_id, COUNT(*) AS n FROM coaching_vods WHERE created_at >= ? AND created_at < ?"
    args: list = [start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()]
    if only_obs is not None:
        sql += " AND is_obs = ?"
        args.append(1 if only_obs else 0)
    conn = connect(db_path)
    try:
        return {int(r["user_id"]): int(r["n"]) for r in conn.execute(sql + " GROUP BY user_id", args)}
    finally:
        conn.close()


def totals(db_path: str) -> Dict[int, int]:
    conn = connect(db_path)
    try:
        return {int(r["user_id"]): int(r["n"]) for r in conn.execute("SELECT user_id, COUNT(*) AS n FROM coaching_vods GROUP BY user_id")}
    finally:
        conn.close()
