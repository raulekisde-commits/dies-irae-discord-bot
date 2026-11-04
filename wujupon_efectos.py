"""Efectos automáticos de las cartas de Wujupón — capa de Discord.

- Al canjear una carta con efecto, si afecta a otra persona se elige el objetivo con un selector (privado).
- Mutes por rol: «Muteado chat» (sin escribir) y «Muteado voice» (sin hablar). Los crea /wujupon-efectos roles-mute.
- Un loop saca los roles cuando vencen; al iniciar, se reconcilian roles y efectos (sobrevive reinicios).
- Cada efecto sobre otra persona se anuncia en el canal de /wujupon-efectos canal y le llega un MD al afectado.
"""
import asyncio
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import casino_db
import deposits_db
import efectos_db as edb
import wujupon_db as wdb
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
ROLE_NAMES = {"mute_chat": "Muteado chat", "mute_voice": "Muteado voice"}
ROLE_SETTING = {"mute_chat": "mute_chat_role", "mute_voice": "mute_voice_role"}


def is_admin(member) -> bool:
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if guild.owner_id == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def _say(interaction: discord.Interaction, content: str = None, **kw):
    kw.setdefault("allowed_mentions", NO_MENTIONS)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, **kw)
        else:
            await interaction.response.send_message(content, ephemeral=True, **kw)
    except Exception:
        traceback.print_exc()


def _dur(minutes: Optional[float]) -> str:
    if not minutes:
        return ""
    m = int(minutes)
    if m % 1440 == 0:
        return f"{m // 1440} día(s)" if m // 1440 > 1 else "24 h"
    if m % 60 == 0:
        return f"{m // 60} h"
    return f"{m} min"


class TargetView(discord.ui.View):
    """Selector privado del objetivo de una carta."""

    def __init__(self, cog: "EfectosCog", owner_id: int, card_id: int):
        super().__init__(timeout=120)
        self.cog, self.owner_id, self.card_id = cog, owner_id, card_id
        sel = discord.ui.UserSelect(placeholder="Elegí a quién se la usás", min_values=1, max_values=1)
        sel.callback = lambda inter, s=sel: self.cog.use_on_target(inter, self.card_id, s.values[0] if s.values else None)
        self.add_item(sel)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Esto no es tuyo.")
            return False
        return True


class EfectosCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._tasks: set = set()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    async def cog_load(self):
        await self.run(edb.init_schema)
        self.expire_loop.start()
        self._spawn(self._after_ready())

    async def cog_unload(self):
        self.expire_loop.cancel()
        for t in list(self._tasks):
            t.cancel()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------- utilidades de Discord ----------------
    def guild(self) -> Optional[discord.Guild]:
        getter = getattr(self.bot, "get_guild", None)
        g = getter(self.guild_id) if getter else None
        return g or (getattr(self.bot, "guilds_map", {}) or {}).get(self.guild_id)

    async def _member(self, user_id: int):
        g = self.guild()
        if g is None:
            return None
        m = g.get_member(user_id)
        if m is None:
            try:
                m = await g.fetch_member(user_id)
            except Exception:
                return None
        return m

    async def _role(self, kind: str):
        rid = await self.run(wdb.get_setting, ROLE_SETTING[kind])
        g = self.guild()
        return g.get_role(int(rid)) if (rid and g) else None

    async def _set_role(self, user_id: int, kind: str, on: bool) -> bool:
        if kind not in ROLE_SETTING:
            return True
        role = await self._role(kind)
        member = await self._member(user_id)
        if role is None or member is None:
            return False
        try:
            if on:
                await member.add_roles(role, reason="Wujupón: carta")
            else:
                await member.remove_roles(role, reason="Wujupón: venció o se limpió")
            return True
        except Exception:
            traceback.print_exc()
            return False

    async def announce(self, text: str, target_id: Optional[int] = None, dm: Optional[str] = None):
        try:
            cid = await self.run(wdb.get_setting, "efx_channel")
            ch = self.bot.get_channel(int(cid)) if cid else None
            if ch is not None:
                await ch.send(text, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        if target_id and dm:
            try:
                user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
                await user.send(dm)
            except Exception:
                pass

    # ---------------- usar una carta ----------------
    async def start_use(self, interaction: discord.Interaction, card: "wdb.Card", prize: "wdb.Prize"):
        action, value = prize.effect
        if action in ("casino_open", "casino_close"):
            return await self._casino(interaction, card, prize, action == "casino_open")
        if action in edb.TARGETED:
            if action in ROLE_SETTING and await self._role(action) is None:
                return await _say(interaction, "❌ Todavía no están configurados los roles de mute (`/wujupon-efectos roles-mute`). "
                                               "Tu carta no se gastó.")
            return await _say(interaction, f"🎯 **{prize.name}**: elegí a quién se la usás.", view=TargetView(self, interaction.user.id, card.id))
        # cartas propias: escudo y limpiezas
        res = await self.run(edb.use_card, card_id=card.id, owner_id=interaction.user.id, prize_key=prize.key, action=action,
                             minutes=value, target_id=interaction.user.id)
        if res.status != "ok":
            return await _say(interaction, f"❌ {res.message}")
        for e in res.ended:
            await self._set_role(e.user_id, e.kind, False)
        if action == "shield":
            await _say(interaction, f"🛡️ Escudo activo hasta <t:{int(res.effect.until)}:t>: nadie te puede usar cartas y las malas no te afectan.")
            return await self.announce(f"🛡️ <@{interaction.user.id}> activó un escudo hasta <t:{int(res.effect.until)}:t>.")
        await _say(interaction, "🧹 Listo, se limpió: " + ", ".join(edb.KIND_LABELS[e.kind] for e in res.ended) + ".")

    async def use_on_target(self, interaction: discord.Interaction, card_id: int, target):
        if target is None:
            return await _say(interaction, "❌ Elegí a alguien.")
        if getattr(target, "bot", False):
            return await _say(interaction, "❌ No se le pueden usar cartas a un bot. Tu carta no se gastó.")
        card = await self.run(wdb.get_card, card_id)
        if card is None or card.user_id != interaction.user.id or card.status != "available":
            return await _say(interaction, "❌ Esa carta ya no está disponible.")
        prize = await self.run(wdb.get_prize, card.prize_key)
        action, value = prize.effect
        casino_open = None
        if action == "casino_block":
            casino_open = await self.run(casino_db.is_open, self.guild_id)
        res = await self.run(edb.use_card, card_id=card.id, owner_id=interaction.user.id, prize_key=prize.key, action=action,
                             minutes=value, target_id=target.id, casino_open=casino_open)
        if res.status != "ok":
            return await _say(interaction, f"❌ {res.message}")
        actor = f"<@{interaction.user.id}>"
        if action == "purge":
            await _say(interaction, f"💥 Le rompiste el escudo a {target.mention}.")
            return await self.announce(f"💥 {actor} le rompió el escudo a {target.mention}.", target.id,
                                       f"💥 {interaction.user.display_name} te rompió el escudo de Wujupón.")
        eff = res.effect
        if action in ROLE_SETTING and not await self._set_role(target.id, action, True):
            await self.run(edb.undo_use, card.id, eff.id, interaction.user.id)
            return await _say(interaction, "❌ No pude aplicar el mute (revisá los roles y mis permisos). Tu carta no se gastó.")
        until = f"hasta <t:{int(eff.until)}:t>" if eff.until else "hasta que el casino se cierre y se vuelva a abrir"
        label = edb.KIND_LABELS[action]
        await _say(interaction, f"✅ **{prize.name}** aplicada a {target.mention} ({until}).")
        await self.announce(f"{label}: {actor} se la usó a {target.mention} {until}.", target.id,
                            f"{label} por una carta de Wujupón de {interaction.user.display_name}, {until}.")

    async def _casino(self, interaction, card, prize, open_: bool):
        if await self.run(casino_db.is_open, self.guild_id) == open_:
            return await _say(interaction, f"ℹ️ El casino ya está {'abierto' if open_ else 'cerrado'}. Tu carta no se gastó.")
        if not await self.run(edb.consume_simple, card_id=card.id, owner_id=interaction.user.id, prize_key=prize.key,
                              note="Abrió el casino" if open_ else "Cerró el casino"):
            return await _say(interaction, "❌ Esa carta ya no está disponible.")
        await self.run(casino_db.set_open, self.guild_id, open_, interaction.user.id)
        txt = "🎰 Abriste el casino." if open_ else "🔒 Cerraste el casino."
        await _say(interaction, txt)
        await self.announce(f"{'🎰' if open_ else '🔒'} <@{interaction.user.id}> {'abrió' if open_ else 'cerró'} el casino con una carta de Wujupón.")

    # ---------------- cartas malas ----------------
    async def apply_forced(self, card_id: int, user_id: int, prize: "wdb.Prize") -> str:
        """Aplica una carta mala recién salida. Devuelve el texto para el jugador."""
        action, value = prize.effect
        minutes = value if action in ("mute_chat", "mute_voice") else None
        amount = int(value or 0) if action in ("wuju", "embo", "cooldown") else 0
        res = await self.run(edb.apply_forced, card_id=card_id, user_id=user_id, prize_key=prize.key, action=action,
                             amount=amount, minutes=minutes)
        if res.status == "gone":
            return ""
        if res.status == "blocked":
            await self.announce(f"🛡️ La carta mala **{prize.name}** de <@{user_id}> fue bloqueada.")
            return res.message
        if res.effect is not None and not await self._set_role(user_id, res.effect.kind, True):
            print(f"⚠️ Wujupón: no pude aplicar {res.effect.kind} a {user_id} (¿faltan los roles de mute?)")
        if res.cooldown_hours:
            await asyncio.to_thread(deposits_db.add_sell_cooldown, self.db_path, self.guild_id, user_id, res.cooldown_hours)
        await self.announce(f"☠️ <@{user_id}> sacó **{prize.name}**: {res.message}")
        return f"☠️ {res.message}"

    # ---------------- vencimientos y reconciliación ----------------
    @tasks.loop(seconds=30)
    async def expire_loop(self):
        try:
            for e in await self.run(edb.expire_due):
                await self._set_role(e.user_id, e.kind, False)
        except Exception:
            traceback.print_exc()

    async def _after_ready(self):
        try:
            wait = getattr(self.bot, "wait_until_ready", None)
            if wait is not None:
                await wait()
            await self.reconcile()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    async def reconcile(self):
        """Roles de mute = efectos activos; cartas malas que quedaron sin aplicar se aplican ahora."""
        active = await self.run(edb.list_active)
        for kind in ROLE_SETTING:
            role = await self._role(kind)
            if role is None:
                continue
            want = {e.user_id for e in active if e.kind == kind}
            for m in list(getattr(role, "members", []) or []):
                if m.id not in want:
                    await self._set_role(m.id, kind, False)
            have = {m.id for m in getattr(role, "members", []) or []}
            for uid in want - have:
                await self._set_role(uid, kind, True)
        for card_id, user_id, key in await self.run(edb.pending_forced_cards):
            prize = await self.run(wdb.get_prize, key)
            if prize is not None and prize.effect:
                await self.apply_forced(card_id, user_id, prize)

    # ============================================================
    # /wujupon-efectos
    # ============================================================
    efx = app_commands.Group(name="wujupon-efectos", description="Efectos de las cartas de Wujupón (mutes, congelamientos, inmunes)",
                             guild_only=True)
    inmunes = app_commands.Group(name="inmunes", description="Lista de inmunes a las cartas", parent=efx)

    async def _council(self, interaction) -> bool:
        if not can_manage(interaction.user):
            await _say(interaction, "❌ Solo el Council o administradores.")
            return False
        return True

    async def _admin(self, interaction) -> bool:
        if not is_admin(interaction.user):
            await _say(interaction, "❌ Solo el dueño del servidor o un administrador.")
            return False
        return True

    @efx.command(name="ver", description="(Council) Efectos activos (de todos o de una persona)")
    async def ver(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if not await self._council(interaction):
            return
        rows = await self.run(edb.list_active, usuario.id if usuario else None)
        lines = [f"<@{e.user_id}> · {edb.KIND_LABELS[e.kind]} · " + (f"hasta <t:{int(e.until)}:t>" if e.until else "hasta la reapertura del casino")
                 for e in rows][:40]
        await _say(interaction, embed=discord.Embed(title="✨ Efectos activos", color=0x9B59B6,
                                                    description="\n".join(lines) or "No hay efectos activos."))

    @efx.command(name="quitar", description="(Council) Corta todos los efectos activos de una persona")
    async def quitar(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._council(interaction):
            return
        ended = await self.run(edb.end_all, usuario.id, interaction.user.id)
        for e in ended:
            await self._set_role(e.user_id, e.kind, False)
        await _say(interaction, f"✅ Se cortaron {len(ended)} efecto(s) de {usuario.mention}." if ended
                   else f"ℹ️ {usuario.mention} no tenía efectos activos.")

    @efx.command(name="canal", description="(Council) Canal donde se anuncian los efectos que se usan contra otros")
    async def canal(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        if not await self._council(interaction):
            return
        target = canal or interaction.channel
        await self.run(wdb.set_setting, "efx_channel", str(target.id))
        await _say(interaction, f"📣 Los efectos se anuncian en {target.mention}.")

    @efx.command(name="roles-mute", description="(Admin) Crea los roles «Muteado chat» y «Muteado voice» y los aplica en todos los canales")
    async def roles_mute(self, interaction: discord.Interaction):
        if not await self._admin(interaction):
            return
        g = interaction.guild
        await interaction.response.defer(ephemeral=True)
        roles = {}
        try:
            for kind, name in ROLE_NAMES.items():
                role = await self._role(kind)
                if role is None:
                    role = await g.create_role(name=name, reason="Wujupón: mutes por carta")
                    await self.run(wdb.set_setting, ROLE_SETTING[kind], str(role.id))
                roles[kind] = role
        except Exception as exc:
            return await interaction.followup.send(f"❌ No pude crear los roles (¿tengo permiso de **Gestionar roles**?): {exc}", ephemeral=True)
        chat_deny = discord.PermissionOverwrite(send_messages=False, add_reactions=False, send_messages_in_threads=False,
                                                create_public_threads=False, create_private_threads=False)
        voice_deny = discord.PermissionOverwrite(speak=False)
        ok = fail = 0
        for ch in list(getattr(g, "channels", []) or []):
            try:
                await ch.set_permissions(roles["mute_chat"], overwrite=chat_deny, reason="Wujupón: mute de chat")
                if isinstance(ch, (discord.VoiceChannel, discord.CategoryChannel)):
                    await ch.set_permissions(roles["mute_voice"], overwrite=voice_deny, reason="Wujupón: mute de voice")
                ok += 1
            except Exception:
                fail += 1
        await interaction.followup.send(f"✅ Roles listos: {roles['mute_chat'].mention} y {roles['mute_voice'].mention}. "
                                        f"Permisos aplicados en {ok} canal(es)" + (f"; fallaron {fail} (revisá mis permisos)." if fail else ".")
                                        + "\nSi creás canales nuevos, volvé a usar este comando.", ephemeral=True)

    @inmunes.command(name="agregar", description="(Admin) Agrega a alguien a la lista de inmunes a las cartas")
    async def inm_add(self, interaction: discord.Interaction, usuario: discord.Member, motivo: Optional[str] = None):
        if not await self._admin(interaction):
            return
        done = await self.run(edb.set_immune, usuario.id, interaction.user.id, motivo or "", True)
        await _say(interaction, f"🛡️ {usuario.mention} ahora es inmune a las cartas." if done else f"ℹ️ {usuario.mention} ya era inmune.")

    @inmunes.command(name="quitar", description="(Admin) Saca a alguien de la lista de inmunes")
    async def inm_remove(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._admin(interaction):
            return
        done = await self.run(edb.set_immune, usuario.id, interaction.user.id, "", False)
        await _say(interaction, f"✅ {usuario.mention} ya no es inmune." if done else f"ℹ️ {usuario.mention} no estaba en la lista.")

    @inmunes.command(name="ver", description="(Council) Lista de inmunes a las cartas")
    async def inm_list(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        rows = await self.run(edb.list_immune)
        await _say(interaction, embed=discord.Embed(title="🛡️ Inmunes a las cartas", color=0x3498DB,
                                                    description="\n".join(f"<@{u}>" + (f" · {r}" if r else "") for u, r in rows)
                                                    or "No hay nadie en la lista."))


async def setup_efectos(bot: commands.Bot, db_path: str, guild_id: int) -> EfectosCog:
    cog = EfectosCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
