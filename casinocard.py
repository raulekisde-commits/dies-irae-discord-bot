"""Tarjeta PNG de las stats del casino (/winrate y !winrate), con el mismo estilo Smogg que /stats.

Reutiliza de statscard.py el fondo con humo, paneles, fuentes, avatar, caché con TTL y semáforo (render_async con renderer=render).
Los emojis personalizados (Wuju Coins y Embo) son los archivos originales de assets/emojis: se cargan una sola vez y se
escalan con LANCZOS al tamaño que haga falta (también cacheado). La Embo queda cargada para cuando haya datos de embo."""
import io
import math
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

import statscard as sc
from buildcard import DIM, GOLD, GREEN, MUTED, RED, TEXT
from statscard import (CHARCOAL, GOLD_DIM, GOLD_DEEP, TRACK, W, _fit, _fmt, _gold_rule, _octagon, _overlay, _panel, _rgba,
                       _seg_bar, _tracked, _vrule, pf)

NS = "|casino"                                    # espacio propio en la caché compartida con /stats
EMOJI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "emojis")
_emoji_src: dict = {}
_emoji_cache: dict = {}


def emoji(name: str, size: int) -> Optional[Image.Image]:
    """Emoji original (RGBA, transparencia intacta) al tamaño pedido, sin deformar. Se carga y escala una sola vez por tamaño."""
    key = (name, size)
    if key not in _emoji_cache:
        if name not in _emoji_src:
            try:
                _emoji_src[name] = Image.open(os.path.join(EMOJI_DIR, f"{name}.png")).convert("RGBA")
            except Exception:
                _emoji_src[name] = None
        src = _emoji_src[name]
        if src is None:
            _emoji_cache[key] = None
        else:
            r = size / max(src.size)
            _emoji_cache[key] = src.resize((max(1, round(src.width * r)), max(1, round(src.height * r))), Image.LANCZOS)
    return _emoji_cache[key]


def _put_emoji(img, name: str, x, cy, size: int) -> int:
    """Pega el emoji centrado verticalmente en `cy`, con un halo dorado suave para integrarlo al fondo. Devuelve el ancho."""
    e = emoji(name, size)
    if e is None:
        return 0
    pad = size // 3
    glow = Image.new("RGBA", (e.width + 2 * pad, e.height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((pad - 2, pad - 2, pad + e.width + 2, pad + e.height + 2), fill=_rgba(GOLD, 60))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(pad / 2)), (int(x) - pad, int(cy - e.height / 2) - pad))
    img.alpha_composite(e, (int(x), int(cy - e.height / 2)))
    return e.width


def _amount(img, x, y, value: int, size: int, color, emoji_size: Optional[int] = None) -> int:
    """Monto con el emoji Wuju a la izquierda. `y` = arriba del texto. Devuelve el ancho total."""
    es = emoji_size or int(size * 0.95)
    f = pf(size)
    d = ImageDraw.Draw(img)
    txt = _fmt(value)
    asc = f.getbbox("0")
    cy = y + (asc[1] + asc[3]) / 2
    ew = _put_emoji(img, "wuju", x, cy, es)
    d = ImageDraw.Draw(img)
    gap = max(6, size // 6) if ew else 0
    d.text((x + ew + gap, y), txt, font=f, fill=color)
    return ew + gap + d.textlength(txt, font=f)


def _dice_icon(d, x, y, s, c):
    d.rounded_rectangle((x, y, x + s, y + s), radius=s // 5, outline=c, width=2)
    for px, py in ((0.28, 0.28), (0.72, 0.72), (0.5, 0.5), (0.72, 0.28), (0.28, 0.72)):
        d.ellipse((x + s * px - 2, y + s * py - 2, x + s * px + 2, y + s * py + 2), fill=c)


def _flip_icon(d, x, y, s, c):
    d.arc((x, y, x + s, y + s), 200, 340, fill=c, width=2)
    d.arc((x, y, x + s, y + s), 20, 160, fill=c, width=2)
    d.polygon([(x + s - 1, y + s * 0.62), (x + s + 4, y + s * 0.38), (x + s - 7, y + s * 0.42)], fill=c)
    d.polygon([(x + 1, y + s * 0.38), (x - 4, y + s * 0.62), (x + 7, y + s * 0.58)], fill=c)


def _ring(d, cx, cy, r, frac, color, width=12):
    d.arc((cx - r, cy - r, cx + r, cy + r), 0, 360, fill=TRACK, width=width)
    if frac > 0:
        d.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * min(1.0, frac), fill=color, width=width)


def render(data: dict, avatar: Optional[Image.Image] = None) -> bytes:
    """PNG de las stats del casino. `data` sale de GamesCog.winrate (solo datos ya calculados)."""
    M, G = 40, 20
    HH, WH, AH, NH = 170, 200, 210, 150
    H = M + HH + G + WH + G + AH + G + NH + 70
    img = sc._background(H)
    x0, x1 = M, W - M
    mid = (x0 + x1) / 2

    def frame(d):
        d.rounded_rectangle((8, 8, W - 9, H - 9), radius=18, outline=_rgba(GOLD, 60), width=1)
        for cx, cy in ((8, 8), (W - 9, 8), (8, H - 9), (W - 9, H - 9)):
            d.polygon([(cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy)], fill=_rgba(GOLD, 200))
    _overlay(img, frame)

    # ---- encabezado ----
    y = M
    _panel(img, (x0, y, x1, y + HH), radius=16, accent=True)
    img.alpha_composite(sc._header_smoke(int(x1 - x0), HH), (int(x0), int(y)))
    d = ImageDraw.Draw(img)
    av_r = 62
    acx, acy = x0 + 32 + av_r, y + HH / 2
    d.polygon(_octagon(acx, acy, av_r + 9), fill=GOLD)
    d.polygon(_octagon(acx, acy, av_r + 5), fill=CHARCOAL)
    d.polygon(_octagon(acx, acy, av_r + 2), fill=GOLD_DIM)
    if avatar is not None:
        a = avatar.resize((2 * av_r, 2 * av_r), Image.LANCZOS) if avatar.size != (2 * av_r, 2 * av_r) else avatar
        img.alpha_composite(a, (int(acx - av_r), int(acy - av_r)))
    else:
        d.polygon(_octagon(acx, acy, av_r), fill=TRACK)
        d.text((acx, acy), (data.get("name") or "?")[:1].upper(), font=pf(52), fill=GOLD_DIM, anchor="mm")
    d = ImageDraw.Draw(img)
    tx = acx + av_r + 32
    _tracked(d, tx, y + 24, "Casino stats", pf(15, "Medium"), GOLD, 3.2)
    d.line((tx, y + 49, tx + 46, y + 49), fill=GOLD, width=2)
    name = data.get("name") or "—"
    d.text((tx - 2, y + 54), name, font=_fit(d, name, 48, x1 - tx - 330), fill=TEXT)
    if data.get("banned"):
        f = pf(15, "Bold")
        t = "LISTA NEGRA DEL CASINO"
        tw = d.textlength(t, font=f) + 28
        d.rounded_rectangle((tx, y + 120, tx + tw, y + 150), radius=7, fill=(48, 16, 16), outline=RED, width=1)
        d.text((tx + tw / 2, y + 135), t, font=f, fill=RED, anchor="mm")
    # nivel de ludópata (escudo)
    sw_, sh_ = 250, 122
    sx0, sy0 = x1 - 30 - sw_, y + (HH - sh_) / 2 - 2
    lvl = data.get("level")
    d.polygon([(sx0, sy0), (sx0 + sw_, sy0), (sx0 + sw_, sy0 + sh_ * 0.6), (sx0 + sw_ / 2, sy0 + sh_), (sx0, sy0 + sh_ * 0.6)],
              fill=(46, 37, 15) if lvl else (28, 28, 33), outline=GOLD if lvl else (70, 72, 82), width=2)
    _tracked(d, sx0 + sw_ / 2 - 74, sy0 + 16, "Nivel de ludópata", pf(12, "Medium"), GOLD_DIM if lvl else DIM, 2.2)
    lt = (lvl or "Sin nivel").upper()
    d.text((sx0 + sw_ / 2, sy0 + 56), lt, font=_fit(d, lt, 24, sw_ - 30), fill=GOLD if lvl else MUTED, anchor="mm")

    # ---- winrates ----
    y += HH + G
    _panel(img, (x0, y, x1, y + WH), radius=14)
    _vrule(img, mid, y + 20, y + WH - 20)
    d = ImageDraw.Draw(img)
    for i, (key, label, icon) in enumerate((("dice", "Winrate dados", _dice_icon), ("coin", "Winrate coinflip", _flip_icon))):
        bx = x0 + 30 if i == 0 else mid + 30
        w = data.get(key) or {}
        played, wins = int(w.get("played") or 0), int(w.get("wins") or 0)
        rate = wins / played if played else 0.0
        icon(d, bx, y + 24, 20, GOLD)
        _tracked(d, bx + 32, y + 26, label, pf(15, "Bold"), GOLD, 3)
        col = GREEN if rate >= 0.5 else RED
        if not played:
            col = MUTED
        rcx, rcy = bx + 66, y + 124
        _ring(d, rcx, rcy, 52, rate, col)
        d.text((rcx, rcy), f"{rate * 100:.0f}%" if played else "—", font=pf(28), fill=TEXT, anchor="mm")
        tx2 = bx + 150
        d.text((tx2, y + 78), _fmt(wins), font=pf(54), fill=col if played else MUTED)
        ww = d.textlength(_fmt(wins), font=pf(54))
        _tracked(d, tx2 + ww + 12, y + 108, "ganadas", pf(13, "Medium"), MUTED, 2)
        d.text((tx2, y + 148), f"de {_fmt(played)} partidas", font=pf(17, "Regular"), fill=MUTED)

    # ---- apostado + nivel ----
    y += WH + G
    _panel(img, (x0, y, x1, y + AH), radius=14)
    _vrule(img, mid, y + 20, y + AH - 20)
    d = ImageDraw.Draw(img)
    lx0, lx1, rx0, rx1 = x0 + 30, mid - 30, mid + 30, x1 - 30
    _tracked(d, lx0, y + 24, "Total apostado", pf(15, "Bold"), GOLD, 3)
    _amount(img, lx0, y + 62, int(data.get("wagered") or 0), 60, TEXT, 58)
    d = ImageDraw.Draw(img)
    d.text((lx0, y + 152), f"en {_fmt(data.get('games') or 0)} apuestas", font=pf(18, "Regular"), fill=MUTED)

    _tracked(d, rx0, y + 24, "Próximo nivel", pf(15, "Bold"), GOLD, 3)
    nxt = data.get("next")
    if nxt:
        cur_min, nxt_min = int(data.get("level_min") or 0), int(nxt["min"])
        frac = (int(data.get("wagered") or 0) - cur_min) / (nxt_min - cur_min) if nxt_min > cur_min else 0.0
        d.text((rx0, y + 58), nxt["name"].upper(), font=_fit(d, nxt["name"].upper(), 30, rx1 - rx0), fill=TEXT)
        _seg_bar(d, rx0, y + 110, rx1, 12, frac, GOLD, segs=28)
        _tracked(d, rx0, y + 146, "Le faltan", pf(13, "Bold"), MUTED, 2)
        _amount(img, rx0 + 104, y + 138, int(nxt["missing"]), 26, GOLD, 30)
        d = ImageDraw.Draw(img)
    else:
        d.text((rx0, y + 76), "NIVEL MÁXIMO", font=pf(34), fill=GOLD)

    # ---- ganado / perdido / neto + racha ----
    y += AH + G
    _panel(img, (x0, y, x1, y + NH), radius=14)
    cw = (x1 - x0) / 4
    for i in range(1, 4):
        _vrule(img, x0 + i * cw, y + 18, y + NH - 18)
    d = ImageDraw.Draw(img)
    net = int(data.get("net") or 0)
    cols = [("Total ganado", max(net, 0), GREEN), ("Total perdido", max(-net, 0), RED),
            ("Resultado neto", net, GREEN if net > 0 else (RED if net < 0 else TEXT))]
    for i, (lab, val, c) in enumerate(cols):
        c = c if val else TEXT
        bx = x0 + i * cw + 26
        _tracked(d, bx, y + 26, lab, pf(13, "Bold"), MUTED, 2)
        _amount(img, bx, y + 64, val if lab != "Resultado neto" else val, 32, c, 34)
        d = ImageDraw.Draw(img)
    bx = x0 + 3 * cw + 26
    _tracked(d, bx, y + 26, "Racha coinflip", pf(13, "Bold"), MUTED, 2)
    d.text((bx, y + 58), str(int(data.get("streak") or 0)), font=pf(44), fill=TEXT)
    sw2 = d.textlength(str(int(data.get("streak") or 0)), font=pf(44))
    d.text((bx + sw2 + 10, y + 92), "actual", font=pf(14, "Regular"), fill=MUTED, anchor="ls")
    d.text((bx, y + 116), f"Mejor: {int(data.get('best') or 0)} seguidas", font=pf(15, "Bold"), fill=GOLD)

    # ---- pie ----
    y += NH + 16
    _gold_rule(img, x0 + 200, x1 - 200, y + 4)
    d = ImageDraw.Draw(img)
    d.text((W / 2, y + 24), "WINRATE: SOLO PARTIDAS TERMINADAS CON RIVAL  ·  GANADO / PERDIDO ES EL NETO",
           font=pf(12, "Medium"), fill=DIM, anchor="mm")

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=6)
    return buf.getvalue()
