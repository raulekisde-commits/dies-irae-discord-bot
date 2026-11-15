"""Blacklist: personajes de Albion y cuentas de Discord vetadas (lógica y persistencia, sin Discord).

Cada entrada guarda el motivo, quién la agregó y cuándo. Se consulta al abrir un ticket de reclutamiento."""
import re
import sqlite3
from typing import Iterable, List, Optional

from dbutil import connect, utcnow_iso, write_tx

KINDS = ("albion", "discord")
SCHEMA = """
CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,             -- albion | discord
    value      TEXT NOT NULL,             -- nombre del personaje (albion) o ID de usuario (discord)
    display    TEXT NOT NULL,             -- cómo se muestra (nombre o tag de Discord al momento de agregarlo)
    reason     TEXT NOT NULL,
    added_by   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_blacklist_kv ON blacklist(kind, lower(value));
"""


class BlacklistError(Exception):
    pass


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _norm(kind: str, value) -> str:
    if kind not in KINDS:
        raise BlacklistError("Tipo inválido.")
    if kind == "discord":
        return str(int(value))
    name = clean_name(str(value))
    if not (2 <= len(name) <= 30) or not re.fullmatch(r"[\w .'\-]+", name, re.UNICODE):
        raise BlacklistError("Nombre de personaje inválido (2 a 30 caracteres: letras, números, espacios, . ' -).")
    return name


def add_entry(db_path: str, kind: str, value, display: str, reason: str, added_by: int) -> dict:
    val = _norm(kind, value)
    reason = (reason or "").strip()
    if not reason:
        raise BlacklistError("Tenés que poner un motivo.")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            dup = conn.execute("SELECT * FROM blacklist WHERE kind=? AND lower(value)=lower(?)", (kind, val)).fetchone()
            if dup:
                raise BlacklistError(f"**{dup['display']}** ya está en la blacklist (motivo: {dup['reason']}).")
            conn.execute("INSERT INTO blacklist(kind, value, display, reason, added_by, created_at) VALUES (?,?,?,?,?,?)",
                         (kind, val, (display or val)[:100], reason[:500], added_by, utcnow_iso()))
            return dict(conn.execute("SELECT * FROM blacklist WHERE kind=? AND lower(value)=lower(?)", (kind, val)).fetchone())
    finally:
        conn.close()


def remove_entry(db_path: str, kind: str, value) -> Optional[dict]:
    val = _norm(kind, value) if kind == "discord" else clean_name(str(value))
    conn = connect(db_path)
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM blacklist WHERE kind=? AND lower(value)=lower(?)", (kind, val)).fetchone()
            if row:
                conn.execute("DELETE FROM blacklist WHERE id=?", (row["id"],))
            return dict(row) if row else None
    finally:
        conn.close()


def all_entries(db_path: str) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM blacklist ORDER BY created_at")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def find_matches(db_path: str, user_id: Optional[int], names: Iterable[str]) -> List[dict]:
    """Entradas que coinciden con la cuenta de Discord o con alguno de los nombres (sin distinguir mayúsculas)."""
    cands = set()
    for n in names or ():
        n = clean_name(n)
        if n:
            cands.add(n.lower())
            cands.update(t.lower() for t in re.split(r"[\s,;/|]+", n) if t)   # «Nick1 / Nick2» → cada uno
    conn = connect(db_path)
    try:
        out = []
        if user_id is not None:
            out += [dict(r) for r in conn.execute("SELECT * FROM blacklist WHERE kind='discord' AND value=?", (str(int(user_id)),))]
        for c in sorted(cands):
            out += [dict(r) for r in conn.execute("SELECT * FROM blacklist WHERE kind='albion' AND lower(value)=?", (c,))]
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
