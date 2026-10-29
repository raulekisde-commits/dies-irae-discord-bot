"""/addhorautc — canal de voz que muestra la hora UTC (se renombra cada 10 minutos)."""
import asyncio
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import utcclock_db as udb
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
TICK_SECONDS = 20
MIN_SECONDS_BETWEEN_RENAMES = 240          # tope propio: muy por debajo del límite de Discord (2 cada 10 min)
RENAME_TIMEOUT = 25                        # si Discord frena el renombre (429 largo), no se queda colgado: reintenta en el próximo ciclo


async def _reply(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class UtcClockCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.now = lambda: datetime.now(timezone.utc)      # se puede reemplazar en las pruebas
        self._last_rename = 0.0
        self._last_warn = 0.0

    async def cog_load(self):
        await asyncio.to_thread(udb.init_schema, self.db_path)
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    async def _get_channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            return False                      # borrado de verdad
        except Exception:
            return None                       # error transitorio: no se toca nada

    # ---------- /addhorautc ----------
    @app_commands.command(name="addhorautc", description="Crea un canal de voz que muestra la hora UTC (se actualiza cada 10 minutos)")
    @app_commands.describe(categoria="Categoría donde crear el canal (opcional)")
    @app_commands.guild_only()
    async def addhorautc(self, interaction: discord.Interaction, categoria: Optional[discord.CategoryChannel] = None):
        if not can_manage(interaction.user):
            return await _reply(interaction, "🔒 Solo el Council (o un administrador) puede crear el reloj UTC.")
        guild = interaction.guild
        existing = await self.run(udb.get_clock, guild.id)
        if existing:
            ch = await self._get_channel(existing)
            if ch:
                return await _reply(interaction, f"ℹ️ Ya hay un reloj UTC: {ch.mention}. Si lo borrás, volvé a usar `/addhorautc`.")
            if ch is None:
                return await _reply(interaction, "⚠️ No pude verificar el reloj actual ahora. Probá de nuevo en un momento.")
        await interaction.response.defer(ephemeral=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False)}
        if guild.me is not None:                  # el bot conserva el permiso de renombrarlo aunque la categoría lo restrinja
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        try:
            ch = await guild.create_voice_channel(udb.label_for(self.now()), category=categoria, overwrites=overwrites,
                                                  reason=f"Reloj UTC creado por {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("❌ No tengo permiso para crear canales (**Gestionar canales**).", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ Discord no me dejó crear el canal: {exc}", ephemeral=True)
        await self.run(udb.set_clock, guild.id, ch.id, interaction.user.id)
        self._last_rename = time.monotonic()
        await interaction.followup.send(
            f"✅ Creé {ch.mention}. Muestra la hora UTC y se actualiza cada 10 minutos "
            f"(Discord solo permite renombrar un canal 2 veces cada 10 minutos, por eso no es minuto a minuto). "
            f"Nadie puede entrar al canal: es solo para mirarlo.", ephemeral=True)

    # ---------- actualización ----------
    @tasks.loop(seconds=TICK_SECONDS)
    async def ticker(self):
        try:
            await self.update_once()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def update_once(self) -> str:
        """Renombra el canal si la hora (en bloques de 10 min) cambió. Devuelve lo que hizo, para las pruebas."""
        cid = await self.run(udb.get_clock, self.guild_id)
        if not cid:
            return "none"
        ch = await self._get_channel(cid)
        if ch is None:
            return "unknown"
        if ch is False:
            await self.run(udb.clear_clock, self.guild_id, cid)
            print("⚠️ El canal del reloj UTC fue borrado: se desactivó. Usá /addhorautc para crear otro.")
            return "deleted"
        target = udb.label_for(self.now())
        if ch.name == target:
            return "same"
        if time.monotonic() - self._last_rename < MIN_SECONDS_BETWEEN_RENAMES:
            return "throttled"
        try:
            await asyncio.wait_for(ch.edit(name=target, reason="Reloj UTC"), timeout=RENAME_TIMEOUT)
        except asyncio.TimeoutError:
            return "timeout"
        except (discord.Forbidden, discord.HTTPException) as exc:
            if time.monotonic() - self._last_warn > 3600:
                self._last_warn = time.monotonic()
                print(f"⚠️ No pude renombrar el reloj UTC: {exc!r} (revisá que el bot tenga Gestionar canales en ese canal)")
            return "error"
        self._last_rename = time.monotonic()
        return "renamed"


async def setup_utcclock(bot: commands.Bot, db_path: str, guild_id: int) -> UtcClockCog:
    cog = UtcClockCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
