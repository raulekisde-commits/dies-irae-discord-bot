"""Blacklist: /blacklist agregar, /blacklist sacar, /blacklist listar.
Agregan Staff y Council (y administradores); sacar, solo Council (y administradores).
Al abrir un ticket de reclutamiento se avisa en el ticket si el personaje o la cuenta de Discord están en la lista."""
import asyncio
import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import blacklist_db as bdb
from guildshop import can_manage

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
NO_MENTIONS = discord.AllowedMentions.none()


def can_add(member) -> bool:
    return can_manage(member) or any(getattr(r, "id", None) == STAFF_ROLE_ID for r in getattr(member, "roles", []))


def can_remove(member) -> bool:
    return can_manage(member)


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return 0


def entry_label(r: dict) -> str:
    if r["kind"] == "discord":
        return f"🆔 Discord <@{r['value']}> (`{discord.utils.escape_markdown(r['display'])}`)"
    return f"⚔️ Albion **{discord.utils.escape_markdown(r['display'])}**"


def entry_line(r: dict) -> str:
    return (f"• {entry_label(r)} — {discord.utils.escape_markdown(r['reason'])} "
            f"· agregado por <@{r['added_by']}> el <t:{_unix(r['created_at'])}:d>")


def ticket_warning_embed(matches) -> discord.Embed:
    """Aviso visible para todos en el ticket de reclutamiento."""
    e = discord.Embed(title="⛔ POSTULANTE EN BLACKLIST",
                      description="\n".join(entry_line(r) for r in matches)[:4000], color=discord.Color.red())
    e.set_footer(text="Revisar con el Council antes de aceptar")
    return e


async def _say(interaction, text):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


class BlacklistCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        bdb.init_schema(db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    bl_g = app_commands.Group(name="blacklist", description="Personajes de Albion y cuentas de Discord vetadas", guild_only=True)

    @bl_g.command(name="agregar", description="(Staff/Council) Agrega un personaje de Albion y/o una cuenta de Discord a la blacklist")
    @app_commands.describe(motivo="Por qué se lo blacklistea", personaje="Nombre del personaje en Albion",
                           usuario="Cuenta de Discord (mención o ID)")
    async def bl_agregar(self, interaction: discord.Interaction, motivo: str, personaje: str = None, usuario: discord.User = None):
        if not can_add(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden agregar a la blacklist.")
        if not personaje and usuario is None:
            return await _say(interaction, "Indicá un `personaje` de Albion, un `usuario` de Discord, o ambos.")
        ok, errs = [], []
        for kind, value, display in (("albion", personaje, personaje), ("discord", getattr(usuario, "id", None), str(usuario))):
            if value is None or value == "":
                continue
            try:
                ok.append(await self.run(bdb.add_entry, kind, value, display, motivo, interaction.user.id))
            except bdb.BlacklistError as exc:
                errs.append(f"❌ {exc}")
        lines = [f"✅ {entry_label(r)} agregado a la blacklist por {interaction.user.mention}." for r in ok] + errs
        await _say(interaction, "\n".join(lines))

    @bl_g.command(name="sacar", description="(Council) Saca un personaje de Albion y/o una cuenta de Discord de la blacklist")
    @app_commands.describe(personaje="Nombre del personaje en Albion", usuario="Cuenta de Discord (mención o ID)")
    async def bl_sacar(self, interaction: discord.Interaction, personaje: str = None, usuario: discord.User = None):
        if not can_remove(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede sacar de la blacklist.")
        if not personaje and usuario is None:
            return await _say(interaction, "Indicá un `personaje` de Albion, un `usuario` de Discord, o ambos.")
        lines = []
        for kind, value in (("albion", personaje), ("discord", getattr(usuario, "id", None))):
            if value is None or value == "":
                continue
            row = await self.run(bdb.remove_entry, kind, value)
            lines.append(f"🗑️ Se sacó {entry_label(row)} (motivo era: {discord.utils.escape_markdown(row['reason'])})." if row
                         else f"ℹ️ `{discord.utils.escape_markdown(str(personaje if kind == 'albion' else usuario))}` no está en la blacklist.")
        await _say(interaction, "\n".join(lines))

    @bl_g.command(name="listar", description="(Staff/Council) Lista la blacklist")
    async def bl_listar(self, interaction: discord.Interaction):
        if not can_add(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden ver la blacklist.")
        rows = await self.run(bdb.all_entries)
        if not rows:
            return await _say(interaction, "La blacklist está vacía.")
        chunks, cur = [], f"**Blacklist ({len(rows)})**\n"
        for ln in map(entry_line, rows):
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        for c in chunks:
            await _say(interaction, c)


async def setup_blacklist(bot, db_path: str, guild_id: int) -> BlacklistCog:
    cog = BlacklistCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
