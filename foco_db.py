"""Foco donado: se suma al cerrar como exitoso un ticket de Foco Donor (bot.py, FocoCloseModal) y se muestra en /stats jugador."""
import re
from typing import List, Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS foco_donations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,                 -- quien donó
    amount     INTEGER NOT NULL,                 -- foco donado (lo confirma quien cierra el ticket)
    channel_id INTEGER NOT NULL UNIQUE,          -- ticket: evita contar dos veces el mismo cierre
    closed_by  INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_foco_donations_user ON foco_donations(user_id);
"""
MAX_AMOUNT = 100_000_000
_NUM = re.compile(r"^(\d+(?:[.,]\d+)*)\s*([km]?)$")


def parse_amount(text: Optional[str]) -> Optional[int]:
    """'30000', '30.000', '30,000', '30k', '1.5k', '1,2m' → entero. None si no se entiende."""
    s = (text or "").strip().lower().replace(" ", "")
    m = _NUM.match(s)
    if not m:
        return None
    num, suf = m.groups()
    if suf:
        try:
            return int(round(float(num.replace(",", ".")) * (1_000 if suf == "k" else 1_000_000)))
        except ValueError:
            return None
    return int(num.replace(".", "").replace(",", ""))


def fmt_short(n: int) -> str:
    """150000 → '150k', 1500000 → '1.5m', 2000000000 → '2b' (un decimal, sin '.0')."""
    n = int(n or 0)
    for div, suf in ((1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")):
        if abs(n) >= div:
            v = f"{n / div:.1f}".rstrip("0").rstrip(".")
            return v + suf
    return str(n)


def _conn(db_path: str):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record(db_path: str, user_id: int, amount: int, channel_id: int, closed_by: int) -> bool:
    """Suma una donación. False si ese ticket ya estaba registrado."""
    conn = _conn(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT OR IGNORE INTO foco_donations(user_id, amount, channel_id, closed_by, created_at) "
                               "VALUES (?,?,?,?,?)", (user_id, amount, channel_id, closed_by, utcnow_iso()))
            return cur.rowcount > 0
    finally:
        conn.close()


def user_total(db_path: str, user_id: int) -> Tuple[int, int]:
    """(foco donado en total, cantidad de donaciones)."""
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT COALESCE(SUM(amount), 0) AS t, COUNT(*) AS n FROM foco_donations WHERE user_id=?",
                         (user_id,)).fetchone()
        return int(r["t"]), int(r["n"])
    finally:
        conn.close()


def top(db_path: str) -> List[Tuple[int, int, int]]:
    """[(user_id, foco donado, donaciones)] de mayor a menor."""
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT user_id, SUM(amount) AS t, COUNT(*) AS n FROM foco_donations GROUP BY user_id "
                            "HAVING SUM(amount) > 0 ORDER BY t DESC, n DESC").fetchall()
        return [(int(r["user_id"]), int(r["t"]), int(r["n"])) for r in rows]
    finally:
        conn.close()
