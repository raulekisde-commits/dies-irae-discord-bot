"""Sistema de alters: /alter-agregar, /alter-quitar, /alter-ver.
Solo Staff, Council y Administradores pueden agregar o quitar; queda guardado quién lo autorizó."""
import asyncio
import os
import traceback
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import alters_db as adb
from guildshop import can_manage

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
NO_MENTIONS = discord.AllowedMentions.none()


def can_alter(member) -> bool:
    """Staff, Council y Administradores (se valida siempre en el backend)."""
    return can_manage(member) or any(getattr(r, "id", None) == STAFF_ROLE_ID for r in getattr(member, "roles", []))


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return 0


async def _say(interaction, text):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


def alter_lines(rows) -> list:
    return [f"• **{discord.utils.escape_markdown(r['alter_name'])}** — autorizado por <@{r['added_by']}> el <t:{_unix(r['created_at'])}:d>"
            + (f" · {r['note']}" if r.get("note") else "") for r in rows]


class AltersCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        adb.init_schema(db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    @app_commands.command(name="alter-agregar", description="(Staff/Council/Admin) Enlaza una cuenta alter a una persona del Discord")
    @app_commands.describe(usuario="Persona dueña del alter", alter="Nombre de la cuenta / personaje alter", nota="Nota opcional")
    @app_commands.guild_only()
    async def alter_agregar(self, interaction: discord.Interaction, usuario: discord.Member, alter: str, nota: str = None):
        if not can_alter(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los administradores pueden agregar alters.")
        if usuario.bot:
            return await _say(interaction, "❌ No se puede enlazar un alter a un bot.")
        try:
            row = await self.run(adb.add_alter, usuario.id, alter, interaction.user.id, nota)
        except adb.AlterError as exc:
            return await _say(interaction, f"❌ {exc}")
        await _say(interaction, f"✅ **{discord.utils.escape_markdown(row['alter_name'])}** quedó enlazado como alter de {usuario.mention}.\n"
                                f"Autorizado por {interaction.user.mention}.")

    @app_commands.command(name="alter-quitar", description="(Staff/Council/Admin) Quita un alter enlazado")
    @app_commands.describe(alter="Nombre exacto del alter")
    @app_commands.guild_only()
    async def alter_quitar(self, interaction: discord.Interaction, alter: str):
        if not can_alter(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los administradores pueden quitar alters.")
        row = await self.run(adb.remove_alter, alter)
        if not row:
            return await _say(interaction, "ℹ️ Ese alter no está enlazado.")
        await _say(interaction, f"🗑️ Se quitó **{discord.utils.escape_markdown(row['alter_name'])}** (era alter de <@{row['user_id']}>, "
                                f"lo había autorizado <@{row['added_by']}>).")

    @app_commands.command(name="alter-ver", description="(Staff/Council/Admin) Alters de una persona, o de quién es un alter")
    @app_commands.describe(usuario="Persona del Discord", alter="O el nombre de un alter para saber de quién es")
    @app_commands.guild_only()
    async def alter_ver(self, interaction: discord.Interaction, usuario: discord.Member = None, alter: str = None):
        if not can_alter(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los administradores pueden ver los alters.")
        if usuario is None and not alter:
            return await _say(interaction, "Indicá una persona o el nombre de un alter.")
        if usuario is not None:
            rows = await self.run(adb.alters_of, usuario.id)
            if not rows:
                return await _say(interaction, f"{usuario.mention} no tiene alters enlazados.")
            return await _say(interaction, f"**Alters de {usuario.mention}**\n" + "\n".join(alter_lines(rows))[:1800])
        row = await self.run(adb.find_alter, alter)
        if not row:
            return await _say(interaction, "ℹ️ Ese alter no está enlazado a nadie.")
        await _say(interaction, f"**{discord.utils.escape_markdown(row['alter_name'])}** es alter de <@{row['user_id']}>\n" + "\n".join(alter_lines([row])))


async def setup_alters(bot, db_path: str, guild_id: int) -> AltersCog:
    cog = AltersCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
