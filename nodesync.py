"""Copia los timers de nodos/vortex de un panel que otro bot mantiene en otro servidor al panel de timers de este servidor.

El otro bot tiene UN mensaje que va editando («8.4 Ore - Redtree Enclave - en 28 minutos 14:57 UTC»). Cada pocos minutos se lee ese canal
(sin escuchar eventos: así el bot no reacciona a nada más de ese servidor), se interpretan las líneas y se cargan con el mismo mecanismo
que /timeradd. Cada timer queda a nombre de la persona dueña del otro bot, y no se vuelve a cargar si ya estaba o si alguien lo borró."""
import re
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

POLL_MINUTES = 2
SEEN_HOURS = 24
LOOKAHEAD_MAX_H = 20                  # un horario que ya pasó daría «mañana»; más lejos que esto es un timer que ya salió
TIERS_NORMAL = {"4.4", "5.4", "6.4", "7.4", "8.4"}
NODE_MATERIAL = {"ore": "mineral", "hide": "cuero", "wood": "madera", "fiber": "fibra", "fibre": "fibra"}
VORTEX_TIER = {"green": "common", "blue": "rare", "purple": "epic", "gold": "legendary", "yellow": "legendary"}

_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_MD_RE = re.compile(r"[*_`~|>]")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})\s*UTC", re.I)
_UNIX_RE = re.compile(r"<t:(\d{9,11})(?::[a-zA-Z])?>")
_NODE_RE = re.compile(r"^(?P<tier>\d\.\d)\s+(?P<mat>[A-Za-z]+)$")
_VORTEX_RE = re.compile(r"^(?P<color>[A-Za-z]+)\s+(?P<kind>vortex|core)$", re.I)


def message_text(msg) -> str:
    """Todo el texto visible de un mensaje: contenido más títulos, descripciones y campos de sus embeds."""
    parts = [getattr(msg, "content", "") or ""]
    for e in getattr(msg, "embeds", None) or []:
        parts.extend([getattr(e, "title", "") or "", getattr(e, "description", "") or ""])
        for f in getattr(e, "fields", None) or []:
            parts.extend([getattr(f, "name", "") or "", getattr(f, "value", "") or ""])
    return "\n".join(p for p in parts if p)


def parse_timers(text: str, now: Optional[datetime] = None) -> Tuple[List[dict], int]:
    """([{material, tier, mapa, spawn_at, clock}], líneas con pinta de timer que no se pudieron interpretar).
    Las líneas que no son timers (títulos, «14 active objective(s)») se ignoran sin contarse."""
    now = now or datetime.now(timezone.utc)
    out: List[dict] = []
    skipped = 0
    for raw in (text or "").splitlines():
        line = _EMOJI_RE.sub("", raw)
        unix = _UNIX_RE.search(line)
        line = _MD_RE.sub("", line).strip()
        line = re.sub(r"^[^\w<]+", "", line)
        clock_m = _CLOCK_RE.search(line)
        parts = [p.strip() for p in line.split(" - ")]
        if len(parts) < 3 or not (clock_m or unix):
            continue
        head, mapa = parts[0], parts[1]
        node, vortex = _NODE_RE.match(head), _VORTEX_RE.match(head)
        material = tier = None
        if node and node.group("mat").lower() in NODE_MATERIAL and node.group("tier") in TIERS_NORMAL:
            material, tier = NODE_MATERIAL[node.group("mat").lower()], node.group("tier")
        elif vortex and vortex.group("color").lower() in VORTEX_TIER:
            material, tier = vortex.group("kind").lower(), VORTEX_TIER[vortex.group("color").lower()]
        elif node or vortex:
            skipped += 1
            continue
        else:
            continue
        if not mapa:
            skipped += 1
            continue
        if unix:
            spawn = datetime.fromtimestamp(int(unix.group(1)), timezone.utc)
        else:
            h, m = int(clock_m.group(1)), int(clock_m.group(2))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                skipped += 1
                continue
            spawn = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if spawn <= now:
                spawn += timedelta(days=1)
            if spawn - now > timedelta(hours=LOOKAHEAD_MAX_H):
                continue                                   # ya salió hace un rato y el otro panel todavía lo lista
        out.append({"material": material, "tier": tier, "mapa": mapa[:60], "spawn_at": spawn, "clock": (spawn.hour, spawn.minute)})
    return out, skipped


def seen_key(t: dict) -> str:
    return f"{t['material']}|{t['tier']}|{t['mapa'].casefold()}|{t['clock'][0]:02d}:{t['clock'][1]:02d}"


def init_schema(db_path: str):
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS nodesync_seen (key TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
        conn.commit()


def filter_new(db_path: str, timers: List[dict], now: Optional[datetime] = None) -> List[dict]:
    """Se queda con los timers que todavía no se procesaron (y los marca): así uno borrado a mano no vuelve a aparecer."""
    now = now or datetime.now(timezone.utc)
    limit = (now - timedelta(hours=SEEN_HOURS)).isoformat()
    fresh = []
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("DELETE FROM nodesync_seen WHERE seen_at < ?", (limit,))
        for t in timers:
            k = seen_key(t)
            if conn.execute("SELECT 1 FROM nodesync_seen WHERE key=?", (k,)).fetchone():
                continue
            conn.execute("INSERT INTO nodesync_seen(key, seen_at) VALUES (?, ?)", (k, now.isoformat()))
            fresh.append(t)
        conn.commit()
    return fresh


class NodeSyncCog(commands.Cog):
    def __init__(self, bot, db_path: str, *, source_channel_id: int, source_bot_id: int, owner_id: int, target_guild_id: int,
                 find_entries: Callable, add_entry: Callable, category_of: Callable, refresh: Callable):
        self.bot, self.db_path = bot, db_path
        self.channel_id, self.source_bot_id, self.owner_id, self.guild_id = source_channel_id, source_bot_id, owner_id, target_guild_id
        self.find_entries, self.add_entry, self.category_of, self.refresh = find_entries, add_entry, category_of, refresh
        self._warned_empty = False
        self.poll.start()

    def clock(self) -> datetime:
        return datetime.now(timezone.utc)

    def cog_unload(self):
        self.poll.cancel()

    async def read_panel_text(self) -> Optional[str]:
        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            ch = await self.bot.fetch_channel(self.channel_id)
        texts = []
        async for msg in ch.history(limit=15):
            if msg.author.id == self.source_bot_id:
                texts.append(message_text(msg))
        return "\n".join(texts) if texts else None

    async def sync_once(self) -> int:
        """Lee el panel y carga lo nuevo. Devuelve cuántos timers agregó."""
        import asyncio
        text = await self.read_panel_text()
        if not text:
            return 0
        timers, skipped = parse_timers(text, self.clock())
        if not timers:
            if not self._warned_empty:
                self._warned_empty = True
                print(f"nodesync: el panel no tiene timers que entienda ({skipped} líneas descartadas). Primeros caracteres: {text[:200]!r}")
            return 0
        self._warned_empty = False
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return 0
        fresh = await asyncio.to_thread(filter_new, self.db_path, timers)
        added, cats = 0, set()
        for t in fresh:
            dup = await asyncio.to_thread(self.find_entries, self.guild_id, t["material"], t["mapa"], t["clock"], t["tier"])
            if dup:
                continue
            await asyncio.to_thread(self.add_entry, self.guild_id, t["material"], t["tier"], t["mapa"], t["spawn_at"], self.owner_id)
            cats.add(self.category_of(t["material"]))
            added += 1
        if cats:
            await self.refresh(guild, cats)
        return added

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self):
        try:
            n = await self.sync_once()
            if n:
                print(f"nodesync: {n} timer(s) nuevo(s) cargado(s) desde el panel externo")
        except discord.Forbidden:
            print("nodesync: no tengo permiso para leer el canal de nodos (Ver canal + Leer historial de mensajes).")
        except Exception:
            traceback.print_exc()

    @poll.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup_nodesync(bot, db_path: str, **kw) -> NodeSyncCog:
    init_schema(db_path)
    cog = NodeSyncCog(bot, db_path, **kw)
    await bot.add_cog(cog)
    return cog
