"""Registro de entradas y salidas del servidor: /canal-entradas y /canal-salidas.

• Entradas: el bot solo menciona a quien entró («@persona entró al servidor»), sin bienvenida.
• Salidas: quién se fue, qué roles tenía y qué cuenta de Albion tenía registrada."""
import asyncio
import traceback
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx
from guildshop import can_manage

SCHEMA = """
CREATE TABLE IF NOT EXISTS member_log_channels (
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,              -- 'join' | 'leave'
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);
"""
KINDS = {"join": "entradas", "leave": "salidas"}
USERS_ONLY = discord.AllowedMentions(users=True, roles=False, everyone=False)
NO_MENTIONS = discord.AllowedMentions.none()


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_channel_id(db_path: str, guild_id: int, kind: str) -> Optional[int]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT channel_id FROM member_log_channels WHERE guild_id=? AND kind=?", (guild_id, kind)).fetchone()
        return int(r["channel_id"]) if r else None
    finally:
        conn.close()


def set_channel_id(db_path: str, guild_id: int, kind: str, channel_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if channel_id is None:
                conn.execute("DELETE FROM member_log_channels WHERE guild_id=? AND kind=?", (guild_id, kind))
            else:
                conn.execute("INSERT INTO member_log_channels(guild_id, kind, channel_id) VALUES (?,?,?) "
                             "ON CONFLICT(guild_id, kind) DO UPDATE SET channel_id=excluded.channel_id", (guild_id, kind, channel_id))
    finally:
        conn.close()


def leave_embed(member, regs: list) -> discord.Embed:
    """Ficha de quien se fue: roles que tenía y personaje(s) de Albion."""
    emb = discord.Embed(title="🚪 Salió del servidor", color=discord.Color.dark_grey())
    name = getattr(member, "display_name", None) or getattr(member, "name", "?")
    emb.description = f"**{discord.utils.escape_markdown(str(name))}** (`{getattr(member, 'name', '?')}` · `{member.id}`)"
    try:
        emb.set_thumbnail(url=member.display_avatar.url)
    except Exception:
        pass
    roles = [r for r in getattr(member, "roles", []) if getattr(r, "name", "") != "@everyone" and not getattr(r, "is_default", lambda: False)()]
    roles.sort(key=lambda r: getattr(r, "position", 0), reverse=True)
    emb.add_field(name=f"🎭 Roles ({len(roles)})", value=(" ".join(f"<@&{r.id}>" for r in roles) or "Sin roles")[:1000], inline=False)
    if regs:
        lines = []
        for r in regs:
            extra = r.guild_name or "sin guild"
            lines.append(f"**{discord.utils.escape_markdown(r.char_name)}** · {extra}" + (f" · {r.region}" if r.region else ""))
        emb.add_field(name="⚔️ Cuenta de Albion", value="\n".join(lines)[:1000], inline=False)
    else:
        emb.add_field(name="⚔️ Cuenta de Albion", value="No estaba registrado.", inline=False)
    joined = getattr(member, "joined_at", None)
    if joined:
        emb.add_field(name="📅 Había entrado", value=f"<t:{int(joined.timestamp())}:D> (<t:{int(joined.timestamp())}:R>)", inline=False)
    emb.set_footer(text=f"ID {member.id}")
    emb.timestamp = datetime.now(timezone.utc)
    return emb


class MemberLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    async def _channel(self, guild: discord.Guild, kind: str):
        cid = await self.run(get_channel_id, guild.id, kind)
        if not cid:
            return None
        ch = guild.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except Exception:
                return None
        return ch

    # ---------- comandos ----------
    async def _configure(self, interaction: discord.Interaction, kind: str, canal, desactivar: bool):
        if interaction.guild is None or not can_manage(interaction.user):
            return await interaction.response.send_message("🔒 Solo el Council o los administradores pueden usar este comando.", ephemeral=True)
        label = KINDS[kind]
        if desactivar:
            await self.run(set_channel_id, interaction.guild.id, kind, None)
            return await interaction.response.send_message(f"🚫 Registro de {label} **desactivado**.", ephemeral=True)
        channel = canal or interaction.channel
        if channel is None or not hasattr(channel, "send"):
            return await interaction.response.send_message("❌ No puedo usar ese canal.", ephemeral=True)
        me = interaction.guild.me
        if me is not None and hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                return await interaction.response.send_message(f"❌ No tengo permiso para escribir en {channel.mention}.", ephemeral=True)
        await self.run(set_channel_id, interaction.guild.id, kind, channel.id)
        what = "se va a mencionar a quien entre al servidor" if kind == "join" else "se va a publicar quién se fue, sus roles y su cuenta de Albion"
        await interaction.response.send_message(f"✅ Registro de {label} configurado en {channel.mention}: {what}.", ephemeral=True)

    @app_commands.command(name="canal-entradas", description="(Council/Admin) Canal donde se menciona a quien entra al servidor")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar este registro")
    @app_commands.guild_only()
    async def canal_entradas(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self._configure(interaction, "join", canal, desactivar)

    @app_commands.command(name="canal-salidas", description="(Council/Admin) Canal donde se registra quién se fue, sus roles y su cuenta de Albion")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar este registro")
    @app_commands.guild_only()
    async def canal_salidas(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self._configure(interaction, "leave", canal, desactivar)

    # ---------- eventos ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            ch = await self._channel(member.guild, "join")
            if ch is not None:
                await ch.send(f"{member.mention} entró al servidor", allowed_mentions=USERS_ONLY)
        except Exception:
            traceback.print_exc()

    async def post_leave(self, member, regs: List) -> bool:
        """Lo llama el módulo de registro al irse alguien (así se leen los personajes ANTES de liberarlos)."""
        try:
            ch = await self._channel(member.guild, "leave")
            if ch is None:
                return False
            await ch.send(embed=leave_embed(member, regs), allowed_mentions=NO_MENTIONS)
            return True
        except Exception:
            traceback.print_exc()
            return False


async def setup_miembros(bot, db_path: str, guild_id: int) -> MemberLogCog:
    cog = MemberLogCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
