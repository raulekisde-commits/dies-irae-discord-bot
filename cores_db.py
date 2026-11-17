"""Base de /core y /vortex: reportes de orbes con foto, aprobación del Staff, reparto de tiradas de Wujupón y estadísticas.

Cada reporte pasa por: pending → processing (mientras se dan las tiradas) → approved | shared | rejected.
La estadística de /stats cuenta, por tipo y rareza, cuántos orbes le dieron tiradas a cada persona (orb_awards con spins > 0)."""
import random
from typing import Dict, Iterable, List, Optional

from dbutil import connect, utcnow_iso, write_tx

KINDS = ("core", "vortex")
RARITIES = ("verde", "azul", "morado", "dorado")
RARITY_LABEL = {"verde": "Verde", "azul": "Azul", "morado": "Morado", "dorado": "Dorado"}
RARITY_EMOJI = {"verde": "🟢", "azul": "🔵", "morado": "🟣", "dorado": "🟡"}
RARITY_SPINS = {"verde": 1, "azul": 2, "morado": 3, "dorado": 5}
KIND_LABEL = {"core": "Core", "vortex": "Vortex"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orb_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,                 -- core | vortex
    user_id     INTEGER NOT NULL,              -- quien lo reportó
    rarity      TEXT NOT NULL,                 -- verde | azul | morado | dorado
    share       INTEGER NOT NULL DEFAULT 0,    -- quiere repartirlo
    status      TEXT NOT NULL DEFAULT 'pending',
    channel_id  INTEGER,
    message_id  INTEGER UNIQUE,
    created_at  TEXT NOT NULL,
    decided_by  INTEGER,
    decided_at  TEXT,
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS orb_awards (
    report_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    spins     INTEGER NOT NULL,
    PRIMARY KEY (report_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_orb_awards_user ON orb_awards(user_id);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _row(r) -> Optional[dict]:
    return dict(r) if r is not None else None


def create(db_path: str, *, guild_id: int, kind: str, user_id: int, rarity: str, share: bool) -> int:
    if kind not in KINDS or rarity not in RARITIES:
        raise ValueError("tipo o rareza inválidos")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO orb_reports(guild_id, kind, user_id, rarity, share, created_at) VALUES (?,?,?,?,?,?)",
                               (guild_id, kind, user_id, rarity, 1 if share else 0, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def set_message(db_path: str, report_id: int, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE orb_reports SET channel_id=?, message_id=? WHERE id=?", (channel_id, message_id, report_id))
    finally:
        conn.close()


def delete(db_path: str, report_id: int) -> None:
    """Solo para cuando no se pudo publicar el panel."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM orb_reports WHERE id=? AND status='pending'", (report_id,))
    finally:
        conn.close()


def get(db_path: str, report_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM orb_reports WHERE id=?", (report_id,)).fetchone())
    finally:
        conn.close()


def get_by_message(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM orb_reports WHERE message_id=?", (message_id,)).fetchone())
    finally:
        conn.close()


def set_rarity(db_path: str, report_id: int, rarity: str) -> bool:
    """Editar el color: solo mientras está pendiente."""
    if rarity not in RARITIES:
        raise ValueError("rareza inválida")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return conn.execute("UPDATE orb_reports SET rarity=? WHERE id=? AND status='pending'", (rarity, report_id)).rowcount == 1
    finally:
        conn.close()


def claim(db_path: str, report_id: int) -> Optional[dict]:
    """Toma el reporte para decidirlo (pending → processing). None si ya lo tomó o lo decidió otro."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("UPDATE orb_reports SET status='processing' WHERE id=? AND status='pending'", (report_id,)).rowcount != 1:
                return None
            return _row(conn.execute("SELECT * FROM orb_reports WHERE id=?", (report_id,)).fetchone())
    finally:
        conn.close()


def release(db_path: str, report_id: int) -> None:
    """Algo falló a mitad de camino: vuelve a pendiente para reintentar (las tiradas ya dadas no se repiten: son idempotentes)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE orb_reports SET status='pending' WHERE id=? AND status='processing'", (report_id,))
    finally:
        conn.close()


def finish(db_path: str, report_id: int, status: str, actor_id: int, awards: Optional[Dict[int, int]] = None,
           reason: Optional[str] = None) -> None:
    """processing → approved | shared | rejected, guardando quién recibió cuántas tiradas (para /stats)."""
    if status not in ("approved", "shared", "rejected"):
        raise ValueError("estado inválido")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE orb_reports SET status=?, decided_by=?, decided_at=?, reason=? WHERE id=? AND status='processing'",
                         (status, actor_id, utcnow_iso(), reason, report_id))
            for uid, n in (awards or {}).items():
                if n > 0:
                    conn.execute("INSERT OR REPLACE INTO orb_awards(report_id, user_id, spins) VALUES (?,?,?)", (report_id, int(uid), int(n)))
    finally:
        conn.close()


def allocate(spins: int, participants: Iterable[int], rng: Optional[random.Random] = None) -> Dict[int, int]:
    """Reparte `spins` tiradas: a cada uno le toca la parte entera y el sobrante se sortea (1 tirada por ganador, sin repetir).
    Si hay más personas que tiradas, todo se sortea. Devuelve {user_id: tiradas} solo con quienes reciben algo."""
    people = list(dict.fromkeys(int(u) for u in participants))
    if not people or spins <= 0:
        return {}
    rng = rng or random.SystemRandom()
    base, rest = divmod(spins, len(people))
    out = {u: base for u in people}
    for u in rng.sample(people, rest):
        out[u] += 1
    return {u: n for u, n in out.items() if n > 0}


def user_stats(db_path: str, user_id: int) -> Dict[str, Dict[str, int]]:
    """{'core': {'verde': n, ...}, 'vortex': {...}}: orbes que le dieron tiradas a la persona."""
    out = {k: {r: 0 for r in RARITIES} for k in KINDS}
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT r.kind, r.rarity, COUNT(*) AS n FROM orb_awards a JOIN orb_reports r ON r.id=a.report_id "
                            "WHERE a.user_id=? AND a.spins>0 AND r.status IN ('approved','shared') GROUP BY r.kind, r.rarity",
                            (user_id,)).fetchall()
    except Exception:                                  # tablas todavía no creadas
        return out
    finally:
        conn.close()
    for r in rows:
        if r["kind"] in out and r["rarity"] in out[r["kind"]]:
            out[r["kind"]][r["rarity"]] = int(r["n"])
    return out


def awards_of(db_path: str, report_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM orb_awards WHERE report_id=? ORDER BY spins DESC, user_id", (report_id,))]
    finally:
        conn.close()
