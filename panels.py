"""Grupo /panel: publica paneles en el canal (apuestas, cambiomoneda, reclutamiento, foco, shop, coaching, wujupon, embo)."""
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


class PanelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot, self.guild_id = bot, guild_id

    panel = app_commands.Group(name="panel", description="Publica paneles en este canal", guild_only=True)

    async def _missing(self, interaction: discord.Interaction, name: str):
        try:
            await interaction.response.send_message(f"❌ El módulo de {name} no está cargado.", ephemeral=True)
        except Exception:
            traceback.print_exc()

    @panel.command(name="apuestas", description="Publica los paneles de apuestas: tops, total perdido, winrate y lista negra del casino")
    async def apuestas(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("RankingsCog")
        if cog is None:
            return await self._missing(interaction, "apuestas")
        await cog.publish_panels(interaction)

    @panel.command(name="cambiomoneda", description="(Council) Publica los paneles para vender y comprar Wuju Coins")
    @app_commands.describe(canal="Canal donde publicarlos (por defecto, este canal)")
    async def cambiomoneda(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        cog = self.bot.get_cog("DepositsCog")
        if cog is None:
            return await self._missing(interaction, "cambio de moneda")
        await cog.publish_panels(interaction, canal)

    @panel.command(name="reclutamiento", description="(Admin) Publica el panel de reclutamiento en este canal")
    async def reclutamiento(self, interaction: discord.Interaction):
        fn = (getattr(self.bot, "panel_handlers", None) or {}).get("reclutamiento")
        if fn is None:
            return await self._missing(interaction, "reclutamiento")
        await fn(interaction)                                      # valida que sea administrador

    @panel.command(name="foco", description="(Admin) Publica el panel de Foco Donor en este canal")
    async def foco(self, interaction: discord.Interaction):
        fn = (getattr(self.bot, "panel_handlers", None) or {}).get("foco")
        if fn is None:
            return await self._missing(interaction, "Foco Donor")
        await fn(interaction)                                      # valida que sea administrador

    @panel.command(name="shop", description="(Council) Publica el Top Vendedores y el panel de compra del shop de la guild")
    @app_commands.describe(canal="Canal donde publicar los dos paneles (por defecto, este canal)")
    async def shop(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        cog = self.bot.get_cog("GuildShopCog")
        if cog is None:
            return await self._missing(interaction, "Guild Shop")
        await cog.guild_shop(interaction, canal)                   # valida que sea Council/Admin

    @panel.command(name="coaching", description="(Council) Publica el panel para abrir el post de VODs del coaching")
    async def coaching(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("CoachingCog")
        if cog is None:
            return await self._missing(interaction, "coaching")
        await cog.coaching_panel(interaction)                      # valida que sea Council/Admin


    @panel.command(name="wujupon", description="(Council) Publica el panel del gachapon Wujupón en este canal")
    async def wujupon(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("WujuponCog")
        if cog is None:
            return await self._missing(interaction, "Wujupón")
        await cog.publish_panel(interaction)                       # valida que sea Council/Admin

    @panel.command(name="embo", description="(Council) Publica el panel para comprar embo o saldar la deuda de embo")
    @app_commands.describe(canal="Canal donde publicarlo (por defecto, este canal)")
    async def embo(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        cog = self.bot.get_cog("EmboTicketsCog")
        if cog is None:
            return await self._missing(interaction, "embo")
        await cog.publish_panel(interaction, canal)                # valida que sea Council


async def setup_panels(bot: commands.Bot, guild_id: int) -> PanelsCog:
    cog = PanelsCog(bot, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
