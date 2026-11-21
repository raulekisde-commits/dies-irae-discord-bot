"""Tarjeta PNG de Wuju Coins (!bal y /bal), con el mismo estilo Smogg que /stats y /winrate.

Solo dibuja: los números llegan ya calculados desde EconomyCog (mismas consultas que el embed de !bal).
Reutiliza de statscard.py el fondo, paneles, fuentes, rango (badge + halo), caché con TTL y semáforo
(render_async con renderer=render), y de casinocard.py los emojis originales (assets/emojis), que se cargan
una sola vez y se escalan con LANCZOS por tamaño (también cacheado).
- Wuju (wuju.png): balance actual, histórico, total apostado, ganado y perdido apostando.
- Embo (embo.png): solo el balance de Energías Embotelladas."""
import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

import statscard as sc
from buildcard import DIM, GOLD, GREEN, MUTED, RED, TEXT
from casinocard import _put_emoji
from statscard import (CHARCOAL, GOLD_DIM, RANKS, TRACK, W, _fit, _fmt, _gold_rule, _octagon, _overlay, _panel, _rank_badge,
                       _rgba, _tracked, _vrule, pf)

NS = "|wallet"                                    # espacio propio en la caché compartida con /stats y /winrate


def _signed(n: int, signed: bool) -> str:
    return ("+" if signed and n > 0 else "") + _fmt(n)


def _amount(img, x, y, text: str, size: int, color, emoji: str, suffix: str = "", max_w: float = 0) -> float:
    """Emoji + número (+ sufijo opcional chico). `y` = arriba del texto. El emoji mide ~1,3× la altura de los dígitos de
    `size` y queda centrado con ellos; la separación es proporcional. Si el número no entra en `max_w`, se achica solo el
    número (el emoji y su línea quedan iguales en toda la fila). Devuelve el ancho total."""
    base = pf(size)
    box = base.getbbox("0")
    cy = y + (box[1] + box[3]) / 2
    es = max(16, round((box[3] - box[1]) * 1.3))
    ew = _put_emoji(img, emoji, x, cy, es)
    gap = max(8, round(size * 0.18)) if ew else 0
    d = ImageDraw.Draw(img)
    f = _fit(d, text, size, max_w - ew - gap) if max_w else base
    fb = f.getbbox("0")
    tx = x + ew + gap
    d.text((tx, cy - (fb[1] + fb[3]) / 2), text, font=f, fill=color)
    w = ew + gap + d.textlength(text, font=f)
    if suffix:
        fs = pf(max(13, size // 3), "Bold")
        sb = fs.getbbox("E")
        d.text((x + w + max(6, size // 7), cy + (fb[3] - fb[1]) / 2 - sb[3]), suffix, font=fs, fill=MUTED)
    return w


def _tone(n: int):
    return GREEN if n > 0 else (RED if n < 0 else TEXT)


def render(data: dict, avatar: Optional[Image.Image] = None) -> bytes:
    """PNG del balance. `data` sale de EconomyCog._bal_values (solo datos ya calculados)."""
    M, G = 40, 20
    HH, BH, RH, GH = 170, 190, 160, 160
    H = M + HH + G + BH + G + RH + G + GH + 70
    img = sc._background(H)
    x0, x1 = M, W - M
    mid = (x0 + x1) / 2

    def frame(d):
        d.rounded_rectangle((8, 8, W - 9, H - 9), radius=18, outline=_rgba(GOLD, 60), width=1)
        for cx, cy in ((8, 8), (W - 9, 8), (8, H - 9), (W - 9, H - 9)):
            d.polygon([(cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy)], fill=_rgba(GOLD, 200))
    _overlay(img, frame)

    # ---- encabezado: avatar (halo del rango), nombre y rango, igual que /stats ----
    y = M
    _panel(img, (x0, y, x1, y + HH), radius=16, accent=True)
    img.alpha_composite(sc._header_smoke(int(x1 - x0), HH), (int(x0), int(y)))
    av_r = 62
    acx, acy = x0 + 32 + av_r, y + HH / 2
    rank = data.get("rank") if data.get("rank") in RANKS else None
    halo_c = RANKS[rank][4] if rank else None
    if halo_c is not None:
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).polygon(_octagon(acx, acy, av_r + 22), fill=_rgba(halo_c, 70))
        img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    d.polygon(_octagon(acx, acy, av_r + 9), fill=GOLD)
    d.polygon(_octagon(acx, acy, av_r + 5), fill=CHARCOAL)
    d.polygon(_octagon(acx, acy, av_r + 2), fill=GOLD_DIM)
    if avatar is not None:
        a = avatar if avatar.size == (2 * av_r, 2 * av_r) else avatar.resize((2 * av_r, 2 * av_r), Image.LANCZOS)
        img.alpha_composite(a, (int(acx - av_r), int(acy - av_r)))
    else:
        d.polygon(_octagon(acx, acy, av_r), fill=TRACK)
        d.text((acx, acy), (data.get("name") or "?")[:1].upper(), font=pf(52), fill=GOLD_DIM, anchor="mm")
    d = ImageDraw.Draw(img)
    tx = acx + av_r + 32
    ew = _tracked(d, tx, y + 24, data.get("currency") or "Wuju Coins", pf(15, "Medium"), GOLD, 3.2)
    sc.guild_pill(img, tx + ew + 16, y + 19, data.get("guild"))
    d = ImageDraw.Draw(img)
    d.line((tx, y + 49, tx + 46, y + 49), fill=GOLD, width=2)
    name = data.get("name") or "—"
    d.text((tx - 2, y + 54), name, font=_fit(d, name, 48, x1 - tx - 40), fill=TEXT)
    if rank:
        _rank_badge(img, tx, y + 120, rank)

    # ---- balance actual (protagonista) ----
    y += HH + G
    _panel(img, (x0, y, x1, y + BH), radius=14)
    d = ImageDraw.Draw(img)
    bal = int(data.get("balance") or 0)
    _tracked(d, x0 + 30, y + 24, "Balance actual", pf(15, "Bold"), GOLD, 3)
    _amount(img, x0 + 30, y + 64, _fmt(bal), 84, _tone(bal), "wuju")

    # ---- histórico (Wuju) | Energías Embotelladas (Embo) ----
    y += BH + G
    _panel(img, (x0, y, x1, y + RH), radius=14)
    _vrule(img, mid, y + 20, y + RH - 20)
    d = ImageDraw.Draw(img)
    _tracked(d, x0 + 30, y + 24, "Balance histórico", pf(15, "Bold"), GOLD, 3)
    _amount(img, x0 + 30, y + 66, _fmt(int(data.get("lifetime") or 0)), 50, TEXT, "wuju")
    d = ImageDraw.Draw(img)
    embo = int(data.get("embo") or 0)
    _tracked(d, mid + 30, y + 24, "Energías Embotelladas", pf(15, "Bold"), GOLD, 3)
    _amount(img, mid + 30, y + 66, _fmt(embo), 50, RED if embo < 0 else TEXT, "embo", "EMBO")

    # ---- apuestas: apostado / ganado / perdido (Wuju) ----
    y += RH + G
    _panel(img, (x0, y, x1, y + GH), radius=14)
    cw = (x1 - x0) / 3
    for i in range(1, 3):
        _vrule(img, x0 + i * cw, y + 18, y + GH - 18)
    won, lost = int(data.get("won") or 0), int(data.get("lost") or 0)
    cols = [("Total apostado", _fmt(int(data.get("wagered") or 0)), TEXT),
            ("Ganado apostando", _signed(won, True), GREEN if won else TEXT),
            ("Perdido apostando", _fmt(lost), RED if lost else TEXT)]
    for i, (lab, txt, c) in enumerate(cols):
        bx = x0 + i * cw + 28
        d = ImageDraw.Draw(img)
        _tracked(d, bx, y + 26, lab, pf(13, "Bold"), MUTED, 2)
        _amount(img, bx, y + 70, txt, 38, c, "wuju", max_w=cw - 56)

    # ---- pie ----
    y += GH + 16
    _gold_rule(img, x0 + 200, x1 - 200, y + 4)
    d = ImageDraw.Draw(img)
    d.text((W / 2, y + 24), "GANADO / PERDIDO APOSTANDO ES EL NETO, IGUAL QUE EN EL PANEL DE APUESTAS",
           font=pf(12, "Medium"), fill=DIM, anchor="mm")

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=6)
    return buf.getvalue()
