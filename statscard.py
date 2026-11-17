"""Tarjeta PNG del panel /stats (PLAYER PERFORMANCE). Necesita Pillow.

Reutiliza las fuentes cacheadas, la paleta y las tarjetas con sombra de buildcard.py.
- `render(data, avatar)` dibuja (CPU, sin red): se llama en un hilo.
- `render_async(data, avatar_loader)` es lo que usa el bot: caché en memoria con TTL, un solo dibujo por tarjeta aunque la pidan
  varios a la vez y como mucho MAX_PARALELO dibujos simultáneos (asyncio.to_thread: no bloquea el event loop)."""
import asyncio
import hashlib
import io
import json
import os
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from buildcard import BG, BORDER, DIM, GOLD, GREEN, MUTED, RED, TEXT, font

CACHE_TTL = int(os.getenv("STATS_CARD_TTL", "300"))       # segundos que vale una tarjeta ya dibujada (mismos datos)
CACHE_MAX = int(os.getenv("STATS_CARD_CACHE_MAX", "200"))  # tarjetas guardadas en memoria como máximo
MAX_PARALELO = int(os.getenv("STATS_CARD_PARALELO", "3"))  # dibujos a la vez (cada uno usa CPU)
AVATAR_SIZE = 256

# Muertes en el primer engage: hasta GOOD_PCT % es bueno, hasta WARN_PCT % regular, más es riesgo alto.
ENGAGE_GOOD_PCT, ENGAGE_WARN_PCT = 10.0, 20.0

W, M = 1200, 26
TRACK = (34, 35, 41)
GOLD_DIM = (150, 120, 40)
GOLD_DEEP = (92, 70, 22)
CHARCOAL = (19, 19, 23)
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
_pfonts: dict = {}
_cache: "OrderedDict[str, tuple]" = OrderedDict()   # clave -> (vence, png)
_avatars: "OrderedDict[str, tuple]" = OrderedDict()  # url -> (vence, Image RGBA ya recortada)
_inflight: dict = {}
_sem: Optional[asyncio.Semaphore] = None
_bg_cache: dict = {}


def pf(size: int, weight: str = "Bold"):
    """Poppins del repo (assets/fonts), cacheada por tamaño. Si no está, la fuente del sistema de buildcard."""
    key = (size, weight)
    if key not in _pfonts:
        try:
            _pfonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, f"Poppins-{weight}.ttf"), size)
        except Exception:
            _pfonts[key] = font(size, weight != "Regular")
    return _pfonts[key]


# ---------------- utilidades de dibujo ----------------
def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", ".")


def _rgba(c, a):
    return (c[0], c[1], c[2], a)


def _overlay(img, fn):
    """Dibuja con transparencia: `fn(draw)` sobre una capa RGBA que se mezcla con la imagen."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer))
    img.alpha_composite(layer)


def _tracked(d, x, y, text, f, fill, spacing=2.0, anchor_right=False):
    """Texto en mayúsculas con espaciado entre letras."""
    text = text.upper()
    width = sum(d.textlength(ch, font=f) + spacing for ch in text) - spacing
    if anchor_right:
        x -= width
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + spacing
    return width


def _fit(d, text, size, max_w, weight="Bold"):
    while size > 14 and d.textlength(text, font=pf(size, weight)) > max_w:
        size -= 2
    return pf(size, weight)


def _background(h: int) -> Image.Image:
    """Fondo carbón con degradado, viñeta y trama diagonal sutil (se arma una vez por alto y se reutiliza)."""
    if h not in _bg_cache:
        top, bot = (22, 22, 27), (9, 9, 11)
        grad = Image.new("RGB", (1, h))
        for y in range(h):
            t = y / max(1, h - 1)
            grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
        bg = grad.resize((W, h)).convert("RGBA")
        hatch = Image.new("RGBA", (W, h), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hatch)
        for x in range(-h, W, 14):
            hd.line((x, h, x + h, 0), fill=(255, 255, 255, 5), width=1)
        bg.alpha_composite(hatch)
        vig = Image.new("L", (W // 4, h // 4), 0)
        ImageDraw.Draw(vig).ellipse((-W // 10, -h // 10, W // 4 + W // 10, h // 4 + h // 10), fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(28)).resize((W, h))
        dark = Image.new("RGBA", (W, h), (0, 0, 0, 150))
        dark.putalpha(Image.eval(vig, lambda v: int((255 - v) * 0.55)))
        bg.alpha_composite(dark)
        _bg_cache[h] = bg
    return _bg_cache[h].copy()


def _panel(img, box, radius=14, accent=False):
    """Panel de vidrio oscuro con borde fino (dorado si `accent`)."""
    x0, y0, x1, y1 = box
    pad = 20                                           # sombra solo en el recuadro del panel (más liviano que todo el lienzo)
    rx0, ry0 = max(0, int(x0) - pad), max(0, int(y0) - pad)
    rx1, ry1 = min(img.width, int(x1) + pad), min(img.height, int(y1) + pad + 6)
    sh = Image.new("RGBA", (rx1 - rx0, ry1 - ry0), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((x0 + 2 - rx0, y0 + 6 - ry0, x1 + 2 - rx0, y1 + 6 - ry0), radius=radius, fill=(0, 0, 0, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)), (rx0, ry0))

    def f(d):
        d.rounded_rectangle(box, radius=radius, fill=(24, 24, 29, 228),
                            outline=_rgba(GOLD, 120) if accent else (255, 255, 255, 22), width=1)
        d.line((x0 + radius, y0 + 1, x1 - radius, y0 + 1), fill=(255, 255, 255, 14), width=1)   # brillo superior
    _overlay(img, f)


def _gold_rule(img, x0, x1, y, diamond=True):
    """Filete dorado que se desvanece en los extremos, con rombo al centro."""
    def f(d):
        n = 40
        for i in range(n):
            t0, t1 = i / n, (i + 1) / n
            a = int(170 * (1 - abs(0.5 - (t0 + t1) / 2) * 2) ** 0.8)
            d.line((x0 + (x1 - x0) * t0, y, x0 + (x1 - x0) * t1, y), fill=_rgba(GOLD, a), width=1)
        if diamond:
            cx = (x0 + x1) / 2
            d.polygon([(cx, y - 5), (cx + 5, y), (cx, y + 5), (cx - 5, y)], fill=_rgba(GOLD, 220))
    _overlay(img, f)


def _vrule(img, x, y0, y1):
    def f(d):
        n = 20
        for i in range(n):
            t0, t1 = i / n, (i + 1) / n
            a = int(120 * (1 - abs(0.5 - (t0 + t1) / 2) * 2))
            d.line((x, y0 + (y1 - y0) * t0, x, y0 + (y1 - y0) * t1), fill=_rgba(GOLD, a), width=1)
    _overlay(img, f)


def _seg_bar(d, x0, y, x1, h, frac, color, segs=24, gap=3):
    """Barra segmentada (estilo HUD)."""
    frac = max(0.0, min(1.0, frac))
    sw = ((x1 - x0) - gap * (segs - 1)) / segs
    lit = round(frac * segs)
    if frac > 0 and lit == 0:
        lit = 1
    for i in range(segs):
        sx = x0 + i * (sw + gap)
        d.rectangle((sx, y, sx + sw, y + h), fill=color if i < lit else TRACK)


def _octagon(cx, cy, r):
    import math
    return [(cx + r * math.cos(math.radians(22.5 + 45 * i)), cy + r * math.sin(math.radians(22.5 + 45 * i))) for i in range(8)]


def _avatar_img(raw: Optional[bytes], size: int) -> Optional[Image.Image]:
    """Avatar recortado en octógono (RGBA). Se arma en un hilo y se cachea."""
    if not raw:
        return None
    try:
        av = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None
    k = 4
    mask = Image.new("L", (size * k, size * k), 0)
    ImageDraw.Draw(mask).polygon(_octagon(size * k / 2, size * k / 2, size * k / 2 / 0.924 * 0.999), fill=255)
    av.putalpha(mask.resize((size, size), Image.LANCZOS))
    return av


_circle_avatar = _avatar_img          # nombre anterior


def engage_rating(pct: float) -> tuple:
    if pct <= ENGAGE_GOOD_PCT:
        return "BUENO", GREEN
    if pct <= ENGAGE_WARN_PCT:
        return "REGULAR", GOLD
    return "RIESGO ALTO", RED


# ---------------- íconos (dibujados, sin archivos) ----------------
def _icon_sword(d, x, y, s, c):
    d.line((x + s * 0.15, y + s * 0.85, x + s * 0.85, y + s * 0.15), fill=c, width=max(2, int(s * 0.12)))
    d.line((x + s * 0.12, y + s * 0.58, x + s * 0.42, y + s * 0.88), fill=c, width=max(2, int(s * 0.1)))
    d.polygon([(x + s * 0.85, y + s * 0.15), (x + s * 0.98, y + s * 0.02), (x + s * 0.92, y + s * 0.2)], fill=c)


def _icon_skull(d, x, y, s, c):
    d.ellipse((x + s * 0.12, y + s * 0.05, x + s * 0.88, y + s * 0.72), fill=c)
    d.rectangle((x + s * 0.3, y + s * 0.6, x + s * 0.7, y + s * 0.92), fill=c)
    for ex in (0.36, 0.64):
        d.ellipse((x + s * (ex - 0.11), y + s * 0.3, x + s * (ex + 0.11), y + s * 0.52), fill=CHARCOAL)
    for tx in (0.42, 0.58):
        d.line((x + s * tx, y + s * 0.74, x + s * tx, y + s * 0.92), fill=CHARCOAL, width=2)


def _icon_banner(d, x, y, s, c):
    d.line((x + s * 0.2, y + s * 0.05, x + s * 0.2, y + s * 0.98), fill=c, width=max(2, int(s * 0.1)))
    d.polygon([(x + s * 0.25, y + s * 0.08), (x + s * 0.92, y + s * 0.08), (x + s * 0.92, y + s * 0.55),
               (x + s * 0.6, y + s * 0.42), (x + s * 0.25, y + s * 0.55)], fill=c)


def _icon_star(d, x, y, s, c):
    import math
    pts = []
    for i in range(10):
        r = s * (0.5 if i % 2 == 0 else 0.21)
        a = math.radians(-90 + 36 * i)
        pts.append((x + s / 2 + r * math.cos(a), y + s * 0.53 + r * math.sin(a)))
    d.polygon(pts, fill=c)


def _shield(d, x0, y0, w, h, fill, outline):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h * 0.55), (x0 + w / 2, y0 + h), (x0, y0 + h * 0.55)]
    d.polygon(pts, fill=fill, outline=outline, width=2)


# ---------------- dibujo ----------------
def render(data: dict, avatar: Optional[Image.Image] = None) -> bytes:
    """PNG de la tarjeta. `data` sale de KillboardCog.stats (solo datos ya calculados, nada se calcula acá)."""
    has_kb = bool(data.get("has_kb"))
    main = data.get("main")
    HH, CH, MH, BH, G = 176, 150, 236, 262, 16
    H = M + HH + G + CH + G + MH + G + BH + 58
    img = _background(H)
    inner_x0, inner_x1 = M, W - M

    # marco exterior con esquinas
    def frame(d):
        d.rounded_rectangle((8, 8, W - 9, H - 9), radius=18, outline=_rgba(GOLD, 60), width=1)
        for cx, cy in ((8, 8), (W - 9, 8), (8, H - 9), (W - 9, H - 9)):
            d.polygon([(cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy)], fill=_rgba(GOLD, 200))
    _overlay(img, frame)

    # ---- A. encabezado ----
    y = M
    hb = (inner_x0, y, inner_x1, y + HH)
    _panel(img, hb, radius=16, accent=True)
    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bw = int((inner_x1 - inner_x0) * 0.62)
    grad = Image.new("L", (bw, 1))
    for i in range(bw):
        grad.putpixel((i, 0), int(95 * (1 - i / bw) ** 1.6))
    grad = grad.resize((bw, HH - 2))
    col = Image.new("RGBA", (bw, HH - 2), _rgba(GOLD_DEEP, 255))
    col.putalpha(grad)
    mask = Image.new("L", (bw, HH - 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw + 20, HH - 3), radius=15, fill=255)
    col.putalpha(Image.composite(col.getchannel("A"), Image.new("L", col.size, 0), mask))
    band.paste(col, (inner_x0 + 1, y + 1))
    img.alpha_composite(band)

    def watermark(d):
        f = pf(118, "Bold")
        d.text((inner_x1 - 34 - 168 - 26, y + HH / 2 + 6), "SMOGG", font=f, fill=_rgba(GOLD, 12), anchor="rm")
    _overlay(img, watermark)
    d = ImageDraw.Draw(img)

    av_r = 66
    acx, acy = inner_x0 + 34 + av_r, y + HH / 2
    d.polygon(_octagon(acx, acy, av_r + 9), fill=GOLD)
    d.polygon(_octagon(acx, acy, av_r + 5), fill=CHARCOAL)
    d.polygon(_octagon(acx, acy, av_r + 2), fill=GOLD_DIM)
    if avatar is not None:
        a = avatar if avatar.size == (2 * av_r, 2 * av_r) else avatar.resize((2 * av_r, 2 * av_r), Image.LANCZOS)
        img.alpha_composite(a, (int(acx - av_r), int(acy - av_r)))
    else:
        d.polygon(_octagon(acx, acy, av_r), fill=TRACK)
        d.text((acx, acy), (data.get("title") or "?")[:1].upper(), font=pf(56), fill=GOLD_DIM, anchor="mm")
    d = ImageDraw.Draw(img)

    tx = acx + av_r + 34
    _tracked(d, tx, y + 30, "Player Performance", pf(15, "Medium"), GOLD, 3.2)
    d.line((tx, y + 56, tx + 46, y + 56), fill=GOLD, width=2)
    name = data.get("title") or "—"
    d.text((tx - 2, y + 62), name, font=_fit(d, name, 50, inner_x1 - tx - 250), fill=TEXT)
    d.text((tx, y + 124), data.get("display") or "", font=pf(19, "Regular"), fill=MUTED)

    # escudo del rol main
    sw_, sh_ = 168, 118
    sx0, sy0 = inner_x1 - 34 - sw_, y + (HH - sh_) / 2 - 4
    if main:
        _shield(d, sx0, sy0, sw_, sh_, (46, 37, 15), GOLD)
        _tracked(d, sx0 + sw_ / 2 - 36, sy0 + 16, "Rol main", pf(12, "Medium"), GOLD_DIM, 2.4)
        rn = main["name"].upper()
        d.text((sx0 + sw_ / 2, sy0 + 56), rn, font=_fit(d, rn, 26, sw_ - 26), fill=GOLD, anchor="mm")
    else:
        _shield(d, sx0, sy0, sw_, sh_, (28, 28, 33), BORDER)
        _tracked(d, sx0 + sw_ / 2 - 36, sy0 + 16, "Rol main", pf(12, "Medium"), DIM, 2.4)
        d.text((sx0 + sw_ / 2, sy0 + 56), "SIN CARGAR", font=pf(20), fill=MUTED, anchor="mm")

    # ---- B. combate (franja única) ----
    y += HH + G
    _panel(img, (inner_x0, y, inner_x1, y + CH), radius=14)
    cw = (inner_x1 - inner_x0) / 4
    kda_box = (int(inner_x0 + 3 * cw), y, inner_x1, y + CH)

    def kglow(dd):
        dd.rounded_rectangle(kda_box, radius=14, fill=_rgba(GOLD, 18))
    _overlay(img, kglow)
    for i in range(1, 4):
        _vrule(img, inner_x0 + i * cw, y + 18, y + CH - 18)
    d = ImageDraw.Draw(img)
    tiles = [("Kills", _fmt(data["kills"]) if has_kb else "—", GREEN, _icon_sword),
             ("Muertes", _fmt(data["deaths"]) if has_kb else "—", RED, _icon_skull),
             ("Peleas", _fmt(data["attendance"]) if has_kb else "—", TEXT, _icon_banner),
             ("KDA", f"{data['kda']:.2f}" if has_kb else "—", GOLD, _icon_star)]
    for i, (lab, val, col_, icon) in enumerate(tiles):
        x0 = inner_x0 + i * cw + 30
        icon(d, x0, y + 24, 22, col_ if lab != "Peleas" else MUTED)
        _tracked(d, x0 + 32, y + 26, lab, pf(15, "Medium"), MUTED, 2.6)
        d.text((x0 - 2, y + 50), val, font=_fit(d, val, 68, cw - 56), fill=col_ if lab in ("KDA",) or not has_kb else TEXT)

    # ---- C/D. primer engage + item power ----
    y += CH + G
    _panel(img, (inner_x0, y, inner_x1, y + MH), radius=14)
    mid = (inner_x0 + inner_x1) / 2
    _vrule(img, mid, y + 22, y + MH - 22)
    d = ImageDraw.Draw(img)
    lx0, lx1, rx0, rx1 = inner_x0 + 30, mid - 30, mid + 30, inner_x1 - 30
    _tracked(d, lx0, y + 24, "Primer engage", pf(15, "Bold"), GOLD, 3)
    if has_kb and data.get("attendance"):
        pct = float(data.get("first5_pct") or 0)
        rate, rc = engage_rating(pct)
        # medidor semicircular: escala 0–40 %
        gcx, gcy, gr = lx0 + 112, y + 168, 92
        scale = 40.0
        zones = [(0, ENGAGE_GOOD_PCT, GREEN), (ENGAGE_GOOD_PCT, ENGAGE_WARN_PCT, GOLD), (ENGAGE_WARN_PCT, scale, RED)]
        for a, b, zc in zones:
            d.arc((gcx - gr, gcy - gr, gcx + gr, gcy + gr), 180 + 180 * a / scale, 180 + 180 * b / scale,
                  fill=tuple(int(v * 0.45) for v in zc), width=16)
        d.arc((gcx - gr, gcy - gr, gcx + gr, gcy + gr), 180, 180 + 180 * min(pct, scale) / scale, fill=rc, width=16)
        import math
        ang = math.radians(180 + 180 * min(pct, scale) / scale)
        nx, ny = gcx + (gr - 30) * math.cos(ang), gcy + (gr - 30) * math.sin(ang)
        d.line((gcx, gcy, nx, ny), fill=TEXT, width=4)
        d.ellipse((gcx - 9, gcy - 9, gcx + 9, gcy + 9), fill=TEXT, outline=CHARCOAL, width=3)
        d.text((gcx - gr, gcy + 14), "0%", font=pf(13, "Medium"), fill=DIM, anchor="mt")
        d.text((gcx + gr, gcy + 14), f"{scale:.0f}%+", font=pf(13, "Medium"), fill=DIM, anchor="mt")
        tx2 = gcx + gr + 34
        d.text((tx2, y + 46), f"{pct:.0f}%", font=pf(76), fill=rc)
        rf = pf(14, "Bold")
        rw = d.textlength(rate, font=rf) + 28
        d.text((tx2 + 2, y + 136), "de muertes en el primer engage", font=pf(14, "Regular"), fill=MUTED)
        d.rounded_rectangle((tx2 + 2, y + 162, tx2 + 2 + rw, y + 188), radius=13, fill=rc)
        d.text((tx2 + 2 + rw / 2, y + 175), rate, font=rf, fill=BG, anchor="mm")
        d.text((tx2 + rw + 16, y + 175), f"{data.get('first5', 0)} de {data.get('attendance', 0)} peleas",
               font=pf(15, "Regular"), fill=MUTED, anchor="lm")
    else:
        d.text((lx0, y + 100), "Sin killboards cargados", font=pf(26), fill=DIM)

    _tracked(d, rx0, y + 24, "Item power", pf(15, "Bold"), GOLD, 3)
    if has_kb and data.get("avg_ip"):
        avg, mx, mn = float(data["avg_ip"]), float(data.get("max_ip") or 0), float(data.get("min_ip") or 0)
        d.text((rx0 - 2, y + 54), _fmt(avg), font=pf(76), fill=TEXT)
        aw = d.textlength(_fmt(avg), font=pf(76))
        _tracked(d, rx0 + aw + 16, y + 92, "IP promedio", pf(14, "Medium"), MUTED, 2.4)
        ry_ = y + 172
        d.line((rx0, ry_, rx1, ry_), fill=TRACK, width=6)
        for k in range(11):
            tx_ = rx0 + (rx1 - rx0) * k / 10
            d.line((tx_, ry_ - (9 if k % 5 == 0 else 5), tx_, ry_ + (9 if k % 5 == 0 else 5)), fill=(70, 72, 82), width=2)
        frac = (avg - mn) / (mx - mn) if mx > mn else 0.5
        ax_ = rx0 + (rx1 - rx0) * frac
        d.line((rx0, ry_, ax_, ry_), fill=GOLD_DIM, width=6)
        d.polygon([(ax_, ry_ - 14), (ax_ + 12, ry_), (ax_, ry_ + 14), (ax_ - 12, ry_)], fill=GOLD, outline=CHARCOAL)
        _tracked(d, rx0, ry_ + 22, f"Mín {_fmt(mn)}", pf(15, "Bold"), MUTED, 1.6)
        _tracked(d, rx1, ry_ + 22, f"Máx {_fmt(mx)}", pf(15, "Bold"), MUTED, 1.6, anchor_right=True)
    else:
        d.text((rx0, y + 100), "Sin datos de IP", font=pf(26), fill=DIM)

    # ---- E/G. roles + actividad ----
    y += MH + G
    _panel(img, (inner_x0, y, inner_x1, y + BH), radius=14)
    _vrule(img, mid, y + 22, y + BH - 22)
    d = ImageDraw.Draw(img)
    _tracked(d, lx0, y + 24, "Roles más jugados", pf(15, "Bold"), GOLD, 3)
    roles = data.get("roles") or []
    if roles:
        ry = y + 60
        for i, (rname, rpct, rn_) in enumerate(roles[:3]):
            top = i == 0
            if top:
                def hl(dd, ry=ry):
                    dd.rectangle((lx0 - 12, ry - 6, lx1 + 8, ry + 44), fill=_rgba(GOLD, 14))
                    dd.rectangle((lx0 - 12, ry - 6, lx0 - 9, ry + 44), fill=_rgba(GOLD, 230))
                _overlay(img, hl)
                d = ImageDraw.Draw(img)
            c_ = GOLD if top else (TEXT if i == 1 else MUTED)
            d.text((lx0, ry), f"0{i + 1}", font=pf(15, "Bold"), fill=GOLD_DIM if top else DIM)
            d.text((lx0 + 32, ry - 4), rname, font=pf(21, "Bold" if top else "Medium"), fill=c_)
            d.text((lx1, ry - 2), f"{rpct:.0f}%", font=pf(21, "Bold"), fill=c_, anchor="ra")
            pw = d.textlength(f"{rpct:.0f}%", font=pf(21, "Bold"))
            d.text((lx1 - pw - 12, ry + 3), f"{rn_} CTA", font=pf(14, "Regular"), fill=MUTED, anchor="ra")
            _seg_bar(d, lx0 + 32, ry + 30, lx1, 7, rpct / 100.0, GOLD if top else (112, 116, 130), segs=30)
            ry += 50
    else:
        d.text((lx0, y + 100), "Todavía sin CTA cerradas", font=pf(24), fill=DIM)
    # rol main
    fy = y + BH - 46
    d.line((lx0, fy - 12, lx1, fy - 12), fill=(255, 255, 255, 18) if False else (42, 43, 50), width=1)
    if main and main.get("total"):
        mp = 100.0 * main["n"] / main["total"]
        mc = GREEN if mp >= 50 else RED
        _tracked(d, lx0, fy + 2, "Main", pf(13, "Bold"), GOLD_DIM, 2.4)
        d.text((lx0 + 56, fy - 4), main["name"], font=pf(18, "Bold"), fill=TEXT)
        d.text((lx1, fy - 4), f"{mp:.0f}% de sus CTA  ·  {main['n']}/{main['total']}", font=pf(17, "Bold"), fill=mc, anchor="ra")
    elif main:
        _tracked(d, lx0, fy + 2, "Main", pf(13, "Bold"), GOLD_DIM, 2.4)
        d.text((lx0 + 56, fy - 4), f"{main['name']} · todavía sin CTA cerradas", font=pf(17, "Medium"), fill=MUTED)
    else:
        d.text((lx0, fy - 4), "Rol main sin cargar", font=pf(17, "Medium"), fill=DIM)

    _tracked(d, rx0, y + 24, "Actividad en CTA", pf(15, "Bold"), GOLD, 3)
    tot = data.get("cta_total")
    if tot is not None:
        d.text((rx0 - 2, y + 50), _fmt(tot), font=pf(72), fill=TEXT)
        tw_ = d.textlength(_fmt(tot), font=pf(72))
        _tracked(d, rx0 + tw_ + 16, y + 86, "Asistencias totales", pf(14, "Medium"), MUTED, 2.2)
        wk, wn = int(data.get("cta_week") or 0), int(data.get("cta_week_n") or 0)
        py_ = y + 152
        _tracked(d, rx0, py_ - 4, "Esta semana", pf(13, "Bold"), MUTED, 2.2)
        wcol = GREEN if wn and wk / wn >= 0.5 else (GOLD if wk else RED)
        d.text((rx1, py_ - 10), f"{wk} / {wn}", font=pf(22, "Bold"), fill=wcol if wn else MUTED, anchor="ra")
        if 0 < wn <= 14:
            pw_, gap_ = ((rx1 - rx0) - 6 * (wn - 1)) / wn, 6
            for k in range(wn):
                px = rx0 + k * (pw_ + gap_)
                d.rounded_rectangle((px, py_ + 22, px + pw_, py_ + 34), radius=4, fill=wcol if k < wk else TRACK)
        else:
            _seg_bar(d, rx0, py_ + 22, rx1, 12, (wk / wn) if wn else 0.0, wcol, segs=20)
    else:
        d.text((rx0, y + 100), "Sin datos de asistencia", font=pf(24), fill=DIM)
    # skipeadas / looteado
    fy2 = y + BH - 58
    d.line((rx0, fy2 - 2, rx1, fy2 - 2), fill=(42, 43, 50), width=1)
    half_ = (rx1 - rx0) / 2
    sk = int(data.get("skips") or 0)
    _tracked(d, rx0, fy2 + 10, "CTAs skipeadas", pf(12, "Bold"), MUTED, 2)
    d.text((rx0, fy2 + 26), _fmt(sk), font=pf(22, "Bold"), fill=RED if sk else GREEN)
    _tracked(d, rx0 + half_, fy2 + 10, "Total looteado", pf(12, "Bold"), MUTED, 2)
    d.text((rx0 + half_, fy2 + 26), _fmt(data.get("looted") or 0), font=pf(22, "Bold"), fill=GOLD)

    # ---- pie ----
    y += BH + 16
    _gold_rule(img, inner_x0 + 200, inner_x1 - 200, y + 4)
    d = ImageDraw.Draw(img)
    d.text((W / 2, y + 24), "SMOGG  ·  DATOS DE LOS KILLBOARDS CARGADOS POR EL STAFF", font=pf(12, "Medium"), fill=DIM, anchor="mm")

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


# ---------------- caché y concurrencia ----------------
def card_key(data: dict, avatar_url: str = "") -> str:
    return hashlib.sha1(json.dumps([data, avatar_url], sort_keys=True, default=str).encode()).hexdigest()[:24]


def _cache_get(store: OrderedDict, key: str):
    hit = store.get(key)
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        store.pop(key, None)
        return None
    store.move_to_end(key)
    return hit[1]


def _cache_put(store: OrderedDict, key: str, value, ttl: int = None):
    store[key] = (time.monotonic() + (CACHE_TTL if ttl is None else ttl), value)
    store.move_to_end(key)
    while len(store) > CACHE_MAX:
        store.popitem(last=False)


def cached(data: dict, avatar_url: str = "") -> Optional[bytes]:
    return _cache_get(_cache, card_key(data, avatar_url))


async def render_async(data: dict, avatar_url: str = "",
                       avatar_loader: Optional[Callable[[], Awaitable[bytes]]] = None) -> bytes:
    """PNG de la tarjeta: de la caché si está vigente; si no, se dibuja en un hilo (de a MAX_PARALELO a la vez).
    Si varios piden la misma tarjeta mientras se dibuja, esperan ese mismo dibujo."""
    global _sem
    key = card_key(data, avatar_url)
    hit = _cache_get(_cache, key)
    if hit is not None:
        return hit
    task = _inflight.get(key)
    if task is None:
        if _sem is None:
            _sem = asyncio.Semaphore(MAX_PARALELO)

        async def work():
            av = _cache_get(_avatars, avatar_url) if avatar_url else None
            if av is None and avatar_loader is not None:
                try:
                    raw = await asyncio.wait_for(avatar_loader(), timeout=5)
                    av = await asyncio.to_thread(_avatar_img, raw, 132)
                    if av is not None and avatar_url:
                        _cache_put(_avatars, avatar_url, av, ttl=max(CACHE_TTL, 1800))
                except Exception:
                    av = None
            async with _sem:
                png = await asyncio.to_thread(render, data, av)
            _cache_put(_cache, key, png)
            return png

        task = asyncio.ensure_future(work())
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    return await asyncio.shield(task)
