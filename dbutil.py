"""Utilidades SQLite compartidas por los módulos nuevos (economía y CTA).

- Una conexión corta por operación (seguro con hilos / asyncio.to_thread).
- Modo WAL + busy_timeout para que varias escrituras simultáneas se encolen en lugar de fallar.
- Las escrituras que deben ser atómicas usan BEGIN IMMEDIATE (toma el bloqueo de escritura de entrada,
  así dos operaciones sobre la misma cuenta/plaza nunca se pisan).
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)  # autocommit; transacciones manuales
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass  # p. ej. sistemas de archivos sin soporte de WAL: sigue funcionando con el modo por defecto
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Transacción de escritura: BEGIN IMMEDIATE ... COMMIT, o ROLLBACK si algo falla."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")
