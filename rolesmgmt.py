"""Gestión de roles de Observación y de CTA: /rol-dar, /rol-quitar, /rol-historial.

Quién puede qué (se valida siempre acá, no solo con permisos de Discord):
  • Observación — dar: Staff, Council, Admin, Coaching y Opinólogo · quitar: Staff, Council, Admin y Coaching.
  • Roles de CTA — dar: Staff, Council, Admin y Coaching · quitar: Staff, Council y Admin.
Cada cambio queda registrado con quién lo hizo."""
import asyncio
import os
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import roles_db as rdb
from guildshop import can_manage

OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
COACHING_ROLE_ID = int(os.getenv("COACHING_ROLE_ID", "1566691393726980167"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
# Roles de CTA que se pueden manejar (clave del desplegable -> (nombre, id))
CTA_ROLES = {
    "tank": ("Tank", 1260755129754189854),
    "healer": ("Healer", 1260755151296266331),
    "supp": ("Supp", 1260755342472646656),
    "dps": ("DPS", 1260755289062248458),
    "battle_mount": ("Battle Mount", 1469369363739181087),
    "flex": ("Flex", 1558522576743964762),
    "bomb": ("Bomb", int(os.getenv("BOMB_ROLE_ID", "1565982191945519194"))),
}
ROLE_CHOICES = {"observacion": ("Observación", OBSERVACION_ROLE_ID), **CTA_ROLES}
NO_MENTIONS = discord.AllowedMentions.none()


def _ids(m) -> set:
    return {getattr(r, "id", None) for r in getattr(m, "roles", [])}


def is_privileged(m) -> bool:
    """Staff, Council o Administrador."""
    return can_manage(m) or STAFF_ROLE_ID in _ids(m)


def can_change(member, key: str, action: str) -> bool:
    """¿Puede `member` dar/quitar el rol `key`? (`action` = 'dar' | 'quitar')"""
    if is_privileged(member):
        return key in ROLE_CHOICES
    ids = _ids(member)
    if key == "observacion":
        return bool({COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} & ids) if action == "dar" else COACHING_ROLE_ID in ids
    if key in CTA_ROLES:
        return action == "dar" and COACHING_ROLE_ID in ids
    return False


def can_use_any(member) -> bool:
    return is_privileged(member) or bool({COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} & _ids(member))


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return 0


async def _say(interaction, text):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


CHOICES = [app_commands.Choice(name=n, value=k) for k, (n, _) in ROLE_CHOICES.items()]


class RolesMgmtCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        rdb.init_schema(db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    async def _change(self, interaction: discord.Interaction, usuario: discord.Member, rol, motivo: Optional[str], action: str):
        key = getattr(rol, "value", rol)
        actor = interaction.user
        if key not in ROLE_CHOICES:
            return await _say(interaction, "❌ Ese rol no se puede manejar con este comando.")
        if not can_change(actor, key, action):
            verbo = "dar" if action == "dar" else "quitar"
            return await _say(interaction, f"🔒 No tenés permiso para {verbo} el rol **{ROLE_CHOICES[key][0]}**.")
        if usuario.bot:
            return await _say(interaction, "❌ No se puede cambiar roles a un bot.")
        if usuario.id == actor.id:
            return await _say(interaction, "❌ No podés cambiarte los roles a vos mismo.")
        name, rid = ROLE_CHOICES[key]
        guild = interaction.guild
        role = guild.get_role(rid)
        if role is None:
            return await _say(interaction, f"❌ No encuentro el rol **{name}** en el servidor.")
        has = rid in _ids(usuario)
        if action == "dar" and has:
            return await _say(interaction, f"ℹ️ {usuario.mention} ya tiene el rol **{name}**.")
        if action == "quitar" and not has:
            return await _say(interaction, f"ℹ️ {usuario.mention} no tiene el rol **{name}**.")
        me = getattr(guild, "me", None)
        top = getattr(me, "top_role", None)
        if top is not None and getattr(role, "position", 0) >= getattr(top, "position", 0):
            return await _say(interaction, f"❌ El rol **{name}** está por encima del rol del bot: subí el rol del bot para poder manejarlo.")
        try:
            if action == "dar":
                await usuario.add_roles(role, reason=f"{actor} ({actor.id}): {motivo or 'sin motivo'}"[:400])
            else:
                await usuario.remove_roles(role, reason=f"{actor} ({actor.id}): {motivo or 'sin motivo'}"[:400])
        except discord.HTTPException:
            return await _say(interaction, "❌ Discord no me dejó cambiar el rol (revisá que el bot tenga *Gestionar roles* y esté por encima de ese rol).")
        await self.run(rdb.log_change, usuario.id, rid, name, action, actor.id, motivo)
        if action == "dar":
            await _say(interaction, f"✅ {usuario.mention} recibió el rol **{name}**. Lo dio {actor.mention}.")
        else:
            await _say(interaction, f"✅ A {usuario.mention} se le quitó el rol **{name}**. Lo hizo {actor.mention}.")

    @app_commands.command(name="rol-dar", description="(Staff/Council/Coaching/Opinólogo) Da el rol de Observación o de CTA a una persona")
    @app_commands.describe(usuario="Persona", rol="Rol a dar", motivo="Motivo (opcional, queda en el registro)")
    @app_commands.choices(rol=CHOICES)
    @app_commands.guild_only()
    async def rol_dar(self, interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str], motivo: Optional[str] = None):
        await self._change(interaction, usuario, rol, motivo, "dar")

    @app_commands.command(name="rol-quitar", description="(Staff/Council/Coaching) Quita el rol de Observación o de CTA a una persona")
    @app_commands.describe(usuario="Persona", rol="Rol a quitar", motivo="Motivo (opcional, queda en el registro)")
    @app_commands.choices(rol=CHOICES)
    @app_commands.guild_only()
    async def rol_quitar(self, interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str], motivo: Optional[str] = None):
        await self._change(interaction, usuario, rol, motivo, "quitar")

    @app_commands.command(name="rol-historial", description="(Staff/Council/Coaching/Opinólogo) Quién dio o quitó roles")
    @app_commands.describe(usuario="Persona (si no, se muestran los últimos movimientos)")
    @app_commands.guild_only()
    async def rol_historial(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if not can_use_any(interaction.user):
            return await _say(interaction, "🔒 No tenés permiso para ver el historial de roles.")
        rows = await self.run(rdb.history, usuario.id if usuario else None, 15)
        if not rows:
            return await _say(interaction, "No hay movimientos registrados.")
        lines = [f"`{'➕' if r['action'] == 'dar' else '➖'}` <@{r['user_id']}> · **{discord.utils.escape_markdown(r['role_name'])}** · "
                 f"por <@{r['actor_id']}> · <t:{_unix(r['created_at'])}:f>" + (f" · {r['reason']}" if r["reason"] else "") for r in rows]
        head = f"**Historial de roles{' de ' + usuario.mention if usuario else ''}**\n"
        await _say(interaction, (head + "\n".join(lines))[:1900])


async def setup_rolesmgmt(bot, db_path: str, guild_id: int) -> RolesMgmtCog:
    cog = RolesMgmtCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
