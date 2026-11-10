"""Ausencias: /ausencia avisar (cualquier miembro), /ausencia lista y /ausencia ver (Staff/Council).
Todas las respuestas son privadas. Si alguien avisa teniendo una ausencia activa, se reemplaza por la nueva."""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx, utcnow_iso
from warnings_sys import can_warn as is_staff

NO_MENTIONS = discord.AllowedMentions.none()
TEXT_MAX = 300
DIAS_MAX = 90
LIST_MAX = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS ausencias (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    texto      TEXT NOT NULL,
    dias       INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    hasta      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ausencias_user ON ausencias(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_ausencias_hasta ON ausencias(guild_id, hasta);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def add_ausencia(db_path: str, guild_id: int, user_id: int, texto: str, dias: int, now: Optional[datetime] = None) -> dict:
    """Carga una ausencia; si la persona ya tenía una activa, la cierra (hasta=ahora) y deja la nueva."""
    now = now or datetime.now(timezone.utc)
    hasta = (now + timedelta(days=dias)).isoformat()
    conn = connect(db_path)
    try:
        with write_tx(conn):
            reemplazo = conn.execute("UPDATE ausencias SET hasta=? WHERE guild_id=? AND user_id=? AND hasta>?",
                                     (now.isoformat(), guild_id, user_id, now.isoformat())).rowcount
            cur = conn.execute("INSERT INTO ausencias(guild_id, user_id, texto, dias, created_at, hasta) VALUES (?,?,?,?,?,?)",
                               (guild_id, user_id, texto, dias, now.isoformat(), hasta))
            return {"id": int(cur.lastrowid), "hasta": hasta, "reemplazo": reemplazo > 0}
    finally:
        conn.close()


def list_activas(db_path: str, guild_id: int, now: Optional[datetime] = None) -> List[dict]:
    now = (now or datetime.now(timezone.utc)).isoformat()
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ausencias WHERE guild_id=? AND hasta>? ORDER BY hasta ASC", (guild_id, now))]
    finally:
        conn.close()


def list_usuario(db_path: str, guild_id: int, user_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ausencias WHERE guild_id=? AND user_id=? ORDER BY id DESC", (guild_id, user_id))]
    finally:
        conn.close()


def _ts(iso: str) -> int:
    try:
        dt = datetime.fromisoformat(iso)
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        return 0


class AusenciasCog(commands.Cog):
    ausencia_g = app_commands.Group(name="ausencia", description="Avisar y consultar ausencias", guild_only=True)

    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    @staticmethod
    async def _say(interaction: discord.Interaction, text: str = "", **kw):
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not is_staff(interaction.user):
            await self._say(interaction, "🔒 Solo el Staff y el Council pueden ver las ausencias.")
            return False
        return True

    @ausencia_g.command(name="avisar", description="Avisá que vas a estar ausente")
    @app_commands.describe(texto="Motivo / detalle de la ausencia", dias="Cantidad de días")
    async def avisar(self, interaction: discord.Interaction, texto: app_commands.Range[str, 3, TEXT_MAX],
                     dias: app_commands.Range[int, 1, DIAS_MAX]):
        if interaction.guild is None:
            return await self._say(interaction, "❌ Solo dentro del servidor.")
        r = await self.run(add_ausencia, interaction.guild.id, interaction.user.id, texto.strip(), int(dias))
        extra = "\n(Reemplazó tu ausencia anterior.)" if r["reemplazo"] else ""
        await self._say(interaction, f"✅ Ausencia registrada por **{dias}** día{'s' if dias != 1 else ''}, "
                                     f"hasta <t:{_ts(r['hasta'])}:f>.\n**Motivo:** {discord.utils.escape_markdown(texto.strip())}{extra}")

    @ausencia_g.command(name="lista", description="(Staff/Council) Ausencias activas")
    async def lista(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        rows = await self.run(list_activas, interaction.guild.id)
        emb = discord.Embed(title="🏖️ Ausencias activas", color=discord.Color.blurple())
        if not rows:
            emb.description = "No hay nadie ausente."
        else:
            lines = [f"<@{r['user_id']}> · hasta <t:{_ts(r['hasta'])}:d> (<t:{_ts(r['hasta'])}:R>)\n{discord.utils.escape_markdown(r['texto'])}"
                     for r in rows[:LIST_MAX]]
            emb.description = f"**{len(rows)}** ausente{'s' if len(rows) != 1 else ''}\n\n" + "\n\n".join(lines)
            if len(emb.description) > 4000:
                emb.description = emb.description[:3990] + "…"
            if len(rows) > LIST_MAX:
                emb.set_footer(text=f"Se muestran {LIST_MAX} de {len(rows)}.")
        await self._say(interaction, embed=emb)

    @ausencia_g.command(name="ver", description="(Staff/Council) Ausencias de alguien")
    @app_commands.describe(usuario="De quién")
    async def ver(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._gate(interaction):
            return
        rows = await self.run(list_usuario, interaction.guild.id, usuario.id)
        now = datetime.now(timezone.utc).isoformat()
        emb = discord.Embed(title=f"🏖️ Ausencias de {usuario.display_name}", color=discord.Color.blurple())
        if not rows:
            emb.description = f"{usuario.mention} no registró ausencias."
        else:
            lines = []
            for r in rows[:LIST_MAX]:
                estado = "🟢 activa" if r["hasta"] > now else "⚪ terminada"
                lines.append(f"{estado} · <t:{_ts(r['created_at'])}:d> → <t:{_ts(r['hasta'])}:d> ({r['dias']} d)\n"
                             f"{discord.utils.escape_markdown(r['texto'])}")
            emb.description = f"{usuario.mention} · **{len(rows)}** ausencia{'s' if len(rows) != 1 else ''}\n\n" + "\n\n".join(lines)
            if len(emb.description) > 4000:
                emb.description = emb.description[:3990] + "…"
            if len(rows) > LIST_MAX:
                emb.set_footer(text=f"Se muestran las últimas {LIST_MAX} de {len(rows)}.")
        await self._say(interaction, embed=emb)


async def setup_ausencias(bot, db_path: str, guild_id: int) -> AusenciasCog:
    cog = AusenciasCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
