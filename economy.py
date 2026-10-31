"""Wuju Coins — capa de Discord (comandos, paneles, permisos).

Toda la lógica de dinero vive en economy_db.py; acá solo se resuelven usuarios/roles,
se valida el contexto y se arman los Embeds.
"""
import asyncio
import functools
import io
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco
from slashctx import SlashContext
from paging import PagedView, chunk
import stats_db
from names import clean as clean_name

NO_MENTIONS = discord.AllowedMentions.none()
EMBED_DESC_BUDGET = 3600          # margen bajo el límite de 4096 caracteres de la descripción
_MENTION_USER_RE = re.compile(r"^<@!?(\d{15,25})>$")
_MENTION_ROLE_RE = re.compile(r"^<@&(\d{15,25})>$")
_ID_RE = re.compile(r"^\d{15,25}$")


# ============================================================
# Helpers puros (probables sin Discord real)
# ============================================================
def chunk_lines(lines: List[str], budget: int = EMBED_DESC_BUDGET) -> List[str]:
    """Agrupa líneas en bloques que no superen `budget` caracteres. Nunca devuelve una lista vacía."""
    pages: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        extra = len(line) + 1
        if current and size + extra > budget:
            pages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        pages.append("\n".join(current))
    return pages or [""]


def is_admin_member(member: discord.abc.User) -> bool:
    """Propietario del servidor o permiso nativo Administrator (se evalúa en cada ejecución)."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if guild.owner_id == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def guard(func):
    """Evita que un error inesperado muestre detalles técnicos al usuario."""
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        try:
            return await func(self, ctx, *args, **kwargs)
        except Exception:
            traceback.print_exc()
            try:
                await ctx.reply(
                    "❌ Ocurrió un error inesperado. Revisá el estado con `!bal` antes de repetir la operación.",
                    mention_author=False, allowed_mentions=NO_MENTIONS,
                )
            except Exception:
                pass
    return wrapper


class ImportConfirmView(discord.ui.View):
    def __init__(self, cog: "EconomyCog", owner_id: int, matched, token: int):
        super().__init__(timeout=300.0)
        self.cog, self.owner_id, self.matched, self.token, self.done = cog, owner_id, matched, token, False
        ok = discord.ui.Button(label="Confirmar y cargar", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_admin_member(interaction.user):
            await interaction.response.send_message("Este mensaje no es tuyo.", ephemeral=True)
            return False
        return True

    async def _ok(self, interaction):
        if self.done:
            return await interaction.response.send_message("Ya fue procesado.", ephemeral=True)
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Cargando saldos…", view=None)
        applied, unchanged, repeated, failed = await self.cog.apply_import(self.owner_id, self.matched, self.token)
        msg = f"✅ Listo: **{applied}** saldo(s) ajustado(s)."
        if unchanged:
            msg += f" {unchanged} ya estaban en ese valor."
        if repeated:
            msg += f" {repeated} ya se habían procesado en esta ejecución."
        if failed:
            msg += f" ⚠️ {failed} fallaron; revisá con `!bal`."
        await interaction.edit_original_response(content=msg)

    async def _no(self, interaction):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelado. No se modificó ningún balance.", view=None)


class ConfirmActionView(discord.ui.View):
    """Confirmación para acciones destructivas: solo la persona que ejecutó el comando, una sola vez."""

    def __init__(self, owner_id: int, on_confirm: Callable, label: str = "Confirmar"):
        super().__init__(timeout=120.0)
        self.owner_id, self.on_confirm, self.done = owner_id, on_confirm, False
        ok = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_admin_member(interaction.user):
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
        await interaction.response.edit_message(content="Cancelado. No se modificó nada.", view=None)


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, member_role_id: int, staff_role_id: Optional[int] = None,
                 guild_id: Optional[int] = None):
        self.bot = bot
        self.guild_id = guild_id
        self.db_path = db_path
        self.member_role_id = member_role_id
        self.staff_role_id = staff_role_id
        # roles que pueden usar el comando sin /perms-comando (además de los configurados y de los admins)
        self.default_roles = {"bal": {member_role_id}, "top": {member_role_id}}
        self._emoji_raw: Optional[str] = None
        self._log_lock = asyncio.Lock()
        self._csv_last: dict = {}

    # ---------------- ciclo de vida ----------------
    async def cog_load(self):
        await self._db(eco.init_schema)
        self._emoji_raw = await self._db(eco.get_setting, "currency_emoji")
        self.retry_pending_logs.start()

    async def cog_unload(self):
        self.retry_pending_logs.cancel()

    async def _db(self, fn: Callable, *args, **kwargs):
        """Las consultas SQLite corren en un hilo para no bloquear al bot."""
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    # ---------------- moneda ----------------
    def icon(self) -> Optional[str]:
        parsed = eco.parse_custom_emoji(self._emoji_raw or "")
        if not parsed:
            return None
        emoji = self.bot.get_emoji(parsed[1])
        return str(emoji) if emoji else None   # None => el emoji ya no está disponible

    def money(self, amount: int, signed: bool = False) -> str:
        text = eco.format_amount(amount)
        if signed and amount > 0:
            text = "+" + text
        icon = self.icon()
        return f"{icon} {text}" if icon else f"{text} {eco.CURRENCY_NAME}"

    def plain(self, amount: int, signed: bool = False) -> str:
        text = eco.format_amount(amount)
        return "+" + text if signed and amount > 0 else text

    # ---------------- resolución de usuarios / roles ----------------
    async def _ensure_members(self, guild: discord.Guild):
        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except Exception:
                pass

    async def _member_by_id(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        return member

    async def _resolve_member_strict(self, guild: discord.Guild, token: str) -> Optional[discord.Member]:
        """Solo mención o ID (para comandos que modifican dinero)."""
        token = token.strip()
        m = _MENTION_USER_RE.match(token)
        if m:
            return await self._member_by_id(guild, int(m.group(1)))
        if _ID_RE.match(token):
            return await self._member_by_id(guild, int(token))
        return None

    async def _resolve_member_flexible(self, guild: discord.Guild, text: str) -> Tuple[Optional[discord.Member], Optional[str]]:
        """Mención, ID o nombre EXACTO (sin coincidencias parciales). Devuelve (miembro, mensaje_de_error)."""
        text = text.strip()
        member = await self._resolve_member_strict(guild, text)
        if member is not None:
            return member, None
        if _MENTION_USER_RE.match(text) or _ID_RE.match(text):
            return None, "No encontré a ese usuario en el servidor."

        await self._ensure_members(guild)
        query = text.casefold()
        found = {}
        for m in guild.members:
            for name in (m.name, m.display_name, m.nick, getattr(m, "global_name", None)):
                if name and name.casefold() == query:
                    found[m.id] = m
                    break
        if len(found) == 1:
            return next(iter(found.values())), None
        if len(found) > 1:
            sample = ", ".join(f"{m.display_name} (`{m.id}`)" for m in list(found.values())[:5])
            return None, f"Hay varios usuarios con ese nombre: {sample}. Usá una mención o el ID para indicar a quién te referís."
        return None, f"No encontré a ningún miembro llamado **{discord.utils.escape_markdown(text)}**. Probá con una mención o con su ID."

    # ---------------- autorización ----------------
    async def _authorize(self, ctx: commands.Context, command: str) -> bool:
        member = ctx.author
        if ctx.guild is None or not isinstance(member, discord.Member):
            return False
        configured_channel = await self._db(eco.get_command_channel, command)
        db_roles = await self._db(eco.get_command_roles, command)
        allowed = set(db_roles) | self.default_roles.get(command, set())
        result = eco.authorize(
            command=command,
            is_admin=is_admin_member(member),
            member_role_ids={r.id for r in member.roles},
            allowed_role_ids=allowed,
            channel_id=ctx.channel.id,
            configured_channel_id=configured_channel,
        )
        if not result.ok:
            await ctx.reply(f"❌ {result.reason}", mention_author=False, allowed_mentions=NO_MENTIONS)
        return result.ok

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member) or not is_admin_member(member):
            await self._ephemeral(interaction, "❌ Este comando es solo para el propietario del servidor o administradores.")
            return False
        return True

    async def has_command_permission(self, member, command: str, *, staff_ok: bool = False) -> bool:
        """Administrador/propietario, o un rol otorgado con /perms-comando (y, si staff_ok, el rol Staff)."""
        if not isinstance(member, discord.Member):
            return False
        if is_admin_member(member):
            return True
        role_ids = {r.id for r in member.roles}
        if staff_ok and self.staff_role_id and self.staff_role_id in role_ids:
            return True
        granted = await self._db(eco.get_command_roles, command)
        return bool(role_ids & set(granted))

    @staticmethod
    async def _ephemeral(interaction: discord.Interaction, content: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            pass

    # ---------------- paneles ----------------
    def build_pages(self, op: eco.OperationResult, *, detail: bool) -> List[discord.Embed]:
        """Embeds de una operación. detail=True => versión de auditoría (balances e IDs)."""
        is_add = op.kind == "add"
        verb = "agregado" if is_add else "retirado"
        color = discord.Color.green() if is_add else discord.Color.red()
        title = ("➕ Wuju Coins agregados" if is_add else "➖ Wuju Coins retirados")
        if detail:
            title = "🧾 " + title
        created = datetime.fromisoformat(op.created_at)
        short_id = op.op_id[:8]

        if op.scope == "user":
            mv = op.movements[0]
            emb = discord.Embed(title=title, color=color, timestamp=created)
            emb.add_field(name="Ejecutado por", value=f"<@{op.actor_id}>", inline=True)
            emb.add_field(name="Destinatario", value=f"<@{mv.user_id}>", inline=True)
            emb.add_field(name=f"Cantidad {verb}", value=f"**{self.money(op.per_user_amount)}**", inline=False)
            if op.reason:
                emb.add_field(name="Motivo", value=op.reason[:1000], inline=False)
            emb.add_field(name="Balance anterior", value=self.money(mv.before), inline=True)
            emb.add_field(name="Balance nuevo", value=f"**{self.money(mv.after)}**", inline=True)
            if detail:
                emb.add_field(name="IDs", value=f"```actor: {op.actor_id}\ndestinatario: {mv.user_id}```", inline=False)
            emb.set_footer(text=f"Operación {short_id}")
            return [emb]

        # operación por rol
        lines = []
        for i, mv in enumerate(op.movements, start=1):
            line = f"`{i}.` <@{mv.user_id}> · **{self.plain(mv.delta, signed=True)}**"
            if detail:
                line += f" ({eco.format_amount(mv.before)} → {eco.format_amount(mv.after)})"
            lines.append(line)
        pages = chunk_lines(lines)

        embeds: List[discord.Embed] = []
        total_pages = len(pages)
        for idx, body in enumerate(pages):
            emb = discord.Embed(
                title=title if idx == 0 else f"{title} (cont.)",
                description=body, color=color, timestamp=created,
            )
            if idx == 0:
                emb.add_field(name="Rol", value=f"<@&{op.role_id}>", inline=True)
                emb.add_field(name=f"Por miembro", value=self.money(op.per_user_amount), inline=True)
                emb.add_field(name="Destinatarios", value=str(len(op.movements)), inline=True)
                emb.add_field(name=f"Total {verb}", value=f"**{self.money(op.total_amount)}**", inline=True)
                emb.add_field(name="Ejecutado por", value=f"<@{op.actor_id}>", inline=True)
                emb.add_field(name="Estado", value="✅ Completada", inline=True)
                if op.reason:
                    emb.add_field(name="Motivo", value=op.reason[:1000], inline=False)
                if detail:
                    emb.add_field(name="IDs", value=f"```actor: {op.actor_id}\nrol: {op.role_id}```", inline=False)
            emb.set_footer(text=f"Operación {short_id} · Página {idx + 1}/{total_pages}")
            embeds.append(emb)
        return embeds

    # ---------------- registro de auditoría ----------------
    async def _get_log_channel(self) -> Optional[discord.abc.Messageable]:
        raw = await self._db(eco.get_setting, "log_channel_id")
        if not raw:
            return None
        channel_id = int(raw)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        return channel if hasattr(channel, "send") else None

    async def publish_log(self, op_id: str) -> bool:
        """Publica (o continúa publicando) el registro de una operación YA confirmada.

        Nunca vuelve a aplicar la operación: solo envía los Embeds que faltan, recordando en la base
        de datos cuántas páginas ya salieron.
        """
        async with self._log_lock:
            op = await self._db(eco.get_operation, op_id)
            if op is None or op.log_status == "sent":
                return True
            channel = await self._get_log_channel()
            if channel is None:
                return False
            pages = self.build_pages(op, detail=True)
            for i in range(op.log_pages_sent, len(pages)):
                try:
                    await channel.send(embed=pages[i], allowed_mentions=NO_MENTIONS)
                except Exception as exc:
                    print(f"⚠️ No pude publicar el registro económico {op_id[:8]} (página {i + 1}): {exc!r}")
                    await self._db(eco.set_log_progress, op_id, i, False, True)
                    return False
                await self._db(eco.set_log_progress, op_id, i + 1, i + 1 == len(pages))
            return True

    @tasks.loop(minutes=2)
    async def retry_pending_logs(self):
        try:
            for op_id in await self._db(eco.pending_log_operations):
                if not await self.publish_log(op_id):
                    break   # sin canal o con error: se reintenta en el próximo ciclo
        except Exception:
            traceback.print_exc()

    @retry_pending_logs.before_loop
    async def _before_retry(self):
        await self.bot.wait_until_ready()

    # ============================================================
    # Comandos de prefijo
    # ============================================================
    async def bal_embed(self, target) -> discord.Embed:
        """El mismo panel de `!bal` (se reutiliza en los tickets de cambio de moneda)."""
        balance = await self._db(eco.get_balance, target.id)
        lifetime = await self._db(eco.get_lifetime_deposits, target.id)
        color = discord.Color.green() if balance > 0 else (discord.Color.red() if balance < 0 else discord.Color.light_grey())
        emb = discord.Embed(title=eco.CURRENCY_NAME, color=color)
        emb.description = f"{target.mention}\n\n**Balance actual**\n# {self.money(balance)}"
        emb.add_field(name="📈 Balance histórico", value=self.money(lifetime), inline=False)
        gamble = await self._db(stats_db.get_user_stats, target.id)
        emb.add_field(name="🎲 Total apostado", value=self.money(gamble.wagered), inline=True)
        # Mismos números que los paneles «Top ganado» / «Top perdido» de /panel apuestas (ganado − perdido): positivo o negativo.
        net = gamble.net
        emb.add_field(name="🏆 Total ganado apostando", value=self.money(net, signed=True) if net > 0 else self.money(0), inline=True)
        emb.add_field(name="📉 Total perdido apostando", value=self.money(net) if net < 0 else self.money(0), inline=True)
        try:
            emb.set_thumbnail(url=target.display_avatar.url)
        except Exception:
            pass
        emb.set_footer(text=eco.CURRENCY_NAME)
        return emb

    @commands.command(name="bal")
    @guard
    async def bal(self, ctx: commands.Context, *, objetivo: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "bal"):
            return

        if objetivo.strip():
            target, error = await self._resolve_member_flexible(ctx.guild, objetivo)
            if target is None:
                return await ctx.reply(f"❌ {error}", mention_author=False, allowed_mentions=NO_MENTIONS)
        else:
            target = ctx.author

        emb = await self.bal_embed(target)
        await ctx.send(embed=emb, allowed_mentions=NO_MENTIONS)   # público, sin respuestas efímeras

    @commands.command(name="top")
    @guard
    async def top(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "top"):
            return
        await self._ensure_members(ctx.guild)
        balances = await self._db(eco.get_all_balances)
        eligible = [m.id for m in ctx.guild.members if not m.bot]
        ranking = eco.rank_top(balances, eligible, limit=len(eligible) or 1)
        page_size = 10
        groups = chunk(ranking, page_size)
        pages = len(groups)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        names = {m.id: clean_name(m.display_name) for m in ctx.guild.members}

        def build(page: int) -> discord.Embed:
            rows = groups[page]
            title = f"🏆 Top 10 — {eco.CURRENCY_NAME}" if page == 0 else f"🏆 Puestos {rows[0][0]}–{rows[-1][0]} — {eco.CURRENCY_NAME}"
            emb = discord.Embed(title=title, color=discord.Color.gold())
            if not rows:
                emb.description = "Todavía no hay usuarios para mostrar."
            else:
                emb.description = "\n".join(
                    f"{medals.get(pos, f'`{pos}.`')} {names.get(uid) or f'Usuario {uid}'} — **{self.money(bal)}**" for pos, uid, bal in rows)
            emb.set_footer(text=f"{eco.CURRENCY_NAME} · Página {page + 1}/{pages}" if pages > 1 else eco.CURRENCY_NAME)
            return emb

        if pages > 1:
            view = PagedView([build(i) for i in range(pages)], owner_id=ctx.author.id)
            await ctx.send(embed=build(0), view=view, allowed_mentions=NO_MENTIONS)
        else:
            await ctx.send(embed=build(0), allowed_mentions=NO_MENTIONS)

    # ---------------- addmoney / remove-money ----------------
    async def _parse_amount_or_reply(self, ctx: commands.Context, text: str) -> Optional[int]:
        try:
            return eco.parse_amount(text)
        except eco.AmountError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False, allowed_mentions=NO_MENTIONS)
            return None

    async def _reason_or_reply(self, ctx: commands.Context, tokens: List[str], default: str) -> Optional[str]:
        try:
            return eco.reason_from_tokens(tokens, default)
        except eco.AmountError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False, allowed_mentions=NO_MENTIONS)
            return None

    async def _apply_and_report(self, ctx: commands.Context, *, kind: str, user_ids: List[int],
                                amount: int, role_id: Optional[int], reason: Optional[str] = None):
        try:
            op = await self._db(
                eco.apply_operation, kind=kind, actor_id=ctx.author.id, user_ids=user_ids, amount=amount,
                request_key=f"msg:{ctx.message.id}", role_id=role_id, reason=reason,
            )
        except OverflowError:
            return await ctx.reply("❌ El balance resultante superaría el límite permitido. No se modificó nada.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            return await ctx.reply("❌ No se pudo completar la operación. No se modificó ningún balance.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)

        if not op.created:
            return await ctx.reply("ℹ️ Esta solicitud ya había sido procesada; no se repitió.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)

        pages = self.build_pages(op, detail=False)
        await ctx.reply(embed=pages[0], mention_author=False, allowed_mentions=NO_MENTIONS)
        for extra in pages[1:]:
            await ctx.send(embed=extra, allowed_mentions=NO_MENTIONS)

        # El registro se publica DESPUÉS de confirmar en la base de datos; si falla, se reintenta solo
        # (sin volver a aplicar el dinero).
        try:
            await self.publish_log(op.op_id)
        except Exception:
            traceback.print_exc()

    @commands.command(name="addmoney")
    @guard
    async def addmoney(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "addmoney"):
            return
        tokens = args.split(None, 2)          # usuario, cantidad y motivo opcional (varias palabras)
        if len(tokens) < 2:
            return await ctx.reply("Uso: `!addmoney @Usuario cantidad [motivo]` (ej: `!addmoney @Jugador 150.000 recompensa evento`)",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        member = await self._resolve_member_strict(ctx.guild, tokens[0])
        if member is None:
            return await ctx.reply("❌ Indicá al usuario con una mención o su ID.", mention_author=False, allowed_mentions=NO_MENTIONS)
        if member.bot:
            return await ctx.reply("❌ No se puede operar sobre bots.", mention_author=False, allowed_mentions=NO_MENTIONS)
        amount = await self._parse_amount_or_reply(ctx, tokens[1])
        if amount is None:
            return
        reason = await self._reason_or_reply(ctx, tokens, eco.REASON_ADD_DEFAULT)
        if reason is None:
            return
        await self._apply_and_report(ctx, kind="add", user_ids=[member.id], amount=amount, role_id=None, reason=reason)

    async def _role_members(self, guild: discord.Guild, role: discord.Role) -> List[discord.Member]:
        await self._ensure_members(guild)
        return sorted((m for m in role.members if not m.bot), key=lambda m: m.id)

    async def _role_operation(self, ctx: commands.Context, kind: str, role: discord.Role, amount: int, reason: Optional[str] = None):
        members = await self._role_members(ctx.guild, role)
        if not members:
            return await ctx.reply(f"❌ El rol {role.mention} no tiene miembros elegibles (los bots no cuentan). No se modificó ningún balance.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        await self._apply_and_report(ctx, kind=kind, user_ids=[m.id for m in members], amount=amount, role_id=role.id, reason=reason)

    @commands.command(name="addmoney-role")
    @guard
    async def addmoney_role(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "addmoney-role"):
            return
        tokens = args.split(None, 2)
        m = _MENTION_ROLE_RE.match(tokens[0]) if len(tokens) >= 2 else None
        if not m:
            return await ctx.reply("Uso: `!addmoney-role @Rol cantidad [motivo]` (la cantidad es para **cada** miembro).",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        role = ctx.guild.get_role(int(m.group(1)))
        if role is None:
            return await ctx.reply("❌ No encontré ese rol.", mention_author=False, allowed_mentions=NO_MENTIONS)
        amount = await self._parse_amount_or_reply(ctx, tokens[1])
        if amount is None:
            return
        reason = await self._reason_or_reply(ctx, tokens, eco.REASON_ADD_DEFAULT)
        if reason is None:
            return
        await self._role_operation(ctx, "add", role, amount, reason)

    @commands.command(name="remove-money")
    @guard
    async def remove_money(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "remove-money"):
            return
        tokens = args.split(None, 2)
        if len(tokens) < 2:
            return await ctx.reply("Uso: `!remove-money @Usuario cantidad [motivo]` o `!remove-money @Rol cantidad [motivo]`",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        target_token = tokens[0]
        role: Optional[discord.Role] = None
        rm = _MENTION_ROLE_RE.match(target_token)
        if rm:
            role = ctx.guild.get_role(int(rm.group(1)))
            if role is None:
                return await ctx.reply("❌ No encontré ese rol.", mention_author=False, allowed_mentions=NO_MENTIONS)
        elif _ID_RE.match(target_token) and ctx.guild.get_role(int(target_token)) is not None:
            role = ctx.guild.get_role(int(target_token))
        amount = await self._parse_amount_or_reply(ctx, tokens[1])
        if amount is None:
            return
        reason = await self._reason_or_reply(ctx, tokens, eco.REASON_REMOVE_DEFAULT)
        if reason is None:
            return
        if role is not None:
            return await self._role_operation(ctx, "remove", role, amount, reason)

        member = await self._resolve_member_strict(ctx.guild, target_token)
        if member is None:
            return await ctx.reply("❌ Indicá a la persona o al rol con una mención (o ID).", mention_author=False, allowed_mentions=NO_MENTIONS)
        if member.bot:
            return await ctx.reply("❌ No se puede operar sobre bots.", mention_author=False, allowed_mentions=NO_MENTIONS)
        await self._apply_and_report(ctx, kind="remove", user_ids=[member.id], amount=amount, role_id=None, reason=reason)

    # ============================================================
    # Versiones slash de los comandos con "!" (misma lógica, mismos permisos y canales)
    # ============================================================
    @app_commands.command(name="bal", description="Mirá tu balance de Wuju Coins (o el de otra persona)")
    @app_commands.describe(usuario="Persona (opcional)")
    @app_commands.guild_only()
    async def bal_slash(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        await self.bal.callback(self, SlashContext(interaction), objetivo=usuario.mention if usuario else "")

    @app_commands.command(name="top", description="Top 10 de Wuju Coins")
    @app_commands.guild_only()
    async def top_slash(self, interaction: discord.Interaction):
        await self.top.callback(self, SlashContext(interaction))

    @app_commands.command(name="addmoney", description="Agrega Wuju Coins a una persona")
    @app_commands.describe(usuario="Persona que recibe", cantidad="Cantidad (ej: 150.000)", motivo="Motivo (opcional)")
    @app_commands.guild_only()
    async def addmoney_slash(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: str, motivo: Optional[str] = None):
        await self.addmoney.callback(self, SlashContext(interaction), args=f"{usuario.mention} {cantidad} {motivo or ''}".strip())

    @app_commands.command(name="addmoney-role", description="Agrega Wuju Coins a CADA miembro de un rol")
    @app_commands.describe(rol="Rol", cantidad="Cantidad para cada miembro", motivo="Motivo (opcional)")
    @app_commands.guild_only()
    async def addmoney_role_slash(self, interaction: discord.Interaction, rol: discord.Role, cantidad: str, motivo: Optional[str] = None):
        await self.addmoney_role.callback(self, SlashContext(interaction), args=f"{rol.mention} {cantidad} {motivo or ''}".strip())

    @app_commands.command(name="remove-money", description="Retira Wuju Coins a una persona o a un rol")
    @app_commands.describe(cantidad="Cantidad a retirar", usuario="Persona (o usá rol)", rol="Rol (o usá usuario)", motivo="Motivo (opcional)")
    @app_commands.guild_only()
    async def remove_money_slash(self, interaction: discord.Interaction, cantidad: str, usuario: Optional[discord.Member] = None,
                                 rol: Optional[discord.Role] = None, motivo: Optional[str] = None):
        if (usuario is None) == (rol is None):
            return await self._ephemeral(interaction, "❌ Indicá una persona **o** un rol (uno solo de los dos).")
        target = usuario.mention if usuario is not None else rol.mention
        await self.remove_money.callback(self, SlashContext(interaction), args=f"{target} {cantidad} {motivo or ''}".strip())

    # ============================================================
    # Comandos slash de administración
    # ============================================================
    @app_commands.command(name="config-moneda", description="(Admin) Configura el emoji de Wuju Coins")
    @app_commands.describe(icono="Emoji personalizado del servidor (ej: :wuju:). Escribí 'quitar' para borrarlo.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def config_moneda(self, interaction: discord.Interaction, icono: str):
        if not await self._require_admin(interaction):
            return
        value = icono.strip()
        if value.casefold() in ("quitar", "borrar", "reset", "ninguno"):
            await self._db(eco.set_setting, "currency_emoji", None)
            self._emoji_raw = None
            return await self._ephemeral(interaction, f"✅ Icono eliminado. Se mostrará «{eco.CURRENCY_NAME}».")
        parsed = eco.parse_custom_emoji(value)
        if parsed is None:
            return await self._ephemeral(
                interaction,
                "❌ Tiene que ser un emoji **personalizado** de Discord (al escribir `:` elegilo de la lista para que quede como `<:nombre:id>`)."
            )
        raw, emoji_id = parsed
        emoji = self.bot.get_emoji(emoji_id)
        if emoji is None:
            return await self._ephemeral(interaction, "❌ No puedo usar ese emoji: tiene que pertenecer a un servidor donde esté el bot.")
        await self._db(eco.set_setting, "currency_emoji", raw)
        self._emoji_raw = raw
        await self._ephemeral(interaction, f"✅ Icono de {eco.CURRENCY_NAME} actualizado: {emoji}")

    @app_commands.command(name="bal-registro", description="(Admin) Canal donde se publican los movimientos de Wuju Coins")
    @app_commands.describe(canal="Canal de auditoría económica")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def registro_bal(self, interaction: discord.Interaction, canal: discord.TextChannel):
        if not await self._require_admin(interaction):
            return
        me = interaction.guild.me
        perms = canal.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await self._ephemeral(interaction, f"❌ No tengo permisos para escribir embeds en {canal.mention}.")
        await self._db(eco.set_setting, "log_channel_id", str(canal.id))
        await self._ephemeral(interaction, f"✅ Los movimientos económicos se publicarán en {canal.mention}. "
                                           "Si había registros pendientes se publicarán en unos minutos.")

    @app_commands.command(name="bal-reintentar-registro", description="(Admin) Reintenta publicar registros económicos pendientes")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def registro_reintentar(self, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        pending = await self._db(eco.pending_log_operations, 100)
        done = 0
        for op_id in pending:
            if await self.publish_log(op_id):
                done += 1
        await interaction.followup.send(f"✅ Registros publicados: {done}/{len(pending)}.", ephemeral=True)

    @staticmethod
    def _cmd_label(command: str) -> str:
        if command == "1v1":
            return "`/1v1`"
        if command in eco.GAME_COMMANDS or command in eco.ECON_COMMANDS:
            return f"`/{command}` y `!{command}`"
        return f"`/{command}`" if command in ("verhistorial", "set-balance", "apuesta", "apuestas", "eco-stats", "give-money") else f"`!{command}`"

    @app_commands.command(name="perms-canal", description="(Admin) Habilita un comando en ESTE canal; si ya estaba habilitado acá, lo deshabilita")
    @app_commands.describe(comando="Comando a habilitar en este canal (si ya está acá, se deshabilita)")
    @app_commands.choices(comando=[app_commands.Choice(name=c, value=c) for c in eco.CHANNEL_COMMANDS])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def perms_canal(self, interaction: discord.Interaction, comando: app_commands.Choice[str]):
        if not await self._require_admin(interaction):
            return
        current = await self._db(eco.get_command_channel, comando.value)
        if current == interaction.channel_id:
            await self._db(eco.clear_command_channel, comando.value)
            return await self._ephemeral(
                interaction,
                f"🚫 {self._cmd_label(comando.value)} quedó **deshabilitado** (ya no funciona en <#{interaction.channel_id}> ni en ningún otro canal). "
                f"Volvé a usar `/perms-canal` en el canal que quieras para habilitarlo de nuevo.")
        await self._db(eco.set_command_channel, comando.value, interaction.channel_id)
        await self._ephemeral(interaction, f"✅ {self._cmd_label(comando.value)} ahora solo funciona en <#{interaction.channel_id}>.")

    @app_commands.command(name="perms-comando", description="(Admin) Otorga o revoca a un rol el uso de un comando económico")
    @app_commands.describe(rol="Rol", comando="Comando económico", accion="Otorgar (por defecto) o revocar")
    @app_commands.choices(
        comando=[app_commands.Choice(name=c, value=c) for c in eco.GRANTABLE_COMMANDS],
        accion=[app_commands.Choice(name="Otorgar", value="otorgar"), app_commands.Choice(name="Revocar", value="revocar")],
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def perms_comando(self, interaction: discord.Interaction, rol: discord.Role,
                            comando: app_commands.Choice[str], accion: Optional[app_commands.Choice[str]] = None):
        if not await self._require_admin(interaction):
            return
        revoke = accion is not None and accion.value == "revocar"
        if revoke:
            changed = await self._db(eco.revoke_command_role, comando.value, rol.id)
            msg = (f"✅ {rol.mention} ya no puede usar {self._cmd_label(comando.value)}." if changed
                   else f"ℹ️ {rol.mention} no tenía permiso para {self._cmd_label(comando.value)}.")
        else:
            changed = await self._db(eco.grant_command_role, comando.value, rol.id)
            msg = (f"✅ {rol.mention} ahora puede usar {self._cmd_label(comando.value)}." if changed
                   else f"ℹ️ {rol.mention} ya tenía permiso para {self._cmd_label(comando.value)}.")
        await self._ephemeral(interaction, msg)

    @app_commands.command(name="perms-ver", description="(Admin) Muestra roles y canales configurados de los comandos económicos")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def perms_ver(self, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return
        overview = await self._db(eco.get_permissions_overview)
        log_raw = await self._db(eco.get_setting, "log_channel_id")
        lines = []
        for cmd in eco.ECON_COMMANDS:
            info = overview[cmd]
            roles = set(info["roles"]) | self.default_roles.get(cmd, set())
            roles_txt = ", ".join(f"<@&{r}>" for r in sorted(roles)) or "solo administradores"
            if cmd in eco.ANY_CHANNEL_COMMANDS:
                chan_txt = "cualquier canal"
            else:
                chan_txt = f"<#{info['channel']}>" if info["channel"] else "⚠️ sin canal (deshabilitado)"
            lines.append(f"**!{cmd}** — canal: {chan_txt} — roles: {roles_txt}")
        for extra in eco.EXTRA_PERMISSION_COMMANDS:
            roles = set(await self._db(eco.get_command_roles, extra))
            roles_txt = ", ".join(f"<@&{r}>" for r in sorted(roles)) or "solo administradores" + (" y Staff" if extra in ("verhistorial", "eco-stats") else "")
            if extra in eco.GAME_COMMANDS:
                chan = await self._db(eco.get_command_channel, extra)
                chan_txt = f"<#{chan}>" if chan else "⚠️ sin canal (deshabilitado)"
            else:
                chan_txt = "cualquier canal"
            lines.append(f"**{'/' + extra}** — canal: {chan_txt} — roles: {roles_txt}")
        lines.append(f"\n**Registro:** {f'<#{log_raw}>' if log_raw else '⚠️ sin configurar'}")
        icon = self.icon()
        lines.append(f"**Icono:** {icon if icon else f'{eco.CURRENCY_NAME} (sin emoji)'}")
        await self._ephemeral(interaction, "\n".join(lines))


    # ============================================================
    # Importación única de saldos (admin)
    # ============================================================
    def _match_nickname(self, guild: discord.Guild, name: str):
        """Coincidencia EXACTA (sin distinguir mayúsculas) con el nombre de usuario de Discord o su nombre visible global.
        No se usan los apodos del servidor."""
        key = " ".join(name.split()).casefold()
        found = {}
        for m in guild.members:
            if m.bot:
                continue
            for candidate in (m.name, getattr(m, "global_name", None)):
                if candidate and " ".join(candidate.split()).casefold() == key:
                    found[m.id] = m
                    break
        return list(found.values())

    @app_commands.command(name="importar-bal", description="(Admin) Deja el saldo de cada persona EXACTAMENTE en el valor del archivo (nombre;cantidad)")
    @app_commands.describe(archivo="Archivo .txt o .csv con líneas `nombre_de_discord;saldo` (ej: juan123;5000)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def importar_bal(self, interaction: discord.Interaction, archivo: discord.Attachment):
        if not await self._require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            if archivo.size > 256 * 1024:
                return await self._ephemeral(interaction, "❌ El archivo es demasiado grande (máximo 256 KB).")
            raw = await archivo.read()
            text = raw.decode("utf-8-sig", errors="replace")
            try:
                entries, errors = eco.parse_import_text(text)
            except eco.AmountError as exc:
                return await self._ephemeral(interaction, f"❌ {exc}")
            await self._ensure_members(interaction.guild)

            matched, not_found, ambiguous, seen = [], [], [], {}
            for line, name, amount in entries:
                users = self._match_nickname(interaction.guild, name)
                if len(users) == 1:
                    uid = users[0].id
                    if uid in seen:
                        errors.append((line, name, f"Repetido (ya estaba en la línea {seen[uid]}); se ignora."))
                        continue
                    seen[uid] = line
                    matched.append((users[0], amount))
                elif not users:
                    not_found.append((line, name, amount))
                else:
                    ambiguous.append((line, name, amount, len(users)))

            report = None
            if not_found or ambiguous or errors:
                parts = [f"# Personas NO encontradas: {len(not_found)}. Corregí los nombres y volvé a importar este mismo archivo",
                         "# (las líneas que empiezan con # se ignoran)."]
                parts += [f"{name};{amount}" for _, name, amount in not_found] or ["# (ninguna)"]
                if ambiguous:
                    parts += ["", "# --- Nombres con más de una coincidencia (usá el nombre de usuario exacto) ---"]
                    parts += [f"# línea {l}: {n};{a} — {c} personas" for l, n, a, c in ambiguous]
                if errors:
                    parts += ["", "# --- Otras líneas con problemas ---"]
                    parts += [f"# línea {l}: {t[:60]} — {m}" for l, t, m in errors]
                report = "\n".join(parts) + "\n"

            def attach():
                return discord.File(io.BytesIO(report.encode("utf-8")), filename="no_encontrados.txt") if report else None

            def problems_summary() -> str:
                lines = []
                if not_found:
                    shown = ", ".join(f"**{n}**" for _, n, _ in not_found[:10])
                    lines.append(f"🔎 **{len(not_found)} no encontrado(s):** {shown}" + (" …" if len(not_found) > 10 else ""))
                if ambiguous:
                    lines.append(f"👥 {len(ambiguous)} con más de una coincidencia")
                if errors:
                    lines.append(f"⚠️ {len(errors)} línea(s) con formato inválido o repetidas")
                lines.append("📎 La lista completa va en el archivo adjunto `no_encontrados.txt`.")
                return "\n".join(lines)

            if not matched:
                body = "❌ No pude vincular a nadie. No se modificó ningún balance."
                if report:
                    body += "\n" + problems_summary()
                kwargs = {"file": attach()} if report else {}
                return await interaction.followup.send(body[:1900], ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)

            current = await self._db(eco.get_all_balances)
            changing = sum(1 for m, a in matched if current.get(m.id, 0) != a)
            preview = [f"{m.mention}: {self.plain(current.get(m.id, 0))} → **{self.plain(a)}**" for m, a in matched[:10]]
            if len(matched) > 10:
                preview.append(f"… y {len(matched) - 10} más")
            body = (f"**Vista previa** — el saldo de **{len(matched)}** persona(s) quedará **exactamente** en el valor del archivo "
                    f"({changing} cambian, {len(matched) - changing} ya estaban igual).\n" + "\n".join(preview))
            if report:
                body += "\n\n" + problems_summary() + "\nEsas líneas NO se cargarán."
            body += "\n\nSi está bien, tocá **Confirmar**. Es seguro repetirlo con el mismo archivo: el saldo queda igual."
            view = ImportConfirmView(self, interaction.user.id, matched, interaction.id)
            kwargs = {"file": attach()} if report else {}
            await interaction.followup.send(body[:1950], view=view, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No pude procesar el archivo.")

    async def apply_import(self, actor_id: int, matched, token: int) -> Tuple[int, int, int, int]:
        """Deja cada saldo en el valor pedido (operación propia e idempotente por persona).
        Devuelve (ajustadas, sin_cambio, repetidas, fallidas)."""
        applied = unchanged = repeated = failed = 0
        for member, target in matched:
            try:
                status, _, _ = await self._db(eco.set_balance, user_id=member.id, target=target, actor_id=actor_id,
                                              request_key=f"import:{token}:{member.id}", reason=eco.IMPORT_REASON)
                if status == "applied":
                    applied += 1
                elif status == "unchanged":
                    unchanged += 1
                else:
                    repeated += 1
            except Exception:
                traceback.print_exc()
                failed += 1
        return applied, unchanged, repeated, failed

    # ============================================================
    # Salida del servidor, /set-balance, /historico-bal, /eco-stats
    # ============================================================
    async def _audit_note(self, embed: discord.Embed):
        """Aviso en el canal de registro (mejor esfuerzo: si falla no afecta a la operación ya confirmada)."""
        try:
            channel = await self._get_log_channel()
            if channel is not None:
                await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ No pude publicar el aviso en el registro económico: {exc!r}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Cuando alguien sale (o lo echan/banean) su balance vuelve a 0. Queda anotado en su historial."""
        try:
            if getattr(member, "bot", False):
                return
            if self.guild_id and getattr(getattr(member, "guild", None), "id", self.guild_id) != self.guild_id:
                return
            actor = self.bot.user.id if getattr(self.bot, "user", None) else 0
            status, before, _ = await self._db(
                eco.set_balance, user_id=member.id, target=0, actor_id=actor,
                request_key=f"leave:{member.id}:{uuid.uuid4().hex}", reason="Salió del servidor", mov_type="balance_reset")
            try:                                  # además de la plata, se reinician sus estadísticas (tops y winrate)
                await self._db(stats_db.reset_user, member.id)
            except Exception:
                traceback.print_exc()
            if status == "applied":
                emb = discord.Embed(title="🚪 Balance restablecido por salida del servidor", color=discord.Color.orange())
                emb.add_field(name="Usuario", value=f"<@{member.id}> (`{member.id}`)", inline=True)
                emb.add_field(name="Balance anterior", value=self.money(before), inline=True)
                await self._audit_note(emb)
        except Exception:
            traceback.print_exc()

    @app_commands.command(name="set-balance", description="(Admin) Deja el balance de una persona EXACTAMENTE en el número indicado")
    @app_commands.describe(usuario="Persona", cantidad="Nuevo balance (admite 0 y negativos, ej: 5,000)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_balance_cmd(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: str):
        try:
            member = interaction.user
            if interaction.guild is None or not await self.has_command_permission(member, "set-balance"):
                return await self._ephemeral(interaction, "❌ Este comando es solo para el propietario del servidor o administradores.")
            if usuario.bot:
                return await self._ephemeral(interaction, "❌ No se puede operar sobre bots.")
            try:
                target = eco.parse_balance_value(cantidad)
            except eco.AmountError as exc:
                return await self._ephemeral(interaction, f"❌ {exc}")
            try:
                status, before, after = await self._db(
                    eco.set_balance, user_id=usuario.id, target=target, actor_id=member.id,
                    request_key=f"setbal:{interaction.id}", reason=f"Balance fijado por {member.display_name}")
            except OverflowError:
                return await self._ephemeral(interaction, "❌ El número supera el límite permitido.")
            if status == "unchanged":
                return await self._ephemeral(interaction, f"ℹ️ {usuario.mention} ya tenía exactamente {self.money(before)}. No se cambió nada.")
            await self._ephemeral(interaction, f"✅ Balance de {usuario.mention} fijado en **{self.money(after)}** (antes: {self.money(before)}).")
            emb = discord.Embed(title="🧾 Balance fijado", color=discord.Color.blurple())
            emb.add_field(name="Ejecutado por", value=member.mention, inline=True)
            emb.add_field(name="Usuario", value=usuario.mention, inline=True)
            emb.add_field(name="Antes → Ahora", value=f"{self.money(before)} → **{self.money(after)}**", inline=False)
            await self._audit_note(emb)
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No se pudo completar la operación. No se modificó el balance.")

    historico_bal = app_commands.Group(name="historico-bal", description="Balance histórico de Wuju Coins", guild_only=True)

    @historico_bal.command(name="reset", description="(Admin) Pone en cero el Balance histórico de TODOS (no toca los balances)")
    async def historico_reset(self, interaction: discord.Interaction):
        try:
            if interaction.guild is None or not is_admin_member(interaction.user):
                return await self._ephemeral(interaction, "❌ Este comando es solo para el propietario del servidor o administradores.")

            async def go(inter):
                affected = await self._db(eco.reset_lifetime, interaction.user.id)
                await inter.response.edit_message(
                    content=f"✅ Balance histórico reiniciado para todos ({affected} persona(s) tenían histórico). Los balances no cambiaron.", view=None)
                emb = discord.Embed(title="♻️ Balance histórico reiniciado", color=discord.Color.orange())
                emb.add_field(name="Ejecutado por", value=interaction.user.mention, inline=True)
                emb.add_field(name="Personas con histórico", value=str(affected), inline=True)
                await self._audit_note(emb)
            view = ConfirmActionView(interaction.user.id, go, "Sí, reiniciar histórico de todos")
            await interaction.response.send_message(
                "⚠️ Vas a poner en **cero el Balance histórico de TODOS**. Los balances actuales y el historial de movimientos no se tocan. "
                "Desde ahora el histórico contará solo lo que se deposite de nuevo. ¿Confirmás?",
                view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No pude completar la operación.")

    @app_commands.command(name="eco-stats", description="(Staff) Muestra cuánto Wuju Coins hay cargado en total")
    @app_commands.guild_only()
    async def eco_stats(self, interaction: discord.Interaction):
        try:
            if interaction.guild is None or not await self.has_command_permission(interaction.user, "eco-stats", staff_ok=True):
                return await self._ephemeral(interaction, "❌ Este comando es solo para el staff.")
            st = await self._db(eco.get_stats)
            emb = discord.Embed(title=f"📊 {eco.CURRENCY_NAME} — estadísticas", color=discord.Color.gold())
            emb.add_field(name="Total cargado (suma de todos los balances)", value=f"**{self.money(st.total)}**", inline=False)
            emb.add_field(name="Balances positivos", value=f"{self.money(st.positive)} · {st.holders_positive} persona(s)", inline=True)
            emb.add_field(name="Balances negativos", value=self.money(st.negative), inline=True)
            emb.add_field(name="Balance histórico total (depositado)", value=self.money(st.lifetime_total), inline=False)
            emb.set_footer(text=f"{st.accounts} cuenta(s) registradas")
            await interaction.response.send_message(embed=emb, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No pude obtener las estadísticas.")

    # ============================================================
    # /give-money: pasar Wuju Coins propios a otra persona
    # ============================================================
    @app_commands.command(name="give-money", description="Dale Wuju Coins de tu balance a otra persona")
    @app_commands.describe(usuario="Quién los recibe", cantidad="Cantidad (ej: 5,000)", motivo="Motivo (opcional)")
    @app_commands.guild_only()
    async def give_money(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: str, motivo: Optional[str] = None):
        try:
            member = interaction.user
            if interaction.guild is None or not isinstance(member, discord.Member):
                return await self._ephemeral(interaction, "❌ Solo disponible en el servidor.")
            allowed = (self.member_role_id in {r.id for r in member.roles}) or await self.has_command_permission(member, "give-money", staff_ok=True)
            if not allowed:
                return await self._ephemeral(interaction, "❌ No tenés permiso para usar este comando.")
            if usuario.bot:
                return await self._ephemeral(interaction, "❌ No se puede dar Wuju Coins a un bot.")
            if usuario.id == member.id:
                return await self._ephemeral(interaction, "❌ No te podés dar Wuju Coins a vos mismo.")
            try:
                amount = eco.parse_amount(cantidad)
            except eco.AmountError as exc:
                return await self._ephemeral(interaction, f"❌ {exc}")
            reason = eco.clean_reason(motivo, eco.GIVE_DEFAULT_REASON)
            if await self._db(eco.is_frozen, member.id):
                return await self._ephemeral(interaction, eco.FROZEN_TEXT)
            try:
                status, after_from, after_to = await self._db(
                    eco.transfer, from_id=member.id, to_id=usuario.id, amount=amount,
                    request_key=f"give:{interaction.id}", reason=reason)
            except eco.InsufficientFunds:
                balance = await self._db(eco.get_balance, member.id)
                return await self._ephemeral(interaction, f"❌ No tenés saldo suficiente. Tu saldo: {self.money(balance)}.")
            except OverflowError:
                return await self._ephemeral(interaction, "❌ El balance de esa persona superaría el límite permitido. No se movió nada.")
            if status == "repeated":
                return await self._ephemeral(interaction, "ℹ️ Esta solicitud ya había sido procesada; no se repitió.")
            text = f"💸 {member.mention} le dio **{self.money(amount)}** a {usuario.mention}."
            if reason != eco.GIVE_DEFAULT_REASON:
                text += f"\nMotivo: {reason}"
            await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions(users=[usuario]))
            emb = discord.Embed(title="💸 Transferencia entre usuarios", color=discord.Color.blurple())
            emb.add_field(name="De", value=f"{member.mention} → saldo {self.money(after_from)}", inline=True)
            emb.add_field(name="Para", value=f"{usuario.mention} → saldo {self.money(after_to)}", inline=True)
            emb.add_field(name="Cantidad", value=self.money(amount), inline=True)
            emb.add_field(name="Motivo", value=reason, inline=False)
            await self._audit_note(emb)
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No se pudo completar la transferencia. No se movió nada.")

    # ============================================================
    # Historial de movimientos
    # ============================================================
    def history_embed(self, rows: List["eco.HistoryRow"], title: str) -> discord.Embed:
        lines = []
        for r in rows:
            when = datetime.fromisoformat(r.created_at)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            reason = r.reason if len(r.reason) <= 70 else r.reason[:67] + "…"
            lines.append(f"<t:{int(when.timestamp())}:f> · **{r.label}** · `{self.plain(r.delta, signed=True)}` · {reason} · saldo: `{eco.format_amount(r.balance_after)}`")
        emb = discord.Embed(title=title, description="\n".join(lines)[:4000], color=discord.Color.blurple())
        emb.set_footer(text=f"Últimos {len(rows)} movimientos · del más reciente al más antiguo")
        return emb

    HISTORY_CSV_COOLDOWN = 60            # segundos entre pedidos de historial completo por persona
    HISTORY_CSV_MAX_BYTES = 7_000_000    # por archivo (Discord admite ~10 MB); si no entra, se parte en varios

    @staticmethod
    def history_csv_files(rows: List["eco.HistoryRow"], base_name: str) -> List[Tuple[str, bytes]]:
        """CSV(s) con el historial completo (más antiguo primero). Se arma en memoria; se parte si pasa el tamaño máximo."""
        import csv
        header = ["fecha_utc", "tipo", "monto", "saldo_despues", "motivo", "referencia"]
        parts: List[Tuple[str, bytes]] = []
        buf, writer, size = None, None, 0

        def start():
            nonlocal buf, writer, size
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            size = buf.tell()

        def flush():
            if buf is not None:
                parts.append(("", ("\ufeff" + buf.getvalue()).encode("utf-8")))     # BOM: Excel abre bien los acentos

        start()
        for r in reversed(rows):
            if size > EconomyCog.HISTORY_CSV_MAX_BYTES:
                flush()
                start()
            writer.writerow([r.created_at, r.label, r.delta, r.balance_after, (r.reason or "").replace("\n", " "), r.related_id or ""])
            size = buf.tell()
        flush()
        total = len(parts)
        return [(f"{base_name}.csv" if total == 1 else f"{base_name}_parte{i}.csv", data) for i, (_, data) in enumerate(parts, 1)]

    async def send_full_history(self, interaction: discord.Interaction, target: discord.abc.User, own: bool):
        now = asyncio.get_running_loop().time()
        last = self._csv_last.get(interaction.user.id)
        if last is not None and now - last < self.HISTORY_CSV_COOLDOWN:
            return await self._ephemeral(interaction, f"⏳ Esperá {int(self.HISTORY_CSV_COOLDOWN - (now - last)) + 1} segundos antes de pedir otro historial completo.")
        await interaction.response.defer(ephemeral=True)
        rows = await self._db(eco.get_history, target.id, 10_000_000)
        if not rows:
            return await self._ephemeral(interaction, "ℹ️ Todavía no tenés movimientos de Wuju Coins." if own else f"ℹ️ {target.mention} todavía no tiene movimientos.")
        self._csv_last[interaction.user.id] = now
        files = await asyncio.to_thread(self.history_csv_files, rows, f"historial_{target.id}")
        sent = 0
        while sent < len(files):                   # máx. 10 archivos por mensaje
            chunk_files = files[sent:sent + 10]
            await interaction.followup.send(
                f"📜 Historial completo de **{clean_name(getattr(target, 'display_name', str(target.id)))}**: {len(rows):,} movimientos (del más antiguo al más reciente).",
                files=[discord.File(io.BytesIO(data), filename=name) for name, data in chunk_files],
                ephemeral=True, allowed_mentions=NO_MENTIONS)
            sent += len(chunk_files)

    @app_commands.command(name="verhistorial", description="Mirá tu historial de Wuju Coins (el staff puede ver el de otra persona)")
    @app_commands.describe(usuario="(Solo staff) Persona cuyo historial querés consultar",
                           completo="true: te envío TODO el historial como archivo CSV, solo visible para vos")
    @app_commands.guild_only()
    async def verhistorial(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None, completo: Optional[bool] = False):
        try:
            member = interaction.user
            if interaction.guild is None or not isinstance(member, discord.Member):
                return await self._ephemeral(interaction, "❌ Usá este comando dentro del servidor.")

            if completo:
                target = usuario or member
                if target.id != member.id and not await self.has_command_permission(member, "verhistorial", staff_ok=True):
                    return await self._ephemeral(interaction, "❌ Solo el staff puede consultar el historial de otra persona.")
                return await self.send_full_history(interaction, target, own=target.id == member.id)

            if usuario is not None and usuario.id != member.id:
                if not await self.has_command_permission(member, "verhistorial", staff_ok=True):
                    return await self._ephemeral(interaction, "❌ Solo el staff puede consultar el historial de otra persona.")
                rows = await self._db(eco.get_history, usuario.id, 20)
                if not rows:
                    return await self._ephemeral(interaction, f"ℹ️ {usuario.mention} todavía no tiene movimientos.")
                emb = self.history_embed(rows, f"📜 Historial de {usuario.display_name}")
                # Respuesta efímera: solo la ve quien ejecutó el comando; nunca va al canal ni a un DM.
                return await interaction.response.send_message(embed=emb, ephemeral=True, allowed_mentions=NO_MENTIONS)

            rows = await self._db(eco.get_history, member.id, 20)
            if not rows:
                return await self._ephemeral(interaction, "ℹ️ Todavía no tenés movimientos de Wuju Coins.")
            emb = self.history_embed(rows, "📜 Tu historial de Wuju Coins")
            try:
                await member.send(embed=emb)
            except (discord.Forbidden, discord.HTTPException):
                return await self._ephemeral(interaction, "❌ No pude enviarte el historial por mensaje privado. Activá tus DMs para este servidor y probá de nuevo.")
            await self._ephemeral(interaction, "✅ Te envié tu historial por mensaje privado.")
        except Exception:
            traceback.print_exc()
            await self._ephemeral(interaction, "❌ No pude obtener el historial en este momento.")


async def setup_economy(bot: commands.Bot, db_path: str, guild_id: int, member_role_id: int,
                        staff_role_id: Optional[int] = None) -> EconomyCog:
    cog = EconomyCog(bot, db_path, member_role_id, staff_role_id, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
