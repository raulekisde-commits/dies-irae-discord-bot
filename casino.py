"""Grupo /casino: abrir/cerrar el casino, límite, ban, y los juegos (dados, coinflip, 1v1, ruleta) y sus estadísticas.
Un solo comando raíz para no pasar el límite de 100 comandos de Discord; cada juego sigue validando sus permisos en su módulo."""
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

    async def casinoban(self, interaction: discord.Interaction, usuario: discord.Member):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** (o administradores) puede usar este comando.")
            if usuario.bot:
                return await _reply(interaction, "❌ No se puede bloquear a un bot.")
            banned = await self.run(cdb.toggle_ban, interaction.guild.id, usuario.id, interaction.user.id)
            if banned:
                await _reply(interaction, f"🚫 {usuario.mention} fue **bloqueado** del casino. Vuelve a usar `/casino ban` para sacarlo de la lista.")
            else:
                await _reply(interaction, f"✅ {usuario.mention} fue **sacado** de la lista de bloqueados del casino.")
            await self._refresh_panels()
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    # ---------- /casino ...: subcomandos (la lógica vive en cada módulo; acá solo se delega) ----------
    casino_g = app_commands.Group(name="casino", description="Casino: abrir/cerrar, límites, juegos y estadísticas", guild_only=True)

    async def _delegate(self, interaction: discord.Interaction, cog_name: str, method: str, label: str, *args):
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return await _reply(interaction, f"❌ El módulo de {label} no está cargado.")
        await getattr(cog, method)(interaction, *args)

    @casino_g.command(name="abrir", description="(Council) Abre el casino: dados, coinflip y 1v1")
    async def casino_abrir(self, interaction: discord.Interaction):
        await self.casino(interaction, app_commands.Choice(name="abrir", value="abrir"))

    @casino_g.command(name="cerrar", description="(Council) Cierra el casino: dados, coinflip y 1v1")
    async def casino_cerrar(self, interaction: discord.Interaction):
        await self.casino(interaction, app_commands.Choice(name="cerrar", value="cerrar"))

    @casino_g.command(name="limite", description="(Council) Pone la apuesta máxima de dados, coinflip o 1v1 (0 = sin límite propio)")
    @app_commands.describe(juego="Juego", cantidad="Apuesta máxima, por ejemplo 1.000.000 o 1m (0 para quitar el límite)")
    @app_commands.choices(juego=[app_commands.Choice(name=n, value=n) for n in eco.GAME_COMMANDS])
    async def casino_limite(self, interaction: discord.Interaction, juego: app_commands.Choice[str], cantidad: str):
        await self.limite(interaction, juego, cantidad)

    @casino_g.command(name="ban", description="(Council) Bloquea o desbloquea a una persona del casino (repetirlo la saca de la lista)")
    @app_commands.describe(usuario="Persona")
    async def casino_ban(self, interaction: discord.Interaction, usuario: discord.Member):
        await self.casinoban(interaction, usuario)

    @casino_g.command(name="dados", description="Crea una partida de dados (5 minutos). Gana el número más alto y se lleva el pozo")
    @app_commands.describe(cantidad="Lo que ponés para entrar")
    async def casino_dados(self, interaction: discord.Interaction, cantidad: str):
        await self._delegate(interaction, "GamesCog", "dados_slash", "juegos", cantidad)

    @casino_g.command(name="coinflip", description="Desafía a alguien a cara o cruz (dura 5 minutos). El que sale se queda con todo")
    @app_commands.describe(cantidad="Lo que apostás", lado="Lado que elegís (opcional; si no, cara). Quien acepte toma el otro")
    @app_commands.choices(lado=[app_commands.Choice(name="Cara", value="cara"), app_commands.Choice(name="Cruz", value="cruz")])
    async def casino_coinflip(self, interaction: discord.Interaction, cantidad: str, lado: Optional[app_commands.Choice[str]] = None):
        await self._delegate(interaction, "GamesCog", "coinflip_slash", "juegos", cantidad, lado)

    @casino_g.command(name="1v1", description="Retá a alguien a un duelo de cara o cruz: el que sale se queda con todo")
    @app_commands.describe(usuario="A quién retás", cantidad="Lo que apostás (sin tope: hasta tu balance total)")
    async def casino_1v1(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: str):
        await self._delegate(interaction, "DuelsCog", "duel", "duelos", usuario, cantidad)

    @casino_g.command(name="winrate", description="Winrate, total apostado, ganado/perdido y lista negra de una persona (por defecto, vos)")
    @app_commands.describe(usuario="Persona a consultar")
    async def casino_winrate(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        await self._delegate(interaction, "GamesCog", "winrate_slash", "juegos", usuario)

    @casino_g.command(name="total", description="Todo lo que se apostó en el servidor (apuestas, dados, coinflip y duelos)")
    async def casino_total(self, interaction: discord.Interaction):
        await self._delegate(interaction, "GamesCog", "totalapostado_slash", "juegos")

    @casino_g.command(name="ruleta", description="Abre la ruleta del casino (solo staff; hay una única ruleta en todo el servidor)")
    async def casino_ruleta(self, interaction: discord.Interaction):
        await self._delegate(interaction, "RouletteCog", "ruleta", "ruleta")

    @casino_g.command(name="ruleta-editar", description="Edita la configuración de la ruleta (solo staff)")
    @app_commands.describe(capital_inicial="Capital inicial (no cambia el balance actual)", balance_casa="Nuevo balance de la casa (ajuste auditado; requiere motivo)",
                           motivo="Motivo del ajuste de balance", apuesta_min="Apuesta mínima", apuesta_max="Apuesta máxima",
                           reserva="Reserva de seguridad", segundos="Duración de la ronda en segundos (15-600)",
                           pagos_editables="Habilitar/deshabilitar la edición de pagos", pago_color="Multiplicador rojo/negro",
                           pago_paridad="Multiplicador par/impar", pago_falta_pasa="Multiplicador falta/pasa", pago_docena="Multiplicador docenas",
                           pago_columna="Multiplicador columnas", pago_numero="Multiplicador número exacto")
    async def casino_ruleta_editar(self, interaction: discord.Interaction, capital_inicial: Optional[str] = None, balance_casa: Optional[str] = None,
                                   motivo: Optional[str] = None, apuesta_min: Optional[str] = None, apuesta_max: Optional[str] = None,
                                   reserva: Optional[str] = None, segundos: Optional[app_commands.Range[int, 15, 600]] = None,
                                   pagos_editables: Optional[bool] = None, pago_color: Optional[app_commands.Range[int, 1, 1000]] = None,
                                   pago_paridad: Optional[app_commands.Range[int, 1, 1000]] = None, pago_falta_pasa: Optional[app_commands.Range[int, 1, 1000]] = None,
                                   pago_docena: Optional[app_commands.Range[int, 1, 1000]] = None, pago_columna: Optional[app_commands.Range[int, 1, 1000]] = None,
                                   pago_numero: Optional[app_commands.Range[int, 1, 1000]] = None):
        await self._delegate(interaction, "RouletteCog", "ruleta_editar", "ruleta", capital_inicial, balance_casa, motivo, apuesta_min, apuesta_max,
                             reserva, segundos, pagos_editables, pago_color, pago_paridad, pago_falta_pasa, pago_docena, pago_columna, pago_numero)

    @casino_g.command(name="ruleta-stats", description="Estadísticas de la ruleta (solo staff)")
    async def casino_ruleta_stats(self, interaction: discord.Interaction):
        await self._delegate(interaction, "RouletteCog", "ruleta_stats", "ruleta")


async def setup_casino(bot: commands.Bot, db_path: str, guild_id: int) -> CasinoCog:
    cog = CasinoCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
