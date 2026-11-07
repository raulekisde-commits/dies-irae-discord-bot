"""Grupo /casino: abrir/cerrar el casino, límite, ban, y los juegos (dados, coinflip, 1v1, ruleta) y sus estadísticas.
Un solo comando raíz para no pasar el límite de 100 comandos de Discord; cada juego sigue validando sus permisos en su módulo."""
import asyncio
import json
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import casino_db as cdb
import economy_db as eco
import provably_fair as pf
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
        await self.run(pf.init_schema)

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

    # ---------- /casino apuestas ...: la lógica vive en bets.py ----------
    apuestas_g = app_commands.Group(name="apuestas", description="Crear, cerrar y resolver apuestas con Wuju Coins", parent=casino_g)

    @apuestas_g.command(name="crear", description="Crea una apuesta con Wuju Coins")
    @app_commands.describe(enunciado="Qué se apuesta", opciones="Opciones separadas por comas (ej: Gana Smogg, Pierde Smogg, Empate)")
    async def apuestas_crear(self, interaction: discord.Interaction, enunciado: str, opciones: str):
        await self._delegate(interaction, "BetsCog", "apuesta", "apuestas", enunciado, opciones)

    @apuestas_g.command(name="apostar", description="Apostá Wuju Coins a una opción (dentro del hilo de la apuesta)")
    @app_commands.describe(opcion="Opción a la que apostás", cantidad="Cantidad (ej: 5,000)")
    async def apuestas_apostar(self, interaction: discord.Interaction, opcion: str, cantidad: str):
        await self._delegate(interaction, "BetsCog", "apostar_slash", "apuestas", opcion, cantidad)

    @apuestas_g.command(name="todo", description="Apostá TODO tu saldo a una opción (pide confirmación)")
    @app_commands.describe(opcion="Opción a la que apostás todo")
    async def apuestas_todo(self, interaction: discord.Interaction, opcion: str):
        await self._delegate(interaction, "BetsCog", "apostartodo_slash", "apuestas", opcion)

    @apuestas_g.command(name="cerrar", description="Cierra una apuesta: ya no se aceptan más apuestas (quien la creó, Council o administradores)")
    @app_commands.describe(id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    async def apuestas_cerrar(self, interaction: discord.Interaction, id: Optional[int] = None):
        await self._delegate(interaction, "BetsCog", "cerrarapuesta", "apuestas", id)

    @apuestas_g.command(name="resolver", description="Define la opción ganadora y reparte el pozo (quien la creó, Council o administradores)")
    @app_commands.describe(opcion="Opción ganadora", id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    async def apuestas_resolver(self, interaction: discord.Interaction, opcion: str, id: Optional[int] = None):
        await self._delegate(interaction, "BetsCog", "resolverapuesta", "apuestas", opcion, id)

    @apuestas_g.command(name="cancelar", description="Cancela una apuesta y devuelve todo lo apostado (creador o administrador)")
    @app_commands.describe(id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    async def apuestas_cancelar(self, interaction: discord.Interaction, id: Optional[int] = None):
        await self._delegate(interaction, "BetsCog", "cancelarapuesta", "apuestas", id)

    async def _ac_apuestas_opcion(self, interaction: discord.Interaction, current: str):
        bets = self.bot.get_cog("BetsCog")
        return await bets.option_autocomplete(interaction, current) if bets is not None else []

    @apuestas_apostar.autocomplete("opcion")
    async def _ac_apuestas_apostar(self, interaction: discord.Interaction, current: str):
        return await self._ac_apuestas_opcion(interaction, current)

    @apuestas_todo.autocomplete("opcion")
    async def _ac_apuestas_todo(self, interaction: discord.Interaction, current: str):
        return await self._ac_apuestas_opcion(interaction, current)

    @apuestas_resolver.autocomplete("opcion")
    async def _ac_apuestas_resolver(self, interaction: discord.Interaction, current: str):
        return await self._ac_apuestas_opcion(interaction, current)

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

    # ---------- azar verificable (provably fair) ----------
    @casino_g.command(name="verificar", description="Comprueba con las semillas reveladas que el resultado de una partida no fue alterado")
    @app_commands.describe(juego="Tipo de partida", numero="Número de la partida, duelo o ronda (sale en el pie del resultado)")
    @app_commands.choices(juego=[app_commands.Choice(name="Coinflip", value="coinflip"), app_commands.Choice(name="Dados", value="dice"),
                                 app_commands.Choice(name="1v1", value="duel"), app_commands.Choice(name="Ruleta", value="roulette")])
    async def casino_verificar(self, interaction: discord.Interaction, juego: app_commands.Choice[str], numero: app_commands.Range[int, 1]):
        try:
            v = await self.run(pf.verify, juego.value, int(numero))
            label = f"{juego.name} #{numero}"
            if v.status == "notfound":
                return await _reply(interaction, f"❌ {label} no tiene registro de azar verificable (no existe o es anterior a este sistema).")
            if v.status == "pending":
                return await _reply(interaction, f"🔒 {label} todavía no termina. Hash publicado: `{v.record['server_hash']}`\n"
                                                 "La semilla del bot se revela cuando sale el resultado.")
            rec = v.record
            lines = [f"**{label}** — {'✅ resultado verificado' if v.status == 'ok' else '⚠️ NO COINCIDE'}",
                     f"{'✅' if v.hash_ok else '❌'} SHA-256 de la semilla revelada = hash publicado antes de jugar",
                     f"Hash publicado: `{rec['server_hash']}`", f"Semilla del bot: `{rec['server_seed']}`",
                     f"Semilla de jugadores: `{rec['client_seed']}`", f"Nonce: `{rec['nonce']}`"]
            if v.computed is not None:
                lines.append(f"{'✅' if v.computed == v.stored else '❌'} Recalculado: `{json.dumps(v.computed, ensure_ascii=False)}` · "
                             f"registrado: `{json.dumps(v.stored, ensure_ascii=False)}`")
            else:
                lines.append("La partida se devolvió o se canceló: no hubo resultado que pagar.")
            lines.append("Se puede recalcular sin el bot con `provably_fair.py` o cualquier verificador compatible con Stake "
                         "(HMAC-SHA256, mensaje `semilla_jugadores:nonce:ronda`).")
            await _reply(interaction, "\n".join(lines)[:2000])
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    @casino_g.command(name="semilla", description="Ve o cambia tu semilla de jugador (se mezcla con la del bot en cada partida)")
    @app_commands.describe(nueva="Nueva semilla: 1 a 32 letras, números o _ (vacío = solo ver la actual)")
    async def casino_semilla(self, interaction: discord.Interaction, nueva: Optional[str] = None):
        try:
            if nueva:
                try:
                    seed = await self.run(pf.set_client_seed, interaction.user.id, nueva)
                except ValueError as exc:
                    return await _reply(interaction, f"❌ {exc}")
                return await _reply(interaction, f"✅ Tu semilla ahora es `{seed}`. Se usa en las próximas partidas que se resuelvan.")
            seed = await self.run(pf.get_client_seed, interaction.user.id)
            await _reply(interaction, f"🎲 Tu semilla de jugador es `{seed}`.\nSe combina con la semilla secreta del bot (cuyo hash se publica "
                                      "antes de jugar), así que ni el bot ni nadie puede elegir el resultado. Cámbiala con "
                                      "`/casino semilla nueva:...`.")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")


async def setup_casino(bot: commands.Bot, db_path: str, guild_id: int) -> CasinoCog:
    cog = CasinoCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
