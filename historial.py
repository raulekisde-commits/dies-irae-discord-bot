"""/miembros historial: guilds por las que pasó un personaje y con quién comparte kills, consultando la API de Albion en vivo.

No guarda nada: cada uso consulta gameinfo (búsqueda, jugador, kills, top kills del mes y muertes) y arma el resumen.
La API solo devuelve eventos recientes, así que el historial es parcial (lo que se vio en esos eventos).
Permiso: Staff, Council (o dueño/administrador) y Reclutador. Respuesta oculta salvo publico:True."""
import asyncio
import traceback
from collections import Counter
from typing import Dict, List, Optional

import discord
from discord import app_commands

from guildshop import can_manage
from registration import GAMEINFO_BASES, HTTP_TIMEOUT, STAFF_ROLE_ID

RECRUITER_ROLE_ID = 1257896905099444354
NO_MENTIONS = discord.AllowedMentions.none()
MAX_COMPANIONS = 15
MAX_GUILD_ROWS = 10
REGION_CHOICES = [app_commands.Choice(name=r, value=r) for r, _b in GAMEINFO_BASES]


def can_use(member) -> bool:
    return isinstance(member, discord.Member) and (
        can_manage(member) or any(getattr(r, "id", None) in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in member.roles))


def _gname(p: Optional[dict]) -> str:
    return str((p or {}).get("GuildName") or "").strip()


def _day(ts: str) -> str:
    return (ts or "")[:10] or "?"


def guild_segments(player_id: str, kills: List[dict], deaths: List[dict]) -> List[dict]:
    """Guild que tenía el jugador en cada evento, en orden cronológico, agrupada en tramos consecutivos."""
    seen = []
    for ev in kills + deaths:
        ts = str(ev.get("TimeStamp") or "")
        who = None
        for p in [ev.get("Killer"), ev.get("Victim")] + list(ev.get("Participants") or []):
            if isinstance(p, dict) and str(p.get("Id")) == player_id:
                who = p
                break
        if who is not None and ts:
            seen.append((ts, _gname(who) or "Sin guild"))
    seen.sort()
    segs: List[dict] = []
    for ts, g in seen:
        if segs and segs[-1]["guild"] == g:
            segs[-1]["to"] = ts
            segs[-1]["events"] += 1
        else:
            segs.append({"guild": g, "from": ts, "to": ts, "events": 1})
    return segs


def companions(player_id: str, kills: List[dict]):
    """Quiénes aparecen junto al jugador en sus kills (participantes y grupo), con la guild más reciente de cada uno."""
    counts: Counter = Counter()
    info: Dict[str, dict] = {}
    for ev in sorted(kills, key=lambda e: str(e.get("TimeStamp") or "")):
        ids = set()
        for key in ("Participants", "GroupMembers"):
            for p in ev.get(key) or []:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("Id") or "")
                if not pid or pid == player_id or pid in ids:
                    continue
                ids.add(pid)
                info[pid] = {"name": str(p.get("Name") or "?"), "guild": _gname(p) or "Sin guild"}
        for pid in ids:
            counts[pid] += 1
    top = [(info[pid]["name"], info[pid]["guild"], n) for pid, n in counts.most_common()]
    by_guild: Counter = Counter()
    for _n, g, n in top:
        by_guild[g] += n
    return top, by_guild


def _esc(s: str) -> str:
    return discord.utils.escape_markdown(s)


def build_embed(player: dict, region: str, kills: List[dict], deaths: List[dict]) -> discord.Embed:
    pid = str(player.get("Id"))
    segs = guild_segments(pid, kills, deaths)
    top, by_guild = companions(pid, kills)
    current = _gname(player) or "Sin guild"
    e = discord.Embed(title=f"Historial de {_esc(str(player.get('Name') or '?'))}", color=discord.Color.gold())
    e.description = f"Servidor: **{region}** · Guild actual: **{_esc(current)}**"
    if segs:
        rows = [f"• **{_esc(s['guild'])}** — {_day(s['from'])} → {_day(s['to'])} ({s['events']} ev.)"
                for s in reversed(segs[-MAX_GUILD_ROWS:])]
        e.add_field(name="Guilds vistas (más reciente primero)", value="\n".join(rows)[:1024], inline=False)
    else:
        e.add_field(name="Guilds vistas", value="Sin eventos recientes en la API.", inline=False)
    if top:
        rows = [f"`{n:>3}` {_esc(name)} · {_esc(g)}" for name, g, n in top[:MAX_COMPANIONS]]
        e.add_field(name="Comparte kills con", value="\n".join(rows)[:1024], inline=False)
        rows = [f"`{n:>3}` {_esc(g)}" for g, n in by_guild.most_common(MAX_GUILD_ROWS)]
        e.add_field(name="Por guild (asistencias sumadas)", value="\n".join(rows)[:1024], inline=False)
    else:
        e.add_field(name="Comparte kills con", value="Sin kills recientes en la API.", inline=False)
    stamps = sorted(str(ev.get("TimeStamp") or "") for ev in kills + deaths if ev.get("TimeStamp"))
    rango = f" · {_day(stamps[0])} → {_day(stamps[-1])}" if stamps else ""
    e.set_footer(text=f"API de Albion en vivo: {len(kills)} kills y {len(deaths)} muertes recientes{rango}. Historial parcial.")
    return e


class Historial:
    """Consultas en vivo; el subcomando vive en guildcheck.py (/miembros historial)."""

    def __init__(self, bot):
        self.bot = bot

    async def fetch_json(self, url: str, params: Optional[dict] = None):
        import aiohttp
        session = getattr(self.bot, "http_session", None)
        own = session is None
        if own:
            session = aiohttp.ClientSession()
        try:
            async with session.get(url, params=params, headers={"User-Agent": "SmoggBot/1.0", "Accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        finally:
            if own:
                await session.close()

    async def _search(self, name: str, region: Optional[str]):
        bases = [(r, b) for r, b in GAMEINFO_BASES if region is None or r == region]

        async def one(r, b):
            data = await self.fetch_json(f"{b}/api/gameinfo/search", {"q": name})
            players = data.get("players", []) if isinstance(data, dict) else []
            return [(r, b, p) for p in players if isinstance(p, dict) and str(p.get("Name", "")).casefold() == name.casefold()]

        res = await asyncio.gather(*(asyncio.wait_for(one(r, b), HTTP_TIMEOUT + 5) for r, b in bases), return_exceptions=True)
        found = [x for r in res if not isinstance(r, Exception) for x in r]
        errors = [f"{r}: {type(x).__name__}" for (r, _b), x in zip(bases, res) if isinstance(x, Exception)]
        return found, errors

    async def _list(self, url: str, params: Optional[dict] = None) -> List[dict]:
        try:
            data = await asyncio.wait_for(self.fetch_json(url, params), HTTP_TIMEOUT + 5)
            return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        except Exception as exc:
            print(f"⚠️ /miembros historial: {url} falló: {type(exc).__name__} {exc}")
            return []

    async def run(self, interaction: discord.Interaction, nombre: str, servidor: Optional[str], publico: bool):
        if not can_use(interaction.user):
            return await interaction.response.send_message("No tenés permisos (solo Staff, Council y Reclutador).", ephemeral=True)
        await interaction.response.defer(ephemeral=not publico, thinking=True)
        try:
            found, errors = await self._search(nombre.strip(), servidor)
            if not found:
                msg = (f"No pude consultar la API de Albion ({', '.join(errors)})." if errors
                       else f"No encontré ningún personaje llamado **{_esc(nombre)}**.")
                return await interaction.followup.send(msg, allowed_mentions=NO_MENTIONS)
            if len(found) > 1:
                regs = ", ".join(sorted({r for r, _b, _p in found}))
                return await interaction.followup.send(f"Hay más de un **{_esc(nombre)}** ({regs}). Elegí el `servidor`.",
                                                       allowed_mentions=NO_MENTIONS)
            region, base, hit = found[0]
            pid = str(hit.get("Id"))
            api = f"{base}/api/gameinfo/players/{pid}"
            player, kills, top, deaths = await asyncio.gather(
                self.fetch_json(api), self._list(f"{api}/kills"),
                self._list(f"{api}/topkills", {"range": "month", "offset": 0, "limit": 50}), self._list(f"{api}/deaths"),
                return_exceptions=True)
            if not isinstance(player, dict):
                player = hit
            kills = kills if isinstance(kills, list) else []
            deaths = deaths if isinstance(deaths, list) else []
            ids = {ev.get("EventId") for ev in kills}
            kills += [ev for ev in (top if isinstance(top, list) else []) if ev.get("EventId") not in ids]
            await interaction.followup.send(embed=build_embed(player, region, kills, deaths), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("❌ Error consultando la API de Albion. Probá de nuevo en un rato.", allowed_mentions=NO_MENTIONS)

