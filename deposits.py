"""Pasar silver a Wuju Coins — panel con botón, ticket privado y cierre solo para Council (capa de Discord)."""
import asyncio
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import deposits_db as ddb
from guildshop import COUNCIL_ROLE_ID, can_manage, safe_channel_name

NO_MENTIONS = discord.AllowedMentions.none()
PING = discord.AllowedMentions(roles=True, users=True, everyone=False)
CLOSE_DELAY_SECONDS = 5
GREEN = 0x2ECC71


def is_council(member) -> bool:
    """Solo el rol Council (se evalúa en cada pulsación)."""
    return any(getattr(r, "id", None) == COUNCIL_ROLE_ID for r in getattr(member, "roles", []))


async def _reply(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class DepositPanelView(discord.ui.View):
    def __init__(self, cog: "DepositsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Crear ticket", style=discord.ButtonStyle.success, custom_id="dep:open", emoji="🎫")
    async def open(self, interaction: discord.Interaction, button):
        await self.cog.on_open_pressed(interaction)


class DepositCloseView(discord.ui.View):
    def __init__(self, cog: "DepositsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cerrar ticket", style=discord.ButtonStyle.danger, custom_id="dep:close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button):
        await self.cog.on_close_pressed(interaction)


class SilverModal(discord.ui.Modal, title="Pasar silver a Wuju Coins"):
    silver = discord.ui.TextInput(label="Cantidad de silver que querés pasar", style=discord.TextStyle.short, required=True,
                                  max_length=25, placeholder="Ej: 1.500.000")

    def __init__(self, cog: "DepositsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_silver_submitted(interaction, str(self.silver.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude abrir el ticket. Probá de nuevo en un momento.")


class DepositsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.close_delay = CLOSE_DELAY_SECONDS

    async def cog_load(self):
        await self.run(ddb.init_schema)
        self.bot.add_view(DepositPanelView(self))
        self.bot.add_view(DepositCloseView(self))

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @staticmethod
    def panel_embed() -> discord.Embed:
        emb = discord.Embed(title="🪙 Pasar silver a Wuju Coins", color=GREEN)
        emb.description = (
            "¿Querés cambiar silver por **Wuju Coins**?\n\n"
            "1. Tocá **Crear ticket** e ingresá cuánto silver querés pasar.\n"
            "2. Se abre un ticket privado: mandá ahí la **captura del depósito** de ese silver a la guild.\n"
            "3. Un **Council** lo revisa y acredita tus Wuju Coins.")
        return emb

    # ---------- /agregar wuju-coins ----------
    agregar = app_commands.Group(name="agregar", description="Agregar Wuju Coins", guild_only=True)

    @agregar.command(name="wuju-coins", description="(Council) Publica el panel para pasar silver a Wuju Coins")
    @app_commands.describe(canal="Canal donde publicar el panel (por defecto, este canal)")
    async def agregar_wuju_coins(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo publicar en ese canal.")
            await interaction.response.defer(ephemeral=True)
            try:
                msg = await channel.send(embed=self.panel_embed(), view=DepositPanelView(self), allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException) as exc:
                return await interaction.followup.send(f"❌ No pude publicar el panel en {channel.mention}: {exc}", ephemeral=True)
            old = await self.run(ddb.get_panel, interaction.guild.id)
            await self.run(ddb.set_panel, interaction.guild.id, channel.id, msg.id)
            if old and old[1] != msg.id:
                try:
                    och = interaction.guild.get_channel(old[0]) or await self.bot.fetch_channel(old[0])
                    await (await och.fetch_message(old[1])).delete()
                except Exception:
                    pass
            await interaction.followup.send(f"✅ Panel publicado en {channel.mention}.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    # ---------- botón → modal → ticket ----------
    async def on_open_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        existing = await self.run(ddb.open_ticket_of, interaction.guild.id, interaction.user.id)
        if existing is not None:
            return await _reply(interaction, f"ℹ️ Ya tenés un ticket abierto: <#{existing.channel_id}>.")
        await interaction.response.send_modal(SilverModal(self))

    async def on_silver_submitted(self, interaction: discord.Interaction, silver_text: str):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        try:
            silver = ddb.parse_silver(silver_text)
        except ddb.DepositError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await interaction.response.defer(ephemeral=True)
        existing = await self.run(ddb.open_ticket_of, guild.id, interaction.user.id)
        if existing is not None:
            return await interaction.followup.send(f"ℹ️ Ya tenés un ticket abierto: <#{existing.channel_id}>.", ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }
        council = guild.get_role(COUNCIL_ROLE_ID)
        if council is not None:
            overwrites[council] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        name = safe_channel_name(interaction.user.name, interaction.user.id).replace("shop-", "deposito-", 1)
        try:
            channel = await guild.create_text_channel(name=name, category=getattr(interaction.channel, "category", None),
                                                      overwrites=overwrites, topic=f"Depósito de silver · usuario {interaction.user.id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.followup.send(f"❌ No pude crear el ticket (revisá mis permisos de **Gestionar canales**): {exc}", ephemeral=True)
        try:
            ticket_id = await self.run(ddb.create_ticket, guild.id, channel.id, interaction.user.id, silver)
        except Exception:
            traceback.print_exc()
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude registrar el ticket. Probá de nuevo.", ephemeral=True)
        emb = discord.Embed(title=f"🪙 Ticket de depósito #{ticket_id}", color=GREEN)
        emb.add_field(name="Usuario", value=interaction.user.mention, inline=True)
        emb.add_field(name="Silver a pasar a Wuju Coins", value=f"**{ddb.eco.format_amount(silver)}**", inline=True)
        emb.add_field(name="📸 Falta un paso", value=(f"{interaction.user.mention}, mandá en este canal la **foto / captura del depósito** "
                                                       "de ese silver a la guild. Un Council la va a revisar."), inline=False)
        emb.set_footer(text="Solo los Council pueden cerrar este ticket.")
        try:
            await channel.send(f"{interaction.user.mention} <@&{COUNCIL_ROLE_ID}>", embed=emb, view=DepositCloseView(self), allowed_mentions=PING)
        except Exception:
            traceback.print_exc()
            await self.run(ddb.discard_ticket, ticket_id)
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude abrir el ticket. Probá de nuevo.", ephemeral=True)
        await interaction.followup.send(f"✅ Ticket abierto: {channel.mention}", ephemeral=True)

    @staticmethod
    async def _try_delete(channel):
        try:
            await channel.delete()
        except Exception:
            pass

    # ---------- cerrar (solo Council) ----------
    async def on_close_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None or not is_council(interaction.user):
            return await _reply(interaction, "❌ Solo los **Council** pueden cerrar este ticket.")
        ticket = await self.run(ddb.get_by_channel, interaction.channel.id)
        if ticket is None:
            return await _reply(interaction, "❌ Este canal no es un ticket de depósito.")
        if not await self.run(ddb.close_ticket, ticket.id, interaction.user.id):
            return await _reply(interaction, "ℹ️ Este ticket ya estaba cerrado.")
        await interaction.response.send_message(f"🔒 Ticket cerrado por {interaction.user.mention}. El canal se borra en unos segundos.",
                                                allowed_mentions=NO_MENTIONS)
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        await self._try_delete(interaction.channel)


async def setup_deposits(bot: commands.Bot, db_path: str, guild_id: int) -> DepositsCog:
    cog = DepositsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
