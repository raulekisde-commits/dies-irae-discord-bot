"""Rifas: /rifas premio precio tiempo (hh:mm) → panel con botones para comprar tickets, sorteo automático al terminar."""
import asyncio
import time
import traceback
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco
import raffles_db as rdb
from guildshop import can_manage
from memberguard import button_block_reason

NO_MENTIONS = discord.AllowedMentions.none()
GOLD = discord.Color.gold()
GREY = discord.Color.light_grey()
MAX_LISTED = 25


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


class RaffleView(discord.ui.View):
    """Botones persistentes del panel (funcionan aunque el bot se reinicie: la rifa se busca por el id del mensaje)."""
    def __init__(self, cog: "RafflesCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Comprar 1 ticket", style=discord.ButtonStyle.success, custom_id="raffle:buy1", emoji="🎟️")
    async def buy_one(self, interaction: discord.Interaction, button):
        await self.cog.on_buy(interaction, 1)

    @discord.ui.button(label="Comprar varios", style=discord.ButtonStyle.primary, custom_id="raffle:buyn", emoji="🎫")
    async def buy_many(self, interaction: discord.Interaction, button):
        await self.cog.open_quantity_modal(interaction)


class QuantityModal(discord.ui.Modal, title="Comprar tickets"):
    qty = discord.ui.TextInput(label="¿Cuántos tickets querés?", placeholder="Ej: 5", required=True, max_length=4)

    def __init__(self, cog: "RafflesCog", message_id: Optional[int]):
        super().__init__(timeout=None)
        self.cog, self.message_id = cog, message_id

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.qty).strip()
        if not text.isdigit() or not (1 <= int(text) <= rdb.MAX_TICKETS_PER_USER):
            return await _say(interaction, f"❌ Poné un número entero entre 1 y {rdb.MAX_TICKETS_PER_USER}.")
        await self.cog.on_buy(interaction, int(text), message_id=self.message_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exc()
        await _say(interaction, "❌ No pude procesar la compra. Revisá tu saldo con `!bal` antes de reintentar.")


class RafflesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self.run(rdb.init_schema)
        self.bot.add_view(RaffleView(self))
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    def money(self, amount: int) -> str:
        e = self.bot.get_cog("EconomyCog")
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    # ---------- panel ----------
    def panel_embed(self, r: rdb.Raffle) -> discord.Embed:
        e = discord.Embed(title=f"🎟️ Rifa #{r.id}: {r.prize}", color=GOLD if r.status == "open" else GREY)
        e.add_field(name="💰 Precio por ticket", value=self.money(r.price), inline=True)
        e.add_field(name="🎫 Tickets vendidos", value=f"{r.total_tickets:,}", inline=True)
        e.add_field(name="👥 Participantes", value=f"{len(r.tickets):,}", inline=True)
        if r.status == "open":
            e.add_field(name="⏰ Termina", value=f"<t:{int(r.ends_at)}:R> (<t:{int(r.ends_at)}:f>)", inline=False)
        elif r.status == "cancelled":
            e.add_field(name="Estado", value="🚫 Cancelada: se devolvió todo lo cobrado.", inline=False)
        else:
            e.add_field(name="🏆 Ganador", value=f"<@{r.winner_id}>" if r.winner_id else "Nadie compró tickets.", inline=False)
        if r.tickets:
            lines = [f"<@{uid}> — **{qty}** ticket{'s' if qty != 1 else ''}" for uid, qty in list(r.tickets.items())[:MAX_LISTED]]
            if len(r.tickets) > MAX_LISTED:
                lines.append(f"… y {len(r.tickets) - MAX_LISTED} más")
            e.add_field(name="📋 Quién compró", value="\n".join(lines)[:1024], inline=False)
        else:
            e.add_field(name="📋 Quién compró", value="Todavía nadie. ¡Sé el primero!", inline=False)
        e.set_footer(text=f"Máximo {rdb.MAX_TICKETS_PER_USER} tickets por persona · cada ticket es una chance")
        return e

    async def _get_channel(self, channel_id):
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def refresh_panel(self, r: rdb.Raffle, *, closed: bool = False):
        lock = self._locks.setdefault(r.id, asyncio.Lock())
        async with lock:
            try:
                fresh = await self.run(rdb.get_raffle, r.id) or r        # siempre el estado más nuevo, aunque varios toquen a la vez
                ch = await self._get_channel(fresh.channel_id)
                if ch is None or not fresh.message_id:
                    return
                msg = await ch.fetch_message(fresh.message_id)
                await msg.edit(embed=self.panel_embed(fresh), view=None if (closed or fresh.status != "open") else RaffleView(self),
                               allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"ℹ️ Rifas: no pude actualizar el panel #{r.id}: {exc!r}")

    # ---------- compra ----------
    async def open_quantity_modal(self, interaction: discord.Interaction):
        msg = getattr(interaction, "message", None)
        await interaction.response.send_modal(QuantityModal(self, getattr(msg, "id", None)))

    async def on_buy(self, interaction: discord.Interaction, qty: int, message_id: Optional[int] = None):
        try:
            if interaction.guild is None or getattr(interaction.user, "bot", False):
                return
            denied = await button_block_reason(self.bot, interaction.user)
            if denied:
                return await _say(interaction, denied)
            if message_id is None:
                message_id = getattr(getattr(interaction, "message", None), "id", None)
            r = await self.run(rdb.find_by_message, message_id) if message_id else None
            if r is None:
                return await _say(interaction, "❌ No encuentro esa rifa.")
            if r.status != "open" or time.time() >= r.ends_at:
                return await _say(interaction, "⌛ Esa rifa ya terminó.")
            balance = await self.run(eco.get_balance, interaction.user.id)              # chequeo previo (el definitivo es atómico, dentro de la compra)
            cost = r.price * qty
            if balance < cost:
                return await _say(interaction, f"❌ No te alcanza: {qty} ticket{'s' if qty != 1 else ''} cuesta{'n' if qty != 1 else ''} {self.money(cost)} "
                                               f"y tenés {self.money(balance)}.")
            res = await self.run(rdb.buy_tickets, r.id, interaction.user.id, qty, f"i{interaction.id}")
            if res.status == "duplicate":
                return await _say(interaction, "ℹ️ Esa compra ya estaba procesada: no se cobró de nuevo.")
            if res.status == "closed" or res.status == "notfound":
                return await _say(interaction, "⌛ Esa rifa ya terminó.")
            if res.status == "limit":
                return await _say(interaction, f"❌ El máximo es {rdb.MAX_TICKETS_PER_USER} tickets por persona y ya tenés {res.mine}.")
            if res.status == "funds":
                return await _say(interaction, f"❌ No tenés saldo suficiente. Tu saldo: {self.money(res.balance)}.")
            await _say(interaction, f"✅ Compraste {qty} ticket{'s' if qty != 1 else ''} por {self.money(res.spent)}. "
                                    f"Ahora tenés **{res.mine}** en esta rifa. Tu saldo: {self.money(res.balance)}.")
            await self.refresh_panel(res.raffle)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ Ocurrió un error. Revisá tu saldo con `!bal` antes de reintentar.")

    # ---------- sorteo ----------
    @tasks.loop(seconds=10)
    async def ticker(self):
        try:
            await self.process_due()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def process_due(self, now: Optional[float] = None, rng=None) -> int:
        done = 0
        for rid in await self.run(rdb.due_raffles, now):
            r = await self.run(rdb.draw, rid, now, rng)
            if r is not None:
                done += 1
                await self.announce(r)
        return done

    async def announce(self, r: rdb.Raffle):
        await self.refresh_panel(r, closed=True)
        ch = await self._get_channel(r.channel_id)
        if ch is None:
            return
        try:
            if r.winner_id:
                chance = r.tickets.get(r.winner_id, 0) / max(r.total_tickets, 1) * 100
                e = discord.Embed(title=f"🏆 ¡Rifa #{r.id} sorteada!", color=GOLD,
                                  description=f"Premio: **{r.prize}**\nGanador: <@{r.winner_id}> (tenía {r.tickets.get(r.winner_id, 0)} de {r.total_tickets} tickets · {chance:.1f}%)")
                await ch.send(content=f"🎉 <@{r.winner_id}>", embed=e, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=r.winner_id)]))
            else:
                await ch.send(f"🎟️ La rifa #{r.id} (**{r.prize}**) terminó sin ningún ticket vendido.", allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ Rifas: no pude anunciar el resultado #{r.id}: {exc!r}")
        self._locks.pop(r.id, None)

    # ---------- comandos ----------
    @app_commands.command(name="rifas", description="(Staff) Crea una rifa: premio, precio de la entrada y tiempo (hh:mm)")
    @app_commands.describe(premio="Qué se sortea", precio="Precio por ticket (acepta 1m, 500k, 1.500.000…)", tiempo="Duración en horas:minutos, ej: 02:30")
    @app_commands.guild_only()
    async def rifas(self, interaction: discord.Interaction, premio: str, precio: str, tiempo: str):
        try:
            if interaction.guild is None or not can_manage(interaction.user):
                return await _say(interaction, "🔒 Solo el Council o los administradores pueden crear rifas.")
            try:
                price = eco.parse_amount_short(precio)
                seconds = rdb.parse_duration(tiempo)
            except (eco.AmountError, rdb.RaffleError) as exc:
                return await _say(interaction, f"❌ {exc}")
            try:
                r = await self.run(rdb.create_raffle, guild_id=interaction.guild.id, channel_id=interaction.channel.id, creator_id=interaction.user.id,
                                   prize=premio, price=price, duration_seconds=seconds)
            except rdb.RaffleError as exc:
                return await _say(interaction, f"❌ {exc}")
            await interaction.response.defer(ephemeral=True)
            try:
                msg = await interaction.channel.send(embed=self.panel_embed(r), view=RaffleView(self), allowed_mentions=NO_MENTIONS)
            except Exception:
                traceback.print_exc()
                await self.run(rdb.cancel, r.id)
                return await _say(interaction, "❌ No pude publicar la rifa en este canal (revisá los permisos del bot).")
            await self.run(rdb.set_message, r.id, msg.id)
            await _say(interaction, f"✅ Rifa #{r.id} publicada. Se sortea automáticamente cuando termine el tiempo.")
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ No pude crear la rifa.")

    @app_commands.command(name="rifa-cancelar", description="(Staff) Cancela una rifa abierta y devuelve lo cobrado")
    @app_commands.describe(numero="Número de la rifa (aparece en el título del panel)")
    @app_commands.guild_only()
    async def rifa_cancelar(self, interaction: discord.Interaction, numero: int):
        if interaction.guild is None or not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden cancelar rifas.")
        r = await self.run(rdb.cancel, numero)
        if r is None:
            return await _say(interaction, "❌ Esa rifa no existe o ya terminó.")
        await self.refresh_panel(r, closed=True)
        await _say(interaction, f"✅ Rifa #{r.id} cancelada. Se devolvieron {self.money(r.pot)} a {len(r.tickets)} persona(s).")


async def setup_raffles(bot: commands.Bot, db_path: str, guild_id: int) -> RafflesCog:
    cog = RafflesCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
