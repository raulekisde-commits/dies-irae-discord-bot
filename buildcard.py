"""Imagen de una build de AlbionZvZ: equipo con íconos (misma disposición que la web), habilidades, alternativas, comida y poción.
Necesita Pillow. Los íconos se bajan del render oficial de Albion una sola vez y quedan guardados en disco."""
import io
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RENDER = "https://render.albiononline.com/v1"
UA = {"User-Agent": "SmoggGuildBot/1.0"}
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_iconos")

W, M = 1280, 36
BG, PANEL, BORDER = (13, 13, 16), (24, 25, 30), (52, 54, 64)
TEXT, MUTED, DIM = (240, 241, 245), (150, 155, 168), (96, 100, 112)
GOLD, RED, GREEN, BLUE = (234, 187, 62), (214, 62, 58), (66, 184, 108), (92, 150, 232)
SLOT_BG = (36, 38, 46)
LABELS = {"head": "CABEZA", "cape": "CAPA", "mainHand": "MANO PRINCIPAL", "chest": "PECHO", "offHand": "FUERA DE MANO",
          "potion": "POCIÓN", "shoes": "ZAPATOS", "food": "ALIMENTO", "mount": "MONTURA"}
GRID = [[None, "head", "cape"], ["mainHand", "chest", "offHand"], ["potion", "shoes", "food"], [None, "mount", None]]
DETAIL_ORDER = ["mainHand", "offHand", "head", "chest", "shoes", "cape", "mount", "food", "potion"]
PLAY = {"dtank": ("TANQUE", BLUE), "tank": ("TANQUE", BLUE), "healer": ("HEALER", GREEN), "dps": ("DPS", RED), "support": ("SOPORTE", GOLD)}
FONTS = {False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"],
         True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]}
_fonts: dict = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _fonts:
        f = None
        for path in FONTS[bold]:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
        if f is None:
            try:
                f = ImageFont.load_default(size)          # Pillow >= 10.1: fuente escalable incluida
            except Exception:
                f = ImageFont.load_default()
        _fonts[key] = f
    return _fonts[key]


def to_type(it: dict) -> str:
    """itemId de AlbionZvZ -> id del render de Albion (T8_MAIN_MACE, T7_MEAL_X@2)."""
    import re
    iid = it.get("itemId") or ""
    tier = (it.get("tier") or "").upper()
    m = re.match(r"^(.*)__T(\d)$", iid)
    if m:
        iid, tier = m.group(1), f"T{m.group(2)}"
    return f"{tier or 'T8'}_{iid}" + (f"@{it['enchantment']}" if it.get("enchantment") else "")


def _download(url: str):
    last = None
    for u in (url, url.split("?")[0]):                         # con tamaño y, si no, sin parámetros
        try:
            req = urllib.request.Request(u, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if len(data) > 300:
                return data
        except Exception as exc:
            last = exc
    print(f"[buildcard] no pude bajar {url}: {last!r}")
    return None


def _save(path: str, data: bytes) -> bool:
    try:
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception as exc:
        print(f"[buildcard] no pude guardar {path}: {exc!r}")
        return False


def _variants(item_type: str):
    base, _, ench = item_type.partition("@")
    out = [item_type] + ([base] if ench else [])
    parts = base.split("_", 1)
    if len(parts) == 2 and parts[0][:1] == "T" and parts[0][1:].isdigit():
        out += [f"T{t}_{parts[1]}" for t in (8, 7, 6, 5, 4) if f"T{t}_{parts[1]}" not in out]
    return out


def item_icon(item_type: str):
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
    except Exception:
        pass
    path = os.path.join(ICON_DIR, item_type.replace("@", "_e") + ".png")
    if os.path.exists(path):
        return path
    for v in _variants(item_type):
        data = _download(f"{RENDER}/item/{v}.png?size=217")
        if data and _save(path, data):
            return path
    return None


def spell_icon(spell_id: str):
    if not spell_id:
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    path = os.path.join(ICON_DIR, f"spell_{spell_id}.png")
    if os.path.exists(path):
        return path
    data = _download(f"{RENDER}/spell/{spell_id}.png?size=64")
    if data and _save(path, data):
        return path
    return None


def prefetch(draft: dict) -> None:
    """Baja en paralelo los íconos que faltan (así la primera imagen no tarda minutos)."""
    items = [draft[k] for k in DETAIL_ORDER if draft.get(k)]
    items += [s for it in list(items) for s in (it.get("swaps") or [])]
    jobs = {("i", to_type(it)) for it in items}
    jobs |= {("s", a.get("abilityId")) for it in items for a in (it.get("abilities") or []) if a.get("abilityId")}
    def one(j):
        try:
            return item_icon(j[1]) if j[0] == "i" else spell_icon(j[1])
        except Exception as exc:
            print(f"[buildcard] ícono {j}: {exc!r}")
            return None
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, jobs))


def card(img, box, radius=16, outline=BORDER, fill=PANEL, width=2):
    x0, y0, x1, y1 = box
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((x0 + 3, y0 + 8, x1 + 3, y1 + 8), radius=radius, fill=(0, 0, 0, 150))
    sh = sh.filter(ImageFilter.GaussianBlur(9))
    img.paste(sh, (0, 0), sh)
    ImageDraw.Draw(img).rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def put(img, box, x, y):
    img.paste(box, (x, y), box)


def icon_box(item_type: str, size: int):
    box = Image.new("RGBA", (size, size), SLOT_BG + (255,))
    path = item_icon(item_type)
    if path:
        try:
            box.alpha_composite(Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS))
            return box
        except Exception:
            pass
    d = ImageDraw.Draw(box)                                 # sin ícono: cuadro con el nombre corto
    tier, _, rest = item_type.partition("_")
    d.text((5, 4), tier, font=font(16 if size >= 70 else 11, True), fill=GOLD)
    for i, w in enumerate(rest.split("@")[0].replace("_", " ").title().split()[:3]):
        d.text((5, 24 + i * 15), w[:11], font=font(12 if size >= 70 else 9), fill=MUTED)
    return box


def spell_circle(ab: dict, size: int):
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    path = spell_icon(ab.get("abilityId") or "")
    ok = False
    if path:
        try:
            base.alpha_composite(Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS))
            ok = True
        except Exception:
            ok = False
    if not ok:
        d = ImageDraw.Draw(base)
        d.ellipse((0, 0, size - 1, size - 1), fill=(38, 40, 52), outline=BORDER, width=2)
        t = (ab.get("slot") or "?")[:1].upper()
        d.text((size / 2, size / 2), t if t.isalpha() else "•", font=font(max(10, size // 2), True), fill=MUTED, anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    ImageDraw.Draw(out).ellipse((0, 0, size - 1, size - 1), outline=(120, 124, 140), width=2)
    return out


def tier_chip(d, x, y, text, size=15):
    w = int(d.textlength(text, font=font(size, True))) + 18
    d.rounded_rectangle((x, y, x + w, y + size + 10), radius=8, fill=(40, 42, 54))
    d.text((x + w / 2, y + (size + 10) / 2), text, font=font(size, True), fill=MUTED, anchor="mm")
    return w


def render(data: dict, title: str = "") -> bytes:
    """PNG de la build. `title` = nombre tal cual lo escribió quien armó la compo (si no, el de la propia build)."""
    b = data.get("build") or data
    draft = b.get("draft") or b
    prefetch(draft)
    name = draft.get("name") or title or "Build"
    img = Image.new("RGBA", (W, 3000), BG + (255,))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 6), fill=GOLD)
    d.text((M, 26), name.upper()[:40], font=font(36, True), fill=TEXT)
    play, col = PLAY.get((draft.get("playstyle") or "").lower(), ((draft.get("playstyle") or "").upper() or None, MUTED))
    x = M
    if play:
        w = int(d.textlength(play, font=font(14, True))) + 24
        d.rounded_rectangle((x, 78, x + w, 104), radius=9, fill=col)
        d.text((x + w / 2, 91), play, font=font(14, True), fill=(255, 255, 255), anchor="mm")
        x += w + 14
    if draft.get("description"):
        d.text((x, 82), draft["description"][:100], font=font(16), fill=MUTED)
    d.text((W - M, 30), "AlbionZvZ", font=font(17, True), fill=DIM, anchor="ra")
    upd = (b.get("updatedAt") or "")[:10]
    if upd:
        d.text((W - M, 56), f"actualizada {upd}", font=font(14), fill=DIM, anchor="ra")

    top, tile, gap = 124, 150, 12
    lw = 3 * tile + 2 * gap + 44
    lh = 4 * (tile + gap) + 36
    card(img, (M, top, M + lw, top + lh), radius=16)
    d = ImageDraw.Draw(img)
    d.text((M + 22, top + 18), "EQUIPO", font=font(12, True), fill=DIM)
    for r, row in enumerate(GRID):
        for c, key in enumerate(row):
            if not key:
                continue
            it = draft.get(key)
            tx, ty = M + 22 + c * (tile + gap), top + 44 + r * (tile + gap)
            card(img, (tx, ty, tx + tile, ty + tile), radius=12, fill=(22, 23, 30) if it else (17, 18, 23))
            d = ImageDraw.Draw(img)
            if it:
                put(img, icon_box(to_type(it), 100), tx + (tile - 100) // 2, ty + 12)
                if it.get("swaps"):
                    d.rounded_rectangle((tx + tile - 40, ty + 8, tx + tile - 8, ty + 26), radius=6, fill=GOLD)
                    d.text((tx + tile - 24, ty + 17), "ALT", font=font(11, True), fill=(20, 20, 20), anchor="mm")
            d.text((tx + tile / 2, ty + tile - 20), LABELS[key], font=font(11, True), fill=MUTED if it else (70, 72, 82), anchor="mm")

    dx = M + lw + 24
    dw = W - M - dx
    items = [(k, draft[k]) for k in DETAIL_ORDER if draft.get(k)]
    rows = [88 + 60 * len(it.get("swaps") or []) for _, it in items]
    dh = 52 + sum(rows) + 12
    card(img, (dx, top, dx + dw, top + dh), radius=16)
    d = ImageDraw.Draw(img)
    d.text((dx + 22, top + 18), "DETALLES DEL ARTÍCULO", font=font(12, True), fill=DIM)
    y = top + 46
    for n, ((k, it), h) in enumerate(zip(items, rows)):
        if n:
            d.line((dx + 22, y - 6, dx + dw - 22, y - 6), fill=BORDER, width=1)
        put(img, icon_box(to_type(it), 68), dx + 22, y + 4)
        nm = it.get("itemName") or "?"
        d.text((dx + 106, y + 4), nm, font=font(21, True), fill=TEXT)
        tier = (it.get("tier") or "").upper()
        ench = f".{it['enchantment']}" if it.get("enchantment") else ""
        if tier:
            tier_chip(d, dx + 106 + int(d.textlength(nm, font=font(21, True))) + 12, y + 3, tier + ench)
        cx = dx + 106
        for a in it.get("abilities") or []:
            put(img, spell_circle(a, 38), cx, y + 38)
            cx += 46
        yy = y + 88
        for s in it.get("swaps") or []:
            d.text((dx + 106, yy - 12), "ALTERNATIVA", font=font(10, True), fill=GOLD)
            put(img, icon_box(to_type(s), 40), dx + 106, yy + 2)
            sn = s.get("itemName") or "?"
            d.text((dx + 156, yy + 2), sn, font=font(16, True), fill=TEXT)
            tier_chip(d, dx + 156 + int(d.textlength(sn, font=font(16, True))) + 10, yy + 1, (s.get("tier") or "").upper(), 12)
            cx = dx + 156
            for a in s.get("abilities") or []:
                put(img, spell_circle(a, 22), cx, yy + 22)
                cx += 28
            yy += 60
        y += h
    end = max(top + lh, top + dh) + 14
    rv = b.get("regearValueSilver")
    d.text((M, end), f"Valor de regear: {rv:,}".replace(",", ".") if rv else "Valor de regear: no disponible", font=font(15), fill=MUTED)
    d.text((M, end + 26), "Datos de AlbionZvZ · el sitio no informa el encantamiento de armas y armaduras", font=font(12), fill=DIM)
    buf = io.BytesIO()
    img.crop((0, 0, W, end + 58)).convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()
