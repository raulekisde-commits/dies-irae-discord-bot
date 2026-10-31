"""/encargar — tareas para el staff: panel con botones Tomar / Soltar / Terminada, y publicación programada."""
import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import encargos_db as edb
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
TASK_LOG_CHANNEL_ID = int(os.getenv("TASK_LOG_CHANNEL_ID", "1566014626309873776"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
TASKS_CHANNEL_ID = int(os.getenv("TASKS_CHANNEL_ID", "1565986098679644270"))
STAFF_PING = discord.AllowedMentions(users=False, roles=[discord.Object(id=STAFF_ROLE_ID)], everyone=False)

COLORS = {"open": discord.Color.orange(), "taken": discord.Color.blue(), "done": discord.Color.green()}
EVENT_TEXT = {"created": "📌 creada por", "taken": "✋ tomada por", "released": "↩️ soltada por", "done": "✅ terminada por"}


async def _say(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


def _roles(member) -> set:
    return {r.id for r in getattr(member, "roles", [])}


def is_staff_or_council(member) -> bool:
    """Staff, Council, dueño o administrador."""
    return can_manage(member) or STAFF_ROLE_ID in _roles(member)


def can_take(member) -> bool:
    """Pueden tomar una tarea: Staff, Council, administradores y el rol Opinólogo."""
    return is_staff_or_council(member) or OPINOLOGO_ROLE_ID in _roles(member)


class TaskView(discord.ui.View):
    """Botones persistentes: la tarea se busca por el id del mensaje, así sirven aunque el bot se reinicie."""
    def __init__(self, cog: "EncargosCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Tomar tarea", style=discord.ButtonStyle.success, custom_id="task:take", emoji="✋")
    async def take(self, interaction: discord.Interaction, button):
        await self.cog.on_take(interaction)

    @discord.ui.button(label="Soltar tarea", style=discord.ButtonStyle.secondary, custom_id="task:release", emoji="↩️")
    async def release(self, interaction: discord.Interaction, button):
        await self.cog.on_release(interaction)

    @discord.ui.button(label="Tarea terminada", style=discord.ButtonStyle.primary, custom_id="task:done", emoji="✅")
    async def done(self, interaction: discord.Interaction, button):
        await self.cog.on_done(interaction)


class EncargosCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await self.run(edb.init_schema)
        self.bot.add_view(TaskView(self))
        if not self.ticker.is_running():
            self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    # ---------- panel ----------
    def embed(self, t: edb.Task) -> discord.Embed:
        state = {"open": "🟠 Pendiente", "taken": "🔵 En curso", "done": "🟢 Terminada"}.get(t.status, "⏳ Programada")
        e = discord.Embed(title=f"📌 Tarea #{t.id}", description=t.text, color=COLORS.get(t.status, discord.Color.light_grey()))
        e.add_field(name="Estado", value=state, inline=True)
        e.add_field(name="Creada por", value=f"<@{t.created_by}>", inline=True)
        if t.status == "taken" and t.taker_id:
            e.add_field(name="Tomada por", value=f"<@{t.taker_id}>", inline=True)
        if t.status == "done":
            e.add_field(name="Terminada por", value=f"<@{t.done_by}>", inline=True)
        if t.link:
            e.add_field(name="Link", value=t.link, inline=False)
        lines = []
        for at, kind, actor in t.events:
            try:
                dt = datetime.fromisoformat(at)
                ts = int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
            except Exception:
                ts = 0
            lines.append(f"<t:{ts}:t> {EVENT_TEXT.get(kind, kind)} <@{actor}>" if actor else f"<t:{ts}:t> {EVENT_TEXT.get(kind, kind)}")
        if lines:
            e.add_field(name="Historial", value="\n".join(lines[-12:])[:1024], inline=False)
        return e

    async def _channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def publish(self, task_id: int, late: bool = False) -> bool:
        """Publica la tarea (si todavía no se publicó). Devuelve True si salió."""
        if not await self.run(edb.claim_publish, task_id):
            return False
        t = await self.run(edb.get_task, task_id)
        try:
            ch = await self._channel(t.channel_id)
            if ch is None:
                raise RuntimeError("canal no disponible")
            note = "\n_(Salió con retraso: el bot estaba desconectado a la hora programada.)_" if late else ""
            msg = await ch.send(content=f"<@&{STAFF_ROLE_ID}> nueva tarea{note}", embed=self.embed(t), view=TaskView(self), allowed_mentions=STAFF_PING)
        except Exception:
            traceback.print_exc()
            await self.run(edb.unclaim_publish, task_id)          # se reintenta en el próximo ciclo
            return False
        await self.run(edb.mark_published, task_id, msg.id)
        return True

    async def create(self, *, text: str, created_by: int, publish_at: Optional[float] = None, source: Optional[str] = None,
                     link: Optional[str] = None, channel_id: Optional[int] = None) -> edb.Task:
        """Crea una tarea (la usan /encargar y los avisos automáticos, como el del combatlog de una CTA)."""
        t = await self.run(edb.create_task, guild_id=self.guild_id, channel_id=channel_id or TASKS_CHANNEL_ID, text=text, created_by=created_by,
                           publish_at=publish_at, source=source, link=link)
        if t.status == "scheduled" and (t.publish_at or 0) <= datetime.now(timezone.utc).timestamp():
            await self.publish(t.id)
        return await self.run(edb.get_task, t.id)

    # ---------- comando ----------
    @app_commands.command(name="encargar", description="Crea una tarea para el staff (panel con botones y mención al Staff)")
    @app_commands.describe(tarea="Qué hay que hacer", cuando="(Opcional) Cuándo publicarla: HH:MM en UTC (ej: 22:30) o +90m / +2h")
    @app_commands.guild_only()
    async def encargar(self, interaction: discord.Interaction, tarea: str, cuando: Optional[str] = None):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden encargar tareas.")
        try:
            at = edb.parse_when(cuando) if cuando and cuando.strip() else None
            t = await self.create(text=tarea, created_by=interaction.user.id, publish_at=at)
        except edb.TaskError as exc:
            return await _say(interaction, f"❌ {exc}")
        if at and at > datetime.now(timezone.utc).timestamp():
            return await _say(interaction, f"⏰ Tarea #{t.id} programada: se publica <t:{int(at)}:R> (<t:{int(at)}:f>) en <#{t.channel_id}>.")
        await _say(interaction, f"✅ Tarea #{t.id} publicada en <#{t.channel_id}>." if t.status != "scheduled"
                   else f"⚠️ Tarea #{t.id} guardada, pero no pude publicarla ahora; se reintenta sola.")

    async def send_top(self, interaction: discord.Interaction):
        """Lo usa `/top tareas`: quién aceptó y quién cerró más tareas del staff."""
        if not can_take(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y el rol Opinólogo pueden ver este ranking.")
        taken, done = await self.run(edb.top_users)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        def lines(rows):
            return "\n".join(f"{medals.get(i, f'`{i}.`')} <@{u}> — **{n}**" for i, (u, n) in enumerate(rows, start=1)) or "Todavía no hay datos."
        emb = discord.Embed(title="🏆 Top tareas", color=discord.Color.gold())
        emb.add_field(name="✋ Más tareas aceptadas", value=lines(taken), inline=True)
        emb.add_field(name="✅ Más tareas cerradas", value=lines(done), inline=True)
        await interaction.response.send_message(embed=emb, allowed_mentions=NO_MENTIONS)

    # ---------- botones ----------
    async def _task(self, interaction) -> Optional[edb.Task]:
        t = await self.run(edb.find_by_message, interaction.message.id) if interaction.message else None
        if t is None:
            await _say(interaction, "❌ No encuentro esa tarea.")
        return t

    async def _after(self, interaction: discord.Interaction, res: edb.Change, ok_text: str):
        messages = {"notyou": "❌ Eso solo lo puede hacer quien tomó la tarea.", "closed": "ℹ️ Esta tarea ya está terminada.",
                    "taken": "ℹ️ Esta tarea ya la tomó otra persona.", "notopen": "ℹ️ La tarea no está en un estado que permita eso.",
                    "notfound": "❌ No encuentro esa tarea."}
        if res.status != "ok":
            return await _say(interaction, messages.get(res.status, "❌ No se pudo."))
        if res.task.status == "done" and await self.log_done(res.task):
            try:
                await interaction.response.send_message("✅ Tarea terminada. Quedó registrada en el canal de logs.", ephemeral=True)
                await interaction.message.delete()
                return
            except Exception:
                traceback.print_exc()
        try:
            await interaction.response.edit_message(content=interaction.message.content, embed=self.embed(res.task),
                                                    view=None if res.task.status == "done" else TaskView(self), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await _say(interaction, ok_text)

    async def on_take(self, interaction: discord.Interaction):
        if not can_take(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y el rol Opinólogo pueden tomar tareas.")
        t = await self._task(interaction)
        if t:
            await self._after(interaction, await self.run(edb.take, t.id, interaction.user.id), "✅ Tomaste la tarea.")

    async def on_release(self, interaction: discord.Interaction):
        t = await self._task(interaction)
        if t:
            await self._after(interaction, await self.run(edb.release, t.id, interaction.user.id), "↩️ Soltaste la tarea.")

    async def on_done(self, interaction: discord.Interaction):
        t = await self._task(interaction)
        if t:
            override = is_staff_or_council(interaction.user)
            await self._after(interaction, await self.run(edb.finish, t.id, interaction.user.id, override), "✅ Tarea terminada.")

    async def log_done(self, t: edb.Task) -> bool:
        """Deja el registro de la tarea terminada en el canal de logs. False si no se pudo (entonces el panel NO se borra)."""
        try:
            ch = await self._channel(TASK_LOG_CHANNEL_ID)
            if ch is None:
                return False
            await ch.send(content=f"✅ Tarea #{t.id} terminada por <@{t.done_by}>", embed=self.embed(t), allowed_mentions=NO_MENTIONS)
            return True
        except Exception:
            traceback.print_exc()
            return False

    async def complete_by_source(self, source: str, user_id: int) -> bool:
        """Termina sola una tarea automática (p. ej. cargar el killboard de una CTA) y actualiza su panel. False si no hay tarea abierta."""
        t = await self.run(edb.find_by_source, source)
        if t is None:
            return False
        res = await self.run(edb.finish, t.id, user_id, True)
        if res.status != "ok":
            return False
        try:
            ch = self.bot.get_channel(res.task.channel_id) or await self.bot.fetch_channel(res.task.channel_id)
            msg = await ch.fetch_message(res.task.message_id)
            if await self.log_done(res.task):
                await msg.delete()
                return True
            await msg.edit(embed=self.embed(res.task), view=None, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"Tarea {t.id}: terminada pero no pude actualizar el panel: {exc!r}")
        return True

    # ---------- publicación programada ----------
    @tasks.loop(seconds=10)
    async def ticker(self):
        try:
            await self.process_due()
        except Exception:
            traceback.print_exc()

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    async def process_due(self, now: Optional[float] = None) -> int:
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        done = 0
        for tid in await self.run(edb.due_for_publish, now):
            t = await self.run(edb.get_task, tid)
            late = bool(t and t.publish_at and now - t.publish_at > 120)
            if await self.publish(tid, late=late):
                done += 1
        return done


async def setup_encargos(bot: commands.Bot, db_path: str, guild_id: int) -> EncargosCog:
    cog = EncargosCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
