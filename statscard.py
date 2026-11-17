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

from PIL import Image, ImageDraw

from buildcard import BG, BORDER, DIM, GOLD, GREEN, MUTED, PANEL, RED, TEXT, card, font

CACHE_TTL = int(os.getenv("STATS_CARD_TTL", "300"))       # segundos que vale una tarjeta ya dibujada (mismos datos)
CACHE_MAX = int(os.getenv("STATS_CARD_CACHE_MAX", "200"))  # tarjetas guardadas en memoria como máximo
MAX_PARALELO = int(os.getenv("STATS_CARD_PARALELO", "3"))  # dibujos a la vez (cada uno usa CPU)
AVATAR_SIZE = 128

# Muertes en el primer engage: hasta GOOD_PCT % es bueno, hasta WARN_PCT % regular, más es riesgo alto.
ENGAGE_GOOD_PCT, ENGAGE_WARN_PCT = 10.0, 20.0

W, M, GAP = 1100, 32, 20
TRACK = (40, 42, 50)
GOLD_DIM = (150, 120, 40)

_cache: "OrderedDict[str, tuple]" = OrderedDict()   # clave -> (vence, png)
_avatars: "OrderedDict[str, tuple]" = OrderedDict()  # url -> (vence, Image RGBA ya recortada en círculo)
_inflight: dict = {}
_sem: Optional[asyncio.Semaphore] = None


# ---------------- utilidades de dibujo ----------------
def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", ".")


def _label(d, x, y, text, color=MUTED, size=15):
    """Etiqueta en mayúsculas con espaciado (estilo dashboard)."""
    f = font(size, True)
    for ch in text.upper():
        d.text((x, y), ch, font=f, fill=color)
        x += d.textlength(ch, font=f) + 2
    return x


def _bar(d, x0, y0, x1, h, frac, color, track=TRACK):
    frac = max(0.0, min(1.0, frac))
    r = h // 2
    d.rounded_rectangle((x0, y0, x1, y0 + h), radius=r, fill=track)
    if frac > 0:
        d.rounded_rectangle((x0, y0, max(x0 + h, x0 + (x1 - x0) * frac), y0 + h), radius=r, fill=color)


def _fit(d, text, size, max_w, bold=True):
    """Achica la fuente hasta que el texto entre en max_w."""
    while size > 14 and d.textlength(text, font=font(size, bold)) > max_w:
        size -= 2
    return font(size, bold)


def _circle_avatar(raw: Optional[bytes], size: int) -> Optional[Image.Image]:
    if not raw:
        return None
    try:
        av = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    av.putalpha(mask.resize((size, size), Image.LANCZOS))
    return av


def engage_rating(pct: float) -> tuple:
    if pct <= ENGAGE_GOOD_PCT:
        return "BUENO", GREEN
    if pct <= ENGAGE_WARN_PCT:
        return "REGULAR", GOLD
    return "RIESGO ALTO", RED


# ---------------- dibujo ----------------
def render(data: dict, avatar: Optional[Image.Image] = None) -> bytes:
    """PNG de la tarjeta. `data` sale de KillboardCog.stats (solo datos ya calculados, nada se calcula acá)."""
    H = 980
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    has_kb = bool(data.get("has_kb"))
    inner = W - 2 * M

    # ---- A. encabezado ----
    y = M
    hh = 150
    card(img, (M, y, W - M, y + hh), fill=PANEL)
    d = ImageDraw.Draw(img)
    d.rectangle((M + 2, y + 18, M + 6, y + hh - 18), fill=GOLD)
    ax = M + 28
    av_size = 104
    cy = y + hh // 2
    d.ellipse((ax - 4, cy - av_size // 2 - 4, ax + av_size + 4, cy + av_size // 2 + 4), fill=GOLD)
    if avatar is not None:
        img.paste(avatar, (ax, cy - av_size // 2), avatar)
    else:
        d.ellipse((ax, cy - av_size // 2, ax + av_size, cy + av_size // 2), fill=TRACK)
    d = ImageDraw.Draw(img)
    tx = ax + av_size + 28
    _label(d, tx, y + 26, "PLAYER PERFORMANCE", GOLD, 15)
    name = data.get("title") or "—"
    d.text((tx, y + 50), name, font=_fit(d, name, 40, W - M - tx - 230), fill=TEXT)
    d.text((tx, y + 102), data.get("display") or "", font=font(18), fill=MUTED)
    # rol main (chip a la derecha)
    main = data.get("main")
    chip = (main or {}).get("name") or "SIN ROL MAIN"
    f = font(18, True)
    cw = d.textlength(chip.upper(), font=f) + 36
    cx1 = W - M - 28
    d.rounded_rectangle((cx1 - cw, y + 30, cx1, y + 64), radius=17, outline=GOLD if main else BORDER, width=2,
                        fill=(40, 34, 18) if main else PANEL)
    d.text((cx1 - cw / 2, y + 47), chip.upper(), font=f, fill=GOLD if main else MUTED, anchor="mm")
    _label(d, cx1 - cw + 4, y + 74, "ROL MAIN", DIM, 12)
    d.text((W - M - 28, y + hh - 30), "SMOGG", font=font(16, True), fill=GOLD_DIM, anchor="rm")

    # ---- B. combate ----
    y += hh + GAP
    th = 140
    tw = (inner - 3 * GAP) / 4
    tiles = [("KILLS", _fmt(data["kills"]) if has_kb else "—", TEXT),
             ("MUERTES", _fmt(data["deaths"]) if has_kb else "—", TEXT),
             ("PELEAS", _fmt(data["attendance"]) if has_kb else "—", TEXT),
             ("KDA", f"{data['kda']:.2f}" if has_kb else "—", GOLD)]
    for i, (lab, val, col) in enumerate(tiles):
        x0 = M + i * (tw + GAP)
        card(img, (int(x0), y, int(x0 + tw), y + th), fill=PANEL, outline=GOLD_DIM if lab == "KDA" else BORDER)
        d = ImageDraw.Draw(img)
        _label(d, x0 + 24, y + 22, lab, MUTED, 14)
        d.text((x0 + 22, y + 52), val, font=_fit(d, val, 60, tw - 44), fill=col)

    # ---- D. engage + F. item power ----
    y += th + GAP
    bh = 210
    half = (inner - GAP) / 2
    xl, xr = M, M + half + GAP
    card(img, (xl, y, int(xl + half), y + bh), fill=PANEL)
    card(img, (int(xr), y, W - M, y + bh), fill=PANEL)
    d = ImageDraw.Draw(img)
    _label(d, xl + 24, y + 22, "PRIMER ENGAGE", GOLD, 15)
    if has_kb and data.get("attendance"):
        pct = float(data.get("first5_pct") or 0)
        rate, col = engage_rating(pct)
        d.text((xl + 22, y + 52), f"{pct:.0f}%", font=font(64, True), fill=col)
        pw = d.textlength(f"{pct:.0f}%", font=font(64, True))
        d.text((xl + 40 + pw, y + 66), "de muertes", font=font(18), fill=MUTED)
        d.text((xl + 40 + pw, y + 92), "en el primer engage", font=font(18), fill=MUTED)
        rf = font(14, True)
        rw = d.textlength(rate, font=rf) + 24
        d.rounded_rectangle((xl + half - 24 - rw, y + 18, xl + half - 24, y + 44), radius=13, fill=col)
        d.text((xl + half - 24 - rw / 2, y + 31), rate, font=rf, fill=BG, anchor="mm")
        _bar(d, xl + 24, y + 148, xl + half - 24, 12, pct / 100.0, col)
        d.text((xl + 24, y + 172), f"{data.get('first5', 0)} de {data.get('attendance', 0)} peleas analizadas",
               font=font(16), fill=MUTED)
    else:
        d.text((xl + 24, y + 80), "Sin killboards cargados", font=font(24, True), fill=DIM)

    _label(d, xr + 24, y + 22, "ITEM POWER", GOLD, 15)
    if has_kb and data.get("avg_ip"):
        avg, mx, mn = float(data["avg_ip"]), float(data.get("max_ip") or 0), float(data.get("min_ip") or 0)
        d.text((xr + 22, y + 52), _fmt(avg), font=font(64, True), fill=TEXT)
        aw = d.textlength(_fmt(avg), font=font(64, True))
        d.text((xr + 40 + aw, y + 78), "promedio", font=font(18), fill=MUTED)
        x0, x1 = xr + 24, W - M - 24
        _bar(d, x0, y + 148, x1, 12, 1.0, GOLD_DIM)
        if mx > mn:
            ax_ = x0 + (x1 - x0) * (avg - mn) / (mx - mn)
            d.ellipse((ax_ - 9, y + 145, ax_ + 9, y + 163), fill=GOLD, outline=BG, width=3)
        d.text((x0, y + 172), f"MÍN {_fmt(mn)}", font=font(16, True), fill=MUTED)
        d.text((x1, y + 172), f"MÁX {_fmt(mx)}", font=font(16, True), fill=MUTED, anchor="ra")
    else:
        d.text((xr + 24, y + 80), "Sin datos de IP", font=font(24, True), fill=DIM)

    # ---- E. roles + G. actividad ----
    y += bh + GAP
    rh = 290
    card(img, (xl, y, int(xl + half), y + rh), fill=PANEL)
    card(img, (int(xr), y, W - M, y + rh), fill=PANEL)
    d = ImageDraw.Draw(img)
    _label(d, xl + 24, y + 22, "ROLES MÁS JUGADOS", GOLD, 15)
    roles = data.get("roles") or []
    if roles:
        ry = y + 58
        for i, (name_, pct_, n_) in enumerate(roles[:3]):
            col = GOLD if i == 0 else (TEXT if i == 1 else MUTED)
            d.text((xl + 24, ry), name_, font=font(20, True), fill=col)
            d.text((xl + half - 24, ry), f"{pct_:.0f}%  ·  {n_}", font=font(18, True), fill=col, anchor="ra")
            _bar(d, xl + 24, ry + 32, xl + half - 24, 10, pct_ / 100.0, GOLD if i == 0 else (120, 124, 138))
            ry += 60
    else:
        d.text((xl + 24, y + 80), "Todavía sin CTA cerradas", font=font(22, True), fill=DIM)
    # rol main: cuánto lo juega
    if main and main.get("total"):
        mp = 100.0 * main["n"] / main["total"]
        d.line((xl + 24, y + rh - 62, xl + half - 24, y + rh - 62), fill=BORDER, width=1)
        d.text((xl + 24, y + rh - 40), f"Main {main['name']}: {mp:.0f}% de sus CTA ({main['n']} de {main['total']})",
               font=font(16, True), fill=GOLD if mp >= 50 else RED)

    _label(d, xr + 24, y + 22, "ACTIVIDAD EN CTA", GOLD, 15)
    tot = data.get("cta_total")
    if tot is not None:
        d.text((xr + 22, y + 52), _fmt(tot), font=font(64, True), fill=TEXT)
        tw_ = d.textlength(_fmt(tot), font=font(64, True))
        d.text((xr + 40 + tw_, y + 78), "asistencias totales", font=font(18), fill=MUTED)
        wk, wn = int(data.get("cta_week") or 0), int(data.get("cta_week_n") or 0)
        d.text((xr + 24, y + 140), "ESTA SEMANA", font=font(14, True), fill=MUTED)
        d.text((W - M - 24, y + 136), f"{wk} de {wn}", font=font(20, True), fill=TEXT, anchor="ra")
        _bar(d, xr + 24, y + 166, W - M - 24, 10, (wk / wn) if wn else 0.0, GREEN)
    else:
        d.text((xr + 24, y + 80), "Sin datos de asistencia", font=font(22, True), fill=DIM)
    # skipeadas / looteado
    sy = y + rh - 62
    d.line((xr + 24, sy, W - M - 24, sy), fill=BORDER, width=1)
    colw = (W - M - 24 - (xr + 24)) / 2
    _label(d, xr + 24, sy + 14, "CTAS SKIPEADAS", MUTED, 12)
    d.text((xr + 24, sy + 32), _fmt(data.get("skips") or 0), font=font(20, True), fill=RED if data.get("skips") else TEXT)
    _label(d, xr + 24 + colw, sy + 14, "TOTAL LOOTEADO", MUTED, 12)
    d.text((xr + 24 + colw, sy + 32), _fmt(data.get("looted") or 0), font=font(20, True), fill=TEXT)

    # ---- pie ----
    y += rh + 18
    d.text((M, y), "Smogg · datos de los killboards cargados por el staff", font=font(14), fill=DIM)
    img = img.crop((0, 0, W, y + 30))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
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
                    av = await asyncio.to_thread(_circle_avatar, raw, 104)
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
