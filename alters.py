"""Sistema de alters: /alter agregar, /alter eliminar, /alter ver, /alter listar.
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

    alter_g = app_commands.Group(name="alter", description="Alters enlazados a personas del Discord", guild_only=True)

    @alter_g.command(name="agregar", description="(Staff/Council/Admin) Enlaza una cuenta alter a una persona del Discord")
    @app_commands.describe(usuario="Persona dueña del alter", alter="Nombre del personaje alter en Albion",
                           nota="Descripción opcional (p. ej. alter de crafteo)")
    @app_commands.rename(alter="personaje", nota="descripcion")
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

    @alter_g.command(name="eliminar", description="(Staff/Council/Admin) Elimina un alter enlazado")
    @app_commands.describe(alter="Nombre exacto del alter")
    @app_commands.rename(alter="personaje")
    async def alter_quitar(self, interaction: discord.Interaction, alter: str):
        if not can_alter(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los administradores pueden eliminar alters.")
        row = await self.run(adb.remove_alter, alter)
        if not row:
            return await _say(interaction, "ℹ️ Ese alter no está enlazado.")
        await _say(interaction, f"🗑️ Se eliminó **{discord.utils.escape_markdown(row['alter_name'])}** (era alter de <@{row['user_id']}>, "
                                f"lo había autorizado <@{row['added_by']}>).")

    @alter_g.command(name="ver", description="(Staff/Council/Admin) Alters de una persona, o de quién es un alter")
    @app_commands.describe(usuario="Persona del Discord", alter="O el nombre de un alter para saber de quién es")
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

    @alter_g.command(name="listar", description="(Staff/Council/Admin) Lista todos los alters enlazados")
    async def alter_listar(self, interaction: discord.Interaction):
        if not can_alter(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los administradores pueden ver los alters.")
        rows = await self.run(adb.all_alters)
        if not rows:
            return await _say(interaction, "No hay alters enlazados.")
        lines = [f"• <@{r['user_id']}> → " + alter_lines([r])[0][2:] for r in rows]
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        await _say(interaction, f"**Alters ({len(rows)})**\n" + chunks[0])
        for c in chunks[1:]:
            await _say(interaction, c)


async def setup_alters(bot, db_path: str, guild_id: int) -> AltersCog:
    cog = AltersCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
