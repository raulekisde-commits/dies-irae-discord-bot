"""Configuración del bot editable desde Discord (/config): canales que antes estaban escritos en el código.

Cada ajuste se registra con su valor por defecto (el de siempre). Si un administrador lo cambia, el valor guardado en la base
(tabla bot_settings) manda; si lo restaura, vuelve el de siempre. Se lee en el momento de usarlo, así que el cambio vale al instante.
Sin base configurada (por ejemplo en las pruebas) siempre se devuelve el valor por defecto."""
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_settings (
    key        TEXT PRIMARY KEY,
    value      INTEGER NOT NULL,
    updated_by INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_settings_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    key    TEXT NOT NULL,
    old    INTEGER,
    new    INTEGER,
    actor  INTEGER,
    at     TEXT NOT NULL
);
"""


@dataclass
class Setting:
    key: str
    label: str
    default: Union[int, Callable[[], int]]
    help: str = ""


_lock = threading.Lock()
_db: Optional[str] = None
_over: Dict[str, int] = {}
REGISTRY: Dict[str, Setting] = {}


def register(key: str, label: str, default, help: str = "") -> None:
    """Registra un ajuste de canal con su valor por defecto (un id o una función que lo devuelve)."""
    REGISTRY[key] = Setting(key, label, default, help)


def configure(db_path: str) -> None:
    """Crea las tablas y carga los valores guardados. Se llama una vez al arrancar."""
    global _db
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        rows = {r["key"]: int(r["value"]) for r in conn.execute("SELECT key, value FROM bot_settings")}
    finally:
        conn.close()
    with _lock:
        _db = db_path
        _over.clear()
        _over.update(rows)


def default_of(key: str) -> int:
    s = REGISTRY.get(key)
    if s is None:
        return 0
    return int(s.default() if callable(s.default) else s.default)


def override(key: str) -> Optional[int]:
    """El valor que puso un administrador, o None si no se tocó."""
    with _lock:
        return _over.get(key)


def get(key: str) -> int:
    """Valor vigente: el guardado por un administrador o, si no hay, el de siempre."""
    v = override(key)
    return v if v else default_of(key)


def set_value(key: str, value: int, actor_id: int) -> Tuple[int, int]:
    """Guarda un valor nuevo. Devuelve (anterior, nuevo)."""
    if key not in REGISTRY:
        raise KeyError(key)
    if _db is None:
        raise RuntimeError("botsettings sin configurar")
    old = get(key)
    conn = connect(_db)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO bot_settings(key, value, updated_by, updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (key, int(value), actor_id, utcnow_iso()))
            conn.execute("INSERT INTO bot_settings_log(key, old, new, actor, at) VALUES (?,?,?,?,?)", (key, old, int(value), actor_id, utcnow_iso()))
    finally:
        conn.close()
    with _lock:
        _over[key] = int(value)
    return old, int(value)


def reset(key: str, actor_id: int) -> Tuple[int, int]:
    """Vuelve al valor de siempre. Devuelve (anterior, nuevo)."""
    if key not in REGISTRY:
        raise KeyError(key)
    if _db is None:
        raise RuntimeError("botsettings sin configurar")
    old = get(key)
    conn = connect(_db)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
            conn.execute("INSERT INTO bot_settings_log(key, old, new, actor, at) VALUES (?,?,?,?,?)", (key, old, default_of(key), actor_id, utcnow_iso()))
    finally:
        conn.close()
    with _lock:
        _over.pop(key, None)
    return old, default_of(key)


def all_settings() -> List[Setting]:
    return list(REGISTRY.values())
