"""Sistema de warnings (solo Staff y Council): /warning, /warnings (lista pública de alguien) y /warning-top.
Todo lo que responde es visible solo para quien ejecuta el comando, salvo /warnings, que muestra los warnings de la persona consultada."""
import asyncio
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx, utcnow_iso
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
REASON_MAX = 500
LIST_MAX = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS warnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    issued_by  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(guild_id, user_id);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def add_warning(db_path: str, guild_id: int, user_id: int, reason: str, issued_by: int) -> Tuple[int, int]:
    """Devuelve (id del warning, total de warnings de esa persona)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO warnings(guild_id, user_id, reason, issued_by, created_at) VALUES (?,?,?,?,?)",
                               (guild_id, user_id, reason, issued_by, utcnow_iso()))
            total = conn.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()[0]
            return int(cur.lastrowid), int(total)
    finally:
        conn.close()


def list_warnings(db_path: str, guild_id: int, user_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC", (guild_id, user_id))]
    finally:
        conn.close()


def top_warned(db_path: str, guild_id: int, size: int = 10) -> List[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        return [(int(r["user_id"]), int(r["n"])) for r in conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(id) AS last FROM warnings WHERE guild_id=? GROUP BY user_id ORDER BY n DESC, last DESC LIMIT ?",
            (guild_id, size))]
    finally:
        conn.close()


def can_warn(member) -> bool:
    return can_manage(member) or STAFF_ROLE_ID in {r.id for r in getattr(member, "roles", [])}


def _ts(iso: str) -> int:
    try:
        dt = datetime.fromisoformat(iso)
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        return 0


class WarningsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    @staticmethod
    async def _say(interaction: discord.Interaction, text: str, *, ephemeral: bool = True, **kw):
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS, **kw)

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not can_warn(interaction.user):
            await self._say(interaction, "🔒 Solo el Staff y el Council pueden usar el sistema de warnings.")
            return False
        return True

    @app_commands.command(name="warning", description="(Staff/Council) Ponerle un warning a alguien")
    @app_commands.describe(usuario="A quién", razon="Motivo del warning")
    @app_commands.guild_only()
    async def warning(self, interaction: discord.Interaction, usuario: discord.Member, razon: app_commands.Range[str, 3, REASON_MAX]):
        if not await self._gate(interaction):
            return
        if usuario.bot:
            return await self._say(interaction, "❌ No se le pueden poner warnings a un bot.")
        wid, total = await self.run(add_warning, interaction.guild.id, usuario.id, razon.strip(), interaction.user.id)
        await self._say(interaction, f"⚠️ Warning **#{wid}** cargado a {usuario.mention} (lleva **{total}**).\n**Motivo:** {discord.utils.escape_markdown(razon.strip())}")

    @app_commands.command(name="warnings", description="(Staff/Council) Lista los warnings de alguien (lo ve todo el canal)")
    @app_commands.describe(usuario="De quién")
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._gate(interaction):
            return
        rows = await self.run(list_warnings, interaction.guild.id, usuario.id)
        emb = discord.Embed(title=f"⚠️ Warnings de {usuario.display_name}", color=discord.Color.orange())
        emb.set_thumbnail(url=usuario.display_avatar.url) if hasattr(usuario, "display_avatar") else None
        if not rows:
            emb.description = f"{usuario.mention} no tiene warnings."
        else:
            lines = [f"**#{r['id']}** · <t:{_ts(r['created_at'])}:d> · por <@{r['issued_by']}>\n{discord.utils.escape_markdown(r['reason'])}" for r in rows[:LIST_MAX]]
            emb.description = f"{usuario.mention} · **{len(rows)}** warning{'s' if len(rows) != 1 else ''}\n\n" + "\n\n".join(lines)
            if len(emb.description) > 4000:
                emb.description = emb.description[:3990] + "…"
            if len(rows) > LIST_MAX:
                emb.set_footer(text=f"Se muestran los últimos {LIST_MAX} de {len(rows)}.")
        await interaction.response.send_message(embed=emb, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="warning-top", description="(Staff/Council) Quién tiene más warnings")
    @app_commands.guild_only()
    async def warning_top(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        rows = await self.run(top_warned, interaction.guild.id, 10)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medals.get(i, f'`{i:>2}.`')} <@{u}> — **{n}** warning{'s' if n != 1 else ''}" for i, (u, n) in enumerate(rows, start=1)]
        emb = discord.Embed(title="⚠️ Top warnings", description="\n".join(lines) or "Nadie tiene warnings.", color=discord.Color.orange())
        await self._say(interaction, "", embed=emb)


async def setup_warnings(bot, db_path: str, guild_id: int) -> WarningsCog:
    cog = WarningsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
