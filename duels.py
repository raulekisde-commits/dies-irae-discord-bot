"""/1v1 usuario cantidad — duelo entre dos personas (capa de Discord)."""
import asyncio
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import casino_db as cdb
import duels_db as ddb
import economy_db as eco

NO_MENTIONS = discord.AllowedMentions.none()
GOLD, GREY, GREEN, RED = 0xF1C40F, 0x95A5A6, 0x2ECC71, 0xE74C3C


async def _say(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class DuelView(discord.ui.View):
    def __init__(self, cog: "DuelsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Aceptar", style=discord.ButtonStyle.success, custom_id="duel:accept", emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button):
        await self.cog.on_accept(interaction)

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, custom_id="duel:decline", emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button):
        await self.cog.on_decline(interaction)


class DuelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await self.run(ddb.init_schema)
        self.bot.add_view(DuelView(self))
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

    async def _get_channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    # ---------- embeds ----------
    def panel_embed(self, d: ddb.Duel) -> discord.Embed:
        emb = discord.Embed(title="⚔️ Duelo 1v1", color=GOLD)
        emb.description = (f"<@{d.challenger_id}> retó a <@{d.challenged_id}> por **{self.money(d.stake)}**.\n"
                           f"<@{d.challenger_id}> es **cara** y <@{d.challenged_id}> es **cruz**: se tira la moneda y **el que sale se queda con todo**.")
        emb.add_field(name="💰 Pozo si acepta", value=self.money(d.stake * 2), inline=True)
        emb.add_field(name="⏳ Vence", value=f"<t:{int(d.ends_at)}:R> (<t:{int(d.ends_at)}:T>)", inline=True)
        emb.set_footer(text=f"Duelo #{d.id} · solo {'la persona retada'} puede responder")
        return emb

    def closed_embed(self, d: ddb.Duel, text: str, color: int) -> discord.Embed:
        emb = discord.Embed(title=f"⚔️ Duelo 1v1 — {text}", color=color)
        emb.description = f"<@{d.challenger_id}> vs <@{d.challenged_id}> por **{self.money(d.stake)}**."
        emb.set_footer(text=f"Duelo #{d.id}")
        return emb

    # ---------- /1v1 ----------
    @app_commands.command(name="1v1", description="Retá a alguien a un duelo de cara o cruz: el que sale se queda con todo")
    @app_commands.describe(usuario="A quién retás", cantidad="Lo que apostás (sin tope: hasta tu balance total)")
    @app_commands.guild_only()
    async def duel(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: str):
        try:
            me = interaction.user
            if interaction.guild is None or not isinstance(me, discord.Member):
                return await _say(interaction, "❌ Solo disponible en el servidor.")
            eco_cog = self.economy
            if eco_cog is not None:
                from economy import is_admin_member
                channel_id = await self.run(eco.get_command_channel, "1v1")
                roles = set(await self.run(eco.get_command_roles, "1v1"))
                roles.add(eco_cog.member_role_id)               # los miembros pueden usarlo (como hasta ahora); el canal se elige con /perms-canal
                result = eco.authorize(command="1v1", is_admin=is_admin_member(me), member_role_ids={r.id for r in me.roles},
                                       allowed_role_ids=roles, channel_id=interaction.channel_id, configured_channel_id=channel_id)
                if not result.ok:
                    return await _say(interaction, f"❌ {result.reason}")
            if usuario.bot:
                return await _say(interaction, "❌ No podés retar a un bot.")
            if usuario.id == me.id:
                return await _say(interaction, "❌ No podés retarte a vos mismo.")
            blocked = await self.run(cdb.block_reason, interaction.guild.id, me.id, usuario.id)
            if blocked:
                return await _say(interaction, blocked)
            try:
                amount = eco.parse_amount(cantidad)
            except eco.AmountError as exc:
                return await _say(interaction, f"❌ {exc}")
            over = await self.run(eco.check_bet_limit, "1v1", amount)
            if over:
                return await _say(interaction, f"❌ {over}")
            res = await self.run(ddb.create_duel, guild_id=interaction.guild.id, channel_id=interaction.channel.id,
                                 challenger_id=me.id, challenged_id=usuario.id, stake=amount)
            if res.code == "funds":
                return await _say(interaction, f"❌ No tenés saldo suficiente. Tu saldo: {self.money(res.balance)}.")
            if res.code == "target_funds":
                return await _say(interaction, f"❌ {usuario.mention} no tiene saldo suficiente para esa apuesta ({self.money(res.balance)}). No se creó el duelo.")
            duel = res.duel
            try:
                await interaction.response.send_message(f"{usuario.mention}, {me.mention} te retó a un duelo.", embed=self.panel_embed(duel),
                                                        view=DuelView(self), allowed_mentions=discord.AllowedMentions(users=[usuario]))
                msg = await interaction.original_response()
            except Exception:
                traceback.print_exc()
                await self.run(ddb.abort_unpublished, duel.id)
                return await _say(interaction, "❌ No pude publicar el duelo. Se te devolvió la apuesta.")
            await self.run(ddb.set_message, duel.id, msg.id)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ Ocurrió un error. Revisá tu saldo con `/bal` antes de reintentar.")

    # ---------- botones ----------
    async def _duel_of(self, interaction: discord.Interaction) -> Optional[ddb.Duel]:
        msg = getattr(interaction, "message", None)
        return await self.run(ddb.find_by_message, msg.id) if msg is not None else None

    async def on_accept(self, interaction: discord.Interaction):
        try:
            d = await self._duel_of(interaction)
            if d is None:
                return await _say(interaction, "❌ No encuentro ese duelo.")
            if d.is_open:
                blocked = await self.run(cdb.block_reason, d.guild_id, d.challenger_id, d.challenged_id, check_open=False)   # con el casino cerrado se puede aceptar un duelo ya creado
                if blocked and interaction.user.id == d.challenged_id:
                    return await _say(interaction, blocked)
            res = await self.run(ddb.accept_duel, d.id, interaction.user.id)
            if res.status == "notyou":
                return await _say(interaction, "❌ Este duelo no es para vos: solo la persona retada puede responder.")
            if res.status == "closed":
                return await _say(interaction, "⏱️ Este duelo ya terminó.")
            if res.status == "funds":
                return await _say(interaction, f"❌ No tenés saldo suficiente para aceptar ({self.money(d.stake)}). Tu saldo: {self.money(res.balance)}.")
            if res.status != "ok":
                return await _say(interaction, "❌ No encuentro ese duelo.")
            g = res.duel
            emb = discord.Embed(title="⚔️ Duelo 1v1 — resultado", color=GREEN)
            emb.description = (f"<@{g.challenger_id}> (cara) vs <@{g.challenged_id}> (cruz)\nLa moneda cayó en **{res.side}**.\n\n"
                               f"🏆 **<@{res.winner_id}>** se lleva **{self.money(res.pool)}**.")
            emb.set_footer(text=f"Duelo #{g.id}")
            try:
                await interaction.response.edit_message(embed=self.closed_embed(g, "finalizado", GREY), view=None)
            except Exception:
                traceback.print_exc()
            ch = interaction.channel
            if ch is not None:
                await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=res.winner_id)]))
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ Ocurrió un error. Revisá tu saldo con `/bal` antes de reintentar.")

    async def on_decline(self, interaction: discord.Interaction):
        try:
            d = await self._duel_of(interaction)
            if d is None:
                return await _say(interaction, "❌ No encuentro ese duelo.")
            res = await self.run(ddb.decline_duel, d.id, interaction.user.id)
            if res.status == "notyou":
                return await _say(interaction, "❌ Este duelo no es para vos: solo la persona retada puede responder.")
            if res.status == "closed":
                return await _say(interaction, "⏱️ Este duelo ya terminó.")
            if res.status != "ok":
                return await _say(interaction, "❌ No encuentro ese duelo.")
            await interaction.response.edit_message(embed=self.closed_embed(res.duel, "rechazado", RED), view=None)
            ch = interaction.channel
            if ch is not None:
                await ch.send(f"✖️ <@{d.challenged_id}> rechazó el duelo de <@{d.challenger_id}>. Se devolvió la apuesta.",
                              allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ Ocurrió un error.")

    # ---------- vencimiento ----------
    async def process_due(self) -> int:
        done = 0
        for duel_id in await self.run(ddb.due_duels):
            d = await self.run(ddb.expire_duel, duel_id)
            if d is None:
                continue
            done += 1
            try:
                ch = await self._get_channel(d.channel_id)
                if ch is not None and d.message_id:
                    msg = await ch.fetch_message(d.message_id)
                    await msg.edit(embed=self.closed_embed(d, "sin respuesta", GREY), view=None)
                    await ch.send(f"⏱️ <@{d.challenged_id}> no respondió el duelo de <@{d.challenger_id}>. Se devolvió la apuesta.",
                                  allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"ℹ️ Duelos: no pude cerrar el panel #{d.id}: {exc!r}")
        return done

    @tasks.loop(seconds=5)
    async def ticker(self):
        try:
            await self.process_due()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()


async def setup_duels(bot: commands.Bot, db_path: str, guild_id: int) -> DuelsCog:
    cog = DuelsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
