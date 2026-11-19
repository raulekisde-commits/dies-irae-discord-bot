"""Imagen de lo que alguien no depositó en el chest (grilla de íconos con cantidad), para los tags de /chest auditar.
Los íconos salen del render oficial de Albion con la caché en disco de buildcard (se bajan una sola vez)."""
import io
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from PIL import Image, ImageDraw

import buildcard as bc

COLS = 6
TILE = 120                     # ancho de cada casilla
ICON = 96
TILE_H = 150                   # ícono + nombre (2 líneas)
PAD = 24
HEAD_H = 78
MAX_ICONS = 30                 # más que esto: la última casilla dice «+N»


def _name_lines(d, text: str, width: int, f) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == 2:
            break
    if cur and len(lines) < 2:
        lines.append(cur)
    if len(lines) == 2 and " ".join(lines) != text:
        while lines[1] and d.textlength(lines[1] + "…", font=f) > width:
            lines[1] = lines[1][:-1]
        lines[1] += "…"
    return lines


def render(player: str, items: List[dict], title: str = "", icons: Optional[dict] = None) -> bytes:
    """`items`: [{item, item_id, enchant, qty}] ordenados. `icons`: {item_id: Image} ya bajados (si falta, se dibuja el cuadro)."""
    icons = icons or {}
    shown = items[:MAX_ICONS] if len(items) <= MAX_ICONS else items[:MAX_ICONS - 1]
    extra = len(items) - len(shown)
    n = len(shown) + (1 if extra else 0)
    rows = max(1, (n + COLS - 1) // COLS)
    W = PAD * 2 + COLS * TILE
    H = HEAD_H + rows * TILE_H + PAD
    img = Image.new("RGBA", (W, H), bc.BG + (255,))
    bc.card(img, (8, 8, W - 8, H - 8), radius=16)
    d = ImageDraw.Draw(img)
    total = sum(int(m["qty"]) for m in items)
    d.text((PAD, 18), player[:32], font=bc.font(28, True), fill=bc.TEXT)
    sub = f"No depositó {total} ítem(s)" + (f" · {title}" if title else "")
    d.text((PAD, 52), sub[:70], font=bc.font(16), fill=bc.MUTED)
    fq, fn = bc.font(18, True), bc.font(12)
    for i, m in enumerate(shown):
        x = PAD + (i % COLS) * TILE + (TILE - ICON) // 2
        y = HEAD_H + (i // COLS) * TILE_H
        box = icons.get(m.get("item_id") or "")
        if box is None:
            box = Image.new("RGBA", (ICON, ICON), bc.SLOT_BG + (255,))
        else:
            box = box.resize((ICON, ICON))
        img.paste(box, (x, y), box)
        q = f"x{m['qty']}"
        tw = d.textlength(q, font=fq)
        d.rounded_rectangle((x + ICON - tw - 12, y + ICON - 24, x + ICON - 2, y + ICON - 2), radius=6, fill=(0, 0, 0, 200))
        d.text((x + ICON - tw - 7, y + ICON - 23), q, font=fq, fill=bc.GOLD)
        label = m["item"] + (f" .{m['enchant']}" if m.get("enchant") else "")
        for j, ln in enumerate(_name_lines(d, label, TILE - 8, fn)):
            d.text((x + ICON // 2, y + ICON + 4 + j * 14), ln, font=fn, fill=bc.TEXT if j == 0 else bc.MUTED, anchor="ma")
    if extra:
        i = len(shown)
        x = PAD + (i % COLS) * TILE + (TILE - ICON) // 2
        y = HEAD_H + (i // COLS) * TILE_H
        d.rounded_rectangle((x, y, x + ICON, y + ICON), radius=10, fill=bc.SLOT_BG)
        d.text((x + ICON // 2, y + ICON // 2), f"+{extra}", font=bc.font(26, True), fill=bc.GOLD, anchor="mm")
        d.text((x + ICON // 2, y + ICON + 4), "ítems más", font=fn, fill=bc.MUTED, anchor="ma")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _icon(item_id: str):
    try:
        return bc.icon_box(item_id, ICON)
    except Exception:
        return None


def render_with_icons(player: str, items: List[dict], title: str = "") -> bytes:
    """Baja (o lee de la caché) los íconos que se van a mostrar y dibuja. Bloqueante: llamarla en un hilo."""
    ids = sorted({m.get("item_id") for m in items[:MAX_ICONS] if m.get("item_id")})
    with ThreadPoolExecutor(max_workers=6) as ex:
        icons = dict(zip(ids, ex.map(_icon, ids)))
    return render(player, items, title, {k: v for k, v in icons.items() if v is not None})
