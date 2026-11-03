"""Bombs: grupos con nombre, rol de Discord y caller (lógica y persistencia, sin Discord).

El Council define cada bomb (nombre, rol y caller). Staff y reclutadores agregan o sacan gente; queda guardado quién la agregó."""
import re
import sqlite3
from typing import List, Optional

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS bombs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    role_id    INTEGER NOT NULL,
    caller_id  INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bombs_name ON bombs(guild_id, lower(name));
CREATE UNIQUE INDEX IF NOT EXISTS ux_bombs_role ON bombs(guild_id, role_id);
CREATE TABLE IF NOT EXISTS bomb_members (
    bomb_id  INTEGER NOT NULL REFERENCES bombs(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (bomb_id, user_id)
);
"""


class BombError(Exception):
    pass


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def _check_name(name: str) -> str:
    name = clean_name(name)
    if not (2 <= len(name) <= 40):
        raise BombError("Nombre inválido (2 a 40 caracteres).")
    return name


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def create_bomb(db_path: str, guild_id: int, name: str, role_id: int, caller_id: int, actor_id: int) -> dict:
    name = _check_name(name)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM bombs WHERE guild_id=? AND lower(name)=lower(?)", (guild_id, name)).fetchone():
                raise BombError(f"Ya existe una bomb llamada **{name}**.")
            other = conn.execute("SELECT name FROM bombs WHERE guild_id=? AND role_id=?", (guild_id, role_id)).fetchone()
            if other:
                raise BombError(f"Ese rol ya es el de la bomb **{other['name']}**.")
            now = utcnow_iso()
            cur = conn.execute("INSERT INTO bombs(guild_id,name,role_id,caller_id,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                               (guild_id, name, role_id, caller_id, actor_id, now, now))
            return dict(conn.execute("SELECT * FROM bombs WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def update_bomb(db_path: str, guild_id: int, bomb_id: int, *, name: Optional[str] = None, role_id: Optional[int] = None,
                caller_id: Optional[int] = None) -> dict:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            b = conn.execute("SELECT * FROM bombs WHERE id=? AND guild_id=?", (bomb_id, guild_id)).fetchone()
            if b is None:
                raise BombError("Esa bomb no existe.")
            if name is not None:
                name = _check_name(name)
                dup = conn.execute("SELECT 1 FROM bombs WHERE guild_id=? AND lower(name)=lower(?) AND id<>?", (guild_id, name, bomb_id)).fetchone()
                if dup:
                    raise BombError(f"Ya existe una bomb llamada **{name}**.")
            if role_id is not None:
                other = conn.execute("SELECT name FROM bombs WHERE guild_id=? AND role_id=? AND id<>?", (guild_id, role_id, bomb_id)).fetchone()
                if other:
                    raise BombError(f"Ese rol ya es el de la bomb **{other['name']}**.")
            conn.execute("UPDATE bombs SET name=?, role_id=?, caller_id=?, updated_at=? WHERE id=?",
                         (name if name is not None else b["name"], role_id if role_id is not None else b["role_id"],
                          caller_id if caller_id is not None else b["caller_id"], utcnow_iso(), bomb_id))
            return dict(conn.execute("SELECT * FROM bombs WHERE id=?", (bomb_id,)).fetchone())
    finally:
        conn.close()


def delete_bomb(db_path: str, guild_id: int, bomb_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            b = conn.execute("SELECT * FROM bombs WHERE id=? AND guild_id=?", (bomb_id, guild_id)).fetchone()
            if b:
                conn.execute("DELETE FROM bomb_members WHERE bomb_id=?", (bomb_id,))
                conn.execute("DELETE FROM bombs WHERE id=?", (bomb_id,))
            return dict(b) if b else None
    finally:
        conn.close()


def list_bombs(db_path: str, guild_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM bomb_members m WHERE m.bomb_id=b.id) AS n FROM bombs b WHERE guild_id=? ORDER BY lower(name)",
            (guild_id,))]
    finally:
        conn.close()


def find_bomb(db_path: str, guild_id: int, name: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM bombs WHERE guild_id=? AND lower(name)=lower(?)", (guild_id, clean_name(name))).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def add_member(db_path: str, bomb_id: int, user_id: int, actor_id: int) -> bool:
    """False si ya estaba en la bomb."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            try:
                conn.execute("INSERT INTO bomb_members(bomb_id,user_id,added_by,added_at) VALUES (?,?,?,?)", (bomb_id, user_id, actor_id, utcnow_iso()))
            except sqlite3.IntegrityError:
                return False
            return True
    finally:
        conn.close()


def remove_member(db_path: str, bomb_id: int, user_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT * FROM bomb_members WHERE bomb_id=? AND user_id=?", (bomb_id, user_id)).fetchone()
            if r:
                conn.execute("DELETE FROM bomb_members WHERE bomb_id=? AND user_id=?", (bomb_id, user_id))
            return dict(r) if r else None
    finally:
        conn.close()


def members(db_path: str, bomb_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM bomb_members WHERE bomb_id=? ORDER BY added_at", (bomb_id,))]
    finally:
        conn.close()
