"""Apuestas con Wuju Coins — capa de Discord (comandos, panel, hilo).

Toda la lógica de dinero y estado vive en bets_db.py (una transacción por acción). Acá solo se validan
permisos de Discord en CADA ejecución, se arman Embeds y se mantiene el panel sincronizado con la base.
"""
import asyncio
import functools
import time
import traceback
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import bets_db as bdb
import economy_db as eco
from slashctx import SlashContext

NO_MENTIONS = discord.AllowedMentions.none()
STATUS_LABEL = {"open": "🟢 Abierta", "closed": "🔒 Cerrada (esperando resultado)", "resolved": "🏆 Resuelta", "cancelled": "🚫 Cancelada"}
STATUS_COLOR = {"open": 0x2ECC71, "closed": 0xF1C40F, "resolved": 0x3498DB, "cancelled": 0x95A5A6}
FIELD_BUDGET = 950             # margen bajo el límite de 1024 por campo
EMBED_BUDGET = 5200            # margen bajo los 6000 caracteres por mensaje
INLINE_PARTICIPANT_FIELDS = 2


def chunk_lines(lines: List[str], budget: int) -> List[str]:
    pages, cur, size = [], [], 0
    for line in lines:
        if len(line) > budget:
            line = line[:budget - 1] + "…"
        if cur and size + len(line) + 1 > budget:
            pages.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        pages.append("\n".join(cur))
    return pages


def participant_lines(bet: bdb.Bet) -> List[str]:
    totals = bet.user_totals()
    order = sorted(totals, key=lambda u: (-sum(totals[u].values()), u))
    lines = []
    for uid in order:
        parts = " · ".join(f"{bet.options[i]}: {eco.format_amount(a)}" for i, a in sorted(totals[uid].items()))
        lines.append(f"<@{uid}> — {parts}")
    return lines


def build_panel_embed(bet: bdb.Bet, money: Callable[[int], str]) -> discord.Embed:
    emb = discord.Embed(title=f"🎲 Apuesta #{bet.id}", description=f"**{bet.statement}**", color=discord.Color(STATUS_COLOR[bet.status]))
    emb.add_field(name="Estado", value=STATUS_LABEL[bet.status], inline=True)
    emb.add_field(name="Creada por", value=f"<@{bet.creator_id}>", inline=True)
    emb.add_field(name="Pozo total", value=f"**{money(bet.pool)}**", inline=True)
    emb.add_field(name="Participantes", value=str(len(bet.participants)), inline=True)

    opt_lines = []
    for i, label in enumerate(bet.options):
        mark = "🏆 " if bet.status == "resolved" and bet.winner_idx == i else ""
        opt_lines.append(f"{mark}**{i + 1}. {label}** — {money(bet.option_total(i))} · {len(bet.option_participants(i))} persona(s)")
    for n, page in enumerate(chunk_lines(opt_lines, FIELD_BUDGET)):
        emb.add_field(name="Opciones" if n == 0 else "Opciones (cont.)", value=page, inline=False)

    if bet.status == "open":
        emb.add_field(name="Cómo apostar", value="Escribí en el hilo: `!apostar [opción] [cantidad]`\nEj: `!apostar " + f"{bet.options[0]} 5000`", inline=False)

    if bet.status == "resolved":
        winner = bet.options[bet.winner_idx] if bet.winner_idx is not None else "?"
        text = f"Opción ganadora: **{winner}**\nRepartido: **{money(bet.distributed)}**"
        if bet.remainder:
            text += f"\nSobrante por redondeo (no distribuido): {money(bet.remainder)}"
        emb.add_field(name="Resultado", value=text, inline=False)
        pay_lines = [f"<@{u}> recibió **{money(a)}**" for u, a in sorted(bet.payouts.items(), key=lambda kv: (-kv[1], kv[0]))]
        pages = chunk_lines(pay_lines, FIELD_BUDGET)
        if pages:
            more = len(chunk_lines(pay_lines, FIELD_BUDGET)) - 1
            emb.add_field(name="Pagos", value=pages[0] + (f"\n… y más (ver detalle)" if more else ""), inline=False)
    elif bet.status == "cancelled":
        why = {"no_winner": "Nadie apostó a la opción ganadora: se devolvió todo.", "no_publicada": "No se pudo publicar la apuesta."}.get(
            bet.outcome or "", "La apuesta fue cancelada y se devolvieron todas las apuestas.")
        emb.add_field(name="Resultado", value=why, inline=False)

    lines = participant_lines(bet)
    if lines:
        pages = chunk_lines(lines, FIELD_BUDGET)
        shown = pages[:INLINE_PARTICIPANT_FIELDS]
        for n, page in enumerate(shown):
            emb.add_field(name="Apuestas realizadas" if n == 0 else "Apuestas (cont.)", value=page, inline=False)
        hidden = len(pages) - len(shown)
        if hidden > 0:
            emb.add_field(name="…", value="Hay más participantes. Tocá **📋 Ver detalle** para ver la lista completa.", inline=False)
    emb.set_footer(text=f"Apuesta #{bet.id}")
    # Seguridad final: nunca superar el límite del mensaje
    while len(emb) > EMBED_BUDGET and len(emb.fields) > 6:
        emb.remove_field(len(emb.fields) - 2) if hasattr(emb, "remove_field") else emb.fields.pop(-2)
    return emb


def guard(func):
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        try:
            return await func(self, ctx, *args, **kwargs)
        except Exception:
            traceback.print_exc()
            try:
                await ctx.reply("❌ Ocurrió un error inesperado. No se descontó nada sin registrarse; revisá tu saldo con `!bal`.",
                                mention_author=False, allowed_mentions=NO_MENTIONS)
            except Exception:
                pass
    return wrapper


def safe(func):
    @functools.wraps(func)
    async def wrapper(self, interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except bdb.BetError as e:
            await self.reply(interaction, f"❌ {e.message}")
        except Exception:
            traceback.print_exc()
            try:
                await self.reply(interaction, "❌ Ocurrió un error interno. Probá de nuevo en unos segundos.")
            except Exception:
                pass
    return wrapper


# ============================================================
# Vistas
# ============================================================
class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, on_confirm: Callable, label: str = "Confirmar"):
        super().__init__(timeout=120.0)
        self.owner_id, self.on_confirm, self.done = owner_id, on_confirm, False
        ok = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Este mensaje no es tuyo.", ephemeral=True)
            return False
        return True

    async def _ok(self, interaction):
        if self.done:
            return await interaction.response.send_message("Ya fue procesado.", ephemeral=True)
        self.done = True
        self.stop()
        await self.on_confirm(interaction)

    async def _no(self, interaction):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelado.", view=None)


class DetailView(discord.ui.View):
    """Lista completa de participantes, paginada (efímera)."""

    def __init__(self, title: str, lines: List[str]):
        super().__init__(timeout=300.0)
        self.title = title
        self.pages = chunk_lines(lines, 3500) or ["Todavía no hay apuestas."]
        self.page = 0
        prev_b = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        next_b = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        prev_b.callback, next_b.callback = self._prev, self._next
        self.add_item(prev_b)
        self.add_item(next_b)

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=self.title, description=self.pages[self.page], color=discord.Color.blurple())
        e.set_footer(text=f"Página {self.page + 1}/{len(self.pages)}")
        return e

    async def _prev(self, interaction):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _next(self, interaction):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)


class BetPanelView(discord.ui.View):
    def __init__(self, cog: "BetsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Ver detalle", style=discord.ButtonStyle.secondary, custom_id="bet:detail", emoji="📋")
    async def detail(self, interaction, button):
        await self.cog.show_detail(interaction)

    @discord.ui.button(label="Cancelar apuesta", style=discord.ButtonStyle.danger, custom_id="bet:cancel", emoji="🚫")
    async def cancel(self, interaction, button):
        await self.cog.cancel_from_panel(interaction)


# ============================================================
# Cog
# ============================================================
class BetsCog(commands.Cog):
    def __init__(self, bot, db_path: str):
        self.bot, self.db_path = bot, db_path
        self._locks: Dict[int, asyncio.Lock] = {}
        self._retry_at: Dict[int, float] = {}

    # ---------- infraestructura ----------
    async def cog_load(self):
        await self.run(bdb.init_schema)
        self.bot.add_view(BetPanelView(self))
        await self.run(bdb.mark_all_dirty)            # al arrancar se resincronizan los paneles pendientes (sin duplicarlos)
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def money(self, amount: int) -> str:
        eco_cog = self.economy
        return eco_cog.money(amount) if eco_cog else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    async def reply(self, interaction, content=None, *, embed=None, view=None, ephemeral=True):
        kwargs = {k: v for k, v in dict(content=content, embed=embed, view=view, ephemeral=ephemeral,
                                        allowed_mentions=NO_MENTIONS).items() if v is not None}
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    def is_admin(self, member) -> bool:
        from economy import is_admin_member
        return is_admin_member(member)

    async def can_create(self, member) -> bool:
        eco_cog = self.economy
        if eco_cog is not None:
            return await eco_cog.has_command_permission(member, "apuesta")
        return self.is_admin(member)

    # ---------- panel ----------
    @tasks.loop(seconds=5)
    async def ticker(self):
        try:
            for bet_id in await self.run(bdb.needing_sync):
                await self.sync_panel(bet_id)
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def _get_channel(self, channel_id: Optional[int]):
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def sync_panel(self, bet_id: int):
        lock = self._locks.setdefault(bet_id, asyncio.Lock())
        async with lock:
            bet = await self.run(bdb.get_bet, bet_id)
            if bet is None or bet.channel_id is None or time.monotonic() < self._retry_at.get(bet_id, 0):
                return
            rev = bet.panel_rev
            channel = await self._get_channel(bet.channel_id)
            if channel is None:
                self._retry_at[bet_id] = time.monotonic() + 300
                return
            embed = build_panel_embed(bet, self.money)
            message = None
            if bet.message_id:
                try:
                    message = await channel.fetch_message(bet.message_id)
                except discord.NotFound:
                    message = None
                except Exception:
                    traceback.print_exc()
                    self._retry_at[bet_id] = time.monotonic() + 60
                    return
            try:
                if message is not None:
                    await message.edit(embed=embed, view=BetPanelView(self), allowed_mentions=NO_MENTIONS)
                elif not bet.is_final:
                    # El mensaje fue borrado: se recrea UNA vez. Los datos de la apuesta nunca dependen del mensaje.
                    message = await channel.send(embed=embed, view=BetPanelView(self), allowed_mentions=NO_MENTIONS)
                    await self.run(bdb.set_location, bet_id, message_id=message.id)
                await self.run(bdb.mark_synced, bet_id, rev)
                self._retry_at.pop(bet_id, None)
            except Exception:
                traceback.print_exc()
                self._retry_at[bet_id] = time.monotonic() + 60

    async def thread_say(self, bet: bdb.Bet, text: str):
        target = await self._get_channel(bet.thread_id)
        if target is None:
            return
        try:
            await target.send(text, allowed_mentions=NO_MENTIONS)
        except Exception as exc:                      # hilo archivado/borrado, sin permisos...
            print(f"⚠️ No pude escribir en el hilo de la apuesta #{bet.id}: {exc!r}")

    # ---------- /apuesta ----------
    @app_commands.command(name="apuesta", description="Crea una apuesta con Wuju Coins")
    @app_commands.describe(enunciado="Qué se apuesta", opciones="Opciones separadas por comas (ej: Gana Smogg, Pierde Smogg, Empate)")
    @app_commands.guild_only()
    @safe
    async def apuesta(self, interaction: discord.Interaction, enunciado: str, opciones: str):
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member) or not await self.can_create(member):
            return await self.reply(interaction, "❌ No tenés permiso para crear apuestas.")
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await self.reply(interaction, "❌ Creá la apuesta en un canal de texto del servidor (no dentro de un hilo).")
        options = bdb.parse_options(opciones)
        await interaction.response.defer(ephemeral=True)
        bet_id = await self.run(bdb.create_bet, guild_id=interaction.guild.id, channel_id=channel.id,
                                creator_id=member.id, statement=enunciado, options=options)
        bet = await self.run(bdb.get_bet, bet_id)
        try:
            message = await channel.send(embed=build_panel_embed(bet, self.money), view=BetPanelView(self), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self.run(bdb.abort_unpublished, bet_id)
            return await interaction.followup.send("❌ No pude publicar el panel (¿permisos del bot en este canal?). La apuesta no se creó.", ephemeral=True)
        await self.run(bdb.set_location, bet_id, message_id=message.id)
        try:
            try:
                thread = await message.create_thread(name=f"Apuesta #{bet_id}", auto_archive_duration=10080)
            except discord.HTTPException:
                thread = await message.create_thread(name=f"Apuesta #{bet_id}", auto_archive_duration=1440)
        except Exception:
            traceback.print_exc()
            await self.run(bdb.abort_unpublished, bet_id)
            try:
                await message.delete()
            except Exception:
                pass
            return await interaction.followup.send("❌ No pude crear el hilo (revisá el permiso *Crear hilos públicos* del bot). La apuesta se canceló.", ephemeral=True)
        await self.run(bdb.set_location, bet_id, thread_id=thread.id)
        try:
            await thread.send(f"Apostá acá con `!apostar [opción] [cantidad]`. Opciones: " + " · ".join(f"`{o}`" for o in options),
                              allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        await interaction.followup.send(f"✅ Apuesta #{bet_id} creada. Hilo: {thread.mention}", ephemeral=True)

    # ---------- !apostar ----------
    @commands.command(name="apostar")
    @guard
    async def apostar(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        bet = await self.run(bdb.find_by_thread, ctx.channel.id)
        if bet is None:
            return await ctx.reply("❌ `!apostar` solo se puede usar dentro del hilo de una apuesta abierta.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        try:
            option_text, amount_text = bdb.split_option_and_amount(args)
            amount = eco.parse_amount(amount_text)
        except (bdb.BetError, eco.AmountError) as exc:
            msg = exc.message if isinstance(exc, bdb.BetError) else str(exc)
            return await ctx.reply(f"❌ {msg}", mention_author=False, allowed_mentions=NO_MENTIONS)
        result = await self.run(bdb.place_wager, bet.id, ctx.author.id, option_text, amount, thread_id=ctx.channel.id)
        if not result.ok:
            extra = f" Tu saldo: {self.money(result.balance)}." if result.code == "funds" and result.balance is not None else ""
            return await ctx.reply(f"❌ {result.message}{extra}", mention_author=False, allowed_mentions=NO_MENTIONS)
        label = bet.options[result.option_idx]
        await ctx.reply(f"✅ {ctx.author.mention} apostó **{self.money(amount)}** a **{label}**. Tu saldo ahora: {self.money(result.balance)}.",
                        mention_author=False, allowed_mentions=NO_MENTIONS)
        # el panel se actualiza solo (ticker) a partir de la marca guardada en la base

    # ---------- !apostartodo ----------
    @commands.command(name="apostartodo")
    @guard
    async def apostartodo(self, ctx: commands.Context, *, opcion: str = ""):
        """Apuesta TODO el saldo a una opción, pidiendo confirmación antes de descontar nada."""
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        bet = await self.run(bdb.find_by_thread, ctx.channel.id)
        if bet is None:
            return await ctx.reply("❌ `!apostartodo` solo se puede usar dentro del hilo de una apuesta abierta.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        option_text = " ".join((opcion or "").split())
        if not option_text:
            return await ctx.reply("Uso: `!apostartodo [opción]` (apuesta todo tu saldo a esa opción).",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        if bet.status != "open":
            return await ctx.reply("❌ Esa apuesta ya no acepta más apuestas.", mention_author=False, allowed_mentions=NO_MENTIONS)
        idx = bet.find_option(option_text)
        if idx is None:
            listing = " · ".join(f"`{o}`" for o in bet.options)
            return await ctx.reply(f"❌ Esa opción no existe. Opciones: {listing}", mention_author=False, allowed_mentions=NO_MENTIONS)
        balance = await self.run(eco.get_balance, ctx.author.id)
        if balance <= 0:
            return await ctx.reply("❌ No tenés saldo para apostar.", mention_author=False, allowed_mentions=NO_MENTIONS)
        label = bet.options[idx]
        author_id = ctx.author.id
        thread_id = ctx.channel.id

        async def go(inter):
            now_balance = await self.run(eco.get_balance, author_id)
            if now_balance != balance:
                return await inter.response.edit_message(
                    content=f"⚠️ Tu saldo cambió ({self.money(balance)} → {self.money(now_balance)}). No se apostó nada: volvé a usar el comando.", view=None)
            result = await self.run(bdb.place_wager, bet.id, author_id, label, balance, thread_id=thread_id)
            if not result.ok:
                return await inter.response.edit_message(content=f"❌ {result.message}", view=None)
            await inter.response.edit_message(
                content=f"✅ <@{author_id}> apostó **todo su saldo ({self.money(balance)})** a **{label}**. Tu saldo ahora: {self.money(result.balance)}.", view=None)

        await ctx.reply(f"⚠️ ¿Confirmás apostar **TODO tu saldo ({self.money(balance)})** a **{label}**?\n"
                        "Se descuenta al instante y no se puede retirar.",
                        view=ConfirmView(author_id, go, "Sí, apostar todo"), mention_author=False, allowed_mentions=NO_MENTIONS)

    # ---------- versiones slash ----------
    @app_commands.command(name="apostar", description="Apostá Wuju Coins a una opción (dentro del hilo de la apuesta)")
    @app_commands.describe(opcion="Opción a la que apostás", cantidad="Cantidad (ej: 5.000)")
    @app_commands.guild_only()
    async def apostar_slash(self, interaction: discord.Interaction, opcion: str, cantidad: str):
        await self.apostar.callback(self, SlashContext(interaction), args=f"{opcion} {cantidad}")

    @app_commands.command(name="apostartodo", description="Apostá TODO tu saldo a una opción (pide confirmación)")
    @app_commands.describe(opcion="Opción a la que apostás todo")
    @app_commands.guild_only()
    async def apostartodo_slash(self, interaction: discord.Interaction, opcion: str):
        await self.apostartodo.callback(self, SlashContext(interaction), opcion=opcion)

    @apostar_slash.autocomplete("opcion")
    async def _ac_apostar(self, interaction, current: str):
        return await self.option_autocomplete(interaction, current)

    @apostartodo_slash.autocomplete("opcion")
    async def _ac_apostartodo(self, interaction, current: str):
        return await self.option_autocomplete(interaction, current)

    # ---------- cerrar / resolver / cancelar ----------
    async def _target_bet(self, interaction, bet_id: Optional[int]) -> Optional[bdb.Bet]:
        if bet_id is not None:
            bet = await self.run(bdb.get_bet, bet_id)
        else:
            bet = await self.run(bdb.find_by_thread, interaction.channel.id if interaction.channel else 0)
        if bet is None:
            await self.reply(interaction, "❌ No encuentro la apuesta. Usá el comando dentro de su hilo o indicá el `id`.")
        return bet

    @app_commands.command(name="cerrarapuesta", description="Cierra una apuesta: ya no se aceptan más apuestas (solo quien la creó)")
    @app_commands.describe(id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    @app_commands.guild_only()
    @safe
    async def cerrarapuesta(self, interaction: discord.Interaction, id: Optional[int] = None):
        bet = await self._target_bet(interaction, id)
        if bet is None:
            return
        if interaction.user.id != bet.creator_id:
            return await self.reply(interaction, "❌ Solo quien creó la apuesta puede cerrarla.")
        res = await self.run(bdb.close_bet, bet.id, interaction.user.id)
        if not res.ok:
            return await self.reply(interaction, f"❌ {res.message}")
        await self.reply(interaction, f"🔒 Apuesta #{bet.id} cerrada. Elegí el resultado con `/resolverapuesta`.")
        await self.sync_panel(bet.id)
        await self.thread_say(bet, "🔒 La apuesta se cerró: ya no se aceptan más apuestas.")

    async def option_autocomplete(self, interaction, current: str):
        bet = await self.run(bdb.find_by_thread, interaction.channel.id if interaction.channel else 0)
        if bet is None:
            return []
        cur = bdb.normalize(current)
        return [app_commands.Choice(name=o[:100], value=o) for o in bet.options if cur in bdb.normalize(o)][:25]

    @app_commands.command(name="resolverapuesta", description="Define la opción ganadora y reparte el pozo (solo quien la creó)")
    @app_commands.describe(opcion="Opción ganadora", id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    @app_commands.guild_only()
    @safe
    async def resolverapuesta(self, interaction: discord.Interaction, opcion: str, id: Optional[int] = None):
        bet = await self._target_bet(interaction, id)
        if bet is None:
            return
        if interaction.user.id != bet.creator_id:
            return await self.reply(interaction, "❌ Solo quien creó la apuesta puede resolverla.")
        if bet.status == "open":
            return await self.reply(interaction, "❌ Primero cerrá la apuesta con `/cerrarapuesta`.")
        if bet.status != "closed":
            return await self.reply(interaction, "❌ Esa apuesta ya fue resuelta o cancelada.")
        idx = bet.find_option(opcion)
        if idx is None:
            return await self.reply(interaction, "❌ Esa opción no existe. Opciones: " + " · ".join(f"`{o}`" for o in bet.options))
        label = bet.options[idx]
        winners = bet.option_participants(idx)
        if winners:
            warn = f"Ganan **{len(winners)}** persona(s); se reparte el pozo de **{self.money(bet.pool)}** según lo apostado a esta opción."
        else:
            warn = "⚠️ **Nadie apostó a esa opción**: la apuesta se cancelará y se devolverá todo."

        async def go(inter):
            res = await self.run(bdb.resolve_bet, bet.id, interaction.user.id, idx)
            if not res.ok and res.code != "no_winner":
                return await inter.response.edit_message(content=f"❌ {res.message}", view=None)
            text = "🏆 Apuesta resuelta y pagada." if res.ok else f"ℹ️ {res.message}"
            await inter.response.edit_message(content=text, view=None)
            await self.sync_panel(bet.id)
            if res.ok:
                lines = [f"<@{u}> recibió {self.money(a)}" for u, a in sorted(res.payouts.items(), key=lambda kv: (-kv[1], kv[0]))]
                shown = lines[:15] + ([f"… y {len(lines) - 15} más"] if len(lines) > 15 else [])
                await self.thread_say(bet, f"🏆 Ganó **{label}**. Repartido: {self.money(sum(res.payouts.values()))}"
                                           + (f" (sobrante por redondeo: {self.money(res.remainder)})" if res.remainder else "")
                                           + "\n" + "\n".join(shown))
            else:
                await self.thread_say(bet, "🚫 Nadie apostó a la opción elegida: la apuesta se canceló y se devolvieron todas las apuestas.")
        await self.reply(interaction, f"¿Confirmás que la opción ganadora es **{label}**?\n{warn}\nEsta acción no se puede deshacer.",
                         view=ConfirmView(interaction.user.id, go, "Sí, resolver"))

    @resolverapuesta.autocomplete("opcion")
    async def _ac_opcion(self, interaction, current: str):
        return await self.option_autocomplete(interaction, current)

    @app_commands.command(name="cancelarapuesta", description="Cancela una apuesta y devuelve todo lo apostado (creador o administrador)")
    @app_commands.describe(id="Número de la apuesta (opcional si lo usás dentro de su hilo)")
    @app_commands.guild_only()
    @safe
    async def cancelarapuesta(self, interaction: discord.Interaction, id: Optional[int] = None):
        bet = await self._target_bet(interaction, id)
        if bet is None:
            return
        await self._ask_cancel(interaction, bet)

    @safe
    async def cancel_from_panel(self, interaction):
        """Botón 🚫 del panel de la apuesta: solo quien la creó (o un administrador)."""
        msg = getattr(interaction, "message", None)
        bet = await self.run(bdb.find_by_message, msg.id) if msg is not None else None
        if bet is None:
            return await self.reply(interaction, "❌ No encuentro la apuesta de este panel.")
        await self._ask_cancel(interaction, bet)

    async def _ask_cancel(self, interaction, bet: bdb.Bet):
        if interaction.user.id != bet.creator_id and not self.is_admin(interaction.user):
            return await self.reply(interaction, "❌ Solo quien creó la apuesta (o un administrador) puede cancelarla.")
        if bet.is_final:
            return await self.reply(interaction, "❌ Esa apuesta ya fue resuelta o cancelada.")

        async def go(inter):
            res = await self.run(bdb.cancel_bet, bet.id, interaction.user.id, is_admin=self.is_admin(interaction.user))
            if not res.ok:
                return await inter.response.edit_message(content=f"❌ {res.message}", view=None)
            await inter.response.edit_message(content=f"🚫 Apuesta #{bet.id} cancelada. Se devolvieron {self.money(sum(res.payouts.values()))} a {len(res.payouts)} persona(s).", view=None)
            await self.sync_panel(bet.id)
            await self.thread_say(bet, "🚫 La apuesta fue cancelada y se devolvió todo lo apostado.")
        await self.reply(interaction, f"¿Cancelar la apuesta #{bet.id}? Se devolverá {self.money(bet.pool)} a quienes apostaron.",
                         view=ConfirmView(interaction.user.id, go, "Sí, cancelar"))

    # ---------- detalle ----------
    @safe
    async def show_detail(self, interaction):
        msg = getattr(interaction, "message", None)
        bet = await self.run(bdb.find_by_message, msg.id) if msg is not None else None
        if bet is None:
            return await self.reply(interaction, "❌ No encuentro esa apuesta.")
        lines = participant_lines(bet)
        view = DetailView(f"Apuesta #{bet.id} — participantes ({len(bet.participants)})", lines)
        await self.reply(interaction, embed=view.embed(), view=view)


async def setup_bets(bot, db_path: str, guild_id: int) -> BetsCog:
    cog = BetsCog(bot, db_path)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
