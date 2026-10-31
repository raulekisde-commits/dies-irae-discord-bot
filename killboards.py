"""Killboards cargados por el staff (/killboard cargar|borrar|probar), tops (/top) y estadísticas de jugadores (/stats)."""
import asyncio
import os
import re
import time
import traceback
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import ctautil as cu
import ctavoice as cv
import killboard_db as kb
import registration_db as rdb
from dbutil import connect
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
GUILD_NAME = os.getenv("ALBION_GUILD_NAME", "Smogg")
REGION = os.getenv("ALBIONBB_REGION", "eu")
BASE_URL = f"https://api.albionbb.com/{REGION}"
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
CALLER_ROLE_ID = int(os.getenv("CALLER_ROLE_ID", "1559711827422617720"))
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
MAX_BATTLES_PER_LOAD = 15
USER_COOLDOWN = 10


def _roles(m) -> set:
    return {r.id for r in getattr(m, "roles", [])}


def is_staff_or_council(m) -> bool:
    return can_manage(m) or STAFF_ROLE_ID in _roles(m)


def is_loader_in_cta(m) -> bool:
    return is_staff_or_council(m) or CALLER_ROLE_ID in _roles(m)


def is_member(m) -> bool:
    return MIEMBRO_ROLE_ID in _roles(m) or is_staff_or_council(m)


def parse_ids(text: str) -> List[str]:
    """Acepta IDs, links de una batalla o links /battles/multi?ids=a,b,c. Sin repetidos y en orden."""
    t = (text or "").strip()
    m = re.search(r"[?&]ids=([^&\s]+)", t, re.I)
    if m:
        t = m.group(1)
    out: List[str] = []
    for x in re.findall(r"\d{5,}", t):
        if x not in out:
            out.append(x)
    return out


def extract_guild_names(data, guild_name: str) -> List[str]:
    """Nombres de la guild que figuran en cualquier parte de la ficha de la batalla (para la asistencia)."""
    gl = kb.norm(guild_name)
    found = {}

    def walk(o):
        if isinstance(o, dict):
            n = o.get("Name") or o.get("name")
            g = o.get("GuildName") or o.get("guildName")
            go = o.get("Guild") or o.get("guild")
            if not g and isinstance(go, dict):
                g = go.get("Name") or go.get("name")
            elif not g and isinstance(go, str):
                g = go
            if isinstance(n, str) and isinstance(g, str) and kb.norm(g) == gl:
                found.setdefault(kb.norm(n), n)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return list(found.values())


async def _say(interaction, text, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def find_cta_by_stats_thread(db_path: str, thread_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM cta_runs WHERE stats_thread_id=?", (thread_id,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def battle_exists(db_path: str, battle_id: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM kb_battles WHERE battle_id=?", (battle_id,)).fetchone() is not None
    finally:
        conn.close()


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, cog: "KillboardCog", battle_id: str, user_id: int):
        super().__init__(timeout=120)
        self.cog, self.battle_id, self.user_id, self.done = cog, battle_id, user_id, False
        ok = discord.ui.Button(label="Sí, borrar", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Esto no es tuyo.", ephemeral=True)
            return False
        return True

    async def _no(self, interaction):
        self.done = True
        await interaction.response.edit_message(content="Listo, no se borró nada.", view=None)

    async def _ok(self, interaction):
        if self.done:
            return await interaction.response.send_message("Ya fue procesado.", ephemeral=True)
        self.done = True
        row = await self.cog.run(self.cog._battle_row, self.battle_id)
        ok = await self.cog.run(kb.delete_battle, self.battle_id, interaction.user.id)
        await interaction.response.edit_message(content=("🗑️ Killboard borrado. Sus datos ya no cuentan en las stats." if ok else "Ese killboard ya no existe."), view=None)
        if ok and row and row.get("cta_id"):
            await self.cog.refresh_stats_panel(int(row["cta_id"]))


class KillboardCog(commands.Cog):
    killboard = app_commands.Group(name="killboard", description="Cargar y administrar killboards", guild_only=True)

    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._last_use: dict = {}

    async def cog_load(self):
        await asyncio.to_thread(kb.init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    def _battle_row(self, db_path: str, battle_id: str):
        conn = connect(db_path)
        try:
            r = conn.execute("SELECT * FROM kb_battles WHERE battle_id=?", (battle_id,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    # ---------- AlbionBB ----------
    async def fetch_json(self, url: str, params: Optional[dict] = None):
        import aiohttp
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SmoggBot/1.0)", "Accept": "application/json"}
        async with self.bot.http_session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def fetch_battle(self, battle_id: str) -> dict:
        events = await self.fetch_json(f"{BASE_URL}/battles/kills", {"ids": battle_id})
        if not isinstance(events, list):
            raise RuntimeError("la respuesta de kills no tiene el formato esperado")
        parsed = kb.parse_events(events, GUILD_NAME)
        try:
            detail = await self.fetch_json(f"{BASE_URL}/battles/{battle_id}")
            kb.add_attendees(parsed, extract_guild_names(detail, GUILD_NAME))
        except Exception as exc:                         # la ficha solo suma asistencia: si falla, se sigue con los eventos
            print(f"Killboard {battle_id}: no pude leer la ficha de la batalla: {exc!r}")
        return parsed

    # ---------- /killboard cargar ----------
    @killboard.command(name="cargar", description="Cargar killboards de AlbionBB (en el hilo de stats de una CTA, o sueltos)")
    @app_commands.describe(enlaces="Links o IDs de AlbionBB (varios separados por coma o espacio)")
    async def cargar(self, interaction: discord.Interaction, enlaces: str):
        me = interaction.user
        cta = await self.run(find_cta_by_stats_thread, interaction.channel.id) if interaction.channel else None
        if cta is not None:
            if not is_loader_in_cta(me):
                return await _say(interaction, "🔒 Solo el Staff, el Council y el Caller pueden cargar killboards.")
        elif not is_staff_or_council(me):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden cargar killboards sueltos. Para el de una CTA, usá el hilo de stats de esa CTA.")
        ids = parse_ids(enlaces)
        if not ids:
            return await _say(interaction, "❌ No encontré ningún ID de batalla en lo que mandaste.")
        if len(ids) > MAX_BATTLES_PER_LOAD:
            return await _say(interaction, f"❌ Máximo {MAX_BATTLES_PER_LOAD} batallas por vez.")
        await interaction.response.defer(ephemeral=True)
        cta_id = int(cta["id"]) if cta else None
        lines, loaded = [], 0
        for bid in ids:
            if await self.run(battle_exists, bid):
                lines.append(f"↪️ `{bid}` ya estaba cargado: lo salteé.")
                continue
            try:
                parsed = await self.fetch_battle(bid)
            except Exception as exc:
                lines.append(f"❌ `{bid}`: no pude leerlo de AlbionBB ({exc}).")
                continue
            if not parsed["players"]:
                lines.append(f"⚠️ `{bid}`: no encontré jugadores de **{GUILD_NAME}** (¿otro ID o servidor?). No se guardó.")
                continue
            try:
                res = await self.run(kb.save_battle, bid, cta_id, me.id, parsed)
            except Exception:
                traceback.print_exc()
                lines.append(f"❌ `{bid}`: error guardando.")
                continue
            if res == "dup":
                lines.append(f"↪️ `{bid}` ya estaba cargado: lo salteé.")
                continue
            loaded += 1
            warn = f" ⚠️ {parsed['ts_missing']} muerte(s) sin hora" if parsed["ts_missing"] else ""
            lines.append(f"✅ `{bid}`: {len(parsed['players'])} jugadores de {GUILD_NAME}, {parsed['smogg_deaths']} muertes propias.{warn}")
        await _say(interaction, "\n".join(lines))
        if cta_id is not None and loaded:
            await self.after_cta_load(interaction, cta_id)

    async def after_cta_load(self, interaction: discord.Interaction, cta_id: int):
        await self.refresh_stats_panel(cta_id)
        enc = self.bot.get_cog("EncargosCog")
        if enc is not None:
            try:
                await enc.complete_by_source(f"cta:{cta_id}:killboard", interaction.user.id)
            except Exception:
                traceback.print_exc()
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "📎 Killboard cargado. Las comparaciones con voz las ve el Staff.")
        try:
            text = await self.comparison_text(cta_id)
        except Exception:
            traceback.print_exc()
            return
        for chunk in _chunks(text):
            await _say(interaction, chunk)

    async def comparison_text(self, cta_id: int) -> str:
        regs = await self.run(rdb.all_registrations)
        name2id = {kb.norm(r.char_name): int(r.discord_id) for r in regs}
        kb_names = await self.run(kb.names_in_cta, cta_id)
        kb_ids = {name2id[n] for n in kb_names if n in name2id}
        unmapped = sorted(v for n, v in kb_names.items() if n not in name2id)
        voice = await self.run(cv.present_users, cta_id)
        signed = {int(p["user_id"]) for p in await self.run(cu.positions, cta_id)}
        voice_not_kb = sorted(voice - kb_ids)
        kb_not_voice = sorted(kb_ids - voice)
        topos = sorted(voice - signed - kb_ids)

        def block(title, ids):
            return f"{title} ({len(ids)})\n" + ("\n".join(f"• <@{u}>" for u in ids) if ids else "Nadie.")
        parts = [block("🎧 **En voz pero NO en el killboard**", voice_not_kb),
                 block("🗡️ **En el killboard pero NO estuvieron en voz**", kb_not_voice),
                 block("🕳️ **Topos** (en voz, sin anotarse y sin estar en el killboard)", topos)]
        if unmapped:
            parts.append("ℹ️ Sin Discord registrado (no se pudieron comparar): " + ", ".join(f"`{n}`" for n in unmapped[:40]))
        return "\n\n".join(parts)

    async def refresh_stats_panel(self, cta_id: int):
        """Actualiza el check «Killboard» del panel de stats de la CTA."""
        conn = connect(self.db_path)
        try:
            run = conn.execute("SELECT stats_channel_id, stats_message_id FROM cta_runs WHERE id=?", (cta_id,)).fetchone()
        finally:
            conn.close()
        if not run or not run["stats_message_id"]:
            return
        n = len(await self.run(kb.battles_of_cta, cta_id))
        value = f"✅ Cargado ({n} batalla{'s' if n != 1 else ''})" if n else "⏳ Sin cargar"
        try:
            ch = self.bot.get_channel(run["stats_channel_id"]) or await self.bot.fetch_channel(run["stats_channel_id"])
            msg = await ch.fetch_message(run["stats_message_id"])
            emb = msg.embeds[0]
            for i, f in enumerate(emb.fields):
                if f.name.startswith("🗡️"):
                    emb.set_field_at(i, name=f.name, value=value, inline=f.inline)
            await msg.edit(embed=emb, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"CTA #{cta_id}: no pude actualizar el panel de stats: {exc!r}")

    # ---------- /killboard borrar ----------
    @killboard.command(name="borrar", description="(Staff/Council) Borrar un killboard cargado para que deje de contar")
    @app_commands.describe(batalla="ID o link de la batalla")
    async def borrar(self, interaction: discord.Interaction, batalla: str):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden borrar killboards.")
        ids = parse_ids(batalla)
        if len(ids) != 1:
            return await _say(interaction, "❌ Indicá una sola batalla.")
        row = await self.run(self._battle_row, ids[0])
        if row is None:
            return await _say(interaction, "❌ Esa batalla no está cargada.")
        where = f"de la CTA #{row['cta_id']}" if row["cta_id"] else "suelto"
        await interaction.response.send_message(f"¿Borrar el killboard `{ids[0]}` ({where})? Dejan de contar sus kills, muertes y asistencias. Queda registrado quién lo borró.",
                                                view=ConfirmDeleteView(self, ids[0], interaction.user.id), ephemeral=True)

    # ---------- /killboard probar ----------
    @killboard.command(name="probar", description="(Staff/Council) Muestra qué datos trae AlbionBB para una batalla, sin guardar nada")
    @app_commands.describe(batalla="ID o link de la batalla")
    async def probar(self, interaction: discord.Interaction, batalla: str):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council.")
        ids = parse_ids(batalla)
        if len(ids) != 1:
            return await _say(interaction, "❌ Indicá una sola batalla.")
        await interaction.response.defer(ephemeral=True)
        try:
            events = await self.fetch_json(f"{BASE_URL}/battles/kills", {"ids": ids[0]})
        except Exception as exc:
            return await _say(interaction, f"❌ AlbionBB no respondió: {exc}")
        if not isinstance(events, list):
            return await _say(interaction, "❌ La respuesta no es una lista de eventos. Formato inesperado.")
        p = kb.parse_events(events, GUILD_NAME)
        with_ts = sum(1 for e in events if isinstance(e, dict) and e.get("TimeStamp"))
        with_ip = sum(1 for e in events if isinstance(e, dict) and isinstance(e.get("Victim"), dict) and e["Victim"].get("AverageItemPower"))
        firsts = sorted((v for v in p["players"].values() if v.get("death_rank")), key=lambda v: v["death_rank"])[:5]
        txt = (f"🔎 Batalla `{ids[0]}`\n• Eventos recibidos: **{len(events)}**\n• Con hora de muerte (TimeStamp): **{with_ts}**\n"
               f"• Víctimas con IP: **{with_ip}**\n• Jugadores de {GUILD_NAME}: **{len(p['players'])}** · muertes propias: **{p['smogg_deaths']}**\n"
               f"• Primeras muertes propias: " + (", ".join(f"{v['name']} ({(v['death_time'] or 'sin hora')[11:19]})" for v in firsts) or "—") +
               "\n\nSi los eventos son muchos menos que las muertes reales de la batalla, AlbionBB está recortando la lista: avisame antes de confiar en estos números.")
        await _say(interaction, txt)

    # ---------- tops ----------
    async def top_embed(self, key: str) -> discord.Embed:
        title, _field, _desc, unit = kb.TOP_KEYS[key]
        rows = await self.run(kb.top, key, 10)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (name, value, d) in enumerate(rows, start=1):
            v = f"{value:,.0f}".replace(",", ".")
            extra = f" ({d['first5_pct']:.0f}% de sus peleas)" if key == "first5" else ""
            lines.append(f"{medals.get(i, f'`{i:>2}.`')} **{discord.utils.escape_markdown(name)}** — {v} {unit}{extra}")
        emb = discord.Embed(title=f"{title} · {GUILD_NAME}", description="\n".join(lines) or "Todavía no hay killboards cargados.", color=discord.Color.gold())
        emb.set_footer(text="Solo cuentan los killboards cargados por el staff")
        return emb

    def _cooldown(self, user_id: int) -> bool:
        now = time.time()
        if now - self._last_use.get(user_id, 0.0) < USER_COOLDOWN:
            return True
        self._last_use[user_id] = now
        return False

    async def _allowed(self, interaction, command: str) -> bool:
        """Permisos por defecto (Miembro/Invitado, cualquier canal) editables con /perms-canal y /perms-comando."""
        eco_cog = self.bot.get_cog("EconomyCog")
        if eco_cog is not None:
            return await eco_cog.slash_authorize(interaction, command)
        if not is_member(interaction.user):
            await _say(interaction, "❌ Este comando es solo para miembros de la guild.")
            return False
        return True

    async def send_ranking(self, interaction: discord.Interaction, key: str):
        """Lo usa `/top tipo` para los rankings que salen de los killboards cargados."""
        if not await self._allowed(interaction, "top"):
            return
        if self._cooldown(interaction.user.id):
            return await _say(interaction, "⏳ Esperá unos segundos antes de volver a usarlo.")
        await interaction.response.send_message(embed=await self.top_embed(key), allowed_mentions=NO_MENTIONS)

    # ---------- /stats ----------
    @app_commands.command(name="stats", description="Estadísticas de un jugador (si no indicás a nadie, las tuyas)")
    @app_commands.describe(usuario="Jugador (por defecto, vos)")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if not await self._allowed(interaction, "stats"):
            return
        target = usuario or interaction.user
        regs = await self.run(rdb.get_registrations, target.id)
        if not regs:
            return await _say(interaction, f"{target.mention} no tiene un personaje de Albion registrado.")
        names = [kb.norm(r.char_name) for r in regs]
        s = await self.run(kb.player_stats, names)
        emb = discord.Embed(title=f"📊 Stats · {', '.join(r.char_name for r in regs)}", color=discord.Color.gold())
        try:
            emb.set_thumbnail(url=target.display_avatar.url)
        except Exception:
            pass
        if not s["attendance"]:
            emb.description = f"{target.mention}\n\nTodavía no aparece en ningún killboard cargado."
        else:
            emb.description = target.mention
            emb.add_field(name="⚔️ Kills", value=f"**{s['kills']:,}**".replace(",", "."), inline=True)
            emb.add_field(name="💀 Muertes", value=f"**{s['deaths']:,}**".replace(",", "."), inline=True)
            emb.add_field(name="🛡️ Asistencia", value=f"**{s['attendance']:,}**".replace(",", ".") + " peleas", inline=True)
            emb.add_field(name="💥 Muertes en el primer engage", value=f"**{s['first5_pct']:.0f}%** · {s['first5']} de {s['attendance']} peleas", inline=False)
            counts = await self.run(kb.role_counts, target.id, names)
            total = sum(counts.values())
            if total:
                top3 = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
                medals = ["🥇", "🥈", "🥉"]
                lines = []
                for i, (rid, n) in enumerate(top3):
                    pct = 100.0 * n / total
                    bar = "█" * round(pct / 10) + "░" * (10 - round(pct / 10))
                    lines.append(f"{medals[i]} **{cu.PLAYED_ROLES.get(rid, rid)}** · {pct:.0f}%\n`{bar}`")
                emb.add_field(name="🎭 Roles más jugados", value="\n".join(lines), inline=False)
            if s["avg_ip"]:
                emb.add_field(name="📈 IP promedio", value=f"**{s['avg_ip']:.0f}**", inline=True)
                emb.add_field(name="⬆️ IP máxima", value=f"**{s['max_ip']:.0f}**", inline=True)
                emb.add_field(name="⬇️ IP mínima", value=f"**{s['min_ip']:.0f}**", inline=True)
        emb.set_footer(text=f"Smogg · datos de los killboards cargados por el staff")
        await interaction.response.send_message(embed=emb, allowed_mentions=NO_MENTIONS)


def _chunks(text: str, limit: int = 1800) -> List[str]:
    out, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def setup_killboards(bot, db_path: str, guild_id: int) -> KillboardCog:
    cog = KillboardCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
