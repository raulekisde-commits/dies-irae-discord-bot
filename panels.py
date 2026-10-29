"""Grupo /panel: /panel apuestas y /panel cambiomoneda (publican paneles en el canal)."""
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


async def setup_panels(bot: commands.Bot, guild_id: int) -> PanelsCog:
    cog = PanelsCog(bot, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
