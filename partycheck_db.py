"""/party check: lógica sin Discord. Saca los nicks de la respuesta de OCR.space y los cruza con los anotados de la CTA
(cta_signups → albion_links.char_name, más sus alters). La API key se guarda en partycheck_settings (/party apikey)."""
import re
from typing import Dict, List, Optional, Tuple

from dbutil import connect, utcnow_iso, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS partycheck_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);
"""
API_KEY = "ocrspace_api_key"

NICK_RE = re.compile(r"^[A-Za-z0-9]{3,16}$")              # nicks de Albion: alfanuméricos, 3 a 16
UI_WORDS = {                                             # textos de la ventana "Region Access Priority" y similares
    "region", "access", "priority", "party", "member", "members", "first", "this", "setting", "determines", "who",
    "gets", "preferred", "overcrowded", "regions", "within", "alliance", "guild", "partys",
}
HEADER_WORD = "member"                                   # "Party Member Priority": los nicks están debajo
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s"})


def norm(name: str) -> str:
    """Minúsculas y caracteres que el OCR confunde unificados (I/l/1, 0/O)."""
    return (name or "").strip().lower().translate(_CONFUSABLE)


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_api_key(db_path: str) -> Optional[str]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT value FROM partycheck_settings WHERE key=?", (API_KEY,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_api_key(db_path: str, value: str, actor: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO partycheck_settings(key, value, updated_by, updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
                         "updated_at=excluded.updated_at", (API_KEY, value, actor, utcnow_iso()))
    finally:
        conn.close()


def ocrspace_words(resp: dict) -> List[Tuple[str, float]]:
    """Palabras de la respuesta de OCR.space (isOverlayRequired=true) con el centro vertical de cada una."""
    out = []
    for pr in (resp or {}).get("ParsedResults") or []:
        for line in ((pr.get("TextOverlay") or {}).get("Lines") or []):
            for w in line.get("Words") or []:
                out.append((str(w.get("WordText") or ""), float(w.get("Top") or 0) + float(w.get("Height") or 0) / 2))
    return out


def extract_names(words: List[Tuple[str, float]]) -> List[str]:
    """Nicks entre las palabras leídas [(texto, y)]. Si encuentra el encabezado "Party Member Priority", se queda
    solo con lo que está debajo."""
    header_y = None
    for t, y in words:
        if t.strip().lower() == HEADER_WORD:
            header_y = y if header_y is None else min(header_y, y)
    out, seen = [], set()
    for t, y in words:
        t = t.strip()
        if not NICK_RE.match(t) or t.lower() in UI_WORDS:
            continue
        if header_y is not None and y <= header_y + 2:
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def lev(a: str, b: str, cap: int = 2) -> int:
    """Distancia de Levenshtein (corta cuando pasa de cap)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def same(a: str, b: str) -> bool:
    """Mismo nick tolerando 1 error de lectura."""
    na, nb = norm(a), norm(b)
    return na == nb or (min(len(na), len(nb)) >= 4 and lev(na, nb, 1) <= 1)


def cta_for_channel(db_path: str, channel_id: int, cta_id: Optional[int] = None) -> Optional[dict]:
    """La CTA indicada o la del hilo donde se usa el comando."""
    conn = connect(db_path)
    try:
        if cta_id:
            r = conn.execute("SELECT * FROM cta_runs WHERE id=?", (cta_id,)).fetchone()
        else:
            r = conn.execute("SELECT * FROM cta_runs WHERE thread_id=? ORDER BY id DESC LIMIT 1", (channel_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def signups(db_path: str, cta_id: int) -> List[dict]:
    """Anotados de la CTA: [{user_id, party_no, char_name (o None), names: [main + alters]}]."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT s.user_id, s.slot_key, l.char_name FROM cta_signups s "
                            "LEFT JOIN albion_links l ON l.discord_id = s.user_id WHERE s.cta_id=?", (cta_id,)).fetchall()
        alters: Dict[int, List[str]] = {}
        try:
            for a in conn.execute("SELECT user_id, alter_name FROM alters").fetchall():
                alters.setdefault(int(a["user_id"]), []).append(a["alter_name"])
        except Exception:
            pass                                         # sin tabla de alters: solo el personaje principal
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            party = int(str(r["slot_key"]).split(":", 1)[0])
        except ValueError:
            party = 1
        uid = int(r["user_id"])
        names = ([r["char_name"]] if r["char_name"] else []) + alters.get(uid, [])
        out.append({"user_id": uid, "party_no": party, "char_name": r["char_name"], "names": names})
    return out


def _match(signup: dict, ocr: List[str]) -> Optional[str]:
    for n in signup["names"]:
        for o in ocr:
            if same(n, o):
                return o
    return None


def compare(ocr: List[str], rows: List[dict], party_no: Optional[int] = None) -> dict:
    """Cruza los nicks leídos con los anotados. Sin party_no elige la party de la CTA con más coincidencias.
    Devuelve {party_no, ok, missing, unlinked, extra}; extra = [(nick, party donde está anotado o None)]."""
    parties = sorted({r["party_no"] for r in rows}) or [1]
    if party_no is None:
        party_no = max(parties, key=lambda p: (sum(1 for r in rows if r["party_no"] == p and _match(r, ocr)), -p))
    ok, missing, unlinked, used = [], [], [], set()
    for r in rows:
        if r["party_no"] != party_no:
            continue
        if not r["names"]:
            unlinked.append(r)
            continue
        m = _match(r, ocr)
        if m:
            ok.append((r, m))
            used.add(m)
        else:
            missing.append(r)
    extra: List[Tuple[str, Optional[int]]] = []
    for o in ocr:
        if o in used:
            continue
        other = next((r for r in rows if r["party_no"] != party_no and any(same(n, o) for n in r["names"])), None)
        extra.append((o, other["party_no"] if other else None))
    return {"party_no": party_no, "ok": ok, "missing": missing, "unlinked": unlinked, "extra": extra}
