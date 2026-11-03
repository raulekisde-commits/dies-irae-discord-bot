"""Alters: cuentas / personajes extra de una persona del Discord (lógica y persistencia, sin Discord).

Cada alter queda enlazado a un usuario de Discord y guarda QUIÉN lo autorizó (quien ejecutó el comando) y cuándo."""
import re
import sqlite3
from typing import List, Optional

from dbutil import connect, utcnow_iso, write_tx

MAX_ALTERS_PER_USER = 10
SCHEMA = """
CREATE TABLE IF NOT EXISTS alters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,          -- persona del Discord dueña del alter
    alter_name TEXT NOT NULL,             -- nombre de la cuenta / personaje alter
    added_by   INTEGER NOT NULL,          -- quien ejecutó el comando (autorizó el alter)
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_alters_name ON alters(lower(alter_name));
CREATE INDEX IF NOT EXISTS ix_alters_user ON alters(user_id);
"""


class AlterError(Exception):
    pass


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def add_alter(db_path: str, user_id: int, alter_name: str, added_by: int, note: Optional[str] = None) -> dict:
    name = clean_name(alter_name)
    if not (2 <= len(name) <= 30) or not re.fullmatch(r"[\w .'\-]+", name, re.UNICODE):
        raise AlterError("Nombre de alter inválido (2 a 30 caracteres: letras, números, espacios, . ' -).")
    conn = connect(db_path)
    try:
        with write_tx(conn):
            dup = conn.execute("SELECT user_id FROM alters WHERE lower(alter_name)=lower(?)", (name,)).fetchone()
            if dup:
                raise AlterError(f"**{name}** ya está enlazado como alter de <@{dup['user_id']}>.")
            try:                                            # no puede ser el personaje principal de otra persona
                main = conn.execute("SELECT discord_id FROM albion_links WHERE lower(char_name)=lower(?)", (name,)).fetchone()
            except sqlite3.OperationalError:
                main = None
            if main and int(main["discord_id"]) != user_id:
                raise AlterError(f"**{name}** es el personaje principal de <@{main['discord_id']}>, no puede ser alter de otra persona.")
            n = conn.execute("SELECT COUNT(*) c FROM alters WHERE user_id=?", (user_id,)).fetchone()["c"]
            if n >= MAX_ALTERS_PER_USER:
                raise AlterError(f"Esa persona ya tiene {MAX_ALTERS_PER_USER} alters (máximo).")
            cur = conn.execute("INSERT INTO alters(user_id, alter_name, added_by, note, created_at) VALUES (?,?,?,?,?)",
                               (user_id, name, added_by, (note or "").strip()[:200] or None, utcnow_iso()))
            return dict(conn.execute("SELECT * FROM alters WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def remove_alter(db_path: str, alter_name: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT * FROM alters WHERE lower(alter_name)=lower(?)", (clean_name(alter_name),)).fetchone()
            if r:
                conn.execute("DELETE FROM alters WHERE id=?", (r["id"],))
            return dict(r) if r else None
    finally:
        conn.close()


def alters_of(db_path: str, user_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM alters WHERE user_id=? ORDER BY id", (user_id,)).fetchall()]
    finally:
        conn.close()


def all_alters(db_path: str) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM alters ORDER BY user_id, id").fetchall()]
    finally:
        conn.close()


def find_alter(db_path: str, alter_name: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM alters WHERE lower(alter_name)=lower(?)", (clean_name(alter_name),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()
