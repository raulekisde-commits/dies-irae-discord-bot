"""/party check: lógica sin Discord. Saca los nicks de la respuesta de Google Cloud Vision (TEXT_DETECTION) y los
cruza con los anotados de la CTA (cta_signups → albion_links.char_name, más sus alters)."""
import re
from typing import Dict, List, Optional, Tuple

from dbutil import connect

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


def _center_y(ann: dict) -> float:
    vs = (ann.get("boundingPoly") or {}).get("vertices") or []
    ys = [v.get("y", 0) for v in vs]
    return sum(ys) / len(ys) if ys else 0.0


def extract_names(vision: dict) -> List[str]:
    """Nicks de la respuesta de Vision. Usa las palabras sueltas (textAnnotations[1:]); si encuentra el encabezado
    "Party Member Priority", se queda solo con lo que está debajo."""
    words = (vision or {}).get("textAnnotations") or []
    words = words[1:]                                    # [0] es el texto completo
    header_y = None
    for w in words:
        if (w.get("description") or "").strip().lower() == HEADER_WORD:
            y = _center_y(w)
            header_y = y if header_y is None else min(header_y, y)
    out, seen = [], set()
    for w in words:
        t = (w.get("description") or "").strip()
        if not NICK_RE.match(t) or t.lower() in UI_WORDS:
            continue
        if header_y is not None and _center_y(w) <= header_y + 2:
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
