"""Warnings automáticos de una CTA terminada (sin Discord):
· anotado y NO estuvo en el canal de voz (los anotados como Bomb no cuentan),
· anotado y NO aparece en el killboard (solo con killboard cargado y personaje registrado),
· estuvo en voz (el mínimo de ctavoice) sin estar anotado (los Bomb no cuentan),
· en el killboard usó un arma distinta a la de su posición (sin tier ni encantamiento; ver killboards.weapon_mismatches),
· skipper: figuraba en la lista de conectados y no se anotó ni aparece en el killboard (solo con personaje registrado).

Se guarda qué warning generó cada motivo: al recalcular (killboard cargado o borrado) no se duplica nada
y se borran los automáticos que dejaron de corresponder."""
from typing import Dict, Iterable, List, Set, Tuple

from dbutil import connect, write_tx, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS cta_auto_warnings (
    cta_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,          -- voice_missing | voice_extra | kb_missing | weapon_mismatch | skipper
    warning_id INTEGER NOT NULL,
    PRIMARY KEY (cta_id, user_id, kind)
);
"""

KINDS = ("voice_missing", "voice_extra", "kb_missing", "weapon_mismatch", "skipper")   # orden en el hilo de stats


def reason(kind: str, title: str) -> str:
    return {
        "voice_missing": f"{title}: se anotó y no estuvo en el canal de voz",
        "kb_missing": f"{title}: se anotó y no aparece en el killboard",
        "voice_extra": f"{title}: estuvo en el canal de voz sin estar anotado",
        "weapon_mismatch": f"{title}: usó un arma distinta a la de su posición",
        "skipper": f"{title}: estaba conectado y no se anotó ni aparece en el killboard",
    }[kind]


def init_schema(db_path: str) -> None:
    import warnings_sys
    warnings_sys.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def compute(positions: Iterable[dict], voice_secs: Dict[int, float], min_secs: int,
            kb_loaded: bool, chars: Dict[int, Set[str]], kb_names: Iterable[str],
            weapon_wrong: Iterable[int] = (), skippers: Iterable[int] = ()) -> Dict[str, Set[int]]:
    """skippers: user_id de los nombres de kb_skips (ya calculados por killboard_db.recompute_skips)."""
    """positions: filas de cta_positions (slot_key 'bomb' = anotado como Bomb). chars: {user_id: {name_norm}}."""
    signed = {int(p["user_id"]) for p in positions}
    bombs = {int(p["user_id"]) for p in positions if p.get("slot_key") == "bomb"}
    out = {k: set() for k in KINDS}
    if voice_secs:                                    # sin datos de voz (no hubo seguimiento) no se compara
        voice = {u for u, s in voice_secs.items() if s >= min_secs}
        out["voice_missing"] = (signed - bombs) - voice
        out["voice_extra"] = voice - signed - bombs
    if kb_loaded:
        names = set(kb_names)
        out["kb_missing"] = {u for u in signed if chars.get(u) and not (chars[u] & names)}
        out["weapon_mismatch"] = set(int(u) for u in weapon_wrong) & signed
        in_kb = {u for u, cs in chars.items() if cs & names}          # un alt conectado no cuenta si el main peleó
        out["skipper"] = {int(u) for u in skippers} - signed - in_kb
    return out


def current(db_path: str, cta_id: int) -> Dict[str, List[int]]:
    """Warnings automáticos vigentes de la CTA: {kind: [user_id]} (solo los kinds con gente)."""
    conn = connect(db_path)
    try:
        out: Dict[str, List[int]] = {}
        for r in conn.execute("SELECT kind, user_id FROM cta_auto_warnings WHERE cta_id=? ORDER BY user_id", (cta_id,)):
            out.setdefault(r["kind"], []).append(int(r["user_id"]))
        return out
    finally:
        conn.close()


def reconcile(db_path: str, guild_id: int, cta_id: int, desired: Dict[str, Set[int]], title: str,
              issued_by: int) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """Deja los warnings automáticos de la CTA iguales a `desired`. Devuelve (agregados, quitados) como [(kind, user_id)]."""
    added, removed = [], []
    conn = connect(db_path)
    try:
        with write_tx(conn):
            have = {(r["kind"], int(r["user_id"])): int(r["warning_id"])
                    for r in conn.execute("SELECT kind, user_id, warning_id FROM cta_auto_warnings WHERE cta_id=?", (cta_id,))}
            want = {(k, u) for k, users in desired.items() for u in users}
            for key, wid in have.items():
                if key not in want:
                    conn.execute("DELETE FROM warnings WHERE id=?", (wid,))
                    conn.execute("DELETE FROM cta_auto_warnings WHERE cta_id=? AND kind=? AND user_id=?", (cta_id, key[0], key[1]))
                    removed.append(key)
            for kind, user in sorted(want - set(have)):
                cur = conn.execute("INSERT INTO warnings(guild_id, user_id, reason, issued_by, created_at) VALUES (?,?,?,?,?)",
                                   (guild_id, user, reason(kind, title), issued_by, utcnow_iso()))
                conn.execute("INSERT INTO cta_auto_warnings(cta_id, user_id, kind, warning_id) VALUES (?,?,?,?)",
                             (cta_id, user, kind, int(cur.lastrowid)))
                added.append((kind, user))
    finally:
        conn.close()
    return added, removed
