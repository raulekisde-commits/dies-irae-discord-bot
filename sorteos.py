"""/sorteo — sorteos con botón para anotarse (capa de Discord). La lógica y el estado están en sorteos_db.py.

Un chequeo cada pocos segundos atiende los sorteos vencidos (con la hora real), así que no se atrasa por reinicios, reconexiones
ni relojes suspendidos, y el resultado se guarda ANTES de anunciarlo: un fallo al anunciar se reintenta sin volver a sortear."""
import asyncio
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import sorteos_db as sdb
from guildshop import can_manage
from memberguard import button_block_reason

NO_MENTIONS = discord.AllowedMentions.none()
REFRESH_SECONDS = 5


def _staff_role_id(bot) -> Optional[int]:
    return getattr(bot, "staff_role_id", None)


def parse_duration_hhmm(text: str) -> Optional[tuple]:
    if ":" not in text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if h < 0 or m < 0 or m > 59:
        return None
    return h, m


async def _say(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class GiveawayJoinView(discord.ui.View):
    """Botón persistente (mismo custom_id de siempre, así sirven los sorteos ya publicados)."""
    def __init__(self, cog: Optional["SorteosCog"] = None, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if disabled:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(label="🎟️ Anotarme", style=discord.ButtonStyle.success, custom_id="giveaway_join_btn")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_join(interaction)


class SorteosCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int, staff_role_id: int):
        self.bot, self.db_path, self.guild_id, self.staff_role_id = bot, db_path, guild_id, staff_role_id
        self._dirty: set = set()
        self._last_refresh: dict = {}
        self._busy: set = set()

    async def cog_load(self):
        await self.run(sdb.init_schema)
        self.bot.add_view(GiveawayJoinView(self))
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    # ---------- permisos ----------
    def _can_create(self, member) -> bool:
        return can_manage(member) or any(getattr(r, "id", None) == self.staff_role_id for r in getattr(member, "roles", []))

    # ---------- panel ----------
    def embed(self, g: sdb.Giveaway, entrants: Optional[int] = None, winners: Optional[List[int]] = None, finished: bool = False,
              note: Optional[str] = None) -> discord.Embed:
        end_unix = int(g.end_at.timestamp())
        n = len(g.entrants) if entrants is None else entrants
        emb = discord.Embed(title="🕳️ Sorteo del topo" if not finished else "🕳️ Sorteo finalizado",
                            description="Tocá el botón para anotarte." if not finished else "Este sorteo ya terminó.",
                            color=discord.Color.gold() if not finished else discord.Color.dark_grey())
        emb.add_field(name="🎁 Premio", value=f"`{g.prize}`", inline=False)
        emb.add_field(name="🏆 Ganadores", value=str(g.winners_count), inline=True)
        if finished:
            emb.add_field(name="👥 Anotados", value=str(n), inline=True)
            if winners:
                emb.add_field(name="🎉 Ganadores del sorteo", value="\n".join(f"{i}. <@{uid}>" for i, uid in enumerate(winners, start=1)), inline=False)
            elif note:
                emb.add_field(name="Resultado", value=note, inline=False)
        else:
            emb.add_field(name="⏳ Termina", value=f"<t:{end_unix}:t> (<t:{end_unix}:R>)", inline=False)
            emb.add_field(name="👥 Anotados", value=str(n), inline=True)
        emb.set_footer(text="Smogg Sorteos System")
        return emb

    async def _channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        return ch if hasattr(ch, "send") and hasattr(ch, "fetch_message") else None

    # ---------- /sorteo ----------
    @app_commands.command(name="sorteo", description="Crear sorteo (Staff y Council)")
    @app_commands.describe(premio="Qué se sortea (texto libre)", tiempo="Duración en H:M (ej: 0:30, 2:00, 6:10)",
                           ganadores=f"Cantidad de ganadores (1 a {sdb.MAX_WINNERS}). Por defecto 1")
    @app_commands.guild_only()
    async def sorteo(self, interaction: discord.Interaction, premio: str, tiempo: str,
                     ganadores: app_commands.Range[int, 1, sdb.MAX_WINNERS] = 1):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _say(interaction, "❌ Este comando solo funciona dentro del servidor.")
        if not self._can_create(interaction.user):
            return await _say(interaction, "❌ Solo el **Staff** y el **Council** pueden crear sorteos.")
        if not premio or not premio.strip():
            return await _say(interaction, "❌ El premio no puede estar vacío.")
        dur = parse_duration_hhmm(tiempo.strip())
        if dur is None:
            return await _say(interaction, "❌ Tiempo inválido.\nUsá `H:M` (ej: `0:30`, `2:00`, `6:10`).")
        h, m = dur
        if h == 0 and m == 0:
            return await _say(interaction, "❌ La duración tiene que ser mayor a `0:00`.")
        end_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=m)
        await interaction.response.defer(ephemeral=True)
        draft = sdb.Giveaway(0, premio.strip(), end_at, interaction.channel.id, interaction.user.id, interaction.guild.id, int(ganadores), "open")
        try:
            msg = await interaction.channel.send(embed=self.embed(draft), view=GiveawayJoinView(self))
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude publicar el sorteo. Revisá que el bot pueda enviar mensajes en este canal.")
        try:
            await self.run(sdb.create, message_id=msg.id, prize=draft.prize, end_at=end_at, channel_id=interaction.channel.id,
                           creator_id=interaction.user.id, guild_id=interaction.guild.id, winners_count=int(ganadores))
        except Exception:
            traceback.print_exc()
            try:
                await msg.delete()
            except Exception:
                pass
            return await _say(interaction, "❌ No pude guardar el sorteo en la base de datos, así que lo cancelé. Avisale a un administrador.")
        await _say(interaction, f"✅ Sorteo creado. Premio: `{draft.prize}` · Ganadores: {ganadores} · Termina: <t:{int(end_at.timestamp())}:R>")

    # ---------- anotarse ----------
    async def on_join(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _say(interaction, "❌ Solo disponible en el servidor.")
        if interaction.message is None:
            return await _say(interaction, "❌ No pude leer el mensaje del sorteo.")
        if interaction.user.bot:
            return await _say(interaction, "❌ Los bots no pueden participar.")
        blocked = await button_block_reason(self.bot, interaction.user)          # Miembro + registrado (admins exentos)
        if blocked:
            return await _say(interaction, blocked)
        try:
            res = await self.run(sdb.add_entrant, interaction.message.id, interaction.user.id)
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude guardar tu inscripción. Probá de nuevo en unos segundos.")
        if res == "notfound":
            return await _say(interaction, "❌ Este sorteo ya terminó o no está activo.")
        if res == "closed":
            return await _say(interaction, "⏰ Llegaste tarde, el sorteo ya terminó.")
        if res == "already":
            return await _say(interaction, "✅ Ya estabas anotado.")
        self._dirty.add(interaction.message.id)
        await _say(interaction, "✅ Listo, quedaste anotado.")

    # ---------- chequeo periódico ----------
    @tasks.loop(seconds=REFRESH_SECONDS)
    async def ticker(self):
        try:
            await self.process_due()
            await self.refresh_dirty()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def refresh_dirty(self):
        for mid in list(self._dirty):
            self._dirty.discard(mid)
            g = await self.run(sdb.get, mid)
            if g is None or g.state != "open":
                continue
            ch = await self._channel(g.channel_id)
            if ch is None:
                continue
            try:
                await ch.get_partial_message(mid).edit(embed=self.embed(g)) if hasattr(ch, "get_partial_message") else None
            except Exception as exc:
                print(f"Sorteo {mid}: no pude actualizar el contador: {exc!r}")

    async def process_due(self, now: Optional[datetime] = None) -> int:
        done = 0
        for mid in await self.run(sdb.pending, now):
            if mid in self._busy:
                continue
            self._busy.add(mid)
            try:
                if await self.process_one(mid, now):
                    done += 1
            except Exception:
                traceback.print_exc()
            finally:
                self._busy.discard(mid)
        return done

    async def valid_entrants(self, guild: discord.Guild, entrants) -> List[int]:
        """Siguen en el servidor y no son bots. Un error pasajero de Discord NO descarta a nadie (solo «no existe» lo descarta)."""
        valid = []
        for uid in entrants:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.NotFound:
                    member = None
                except Exception as exc:
                    print(f"Sorteo: no pude verificar a {uid} ({exc!r}); lo dejo participar.")
                    valid.append(uid)
                    continue
            if member is not None and not member.bot:
                valid.append(uid)
        return valid

    async def process_one(self, message_id: int, now: Optional[datetime] = None) -> bool:
        g = await self.run(sdb.get, message_id)
        if g is None:
            return False
        if g.state == "open":
            guild = self.bot.get_guild(g.guild_id)
            ch = await self._channel(g.channel_id) if guild is not None else None
            if guild is None or ch is None:
                return False                                   # sigue abierto: se reintenta en el próximo ciclo
            if not await self.run(sdb.claim_draw, message_id, now):
                return False
            try:
                valid = await self.valid_entrants(guild, sorted(g.entrants))
                g = await self.run(sdb.record_result, message_id, valid)
            except Exception:
                traceback.print_exc()
                await self.run(sdb.release_claim, message_id)
                return False
            if g is None:
                return False
        if g.state != "announcing":
            return False
        return await self.announce(g)

    async def announce(self, g: sdb.Giveaway) -> bool:
        """Muestra el resultado ya guardado. Si falla algo, queda en 'announcing' y se reintenta (sin volver a sortear)."""
        ch = await self._channel(g.channel_id)
        if ch is None:
            return False
        no_pings = NO_MENTIONS
        if g.outcome == "no_entrants":
            note, text, allowed = "No hubo participantes válidos.", f"📭 Sorteo de `{g.prize}` cancelado: no se anotó nadie.", no_pings
        elif g.outcome == "insufficient":
            note = f"Se pidieron {g.winners_count} ganadores pero solo hay {g.valid_count} participantes válidos."
            text = (f"⚠️ No se pudo sortear `{g.prize}`: se pidieron **{g.winners_count}** ganadores y solo hay **{g.valid_count}** participantes válidos. "
                    f"<@{g.creator_id}>, creá el sorteo de nuevo con menos ganadores si querés repetirlo.")
            allowed = discord.AllowedMentions(users=[discord.Object(id=g.creator_id)], roles=False, everyone=False)
        else:
            note = None
            lines = [f"🏆 **¡Sorteo finalizado!** Premio: `{g.prize}`", ""]
            lines += [f"{i}. <@{uid}>" for i, uid in enumerate(g.winners, start=1)]
            lines += ["", f"Participantes válidos: **{g.valid_count}**."]
            if g.outcome == "partial":
                lines.append(f"Se pidieron {g.winners_count} ganadores, pero solo había {len(g.winners)} participantes válidos.")
            text = "\n".join(lines)
            allowed = discord.AllowedMentions(users=[discord.Object(id=u) for u in g.winners], roles=False, everyone=False)
        try:
            msg = await ch.fetch_message(g.message_id)
            await msg.edit(embed=self.embed(g, entrants=len(g.entrants), winners=g.winners or None, finished=True, note=note),
                           view=GiveawayJoinView(self, disabled=True))
        except Exception as exc:
            if not isinstance(exc, discord.NotFound):          # si borraron el mensaje original igual se anuncia el resultado
                print(f"Sorteo {g.message_id}: no pude editar el panel ({exc!r}); lo reintento.")
                return False
        try:
            await ch.send(text, allowed_mentions=allowed)
        except Exception as exc:
            print(f"Sorteo {g.message_id}: no pude anunciar el resultado ({exc!r}); lo reintento.")
            return False
        await self.run(sdb.finish, g.message_id)
        return True


async def setup_sorteos(bot: commands.Bot, db_path: str, guild_id: int, staff_role_id: int) -> SorteosCog:
    cog = SorteosCog(bot, db_path, guild_id, staff_role_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
