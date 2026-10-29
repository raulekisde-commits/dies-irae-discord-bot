"""/panel cambiomoneda — dos paneles con tickets: vender Wuju Coins por silver y comprar Wuju Coins con silver (capa de Discord)."""
import asyncio
import traceback
from typing import Optional

import discord
from discord.ext import commands

import deposits_db as ddb
import economy_db as eco
from guildshop import COUNCIL_ROLE_ID, can_manage, safe_channel_name

NO_MENTIONS = discord.AllowedMentions.none()
PING = discord.AllowedMentions(roles=True, users=True, everyone=False)
CLOSE_DELAY_SECONDS = 5
GREEN, ORANGE = 0x2ECC71, 0xE67E22


def is_council(member) -> bool:
    """Solo el rol Council (se evalúa en cada pulsación)."""
    return any(getattr(r, "id", None) == COUNCIL_ROLE_ID for r in getattr(member, "roles", []))


async def _reply(interaction: discord.Interaction, content: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


# ============================================================
# Vistas
# ============================================================
class SellPanelView(discord.ui.View):
    def __init__(self, cog: "DepositsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cobrar balance", style=discord.ButtonStyle.primary, custom_id="cm:sell_open", emoji="💰")
    async def open(self, interaction: discord.Interaction, button):
        await self.cog.on_sell_open(interaction)


class BuyPanelView(discord.ui.View):
    def __init__(self, cog: "DepositsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Comprar Wuju Coins", style=discord.ButtonStyle.success, custom_id="cm:buy_open", emoji="🪙")
    async def open(self, interaction: discord.Interaction, button):
        await self.cog.on_buy_open(interaction)


class SellTicketView(discord.ui.View):
    """Botones del ticket de venta. Solo los Council pueden usarlos (se revisa en cada pulsación)."""

    def __init__(self, cog: "DepositsCog", processed: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if not processed:
            accept = discord.ui.Button(label="Aceptar", style=discord.ButtonStyle.success, custom_id="cm:sell_accept", emoji="✅")
            accept.callback = self._accept
            self.add_item(accept)
        close = discord.ui.Button(label="Cerrar", style=discord.ButtonStyle.danger, custom_id="cm:sell_close", emoji="🔒")
        close.callback = self._close
        self.add_item(close)

    async def _accept(self, interaction: discord.Interaction):
        await self.cog.on_sell_accept(interaction)

    async def _close(self, interaction: discord.Interaction):
        await self.cog.on_close(interaction)


class BuyTicketView(discord.ui.View):
    def __init__(self, cog: "DepositsCog", processed: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if not processed:
            add = discord.ui.Button(label="Agregar Wuju Coins", style=discord.ButtonStyle.success, custom_id="cm:buy_add", emoji="➕")
            add.callback = self._add
            self.add_item(add)
        close = discord.ui.Button(label="Cerrar", style=discord.ButtonStyle.danger, custom_id="cm:buy_close", emoji="🔒")
        close.callback = self._close
        self.add_item(close)

    async def _add(self, interaction: discord.Interaction):
        await self.cog.on_buy_add(interaction)

    async def _close(self, interaction: discord.Interaction):
        await self.cog.on_close(interaction)


class ConfirmAddView(discord.ui.View):
    """Segunda confirmación (efímera) antes de agregar los Wuju Coins."""

    def __init__(self, cog: "DepositsCog", ticket_id: int, owner_id: int, ticket_message):
        super().__init__(timeout=120.0)
        self.cog, self.ticket_id, self.owner_id, self.ticket_message, self.used = cog, ticket_id, owner_id, ticket_message, False
        ok = discord.ui.Button(label="Sí, agregar", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_council(interaction.user):
            await _reply(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _ok(self, interaction: discord.Interaction):
        if self.used:
            return await _reply(interaction, "ℹ️ Ya fue procesado.")
        self.used = True
        self.stop()
        await self.cog.confirm_buy_add(interaction, self.ticket_id, self.ticket_message)

    async def _no(self, interaction: discord.Interaction):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelado. No se agregó nada.", view=None)


# ============================================================
# Modales
# ============================================================
class BuyAmountModal(discord.ui.Modal, title="Comprar Wuju Coins"):
    amount = discord.ui.TextInput(label="Cantidad de Wuju Coins que querés comprar", style=discord.TextStyle.short, required=True,
                                  max_length=25, placeholder="Ej: 1.500.000")

    def __init__(self, cog: "DepositsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_buy_submitted(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude abrir el ticket. Probá de nuevo en un momento.")


class SellAcceptModal(discord.ui.Modal):
    def __init__(self, cog: "DepositsCog", ticket_id: int, default_amount: int, ticket_message):
        super().__init__(title="Retirar Wuju Coins")
        self.cog, self.ticket_id, self.ticket_message = cog, ticket_id, ticket_message
        self.amount = discord.ui.TextInput(label="Monto de Wuju Coins a retirar", style=discord.TextStyle.short, required=True,
                                           max_length=25, default=eco.format_amount(default_amount),
                                           placeholder="Por defecto, todo el balance de la persona")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_sell_submitted(interaction, self.ticket_id, str(self.amount.value), self.ticket_message)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude retirar los Wuju Coins. No se movió nada.")


# ============================================================
# Cog
# ============================================================
class DepositsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.close_delay = CLOSE_DELAY_SECONDS

    async def cog_load(self):
        await self.run(ddb.init_schema)
        for view in (SellPanelView(self), BuyPanelView(self), SellTicketView(self), BuyTicketView(self)):
            self.bot.add_view(view)

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def money(self, amount: int) -> str:
        e = self.economy
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    # ---------- paneles ----------
    @staticmethod
    def sell_panel_embed() -> discord.Embed:
        emb = discord.Embed(title="💰 Cobrar balance", color=ORANGE)
        emb.description = (
            "¿Querés cobrar tu balance de **Wuju Coins** en silver?\n\n"
            "1. Tocá **Cobrar balance**: se abre un ticket privado y se avisa al **Council**.\n"
            "2. En el ticket aparece tu balance. Un Council acepta, confirma el monto (por defecto, todo tu balance) y te lo retira.\n"
            "3. Te entregan el silver correspondiente.")
        return emb

    @staticmethod
    def buy_panel_embed() -> discord.Embed:
        emb = discord.Embed(title="🪙 Comprar Wuju Coins", color=GREEN)
        emb.description = (
            "¿Querés comprar **Wuju Coins** con silver?\n\n"
            "1. **Depositá el silver** en la guild.\n"
            "2. Tocá **Comprar Wuju Coins** e indicá cuántos querés comprar: se abre un ticket privado.\n"
            "3. En el ticket mandá la **foto / captura del depósito**.\n"
            "4. Un **Council** la revisa y te agrega los Wuju Coins.")
        return emb

    async def publish_panels(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        """/panel cambiomoneda: publica los dos paneles (vender y comprar) en el canal."""
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo publicar en ese canal.")
            await interaction.response.defer(ephemeral=True)
            posted = []
            try:
                posted.append(("sell", await channel.send(embed=self.sell_panel_embed(), view=SellPanelView(self), allowed_mentions=NO_MENTIONS)))
                posted.append(("buy", await channel.send(embed=self.buy_panel_embed(), view=BuyPanelView(self), allowed_mentions=NO_MENTIONS)))
            except (discord.Forbidden, discord.HTTPException) as exc:
                for _, m in posted:
                    try:
                        await m.delete()
                    except Exception:
                        pass
                return await interaction.followup.send(f"❌ No pude publicar los paneles en {channel.mention}: {exc}", ephemeral=True)
            for kind, msg in posted:
                old = await self.run(ddb.get_panel, interaction.guild.id, kind)
                await self.run(ddb.set_panel, interaction.guild.id, kind, channel.id, msg.id)
                if old and old[1] != msg.id:
                    try:
                        och = interaction.guild.get_channel(old[0]) or await self.bot.fetch_channel(old[0])
                        await (await och.fetch_message(old[1])).delete()
                    except Exception:
                        pass
            await interaction.followup.send(f"✅ Paneles publicados en {channel.mention}.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    # ---------- abrir tickets ----------
    async def on_sell_open(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        existing = await self.run(ddb.open_ticket_of, interaction.guild.id, interaction.user.id, "sell")
        if existing is not None:
            return await _reply(interaction, f"ℹ️ Ya tenés un ticket de venta abierto: <#{existing.channel_id}>.")
        balance = await self.run(eco.get_balance, interaction.user.id)
        if balance <= 0:
            return await _reply(interaction, f"❌ No tenés Wuju Coins para vender. Tu balance: {self.money(balance)}.")
        await interaction.response.defer(ephemeral=True)
        await self._create_ticket(interaction, "sell", 0)

    async def on_buy_open(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        existing = await self.run(ddb.open_ticket_of, interaction.guild.id, interaction.user.id, "buy")
        if existing is not None:
            return await _reply(interaction, f"ℹ️ Ya tenés un ticket de compra abierto: <#{existing.channel_id}>.")
        await interaction.response.send_modal(BuyAmountModal(self))

    async def on_buy_submitted(self, interaction: discord.Interaction, text: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        try:
            amount = ddb.parse_amount(text)
        except ddb.DepositError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await interaction.response.defer(ephemeral=True)
        existing = await self.run(ddb.open_ticket_of, interaction.guild.id, interaction.user.id, "buy")
        if existing is not None:
            return await interaction.followup.send(f"ℹ️ Ya tenés un ticket de compra abierto: <#{existing.channel_id}>.", ephemeral=True)
        await self._create_ticket(interaction, "buy", amount)

    async def _create_ticket(self, interaction: discord.Interaction, kind: str, amount: int):
        guild, user = interaction.guild, interaction.user
        perms = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: discord.PermissionOverwrite(**perms)}
        council = guild.get_role(COUNCIL_ROLE_ID)
        if council is not None:
            overwrites[council] = discord.PermissionOverwrite(**perms)
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        prefix = "venta" if kind == "sell" else "compra"
        name = safe_channel_name(user.name, user.id).replace("shop-", f"{prefix}-", 1)
        try:
            channel = await guild.create_text_channel(name=name, category=getattr(interaction.channel, "category", None), overwrites=overwrites,
                                                      topic=f"Cambio de moneda ({prefix}) · usuario {user.id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.followup.send(f"❌ No pude crear el ticket (revisá mis permisos de **Gestionar canales**): {exc}", ephemeral=True)
        try:
            ticket_id = await self.run(ddb.create_ticket, guild.id, channel.id, user.id, kind, amount)
        except Exception:
            traceback.print_exc()
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude registrar el ticket. Probá de nuevo.", ephemeral=True)

        try:
            eco_cog = self.economy
            bal_emb = await eco_cog.bal_embed(user) if eco_cog is not None else None
            if kind == "sell":
                emb = discord.Embed(title=f"💸 Ticket de venta #{ticket_id}", color=ORANGE)
                emb.add_field(name="Usuario", value=user.mention, inline=True)
                emb.add_field(name="Qué quiere", value="Cambiar sus Wuju Coins por silver", inline=True)
                emb.add_field(name="Council", value="**Aceptar** para elegir el monto y retirar los Wuju Coins, o **Cerrar** para cancelar.", inline=False)
                view = SellTicketView(self)
            else:
                emb = discord.Embed(title=f"🪙 Ticket de compra #{ticket_id}", color=GREEN)
                emb.add_field(name="Usuario", value=user.mention, inline=True)
                emb.add_field(name="Wuju Coins que quiere comprar", value=f"**{self.money(amount)}**", inline=True)
                emb.add_field(name="📸 Falta un paso", value=(f"{user.mention}, depositá el silver en la guild y mandá en este canal la **foto / captura del depósito**. "
                                                               "Un Council la va a revisar."), inline=False)
                emb.set_footer(text="Solo los Council pueden agregar los Wuju Coins o cerrar este ticket.")
                view = BuyTicketView(self)
            embeds = [emb] + ([bal_emb] if bal_emb is not None else [])
            await channel.send(f"{user.mention} <@&{COUNCIL_ROLE_ID}>", embeds=embeds, view=view, allowed_mentions=PING)
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
    async def on_close(self, interaction: discord.Interaction):
        if interaction.guild is None or not is_council(interaction.user):
            return await _reply(interaction, "❌ Solo los **Council** pueden usar este botón.")
        ticket = await self.run(ddb.get_by_channel, interaction.channel.id)
        if ticket is None:
            return await _reply(interaction, "❌ Este canal no es un ticket de cambio de moneda.")
        if not await self.run(ddb.close_ticket, ticket.id, interaction.user.id):
            return await _reply(interaction, "ℹ️ Este ticket ya estaba cerrado.")
        await interaction.response.send_message(f"🔒 Ticket cerrado por {interaction.user.mention}. El canal se borra en unos segundos.",
                                                allowed_mentions=NO_MENTIONS)
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        await self._try_delete(interaction.channel)

    async def _council_ticket(self, interaction: discord.Interaction, kind: str) -> Optional[ddb.Ticket]:
        """Valida Council + ticket del tipo correcto, abierto y sin procesar. Responde el error y devuelve None si algo falla."""
        if interaction.guild is None or not is_council(interaction.user):
            await _reply(interaction, "❌ Solo los **Council** pueden usar este botón.")
            return None
        ticket = await self.run(ddb.get_by_channel, interaction.channel.id)
        if ticket is None or ticket.kind != kind:
            await _reply(interaction, "❌ Este canal no es un ticket de este tipo.")
            return None
        if ticket.status != "open":
            await _reply(interaction, "ℹ️ Este ticket ya estaba cerrado.")
            return None
        if ticket.processed:
            await _reply(interaction, "ℹ️ En este ticket ya se hizo el movimiento de Wuju Coins.")
            return None
        return ticket

    async def _publish_log(self, op_id: str):
        eco_cog = self.economy
        if eco_cog is None:
            return
        try:
            await eco_cog.publish_log(op_id)
        except Exception:
            traceback.print_exc()

    # ---------- venta: Aceptar → monto → retirar ----------
    async def on_sell_accept(self, interaction: discord.Interaction):
        ticket = await self._council_ticket(interaction, "sell")
        if ticket is None:
            return
        balance = await self.run(eco.get_balance, ticket.user_id)
        if balance <= 0:
            return await _reply(interaction, f"❌ <@{ticket.user_id}> ya no tiene Wuju Coins para retirar (balance: {self.money(balance)}).")
        await interaction.response.send_modal(SellAcceptModal(self, ticket.id, balance, getattr(interaction, "message", None)))

    async def on_sell_submitted(self, interaction: discord.Interaction, ticket_id: int, text: str, ticket_message):
        if interaction.guild is None or not is_council(interaction.user):
            return await _reply(interaction, "❌ Solo los **Council** pueden hacer esto.")
        ticket = await self.run(ddb.get_ticket, ticket_id)
        if ticket is None or ticket.kind != "sell" or ticket.status != "open":
            return await _reply(interaction, "❌ Este ticket ya no está disponible.")
        if ticket.processed:
            return await _reply(interaction, "ℹ️ En este ticket ya se retiraron los Wuju Coins.")
        try:
            amount = ddb.parse_amount(text)
        except ddb.DepositError as exc:
            return await _reply(interaction, f"❌ {exc}")
        reason = f"Cambio de Wuju Coins por silver (ticket #{ticket.id})"
        try:
            op = await self.run(eco.apply_operation, kind="remove", actor_id=interaction.user.id, user_ids=[ticket.user_id], amount=amount,
                                request_key=f"cm:{ticket.id}:sell", reason=reason, require_funds=True)
        except eco.InsufficientFunds as exc:
            balance = await self.run(eco.get_balance, ticket.user_id)
            return await _reply(interaction, f"❌ <@{ticket.user_id}> tiene {self.money(balance)}: no alcanza para retirar {self.money(amount)}. No se movió nada.")
        except Exception:
            traceback.print_exc()
            return await _reply(interaction, "❌ No se pudo retirar. No se movió nada.")
        if not op.created:
            return await _reply(interaction, "ℹ️ Esta operación ya se había hecho; no se repitió.")
        await self.run(ddb.mark_processed, ticket.id, amount, interaction.user.id)
        after = op.movements[0].after
        await interaction.response.send_message(
            f"✅ Se retiraron **{self.money(amount)}** de <@{ticket.user_id}> (ticket #{ticket.id}). Balance nuevo: {self.money(after)}.\n"
            f"Ahora entregale el silver correspondiente y cerrá el ticket.", allowed_mentions=NO_MENTIONS)
        await self._disable_action(ticket_message, SellTicketView(self, processed=True))
        await self._publish_log(op.op_id)

    # ---------- compra: Agregar → confirmar → agregar ----------
    async def on_buy_add(self, interaction: discord.Interaction):
        ticket = await self._council_ticket(interaction, "buy")
        if ticket is None:
            return
        view = ConfirmAddView(self, ticket.id, interaction.user.id, getattr(interaction, "message", None))
        await interaction.response.send_message(
            f"⚠️ ¿Confirmás que querés **agregar {self.money(ticket.amount)}** a <@{ticket.user_id}>? "
            f"Hacelo solo si ya revisaste la captura del depósito.", view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def confirm_buy_add(self, interaction: discord.Interaction, ticket_id: int, ticket_message):
        if interaction.guild is None or not is_council(interaction.user):
            return await _reply(interaction, "❌ Solo los **Council** pueden hacer esto.")
        ticket = await self.run(ddb.get_ticket, ticket_id)
        if ticket is None or ticket.kind != "buy" or ticket.status != "open":
            return await _reply(interaction, "❌ Este ticket ya no está disponible.")
        if ticket.processed:
            return await _reply(interaction, "ℹ️ En este ticket ya se agregaron los Wuju Coins.")
        reason = f"Compra de Wuju Coins con silver (ticket #{ticket.id})"
        try:
            op = await self.run(eco.apply_operation, kind="add", actor_id=interaction.user.id, user_ids=[ticket.user_id], amount=ticket.amount,
                                request_key=f"cm:{ticket.id}:buy", reason=reason)
        except OverflowError:
            return await _reply(interaction, "❌ El balance resultante superaría el límite permitido. No se agregó nada.")
        except Exception:
            traceback.print_exc()
            return await _reply(interaction, "❌ No se pudo agregar. No se movió nada.")
        if not op.created:
            return await interaction.response.edit_message(content="ℹ️ Esta operación ya se había hecho; no se repitió.", view=None)
        await self.run(ddb.mark_processed, ticket.id, ticket.amount, interaction.user.id)
        after = op.movements[0].after
        await interaction.response.edit_message(content="✅ Listo, se agregaron los Wuju Coins.", view=None)
        ch = interaction.channel
        if ch is not None:
            await ch.send(f"✅ Se agregaron **{self.money(ticket.amount)}** a <@{ticket.user_id}> (ticket #{ticket.id}). "
                          f"Balance nuevo: {self.money(after)}. Motivo registrado: {reason}.", allowed_mentions=NO_MENTIONS)
        await self._disable_action(ticket_message, BuyTicketView(self, processed=True))
        await self._publish_log(op.op_id)

    @staticmethod
    async def _disable_action(message, view):
        if message is None:
            return
        try:
            await message.edit(view=view)
        except Exception:
            pass


async def setup_deposits(bot: commands.Bot, db_path: str, guild_id: int) -> DepositsCog:
    cog = DepositsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
