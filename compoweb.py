"""Importar composiciones de AlbionZvZ (con permiso del dueño del sitio): /compo importar y /compo actualizar.

Consulta mínima y solo cuando alguien ejecuta el comando: 1 pedido a la página de la compo + 1 por cada build distinta (con caché).
Cada posición toma el rol de Discord según el arma de la build (reglas de /compo reglas)."""
import json
import os
import re
import sqlite3
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from dbutil import connect, write_tx, utcnow_iso

SITE = "https://www.albionzvz.com"
BUILD_API = SITE + "/api/public-library/builds/{id}"
UA = "DiesIraeGuildBot/1.0 (consulta puntual autorizada por el dueño de AlbionZvZ; solo al ejecutar /compo importar|actualizar)"
ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
CACHE_SECONDS = 3600                       # una build ya consultada hace menos de 1 hora no se vuelve a pedir al importar
UPDATE_COOLDOWN = 300                      # entre /compo actualizar de la misma compo
PAUSE = 0.4                                # pausa entre pedidos de builds
PLAY_TO_LABEL = {"dtank": "tank", "tank": "tank", "healer": "healer", "support": "supp", "dps": "dps"}
LABEL_EMOJI = {"tank": "🛡️", "healer": "💚", "supp": "✨", "dps": "⚔️", "clump maker": "🌀", "pierce": "🗡️", "galatinas": "⚔️", "shaper": "🔮", "bloodletter": "🩸", "battle mount": "🐴", "bomb": "💣"}
ROLE_NAME_MAX = 40
FILL_BATTLEMOUNT_ID = "8d5f2182-aa41-451a-8565-b021beff69e5"        # la plaza «Fill — Battlemount» de la web no es una build
ICON_RE = re.compile(r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})":\{"src":"/item-icons/([A-Z0-9_]+?)__t\d(?:_e\d)?\.png"')

SCHEMA = """
CREATE TABLE IF NOT EXISTS compoweb_links (
    composition_id INTEGER PRIMARY KEY, web_id TEXT NOT NULL, url TEXT NOT NULL, last_sync_at TEXT NOT NULL, last_sync_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS compoweb_builds (
    build_id TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_ts REAL NOT NULL
);
"""


class WebError(Exception):
    pass


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def disabled() -> bool:
    """Interruptor de emergencia: ALBIONZVZ_OFF=1 apaga toda consulta al sitio."""
    return os.getenv("ALBIONZVZ_OFF", "").strip().lower() in ("1", "si", "sí", "yes", "true")


def comp_id_from(text: str) -> Optional[str]:
    m = ID_RE.search(text or "")
    return m.group(0).lower() if m else None


def _get(url: str, timeout: int = 20) -> str:
    if disabled():
        raise WebError("La consulta a AlbionZvZ está desactivada (ALBIONZVZ_OFF).")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:
        raise WebError(f"No pude consultar AlbionZvZ ({exc.__class__.__name__}: {exc})")


# ---------------------------------------------------------------- lectura de la página de la compo
def _balanced(s: str, start: int) -> str:
    depth, inside, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if inside:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                inside = False
        elif c == '"':
            inside = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise WebError("La página de la compo tiene un formato que no entiendo (JSON incompleto).")


def parse_comp_html(html: str) -> dict:
    """Devuelve {"title", "parties": [{"title", "slots": [[build_id, ...], ...]}], "builds": {id: {"name", "playstyle"}}}."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    if not chunks:
        raise WebError("No encontré los datos de la compo en la página (¿cambió el sitio?).")
    try:
        text = "".join(json.loads('"' + c + '"') for c in chunks)
    except ValueError:
        raise WebError("No pude leer los datos de la página de la compo.")
    k = text.find('"roleOptions"')
    a = text.find('"layout":{')
    if k < 0 or a < 0:
        raise WebError("Esa página no parece ser una compo (no tiene grupos ni builds).")
    layout = json.loads(_balanced(text, a + len('"layout":')))
    options = json.loads(_balanced(text, text.find("[", k)))
    t = re.search(r'public-library-detail_title[^"]*","children":"((?:[^"\\]|\\.)*)"', text)
    parties = [{"title": p.get("title") or f"Party {i}", "slots": [list(s) for s in p.get("slots", [])]} for i, p in enumerate(layout.get("parties", []), start=1)]
    if not parties:
        raise WebError("La compo no tiene grupos.")
    weapons = {m.group(1): m.group(2) for m in ICON_RE.finditer(text)}              # id de build -> arma principal (viene en la propia página)
    return {"title": json.loads('"' + t.group(1) + '"') if t else "", "parties": parties, "weapons": weapons,
            "builds": {o["id"]: {"name": o.get("name") or "Build", "playstyle": (o.get("playstyle") or "").lower()} for o in options}}


def fetch_comp(web_id: str) -> dict:
    return parse_comp_html(_get(f"{SITE}/comps/{web_id}"))


# ---------------------------------------------------------------- builds (con caché)
def cached_build(db_path: str, build_id: str, max_age: float = CACHE_SECONDS) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT json, fetched_ts FROM compoweb_builds WHERE build_id=?", (build_id,)).fetchone()
        if r and time.time() - float(r["fetched_ts"]) <= max_age:
            return json.loads(r["json"])
        return None
    finally:
        conn.close()


def fetch_build(db_path: str, build_id: str, force: bool = False) -> dict:
    if not force:
        c = cached_build(db_path, build_id)
        if c is not None:
            return c
    data = json.loads(_get(BUILD_API.format(id=build_id)))
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO compoweb_builds(build_id,json,fetched_ts) VALUES (?,?,?) ON CONFLICT(build_id) DO UPDATE SET json=excluded.json, fetched_ts=excluded.fetched_ts",
                     (build_id, json.dumps(data), time.time()))
    finally:
        conn.close()
    return data


def fetch_builds(db_path: str, ids: List[str], force: bool = False) -> Tuple[Dict[str, dict], List[str]]:
    """Pide cada build una sola vez (las que están en caché no se piden). Devuelve ({id: data}, [ids que fallaron])."""
    out, failed, went_online = {}, [], False
    for bid in dict.fromkeys(ids):
        try:
            if force or cached_build(db_path, bid) is None:
                if went_online:
                    time.sleep(PAUSE)                         # espaciar los pedidos al sitio
                went_online = True
            out[bid] = fetch_build(db_path, bid, force=force)
        except Exception:
            failed.append(bid)
    return out, failed


def weapon_of(build: Optional[dict]) -> str:
    b = (build or {}).get("build") or build or {}
    draft = b.get("draft") or b
    mh = draft.get("mainHand") or {}
    return str(mh.get("itemId") or "")


# ---------------------------------------------------------------- de la web a posiciones del bot
def build_slot_defs(parsed: dict, builds: Dict[str, dict], role_for, discord_role_for, name_for=None) -> Tuple[List[dict], dict]:
    """Arma las posiciones según el ARMA real de cada build (no según el nombre que le puso quien armó la compo).
    role_for(weapon_id) -> rol de combate o None · discord_role_for(label) -> id de rol de Discord o None · name_for(weapon_id) -> nombre del arma.
    Devuelve (posiciones, avisos) con avisos = {"sin_regla": [arma], "sin_discord": [rol], "sin_datos": [build]}."""
    notes = {"sin_regla": set(), "sin_discord": set(), "sin_datos": set()}
    weapons = parsed.get("weapons", {})
    defs: List[dict] = []
    for pn, party in enumerate(parsed["parties"], start=1):
        for pos, slot in enumerate(party["slots"], start=1):
            names, role_ids, label_seen = [], [], ""
            for bid in slot or []:
                if bid == FILL_BATTLEMOUNT_ID:
                    names.append("Battlemount")
                    label = "battle mount"
                else:
                    meta = parsed["builds"].get(bid, {"name": "Build", "playstyle": ""})
                    weapon = weapons.get(bid) or weapon_of(builds.get(bid))
                    label = ""
                    if weapon:
                        names.append((name_for(weapon) if name_for else None) or meta["name"])
                        label = role_for(weapon) or ""
                        if not label:
                            notes["sin_regla"].add(weapon)
                    else:
                        names.append(meta["name"])
                        notes["sin_datos"].add(meta["name"])
                rid = discord_role_for(label) if label else None
                if label and not rid:
                    notes["sin_discord"].add(label)
                if rid and rid not in role_ids:
                    role_ids.append(rid)
                label_seen = label_seen or label
            name = (" / ".join(dict.fromkeys(names)) or "Libre")[:ROLE_NAME_MAX]
            defs.append({"party_no": pn, "position_no": pos, "role_name": name, "emoji": LABEL_EMOJI.get(label_seen, ""),
                         "role_ids": role_ids, "access_mode": "roles" if role_ids else "any"})
    return defs, {k: sorted(v) for k, v in notes.items()}


# ---------------------------------------------------------------- base de datos del bot
def link_for(db_path: str, composition_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM compoweb_links WHERE composition_id=?", (composition_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def save_link(db_path: str, composition_id: int, web_id: str, url: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO compoweb_links(composition_id,web_id,url,last_sync_at,last_sync_ts) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(composition_id) DO UPDATE SET web_id=excluded.web_id, url=excluded.url, last_sync_at=excluded.last_sync_at, last_sync_ts=excluded.last_sync_ts",
                     (composition_id, web_id, url, utcnow_iso(), time.time()))
    finally:
        conn.close()


def cooldown_left(link: Optional[dict]) -> int:
    if not link:
        return 0
    return max(0, int(UPDATE_COOLDOWN - (time.time() - float(link["last_sync_ts"] or 0))))


def create_composition(db_path: str, guild_id: int, name: str, created_by: int, builds_url: str, defs: List[dict], now_iso: str) -> int:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            cur = conn.execute("INSERT INTO cta_compositions(guild_id,name,created_by,updated_at,builds_url) VALUES (?,?,?,?,?)", (guild_id, name, created_by, now_iso, builds_url))
            cid = int(cur.lastrowid)
            conn.executemany("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,?,?,?,'any')",
                             [(cid, d["party_no"], d["position_no"], d["role_name"], d["emoji"], d["access_mode"], json.dumps(d["role_ids"])) for d in defs])
        return cid
    finally:
        conn.close()


def diff_slots(old_slots: List[dict], defs: List[dict]) -> dict:
    """Compara las posiciones actuales (dicts de cta_slots con role_ids) con las nuevas de la web, por (party, posición)."""
    old = {(int(s["party_no"]), int(s["position_no"])): s for s in old_slots}
    new = {(d["party_no"], d["position_no"]): d for d in defs}
    changed = [k for k in new if k in old and (old[k]["role_name"] != new[k]["role_name"] or sorted(old[k].get("role_ids") or []) != sorted(new[k]["role_ids"]))]
    return {"added": sorted(k for k in new if k not in old), "removed": sorted(k for k in old if k not in new), "changed": sorted(changed),
            "same": len(new) - len([k for k in new if k not in old]) - len(changed)}


def apply_update(db_path: str, composition_id: int, defs: List[dict], now_iso: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            old = {(int(r[1]), int(r[2])): int(r[0]) for r in conn.execute("SELECT id, party_no, position_no FROM cta_slots WHERE composition_id=?", (composition_id,))}
            new_keys = {(d["party_no"], d["position_no"]) for d in defs}
            for d in defs:
                key = (d["party_no"], d["position_no"])
                if key in old:
                    conn.execute("UPDATE cta_slots SET role_name=?, emoji=?, access_mode=?, role_ids_json=?, access_match='any' WHERE id=?",
                                 (d["role_name"], d["emoji"], d["access_mode"], json.dumps(d["role_ids"]), old[key]))
                else:
                    conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,?,?,?,'any')",
                                 (composition_id, d["party_no"], d["position_no"], d["role_name"], d["emoji"], d["access_mode"], json.dumps(d["role_ids"])))
            for key, sid in old.items():
                if key not in new_keys:
                    conn.execute("DELETE FROM cta_slots WHERE id=?", (sid,))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (now_iso, composition_id))
    finally:
        conn.close()
