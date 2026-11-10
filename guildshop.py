"""Guild Shop — capa de Discord (paneles, tickets, botones y registro).

El dinero y el estado viven en guildshop_db.py (una transacción por acción). Acá se validan los permisos
en CADA pulsación (rol Seller o Council), se arman los mensajes y se mantiene el panel de Top Vendedores.
"""
import asyncio
import traceback
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import economy_db as eco
import games_db
import guildshop_db as gdb

SELLER_ROLE_ID = 1562621875597615225
COUNCIL_ROLE_ID = 1257896977220501557
NO_MENTIONS = discord.AllowedMentions.none()
PING_ROLE = discord.AllowedMentions(roles=True, users=True, everyone=False)
TOP_LIMIT = 25
CLOSE_DELAY_SECONDS = 5
SALE_CLOSE_DELAY_SECONDS = 120          # después de confirmar una venta, el canal del ticket se borra solo a los 2 minutos
GOLD = 0xF1C40F
SALE_SPIN_MIN = 3_000_000          # quien confirma una venta con valor total MAYOR a esto gana 1 tirada de Wujupón


def can_sell(member) -> bool:
    """Rol Seller o Council (se evalúa en cada pulsación)."""
    return any(getattr(r, "id", None) in (SELLER_ROLE_ID, COUNCIL_ROLE_ID) for r in getattr(member, "roles", []))


def can_confirm_sale(member) -> bool:
    """Confirmar una venta: solo el rol Seller (el Council no)."""
    return any(getattr(r, "id", None) == SELLER_ROLE_ID for r in getattr(member, "roles", []))


def can_manage(member) -> bool:
    """Armar paneles y configurar el registro: Council, propietario o Administrador."""
    if any(getattr(r, "id", None) == COUNCIL_ROLE_ID for r in getattr(member, "roles", [])):
        return True
    guild = getattr(member, "guild", None)
    if guild is not None and guild.owner_id == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def safe_channel_name(name: str, user_id: int) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (name or "").lower()).strip("-")[:40] or "cliente"
    return f"shop-{base}-{str(user_id)[-4:]}"


async def _reply(interaction: discord.Interaction, content: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


# ============================================================
# Vistas y modales (persistentes: sobreviven a reinicios)
# ============================================================
class ShopPanelView(discord.ui.View):
    def __init__(self, cog: "GuildShopCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Comprar", style=discord.ButtonStyle.success, custom_id="gshop:buy", emoji="🛒")
    async def buy(self, interaction: discord.Interaction, button):
        await self.cog.on_buy_pressed(interaction)


class TicketActionView(discord.ui.View):
    def __init__(self, cog: "GuildShopCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cerrar sin transacción", style=discord.ButtonStyle.danger, custom_id="gshop:close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button):
        await self.cog.on_close_pressed(interaction)

    @discord.ui.button(label="Vender por Wuju Coins", style=discord.ButtonStyle.success, custom_id="gshop:wuju", emoji="🪙")
    async def wuju(self, interaction: discord.Interaction, button):
        await self.cog.on_sell_pressed(interaction, "wuju")

    @discord.ui.button(label="Vender por silver", style=discord.ButtonStyle.primary, custom_id="gshop:silver", emoji="💰")
    async def silver(self, interaction: discord.Interaction, button):
        await self.cog.on_sell_pressed(interaction, "silver")


class DiscountTicketView(discord.ui.View):
    """Le pregunta al comprador si quiere usar un ticket de descuento de Wujupón (persistente: se busca por el canal)."""

    def __init__(self, cog: "GuildShopCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Usar ticket 15%", style=discord.ButtonStyle.success, custom_id="gshop:ticket_use15", emoji="🎟️")
    async def use15(self, interaction: discord.Interaction, button):
        await self.cog.on_discount_choice(interaction, "shop_discount_15")

    @discord.ui.button(label="Usar ticket 20%", style=discord.ButtonStyle.success, custom_id="gshop:ticket_use", emoji="🎟️")
    async def use(self, interaction: discord.Interaction, button):
        await self.cog.on_discount_choice(interaction, "shop_discount")

    @discord.ui.button(label="Usar ticket 30%", style=discord.ButtonStyle.success, custom_id="gshop:ticket_use30", emoji="🎟️")
    async def use30(self, interaction: discord.Interaction, button):
        await self.cog.on_discount_choice(interaction, "shop_discount_30")

    @discord.ui.button(label="No usar", style=discord.ButtonStyle.secondary, custom_id="gshop:ticket_skip")
    async def skip(self, interaction: discord.Interaction, button):
        await self.cog.on_discount_choice(interaction, None)

    def only(self, owned) -> "DiscountTicketView":
        """Deja solo los botones de los tickets que tiene el comprador (los callbacks siguen por custom_id)."""
        ids = {"shop_discount_15": "gshop:ticket_use15", "shop_discount": "gshop:ticket_use", "shop_discount_30": "gshop:ticket_use30"}
        keep = {"gshop:ticket_skip"} | {ids[k] for k in owned if k in ids}
        for item in list(self.children):
            if getattr(item, "custom_id", None) not in keep:
                self.remove_item(item)
        return self


class ConfirmSaleView(discord.ui.View):
    def __init__(self, cog: "GuildShopCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, custom_id="gshop:confirm", emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button):
        await self.cog.on_confirm_pressed(interaction)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, custom_id="gshop:cancel", emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel_pressed(interaction)


class SoldTicketView(discord.ui.View):
    """Ya no se usa en ventas nuevas (el canal se cierra solo al confirmar); queda registrado para que funcionen los botones de tickets viejos."""

    def __init__(self, cog: "GuildShopCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cerrar canal", style=discord.ButtonStyle.secondary, custom_id="gshop:delete", emoji="🗑️")
    async def delete(self, interaction: discord.Interaction, button):
        await self.cog.on_delete_pressed(interaction)


PAYMENT_LABEL = {"wuju": "🪙 Wuju Coins", "silver": "💰 Silver"}


class PaymentChoiceView(discord.ui.View):
    """Primer paso (privado): ¿con qué querés pagar? Después se pide la lista de compra."""

    def __init__(self, cog: "GuildShopCog", owner_id: int):
        super().__init__(timeout=180.0)
        self.cog, self.owner_id = cog, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Este mensaje no es tuyo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Wuju Coins", style=discord.ButtonStyle.success, emoji="🪙")
    async def wuju(self, interaction: discord.Interaction, button):
        await self.cog.on_payment_chosen(interaction, "wuju")

    @discord.ui.button(label="Silver", style=discord.ButtonStyle.primary, emoji="💰")
    async def silver(self, interaction: discord.Interaction, button):
        await self.cog.on_payment_chosen(interaction, "silver")


class BuyModal(discord.ui.Modal, title="Comprar en el shop de la guild"):
    items = discord.ui.TextInput(label="Lista de compra", style=discord.TextStyle.paragraph, required=True,
                                 max_length=gdb.MAX_ITEMS_LEN,
                                 placeholder="Todos los ítems que querés comprar, uno por línea (con cantidades)")

    def __init__(self, cog: "GuildShopCog", payment: str = "wuju"):
        super().__init__()
        self.cog, self.payment = cog, payment

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_buy_submitted(interaction, str(self.items.value), self.payment)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude abrir el ticket. Probá de nuevo en un momento.")


class SaleModal(discord.ui.Modal):
    total = discord.ui.TextInput(label="Valor total de la venta", required=True, max_length=25, placeholder="Ej: 1.500.000")
    discount = discord.ui.TextInput(label="Descuento en % (por defecto 10)", required=False, max_length=6,
                                    default="10", placeholder="10")

    def __init__(self, cog: "GuildShopCog", ticket_id: int, currency: str):
        super().__init__(title="Vender por Wuju Coins" if currency == "wuju" else "Vender por silver")
        self.cog, self.ticket_id, self.currency = cog, ticket_id, currency

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_sale_submitted(interaction, self.ticket_id, self.currency, str(self.total.value), str(self.discount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No pude preparar la venta. No se cobró nada.")


# ============================================================
# Cog
# ============================================================
class GuildShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.close_delay = CLOSE_DELAY_SECONDS
        self.sale_close_delay = SALE_CLOSE_DELAY_SECONDS
        self._top_lock = asyncio.Lock()
        self._tasks: set = set()

    # ---------- infraestructura ----------
    async def cog_load(self):
        await self.run(gdb.init_schema)
        self.bot.add_view(ShopPanelView(self))
        self.bot.add_view(TicketActionView(self))
        self.bot.add_view(ConfirmSaleView(self))
        self.bot.add_view(SoldTicketView(self))
        self.bot.add_view(DiscountTicketView(self))
        self._spawn(self._after_ready())

    async def cog_unload(self):
        for t in list(self._tasks):
            t.cancel()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _after_ready(self):
        """Al iniciar: actualiza el texto del panel del shop y borra los canales de ventas que quedaron sin borrar."""
        try:
            wait = getattr(self.bot, "wait_until_ready", None)
            if wait is not None:
                await wait()
            await self.refresh_shop_panel()
            await self.sweep_sold_channels()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    async def refresh_shop_panel(self):
        """Edita el panel del shop ya publicado con el texto actual (no hace falta volver a publicarlo). Nunca lanza."""
        try:
            found = await self.run(gdb.get_panel, self.guild_id, "shop")
            if not found:
                return
            ch = self.bot.get_channel(found[0]) or await self.bot.fetch_channel(found[0])
            msg = await ch.fetch_message(found[1])
            await msg.edit(embed=self.shop_embed(), view=ShopPanelView(self))
        except Exception as exc:
            print(f"⚠️ Guild Shop: no pude actualizar el panel del shop: {exc!r}")

    async def sweep_sold_channels(self):
        """Canales de tickets vendidos que siguen existiendo (por ejemplo, si el bot se reinició durante los 2 minutos)."""
        from datetime import datetime, timezone
        rows = await self.run(gdb.recent_sold_channels, self.guild_id)
        now = datetime.now(timezone.utc)
        for channel_id, closed_at in rows:
            ch = self.bot.get_channel(channel_id)
            if ch is None:
                continue
            try:
                done = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                done = done if done.tzinfo else done.replace(tzinfo=timezone.utc)
                left = max(0.0, self.sale_close_delay - (now - done).total_seconds())
            except ValueError:
                left = 0.0
            self._spawn(self._delete_after(ch, left))

    async def _delete_after(self, channel, delay: float):
        try:
            if delay:
                await asyncio.sleep(delay)
            await self._try_delete(channel)
        except asyncio.CancelledError:
            pass

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def wuju(self, amount: int) -> str:
        eco_cog = self.economy
        return eco_cog.money(amount) if eco_cog is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    @staticmethod
    def silver(amount: int) -> str:
        return f"{eco.format_amount(amount)} silver"

    def money_of(self, currency: str, amount: int) -> str:
        return self.wuju(amount) if currency == "wuju" else self.silver(amount)

    # ---------- registro de transacciones ----------
    async def _log_channel(self, guild: Optional[discord.Guild]):
        cid = await self.run(gdb.get_log_channel_id)
        if not cid:
            return None
        ch = guild.get_channel(cid) if guild is not None else None
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except Exception:
                return None
        return ch

    async def log(self, guild: Optional[discord.Guild], embed: discord.Embed):
        """Mejor esfuerzo: si el registro falla, la operación ya confirmada no se deshace."""
        try:
            ch = await self._log_channel(guild)
            if ch is not None:
                await ch.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ Guild Shop: no pude escribir en el canal de registro: {exc!r}")

    # ---------- paneles ----------
    def top_embed(self, rows: List[gdb.SellerRow]) -> discord.Embed:
        emb = discord.Embed(title="🏆 Top Vendedores — Guild Shop", color=GOLD)
        if not rows:
            emb.description = "_Todavía no hay ventas registradas._"
        else:
            lines = []
            for i, r in enumerate(rows, 1):
                detail = []
                if r.wuju:
                    detail.append(self.wuju(r.wuju))
                if r.silver:
                    detail.append(self.silver(r.silver))
                lines.append(f"`{i:>2}.` <@{r.seller_id}> — **{eco.format_amount(r.total)}** · {' + '.join(detail)} · "
                             f"{r.sales} venta{'s' if r.sales != 1 else ''}")
            emb.description = "\n".join(lines)[:4000]
        emb.set_footer(text="Cantidad acumulada vendida (Wuju Coins + silver, ya con descuento aplicado)")
        return emb

    @staticmethod
    def shop_embed() -> discord.Embed:
        emb = discord.Embed(title="🛒 Shop de la guild", color=0x2ECC71)
        emb.description = (
            "Comprá ítems directamente a la guild.\n\n"
            "**🏷️ Todos los ítems tienen un 10% de descuento.**\n\n"
            "**🎟️ Tickets de descuento**\n"
            + "\n".join(f"• **{gdb.format_pct(bp)}** de descuento hasta {gdb.eco.format_amount(cap)} (lo que pase, al 10%)."
                        for bp, cap in gdb.TICKETS.values())
            + "\nSe consiguen en **Wujupón**. Si tenés uno, al abrir el ticket el bot te pregunta si querés usarlo.\n\n"
            "**Cómo funciona**\n"
            "1. Tocá **Comprar**, elegí si pagás con **Wuju Coins** o **Silver** y escribí la lista de todo lo que querés (con cantidades).\n"
            "2. Se abre un ticket privado y se avisa a los **Seller**.\n"
            "3. Un Seller te confirma el precio final y se cobra en **Wuju Coins** o en **silver**.")
        return emb

    async def refresh_top_panel(self, guild: Optional[discord.Guild] = None):
        """Actualiza el panel de Top Vendedores (si existe). Nunca lanza."""
        try:
            async with self._top_lock:
                found = await self.run(gdb.get_panel, self.guild_id, "top")
                if not found:
                    return
                channel_id, message_id = found
                rows = await self.run(gdb.top_sellers, self.guild_id, TOP_LIMIT)
                ch = guild.get_channel(channel_id) if guild is not None else None
                if ch is None:
                    ch = await self.bot.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                await msg.edit(embed=self.top_embed(rows))
        except Exception as exc:
            print(f"⚠️ Guild Shop: no pude actualizar el Top Vendedores: {exc!r}")

    # ---------- comandos ----------
    async def guild_shop(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo publicar en ese canal.")
            await interaction.response.defer(ephemeral=True)
            rows = await self.run(gdb.top_sellers, interaction.guild.id, TOP_LIMIT)
            try:
                top_msg = await channel.send(embed=self.top_embed(rows), allowed_mentions=NO_MENTIONS)
                try:
                    shop_msg = await channel.send(embed=self.shop_embed(), view=ShopPanelView(self), allowed_mentions=NO_MENTIONS)
                except Exception:
                    await top_msg.delete()                       # no dejar un panel huérfano
                    raise
            except (discord.Forbidden, discord.HTTPException) as exc:
                return await interaction.followup.send(f"❌ No pude publicar los paneles en {channel.mention}: {exc}", ephemeral=True)
            # recién ahora se reemplazan los paneles viejos (si había)
            for kind in ("top", "shop"):
                old = await self.run(gdb.get_panel, interaction.guild.id, kind)
                if old:
                    try:
                        och = interaction.guild.get_channel(old[0]) or await self.bot.fetch_channel(old[0])
                        await (await och.fetch_message(old[1])).delete()
                    except Exception:
                        pass
            await self.run(gdb.set_panel, interaction.guild.id, "top", channel.id, top_msg.id)
            await self.run(gdb.set_panel, interaction.guild.id, "shop", channel.id, shop_msg.id)
            await interaction.followup.send(f"✅ Paneles publicados en {channel.mention}.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude completar el comando.")

    canal = app_commands.Group(name="canal", description="Configuración de canales", guild_only=True)
    canal_logs = app_commands.Group(name="logs", description="Canales de registro", parent=canal)

    # Estos canales se configuran con /canal ...; la lógica y la validación de permisos siguen en cada módulo.
    async def _canal_delegate(self, interaction: discord.Interaction, cog_name: str, method: str, label: str, *args):
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return await _reply(interaction, f"❌ El módulo de {label} no está cargado.")
        await getattr(cog, method)(interaction, *args)

    @canal.command(name="entradas", description="(Council/Admin) Canal donde se menciona a quien entra al servidor")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar este registro")
    async def canal_entradas_cmd(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self._canal_delegate(interaction, "MemberLogCog", "canal_entradas", "entradas y salidas", canal, desactivar)

    @canal.command(name="salidas", description="(Council/Admin) Canal donde se registra quién se fue, sus roles y su cuenta de Albion")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar este registro")
    async def canal_salidas_cmd(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self._canal_delegate(interaction, "MemberLogCog", "canal_salidas", "entradas y salidas", canal, desactivar)

    @canal.command(name="registro", description="(Council/Admin) Canal donde el bot avisa los registros y desvinculaciones")
    @app_commands.describe(canal="Canal de texto")
    async def canal_registro_cmd(self, interaction: discord.Interaction, canal: discord.TextChannel):
        await self._canal_delegate(interaction, "RegistrationCog", "registro_canal", "registro", canal)

    @canal.command(name="economia", description="(Admin) Canal donde se publican los movimientos de Wuju Coins")
    @app_commands.describe(canal="Canal de auditoría económica")
    async def canal_economia_cmd(self, interaction: discord.Interaction, canal: discord.TextChannel):
        await self._canal_delegate(interaction, "EconomyCog", "registro_bal", "economía", canal)

    @canal.command(name="wujupon-regalos", description="(Council) Canal donde se registra cada /wujupon regalar-tiradas")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar este registro")
    async def canal_wujupon_regalos_cmd(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self._canal_delegate(interaction, "WujuponCog", "canal_regalos", "Wujupón", canal, desactivar)

    @canal.command(name="log-foco", description="(Council) Canal donde se registran las donaciones de foco aprobadas")
    @app_commands.describe(canal="Canal de registro (por defecto, este canal)")
    async def canal_log_foco(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo usar ese canal.")
            try:
                await channel.send("✅ Canal de registro de **Foco Donor** configurado: acá se anotará cada donación aprobada.",
                                   allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException):
                return await _reply(interaction, f"❌ No tengo permiso para escribir en {channel.mention}. No cambié nada.")
            import botsettings as bs
            await asyncio.to_thread(bs.set_value, "foco_log", channel.id, interaction.user.id)
            await _reply(interaction, f"✅ Log de Foco Donor configurado en {channel.mention}.")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude guardar el canal.")

    # /canal dados vive acá porque el grupo "/canal" es único en el bot (Discord no permite repetirlo en otro módulo)
    @canal.command(name="dados", description="(Council) Canal donde se publican los paneles de !dados")
    @app_commands.describe(canal="Canal donde aparecerán los paneles de dados")
    async def canal_dados(self, interaction: discord.Interaction, canal: discord.TextChannel):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            try:
                await canal.send("✅ Canal de **dados** configurado: los paneles de `!dados` se publicarán acá.", allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException):
                return await _reply(interaction, f"❌ No tengo permiso para escribir en {canal.mention}. No cambié nada.")
            await self.run(games_db.set_panel_channel_id, "dice", canal.id)
            await _reply(interaction, f"✅ Los paneles de `!dados` ahora se publican en {canal.mention}.")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude guardar el canal.")

    @canal_logs.command(name="tareas", description="(Council) Canal donde se registran las tareas creadas con /tarea crear")
    @app_commands.describe(canal="Canal de registro (por defecto, este canal)")
    async def logs_tareas(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        await self._canal_delegate(interaction, "EncargosCog", "canal_log_tareas", "tareas", canal)

    @canal_logs.command(name="guildshop", description="(Council) Canal donde se registra cada transacción del Guild Shop")
    @app_commands.describe(canal="Canal de registro (por defecto, este canal)")
    async def logs_guildshop(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _reply(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
            channel = canal or interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await _reply(interaction, "❌ No puedo usar ese canal.")
            try:
                await channel.send("✅ Canal de registro del **Guild Shop** configurado: acá se anotará cada transacción.",
                                   allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException):
                return await _reply(interaction, f"❌ No tengo permiso para escribir en {channel.mention}. No cambié nada.")
            await self.run(gdb.set_log_channel_id, channel.id)
            await _reply(interaction, f"✅ Registro del Guild Shop configurado en {channel.mention}.")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude guardar el canal.")

    # ---------- comprar: botón → modal → ticket ----------
    async def on_buy_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        existing = await self.run(gdb.open_ticket_of, interaction.guild.id, interaction.user.id)
        if existing is not None:
            return await _reply(interaction, f"ℹ️ Ya tenés un ticket abierto: <#{existing.channel_id}>. Cerralo o esperá a que lo atiendan antes de abrir otro.")
        await interaction.response.send_message(
            "¿Cómo querés pagar? Elegí **Wuju Coins** o **Silver**. Después te pido la lista de compra.",
            view=PaymentChoiceView(self, interaction.user.id), ephemeral=True)

    async def on_payment_chosen(self, interaction: discord.Interaction, payment: str):
        await interaction.response.send_modal(BuyModal(self, payment))

    async def on_buy_submitted(self, interaction: discord.Interaction, items: str, payment: str = "wuju"):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "❌ Solo disponible en el servidor.")
        items = items.strip()
        if not items:
            return await _reply(interaction, "❌ La lista de compra no puede estar vacía.")
        await interaction.response.defer(ephemeral=True)
        existing = await self.run(gdb.open_ticket_of, guild.id, interaction.user.id)
        if existing is not None:
            return await interaction.followup.send(f"ℹ️ Ya tenés un ticket abierto: <#{existing.channel_id}>.", ephemeral=True)

        seller_role = guild.get_role(SELLER_ROLE_ID)
        council_role = guild.get_role(COUNCIL_ROLE_ID)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for role in (seller_role, council_role):
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        category = getattr(interaction.channel, "category", None)
        try:
            channel = await guild.create_text_channel(name=safe_channel_name(interaction.user.name, interaction.user.id),
                                                      category=category, overwrites=overwrites,
                                                      topic=f"Ticket de compra · comprador {interaction.user.id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.followup.send(f"❌ No pude crear el ticket (revisá mis permisos de **Gestionar canales**): {exc}", ephemeral=True)
        try:
            ticket_id = await self.run(gdb.create_ticket, guild.id, channel.id, interaction.user.id, items, payment)
        except Exception:
            traceback.print_exc()
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude registrar el ticket. Probá de nuevo.", ephemeral=True)
        emb = discord.Embed(title=f"🛒 Ticket de compra #{ticket_id}", color=0x2ECC71)
        emb.add_field(name="Comprador", value=interaction.user.mention, inline=False)
        emb.add_field(name="Lista de compra", value=items[:1000], inline=False)
        emb.add_field(name="Quiere pagar con", value=PAYMENT_LABEL.get(payment, payment), inline=True)
        emb.add_field(name="Descuento", value="Los ítems tienen **10% de descuento**.", inline=False)
        emb.set_footer(text="Solo los Seller pueden usar los botones.")
        ping = f"<@&{SELLER_ROLE_ID}> {interaction.user.mention}"
        try:
            await channel.send(ping, embed=emb, view=TicketActionView(self), allowed_mentions=PING_ROLE)
        except Exception:
            traceback.print_exc()
            await self.run(gdb.discard_ticket, ticket_id)
            await self._try_delete(channel)
            return await interaction.followup.send("❌ No pude abrir el ticket. Probá de nuevo.", ephemeral=True)
        await interaction.followup.send(f"✅ Ticket abierto: {channel.mention}", ephemeral=True)
        await self._offer_discount_ticket(channel, interaction.user)

    # ---------- ticket de descuento (carta de Wujupón) ----------
    async def _offer_discount_ticket(self, channel, buyer):
        try:
            owned = await self.run(gdb.discount_cards_available, buyer.id)
            if not owned:
                return
            lines = "\n".join(f"• **{gdb.ticket_label(k)}** — tenés {n}" for k, n in sorted(owned.items()))
            await channel.send(f"🎟️ {buyer.mention}, tenés tickets de descuento de Wujupón. ¿Querés usar uno en esta compra?\n{lines}\n"
                               f"Reemplaza el **{gdb.format_pct(gdb.DEFAULT_DISCOUNT_BP)}** normal hasta ese monto; lo que pase sigue al "
                               f"{gdb.format_pct(gdb.DEFAULT_DISCOUNT_BP)}. Si el ticket se cierra sin venta, la carta vuelve a tu inventario.",
                               view=DiscountTicketView(self).only(owned),
                               allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception:
            traceback.print_exc()

    async def on_discount_choice(self, interaction: discord.Interaction, prize_key: Optional[str]):
        ticket = await self.run(gdb.get_ticket_by_channel, interaction.channel.id)
        if ticket is None or ticket.status != "open":
            return await _reply(interaction, "ℹ️ Este ticket ya fue cerrado o vendido.")
        if interaction.user.id != ticket.buyer_id:
            return await _reply(interaction, "❌ Solo el comprador puede elegir si usa su ticket de descuento.")
        if not prize_key:
            return await self._finish_msg(interaction, "Sin ticket de descuento: la compra va con el descuento normal.", view=None)
        status = await self.run(gdb.attach_discount_card, ticket.id, interaction.user.id, prize_key)
        texts = {
            "ok": f"🎟️ {interaction.user.mention} usa un **ticket de descuento de {gdb.ticket_label(prize_key)}**. "
                  "El Seller no tiene que cargar el descuento: lo calcula el bot.",
            "already": "ℹ️ Este ticket ya tiene un ticket de descuento aplicado.",
            "none": "❌ Ya no tenés ese ticket de descuento.",
            "pending": "❌ Ya hay una venta esperando confirmación. Que el Seller la cancele y volvé a tocar el botón.",
            "closed": "ℹ️ Este ticket ya fue cerrado o vendido.",
        }
        if status == "pending":
            return await _reply(interaction, texts[status])
        await self._finish_msg(interaction, texts[status], view=None)

    @staticmethod
    async def _try_delete(channel):
        try:
            await channel.delete()
        except Exception:
            pass

    # ---------- botones del ticket ----------
    async def _seller_ticket(self, interaction: discord.Interaction, need_open: bool = True) -> Optional[gdb.Ticket]:
        if interaction.guild is None or not can_sell(interaction.user):
            await _reply(interaction, "❌ Solo los **Seller** (o Council) pueden usar estos botones.")
            return None
        ticket = await self.run(gdb.get_ticket_by_channel, interaction.channel.id)
        if ticket is None:
            await _reply(interaction, "❌ Este canal no es un ticket del shop.")
            return None
        if need_open and ticket.status != "open":
            await _reply(interaction, "ℹ️ Este ticket ya fue cerrado o vendido.")
            return None
        return ticket

    async def on_close_pressed(self, interaction: discord.Interaction):
        ticket = await self._seller_ticket(interaction)
        if ticket is None:
            return
        if not await self.run(gdb.close_ticket, ticket.id, interaction.user.id):
            return await _reply(interaction, "ℹ️ Este ticket ya fue cerrado o vendido.")
        await self._record_task(interaction.user.id, ticket.buyer_id, f"Cerrar el ticket del Guild Shop #{ticket.id}", ticket.id)
        await interaction.response.send_message(f"🔒 Ticket cerrado sin transacción por {interaction.user.mention}. El canal se borra en unos segundos.",
                                                allowed_mentions=NO_MENTIONS)
        emb = discord.Embed(title="🔒 Ticket cerrado sin transacción", color=0x95A5A6)
        emb.add_field(name="Ticket", value=f"#{ticket.id}", inline=True)
        emb.add_field(name="Comprador", value=f"<@{ticket.buyer_id}>", inline=True)
        emb.add_field(name="Cerrado por", value=interaction.user.mention, inline=True)
        emb.add_field(name="Lista de compra", value=ticket.items[:1000], inline=False)
        await self.log(interaction.guild, emb)
        await self._delete_later(interaction.channel)

    async def _record_task(self, actor_id: int, buyer_id: int, text: str, ticket_id: int):
        """Cerrar el ticket de otra persona cuenta como tarea finalizada para /top tareas (una vez por ticket)."""
        enc = self.bot.get_cog("EncargosCog")
        if enc is not None and actor_id != buyer_id:
            await enc.record_done(text, actor_id, f"gshop:{ticket_id}")

    async def _sale_spin(self, actor_id: int, t, p) -> str:
        """1 tirada de Wujupón a quien confirma una venta con valor total > SALE_SPIN_MIN (una vez por ticket; no al propio comprador).
        Devuelve el texto del aviso ('' si no corresponde o falló). Nunca rompe la venta."""
        if p.gross <= SALE_SPIN_MIN or actor_id == t.buyer_id:
            return ""
        wcog = self.bot.get_cog("WujuponCog")
        if wcog is None:
            return ""
        try:
            import wujupon_db as wdb
            after = await wcog.run(wdb.gift_spins, actor_id=self.bot.user.id if self.bot.user else 0, user_id=actor_id, count=1,
                                   request_key=f"gshop-sale:{t.id}", reason=f"Venta Guild Shop #{t.id} (> {eco.format_amount(SALE_SPIN_MIN)})")
        except Exception:
            traceback.print_exc()
            return ""
        text = f"🎰 <@{actor_id}> ganó **1** tirada de Wujupón por cerrar una venta de más de {eco.format_amount(SALE_SPIN_MIN)} (ahora tiene {after})."
        try:
            log = await wcog._channel("gift_log_channel")
            if log is not None:
                await log.send(f"{text}\n🛒 Ticket del Guild Shop #{t.id}", allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        return text

    async def _delete_later(self, channel):
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        await self._try_delete(channel)

    async def on_sell_pressed(self, interaction: discord.Interaction, currency: str):
        ticket = await self._seller_ticket(interaction)
        if ticket is None:
            return
        if ticket.buyer_id == interaction.user.id:
            return await _reply(interaction, "❌ No podés venderte a vos mismo. Que lo atienda otro Seller.")
        await interaction.response.send_modal(SaleModal(self, ticket.id, currency))

    async def on_sale_submitted(self, interaction: discord.Interaction, ticket_id: int, currency: str, total_text: str, discount_text: str):
        # los permisos se revisan otra vez: el modal se pudo abrir antes de que le quitaran el rol
        if interaction.guild is None or not can_sell(interaction.user):
            return await _reply(interaction, "❌ Solo los **Seller** (o Council) pueden hacer esto.")
        ticket = await self.run(gdb.get_ticket, ticket_id)
        if ticket is None or ticket.status != "open":
            return await _reply(interaction, "ℹ️ Este ticket ya fue cerrado o vendido.")
        if ticket.buyer_id == interaction.user.id:
            return await _reply(interaction, "❌ No podés venderte a vos mismo.")
        try:
            gross, bp, net = gdb.build_offer(total_text, discount_text)
        except gdb.ShopError as exc:
            return await _reply(interaction, f"❌ {exc}")
        card_prize = await self.run(gdb.ticket_card_prize, ticket.id) if ticket.wjp_card_id else None
        uses_card = card_prize is not None
        if uses_card:                            # ticket de descuento: el bot calcula el descuento (su % hasta su tope, 10 % el resto)
            bp, net = gdb.offer_with_ticket(gross, card_prize)
        if currency == "wuju":
            if await self.run(eco.is_frozen, ticket.buyer_id):
                return await _reply(interaction, f"🧊 El balance de <@{ticket.buyer_id}> está congelado (ticket de Cobrar balance abierto o carta de Wujupón). No se hizo ningún cambio.")
            balance = await self.run(eco.get_balance, ticket.buyer_id)
            if balance < net:
                return await _reply(interaction,
                                    f"❌ A <@{ticket.buyer_id}> no le alcanza: necesita {self.wuju(net)} y tiene {self.wuju(balance)}. No se hizo ningún cambio.")
        label = "Wuju Coins" if currency == "wuju" else "silver"
        emb = discord.Embed(title=f"❓ Confirmar venta por {label}", color=GOLD)
        emb.add_field(name="Comprador", value=f"<@{ticket.buyer_id}>", inline=True)
        emb.add_field(name="Vendedor", value=interaction.user.mention, inline=True)
        emb.add_field(name="Valor total", value=self.money_of(currency, gross), inline=True)
        emb.add_field(name="Descuento", value=gdb.format_pct(bp), inline=True)
        emb.add_field(name="A cobrar", value=f"**{self.money_of(currency, net)}**", inline=True)
        if uses_card:
            emb.add_field(name="🎟️ Ticket de descuento", value=f"{gdb.ticket_label(card_prize)} (el resto al "
                                                              f"{gdb.format_pct(gdb.DEFAULT_DISCOUNT_BP)}). Se usa al confirmar.", inline=False)
        if currency == "wuju":
            emb.add_field(name="Se descuenta del balance del comprador", value="Al confirmar se le restan estos Wuju Coins.", inline=False)
        emb.set_footer(text="Solo un Seller puede confirmar (cancelar: Seller o Council).")
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await interaction.channel.send(f"<@&{SELLER_ROLE_ID}> ¿{interaction.user.mention} vende por **{label}** a **{self.money_of(currency, net)}**? Confirmá o cancelá.",
                                                 embed=emb, view=ConfirmSaleView(self), allowed_mentions=PING_ROLE)
        except Exception:
            traceback.print_exc()
            return await interaction.followup.send("❌ No pude publicar la confirmación. No se cobró nada.", ephemeral=True)
        try:
            await self.run(gdb.save_pending, msg.id, ticket.id, interaction.user.id, currency, gross, bp, net, uses_card)
        except Exception:
            traceback.print_exc()
            await self._try_delete(msg)
            return await interaction.followup.send("❌ No pude guardar la confirmación. No se cobró nada.", ephemeral=True)
        await interaction.followup.send("✅ Pedí la confirmación en el canal del ticket.", ephemeral=True)

    # ---------- confirmar / cancelar ----------
    async def on_confirm_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None or not can_confirm_sale(interaction.user):
            return await _reply(interaction, "❌ Solo los **Seller** pueden confirmar las ventas.")
        msg = interaction.message
        result = await self.run(gdb.confirm_sale, msg.id, interaction.user.id)
        if result.status == "gone":
            return await _reply(interaction, "ℹ️ Esta confirmación ya fue procesada o cancelada.")
        if result.status == "closed":
            await self._finish_msg(interaction, "ℹ️ El ticket ya no estaba abierto: no se cobró nada.")
            return
        if result.status == "self":
            await self._finish_msg(interaction, "❌ El comprador y el vendedor son la misma persona: no se hizo la venta.")
            return
        if result.status == "card":
            await self._finish_msg(interaction, "🎟️ El ticket de descuento ya no está disponible (se devolvió o se anuló). No se cobró nada: "
                                                "volvé a cargar la venta con el descuento normal.")
            return
        if result.status == "frozen":
            await self._finish_msg(interaction, "🧊 El balance del comprador está congelado (ticket de Cobrar balance abierto o carta de Wujupón). No se cobró nada.")
            return
        if result.status == "funds":
            p = result.pending
            await self._finish_msg(interaction,
                                   f"❌ El comprador ya no tiene saldo suficiente (tiene {self.wuju(result.balance_before)}, hacen falta {self.wuju(p.net)}). No se cobró nada.")
            return
        p, t = result.pending, result.ticket
        await self._record_task(interaction.user.id, t.buyer_id, f"Cerrar el ticket del Guild Shop #{t.id} (venta)", t.id)
        spin_text = await self._sale_spin(interaction.user.id, t, p)
        await self._finish_msg(interaction, f"✅ Venta confirmada por {interaction.user.mention}.", view=None)
        done = discord.Embed(title=f"✅ Venta realizada — ticket #{t.id}", color=0x2ECC71)
        done.add_field(name="Comprador", value=f"<@{t.buyer_id}>", inline=True)
        done.add_field(name="Vendedor", value=f"<@{p.seller_id}>", inline=True)
        done.add_field(name="Cobrado", value=self.money_of(p.currency, p.net), inline=True)
        done.add_field(name="Valor total", value=self.money_of(p.currency, p.gross), inline=True)
        done.add_field(name="Descuento", value=gdb.format_pct(p.discount_bp) + (" · 🎟️ con ticket" if p.uses_card else ""), inline=True)
        if p.currency == "wuju":
            done.add_field(name="Balance del comprador", value=f"{self.wuju(result.balance_before)} → {self.wuju(result.balance_after)}", inline=False)
        done.set_footer(text=f"Venta confirmada: este canal se borra solo en {max(1, round(self.sale_close_delay / 60))} minutos."
                         if self.sale_close_delay >= 60 else "Venta confirmada: este canal se borra solo en unos segundos.")
        try:
            await interaction.channel.send(embed=done, allowed_mentions=NO_MENTIONS)
            if spin_text:
                await interaction.channel.send(spin_text, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        await self._disable_ticket_buttons(interaction.channel)
        log = discord.Embed(title="💰 Venta Guild Shop — " + ("Wuju Coins" if p.currency == "wuju" else "silver"), color=0x2ECC71)
        log.add_field(name="Ticket", value=f"#{t.id}", inline=True)
        log.add_field(name="Comprador", value=f"<@{t.buyer_id}>", inline=True)
        log.add_field(name="Vendedor", value=f"<@{p.seller_id}>", inline=True)
        log.add_field(name="Confirmó", value=interaction.user.mention, inline=True)
        log.add_field(name="Valor total", value=self.money_of(p.currency, p.gross), inline=True)
        log.add_field(name="Descuento", value=gdb.format_pct(p.discount_bp) + (" · 🎟️ ticket de descuento" if p.uses_card else ""), inline=True)
        log.add_field(name="Cobrado", value=self.money_of(p.currency, p.net), inline=True)
        if p.currency == "wuju":
            log.add_field(name="Balance comprador", value=f"{self.wuju(result.balance_before)} → {self.wuju(result.balance_after)}", inline=True)
        if spin_text:
            log.add_field(name="🎰 Wujupón", value=spin_text[:1000], inline=False)
        log.add_field(name="Lista de compra", value=t.items[:1000], inline=False)
        await self.log(interaction.guild, log)
        await self.refresh_top_panel(interaction.guild)
        self._spawn(self._delete_after(interaction.channel, self.sale_close_delay))   # se borra solo, sin esperar ningún botón

    async def on_cancel_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None or not can_sell(interaction.user):
            return await _reply(interaction, "❌ Solo los **Seller** (o Council) pueden cancelar.")
        p = await self.run(gdb.cancel_pending, interaction.message.id)
        if p is None:
            return await _reply(interaction, "ℹ️ Esta confirmación ya fue procesada o cancelada.")
        await self._finish_msg(interaction, f"✖️ Venta cancelada por {interaction.user.mention}. El ticket sigue abierto.", view=None)

    @staticmethod
    async def _finish_msg(interaction: discord.Interaction, content: str, view=None):
        """Reemplaza la confirmación por un texto final y le saca los botones."""
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(content=content, embeds=[], view=view, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.message.edit(content=content, embeds=[], view=view, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def _disable_ticket_buttons(self, channel):
        """Apaga los 3 botones del mensaje original del ticket (best effort)."""
        try:
            async for m in channel.history(limit=15, oldest_first=True):
                if m.author.id == getattr(self.bot.user, "id", None) and m.embeds and "Ticket de compra" in (m.embeds[0].title or ""):
                    await m.edit(view=None)
                    return
        except Exception as exc:
            print(f"ℹ️ Guild Shop: no pude apagar los botones del ticket: {exc!r}")

    async def on_delete_pressed(self, interaction: discord.Interaction):
        ticket = await self._seller_ticket(interaction, need_open=False)
        if ticket is None:
            return
        await interaction.response.send_message("🗑️ Cerrando el canal…", allowed_mentions=NO_MENTIONS)
        await self._delete_later(interaction.channel)


async def setup_guildshop(bot: commands.Bot, db_path: str, guild_id: int) -> GuildShopCog:
    cog = GuildShopCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
