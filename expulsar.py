"""/expulsar usuario [motivo] — el Staff expulsa a alguien del servidor, siempre con un mensaje de confirmación.

Las reglas se validan en el servidor (no solo en lo que muestra el botón) y se vuelven a validar al confirmar: no se puede expulsar a uno mismo,
a un bot, al dueño del servidor ni a alguien con un rol igual o más alto que el de quien lo pide o que el del propio bot; y solo un administrador
puede expulsar a alguien del Staff o del Council. Queda registrado en el canal de registros."""
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from registration import YesNoView, can_admin_registro

NO_MENTIONS = discord.AllowedMentions.none()
MAX_REASON = 300


def _pos(member) -> int:
    return int(getattr(getattr(member, "top_role", None), "position", 0) or 0)


def check_target(actor, target, guild) -> Optional[str]:
    """Texto del motivo por el que NO se puede expulsar a `target`, o None si se puede."""
    if not can_admin_registro(actor):
        return "🔒 Solo el Staff, el Council o los administradores pueden expulsar."
    if getattr(target, "id", None) == getattr(actor, "id", None):
        return "❌ No podés expulsarte a vos mismo."
    if getattr(target, "bot", False):
        return "❌ No se puede expulsar a un bot con este comando."
    if getattr(target, "id", None) == getattr(guild, "owner_id", None):
        return "❌ No se puede expulsar al dueño del servidor."
    me = getattr(guild, "me", None)
    if me is not None and target.id == me.id:
        return "❌ No me voy a expulsar a mí mismo."
    actor_is_admin = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))
    actor_is_owner = getattr(actor, "id", None) == getattr(guild, "owner_id", None)
    if can_admin_registro(target) and not (actor_is_admin or actor_is_owner):
        return "🔒 Solo un administrador puede expulsar a alguien del Staff o del Council."
    if not actor_is_owner and _pos(target) >= _pos(actor):
        return "❌ Esa persona tiene un rol igual o más alto que el tuyo."
    if me is not None:
        if not getattr(getattr(me, "guild_permissions", None), "kick_members", False):
            return "❌ Me falta el permiso **Expulsar miembros**."
        if _pos(target) >= _pos(me):
            return "❌ No puedo expulsar a esa persona: su rol está igual o por encima del mío."
    return None


async def _say(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class ExpulsarCog(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot, self.guild_id = bot, guild_id

    async def audit(self, text: str):
        reg = self.bot.get_cog("RegistrationCog")
        if reg is not None:
            await reg.audit_log(text)

    @app_commands.command(name="expulsar", description="(Staff) Expulsa a una persona del servidor, con confirmación")
    @app_commands.describe(usuario="Quién se va del servidor", motivo="Motivo (queda en el registro de auditoría)")
    @app_commands.guild_only()
    async def expulsar(self, interaction: discord.Interaction, usuario: discord.Member, motivo: Optional[str] = None):
        try:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                return await _say(interaction, "❌ Solo disponible en el servidor.")
            problem = check_target(interaction.user, usuario, interaction.guild)
            if problem:
                return await _say(interaction, problem)
            reason = " ".join((motivo or "").split())[:MAX_REASON]
            actor_id, target_id = interaction.user.id, usuario.id

            async def confirm(i: discord.Interaction):
                await self.confirmed(i, actor_id, target_id, reason)

            await interaction.response.send_message(
                f"⚠️ ¿Expulsar a {usuario.mention} (**{discord.utils.escape_markdown(usuario.display_name)}**) del servidor?"
                + (f"\nMotivo: {discord.utils.escape_markdown(reason)}" if reason else "\nSin motivo indicado.")
                + "\nPuede volver a entrar con una invitación nueva.",
                view=YesNoView(actor_id, confirm, "Sí, expulsar"), ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ Ocurrió un error.")

    async def confirmed(self, interaction: discord.Interaction, actor_id: int, target_id: int, reason: str):
        """Segunda validación, ya con el botón apretado: el estado pudo cambiar entre el comando y la confirmación."""
        guild = interaction.guild
        try:
            actor = interaction.user
            target = guild.get_member(target_id) if guild else None
            if target is None:
                return await interaction.response.edit_message(content="❌ Esa persona ya no está en el servidor.", view=None)
            problem = check_target(actor, target, guild)
            if problem:
                return await interaction.response.edit_message(content=problem, view=None)
            label = f"{target.display_name} ({target.id})"
            try:
                await target.kick(reason=f"Expulsado por {actor} ({actor.id})" + (f": {reason}" if reason else ""))
            except discord.Forbidden:
                return await interaction.response.edit_message(content="❌ Discord no me dejó expulsar a esa persona (revisá mi rol y permisos).", view=None)
            await interaction.response.edit_message(content=f"✅ **{discord.utils.escape_markdown(target.display_name)}** fue expulsado del servidor.", view=None)
            await self.audit(f"🚪 <@{actor.id}> expulsó a **{discord.utils.escape_markdown(label)}**." + (f" Motivo: {discord.utils.escape_markdown(reason)}" if reason else ""))
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ No pude expulsar a esa persona.")


async def setup_expulsar(bot: commands.Bot, guild_id: int) -> ExpulsarCog:
    try:                                   # es un comando del Staff: no debe exigir estar registrado (igual que admin_vincular)
        import registration
        registration.EXEMPT_COMMANDS.add("expulsar")
    except Exception:
        pass
    cog = ExpulsarCog(bot, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
