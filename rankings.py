"""Paneles de ludópatas: /apuestas panel (capa de Discord).

Tres paneles públicos con el top 10 (más ganaron · más perdieron · más apostaron). Cada uno tiene un botón
«Siguiente ▶» que abre, solo para quien lo toca, la lista completa de a 10 personas por página.
Los paneles se refrescan solos cada minuto; los datos salen de stats_db (calculados del historial, no duplicados).
"""
import asyncio
import traceback
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco
import rankings_db as rdb
import stats_db
from paging import PagedView, chunk

NO_MENTIONS = discord.AllowedMentions.none()
PAGE_SIZE = 10
COMMAND_PERMISSION = "apuestas"

INFO = {
    "won": ("🏆 Ludópatas — los que más ganaron", "Total ganado apostando", 0x2ECC71),
    "lost": ("💸 Ludópatas — los que más perdieron", "Total perdido apostando", 0xE74C3C),
    "wagered": ("🎰 Ludópatas — los que más apostaron", "Total apostado", 0xF1C40F),
}


class RankPanelView(discord.ui.View):
    """Vista persistente de un panel: un solo botón, con custom_id propio por ranking."""

    def __init__(self, cog: "RankingsCog", kind: str):
        super().__init__(timeout=None)
        self.cog, self.kind = cog, kind
        button = discord.ui.Button(label="Siguiente ▶", style=discord.ButtonStyle.primary, custom_id=f"rank:{kind}")
        button.callback = self._pressed
        self.add_item(button)

    async def _pressed(self, interaction: discord.Interaction):
        await self.cog.on_next(interaction, self.kind)


class RankingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    apuestas = app_commands.Group(name="apuestas", description="Ranking de apuestas", guild_only=True)

    # ---------- infraestructura ----------
    async def cog_load(self):
        await self.run(rdb.init_schema)
        await self.run(stats_db.init_schema)
        for kind in rdb.KINDS:
            self.bot.add_view(RankPanelView(self, kind))
        if not self.refresher.is_running():
            self.refresher.start()

    async def cog_unload(self):
        self.refresher.cancel()

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

    async def rows_of(self, kind: str) -> List[Tuple[int, int]]:
        stats = await self.run(stats_db.top_by, kind)
        return [(st.user_id, getattr(st, kind)) for st in stats]

    # ---------- embeds ----------
    def page_embed(self, kind: str, rows: List[Tuple[int, int]], page: int) -> discord.Embed:
        title, label, color = INFO[kind]
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        emb = discord.Embed(title=title if page == 0 else f"{title} (puestos {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + len(chunk)})", color=color)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        if not chunk:
            emb.description = "_Todavía no hay datos._"
        else:
            emb.description = "\n".join(
                f"{medals.get(pos, f'`{pos:>2}.`')} <@{uid}> — **{self.money(value)}**"
                for pos, (uid, value) in enumerate(chunk, start=page * PAGE_SIZE + 1))
        emb.set_footer(text=f"{label} (apuestas, dados y coinflip) · Página {page + 1}/{pages}")
        return emb

    # ---------- /apuestas panel ----------
    @apuestas.command(name="panel", description="Publica los tres paneles de ludópatas (más ganaron, perdieron y apostaron) en este canal")
    async def panel(self, interaction: discord.Interaction):
        try:
            eco_cog = self.economy
            if interaction.guild is None or eco_cog is None or not await eco_cog.has_command_permission(interaction.user, COMMAND_PERMISSION):
                return await self._say(interaction, "❌ Solo los administradores o un rol autorizado con `/perms-comando` pueden usar este comando.")
            channel = interaction.channel
            if channel is None or not hasattr(channel, "send"):
                return await self._say(interaction, "❌ No puedo publicar en este canal.")
            await interaction.response.defer(ephemeral=True)
            posted = []
            try:
                for kind in rdb.KINDS:
                    rows = await self.rows_of(kind)
                    posted.append((kind, await channel.send(embed=self.page_embed(kind, rows, 0), view=RankPanelView(self, kind),
                                                            allowed_mentions=NO_MENTIONS)))
            except Exception as exc:
                traceback.print_exc()
                for _, m in posted:                                  # no dejar paneles a medias
                    try:
                        await m.delete()
                    except Exception:
                        pass
                return await interaction.followup.send(f"❌ No pude publicar los paneles: {exc}", ephemeral=True)
            for kind, msg in posted:
                old = await self.run(rdb.get_panel, interaction.guild.id, kind)
                await self.run(rdb.set_panel, interaction.guild.id, kind, channel.id, msg.id)
                if old and old[1] != msg.id:
                    try:
                        och = await self._get_channel(old[0])
                        await (await och.fetch_message(old[1])).delete()
                    except Exception:
                        pass
            await interaction.followup.send("✅ Paneles publicados. Se actualizan solos cada minuto.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await self._say(interaction, "❌ No pude completar el comando.")

    @staticmethod
    async def _say(interaction: discord.Interaction, text: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    # ---------- botón «Siguiente» ----------
    async def on_next(self, interaction: discord.Interaction, kind: str):
        try:
            rows = await self.rows_of(kind)
            groups = chunk(rows, PAGE_SIZE)
            if len(groups) <= 1:
                return await self._say(interaction, "ℹ️ No hay más personas en esta lista: el panel ya las muestra a todas.")
            pages = [self.page_embed(kind, rows, i) for i in range(len(groups))]
            view = PagedView(pages, owner_id=interaction.user.id)
            view.index = 1                                       # «Siguiente» desde el panel = puestos 11–20
            view._sync()
            await interaction.response.send_message(embed=pages[1], view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self._say(interaction, "❌ No pude cargar la lista.")

    # ---------- actualización automática ----------
    @tasks.loop(seconds=60)
    async def refresher(self):
        try:
            await self.refresh_all()
        except Exception:
            traceback.print_exc()

    @refresher.before_loop
    async def _before_refresher(self):
        await self.bot.wait_until_ready()

    async def refresh_all(self) -> int:
        done = 0
        for kind in rdb.KINDS:
            found = await self.run(rdb.get_panel, self.guild_id, kind)
            if not found:
                continue
            try:
                ch = await self._get_channel(found[0])
                if ch is None:
                    continue
                msg = await ch.fetch_message(found[1])
                rows = await self.rows_of(kind)
                await msg.edit(embed=self.page_embed(kind, rows, 0), allowed_mentions=NO_MENTIONS)
                done += 1
            except Exception as exc:
                print(f"⚠️ Ranking '{kind}': no pude actualizar el panel: {exc!r}")
        return done


async def setup_rankings(bot: commands.Bot, db_path: str, guild_id: int) -> RankingsCog:
    cog = RankingsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
