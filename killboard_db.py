"""Killboards cargados a mano por el staff: parseo de los eventos de AlbionBB y estadísticas acumuladas.

Todo sale de aquí (no de la web de AlbionBB): solo cuentan las batallas que alguien autorizado cargó."""
import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from dbutil import connect, write_tx, utcnow_iso

FIRST_DEATHS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_battles (
    battle_id   TEXT PRIMARY KEY,
    cta_id      INTEGER,                       -- NULL = killboard suelto (cuenta para las stats, no para roles)
    loaded_by   INTEGER NOT NULL,
    loaded_at   TEXT NOT NULL,
    events      INTEGER NOT NULL DEFAULT 0,
    smogg_deaths INTEGER NOT NULL DEFAULT 0,
    ts_missing  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kb_players (
    battle_id  TEXT NOT NULL,
    name_norm  TEXT NOT NULL,
    name       TEXT NOT NULL,
    kills      INTEGER NOT NULL DEFAULT 0,
    deaths     INTEGER NOT NULL DEFAULT 0,
    ip_avg     REAL,
    ip_max     REAL,
    ip_min     REAL,
    first5     INTEGER NOT NULL DEFAULT 0,
    death_rank INTEGER,
    death_time TEXT,
    PRIMARY KEY (battle_id, name_norm),
    FOREIGN KEY (battle_id) REFERENCES kb_battles(battle_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kb_players_name ON kb_players(name_norm);
CREATE TABLE IF NOT EXISTS kb_online (          -- lista de conectados de una CTA (los que figuraban "Online")
    cta_id    INTEGER NOT NULL,
    name_norm TEXT NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (cta_id, name_norm)
);
CREATE TABLE IF NOT EXISTS kb_skips (            -- conectados que no estaban ni en el killboard ni anotados en la composición
    cta_id    INTEGER NOT NULL,
    name_norm TEXT NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (cta_id, name_norm)
);
CREATE INDEX IF NOT EXISTS idx_kb_skips_name ON kb_skips(name_norm);
CREATE TABLE IF NOT EXISTS kb_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    battle_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    details TEXT
);
"""


def norm(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


# ---------- parseo ----------
def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _ts(text) -> Optional[datetime]:
    if not text or not isinstance(text, str):
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_events(events: Iterable[dict], guild_name: str) -> dict:
    """Devuelve {'players': {norm: {...}}, 'events': n, 'smogg_deaths': n, 'ts_missing': n}. Solo jugadores de la guild."""
    gl = norm(guild_name)
    players: Dict[str, dict] = {}

    def entry(p) -> Optional[dict]:
        if not isinstance(p, dict):
            return None
        name = p.get("Name") or p.get("name")
        guild = p.get("GuildName") or p.get("guildName")
        if not name or norm(guild) != gl:
            return None
        e = players.setdefault(norm(name), {"name": str(name), "kills": 0, "deaths": 0, "ips": [], "death_time": None, "deaths_list": []})
        return e

    deaths: List[Tuple[Optional[datetime], int, str]] = []
    n_events = 0
    for idx, ev in enumerate(events or []):
        if not isinstance(ev, dict):
            continue
        n_events += 1
        seen_ip = set()

        def note_ip(p, e):
            if e is None or id(e) in seen_ip:
                return
            ip = _num((p or {}).get("AverageItemPower") if isinstance(p, dict) else None)
            if ip:
                e["ips"].append(ip)
            seen_ip.add(id(e))

        k, v = ev.get("Killer"), ev.get("Victim")
        ke, ve = entry(k), entry(v)
        if ke is not None:
            ke["kills"] += 1
            note_ip(k, ke)
        if ve is not None:
            ve["deaths"] += 1
            note_ip(v, ve)
            deaths.append((_ts(ev.get("TimeStamp") or ev.get("timeStamp")), idx, norm(v.get("Name") or v.get("name"))))
        for key in ("Participants", "GroupMembers"):
            for p in ev.get(key) or []:
                e = entry(p)
                note_ip(p, e)
    ts_missing = sum(1 for d in deaths if d[0] is None)
    deaths.sort(key=lambda d: (d[0] is None, d[0] or datetime.max.replace(tzinfo=timezone.utc), d[1]))
    first, rank = [], 0
    for ts, _i, nm in deaths:
        e = players.get(nm)
        if e is None:
            continue
        if e["death_time"] is None:
            rank += 1
            e["death_rank"] = rank
            e["death_time"] = ts.isoformat() if ts else None
            if rank <= FIRST_DEATHS:
                first.append(nm)
    for nm, e in players.items():
        e["first5"] = nm in first
        e.setdefault("death_rank", None)
        ips = e.pop("ips")
        e["ip_avg"] = sum(ips) / len(ips) if ips else None
        e["ip_max"] = max(ips) if ips else None
        e["ip_min"] = min(ips) if ips else None
        e.pop("deaths_list", None)
    return {"players": players, "events": n_events, "smogg_deaths": len(deaths), "ts_missing": ts_missing}


def add_attendees(parsed: dict, names: Iterable[str]) -> None:
    """Suma a la asistencia a quienes la ficha de la batalla lista como de la guild aunque no aparezcan en ningún evento."""
    for n in names:
        parsed["players"].setdefault(norm(n), {"name": str(n), "kills": 0, "deaths": 0, "ip_avg": None, "ip_max": None, "ip_min": None,
                                               "first5": False, "death_rank": None, "death_time": None})


# ---------- guardado ----------
def save_battle(db_path: str, battle_id: str, cta_id: Optional[int], loaded_by: int, parsed: dict) -> str:
    """'new' | 'dup' (ya estaba cargado: no se toca nada)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM kb_battles WHERE battle_id=?", (battle_id,)).fetchone():
                return "dup"
            conn.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at, events, smogg_deaths, ts_missing) VALUES (?,?,?,?,?,?,?)",
                         (battle_id, cta_id, loaded_by, utcnow_iso(), parsed["events"], parsed["smogg_deaths"], parsed["ts_missing"]))
            for nm, e in parsed["players"].items():
                conn.execute("INSERT INTO kb_players(battle_id,name_norm,name,kills,deaths,ip_avg,ip_max,ip_min,first5,death_rank,death_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (battle_id, nm, e["name"], e["kills"], e["deaths"], e["ip_avg"], e["ip_max"], e["ip_min"], int(bool(e["first5"])),
                              e.get("death_rank"), e.get("death_time")))
            conn.execute("INSERT INTO kb_audit(battle_id, action, actor_id, at, details) VALUES (?,?,?,?,?)",
                         (battle_id, "load", loaded_by, utcnow_iso(), json.dumps({"cta_id": cta_id, "players": len(parsed["players"])})))
            return "new"
    finally:
        conn.close()


def delete_battle(db_path: str, battle_id: str, actor_id: int) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT cta_id FROM kb_battles WHERE battle_id=?", (battle_id,)).fetchone()
            if r is None:
                return False
            n = conn.execute("SELECT COUNT(*) FROM kb_players WHERE battle_id=?", (battle_id,)).fetchone()[0]
            conn.execute("DELETE FROM kb_players WHERE battle_id=?", (battle_id,))
            conn.execute("DELETE FROM kb_battles WHERE battle_id=?", (battle_id,))
            conn.execute("INSERT INTO kb_audit(battle_id, action, actor_id, at, details) VALUES (?,?,?,?,?)",
                         (battle_id, "delete", actor_id, utcnow_iso(), json.dumps({"cta_id": r["cta_id"], "players": n})))
            return True
    finally:
        conn.close()


def battles_of_cta(db_path: str, cta_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM kb_battles WHERE cta_id=? ORDER BY loaded_at", (cta_id,))]
    finally:
        conn.close()


def names_in_cta(db_path: str, cta_id: int) -> Dict[str, str]:
    """{name_norm: name} de los jugadores de la guild presentes en los killboards de esa CTA."""
    conn = connect(db_path)
    try:
        return {r["name_norm"]: r["name"] for r in conn.execute(
            "SELECT p.name_norm, p.name FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id WHERE b.cta_id=?", (cta_id,))}
    finally:
        conn.close()


# ---------- estadísticas ----------
def _aggregate(rows) -> dict:
    ips_avg = [r["ip_avg"] for r in rows if r["ip_avg"]]
    ips_max = [r["ip_max"] for r in rows if r["ip_max"]]
    ips_min = [r["ip_min"] for r in rows if r["ip_min"]]
    att = len(rows)
    first5 = sum(r["first5"] for r in rows)
    return {"attendance": att, "kills": sum(r["kills"] for r in rows), "deaths": sum(r["deaths"] for r in rows), "first5": first5,
            "first5_pct": (100.0 * first5 / att) if att else 0.0,
            "avg_ip": (sum(ips_avg) / len(ips_avg)) if ips_avg else None,
            "max_ip": max(ips_max) if ips_max else None, "min_ip": min(ips_min) if ips_min else None}


def player_stats(db_path: str, names_norm: Iterable[str]) -> dict:
    names = list(names_norm)
    if not names:
        return _aggregate([])
    conn = connect(db_path)
    try:
        q = ",".join("?" * len(names))
        return _aggregate(conn.execute(f"SELECT * FROM kb_players WHERE name_norm IN ({q})", names).fetchall())
    finally:
        conn.close()


def all_player_stats(db_path: str) -> Dict[str, dict]:
    conn = connect(db_path)
    try:
        by: Dict[str, list] = {}
        names: Dict[str, str] = {}
        for r in conn.execute("SELECT * FROM kb_players"):
            by.setdefault(r["name_norm"], []).append(r)
            names[r["name_norm"]] = r["name"]
        out = {}
        for k, rows in by.items():
            d = _aggregate(rows)
            d["name"] = names[k]
            out[k] = d
        return out
    finally:
        conn.close()


TOP_KEYS = {
    "kills": ("⚔️ Top kills", "kills", True, "kills"),
    "deaths": ("💀 Top muertes", "deaths", True, "muertes"),
    "attendance": ("🛡️ Top asistencia", "attendance", True, "asistencias"),
    "first5": ("💥 Muertes en el primer engage", "first5", True, "veces"),
    "avg_ip": ("📈 Mejor IP promedio", "avg_ip", True, "IP"),
    "max_ip": ("⬆️ IP más alta registrada", "max_ip", True, "IP"),
    "min_ip": ("⬇️ IP más baja registrada", "min_ip", False, "IP"),
    "skips": ("🚫 Top skippers", "skips", True, "CTAs skipeadas"),
}


def top(db_path: str, key: str, size: int = 10) -> List[Tuple[str, float, dict]]:
    title, field, desc, _unit = TOP_KEYS[key]
    if key == "skips":
        return skip_top(db_path, size)
    rows = []
    for d in all_player_stats(db_path).values():
        v = d[field]
        if v is None or v <= 0:
            continue
        rows.append((d["name"], v, d))
    rows.sort(key=lambda r: ((-r[1] if desc else r[1]), r[0].casefold()))
    return rows[:size]


def role_counts(db_path: str, discord_id: int, names_norm: Iterable[str]) -> Dict[int, int]:
    """Roles de juego más usados: en cuántas CTA estuvo anotado con cada uno, contando solo las CTA donde también está en el killboard."""
    names = list(names_norm)
    if not names:
        return {}
    conn = connect(db_path)
    try:
        q = ",".join("?" * len(names))
        rows = conn.execute(f"""SELECT pos.role_id AS role_id, COUNT(DISTINCT pos.cta_id) AS n
                                FROM cta_positions pos
                                WHERE pos.user_id=? AND pos.role_id IS NOT NULL AND pos.cta_id IN (
                                    SELECT b.cta_id FROM kb_battles b JOIN kb_players p ON p.battle_id=b.battle_id
                                    WHERE b.cta_id IS NOT NULL AND p.name_norm IN ({q}))
                                GROUP BY pos.role_id""", [discord_id] + names).fetchall()
        return {int(r["role_id"]): int(r["n"]) for r in rows}
    finally:
        conn.close()


# ---------- conectados y skippers ----------
def parse_online_file(raw: bytes) -> Tuple[List[str], int]:
    """Lee el bloc de notas del juego: "Character Name"<TAB>"Last Seen"<TAB>"Roles".
    Devuelve (nombres que figuran como Online, total de filas de jugadores)."""
    import csv, io
    text = None
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return [], 0
    online, total = [], 0
    for row in csv.reader(io.StringIO(text), delimiter="\t", quotechar='"'):
        if len(row) < 2 or not row[0].strip():
            continue
        if row[0].strip().casefold() == "character name":
            continue
        total += 1
        if row[1].strip().casefold() == "online":
            online.append(row[0].strip())
    return online, total


def save_online(db_path: str, cta_id: int, names: Iterable[str]) -> int:
    """Reemplaza la lista de conectados de la CTA (cargarla de nuevo corrige la anterior)."""
    names = list(dict.fromkeys(n for n in names if n))
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM kb_online WHERE cta_id=?", (cta_id,))
            for n in names:
                conn.execute("INSERT OR IGNORE INTO kb_online(cta_id, name_norm, name) VALUES (?,?,?)", (cta_id, norm(n), n))
        return len(names)
    finally:
        conn.close()


def online_of_cta(db_path: str, cta_id: int) -> Dict[str, str]:
    conn = connect(db_path)
    try:
        return {r["name_norm"]: r["name"] for r in conn.execute("SELECT name_norm, name FROM kb_online WHERE cta_id=?", (cta_id,))}
    finally:
        conn.close()


def recompute_skips(db_path: str, cta_id: int, in_compo_names: Iterable[str]) -> List[str]:
    """Skippers de la CTA = conectados − killboard − anotados en la composición (por nombre de personaje).
    Solo se calcula si hay lista de conectados Y al menos un killboard cargado; si no, no hay skippers."""
    in_compo = {norm(n) for n in in_compo_names}
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM kb_skips WHERE cta_id=?", (cta_id,))
            online = {r["name_norm"]: r["name"] for r in conn.execute("SELECT name_norm, name FROM kb_online WHERE cta_id=?", (cta_id,))}
            has_kb = conn.execute("SELECT 1 FROM kb_battles WHERE cta_id=?", (cta_id,)).fetchone() is not None
            if not online or not has_kb:
                return []
            in_kb = {r["name_norm"] for r in conn.execute(
                "SELECT p.name_norm FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id WHERE b.cta_id=?", (cta_id,))}
            skips = {k: v for k, v in online.items() if k not in in_kb and k not in in_compo}
            for k, v in skips.items():
                conn.execute("INSERT INTO kb_skips(cta_id, name_norm, name) VALUES (?,?,?)", (cta_id, k, v))
            return sorted(skips.values(), key=str.casefold)
    finally:
        conn.close()


def players_of_cta(db_path: str, cta_id: int) -> set:
    """Nombres (normalizados) de todos los jugadores que aparecen en los killboards cargados de la CTA."""
    conn = connect(db_path)
    try:
        return {r["name_norm"] for r in conn.execute(
            "SELECT p.name_norm FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id WHERE b.cta_id=?", (cta_id,))}
    finally:
        conn.close()


def skips_of_cta(db_path: str, cta_id: int) -> List[str]:
    conn = connect(db_path)
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM kb_skips WHERE cta_id=? ORDER BY name COLLATE NOCASE", (cta_id,))]
    finally:
        conn.close()


def skip_count(db_path: str, names_norm: Iterable[str]) -> int:
    names = list(names_norm)
    if not names:
        return 0
    conn = connect(db_path)
    try:
        q = ",".join("?" * len(names))
        return int(conn.execute(f"SELECT COUNT(DISTINCT cta_id) FROM kb_skips WHERE name_norm IN ({q})", names).fetchone()[0])
    finally:
        conn.close()


def skip_top(db_path: str, size: int = 10) -> List[Tuple[str, float, dict]]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT name_norm, MAX(name) AS name, COUNT(DISTINCT cta_id) AS n FROM kb_skips GROUP BY name_norm "
                            "ORDER BY n DESC, name COLLATE NOCASE LIMIT ?", (size,)).fetchall()
        return [(r["name"], float(r["n"]), {}) for r in rows]
    finally:
        conn.close()
