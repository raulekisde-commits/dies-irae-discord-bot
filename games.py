"""Juegos con Wuju Coins — /dados y /coinflip (capa de Discord).

La lógica de dinero y estado vive en games_db.py. Acá: permisos de creación (canal + rol, igual que el resto de la
economía), paneles con botón, el avance por tiempo (cada 5 s se resuelven los juegos vencidos, también tras un reinicio)
y los mensajes de resultado.
"""
import asyncio
import functools
import time
import traceback
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco
import casino_db as cdb
import games_db as gdb
import stats_db
from slashctx import SlashContext

ERROR_VISIBLE_SECONDS = 8
NO_MENTIONS = discord.AllowedMentions.none()
DICE_FACES = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}
MAX_LISTED = 40
GOLD, GREEN, GREY = 0xF1C40F, 0x2ECC71, 0x95A5A6


def guard(func):
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        try:
            return await func(self, ctx, *args, **kwargs)
        except Exception:
            traceback.print_exc()
            try:
                await ctx.reply("❌ Ocurrió un error inesperado. Revisá tu saldo con `!bal` antes de repetir la operación.",
                                mention_author=False, allowed_mentions=NO_MENTIONS)
            except Exception:
                pass
    return wrapper


# ============================================================
# Vistas persistentes
# ============================================================
class DiceJoinView(discord.ui.View):
    def __init__(self, cog: "GamesCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Anotarse", style=discord.ButtonStyle.success, custom_id="game:dice_join", emoji="🎲")
    async def join(self, interaction: discord.Interaction, button):
        await self.cog.on_join(interaction)


class CoinAcceptView(discord.ui.View):
    def __init__(self, cog: "GamesCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Aceptar el desafío", style=discord.ButtonStyle.primary, custom_id="game:coin_accept", emoji="🪙")
    async def accept(self, interaction: discord.Interaction, button):
        await self.cog.on_join(interaction)


class GamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._locks: Dict[int, asyncio.Lock] = {}

    # ---------- infraestructura ----------
    async def cog_load(self):
        await self.run(gdb.init_schema)
        self.bot.add_view(DiceJoinView(self))
        self.bot.add_view(CoinAcceptView(self))
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
        e = self.economy
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    @tasks.loop(seconds=5)
    async def ticker(self):
        try:
            await self.process_due()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def process_due(self, now: Optional[float] = None, rng=None) -> int:
        """Resuelve y anuncia los juegos vencidos. Devuelve cuántos resolvió."""
        done = 0
        for gid in await self.run(gdb.due_games, now):
            res = await self.run(gdb.resolve_game, gid, now, rng)
            if res is not None:
                done += 1
                await self.announce(res)
        return done

    # ---------- permisos de creación (canal + rol, como el resto de la economía) ----------
    async def _authorize_create(self, ctx, command: str) -> bool:
        from economy import is_admin_member
        member = ctx.author
        if ctx.guild is None or not isinstance(member, discord.Member):
            return False
        channel_id = await self.run(eco.get_command_channel, command)
        roles = set(await self.run(eco.get_command_roles, command))
        result = eco.authorize(command=command, is_admin=is_admin_member(member), member_role_ids={r.id for r in member.roles},
                               allowed_role_ids=roles, channel_id=ctx.channel.id, configured_channel_id=channel_id)
        if not result.ok:
            await ctx.reply(f"❌ {result.reason}", mention_author=False, allowed_mentions=NO_MENTIONS)
        return result.ok

    # ---------- embeds ----------
    def _players_text(self, game: gdb.Game) -> str:
        shown = [f"<@{u}>" for u in game.players[:MAX_LISTED]]
        text = "\n".join(f"`{i}.` {m}" for i, m in enumerate(shown, 1))
        if len(game.players) > MAX_LISTED:
            text += f"\n… y {len(game.players) - MAX_LISTED} más"
        return text or "—"

    def panel_embed(self, game: gdb.Game) -> discord.Embed:
        ends = int(game.ends_at)
        if game.kind == "dice":
            emb = discord.Embed(title=f"🎲 Dados — entrada de {eco.format_amount(game.stake)}", color=GOLD)
            emb.description = (
                f"Tocá **Anotarse** para entrar pagando **{self.money(game.stake)}**.\n"
                "Cada participante tira un dado de 6 caras: **gana el número más alto** y se lleva **todo el pozo**. "
                "Si hay empate arriba, los empatados vuelven a tirar hasta que quede uno.\n"
                "⚠️ **No te podés dar de baja** una vez anotado.")
            emb.add_field(name="💰 Pozo", value=self.money(game.pool), inline=True)
            emb.add_field(name="⏳ Sale el resultado", value=f"<t:{ends}:R> (<t:{ends}:T>)", inline=True)
            emb.add_field(name=f"👥 Anotados ({len(game.players)})", value=self._players_text(game), inline=False)
        else:
            emb = discord.Embed(title=f"🪙 Coinflip — apuesta de {eco.format_amount(game.stake)}", color=GOLD)
            emb.description = (
                f"<@{game.creator_id}> apostó **{self.money(game.stake)}** (**cara**). "
                f"Quien acepte pone lo mismo (**cruz**), se tira la moneda y **el que sale se queda con todo**.\n"
                "Solo puede aceptar **una** persona.")
            emb.add_field(name="💰 Pozo si aceptan", value=self.money(game.stake * 2), inline=True)
            emb.add_field(name="⏳ Vence", value=f"<t:{ends}:R> (<t:{ends}:T>)", inline=True)
        emb.set_footer(text=f"Juego #{game.id}")
        return emb

    def result_embed(self, res: gdb.ResolveResult) -> discord.Embed:
        g = res.game
        kind_title = "🎲 Dados" if g.kind == "dice" else "🪙 Coinflip"
        if res.status == "refunded":
            why = "No se anotó nadie más" if g.kind == "dice" else "Nadie aceptó el desafío"
            emb = discord.Embed(title=f"{kind_title} — sin rival", color=GREY)
            emb.description = f"{why}: se devolvió la entrada de {self.money(g.stake)} a {len(g.players)} persona(s)."
            emb.set_footer(text=f"Juego #{g.id}")
            return emb
        emb = discord.Embed(title=f"{kind_title} — resultado", color=GREEN)
        lines: List[str] = []
        if g.kind == "dice":
            for n, rnd in enumerate(res.rounds, 1):
                title = "Tiradas" if len(res.rounds) == 1 else (f"Ronda {n}" if n == 1 else f"Desempate {n - 1}")
                body = " · ".join(f"<@{u}> {DICE_FACES[r]}" for u, r in rnd[:MAX_LISTED])
                if len(rnd) > MAX_LISTED:
                    body += f" … y {len(rnd) - MAX_LISTED} más"
                top = max(r for _, r in rnd)
                tied = [u for u, r in rnd if r == top]
                suffix = f"\n→ Empate en {DICE_FACES[top]} entre {len(tied)}: vuelven a tirar." if len(tied) > 1 and n < len(res.rounds) else ""
                lines.append(f"**{title}**\n{body}{suffix}")
            if res.note:
                lines.append(f"_{res.note}_")
        else:
            loser = g.players[1] if res.winner_id == g.players[0] else g.players[0]
            lines.append(f"<@{g.players[0]}> (cara) vs <@{g.players[1]}> (cruz)\nLa moneda cayó en **{res.coin_side}**.")
        lines.append(f"\n🏆 **<@{res.winner_id}>** se lleva **{self.money(res.pool)}**.")
        emb.description = "\n\n".join(lines)[:4000]
        emb.set_footer(text=f"Juego #{g.id}")
        return emb

    # ---------- comandos: crear ----------
    @staticmethod
    async def _private_error(ctx, text: str):
        """Error que solo ve quien usó el comando. Con `/` es un mensaje efímero; con `!` Discord no permite mensajes
        privados, así que se borra el comando y la respuesta a los pocos segundos para no ensuciar el canal."""
        if hasattr(ctx, "interaction"):
            return await ctx.reply(text)
        try:
            await ctx.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS, delete_after=ERROR_VISIBLE_SECONDS)
        finally:
            try:
                await ctx.message.delete()
            except Exception:
                pass

    async def _create(self, ctx, kind: str, amount_text: str):
        command = gdb.COMMAND_OF[kind]
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        if not await self._authorize_create(ctx, command):
            return
        blocked = await self.run(cdb.block_reason, ctx.guild.id, ctx.author.id)
        if blocked:
            return await self._private_error(ctx, blocked)
        if not (amount_text or "").strip():
            return await ctx.reply(f"Uso: `!{command} [cantidad]` (máximo {eco.format_amount(gdb.MAX_STAKE)}).",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        try:
            amount = gdb.validate_stake(eco.parse_amount(amount_text.strip()))
        except (eco.AmountError, gdb.GameError) as exc:
            return await ctx.reply(f"❌ {exc}", mention_author=False, allowed_mentions=NO_MENTIONS)
        panel_channel = ctx.channel
        if kind == "dice":                                  # los dados se publican en un canal aparte (/canal dados)
            cid = await self.run(gdb.get_panel_channel_id, kind)
            if not cid:
                return await ctx.reply("❌ Todavía no hay un canal para los dados. Un administrador debe usar `/canal dados`.",
                                       mention_author=False, allowed_mentions=NO_MENTIONS)
            panel_channel = await self._get_channel(cid)
            if panel_channel is None:
                return await ctx.reply("❌ No encuentro el canal configurado para los dados. Un administrador debe volver a usar `/canal dados`.",
                                       mention_author=False, allowed_mentions=NO_MENTIONS)
        created = await self.run(gdb.create_game, kind=kind, guild_id=ctx.guild.id, channel_id=panel_channel.id,
                                 creator_id=ctx.author.id, stake=amount)
        if not created.ok:
            if created.code == "live":
                g = created.game
                where = f" en <#{g.channel_id}>" if g and g.channel_id else ""
                what = "unos dados" if kind == "dice" else "un coinflip"
                return await self._private_error(
                    ctx, f"❌ Ya hay {what} en curso{where} (termina <t:{int(g.ends_at)}:R>). Esperá a que termine para crear otro.")
            return await ctx.reply(f"❌ No tenés saldo suficiente. Tu saldo: {self.money(created.balance)}.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        game = created.game
        view = DiceJoinView(self) if kind == "dice" else CoinAcceptView(self)
        try:
            msg = await panel_channel.send(embed=self.panel_embed(game), view=view, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self.run(gdb.abort_unpublished, game.id)
            return await ctx.reply("❌ No pude publicar el panel (¿permisos del bot en ese canal?). Se te devolvió la entrada.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        await self.run(gdb.set_message, game.id, msg.id)
        if hasattr(ctx, "interaction"):                     # con "/" hay que responder la interacción (en privado)
            await ctx.reply(f"✅ Listo: el panel está en {panel_channel.mention}.", ephemeral=True)
        elif panel_channel.id != ctx.channel.id:
            await ctx.reply(f"✅ Dados creados: el panel está en {panel_channel.mention}.", mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="dados")
    @guard
    async def dados(self, ctx: commands.Context, *, cantidad: str = ""):
        await self._create(ctx, "dice", cantidad)

    @commands.command(name="coinflip")
    @guard
    async def coinflip(self, ctx: commands.Context, *, cantidad: str = ""):
        await self._create(ctx, "coinflip", cantidad)

    @commands.command(name="winrate")
    @guard
    async def winrate(self, ctx: commands.Context, *, objetivo: str = ""):
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        target = ctx.author
        if objetivo.strip():
            mentioned = [m for m in getattr(ctx.message, "mentions", []) if not getattr(m, "bot", False)]
            if mentioned:
                target = mentioned[0]
            else:
                eco_cog = self.bot.get_cog("EconomyCog")
                found, error = (await eco_cog._resolve_member_flexible(ctx.guild, objetivo)) if eco_cog else (None, "No encuentro a esa persona.")
                if found is None:
                    return await ctx.reply(f"❌ {error}", mention_author=False, allowed_mentions=NO_MENTIONS)
                target = found
        dice = await self.run(stats_db.get_winrate, "dice", target.id)
        coin = await self.run(stats_db.get_winrate, "coinflip", target.id)
        emb = discord.Embed(title=f"📊 Winrate de {getattr(target, 'display_name', target.id)}", color=GREEN)
        emb.description = f"<@{target.id}>"
        for icon, name, w in (("🎲", "Dados", dice), ("🪙", "Coinflip", coin)):
            emb.add_field(name=f"{icon} {name}", value=f"**{w.percent}**\n{w.wins} ganadas de {w.played} partidas", inline=True)
        emb.set_footer(text="Solo cuentan partidas terminadas con rival (las devueltas no suman).")
        try:
            emb.set_thumbnail(url=target.display_avatar.url)
        except Exception:
            pass
        await ctx.send(embed=emb, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="winrate", description="Mirá el winrate en dados y coinflip de una persona (por defecto, el tuyo)")
    @app_commands.describe(usuario="Persona a consultar")
    @app_commands.guild_only()
    async def winrate_slash(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        ctx = SlashContext(interaction)
        if usuario is not None:
            ctx.message.mentions = [usuario]
        await self.winrate.callback(self, ctx, objetivo=usuario.mention if usuario else "")

    @app_commands.command(name="dados", description="Crea una partida de dados (5 minutos). Gana el número más alto y se lleva el pozo")
    @app_commands.describe(cantidad=f"Lo que ponés para entrar (máximo {eco.format_amount(gdb.MAX_STAKE)})")
    @app_commands.guild_only()
    async def dados_slash(self, interaction: discord.Interaction, cantidad: str):
        await self.dados.callback(self, SlashContext(interaction), cantidad=cantidad)

    @app_commands.command(name="coinflip", description="Desafía a alguien a cara o cruz (dura 5 minutos). El que sale se queda con todo")
    @app_commands.describe(cantidad=f"Lo que apostás (máximo {eco.format_amount(gdb.MAX_STAKE)})")
    @app_commands.guild_only()
    async def coinflip_slash(self, interaction: discord.Interaction, cantidad: str):
        await self.coinflip.callback(self, SlashContext(interaction), cantidad=cantidad)

    # ---------- botones: anotarse / aceptar ----------
    @staticmethod
    async def _say(interaction: discord.Interaction, text: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    def _join_failure(self, status: str, game: Optional[gdb.Game], balance: int = 0) -> Optional[str]:
        """Texto para los casos en que no se puede entrar (None = se puede)."""
        if status == "notfound":
            return "❌ No encuentro ese juego."
        if status == "closed":
            return "⏱️ Este juego ya terminó."
        if status == "already":
            return "ℹ️ Ya estás anotado. No hace falta (ni se puede) repetirlo."
        if status == "own":
            return "❌ No podés aceptar tu propio coinflip."
        if status == "full":
            return "❌ Otra persona ya aceptó este coinflip."
        if status == "funds":
            return f"❌ No tenés saldo suficiente (entrada: {self.money(game.stake) if game else '?'}). Tu saldo: {self.money(balance)}."
        return None

    async def on_join(self, interaction: discord.Interaction):
        """Un solo toque: cobra la entrada y anota (o acepta el coinflip) en una sola transacción."""
        try:
            msg = getattr(interaction, "message", None)
            game = await self.run(gdb.find_by_message, msg.id) if msg is not None else None
            if game is None:
                return await self._say(interaction, "❌ No encuentro ese juego.")
            if getattr(interaction.user, "bot", False) or interaction.guild is None:
                return
            blocked = await self.run(cdb.block_reason, interaction.guild.id, interaction.user.id, check_open=False)   # anotarse sigue permitido con el casino cerrado
            if blocked:
                return await self._say(interaction, blocked)
            res = await self.run(gdb.join_game, game.id, interaction.user.id)
            problem = self._join_failure(res.status, res.game or game, res.balance)
            if problem:
                return await self._say(interaction, problem)
            g = res.game
            if res.resolved is not None:                                     # coinflip: se resuelve al aceptar
                await self._say(interaction, f"✅ Aceptaste el desafío y pusiste {self.money(g.stake)}. ¡Se tira la moneda!")
                return await self.announce(res.resolved)
            await self._say(interaction, f"✅ Te anotaste pagando {self.money(g.stake)}. Tu saldo: {self.money(res.balance)}. No te podés dar de baja.")
            await self.refresh_panel(g)
        except Exception:
            traceback.print_exc()
            await self._say(interaction, "❌ Ocurrió un error. Revisá tu saldo con `!bal` antes de reintentar.")

    # ---------- paneles y anuncios ----------
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

    async def refresh_panel(self, game: gdb.Game):
        lock = self._locks.setdefault(game.id, asyncio.Lock())
        async with lock:
            try:
                fresh = await self.run(gdb.get_game, game.id)
                if fresh is None or not fresh.is_open:
                    return
                ch = await self._get_channel(fresh.channel_id)
                if ch is None or fresh.message_id is None:
                    return
                msg = await ch.fetch_message(fresh.message_id)
                await msg.edit(embed=self.panel_embed(fresh), allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Juegos: no pude actualizar el panel #{game.id}: {exc!r}")

    async def announce(self, res: gdb.ResolveResult):
        """Cierra el panel (sin botones) y publica el resultado. Mejor esfuerzo: el dinero ya está resuelto."""
        g = res.game
        lock = self._locks.setdefault(g.id, asyncio.Lock())
        async with lock:
            ch = await self._get_channel(g.channel_id)
            if ch is None:
                return
            try:
                if g.message_id:
                    msg = await ch.fetch_message(g.message_id)
                    done = self.panel_embed(g)
                    done.title = (done.title or "") + " — finalizado"
                    done.color = GREY
                    await msg.edit(embed=done, view=None, allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"ℹ️ Juegos: no pude cerrar el panel #{g.id}: {exc!r}")
            try:
                mentions = discord.AllowedMentions(users=[discord.Object(id=res.winner_id)]) if res.winner_id else NO_MENTIONS
                await ch.send(embed=self.result_embed(res), allowed_mentions=mentions)
            except Exception as exc:
                print(f"⚠️ Juegos: no pude publicar el resultado #{g.id}: {exc!r}")
        self._locks.pop(g.id, None)


async def setup_games(bot: commands.Bot, db_path: str, guild_id: int) -> GamesCog:
    cog = GamesCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
