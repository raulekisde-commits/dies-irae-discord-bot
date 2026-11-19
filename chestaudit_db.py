"""Auditoría del chest después de una CTA: combina los combatlogs (loot logger), los compara con el log del chest
y dice qué quedó sin depositar. También guarda lo looteado / dropeado por jugador para /stats jugador.

Reglas (ver claude/diseno-auditoria-chest-cta.md):
- Solo se auditan los looters de las guilds de AUDIT_GUILDS (Smogg y Smogged). No cuentan: Trash, mementos de kill
  (FURNITUREITEM_KILLTROPHY), juguetes/tesoros (TREASURE_*), monturas T3 o menos, Siege Hammer que no sean T8 y
  bolsos que no sean 8.1 a 8.4.
- Tolerancias para taguear: Major Gigantify Potion solo si faltan más de 25; comidas solo si faltan más de 3;
  monturas T4-T6 (sumadas) solo si faltan 4 o más.
- Ítem = nombre + encantamiento (la calidad no viene en el loot logger). Las pociones se comparan solo por nombre.
- Los archivos de distintos loggers se combinan sin duplicar (mismo evento visto por dos personas, ±2 s).
- Muerte = el jugador aparece como `looted_from`. Lo que había looteado antes de morir:
    · si alguien lo saca de su cuerpo, deja de debérselo (si lo saca un auditado, pasa a debérselo ese);
    · si nadie lo saca (no se ve), se le perdona.
  En los dos casos cuenta como «dropeado» para la estadística.
- Chest: los retiros no se procesan y solo valen los depósitos de la última hora del log (desde el último registro).
"""
import csv
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from dbutil import connect, write_tx, utcnow_iso

AUDIT_GUILDS = tuple(g.strip() for g in os.getenv("CHEST_AUDIT_GUILDS", "Smogg,Smogged").split(",") if g.strip())
DEDUPE_SECONDS = 2.0                       # mismo evento en dos combatlogs distintos
DEATH_GAP_SECONDS = 120.0                  # dos loots del mismo cuerpo separados por más de esto = otra muerte
ALIVE_SECONDS = 60.0                       # si la «víctima» lootea algo en este lapso después, no murió (no se toma como muerte)
CHEST_WINDOW = timedelta(hours=1)          # depósitos válidos: [último registro − 1 h, último registro]
# /stats: los promedios de loot por CTA solo cuentan las CTA creadas desde que se empezó a usar la auditoría.
STATS_SINCE = os.getenv("CHEST_STATS_SINCE", "2026-11-19T06:30:00+00:00")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chest_audits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    cta_id      INTEGER NOT NULL,
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    loot_files  INTEGER NOT NULL DEFAULT 0,
    loot_events INTEGER NOT NULL DEFAULT 0,
    chest_from  TEXT,
    chest_to    TEXT,
    tagged_by   INTEGER,
    tagged_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_chest_audits_cta ON chest_audits(cta_id);
CREATE TABLE IF NOT EXISTS chest_audit_players (
    audit_id     INTEGER NOT NULL,
    cta_id       INTEGER NOT NULL,
    name_norm    TEXT NOT NULL,
    name         TEXT NOT NULL,
    guild        TEXT,
    looted       INTEGER NOT NULL DEFAULT 0,      -- unidades looteadas
    dropped      INTEGER NOT NULL DEFAULT 0,      -- unidades looteadas que perdió al morir
    missing      INTEGER NOT NULL DEFAULT 0,      -- unidades sin depositar
    missing_json TEXT NOT NULL DEFAULT '[]',
    filtered     INTEGER NOT NULL DEFAULT 0,      -- 1 = solo le faltaba lo tolerado (sin tag)
    PRIMARY KEY (audit_id, name_norm),
    FOREIGN KEY (audit_id) REFERENCES chest_audits(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chest_audit_players_name ON chest_audit_players(name_norm);
CREATE TABLE IF NOT EXISTS chest_audit_posts (      -- mensaje general del canal de tags (se borra cuando se soluciona todo)
    audit_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS chest_audit_tags (       -- un mensaje (con hilo y botón Solucionado) por persona tagueada
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id    INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    thread_id   INTEGER,
    name_norm   TEXT NOT NULL,
    name        TEXT NOT NULL,
    discord_id  INTEGER,
    title       TEXT,
    missing     INTEGER NOT NULL DEFAULT 0,
    resolved_by INTEGER,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chest_audit_tags_msg ON chest_audit_tags(message_id);
CREATE TABLE IF NOT EXISTS chest_audit_losses (     -- lo looteado que se perdió al morir (para estadísticas)
    audit_id  INTEGER NOT NULL,
    cta_id    INTEGER NOT NULL,
    name_norm TEXT NOT NULL,
    name      TEXT NOT NULL,
    item_name TEXT NOT NULL,
    enchant   INTEGER,
    qty       INTEGER NOT NULL,
    looted_at TEXT,
    died_at   TEXT,
    picked_by TEXT,                               -- quién lo sacó del cuerpo; NULL = nadie visto (perdonado)
    FOREIGN KEY (audit_id) REFERENCES chest_audits(id) ON DELETE CASCADE
);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def norm(name: str) -> str:
    """Igual que killboard_db.norm (así los nombres cruzan con los killboards)."""
    return " ".join(str(name or "").split()).casefold()


def _aud(guild: str) -> bool:
    g = norm(guild)
    return any(g == norm(a) for a in AUDIT_GUILDS)


# ---------- ítems ----------
def enchant_of(item_id: str) -> int:
    _, _, tail = str(item_id or "").partition("@")
    try:
        return int(tail) if tail else 0
    except ValueError:
        return 0


def item_key(name: str, enchant: Optional[int]) -> Tuple[str, Optional[int]]:
    """Clave de comparación. Pociones: solo el nombre (el chest las muestra siempre con encantamiento 0)."""
    n = norm(name)
    return (n, None) if "potion" in n else (n, int(enchant or 0))


def is_food(item_id: str) -> bool:
    return "_MEAL_" in str(item_id or "").upper()


# ---------- loot logger ----------
@dataclass
class LootEvent:
    ts: datetime
    by_guild: str
    by_name: str
    item_id: str
    item_name: str
    qty: int
    from_guild: str
    from_name: str
    sources: set = field(default_factory=set)

    @property
    def enchant(self) -> int:
        return enchant_of(self.item_id)

    @property
    def key(self):
        return item_key(self.item_name, self.enchant)


LOOT_HEADER = "looted_by__name"


def _iso_ts(text: str) -> Optional[datetime]:
    t = str(text or "").strip()
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_loot_log(text: str) -> bool:
    first = (text or "").lstrip("﻿").split("\n", 1)[0]
    return LOOT_HEADER in first


def parse_loot_log(text: str) -> List[LootEvent]:
    """Archivo del loot logger (`timestamp_utc;looted_by__alliance;...`). Las líneas que no se entienden se saltean."""
    text = (text or "").lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or LOOT_HEADER not in lines[0]:
        return []
    delim = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    rows = list(csv.reader(lines, delimiter=delim))
    head = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(head)}
    need = ("timestamp_utc", "looted_by__name", "item_id", "item_name", "quantity", "looted_from__name")
    if any(k not in idx for k in need):
        return []

    def g(row, k):
        i = idx.get(k)
        return row[i].strip() if i is not None and i < len(row) else ""

    out: List[LootEvent] = []
    for row in rows[1:]:
        ts = _iso_ts(g(row, "timestamp_utc"))
        name = g(row, "looted_by__name")
        if ts is None or not name or not g(row, "item_id"):
            continue
        try:
            qty = int(g(row, "quantity") or 1)
        except ValueError:
            qty = 1
        out.append(LootEvent(ts, g(row, "looted_by__guild"), name, g(row, "item_id"), g(row, "item_name") or g(row, "item_id"),
                             max(qty, 0), g(row, "looted_from__guild"), g(row, "looted_from__name")))
    return out


def merge_logs(files: Iterable[List[LootEvent]]) -> List[LootEvent]:
    """Combina varios combatlogs. Un evento que ya vino en OTRO archivo (misma persona, ítem, cantidad y cuerpo, ±2 s)
    no se duplica; dentro de un mismo archivo nunca se descarta nada (dos loots iguales seguidos son reales)."""
    merged: List[LootEvent] = []
    by_key: Dict[tuple, List[LootEvent]] = {}
    for fi, events in enumerate(files):
        for e in events:
            k = (norm(e.by_name), e.item_id.upper(), e.qty, norm(e.from_name))
            hit = None
            for c in by_key.get(k, ()):
                if fi not in c.sources and abs((c.ts - e.ts).total_seconds()) <= DEDUPE_SECONDS:
                    hit = c
                    break
            if hit is not None:
                hit.sources.add(fi)
                continue
            ne = LootEvent(e.ts, e.by_guild, e.by_name, e.item_id, e.item_name, e.qty, e.from_guild, e.from_name, {fi})
            merged.append(ne)
            by_key.setdefault(k, []).append(ne)
    merged.sort(key=lambda e: e.ts)
    return merged


# ---------- chest ----------
@dataclass
class ChestEntry:
    ts: datetime
    player: str
    item: str
    enchant: int
    quality: int
    amount: int

    @property
    def key(self):
        return item_key(self.item, self.enchant)


def parse_chest_log(text: str) -> List[ChestEntry]:
    """Log del chest copiado del juego (tab, entre comillas, encabezado repetido cada 100 filas). Fechas en UTC."""
    out: List[ChestEntry] = []
    for row in csv.reader(io.StringIO((text or "").lstrip("﻿")), delimiter="\t"):
        row = [c.strip().strip('"') for c in row]
        if len(row) < 6 or row[0].lower() == "date":
            continue
        try:
            ts = datetime.strptime(row[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
            out.append(ChestEntry(ts, row[1], row[2], int(row[3] or 0), int(row[4] or 0), int(row[5] or 0)))
        except ValueError:
            continue
    return out


def merge_chest(files: Iterable[List[ChestEntry]]) -> List[ChestEntry]:
    """Combina varias subidas del log del chest. Una fila idéntica (misma hora, persona, ítem, calidad y cantidad) que aparece
    en dos subidas cuenta una sola vez; si un mismo archivo la tiene repetida, se respeta cuántas veces (de cada fila vale
    el máximo de veces que aparece en una sola subida)."""
    best: Dict[tuple, int] = {}
    rows: Dict[tuple, ChestEntry] = {}
    for entries in files:
        count: Dict[tuple, int] = {}
        for e in entries:
            k = (e.ts, norm(e.player), norm(e.item), e.enchant, e.quality, e.amount)
            count[k] = count.get(k, 0) + 1
            rows.setdefault(k, e)
        for k, n in count.items():
            best[k] = max(best.get(k, 0), n)
    out = [rows[k] for k, n in best.items() for _ in range(n)]
    out.sort(key=lambda e: e.ts, reverse=True)
    return out


def valid_deposits(entries: List[ChestEntry]) -> Tuple[List[ChestEntry], Optional[datetime], Optional[datetime]]:
    """Depósitos que cuentan (sin retiros, dentro de la última hora del log) y la ventana usada."""
    if not entries:
        return [], None, None
    last = max(e.ts for e in entries)
    start = last - CHEST_WINDOW
    return sorted((e for e in entries if e.amount > 0 and e.ts >= start), key=lambda e: e.ts), start, last


# ---------- auditoría ----------
@dataclass
class Lot:
    key: tuple
    item_name: str
    item_id: str
    qty: int
    ts: datetime
    from_name: str


@dataclass
class PlayerResult:
    name: str
    guild: str
    looted: int = 0
    dropped: int = 0
    lots: List[Lot] = field(default_factory=list)        # lo looteado que todavía tiene encima (sin depositar ni perder)
    missing: List[dict] = field(default_factory=list)      # faltantes que cuentan (se taguean)
    tolerated: List[dict] = field(default_factory=list)    # faltantes dentro de la tolerancia (no se taguean)
    filtered: bool = False                                 # solo tenía tolerados

    @property
    def missing_units(self) -> int:
        return sum(m["qty"] for m in self.missing)


@dataclass
class Loss:
    name: str
    item_name: str
    enchant: Optional[int]
    qty: int
    looted_at: datetime
    died_at: datetime
    picked_by: Optional[str]


def _death_clusters(events: List[LootEvent]) -> Dict[str, List[Tuple[datetime, datetime]]]:
    """{víctima_norm: [(inicio, fin)]}: cada grupo de loots de su cuerpo es una muerte."""
    times: Dict[str, List[datetime]] = {}
    for e in events:
        if e.from_name and not e.from_name.startswith("@"):
            times.setdefault(norm(e.from_name), []).append(e.ts)
    out: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for v, ts in times.items():
        ts.sort()
        cl = [[ts[0], ts[0]]]
        for t in ts[1:]:
            if (t - cl[-1][1]).total_seconds() > DEATH_GAP_SECONDS:
                cl.append([t, t])
            else:
                cl[-1][1] = t
        out[v] = [(a, b) for a, b in cl]
    looters: Dict[str, List[datetime]] = {}
    for e in events:
        looters.setdefault(norm(e.by_name), []).append(e.ts)
    for v, cl in out.items():                         # siguió looteando enseguida: estaba vivo, no es una muerte
        mine = looters.get(v, [])
        out[v] = [(a, b) for a, b in cl if not any(a < t <= a + timedelta(seconds=ALIVE_SECONDS) for t in mine)]
    return {v: cl for v, cl in out.items() if cl}


def compute(events: List[LootEvent], deposits: List[ChestEntry]) -> Tuple[Dict[str, PlayerResult], List[Loss]]:
    """Recorre todo en orden de tiempo: loots (suman a lo que lleva encima), depósitos (lo descuentan) y muertes
    (lo que llevaba encima se pierde: lo saca alguien del cuerpo o se perdona). Lo que queda encima al final = faltante."""
    players: Dict[str, PlayerResult] = {}
    losses: List[Loss] = []
    deaths = _death_clusters(events)
    timeline = [(e.ts, 1, i) for i, e in enumerate(events)]
    timeline += [(d.ts, 0, i) for i, d in enumerate(deposits)]                     # mismo segundo: primero el depósito
    timeline += [(end, 2, (v, start)) for v, cl in deaths.items() for start, end in cl]  # fin de una muerte
    timeline.sort(key=lambda x: (x[0], x[1]))

    def lose(pr: PlayerResult, lot: Lot, qty: int, died: datetime, picked: Optional[str]):
        lot.qty -= qty
        pr.dropped += qty
        losses.append(Loss(pr.name, lot.item_name, lot.key[1], qty, lot.ts, died, picked))

    for ts, kind, ref in timeline:
        if kind == 0:                                     # depósito en el chest
            d = deposits[ref]
            pr = players.get(norm(d.player))
            left = d.amount
            for lot in (pr.lots if pr else []):
                if left <= 0:
                    break
                if lot.qty > 0 and lot.key == d.key and lot.ts <= d.ts:
                    take = min(lot.qty, left)
                    lot.qty -= take
                    left -= take
            if pr:
                pr.lots = [l for l in pr.lots if l.qty > 0]
        elif kind == 2:                                   # terminó una muerte: lo que nadie sacó del cuerpo se perdona
            v, died = ref
            pr = players.get(v)
            if pr:
                for lot in pr.lots:
                    if lot.qty > 0 and lot.ts < died:
                        lose(pr, lot, lot.qty, died, None)
                pr.lots = [l for l in pr.lots if l.qty > 0]
        else:                                             # loot
            e = events[ref]
            victim = players.get(norm(e.from_name))
            died = next((s for s, en in deaths.get(norm(e.from_name), []) if s <= e.ts <= en), None)
            if victim and victim.lots and died is not None:   # sacó del cuerpo algo que la víctima había looteado
                left = e.qty
                for lot in victim.lots:
                    if left <= 0:
                        break
                    if lot.qty > 0 and lot.key == e.key and lot.ts < died:
                        take = min(lot.qty, left)
                        left -= take
                        lose(victim, lot, take, died, e.by_name)
                victim.lots = [l for l in victim.lots if l.qty > 0]
            if not _aud(e.by_guild) or e.qty <= 0:
                continue
            pr = players.setdefault(norm(e.by_name), PlayerResult(e.by_name, e.by_guild))
            pr.looted += e.qty
            pr.lots.append(Lot(e.key, e.item_name, e.item_id, e.qty, e.ts, e.from_name))
    return players, losses


# ---------- filtros antes de taguear ----------
GIGANTIFY = "major gigantify potion"
GIGANTIFY_TOLERANCE = 25                   # Major Gigantify Potion: solo cuenta si faltan más de 25
FOOD_TOLERANCE = 3                         # comidas (todas juntas): solo cuentan si faltan más de 3
MOUNT_TOLERANCE = 3                        # monturas T4-T6 (todas juntas): solo cuentan si faltan 4 o más


def is_trash(item_id: str) -> bool:
    return "_TRASH" in str(item_id or "").upper()


def is_bag(item_id: str) -> bool:
    """Bolsos (T4_BAG, T6_BAG@1, T7_BAG_INSIGHT…)."""
    base = str(item_id or "").upper().split("@", 1)[0]
    return "_BAG" in base and base[:1] == "T"


def tier_of(item_id: str) -> Optional[int]:
    head = str(item_id or "").upper().split("_", 1)[0]
    return int(head[1:]) if head[:1] == "T" and head[1:].isdigit() else None


def is_mount(item_id: str) -> bool:
    """Monturas: T6_MOUNT_DIREWOLF, T5_MOUNT_COUGAR_KEEPER@1, UNIQUE_MOUNT_BAT_PERSONAL…"""
    return "_MOUNT" in str(item_id or "").upper()


def is_low_mount(item_id: str) -> bool:
    """Monturas T4, T5 y T6 (tienen tolerancia)."""
    return is_mount(item_id) and tier_of(item_id) in (4, 5, 6)


def ignored(item_id: str) -> bool:
    """Ítems que no cuentan para nada: Trash, mementos de kill (FURNITUREITEM_KILLTROPHY), juguetes/tesoros (TREASURE_*:
    Silver Mirror, Ritual Candles…), monturas T3 o menos, Siege Hammer que no sean T8 y bolsos que no sean 8.1 a 8.4."""
    iid = str(item_id or "").upper()
    if is_trash(iid) or "FURNITUREITEM_KILLTROPHY" in iid or iid.startswith("TREASURE_"):
        return True
    if is_mount(iid) and (tier_of(iid) or 99) <= 3:
        return True
    if "SIEGEHAMMER" in iid and tier_of(iid) != 8:          # Siege Hammer: solo cuentan los T8
        return True
    if is_bag(iid):
        return not (iid.split("_", 1)[0] == "T8" and enchant_of(iid) in (1, 2, 3, 4))
    return False


def split_tolerated(items: List[dict]) -> Tuple[List[dict], List[dict]]:
    """(faltantes que cuentan, tolerados). Gigantify ≤ 25, comidas ≤ 3 y monturas T4-T6 ≤ 3 (cada grupo sumado) se toleran."""
    gig = [m for m in items if norm(m["item"]) == GIGANTIFY]
    food = [m for m in items if m.get("food")]
    tol = []
    if gig and sum(m["qty"] for m in gig) <= GIGANTIFY_TOLERANCE:
        tol += gig
    if food and sum(m["qty"] for m in food) <= FOOD_TOLERANCE:
        tol += food
    mounts = [m for m in items if is_low_mount(m.get("item_id"))]
    if mounts and sum(m["qty"] for m in mounts) <= MOUNT_TOLERANCE:
        tol += mounts
    return [m for m in items if m not in tol], tol


def audit(loot_files: List[List[LootEvent]], chest: List[ChestEntry]) -> dict:
    events = [e for e in merge_logs(loot_files) if not ignored(e.item_id)]      # Trash, mementos y bolsos no T8.1-8.4
    deposits, start, last = valid_deposits(chest)
    players, losses = compute(events, deposits)
    for pr in players.values():
        agg: Dict[tuple, dict] = {}
        for lot in pr.lots:
            m = agg.setdefault(lot.key, {"item": lot.item_name, "item_id": lot.item_id, "enchant": lot.key[1], "qty": 0,
                                         "food": is_food(lot.item_id)})
            m["qty"] += lot.qty
        items = sorted(agg.values(), key=lambda m: (-m["qty"], m["item"]))
        pr.missing, pr.tolerated = split_tolerated(items)
        pr.filtered = not pr.missing and bool(pr.tolerated)
    return {"events": events, "players": players, "losses": losses, "chest_from": start, "chest_to": last,
            "chest_rows": len(chest)}


def item_label(m: dict) -> str:
    ench = m.get("enchant")
    return f"{m['qty']}× {m['item']}" + (f" .{ench}" if ench else "")


def report_text(result: dict, title: str) -> str:
    """Reporte completo en texto (se adjunta como archivo)."""
    pl = sorted(result["players"].values(), key=lambda p: (-p.missing_units, norm(p.name)))
    lines = [f"Auditoría de chest — {title}",
             f"Eventos de loot (sin duplicados): {len(result['events'])}",
             f"Depósitos válidos: {result['chest_from']:%Y-%m-%d %H:%M:%S} → {result['chest_to']:%Y-%m-%d %H:%M:%S} UTC"
             if result.get("chest_to") else "Chest log vacío", ""]
    lines.append("== Faltantes ==")
    for p in pl:
        if not p.missing:
            continue
        lines.append(f"{p.name} [{p.guild}] — looteó {p.looted}, dropeó {p.dropped}, falta {p.missing_units}")
        lines += [f"   - {item_label(m)}" for m in p.missing]
        if p.tolerated:
            lines.append("   tolerado: " + ", ".join(item_label(m) for m in p.tolerated))
    lines += ["", "== Depositaron todo (o solo falta lo tolerado) =="]
    for p in pl:
        if not p.missing:
            tol = f" · tolerado: {', '.join(item_label(m) for m in p.tolerated)}" if p.tolerated else ""
            lines.append(f"{p.name} — looteó {p.looted}, dropeó {p.dropped}{tol}")
    lines += ["", "== Perdido al morir =="]
    for l in result["losses"]:
        who = f"lo sacó {l.picked_by}" if l.picked_by else "nadie lo sacó (perdonado)"
        lines.append(f"{l.name}: {l.qty}× {l.item_name}{f' .{l.enchant}' if l.enchant else ''} · murió {l.died_at:%H:%M:%S} · {who}")
    return "\n".join(lines) + "\n"


# ---------- persistencia ----------
def save_audit(db_path: str, guild_id: int, cta_id: int, actor_id: int, result: dict, loot_files: int) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute(
                "INSERT INTO chest_audits(guild_id, cta_id, created_by, created_at, loot_files, loot_events, chest_from, chest_to)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, cta_id, actor_id, utcnow_iso(), loot_files, len(result["events"]),
                 result["chest_from"].isoformat() if result.get("chest_from") else None,
                 result["chest_to"].isoformat() if result.get("chest_to") else None))
            aid = int(cur.lastrowid)
            for k, p in result["players"].items():
                conn.execute("INSERT INTO chest_audit_players(audit_id, cta_id, name_norm, name, guild, looted, dropped, missing,"
                             " missing_json, filtered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (aid, cta_id, k, p.name, p.guild, p.looted, p.dropped, p.missing_units,
                              json.dumps(p.missing, ensure_ascii=False), int(p.filtered)))
            for l in result["losses"]:
                conn.execute("INSERT INTO chest_audit_losses(audit_id, cta_id, name_norm, name, item_name, enchant, qty, looted_at,"
                             " died_at, picked_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (aid, cta_id, norm(l.name), l.name, l.item_name, l.enchant, l.qty, l.looted_at.isoformat(),
                              l.died_at.isoformat(), l.picked_by))
        return aid
    finally:
        conn.close()


def mark_tagged(db_path: str, audit_id: int, actor_id: int) -> bool:
    """Marca la auditoría como tagueada. False si ya lo estaba (evita taguear dos veces)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE chest_audits SET tagged_by=?, tagged_at=? WHERE id=? AND tagged_at IS NULL",
                               (actor_id, utcnow_iso(), audit_id))
            return cur.rowcount > 0
    finally:
        conn.close()


def discord_ids_by_name(db_path: str, names: Iterable[str]) -> Dict[str, int]:
    """{name_norm: discord_id} según los personajes registrados (albion_links)."""
    want = {norm(n) for n in names}
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT discord_id, char_name FROM albion_links").fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {norm(r["char_name"]): int(r["discord_id"]) for r in rows if norm(r["char_name"]) in want}


def loot_averages(db_path: str, names_norm: Iterable[str]) -> Tuple[float, float, int]:
    """(promedio looteado, promedio dropeado, CTAs) por CTA. Cuenta toda CTA creada desde STATS_SINCE con killboard
    cargado (kb_battles.cta_id) en la que aparece el jugador; si no hay auditoría o no looteó, esa CTA suma 0. De cada CTA vale la última auditoría."""
    names = [n for n in names_norm if n]
    if not names:
        return 0.0, 0.0, 0
    q = ",".join("?" * len(names))
    conn = connect(db_path)
    try:
        try:
            ctas = {int(r[0]) for r in conn.execute(
                f"SELECT DISTINCT b.cta_id FROM kb_players p JOIN kb_battles b ON b.battle_id=p.battle_id"
                f" JOIN cta_runs c ON c.id=b.cta_id"
                f" WHERE b.cta_id IS NOT NULL AND c.created_at >= ? AND p.name_norm IN ({q})", [STATS_SINCE] + names)}
        except Exception:
            ctas = set()
        if not ctas:
            return 0.0, 0.0, 0
        cq = ",".join("?" * len(ctas))
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM(p.looted),0), COALESCE(SUM(p.dropped),0) FROM chest_audit_players p"
                f" WHERE p.name_norm IN ({q}) AND p.cta_id IN ({cq})"
                f" AND p.audit_id = (SELECT MAX(a.id) FROM chest_audits a WHERE a.cta_id=p.cta_id)",
                names + sorted(ctas)).fetchone()
        except Exception:                              # todavía no hay auditorías (tablas sin crear)
            row = (0, 0)
        n = len(ctas)
        return float(row[0]) / n, float(row[1]) / n, n
    finally:
        conn.close()


# ---------- tags en el canal ----------
def set_general(db_path: str, audit_id: int, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO chest_audit_posts(audit_id, channel_id, message_id) VALUES (?, ?, ?)",
                         (audit_id, channel_id, message_id))
    finally:
        conn.close()


def add_tag(db_path: str, audit_id: int, guild_id: int, channel_id: int, message_id: int, name: str,
            discord_id: Optional[int], title: str, missing: int) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO chest_audit_tags(audit_id, guild_id, channel_id, message_id, name_norm, name, discord_id,"
                               " title, missing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (audit_id, guild_id, channel_id, message_id, norm(name), name, discord_id, title, missing))
            return int(cur.lastrowid)
    finally:
        conn.close()


def set_tag_thread(db_path: str, tag_id: int, thread_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE chest_audit_tags SET thread_id=? WHERE id=?", (thread_id, tag_id))
    finally:
        conn.close()


def tag_by_message(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM chest_audit_tags WHERE message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def resolve_tag(db_path: str, tag_id: int, actor_id: int) -> Optional[dict]:
    """Marca el tag como solucionado (una sola vez). Devuelve {'pending': tags sin solucionar de esa auditoría,
    'general': mensaje general si quedó en 0 y hay que borrarlo} o None si ya estaba solucionado."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE chest_audit_tags SET resolved_by=?, resolved_at=? WHERE id=? AND resolved_at IS NULL",
                               (actor_id, utcnow_iso(), tag_id))
            if not cur.rowcount:
                return None
            aid = conn.execute("SELECT audit_id FROM chest_audit_tags WHERE id=?", (tag_id,)).fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM chest_audit_tags WHERE audit_id=? AND resolved_at IS NULL", (aid,)).fetchone()[0]
            general = None
            if not pending:
                g = conn.execute("SELECT channel_id, message_id FROM chest_audit_posts WHERE audit_id=? AND deleted_at IS NULL",
                                 (aid,)).fetchone()
                if g:
                    general = (int(g[0]), int(g[1]))
                    conn.execute("UPDATE chest_audit_posts SET deleted_at=? WHERE audit_id=?", (utcnow_iso(), aid))
            return {"pending": int(pending), "general": general}
    finally:
        conn.close()
