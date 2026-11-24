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
CREATE TABLE IF NOT EXISTS coaching_penalties (
    user_id    INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_flags (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_grades (
    vod_msg_id INTEGER PRIMARY KEY,
    player_id  INTEGER NOT NULL,
    coach_id   INTEGER NOT NULL,
    grade      INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 10),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coaching_grades_player ON coaching_grades(player_id);
CREATE INDEX IF NOT EXISTS idx_coaching_grades_coach ON coaching_grades(coach_id);
CREATE TABLE IF NOT EXISTS coaching_grade_prompts (
    reply_msg_id INTEGER PRIMARY KEY,
    coach_id     INTEGER NOT NULL,
    vod_msgs     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_track (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    title      TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coaching_track_members (
    user_id    INTEGER PRIMARY KEY,
    message_id INTEGER,
    done_at    TEXT
);
CREATE TABLE IF NOT EXISTS coaching_appeals (
    vod_msg_id       INTEGER PRIMARY KEY,
    thread_id        INTEGER NOT NULL,
    panel_msg_id     INTEGER,
    appeal_msg_id    INTEGER,
    requested_by     INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    proposed_by      INTEGER,
    proposed_grade   INTEGER,
    proposed_comment TEXT,
    proposal_msg_id  INTEGER,
    status           TEXT NOT NULL DEFAULT 'open',
    decided_by       INTEGER,
    decided_at       TEXT
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


ROLE_WORDS = {                                       # los 4 roles de coaching (mismos nombres que el menú de coaching.py)
    "Dps": ("dps", "damage"), "Tank": ("tank", "tanque"), "Supp": ("support", "supp", "soporte"),
    "Healer": ("healer", "heal", "curador", "sanador"),
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


def decide_review(db_path: str, review_msg_id: int, status: str, voted_by: int) -> bool:
    """Primer voto del Staff ('ok' o 'bad'). False si ya estaba decidida."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE coaching_reviews SET status=?, voted_by=? WHERE review_msg_id=? AND status='pending'",
                                (status, voted_by, review_msg_id)).rowcount == 1
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
            conn.execute("DELETE FROM coaching_grades WHERE vod_msg_id=?", (message_id,))
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


def count_since_by_user(db_path: str, since: Dict[int, datetime]) -> Dict[int, int]:
    """{usuario: VODs subidos desde su fecha en `since`} (solo usuarios de `since`)."""
    out = {u: 0 for u in since}
    conn = connect(db_path)
    try:
        for r in conn.execute("SELECT user_id, created_at FROM coaching_vods"):
            u = int(r["user_id"])
            if u not in since:
                continue
            try:
                when = datetime.fromisoformat(r["created_at"])
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= since[u]:
                out[u] += 1
        return out
    finally:
        conn.close()


def totals(db_path: str) -> Dict[int, int]:
    conn = connect(db_path)
    try:
        return {int(r["user_id"]): int(r["n"]) for r in conn.execute("SELECT user_id, COUNT(*) AS n FROM coaching_vods GROUP BY user_id")}
    finally:
        conn.close()


# ---------- notas del coach (1 a 10) por VOD ----------
GRADE_RE = re.compile(r"(?i)(?:\b(?:nota|puntaje|puntuaci[oó]n)\s*[:=]?\s*(10|[1-9])\b|\b(10|[1-9])\s*/\s*10\b)")


def grade_in_text(text: str) -> Optional[int]:
    """Nota escrita por el coach en la devolución («Nota: 7», «8/10»). None si no hay."""
    m = GRADE_RE.search(text or "")
    return int(m.group(1) or m.group(2)) if m else None


def add_grade_prompt(db_path: str, reply_msg_id: int, coach_id: int, vod_msgs: List[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO coaching_grade_prompts(reply_msg_id,coach_id,vod_msgs,created_at) VALUES (?,?,?,?)",
                         (reply_msg_id, coach_id, ",".join(str(int(m)) for m in vod_msgs), utcnow_iso()))
    finally:
        conn.close()


def last_review_target(db_path: str, coach_id: int, pending: List[int]) -> Optional[int]:
    """VOD (de `pending`) de la última review que empezó el coach respondiendo al video: sus mensajes siguientes la continúan."""
    if not pending:
        return None
    ok = set(int(m) for m in pending)
    conn = connect(db_path)
    try:
        for r in conn.execute("SELECT vod_msgs FROM coaching_grade_prompts WHERE coach_id=? ORDER BY reply_msg_id DESC LIMIT 50", (coach_id,)):
            for x in str(r["vod_msgs"]).split(","):
                if x and int(x) in ok:
                    return int(x)
        return None
    finally:
        conn.close()


def get_grade_prompt(db_path: str, reply_msg_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_grade_prompts WHERE reply_msg_id=?", (reply_msg_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["vod_msgs"] = [int(x) for x in str(d["vod_msgs"]).split(",") if x]
        return d
    finally:
        conn.close()


def get_grade(db_path: str, vod_msg_id: int) -> Optional[dict]:
    """Nota de un VOD (coach_id, grade) o None si todavía no tiene."""
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_grades WHERE vod_msg_id=?", (int(vod_msg_id),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def pending_with_role(db_path: str) -> List[Tuple[int, int, str]]:
    """(mensaje, post, rol) de los VODs sin puntuar que ya tienen rol, del más viejo al más nuevo."""
    conn = connect(db_path)
    try:
        return [(int(r["message_id"]), int(r["thread_id"]), r["role_label"]) for r in conn.execute(
            "SELECT message_id, thread_id, role_label FROM coaching_msgs WHERE done=0 AND role_label IS NOT NULL ORDER BY message_id")]
    finally:
        conn.close()


# ---------- segunda opinión (una por VOD; el cambio de nota lo autoriza el Council) ----------
def appeal_claim(db_path: str, vod_msg_id: int, thread_id: int, panel_msg_id: Optional[int], requested_by: int) -> bool:
    """Registra el pedido de segunda opinión. False si ese VOD ya tenía uno."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("INSERT OR IGNORE INTO coaching_appeals(vod_msg_id,thread_id,panel_msg_id,requested_by,created_at) "
                                "VALUES (?,?,?,?,?)", (int(vod_msg_id), int(thread_id), panel_msg_id, int(requested_by), utcnow_iso())).rowcount == 1
    finally:
        conn.close()


def appeal_set_msg(db_path: str, vod_msg_id: int, appeal_msg_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE coaching_appeals SET appeal_msg_id=? WHERE vod_msg_id=?", (int(appeal_msg_id), int(vod_msg_id)))
    finally:
        conn.close()


def appeal_get(db_path: str, *, vod_msg_id: Optional[int] = None, appeal_msg_id: Optional[int] = None,
               proposal_msg_id: Optional[int] = None) -> Optional[dict]:
    col, val = (("vod_msg_id", vod_msg_id) if vod_msg_id is not None else ("appeal_msg_id", appeal_msg_id)
                if appeal_msg_id is not None else ("proposal_msg_id", proposal_msg_id))
    if val is None:
        return None
    conn = connect(db_path)
    try:
        r = conn.execute(f"SELECT * FROM coaching_appeals WHERE {col}=?", (int(val),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def appeal_propose(db_path: str, vod_msg_id: int, coach_id: int, grade: int, comment: str, proposal_msg_id: int) -> bool:
    """Guarda la nota propuesta por el segundo coach (reemplaza una propuesta anterior sin decidir). False si ya se cerró."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE coaching_appeals SET proposed_by=?, proposed_grade=?, proposed_comment=?, proposal_msg_id=? "
                                "WHERE vod_msg_id=? AND status='open'",
                                (int(coach_id), int(grade), comment, int(proposal_msg_id), int(vod_msg_id))).rowcount == 1
    finally:
        conn.close()


def appeal_decide(db_path: str, proposal_msg_id: int, approve: bool, decided_by: int) -> bool:
    """El Council autoriza (cierra la segunda opinión) o rechaza (se puede proponer otra) esa propuesta. False si ya no está vigente."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if approve:
                sql = ("UPDATE coaching_appeals SET status='approved', decided_by=?, decided_at=? "
                       "WHERE proposal_msg_id=? AND status='open'")
                return conn.execute(sql, (int(decided_by), utcnow_iso(), int(proposal_msg_id))).rowcount == 1
            sql = ("UPDATE coaching_appeals SET proposed_by=NULL, proposed_grade=NULL, proposed_comment=NULL, proposal_msg_id=NULL "
                   "WHERE proposal_msg_id=? AND status='open'")
            return conn.execute(sql, (int(proposal_msg_id),)).rowcount == 1
    finally:
        conn.close()


def set_grades(db_path: str, vod_msgs: List[int], coach_id: int, grade: int) -> int:
    """Pone (o cambia) la nota de esos VODs. Solo los que siguen existiendo. Devuelve cuántos quedaron con nota."""
    grade = int(grade)
    if not 1 <= grade <= 10:
        raise ValueError("La nota va de 1 a 10.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            n = 0
            for mid in vod_msgs:
                r = conn.execute("SELECT user_id FROM coaching_msgs WHERE message_id=?", (int(mid),)).fetchone()
                if not r:
                    continue
                conn.execute("INSERT INTO coaching_grades(vod_msg_id,player_id,coach_id,grade,created_at) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(vod_msg_id) DO UPDATE SET coach_id=excluded.coach_id, grade=excluded.grade, created_at=excluded.created_at",
                             (int(mid), int(r["user_id"]), int(coach_id), grade, utcnow_iso()))
                n += 1
            return n
    finally:
        conn.close()


def player_avg(db_path: str, user_id: int) -> Tuple[Optional[float], int]:
    """(promedio, cantidad de VODs con nota) de un jugador."""
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT AVG(grade) AS a, COUNT(*) AS n FROM coaching_grades WHERE player_id=?", (user_id,)).fetchone()
        return (float(r["a"]) if r["n"] else None), int(r["n"])
    finally:
        conn.close()


def player_grades(db_path: str, user_id: int, limit: int = 10) -> List[dict]:
    """Últimas notas de un jugador (la más nueva primero)."""
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM coaching_grades WHERE player_id=? ORDER BY created_at DESC, vod_msg_id DESC LIMIT ?",
                                              (user_id, limit))]
    finally:
        conn.close()


def players_between(db_path: str, low: float, high: float) -> List[Tuple[int, float, int]]:
    """[(jugador, promedio, cantidad)] con promedio entre low y high (ambos incluidos), de mayor a menor promedio."""
    conn = connect(db_path)
    try:
        return [(int(r["player_id"]), float(r["a"]), int(r["n"])) for r in conn.execute(
            "SELECT player_id, AVG(grade) AS a, COUNT(*) AS n FROM coaching_grades GROUP BY player_id "
            "HAVING AVG(grade) >= ? AND AVG(grade) <= ? ORDER BY a DESC, n DESC, player_id", (float(low), float(high)))]
    finally:
        conn.close()


def coach_avgs(db_path: str, coach_id: Optional[int] = None) -> List[Tuple[int, float, int]]:
    """[(coach, promedio de las notas que pone, cantidad)] de más notas a menos."""
    sql = "SELECT coach_id, AVG(grade) AS a, COUNT(*) AS n FROM coaching_grades"
    args: list = []
    if coach_id is not None:
        sql += " WHERE coach_id=?"
        args.append(coach_id)
    conn = connect(db_path)
    try:
        return [(int(r["coach_id"]), float(r["a"]), int(r["n"])) for r in conn.execute(sql + " GROUP BY coach_id ORDER BY n DESC, coach_id", args)]
    finally:
        conn.close()


# ---------- track: lista única de quiénes tienen que mandar el VOD de una CTA ----------
def get_track(db_path: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM coaching_track WHERE id=1").fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def create_track(db_path: str, title: str, created_by: int, user_ids: List[int]) -> bool:
    """Crea el track (solo puede haber uno). False si ya hay uno activo."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("INSERT OR IGNORE INTO coaching_track(id, title, created_by, created_at) VALUES (1,?,?,?)",
                            (title, created_by, utcnow_iso())).rowcount != 1:
                return False
            conn.execute("DELETE FROM coaching_track_members")
            conn.executemany("INSERT OR IGNORE INTO coaching_track_members(user_id) VALUES (?)", [(u,) for u in user_ids])
            return True
    finally:
        conn.close()


def close_track(db_path: str) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM coaching_track_members")
            return conn.execute("DELETE FROM coaching_track WHERE id=1").rowcount == 1
    finally:
        conn.close()


def track_add(db_path: str, user_ids: List[int]) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return sum(conn.execute("INSERT OR IGNORE INTO coaching_track_members(user_id) VALUES (?)", (u,)).rowcount for u in user_ids)
    finally:
        conn.close()


def track_remove(db_path: str, user_id: int) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("DELETE FROM coaching_track_members WHERE user_id=?", (user_id,)).rowcount == 1
    finally:
        conn.close()


def track_members(db_path: str) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM coaching_track_members ORDER BY done_at IS NOT NULL, done_at")]
    finally:
        conn.close()


def track_mark(db_path: str, user_id: int, message_id: int, when: Optional[datetime] = None) -> bool:
    """Marca que user_id mandó el VOD del track (solo si hay track, está en la lista, no lo había mandado y el VOD es posterior al track)."""
    when_iso = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        with write_tx(conn):
            t = conn.execute("SELECT created_at FROM coaching_track WHERE id=1").fetchone()
            if t is None or when_iso < t["created_at"]:
                return False
            return conn.execute("UPDATE coaching_track_members SET message_id=?, done_at=? WHERE user_id=? AND done_at IS NULL",
                                (message_id, when_iso, user_id)).rowcount == 1
    finally:
        conn.close()


def track_unmark_message(db_path: str, message_id: int) -> bool:
    """Borraron el mensaje con el VOD del track: vuelve a quedar pendiente."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE coaching_track_members SET message_id=NULL, done_at=NULL WHERE message_id=?",
                                (message_id,)).rowcount > 0
    finally:
        conn.close()


def cta_signups_by_thread(db_path: str, thread_id: int) -> Optional[Tuple[int, List[int]]]:
    """(id de la CTA, anotados) de la CTA cuyo hilo es thread_id: inscriptos y, si ya cerró, el historial de posiciones (incluye tardíos).
    None si ese hilo no es de una CTA."""
    conn = connect(db_path)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "cta_runs" not in tables:
            return None
        r = conn.execute("SELECT id FROM cta_runs WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()
        if r is None:
            return None
        cta_id, out = int(r["id"]), {}
        for t in ("cta_signups", "cta_positions"):
            if t in tables:
                for x in conn.execute(f"SELECT user_id FROM {t} WHERE cta_id=?", (cta_id,)):
                    out[int(x["user_id"])] = None
        return cta_id, list(out)
    finally:
        conn.close()
