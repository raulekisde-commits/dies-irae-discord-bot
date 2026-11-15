"""Ex-miembros de la guild de Albion (lógica y persistencia, sin Discord).

- Reconstrucción del pasado (una sola vez): killboards cargados (solo jugadores de la guild), registros Discord↔Albion de la guild,
  últimos que se fueron del servidor y reclutas registrados por el bot. Se descarta a quien hoy sigue en la guild.
- De ahora en adelante: se compara cada lista de la guild con la anterior y quien desaparece queda guardado con la fecha.
- En el ticket de reclutamiento se busca por cuenta de Discord o por el nick escrito, y se guardan las respuestas
  «¿por qué te fuiste?» / «¿por qué volvés?»."""
import json
import re
import sqlite3
from typing import Iterable, List, Optional, Sequence, Tuple

from dbutil import connect, utcnow_iso, write_tx

# Corte de seguridad: si de una lista a la otra «se van» más que esto, se asume una respuesta incompleta de la API y no se anota nada.
MAX_LEAVE_ABS = 15
MAX_LEAVE_RATIO = 0.25
BACKFILL_KEY = "backfill_done"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ex_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name_norm   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    char_id     TEXT,
    discord_id  INTEGER,
    source      TEXT NOT NULL,            -- salida (detectada por el bot) | killboard | registro | reclutamiento
    last_seen   TEXT,                     -- última vez que se lo vio en la guild (si se sabe)
    left_at     TEXT,                     -- cuándo se fue (NULL = antes de que existiera este registro, fecha desconocida)
    times_left  INTEGER NOT NULL DEFAULT 0,
    rejoined_at TEXT,                     -- volvió a figurar en la guild (se limpia si se vuelve a ir)
    reason_left TEXT,                     -- respuestas del ticket de reclutamiento
    reason_back TEXT,
    answered_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ex_members_char ON ex_members(char_id);
CREATE INDEX IF NOT EXISTS ix_ex_members_discord ON ex_members(discord_id);
CREATE TABLE IF NOT EXISTS ex_known (     -- última lista de la guild vista por el bot (para detectar quién se fue)
    key     TEXT PRIMARY KEY,             -- char_id, o «n:<nombre>» si no hay ID
    name    TEXT NOT NULL,
    char_id TEXT
);
CREATE TABLE IF NOT EXISTS ex_tickets (   -- tickets de reclutamiento abiertos por ex-miembros
    channel_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    ex_ids     TEXT NOT NULL,             -- JSON [id, ...]
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def norm(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def _key(char_id, name) -> str:
    return str(char_id) if char_id else "n:" + norm(name)


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_meta(db_path: str, key: str) -> Optional[str]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT value FROM ex_meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_meta(db_path: str, key: str, value: str) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO ex_meta(key, value) VALUES (?,?)", (key, value))
    finally:
        conn.close()


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:       # tabla de otro módulo que todavía no existe
        return []


# ---------- reconstrucción del pasado ----------
def collect_candidates(db_path: str, guild_name: str) -> List[dict]:
    """Personajes que en algún momento estuvieron en la guild, según lo que el bot ya tiene guardado."""
    gl = norm(guild_name)
    out = []
    conn = connect(db_path)
    try:
        for r in _rows(conn, "SELECT p.name, MAX(b.loaded_at) AS seen FROM kb_players p JOIN kb_battles b ON b.battle_id = p.battle_id "
                             "GROUP BY p.name_norm"):
            out.append(dict(name=r["name"], char_id=None, discord_id=None, source="killboard", last_seen=r["seen"]))
        for r in _rows(conn, "SELECT discord_id, char_id, char_name, guild_name, method, updated_at FROM albion_links"):
            if norm(r["guild_name"]) == gl or r["method"] == "recruitment":
                out.append(dict(name=r["char_name"], char_id=r["char_id"], discord_id=r["discord_id"], source="registro",
                                last_seen=r["updated_at"]))
        for r in _rows(conn, "SELECT discord_id, left_at, regs_json FROM left_members"):
            for g in json.loads(r["regs_json"] or "[]"):
                if norm(g.get("guild_name")) == gl or g.get("method") == "recruitment":
                    out.append(dict(name=g.get("char_name") or "", char_id=g.get("char_id"), discord_id=r["discord_id"],
                                    source="registro", last_seen=r["left_at"]))
        for r in _rows(conn, "SELECT target_id, at, detail FROM albion_link_audit WHERE action='recruit_linked'"):
            m = re.search(r"personaje=(\S+)", r["detail"] or "")
            if m:
                out.append(dict(name=m.group(1), char_id=None, discord_id=r["target_id"], source="reclutamiento", last_seen=r["at"]))
        pending = {norm(r["char_name"]) for r in _rows(conn, "SELECT char_name FROM recruit_pending")}   # aceptados que todavía no entraron
    finally:
        conn.close()
    return [c for c in out if norm(c["name"]) and norm(c["name"]) not in pending]


def backfill(db_path: str, roster: Sequence[Tuple[Optional[str], str]], candidates: Iterable[dict]) -> int:
    """Agrega como ex-miembros (fecha de salida desconocida) a los candidatos que hoy no están en la guild. Devuelve cuántos agregó."""
    in_ids = {str(c) for c, _ in roster if c}
    in_names = {norm(n) for _, n in roster}
    now, added = utcnow_iso(), 0
    conn = connect(db_path)
    try:
        with write_tx(conn):
            for c in candidates:
                n = norm(c["name"])
                if n in in_names or (c.get("char_id") and str(c["char_id"]) in in_ids):
                    continue
                cur = conn.execute("SELECT id, char_id, discord_id, last_seen FROM ex_members WHERE name_norm=?", (n,)).fetchone()
                if cur:
                    conn.execute("UPDATE ex_members SET char_id=COALESCE(char_id, ?), discord_id=COALESCE(discord_id, ?), "
                                 "last_seen=MAX(COALESCE(last_seen,''), COALESCE(?,'')), updated_at=? WHERE id=?",
                                 (c.get("char_id"), c.get("discord_id"), c.get("last_seen"), now, cur["id"]))
                    continue
                conn.execute("INSERT INTO ex_members(name_norm, name, char_id, discord_id, source, last_seen, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", (n, c["name"], c.get("char_id"), c.get("discord_id"), c["source"],
                                                          c.get("last_seen"), now, now))
                added += 1
    finally:
        conn.close()
    return added


# ---------- de ahora en adelante: quién se va ----------
def sync_roster(db_path: str, roster: Sequence[Tuple[Optional[str], str]]):
    """Compara la lista actual con la anterior. Devuelve (se_fueron, volvieron) como listas de nombres,
    o None si la diferencia es sospechosa (no se toca nada). La primera vez solo guarda la lista."""
    now = utcnow_iso()
    cur = {_key(c, n): (c, n) for c, n in roster if norm(n)}
    by_name = {norm(n): k for k, (_, n) in cur.items()}
    conn = connect(db_path)
    try:
        with write_tx(conn):
            known = {r["key"]: (r["char_id"], r["name"]) for r in conn.execute("SELECT * FROM ex_known")}
            left, back = [], []
            if known:
                gone = [(k, v) for k, v in known.items() if k not in cur and norm(v[1]) not in by_name]
                if len(gone) > max(MAX_LEAVE_ABS, int(len(known) * MAX_LEAVE_RATIO)):
                    return None
                for _, (cid, name) in gone:
                    n = norm(name)
                    link = _rows(conn, "SELECT discord_id FROM albion_links WHERE (char_id IS NOT NULL AND char_id=?) OR lower(char_name)=lower(?)",
                                 (cid, name))
                    did = link[0]["discord_id"] if link else None
                    row = conn.execute("SELECT id FROM ex_members WHERE name_norm=? OR (char_id IS NOT NULL AND char_id=?)", (n, cid)).fetchone()
                    if row:
                        conn.execute("UPDATE ex_members SET left_at=?, last_seen=?, times_left=times_left+1, rejoined_at=NULL, "
                                     "discord_id=COALESCE(?, discord_id), char_id=COALESCE(char_id, ?), updated_at=? WHERE id=?",
                                     (now, now, did, cid, now, row["id"]))
                    else:
                        conn.execute("INSERT INTO ex_members(name_norm, name, char_id, discord_id, source, last_seen, left_at, times_left, "
                                     "created_at, updated_at) VALUES (?,?,?,?,'salida',?,?,1,?,?)", (n, name, cid, did, now, now, now, now))
                    left.append(name)
            for k, (cid, name) in cur.items():                    # ex-miembros que vuelven a figurar en la guild
                r = conn.execute("SELECT id FROM ex_members WHERE rejoined_at IS NULL AND (name_norm=? OR (char_id IS NOT NULL AND char_id=?))",
                                 (norm(name), cid)).fetchone()
                if r:
                    conn.execute("UPDATE ex_members SET rejoined_at=?, updated_at=? WHERE id=?", (now, now, r["id"]))
                    back.append(name)
            conn.execute("DELETE FROM ex_known")
            conn.executemany("INSERT INTO ex_known(key, name, char_id) VALUES (?,?,?)", [(k, n, c) for k, (c, n) in cur.items()])
            return left, back
    finally:
        conn.close()


# ---------- consultas ----------
def find_matches(db_path: str, user_id: Optional[int], names: Iterable[str]) -> List[dict]:
    cands = set()
    for n in names or ():
        n = norm(n)
        if n:
            cands.add(n)
            cands.update(t for t in re.split(r"[\s,;/|]+", n) if t)
    conn = connect(db_path)
    try:
        seen, out = set(), []
        rows = list(_rows(conn, "SELECT * FROM ex_members WHERE discord_id=?", (int(user_id),))) if user_id is not None else []
        for c in sorted(cands):
            rows += _rows(conn, "SELECT * FROM ex_members WHERE name_norm=?", (c,))
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(dict(r))
        return out
    finally:
        conn.close()


def all_members(db_path: str) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in _rows(conn, "SELECT * FROM ex_members ORDER BY COALESCE(left_at, last_seen, '') DESC, name_norm")]
    finally:
        conn.close()


# ---------- tickets ----------
def link_ticket(db_path: str, channel_id: int, user_id: int, ex_ids: List[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO ex_tickets(channel_id, user_id, ex_ids, created_at) VALUES (?,?,?,?)",
                         (channel_id, user_id, json.dumps([int(i) for i in ex_ids]), utcnow_iso()))
    finally:
        conn.close()


def ticket_user(db_path: str, channel_id: int) -> Optional[int]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT user_id FROM ex_tickets WHERE channel_id=?", (channel_id,)).fetchone()
        return int(r["user_id"]) if r else None
    finally:
        conn.close()


def save_answers(db_path: str, channel_id: int, reason_left: str, reason_back: str) -> int:
    """Guarda las respuestas en los ex-miembros enlazados al ticket. Devuelve cuántos actualizó."""
    now = utcnow_iso()
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT ex_ids FROM ex_tickets WHERE channel_id=?", (channel_id,)).fetchone()
            ids = json.loads(r["ex_ids"]) if r else []
            for i in ids:
                conn.execute("UPDATE ex_members SET reason_left=?, reason_back=?, answered_at=?, updated_at=? WHERE id=?",
                             (reason_left[:1000], reason_back[:1000], now, now, int(i)))
            return len(ids)
    finally:
        conn.close()
