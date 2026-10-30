"""Diagnóstico de «no me aparecen los comandos /»: revisa, para una persona en el canal actual, todo lo que depende del servidor.

Por qué pasa que dos personas con el MISMO rol vean cosas distintas: que un comando aparezca o no en la lista lo decide DISCORD
(no el bot), según (1) el permiso «Usar comandos de aplicación» del canal, (2) excepciones por persona/rol/canal en
Ajustes del servidor → Integraciones → este bot, (3) estar en espera de la verificación de reglas o con aislamiento (timeout),
y (4) que su aplicación de Discord tenga la lista vieja. El bot no puede cambiar (2) con su token; sí puede decir qué está mal.

`!diagnostico` funciona aunque la persona no vea los comandos con /, y no exige estar registrado.
"""
import traceback
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import registration_db as rdb
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
COMMAND_LIMIT = 100                 # Discord admite como máximo 100 comandos / por servidor


def diagnose(member, channel, *, member_role_id: Optional[int], registered: Optional[bool], command_count: Optional[int]) -> List[Tuple[bool, str]]:
    """Lista de (ok, texto). Función pura para poder probarla."""
    out: List[Tuple[bool, str]] = []
    roles = {r.id for r in getattr(member, "roles", [])}
    if member_role_id:
        out.append((member_role_id in roles, "Tiene el rol **Miembro**." if member_role_id in roles else "NO tiene el rol **Miembro**."))
    if registered is not None:
        out.append((registered, "Está **registrado** (personaje de Albion vinculado)." if registered
                    else "NO está registrado: con `/registrar` (o `!registrar` si no ve los /) se vincula."))
    if getattr(member, "pending", False):
        out.append((False, "Todavía no aceptó las **reglas del servidor** (verificación pendiente): Discord le oculta los comandos hasta que las acepte."))
    until = getattr(member, "timed_out_until", None)
    if until is not None:
        out.append((False, "Está **aislado (timeout)**: no puede usar comandos hasta que termine."))
    if channel is not None:
        try:
            perms = channel.permissions_for(member)
        except Exception:
            perms = None
        if perms is not None:
            if not getattr(perms, "administrator", False):
                for attr, label, why in (("view_channel", "Ver el canal", ""),
                                         ("use_application_commands", "Usar comandos de aplicación",
                                          " Es el permiso que más oculta los / : revisá el canal y la categoría, y los roles/personas con excepciones.")):
                    if not hasattr(perms, attr):
                        continue
                    ok = bool(getattr(perms, attr))
                    out.append((ok, f"En este canal: **{label}** {'permitido' if ok else 'DENEGADO'}.{'' if ok else why}"))
    if command_count is not None:
        near = command_count >= COMMAND_LIMIT - 5
        out.append((not near, f"El bot tiene **{command_count}/{COMMAND_LIMIT}** comandos / (el máximo de Discord es {COMMAND_LIMIT})."
                              + (" Está al límite: al pasarlo, los nuevos no aparecen para nadie." if near else "")))
    return out


TIPS = ("Si todo figura bien, lo que falta está del lado de Discord:\n"
        "• Que la persona **reinicie Discord** (Ctrl+R en PC; cerrar y abrir la app en el celular) y actualice la app.\n"
        "• Un admin: **Ajustes del servidor → Integraciones → este bot → Comandos**: ahí puede haber una excepción **para esa persona** "
        "(o canal/rol) que oculta los comandos aunque tenga el mismo rol que el resto. Se corrige quitando esa excepción.\n"
        "• Revisar permisos del canal/categoría para esa persona (los permisos por **persona** pisan a los del rol).")


class DiagnosticoCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    def _command_count(self) -> Optional[int]:
        try:
            tree = self.bot.tree
            cmds = tree.get_commands(guild=discord.Object(id=self.guild_id)) + tree.get_commands()
            seen = {c.name for c in cmds}
            return len(seen)
        except Exception:
            return None

    async def build(self, member, channel, staff_view: bool):
        import asyncio
        eco_cog = self.bot.get_cog("EconomyCog")
        member_role_id = getattr(eco_cog, "member_role_id", None)
        try:
            registered = await asyncio.to_thread(rdb.is_registered, self.db_path, member.id)
        except Exception:
            registered = None
        items = diagnose(member, channel, member_role_id=member_role_id, registered=registered,
                         command_count=self._command_count() if staff_view else None)
        bad = [t for ok, t in items if not ok]
        lines = [("✅ " if ok else "❌ ") + t for ok, t in items]
        verdict = ("Se encontró **al menos un problema**; arreglando eso deberían aparecer." if bad else "Por parte del bot y de los permisos del canal **está todo bien**.")
        text = f"**Diagnóstico de {discord.utils.escape_markdown(getattr(member, 'display_name', str(member.id)))}**\n" + "\n".join(lines) + f"\n\n{verdict}"
        if not bad:
            text += "\n\n" + TIPS
        return text

    @commands.command(name="diagnostico")
    async def diagnostico_prefix(self, ctx: commands.Context, *, quien: str = ""):
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        target = ctx.author
        mentioned = [m for m in getattr(ctx.message, "mentions", []) if not getattr(m, "bot", False)]
        if mentioned:
            if not can_manage(ctx.author):
                return await ctx.reply("🔒 Solo el Council o los administradores pueden diagnosticar a otra persona.", mention_author=False)
            target = mentioned[0]
        try:
            text = await self.build(target, ctx.channel, can_manage(ctx.author))
            await ctx.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS, delete_after=120)
        except Exception:
            traceback.print_exc()
            await ctx.reply("❌ No pude hacer el diagnóstico.", mention_author=False)

    @app_commands.command(name="diagnostico", description="¿No te aparecen los comandos? Revisa por qué (por defecto, vos)")
    @app_commands.describe(usuario="(Staff) Persona a revisar")
    @app_commands.guild_only()
    async def diagnostico(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        try:
            target = usuario or interaction.user
            if usuario is not None and usuario.id != interaction.user.id and not can_manage(interaction.user):
                return await interaction.response.send_message("🔒 Solo el Council o los administradores pueden diagnosticar a otra persona.", ephemeral=True)
            text = await self.build(target, interaction.channel, can_manage(interaction.user))
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            try:
                await interaction.response.send_message("❌ No pude hacer el diagnóstico.", ephemeral=True)
            except Exception:
                pass


async def setup_diagnostico(bot: commands.Bot, db_path: str, guild_id: int) -> DiagnosticoCog:
    try:                                   # el diagnóstico tiene que poder usarse sin estar registrado (justo ayuda a quien no ve los comandos)
        import registration
        registration.EXEMPT_COMMANDS.add("diagnostico")
    except Exception:
        pass
    cog = DiagnosticoCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
