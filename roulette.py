"""Ruleta europea de casino: /ruleta (panel + hilo de apuestas), /ruleta-editar, /ruleta-stats. Apuestas con /apostar dentro del hilo.

Toda la lógica de dinero y reglas está en roulette_db.py (transacciones atómicas); acá solo hay interfaz de Discord."""
import asyncio
import time
import traceback
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import casino_db
import economy_db as eco
import roulette_db as rdb
from guildshop import can_manage
from memberguard import button_block_reason

NO_MENTIONS = discord.AllowedMentions.none()
GREEN_DARK = discord.Color(0x0B3D24)
BLACK = discord.Color(0x141414)
GOLD = discord.Color(0xD4AF37)
SEP = "━━━━━━━━━━━━━━━━━━━━"
MAX_LINES = 12
OPTION_CHOICES = ["rojo", "negro", "par", "impar", "falta", "pasa", "docena 1", "docena 2", "docena 3",
                  "columna 1", "columna 2", "columna 3", "numero "]


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


class RouletteView(discord.ui.View):
    """Botones persistentes: la ruleta se identifica por el id del mensaje, así que siguen andando tras un reinicio."""
    def __init__(self, cog: "RouletteCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Mis apuestas", style=discord.ButtonStyle.secondary, custom_id="roulette:mine", emoji="🎟️")
    async def mine(self, interaction: discord.Interaction, button):
        await self.cog.on_mine(interaction)

    @discord.ui.button(label="Girar ya", style=discord.ButtonStyle.primary, custom_id="roulette:spin", emoji="🎡")
    async def spin(self, interaction: discord.Interaction, button):
        await self.cog.on_spin(interaction)

    @discord.ui.button(label="CERRAR RULETA", style=discord.ButtonStyle.danger, custom_id="roulette:close", emoji="🔴")
    async def close(self, interaction: discord.Interaction, button):
        await self.cog.on_close(interaction)


class RouletteCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._dirty = set()                       # paneles con cambios pendientes de mostrar
        self._panel_locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self.run(rdb.init_schema)
        self.bot.add_view(RouletteView(self))
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    def money(self, amount: int) -> str:
        e = self.bot.get_cog("EconomyCog")
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    def _is_admin(self, user) -> bool:
        return can_manage(user)

    # ---------- panel ----------
    async def panel_embed(self, panel: rdb.Panel) -> discord.Embed:
        cfg = await self.run(rdb.get_config)
        rnd = await self.run(rdb.current_round, panel.id) if panel.status == "open" else None
        last = await self.run(rdb.last_finished_round, panel.id)
        is_open = panel.status == "open"
        e = discord.Embed(title="🎰  RULETA CASINO", color=GREEN_DARK if is_open else BLACK)
        e.description = f"{SEP}\n{'🟢 **ABIERTA**' if is_open else '🔴 **CERRADA**'}\n{SEP}"
        if rnd is not None:
            parts, n, staked = await self.run(rdb.round_summary, rnd.id)
        else:
            parts, n, staked = 0, 0, 0
        e.add_field(name="🏦 Balance de la casa", value=self.money(cfg.house_balance), inline=True)
        e.add_field(name="💼 Capital inicial", value=self.money(cfg.initial_capital), inline=True)
        e.add_field(name="🛡️ Reserva", value=self.money(cfg.reserve), inline=True)
        e.add_field(name="⬇️ Apuesta mínima", value=self.money(cfg.min_bet), inline=True)
        e.add_field(name="⬆️ Apuesta máxima", value=self.money(cfg.max_bet), inline=True)
        if last is not None:
            col = rdb.color_of(last.number)
            e.add_field(name="🎯 Último número", value=f"{rdb.COLOR_EMOJI[col]} **{last.number}** ({rdb.COLOR_NAME[col]})", inline=True)
        else:
            e.add_field(name="🎯 Último número", value="—", inline=True)
        if is_open:
            when = f"Gira <t:{int(rnd.ends_at)}:R>" if rnd is not None and rnd.ends_at else "Arranca con la primera apuesta"
            e.add_field(name=f"🎡 Ronda #{rnd.id if rnd else '—'}", value=when, inline=False)
            e.add_field(name="👥 Participantes", value=f"{parts:,} ({n:,} apuestas)", inline=True)
            e.add_field(name="💰 Total apostado", value=self.money(staked), inline=True)
        e.add_field(name=SEP, value="Apostá **dentro del hilo** con `/apostar`: `rojo`, `negro`, `par`, `impar`, `falta`, `pasa`, "
                                     "`docena 1-3`, `columna 1-3`, `numero 0-36`." if is_open else "La ruleta está cerrada. Las apuestas pendientes fueron devueltas.",
                    inline=False)
        e.set_footer(text="Ruleta europea · 37 números · ventaja de la casa 2,70%")
        return e

    async def _get_channel(self, channel_id):
        ch = self.bot.get_channel(channel_id) if channel_id else None
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def refresh_panel(self, panel_id: int):
        lock = self._panel_locks.setdefault(panel_id, asyncio.Lock())
        async with lock:
            panel = await self.run(rdb.get_panel, panel_id)
            if panel is None or not panel.message_id:
                return
            ch = await self._get_channel(panel.channel_id)
            if ch is None:
                return
            try:
                msg = await ch.fetch_message(panel.message_id)
                await msg.edit(embed=await self.panel_embed(panel), view=RouletteView(self) if panel.status == "open" else None)
            except Exception:
                traceback.print_exc()

    # ---------- comandos ----------
    def panel_link(self, panel: rdb.Panel) -> str:
        if panel.message_id:
            return f"https://discord.com/channels/{panel.guild_id}/{panel.channel_id}/{panel.message_id}"
        return f"https://discord.com/channels/{panel.guild_id}/{panel.channel_id}"

    @app_commands.command(name="ruleta", description="Abre la ruleta del casino (solo staff; hay una única ruleta en todo el servidor)")
    @app_commands.guild_only()
    async def ruleta(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            return await _say(interaction, "🔒 Solo administradores o el Council pueden abrir la ruleta.")
        ch = interaction.channel
        if ch is None or isinstance(ch, discord.Thread):
            return await _say(interaction, "❌ Usá `/ruleta` en un canal de texto (no dentro de un hilo).")
        await interaction.response.defer(ephemeral=True)
        panel, created = await self.run(rdb.create_panel, interaction.guild.id, ch.id, interaction.user.id)
        if not created:
            return await _say(interaction, f"❌ Ya hay una ruleta abierta en el servidor: {self.panel_link(panel)}")
        try:
            msg = await ch.send(embed=await self.panel_embed(panel), view=RouletteView(self), allowed_mentions=NO_MENTIONS)
            thread = await msg.create_thread(name="🎰 Apuestas de la ruleta", auto_archive_duration=10080)
            await self.run(rdb.set_panel_message, panel.id, msg.id, thread.id)
            await thread.send("🎰 **Bienvenidos a la ruleta.** Apostá acá con `/apostar` (ej: `/apostar rojo 100k`, `/apostar numero 17 25k`, "
                              "`/apostar docena 2 100k`). Podés hacer varias apuestas por ronda.", allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self.run(rdb.abort_panel, panel.id)           # libera el estado global
            return await _say(interaction, "❌ No pude publicar el panel (¿permisos para enviar mensajes y crear hilos?). La ruleta no quedó abierta.")
        await _say(interaction, f"✅ Ruleta abierta: {msg.jump_url}")

    @app_commands.command(name="ruleta-editar", description="Edita la configuración de la ruleta (solo staff)")
    @app_commands.describe(capital_inicial="Capital inicial (no cambia el balance actual)", balance_casa="Nuevo balance de la casa (ajuste auditado; requiere motivo)",
                           motivo="Motivo del ajuste de balance", apuesta_min="Apuesta mínima", apuesta_max="Apuesta máxima",
                           reserva="Reserva de seguridad", segundos="Duración de la ronda en segundos (15-600)",
                           pagos_editables="Habilitar/deshabilitar la edición de pagos", pago_color="Multiplicador rojo/negro",
                           pago_paridad="Multiplicador par/impar", pago_falta_pasa="Multiplicador falta/pasa", pago_docena="Multiplicador docenas",
                           pago_columna="Multiplicador columnas", pago_numero="Multiplicador número exacto")
    @app_commands.guild_only()
    async def ruleta_editar(self, interaction: discord.Interaction, capital_inicial: Optional[str] = None, balance_casa: Optional[str] = None,
                            motivo: Optional[str] = None, apuesta_min: Optional[str] = None, apuesta_max: Optional[str] = None,
                            reserva: Optional[str] = None, segundos: Optional[app_commands.Range[int, 15, 600]] = None,
                            pagos_editables: Optional[bool] = None, pago_color: Optional[app_commands.Range[int, 1, 1000]] = None,
                            pago_paridad: Optional[app_commands.Range[int, 1, 1000]] = None, pago_falta_pasa: Optional[app_commands.Range[int, 1, 1000]] = None,
                            pago_docena: Optional[app_commands.Range[int, 1, 1000]] = None, pago_columna: Optional[app_commands.Range[int, 1, 1000]] = None,
                            pago_numero: Optional[app_commands.Range[int, 1, 1000]] = None):
        if not self._is_admin(interaction.user):
            return await _say(interaction, "🔒 Solo administradores o el Council pueden editar la ruleta.")
        try:
            def amt(t):
                return None if t is None else eco.parse_amount_short(t)
            payouts = {k: v for k, v in (("color", pago_color), ("parity", pago_paridad), ("lowhigh", pago_falta_pasa), ("dozen", pago_docena),
                                          ("column", pago_columna), ("number", pago_numero)) if v is not None}
            new_balance = amt(balance_casa)
            changed = False
            if any(v is not None for v in (capital_inicial, apuesta_min, apuesta_max, reserva, segundos, pagos_editables)) or payouts:
                await self.run(rdb.update_config, interaction.user.id, initial_capital=amt(capital_inicial), min_bet=amt(apuesta_min),
                               max_bet=amt(apuesta_max), reserve=amt(reserva), round_seconds=segundos, payouts_enabled=pagos_editables,
                               payouts=payouts or None)
                changed = True
            if new_balance is not None:
                before, after = await self.run(rdb.adjust_house_balance, interaction.user.id, new_balance, motivo or "")
                changed = True
        except (rdb.RouletteError, eco.AmountError) as exc:
            return await _say(interaction, f"❌ {exc}")
        if not changed:
            return await _say(interaction, "ℹ️ No indicaste nada para cambiar. Mirá la configuración actual en el panel o con `/ruleta-stats`.")
        panel = await self.run(rdb.get_active_panel)
        if panel is not None:
            await self.refresh_panel(panel.id)
        cfg = await self.run(rdb.get_config)
        await _say(interaction, "✅ Configuración guardada (rige sin reiniciar; los pagos nuevos valen desde la próxima ronda).\n"
                                f"Capital inicial: {self.money(cfg.initial_capital)} · Balance casa: {self.money(cfg.house_balance)} · Reserva: {self.money(cfg.reserve)}\n"
                                f"Mín: {self.money(cfg.min_bet)} · Máx: {self.money(cfg.max_bet)} · Ronda: {cfg.round_seconds}s · "
                                f"Pagos editables: {'sí' if cfg.payouts_enabled else 'no'}")

    @app_commands.command(name="ruleta-stats", description="Estadísticas de la ruleta (solo staff)")
    @app_commands.guild_only()
    async def ruleta_stats(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            return await _say(interaction, "🔒 Solo administradores o el Council pueden ver las estadísticas.")
        s = await self.run(rdb.stats)
        c = s["config"]
        e = discord.Embed(title="📊 Estadísticas de la ruleta", color=GOLD)
        e.add_field(name="🏦 Balance actual", value=self.money(c.house_balance), inline=True)
        e.add_field(name="💼 Capital inicial", value=self.money(c.initial_capital), inline=True)
        e.add_field(name="🛡️ Reserva", value=self.money(c.reserve), inline=True)
        e.add_field(name="📈 Ganancias históricas", value=self.money(s["gains"]), inline=True)
        e.add_field(name="📉 Pérdidas históricas", value=self.money(s["losses"]), inline=True)
        e.add_field(name="💰 Total apostado", value=self.money(s["staked"]), inline=True)
        e.add_field(name="🏆 Total pagado", value=self.money(s["paid"]), inline=True)
        e.add_field(name="🎡 Rondas", value=f"{s['rounds']:,}", inline=True)
        e.add_field(name="👥 Jugadores distintos", value=f"{s['players']:,}", inline=True)
        e.add_field(name="🥇 Mayor premio pagado", value=self.money(s["biggest_payout"]), inline=True)
        e.add_field(name="💸 Mayor pérdida de la casa", value=self.money(s["biggest_house_loss"]), inline=True)
        if s["adjustments"]:
            lines = [f"`{a['at'][:16]}` <@{a['actor_id']}> · {a['note']}" + (f" ({'+' if a['delta'] >= 0 else ''}{a['delta']:,})" if a["kind"] == "admin_adjust" else "")
                     for a in s["adjustments"][:8]]
            e.add_field(name="🧾 Ajustes y cambios recientes", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- apuestas (las invoca bets.py cuando /apostar o !apostar se usan en un hilo de ruleta) ----------
    async def autocomplete_options(self, interaction, current: str):
        ch_id = interaction.channel.id if interaction.channel else 0
        panel = await self.run(rdb.find_panel_by_thread, ch_id)
        if panel is None:
            return None
        cur = (current or "").lower()
        return [app_commands.Choice(name=o.strip(), value=o) for o in OPTION_CHOICES if cur in o][:25]

    async def handle_bet_command(self, ctx, args: str) -> bool:
        """True si el canal es el hilo de una ruleta (y por lo tanto el comando ya se atendió acá)."""
        panel = await self.run(rdb.find_panel_by_thread, ctx.channel.id)
        if panel is None:
            return False

        async def reply(text):
            return await ctx.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS)

        user = ctx.author
        if panel.status != "open":
            await reply("❌ Esta ruleta está cerrada.")
            return True
        reason = await asyncio.to_thread(casino_db.block_reason, self.db_path, self.guild_id, user.id)
        if reason:
            await reply(reason)
            return True
        reason = await button_block_reason(self.bot, user)
        if reason:
            await reply(reason)
            return True
        parts = (args or "").strip().rsplit(None, 1)
        if len(parts) < 2:
            await reply("Uso: `/apostar [rojo|negro|par|impar|falta|pasa|docena 1-3|columna 1-3|numero 0-36] [cantidad]`")
            return True
        try:
            kind, sel = rdb.parse_selection(parts[0])
            amount = eco.parse_amount_short(parts[1])
            res = await self.run(rdb.place_bet, panel.id, user.id, kind, sel, amount, f"msg:{ctx.message.id}")
        except (rdb.RouletteError, eco.AmountError) as exc:
            await reply(f"❌ {exc}")
            return True
        msgs = {
            "closed": "❌ Las apuestas de esta ronda ya están cerradas: la ruleta está girando. Apostá en la próxima.",
            "min": f"❌ La apuesta mínima es {self.money(res.detail)}.",
            "max": f"❌ La apuesta máxima es {self.money(res.detail)}.",
            "funds": f"❌ No te alcanza el saldo. Tu saldo: {self.money(res.balance)}.",
            "frozen": eco.FROZEN_TEXT,
            "exposure": "❌ La casa no puede cubrir esa apuesta ahora mismo (límite de riesgo de la ronda). Probá con un monto menor o esperá a la próxima ronda.",
            "too_many": f"❌ Llegaste al máximo de {res.detail} apuestas por ronda.",
            "duplicate": "ℹ️ Esa apuesta ya estaba registrada.",
            "notfound": "❌ No encontré esa ruleta.",
        }
        if res.status != "ok":
            await reply(msgs.get(res.status, "❌ No se pudo apostar."))
            return True
        await reply(f"✅ {user.mention} apostó **{self.money(amount)}** a **{rdb.label_of(kind, sel)}** (ronda #{res.round.id}). "
                    f"Tu saldo: {self.money(res.balance)}. Gira <t:{int(res.round.ends_at)}:R>.")
        self._dirty.add(panel.id)
        return True

    # ---------- botones ----------
    async def _panel_of(self, interaction) -> Optional[rdb.Panel]:
        return await self.run(rdb.find_panel_by_message, interaction.message.id) if interaction.message else None

    async def on_mine(self, interaction: discord.Interaction):
        panel = await self._panel_of(interaction)
        rnd = await self.run(rdb.current_round, panel.id) if panel and panel.status == "open" else None
        if rnd is None:
            return await _say(interaction, "ℹ️ No hay una ronda en curso.")
        bets = await self.run(rdb.user_bets, rnd.id, interaction.user.id)
        if not bets:
            return await _say(interaction, "ℹ️ Todavía no apostaste en esta ronda. Hacelo en el hilo con `/apostar`.")
        lines = [f"• {rdb.label_of(b.kind, b.selection)} — {self.money(b.amount)}" for b in bets[:25]]
        await _say(interaction, f"🎟️ **Tus apuestas (ronda #{rnd.id})** — total {self.money(sum(b.amount for b in bets))}\n" + "\n".join(lines))

    async def on_spin(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            return await _say(interaction, "🔒 Solo el staff puede girar la ruleta.")
        panel = await self._panel_of(interaction)
        rnd = await self.run(rdb.current_round, panel.id) if panel and panel.status == "open" else None
        if rnd is None:
            return await _say(interaction, "ℹ️ No hay ronda en curso.")
        await interaction.response.defer(ephemeral=True)
        st = await self.run(rdb.resolve_round, rnd.id, None, True)
        if st is None:
            return await _say(interaction, "ℹ️ Todavía no hay apuestas en esta ronda (o ya se resolvió).")
        await self.announce(panel, st)
        await _say(interaction, "✅ Ruleta girada.")

    async def on_close(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            return await _say(interaction, "🔒 Solo administradores o el Council pueden cerrar la ruleta.")
        panel = await self._panel_of(interaction)
        if panel is None:
            return await _say(interaction, "❌ No encontré esta ruleta.")
        await interaction.response.defer(ephemeral=True)
        st = await self.run(rdb.close_panel, panel.id, interaction.user.id)
        if st is None:
            await self.refresh_panel(panel.id)
            return await _say(interaction, "ℹ️ La ruleta ya estaba cerrada.")
        await self.refresh_panel(panel.id)
        thread = await self._get_channel(panel.thread_id)
        refunded = sum(st.stake_by_user.values())
        if thread is not None:
            try:
                txt = "🔴 **La ruleta fue cerrada.** No se aceptan más apuestas."
                if refunded:
                    txt += f"\n↩️ Se devolvieron {self.money(refunded)} a {len(st.stake_by_user)} jugador(es) de la ronda sin resolver."
                await thread.send(txt, allowed_mentions=NO_MENTIONS)
            except Exception:
                traceback.print_exc()
        await _say(interaction, "✅ Ruleta cerrada." + (f" Se devolvieron {self.money(refunded)}." if refunded else ""))

    # ---------- giro automático ----------
    @tasks.loop(seconds=3)
    async def ticker(self):
        try:
            await self.process_due()
            for pid in list(self._dirty):
                self._dirty.discard(pid)
                await self.refresh_panel(pid)
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def process_due(self, now: Optional[float] = None) -> int:
        done = 0
        for rid in await self.run(rdb.due_rounds, now):
            st = await self.run(rdb.resolve_round, rid, now)
            if st is not None:
                done += 1
                panel = await self.run(rdb.get_panel, st.round.panel_id)
                if panel is not None:
                    await self.announce(panel, st)
        return done

    def result_embed(self, st: rdb.Settlement) -> discord.Embed:
        if st.status != "done":
            e = discord.Embed(title=f"🎡 Ronda #{st.round.id} cancelada", description=st.note or "Se devolvieron todas las apuestas.", color=BLACK)
            return e
        col = rdb.color_of(st.number)
        e = discord.Embed(title=f"🎡 Ronda #{st.round.id} — salió {rdb.COLOR_EMOJI[col]} {st.number} ({rdb.COLOR_NAME[col]})",
                          color=discord.Color.green() if col == "green" else (discord.Color.red() if col == "red" else BLACK))
        e.add_field(name="👥 Participantes", value=f"{st.round.participants:,}", inline=True)
        e.add_field(name="✅ Apuestas ganadoras", value=f"{len(st.winners):,}", inline=True)
        e.add_field(name="❌ Apuestas perdedoras", value=f"{len(st.losers):,}", inline=True)
        e.add_field(name="💰 Total apostado", value=self.money(st.round.total_staked), inline=True)
        e.add_field(name="🏆 Premios entregados", value=self.money(st.round.total_paid), inline=True)
        sign = "+" if st.round.house_delta >= 0 else "-"
        e.add_field(name="🏦 Resultado de la casa", value=f"{sign}{self.money(abs(st.round.house_delta))}", inline=True)
        if st.winners:
            lines = [f"<@{b.user_id}> · {rdb.label_of(b.kind, b.selection)} · apostó {self.money(b.amount)} → **{self.money(b.payout)}**"
                     for b in sorted(st.winners, key=lambda b: -b.payout)[:MAX_LINES]]
            if len(st.winners) > MAX_LINES:
                lines.append(f"… y {len(st.winners) - MAX_LINES} más")
            e.add_field(name="🥇 Ganadores", value="\n".join(lines)[:1024], inline=False)
        if st.losers:
            lines = [f"<@{b.user_id}> · {rdb.label_of(b.kind, b.selection)} · {self.money(b.amount)}" for b in st.losers[:MAX_LINES]]
            if len(st.losers) > MAX_LINES:
                lines.append(f"… y {len(st.losers) - MAX_LINES} más")
            e.add_field(name="💀 Perdedores", value="\n".join(lines)[:1024], inline=False)
        return e

    async def announce(self, panel: rdb.Panel, st: rdb.Settlement):
        await self.refresh_panel(panel.id)
        thread = await self._get_channel(panel.thread_id)
        if thread is None:
            return
        try:
            await thread.send(embed=self.result_embed(st), allowed_mentions=NO_MENTIONS)
            if st.next_round_id:
                await thread.send("🟢 **Nueva ronda abierta.** ¡Hagan sus apuestas!", allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()


async def setup_roulette(bot: commands.Bot, db_path: str, guild_id: int) -> RouletteCog:
    cog = RouletteCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
