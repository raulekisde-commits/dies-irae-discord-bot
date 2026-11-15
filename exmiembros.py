"""Ex-miembros de la guild: /exmiembros listar · /exmiembros ver, detección automática de salidas y aviso en el ticket de reclutamiento.

Usa la lista de la guild que ya baja RegistrationCog (no hace pedidos propios a la API de Albion)."""
import asyncio
import os
import traceback
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import exmiembros_db as edb
from dbutil import utcnow_iso
from guildshop import can_manage

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
RECRUITER_ROLE_ID = int(os.getenv("RECRUITER_ROLE_ID", "1257896905099444354"))
GUILD_NAME = os.getenv("ALBION_GUILD_NAME", "Smogg")
NO_MENTIONS = discord.AllowedMentions.none()
SOURCES = {"salida": "detectado por el bot", "killboard": "killboards", "registro": "registro del bot", "reclutamiento": "reclutamiento"}


def can_view(member) -> bool:
    return can_manage(member) or any(getattr(r, "id", None) in (STAFF_ROLE_ID, RECRUITER_ROLE_ID) for r in getattr(member, "roles", []))


def _unix(iso) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return 0


def _date(iso) -> str:
    u = _unix(iso)
    return f"<t:{u}:d>" if u else "fecha desconocida"


def ex_line(r: dict) -> str:
    left = f"se fue {_date(r['left_at'])}" if r.get("left_at") else (f"visto por última vez {_date(r['last_seen'])}" if r.get("last_seen")
                                                                  else "fecha de salida desconocida")
    extra = [f"se fue {r['times_left']} veces"] if (r.get("times_left") or 0) > 1 else []
    if r.get("rejoined_at"):
        extra.append(f"volvió {_date(r['rejoined_at'])}")
    if r.get("discord_id"):
        extra.append(f"<@{r['discord_id']}>")
    extra.append(SOURCES.get(r.get("source"), r.get("source") or "?"))
    return f"• **{discord.utils.escape_markdown(r['name'])}** — {left} · " + " · ".join(extra)


def ticket_embed(matches) -> discord.Embed:
    e = discord.Embed(title="🔁 EX-MIEMBRO: QUIERE VOLVER A ENTRAR", color=discord.Color.orange(),
                      description="Ya estuvo en la guild:\n" + "\n".join(ex_line(r) for r in matches)[:3800])
    prev = [r for r in matches if r.get("reason_left") or r.get("reason_back")]
    for r in prev[:2]:
        e.add_field(name=f"Respuestas anteriores ({r['name']})"[:250],
                    value=f"**Se fue:** {r.get('reason_left') or '—'}\n**Vuelve:** {r.get('reason_back') or '—'}"[:1000], inline=False)
    return e


async def _say(interaction, text):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


class ExAnswerModal(discord.ui.Modal, title="Volver a Smogg"):
    se_fue = discord.ui.TextInput(label="¿Por qué te fuiste de Smogg?", style=discord.TextStyle.paragraph, max_length=1000)
    vuelve = discord.ui.TextInput(label="¿Por qué querés volver?", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog: "ExMembersCog"):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        left, back = str(self.se_fue).strip(), str(self.vuelve).strip()
        await self.cog.run(edb.save_answers, interaction.channel.id, left, back)
        e = discord.Embed(title=f"📝 Respuestas de {interaction.user}", color=discord.Color.orange())
        e.add_field(name="¿Por qué se fue?", value=left[:1000] or "—", inline=False)
        e.add_field(name="¿Por qué vuelve?", value=back[:1000] or "—", inline=False)
        await interaction.response.send_message(embed=e, allowed_mentions=NO_MENTIONS)


class ExAnswerView(discord.ui.View):
    """Botón persistente del ticket: solo el postulante responde."""

    def __init__(self, cog: "ExMembersCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Responder", style=discord.ButtonStyle.primary, emoji="📝", custom_id="exm:responder")
    async def responder(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = await self.cog.run(edb.ticket_user, interaction.channel.id)
        if uid != interaction.user.id:
            return await _say(interaction, "🔒 Solo el postulante puede responder.")
        await interaction.response.send_modal(ExAnswerModal(self.cog))


class ExMembersCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._roster_at = None
        edb.init_schema(db_path)

    async def cog_load(self):
        self.bot.add_view(ExAnswerView(self))
        if not self.roster_watch.is_running():
            self.roster_watch.start()

    async def cog_unload(self):
        self.roster_watch.cancel()

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    # ---------- detección de salidas ----------
    async def _log(self, text: str):
        reg = self.bot.get_cog("RegistrationCog")
        try:
            ch = await reg.log_channel() if reg is not None else None
            if ch is not None:
                await ch.send(text[:1900], allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def process_roster(self, roster) -> None:
        """roster = [(char_id, nombre), ...] de la guild. La primera vez reconstruye el pasado; después anota quién se fue."""
        if await self.run(edb.get_meta, edb.BACKFILL_KEY) is None:
            cands = await self.run(edb.collect_candidates, GUILD_NAME)
            added = await self.run(edb.backfill, roster, cands)
            await self.run(edb.set_meta, edb.BACKFILL_KEY, utcnow_iso())
            await self._log(f"📚 Ex-miembros: reconstruí el historial con **{added}** personas que ya no están en la guild "
                            "(killboards, registros y reclutamientos). Desde ahora se anota sola cada salida.")
        res = await self.run(edb.sync_roster, roster)
        if res is None:
            return await self._log("⚠️ Ex-miembros: la lista de la guild vino con demasiadas bajas de golpe; no anoté salidas esta vez.")
        left, _back = res
        if left:
            await self._log("📤 Se fueron de la guild: " + ", ".join(f"**{discord.utils.escape_markdown(n)}**" for n in left))

    @tasks.loop(minutes=10)
    async def roster_watch(self):
        try:
            reg = self.bot.get_cog("RegistrationCog")
            if reg is None or not reg.roster_fresh() or reg.roster_at == self._roster_at:
                return
            self._roster_at = reg.roster_at
            await self.process_roster([(c.char_id, c.name) for c in reg.roster.values()])
        except Exception:
            traceback.print_exc()

    @roster_watch.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    # ---------- comandos ----------
    ex_g = app_commands.Group(name="exmiembros", description="Gente que estuvo en la guild y se fue", guild_only=True)

    @ex_g.command(name="listar", description="(Staff/Council/Reclutador) Lista los ex-miembros de la guild")
    async def ex_listar(self, interaction: discord.Interaction):
        if not can_view(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los reclutadores pueden ver los ex-miembros.")
        rows = [r for r in await self.run(edb.all_members) if not r.get("rejoined_at")]
        if not rows:
            return await _say(interaction, "No hay ex-miembros guardados todavía.")
        chunks, cur = [], f"**Ex-miembros ({len(rows)})**\n"
        for ln in map(ex_line, rows):
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        for c in chunks[:10]:
            await _say(interaction, c)

    @ex_g.command(name="ver", description="(Staff/Council/Reclutador) ¿Estuvo en la guild? Busca por personaje o cuenta de Discord")
    @app_commands.describe(personaje="Nombre del personaje en Albion", usuario="Cuenta de Discord")
    async def ex_ver(self, interaction: discord.Interaction, personaje: str = None, usuario: discord.User = None):
        if not can_view(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los reclutadores pueden ver los ex-miembros.")
        if not personaje and usuario is None:
            return await _say(interaction, "Indicá un `personaje` o un `usuario`.")
        rows = await self.run(edb.find_matches, getattr(usuario, "id", None), [personaje or ""])
        if not rows:
            return await _say(interaction, "ℹ️ No figura como ex-miembro.")
        out = []
        for r in rows:
            out.append(ex_line(r))
            if r.get("reason_left") or r.get("reason_back"):
                out.append(f"  ↳ **Se fue:** {r.get('reason_left') or '—'} · **Vuelve:** {r.get('reason_back') or '—'}")
        await _say(interaction, "\n".join(out)[:1900])

    # ---------- ticket de reclutamiento ----------
    async def ticket_matches(self, user_id: int, nick: str):
        return await self.run(edb.find_matches, user_id, [nick or ""])

    async def post_ticket_question(self, channel, user, matches) -> None:
        await self.run(edb.link_ticket, channel.id, user.id, [r["id"] for r in matches])
        await channel.send(
            content=(f"{user.mention} vemos que **ya estuviste en Smogg**. Antes de seguir, contanos:\n"
                     "• **¿Por qué te fuiste?**\n• **¿Por qué querés volver?**\nTocá **Responder** (o escribilo acá en el ticket)."),
            view=ExAnswerView(self), allowed_mentions=discord.AllowedMentions(users=[user]))


async def setup_exmiembros(bot, db_path: str, guild_id: int) -> ExMembersCog:
    cog = ExMembersCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
