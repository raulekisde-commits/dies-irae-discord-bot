"""/panel embo — panel con ticket para comprar embo o saldar la deuda de embo pagando con Wuju Coins (capa de Discord).

Solo el Council acepta o cancela estos tickets. Al aceptar, el Council confirma la cantidad de embo y el precio por unidad:
se restan los Wuju Coins y se suman las embo. Si no le alcanzan los Wuju Coins, no se mueve nada y el ticket se cierra.
"""
import asyncio
import traceback
from typing import Optional

import discord
from discord.ext import commands

import botsettings as bs
import economy_db as eco
import embo_db
import embo_tickets_db as etdb
from guildshop import COUNCIL_ROLE_ID, safe_channel_name

NO_MENTIONS = discord.AllowedMentions.none()
PING = discord.AllowedMentions(roles=True, users=True, everyone=False)
CLOSE_DELAY_SECONDS = 8
TEAL = 0x1ABC9C


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


def fmt(n: int) -> str:
    return eco.format_amount(n)


# ============================================================
# Vistas
# ============================================================
class EmboPanelView(discord.ui.View):
    def __init__(self, cog: "EmboTicketsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Abrir ticket de embo", style=discord.ButtonStyle.primary, custom_id="embo:open", emoji="🍾")
    async def open(self, interaction: discord.Interaction, button):
        await self.cog.on_open(interaction)


class EmboChoiceView(discord.ui.View):
    """Mensaje efímero: ¿comprar embo o saldar la deuda?"""

    def __init__(self, cog: "EmboTicketsCog", owner_id: int):
        super().__init__(timeout=120.0)
        self.cog, self.owner_id = cog, owner_id
        buy = discord.ui.Button(label="Comprar embo", style=discord.ButtonStyle.success, emoji="🛒")
        debt = discord.ui.Button(label="Saldar deuda", style=discord.ButtonStyle.primary, emoji="💳")
        buy.callback, debt.callback = self._buy, self._debt
        self.add_item(buy)
        self.add_item(debt)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _reply(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _buy(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyAmountModal(self.cog))

    async def _debt(self, interaction: discord.Interaction):
        await self.cog.on_debt_chosen(interaction)


class EmboTicketView(discord.ui.View):
    """Botones del ticket. Solo el Council puede usarlos (se revisa en cada pulsación)."""

    def __init__(self, cog: "EmboTicketsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Aceptar", style=discord.ButtonStyle.success, custom_id="embo:accept", emoji="✅")
    async def accept(self, interaction: discord.Interaction, button):
        await self.cog.on_accept(interaction)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, custom_id="embo:cancel", emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel(interaction)


# ============================================================
# Modales
# ============================================================
class BuyAmountModal(discord.ui.Modal, title="Comprar embo"):
    amount = discord.ui.TextInput(label="¿Cuántas embo querés comprar?", style=discord.TextStyle.short, required=True,
                                  max_length=15, placeholder="Ej: 50")

    def __init__(self, cog: "EmboTicketsCog", note: Optional[str] = None):
        super().__init__()
        self.cog = cog
        if note:
            self.title = note[:45]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_buy_submitted(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude abrir el ticket. Probá de nuevo en un momento.")


class AcceptModal(discord.ui.Modal):
    def __init__(self, cog: "EmboTicketsCog", ticket_id: int, default_qty: int):
        super().__init__(title="Confirmar embo")
        self.cog, self.ticket_id = cog, ticket_id
        self.qty = discord.ui.TextInput(label="Cantidad de embo", style=discord.TextStyle.short, required=True, max_length=15,
                                        default=fmt(default_qty))
        self.price = discord.ui.TextInput(label="Precio por unidad (Wuju Coins)", style=discord.TextStyle.short, required=True,
                                          max_length=25, placeholder="Ej: 150.000")
        self.add_item(self.qty)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_accept_submitted(interaction, self.ticket_id, str(self.qty.value), str(self.price.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No se pudo completar. No se movió nada.")


# ============================================================
# Cog
# ============================================================
class EmboTicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.close_delay = CLOSE_DELAY_SECONDS

    async def cog_load(self):
        await self.run(etdb.init_schema)
        for view in (EmboPanelView(self), EmboTicketView(self)):
            self.bot.add_view(view)

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def money(self, amount: int) -> str:
        e = self.economy
        return e.money(amount) if e is not None else f"{fmt(amount)} {eco.CURRENCY_NAME}"

    @property
    def icon(self) -> str:
        e = self.economy
        return getattr(e, "embo_icon", None) or embo_db.EMOJI

    # ---------- panel ----------
    def panel_embed(self) -> discord.Embed:
        emb = discord.Embed(title=f"{self.icon} Embo — comprar o saldar deuda", color=TEAL)
        emb.description = (
            "¿Querés **comprar embo** o **saldar tu deuda de embo** con Wuju Coins?\n\n"
            "1. Tocá **Abrir ticket de embo** y elegí **Comprar embo** o **Saldar deuda**.\n"
            "2. Se abre un ticket privado y se avisa al **Council**.\n"
            "3. El Council confirma la cantidad y el precio por unidad: se te descuentan los Wuju Coins y se te suman las embo.\n\n"
            "⚠️ Si no te alcanzan los Wuju Coins, el ticket se cierra sin cobrar nada.")
        return emb

    async def publish_panel(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        """/panel embo (solo Council)."""
        try:
            if interaction.guild is None or not is_council(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council** puede usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo publicar en ese canal.")
            try:
                await channel.send(embed=self.panel_embed(), view=EmboPanelView(self), allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException) as exc:
                return await _reply(interaction, f"❌ No pude publicar el panel en {channel.mention}: {exc}")
            await _reply(interaction, f"✅ Panel de embo publicado en {channel.mention}.")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    # ---------- abrir ticket ----------
    async def _existing(self, interaction: discord.Interaction) -> bool:
        t = await self.run(etdb.open_ticket_of, interaction.guild.id, interaction.user.id)
        if t is not None:
            await _reply(interaction, f"ℹ️ Ya tenés un ticket de embo abierto: <#{t.channel_id}>.")
            return True
        return False

    async def on_open(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        if await self._existing(interaction):
            return
        balance = await self.run(embo_db.get_balance, interaction.user.id)
        await interaction.response.send_message(
            f"{self.icon} ¿Qué querés hacer?\nTu balance de embo: **{fmt(balance)}**.",
            view=EmboChoiceView(self, interaction.user.id), ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def on_debt_chosen(self, interaction: discord.Interaction):
        if await self._existing(interaction):
            return
        balance = await self.run(embo_db.get_balance, interaction.user.id)
        if balance >= 0:                                         # no debe nada: pasa a comprar
            return await interaction.response.send_modal(BuyAmountModal(self, "No debés embo: ¿cuántas querés comprar?"))
        await interaction.response.edit_message(content="⏳ Abriendo tu ticket…", view=None)
        await self._create_ticket(interaction, "debt", -balance)

    async def on_buy_submitted(self, interaction: discord.Interaction, text: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        try:
            amount = etdb.parse_amount(text)
        except etdb.EmboTicketError as exc:
            return await _reply(interaction, f"❌ {exc}")
        if await self._existing(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._create_ticket(interaction, "buy", amount)

    def ticket_embed(self, ticket_id: int, user, kind: str, amount: int) -> discord.Embed:
        title = "Saldar deuda de embo" if kind == "debt" else "Compra de embo"
        emb = discord.Embed(title=f"{self.icon} {title} #{ticket_id}", color=TEAL)
        emb.add_field(name="Usuario", value=user.mention, inline=True)
        emb.add_field(name="Debe" if kind == "debt" else "Quiere comprar", value=f"**{fmt(amount)}** embo", inline=True)
        emb.add_field(name="Council", value="**Aceptar** para confirmar la cantidad de embo y el precio por unidad "
                                            "(se descuentan los Wuju Coins y se suman las embo), o **Cancelar** para cerrar el ticket.",
                      inline=False)
        emb.set_footer(text="Solo el Council puede aceptar o cancelar este ticket.")
        return emb

    async def _create_ticket(self, interaction: discord.Interaction, kind: str, amount: int):
        guild, user = interaction.guild, interaction.user
        perms = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: discord.PermissionOverwrite(**perms)}
        council = guild.get_role(COUNCIL_ROLE_ID)
        if council is not None:
            overwrites[council] = discord.PermissionOverwrite(**perms)
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        name = safe_channel_name(user.name, user.id).replace("shop-", "embo-", 1)
        try:
            channel = await guild.create_text_channel(name=name, category=getattr(interaction.channel, "category", None),
                                                      overwrites=overwrites, topic=f"Ticket de embo ({kind}) · usuario {user.id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.followup.send(f"❌ No pude crear el ticket (revisá mis permisos de **Gestionar canales**): {exc}",
                                                   ephemeral=True)
        try:
            ticket_id = await self.run(etdb.create_ticket, guild.id, channel.id, user.id, kind, amount)
        except Exception:
            traceback.print_exc()
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude registrar el ticket. Probá de nuevo.", ephemeral=True)
        try:
            eco_cog = self.economy
            bal_emb = await eco_cog.bal_embed(user) if eco_cog is not None else None        # Wuju Coins + embo
            embeds = [self.ticket_embed(ticket_id, user, kind, amount)] + ([bal_emb] if bal_emb is not None else [])
            sent = await channel.send(f"{user.mention} <@&{COUNCIL_ROLE_ID}>", embeds=embeds, view=EmboTicketView(self),
                                      allowed_mentions=PING)
            await self.run(etdb.set_message, ticket_id, sent.id)
        except Exception:
            traceback.print_exc()
            await self.run(etdb.discard_ticket, ticket_id)
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude abrir el ticket. Probá de nuevo.", ephemeral=True)
        await interaction.followup.send(f"✅ Ticket abierto: {channel.mention}", ephemeral=True)

    # ---------- botones del ticket (solo Council) ----------
    async def _council_ticket(self, interaction: discord.Interaction) -> Optional[etdb.Ticket]:
        if interaction.guild is None or not is_council(interaction.user):
            await _reply(interaction, "❌ Solo el **Council** puede usar este botón.")
            return None
        ticket = await self.run(etdb.get_by_channel, interaction.channel.id)
        if ticket is None:
            await _reply(interaction, "❌ Este canal no es un ticket de embo.")
            return None
        if ticket.status != "open":
            await _reply(interaction, "ℹ️ Este ticket ya estaba cerrado.")
            return None
        return ticket

    async def on_accept(self, interaction: discord.Interaction):
        ticket = await self._council_ticket(interaction)
        if ticket is None:
            return
        default = ticket.amount
        if ticket.kind == "debt":                                # la deuda puede haber cambiado desde que se abrió
            balance = await self.run(embo_db.get_balance, ticket.user_id)
            if balance < 0:
                default = -balance
        await interaction.response.send_modal(AcceptModal(self, ticket.id, default))

    async def on_accept_submitted(self, interaction: discord.Interaction, ticket_id: int, qty_text: str, price_text: str):
        if interaction.guild is None or not is_council(interaction.user):
            return await _reply(interaction, "❌ Solo el **Council** puede hacer esto.")
        try:
            qty = etdb.parse_amount(qty_text)
            price = etdb.parse_amount(price_text)
        except etdb.EmboTicketError as exc:
            return await _reply(interaction, f"❌ {exc}")
        ticket = await self.run(etdb.get_ticket, ticket_id)
        res = await self.run(etdb.settle, ticket_id, interaction.user.id, qty, price)
        if res.status == "invalid":
            return await _reply(interaction, f"❌ {res.message}")
        if res.status in ("closed", "done"):
            return await _reply(interaction, "ℹ️ Este ticket ya fue procesado o cerrado.")
        if res.status == "funds":
            await self.run(etdb.close_ticket, ticket_id, interaction.user.id, "funds")
            await interaction.response.send_message(
                f"❌ A <@{ticket.user_id}> no le alcanzan los Wuju Coins: tiene **{self.money(res.wuju_before)}** y hacen falta "
                f"**{self.money(res.total)}** ({fmt(qty)} embo × {self.money(price)}). No se movió nada.\n"
                f"🔒 Ticket cerrado: el canal se borra en unos segundos.", allowed_mentions=NO_MENTIONS)
            await self._record_task(interaction.user.id, ticket)
            await self._log(interaction.guild, ticket, interaction.user, "❌ Ticket de embo cerrado: no le alcanzaban los Wuju Coins",
                            0xE74C3C, qty, price, res)
            return await self._delete_later(interaction.channel)
        await interaction.response.send_message(
            f"✅ <@{ticket.user_id}>: se descontaron **{self.money(res.total)}** ({fmt(qty)} embo × {self.money(price)}) "
            f"y se sumaron **{fmt(qty)}** embo.\n"
            f"🪙 Wuju Coins: {self.money(res.wuju_before)} → **{self.money(res.wuju_after)}**\n"
            f"{self.icon} Embo: {fmt(res.embo_before)} → **{fmt(res.embo_after)}**\n"
            f"🔒 Ticket cerrado: el canal se borra en unos segundos.", allowed_mentions=NO_MENTIONS)
        await self._disable_buttons(interaction.channel, ticket)
        await self._record_task(interaction.user.id, ticket)
        await self._log(interaction.guild, ticket, interaction.user, f"{self.icon} Embo: ticket aceptado", TEAL, qty, price, res)
        await self._delete_later(interaction.channel)

    async def on_cancel(self, interaction: discord.Interaction):
        ticket = await self._council_ticket(interaction)
        if ticket is None:
            return
        if not await self.run(etdb.close_ticket, ticket.id, interaction.user.id, "cancelled"):
            return await _reply(interaction, "ℹ️ Este ticket ya estaba cerrado.")
        await interaction.response.send_message(f"✖️ Ticket cancelado por {interaction.user.mention}. El canal se borra en unos segundos.",
                                                allowed_mentions=NO_MENTIONS)
        await self._record_task(interaction.user.id, ticket)
        await self._log(interaction.guild, ticket, interaction.user, "✖️ Ticket de embo cancelado", 0x95A5A6)
        await self._delete_later(interaction.channel)

    # ---------- utilidades ----------
    async def _record_task(self, actor_id: int, ticket: etdb.Ticket):
        """Cerrar el ticket de otra persona cuenta como tarea finalizada para /top tareas (una vez por ticket)."""
        enc = self.bot.get_cog("EncargosCog")
        if enc is not None and actor_id != ticket.user_id:
            await enc.record_done(f"Cerrar el ticket de embo #{ticket.id}", actor_id, f"embo:{ticket.id}")

    async def _log(self, guild, ticket: etdb.Ticket, actor, title: str, color: int, qty: int = 0, price: int = 0,
                   res: Optional[etdb.SettleResult] = None):
        try:
            channel = guild.get_channel(bs.get("log")) or await guild.fetch_channel(bs.get("log"))
        except Exception:
            return
        emb = discord.Embed(title=title, color=color)
        emb.add_field(name="Ticket", value=f"#{ticket.id} · {'saldar deuda' if ticket.kind == 'debt' else 'compra'}", inline=True)
        emb.add_field(name="Usuario", value=f"<@{ticket.user_id}>", inline=True)
        emb.add_field(name="Council", value=actor.mention, inline=True)
        if qty:
            emb.add_field(name="Cantidad × precio", value=f"{fmt(qty)} embo × {self.money(price)} = **{self.money(qty * price)}**", inline=False)
        if res is not None and res.status == "ok":
            emb.add_field(name="Wuju Coins", value=f"{self.money(res.wuju_before)} → {self.money(res.wuju_after)}", inline=True)
            emb.add_field(name="Embo", value=f"{fmt(res.embo_before)} → {fmt(res.embo_after)}", inline=True)
        elif res is not None and res.status == "funds":
            emb.add_field(name="Balance", value=self.money(res.wuju_before), inline=True)
        try:
            await channel.send(embed=emb, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def _disable_buttons(self, channel, ticket: etdb.Ticket):
        if not ticket.message_id or channel is None:
            return
        try:
            msg = await channel.fetch_message(ticket.message_id)
            await msg.edit(view=None)
        except Exception:
            pass

    async def _delete_later(self, channel):
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        await self._try_delete(channel)

    @staticmethod
    async def _try_delete(channel):
        try:
            await channel.delete()
        except Exception:
            pass


async def setup_embo_tickets(bot: commands.Bot, db_path: str, guild_id: int) -> EmboTicketsCog:
    cog = EmboTicketsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
