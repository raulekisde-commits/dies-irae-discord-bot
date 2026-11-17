"""Rol main de cada miembro (lógica y persistencia, sin Discord).

- Al aceptar un reclutamiento el reclutador elige el rol main (source='reclutador').
- /mainrole sync lo reconstruye desde los logs de «POSTULACIÓN ACEPTADA» (source='log', o 'log_respaldo' si salió de «Rol asignado»);
  nunca pisa lo que eligió un reclutador o se cargó a mano.
- /mainrole set lo corrige a mano (source='manual').
- main_share: en qué % de sus CTA cerradas jugó su rol main (misma base que «Roles más jugados» de /stats)."""
import re
import sqlite3
import unicodedata
from typing import Dict, List, Optional, Tuple

import ctautil as cu
from dbutil import connect, utcnow_iso, write_tx

# Roles que se pueden elegir como main: los de CTA, sin Bomb (igual que el paso de roles al aceptar un reclutamiento).
MAIN_ROLES: Dict[int, str] = {rid: name for rid, name in cu.PLAYED_ROLES.items() if name != "Bomb"}
_BY_NAME = {name: rid for rid, name in MAIN_ROLES.items()}

# Palabras (ya normalizadas: minúsculas, sin tildes) que identifican cada rol en lo que escribió el postulante.
ALIASES = {
    "Tank": ("tank", "tanque", "tanq", "tanke"),
    "Healer": ("healer", "heal", "healr", "curador", "sanador", "cura", "sagrado", "holy", "naturaleza", "nature"),
    "Dps": ("dps", "damage", "dano", "rdps", "mdps"),
    "Supp": ("support", "supp", "sup", "soporte", "arcano", "arcane"),
    "Galatinas": ("galatina", "galatinas", "gala", "galas"),
    "Shaper": ("shaper", "shapers", "shapeshifter"),
    "Clump Maker": ("clump maker", "clumpmaker", "clump", "clumper"),
    "Bloodletter": ("bloodletter", "blood letter", "bloodleter", "bl"),
    "Pierce": ("pierce", "piercer", "perforador"),
    "Battle Mount": ("battle mount", "battlemount", "bm", "montura"),
}

# Fuentes que la reconstrucción desde los logs no pisa.
PROTECTED_SOURCES = ("reclutador", "manual")

SCHEMA = """
CREATE TABLE IF NOT EXISTS member_main_role (
    user_id     INTEGER PRIMARY KEY,
    role_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,            -- reclutador | manual | log | log_respaldo
    raw_text    TEXT,                     -- lo que escribió el postulante en «Rol main» (si se sabe)
    set_by      INTEGER,                  -- reclutador / staff que lo cargó
    message_url TEXT,                     -- log de donde salió
    updated_at  TEXT NOT NULL
);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


_PATTERNS = [(_BY_NAME[name], " " + alias + " ") for name, aliases in ALIASES.items() if name in _BY_NAME for alias in aliases]


def parse_main(text: str) -> Optional[int]:
    """Rol main según el texto libre: el PRIMER rol que aparece ('Tanque, supp.' → Tank; 'Healer Sagrado' → Healer). None si no se reconoce."""
    t = _norm(text)
    best = None
    for rid, pat in _PATTERNS:
        i = t.find(pat)
        if i >= 0 and (best is None or i < best[0] or (i == best[0] and len(pat) > best[1])):
            best = (i, len(pat), rid)
    return best[2] if best else None


def role_name(role_id) -> str:
    return MAIN_ROLES.get(int(role_id), f"Rol {role_id}") if role_id is not None else "—"


def set_main(db_path: str, user_id: int, role_id: int, source: str, raw_text: Optional[str] = None,
             set_by: Optional[int] = None, message_url: Optional[str] = None, overwrite: bool = True) -> bool:
    """Guarda el rol main. Con overwrite=False no pisa lo que haya cargado un reclutador o el staff. True si se guardó."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if not overwrite:
                row = conn.execute("SELECT source FROM member_main_role WHERE user_id=?", (user_id,)).fetchone()
                if row is not None and row["source"] in PROTECTED_SOURCES:
                    return False
            conn.execute("""INSERT INTO member_main_role(user_id, role_id, source, raw_text, set_by, message_url, updated_at)
                            VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(user_id) DO UPDATE SET role_id=excluded.role_id, source=excluded.source, raw_text=excluded.raw_text,
                                set_by=excluded.set_by, message_url=excluded.message_url, updated_at=excluded.updated_at""",
                         (user_id, int(role_id), source, raw_text, set_by, message_url, utcnow_iso()))
        return True
    finally:
        conn.close()


def get_main(db_path: str, user_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM member_main_role WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def main_share(db_path: str, user_id: int) -> Optional[Tuple[int, int, int]]:
    """(role_id main, CTA cerradas jugando el main, CTA cerradas anotado). None si no tiene rol main guardado."""
    m = get_main(db_path, user_id)
    if m is None:
        return None
    rid = int(m["role_id"])
    conn = connect(db_path)
    try:
        row = conn.execute("""SELECT COUNT(DISTINCT pos.cta_id) AS total,
                                     COUNT(DISTINCT CASE WHEN pos.role_id=? OR (pos.role_id IS NULL AND LOWER(TRIM(pos.role_name))=LOWER(?))
                                                         THEN pos.cta_id END) AS main
                              FROM cta_positions pos
                              WHERE pos.user_id=? AND pos.cta_id IN (SELECT id FROM cta_runs WHERE status='closed')""",
                           (rid, role_name(rid), user_id)).fetchone()
    except sqlite3.OperationalError:            # todavía no hay tablas de CTA
        return rid, 0, 0
    finally:
        conn.close()
    return rid, int(row["main"] or 0), int(row["total"] or 0)


MIN_CTAS_FOR_TOP = 5   # para entrar al top de rol secundario (evita que 0 de 1 = 0 %)


def secondary_top(db_path: str, min_ctas: int = MIN_CTAS_FOR_TOP) -> List[dict]:
    """Quienes MENOS juegan su rol main (en CTA cerradas), de menor a mayor %.
    [{user_id, role_id, main, total, pct, top_other_id, top_other_n}]; top_other = el rol que más jugaron aparte del main."""
    conn = connect(db_path)
    try:
        mains = {int(r["user_id"]): int(r["role_id"]) for r in conn.execute("SELECT user_id, role_id FROM member_main_role")}
        rows = conn.execute("""SELECT pos.user_id AS user_id, pos.role_id AS role_id, pos.role_name AS role_name, COUNT(*) AS n
                               FROM cta_positions pos
                               WHERE pos.cta_id IN (SELECT id FROM cta_runs WHERE status='closed')
                               GROUP BY pos.user_id, pos.role_id, CASE WHEN pos.role_id IS NULL THEN LOWER(TRIM(pos.role_name)) END""").fetchall()
    except sqlite3.OperationalError:            # todavía no hay tablas de CTA
        return []
    finally:
        conn.close()
    names = {name.lower(): rid for rid, name in MAIN_ROLES.items()}
    per_user: Dict[int, Dict[object, int]] = {}
    for r in rows:
        uid = int(r["user_id"])
        if uid not in mains:
            continue
        key = int(r["role_id"]) if r["role_id"] is not None else names.get((r["role_name"] or "").strip().lower(), (r["role_name"] or "").strip() or None)
        if key is None:
            continue
        d = per_user.setdefault(uid, {})
        d[key] = d.get(key, 0) + int(r["n"])
    out = []
    for uid, counts in per_user.items():
        total = sum(counts.values())
        if total < min_ctas:
            continue
        rid = mains[uid]
        n_main = counts.get(rid, 0)
        others = sorted(((k, n) for k, n in counts.items() if k != rid), key=lambda kv: (-kv[1], str(kv[0])))
        out.append({"user_id": uid, "role_id": rid, "main": n_main, "total": total, "pct": 100.0 * n_main / total,
                    "top_other_id": others[0][0] if others else None, "top_other_n": others[0][1] if others else 0})
    out.sort(key=lambda d: (d["pct"], -d["total"], d["user_id"]))
    return out
