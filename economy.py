"""Wuju Coins — capa de Discord (comandos, paneles, permisos).

Toda la lógica de dinero vive en economy_db.py; acá solo se resuelven usuarios/roles,
se valida el contexto y se arman los Embeds.
"""
import asyncio
import functools
import re
import traceback
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco

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


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, member_role_id: int):
        self.bot = bot
        self.db_path = db_path
        self.member_role_id = member_role_id
        # roles que pueden usar el comando sin /perms-comando (además de los configurados y de los admins)
        self.default_roles = {"bal": {member_role_id}, "top": {member_role_id}}
        self._emoji_raw: Optional[str] = None
        self._log_lock = asyncio.Lock()

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

        balance = await self._db(eco.get_balance, target.id)
        color = discord.Color.green() if balance > 0 else (discord.Color.red() if balance < 0 else discord.Color.light_grey())
        emb = discord.Embed(title=f"💰 Balance de {eco.CURRENCY_NAME}", color=color)
        emb.description = f"{target.mention}\n\n# {self.money(balance)}"
        try:
            emb.set_thumbnail(url=target.display_avatar.url)
        except Exception:
            pass
        emb.set_footer(text=eco.CURRENCY_NAME)
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
        ranking = eco.rank_top(balances, eligible, limit=10)

        emb = discord.Embed(title=f"🏆 Top 10 — {eco.CURRENCY_NAME}", color=discord.Color.gold())
        if not ranking:
            emb.description = "Todavía no hay usuarios para mostrar."
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            emb.description = "\n".join(
                f"{medals.get(pos, f'`{pos}.`')} <@{uid}> — **{self.money(bal)}**" for pos, uid, bal in ranking
            )
        emb.set_footer(text=eco.CURRENCY_NAME)
        await ctx.send(embed=emb, allowed_mentions=NO_MENTIONS)

    # ---------------- addmoney / remove-money ----------------
    async def _parse_amount_or_reply(self, ctx: commands.Context, text: str) -> Optional[int]:
        try:
            return eco.parse_amount(text)
        except eco.AmountError as exc:
            await ctx.reply(f"❌ {exc}", mention_author=False, allowed_mentions=NO_MENTIONS)
            return None

    async def _apply_and_report(self, ctx: commands.Context, *, kind: str, user_ids: List[int],
                                amount: int, role_id: Optional[int]):
        try:
            op = await self._db(
                eco.apply_operation, kind=kind, actor_id=ctx.author.id, user_ids=user_ids, amount=amount,
                request_key=f"msg:{ctx.message.id}", role_id=role_id,
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
        tokens = args.split()
        if len(tokens) != 2:
            return await ctx.reply("Uso: `!addmoney @Usuario cantidad` (ej: `!addmoney @Jugador 150.000`)",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        member = await self._resolve_member_strict(ctx.guild, tokens[0])
        if member is None:
            return await ctx.reply("❌ Indicá al usuario con una mención o su ID.", mention_author=False, allowed_mentions=NO_MENTIONS)
        if member.bot:
            return await ctx.reply("❌ No se puede operar sobre bots.", mention_author=False, allowed_mentions=NO_MENTIONS)
        amount = await self._parse_amount_or_reply(ctx, tokens[1])
        if amount is None:
            return
        await self._apply_and_report(ctx, kind="add", user_ids=[member.id], amount=amount, role_id=None)

    async def _role_members(self, guild: discord.Guild, role: discord.Role) -> List[discord.Member]:
        await self._ensure_members(guild)
        return sorted((m for m in role.members if not m.bot), key=lambda m: m.id)

    async def _role_operation(self, ctx: commands.Context, kind: str, role: discord.Role, amount: int):
        members = await self._role_members(ctx.guild, role)
        if not members:
            return await ctx.reply(f"❌ El rol {role.mention} no tiene miembros elegibles (los bots no cuentan). No se modificó ningún balance.",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        await self._apply_and_report(ctx, kind=kind, user_ids=[m.id for m in members], amount=amount, role_id=role.id)

    @commands.command(name="addmoney-role")
    @guard
    async def addmoney_role(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "addmoney-role"):
            return
        tokens = args.split()
        m = _MENTION_ROLE_RE.match(tokens[0]) if len(tokens) == 2 else None
        if not m:
            return await ctx.reply("Uso: `!addmoney-role @Rol cantidad` (la cantidad es para **cada** miembro).",
                                   mention_author=False, allowed_mentions=NO_MENTIONS)
        role = ctx.guild.get_role(int(m.group(1)))
        if role is None:
            return await ctx.reply("❌ No encontré ese rol.", mention_author=False, allowed_mentions=NO_MENTIONS)
        amount = await self._parse_amount_or_reply(ctx, tokens[1])
        if amount is None:
            return
        await self._role_operation(ctx, "add", role, amount)

    @commands.command(name="remove-money")
    @guard
    async def remove_money(self, ctx: commands.Context, *, args: str = ""):
        if ctx.guild is None:
            return
        if not await self._authorize(ctx, "remove-money"):
            return
        tokens = args.split()
        if len(tokens) != 2:
            return await ctx.reply("Uso: `!remove-money @Usuario cantidad` o `!remove-money @Rol cantidad`",
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
        if role is not None:
            return await self._role_operation(ctx, "remove", role, amount)

        member = await self._resolve_member_strict(ctx.guild, target_token)
        if member is None:
            return await ctx.reply("❌ Indicá a la persona o al rol con una mención (o ID).", mention_author=False, allowed_mentions=NO_MENTIONS)
        if member.bot:
            return await ctx.reply("❌ No se puede operar sobre bots.", mention_author=False, allowed_mentions=NO_MENTIONS)
        await self._apply_and_report(ctx, kind="remove", user_ids=[member.id], amount=amount, role_id=None)

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

    @app_commands.command(name="registro-bal", description="(Admin) Canal donde se publican los movimientos de Wuju Coins")
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

    @app_commands.command(name="registro-reintentar", description="(Admin) Reintenta publicar registros económicos pendientes")
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

    @app_commands.command(name="perms-canal", description="(Admin) Habilita un comando económico en ESTE canal")
    @app_commands.describe(comando="Comando que querés habilitar en este canal")
    @app_commands.choices(comando=[app_commands.Choice(name=c, value=c) for c in eco.ECON_COMMANDS])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def perms_canal(self, interaction: discord.Interaction, comando: app_commands.Choice[str]):
        if not await self._require_admin(interaction):
            return
        await self._db(eco.set_command_channel, comando.value, interaction.channel_id)
        await self._ephemeral(interaction, f"✅ `!{comando.value}` ahora solo funciona en <#{interaction.channel_id}>.")

    @app_commands.command(name="perms-comando", description="(Admin) Otorga o revoca a un rol el uso de un comando económico")
    @app_commands.describe(rol="Rol", comando="Comando económico", accion="Otorgar (por defecto) o revocar")
    @app_commands.choices(
        comando=[app_commands.Choice(name=c, value=c) for c in eco.ECON_COMMANDS],
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
            msg = (f"✅ {rol.mention} ya no puede usar `!{comando.value}`." if changed
                   else f"ℹ️ {rol.mention} no tenía permiso para `!{comando.value}`.")
        else:
            changed = await self._db(eco.grant_command_role, comando.value, rol.id)
            msg = (f"✅ {rol.mention} ahora puede usar `!{comando.value}`." if changed
                   else f"ℹ️ {rol.mention} ya tenía permiso para `!{comando.value}`.")
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
            chan_txt = f"<#{info['channel']}>" if info["channel"] else "⚠️ sin canal (deshabilitado)"
            lines.append(f"**!{cmd}** — canal: {chan_txt} — roles: {roles_txt}")
        lines.append(f"\n**Registro:** {f'<#{log_raw}>' if log_raw else '⚠️ sin configurar'}")
        icon = self.icon()
        lines.append(f"**Icono:** {icon if icon else f'{eco.CURRENCY_NAME} (sin emoji)'}")
        await self._ephemeral(interaction, "\n".join(lines))


async def setup_economy(bot: commands.Bot, db_path: str, guild_id: int, member_role_id: int) -> EconomyCog:
    cog = EconomyCog(bot, db_path, member_role_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
