"""/casino abrir|cerrar y /casinoban (solo Council): cierran el casino y bloquean personas."""
import asyncio
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import casino_db as cdb
import economy_db as eco
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()


async def _reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class CasinoCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await self.run(cdb.init_schema)

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    async def _refresh_panels(self):
        rk = self.bot.get_cog("RankingsCog")
        if rk is not None:
            try:
                await rk.refresh_all()
            except Exception:
                traceback.print_exc()

    @app_commands.command(name="casino", description="(Council) Abre o cierra el casino: dados, coinflip y 1v1")
    @app_commands.describe(accion="Abrir o cerrar")
    @app_commands.choices(accion=[app_commands.Choice(name="abrir", value="abrir"), app_commands.Choice(name="cerrar", value="cerrar")])
    @app_commands.guild_only()
    async def casino(self, interaction: discord.Interaction, accion: app_commands.Choice[str]):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** (o administradores) puede usar este comando.")
            opening = accion.value == "abrir"
            changed = await self.run(cdb.set_open, interaction.guild.id, opening, interaction.user.id)
            if not changed:
                return await _reply(interaction, f"ℹ️ El casino ya estaba {'abierto' if opening else 'cerrado'}.")
            if opening:
                await _reply(interaction, f"🎰 {interaction.user.mention} **abrió** el casino. Ya se puede jugar.", ephemeral=False)
            else:
                await _reply(interaction, f"🔒 {interaction.user.mention} **cerró** el casino: dados, coinflip y 1v1 quedan bloqueados "
                                          "(las partidas ya en curso terminan normalmente).", ephemeral=False)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    @app_commands.command(name="limite", description="(Council) Pone la apuesta máxima de dados, coinflip o 1v1 (0 = sin límite propio)")
    @app_commands.describe(juego="Juego", cantidad="Apuesta máxima, por ejemplo 1.000.000 o 1m (0 para quitar el límite)")
    @app_commands.choices(juego=[app_commands.Choice(name=n, value=n) for n in eco.GAME_COMMANDS])
    @app_commands.guild_only()
    async def limite(self, interaction: discord.Interaction, juego: app_commands.Choice[str], cantidad: str):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** (o administradores) puede usar este comando.")
            if (cantidad or "").strip() in ("0", "quitar"):
                await self.run(eco.set_bet_limit, juego.value, None)
                return await _reply(interaction, f"✅ Se quitó el límite de **{juego.value}** (quedan los topes generales).", ephemeral=False)
            try:
                amount = eco.parse_amount_short(cantidad)
            except eco.AmountError as exc:
                return await _reply(interaction, f"❌ {exc}")
            await self.run(eco.set_bet_limit, juego.value, amount)
            await _reply(interaction, f"✅ Apuesta máxima de **{juego.value}**: {eco.format_amount(amount)}.", ephemeral=False)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    @app_commands.command(name="auditoria-casino", description="(Council) Busca indicios de trampa: suerte anómala, parejas que juegan entre sí, transferencias raras")
    @app_commands.describe(dias="Cuántos días hacia atrás revisar (por defecto 30; 0 = todo el historial)")
    @app_commands.guild_only()
    async def auditoria_casino(self, interaction: discord.Interaction, dias: Optional[app_commands.Range[int, 0, 3650]] = 30):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** (o administradores) puede usar este comando.")
            await interaction.response.defer(ephemeral=True)
            import audit_db
            rep = await self.run(audit_db.build_report, dias or None)
            eco_cog = self.bot.get_cog("EconomyCog")
            money = eco_cog.money if eco_cog is not None else eco.format_amount
            sections = audit_db.format_sections(rep, money)
            span = f"últimos {dias} días" if dias else "todo el historial"
            e = discord.Embed(title="🕵️ Auditoría del casino", color=discord.Color.orange(),
                              description=f"Se revisaron **{rep.games:,}** partidas terminadas con ganador ({span}). Son **indicios para mirar a mano**, no pruebas.")
            titles = {"luck": "🍀 Suerte anómala (dados, coinflip y 1v1)", "pairs": "🤝 Parejas que juegan casi solo entre sí",
                      "transfers": "💸 Transferencias sospechosas (/give-money)"}
            empty = {"luck": f"Nadie se aparta del azar (se evalúa a quien jugó {audit_db.MIN_GAMES_LUCK}+ partidas).",
                     "pairs": "Ninguna pareja llama la atención.", "transfers": "Sin envíos en un solo sentido ni cuentas que reciban de muchas."}
            for key in ("luck", "pairs", "transfers"):
                value = "\n".join(sections[key])[:1024] or empty[key]
                e.add_field(name=titles[key], value=value, inline=False)
            e.set_footer(text="Con muchas personas algunas salen raras solo por casualidad; dos amigos también pueden jugar siempre entre sí. Revisá antes de actuar.")
            await interaction.followup.send(embed=e, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude generar la auditoría.")

    @app_commands.command(name="casinoban", description="(Council) Bloquea o desbloquea a una persona del casino (repetirlo la saca de la lista)")
    @app_commands.describe(usuario="Persona")
    @app_commands.guild_only()
    async def casinoban(self, interaction: discord.Interaction, usuario: discord.Member):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** (o administradores) puede usar este comando.")
            if usuario.bot:
                return await _reply(interaction, "❌ No se puede bloquear a un bot.")
            banned = await self.run(cdb.toggle_ban, interaction.guild.id, usuario.id, interaction.user.id)
            if banned:
                await _reply(interaction, f"🚫 {usuario.mention} fue **bloqueado** del casino. Vuelve a usar `/casinoban` para sacarlo de la lista.")
            else:
                await _reply(interaction, f"✅ {usuario.mention} fue **sacado** de la lista de bloqueados del casino.")
            await self._refresh_panels()
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")


async def setup_casino(bot: commands.Bot, db_path: str, guild_id: int) -> CasinoCog:
    cog = CasinoCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
