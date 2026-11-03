"""/encargar — tareas para el staff: panel con botones Tomar / Soltar / Terminada, y publicación programada."""
import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import botsettings as bs
import encargos_db as edb
from paging import PagedView, chunk
from guildshop import can_manage, COUNCIL_ROLE_ID

NO_MENTIONS = discord.AllowedMentions.none()
TASK_LOG_CHANNEL_ID = int(os.getenv("TASK_LOG_CHANNEL_ID", "1566014626309873776"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
TASKS_CHANNEL_ID = int(os.getenv("TASKS_CHANNEL_ID", "1565986098679644270"))
bs.register("task_log", "Registro de tareas terminadas", TASK_LOG_CHANNEL_ID, "Acá queda el registro de cada tarea terminada o cancelada")
bs.register("tasks", "Panel de tareas", TASKS_CHANNEL_ID, "Canal donde se publican las tareas del Staff")
COACHING_ROLE_ID = int(os.getenv("COACHING_ROLE_ID", "1566691393726980167"))
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
    """Pueden tomar una tarea: Staff, Council, administradores, el rol Opinólogo y el rol Coaching."""
    return is_staff_or_council(member) or bool({OPINOLOGO_ROLE_ID, COACHING_ROLE_ID} & _roles(member))


def can_work_on(member, t) -> bool:
    """Quién puede tomar y terminar una tarea: las comunes, el Staff/Council/Opinólogo/Coaching; las que son «para» un rol, ese rol y el Council."""
    if getattr(t, "for_role", None):
        return can_manage(member) or t.for_role in _roles(member)
    return can_take(member)


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

    @discord.ui.button(label="Cancelar tarea", style=discord.ButtonStyle.danger, custom_id="task:cancel", emoji="🗑️")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel(interaction)


class ConfirmCancelTaskView(discord.ui.View):
    """Confirmación privada antes de cancelar una tarea (evita un clic por error)."""
    def __init__(self, cog: "EncargosCog", task_id: int, user_id: int):
        super().__init__(timeout=60.0)
        self.cog, self.task_id, self.user_id = cog, task_id, user_id
        ok = discord.ui.Button(label="Sí, cancelar la tarea", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="No", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _say(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _ok(self, interaction: discord.Interaction):
        self.stop()
        await self.cog.confirm_cancel(interaction, self.task_id)

    async def _no(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Listo, la tarea sigue como estaba.", view=None)


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
        if t.for_role:
            e.add_field(name="Para", value=f"<@&{t.for_role}>", inline=True)
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
            roles = ([STAFF_ROLE_ID] if t.ping_staff else []) + [r for r in t.ping_roles if r != STAFF_ROLE_ID or not t.ping_staff]
            tags = " ".join([f"<@&{r}>" for r in roles] + [f"<@{u}>" for u in t.ping_users])
            allowed = discord.AllowedMentions(users=[discord.Object(id=u) for u in t.ping_users], roles=[discord.Object(id=r) for r in roles], everyone=False)
            msg = await ch.send(content=f"{tags} nueva tarea{note}".strip(), embed=self.embed(t), view=TaskView(self), allowed_mentions=allowed)
        except Exception:
            traceback.print_exc()
            await self.run(edb.unclaim_publish, task_id)          # se reintenta en el próximo ciclo
            return False
        await self.run(edb.mark_published, task_id, msg.id)
        return True

    async def create(self, *, text: str, created_by: int, publish_at: Optional[float] = None, source: Optional[str] = None,
                     link: Optional[str] = None, channel_id: Optional[int] = None, ping_roles=(), ping_users=(), ping_staff: bool = True,
                     for_role: Optional[int] = None) -> edb.Task:
        """Crea una tarea (la usan /encargar y los avisos automáticos, como el del combatlog de una CTA).
        Etiqueta al Staff (salvo ping_staff=False) y, además, a los roles y personas indicados."""
        t = await self.run(edb.create_task, guild_id=self.guild_id, channel_id=channel_id or bs.get("tasks"), text=text, created_by=created_by,
                           publish_at=publish_at, source=source, link=link, ping_roles=ping_roles, ping_users=ping_users, ping_staff=ping_staff,
                           for_role=for_role)
        if t.status == "scheduled" and (t.publish_at or 0) <= datetime.now(timezone.utc).timestamp():
            await self.publish(t.id)
        return await self.run(edb.get_task, t.id)

    # ---------- comando ----------
    @app_commands.command(name="encargar", description="Crea una tarea para el staff (panel con botones y mención al Staff)")
    @app_commands.describe(tarea="Qué hay que hacer", cuando="(Opcional) Cuándo publicarla: HH:MM en UTC (ej: 22:30) o +90m / +2h",
                           rol="(Opcional) Rol a etiquetar además del Staff", persona="(Opcional) Persona a etiquetar además del Staff",
                           para_rol="(Opcional) Tarea PARA este rol (ej: Council): se etiqueta solo a ese rol y solo ese rol y el Council la pueden tomar")
    @app_commands.guild_only()
    async def encargar(self, interaction: discord.Interaction, tarea: str, cuando: Optional[str] = None,
                       rol: Optional[discord.Role] = None, persona: Optional[discord.Member] = None, para_rol: Optional[discord.Role] = None):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden encargar tareas.")
        try:
            at = edb.parse_when(cuando) if cuando and cuando.strip() else None
            if para_rol is not None and para_rol.is_default():
                return await _say(interaction, "❌ Elegí un rol del servidor (no @everyone).")
            pings = [rol.id] if rol is not None and not rol.is_default() else []
            if para_rol is not None:
                pings = list(dict.fromkeys(pings + [para_rol.id]))
            t = await self.create(text=tarea, created_by=interaction.user.id, publish_at=at, ping_roles=pings,
                                  ping_users=[persona.id] if persona is not None else [],
                                  ping_staff=para_rol is None, for_role=para_rol.id if para_rol is not None else None)
        except edb.TaskError as exc:
            return await _say(interaction, f"❌ {exc}")
        if at and at > datetime.now(timezone.utc).timestamp():
            return await _say(interaction, f"⏰ Tarea #{t.id} programada: se publica <t:{int(at)}:R> (<t:{int(at)}:f>) en <#{t.channel_id}>.")
        await _say(interaction, f"✅ Tarea #{t.id} publicada en <#{t.channel_id}>." if t.status != "scheduled"
                   else f"⚠️ Tarea #{t.id} guardada, pero no pude publicarla ahora; se reintenta sola.")

    async def send_top_coach(self, interaction: discord.Interaction):
        """Lo usa `/top coach`: quiénes dan más devoluciones (cierran más tareas de coaching)."""
        from coaching_db import week_start
        since = week_start().isoformat()
        total = await self.run(edb.top_coaches, 10)
        week = await self.run(edb.top_coaches, 10, since)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        def lines(rows):
            return "\n".join(f"{medals.get(i, f'`{i}.`')} <@{u}> — **{n}**" for i, (u, n) in enumerate(rows, start=1)) or "Todavía no hay datos."
        emb = discord.Embed(title="🎓 Top coaches", color=discord.Color.gold(),
                            description="Quienes más devoluciones dan: tareas de coaching cerradas (a mano o respondiendo en el post del jugador).")
        emb.add_field(name="🏆 Total", value=lines(total), inline=True)
        emb.add_field(name="📅 Esta semana", value=lines(week), inline=True)
        await interaction.response.send_message(embed=emb, allowed_mentions=NO_MENTIONS)

    async def send_top(self, interaction: discord.Interaction):
        """Lo usa `/top tareas`: quién finalizó más tareas del staff, de a 10 por página (botón Siguiente)."""
        if not can_take(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y el rol Opinólogo pueden ver este ranking.")
        _, done = await self.run(edb.top_users, 1000)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        pages = chunk(list(enumerate(done, start=1)), 10)
        embeds = []
        for n, page in enumerate(pages, start=1):
            emb = discord.Embed(title="🏆 Top tareas — finalizadas", color=discord.Color.gold(),
                                description="\n".join(f"{medals.get(i, f'`{i}.`')} <@{u}> — **{c}**" for i, (u, c) in page)
                                or "Todavía nadie finalizó tareas.")
            if len(pages) > 1:
                emb.set_footer(text=f"Página {n}/{len(pages)}")
            embeds.append(emb)
        if len(embeds) > 1:
            view = PagedView(embeds, owner_id=interaction.user.id)
            await interaction.response.send_message(embed=embeds[0], view=view, allowed_mentions=NO_MENTIONS)
            try:
                view.message = await interaction.original_response()
            except Exception:
                pass
        else:
            await interaction.response.send_message(embed=embeds[0], allowed_mentions=NO_MENTIONS)

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
            await self.drop_thread(res.task)
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
        t = await self._task(interaction)
        if t and not can_work_on(interaction.user, t):
            if t.for_role:
                return await _say(interaction, f"🔒 Esta tarea es solo para el rol <@&{t.for_role}> (y el Council).")
            return await _say(interaction, "🔒 Solo el Staff, el Council y los roles Opinólogo y Coaching pueden tomar tareas.")
        if t:
            res = await self.run(edb.take, t.id, interaction.user.id)
            await self._after(interaction, res, "✅ Tomaste la tarea.")
            if res.status == "ok":
                await self.open_thread(res.task, interaction.message)

    # ---------- hilo de coordinación + aviso cada hora ----------
    async def open_thread(self, t: edb.Task, msg) -> None:
        """Crea (o reutiliza) el hilo de la tarea y etiqueta a quien la tomó. Si no se puede crear, la tarea sigue igual."""
        if (t.source or "").startswith("coach:"):          # las devoluciones de coaching se hacen en el post del foro: sin hilo aparte
            return
        try:
            thread = await self._channel(t.thread_id) if t.thread_id else None
            if thread is None and msg is not None:
                thread = await msg.create_thread(name=f"Tarea #{t.id}"[:100], auto_archive_duration=1440)
                await self.run(edb.set_thread, t.id, thread.id)
            if thread is not None:
                await thread.send(f"✋ <@{t.taker_id}> tomó la **tarea #{t.id}**. Coordinen por acá. "
                                  "Les aviso cada hora hasta que la cierren con el botón **Tarea terminada**.",
                                  allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=t.taker_id)], roles=False, everyone=False))
        except Exception as exc:
            print(f"Tarea {t.id}: no pude crear el hilo de coordinación: {exc!r}")

    async def drop_thread(self, t: edb.Task) -> None:
        """Al cerrar la tarea el hilo ya no hace falta (el registro queda en el canal de logs)."""
        if not t.thread_id:
            return
        try:
            th = await self._channel(t.thread_id)
            if th is not None:
                await th.delete()
        except Exception as exc:
            print(f"Tarea {t.id}: no pude borrar el hilo: {exc!r}")

    async def remind_taken(self, now: Optional[float] = None) -> int:
        """Cada hora etiqueta a quien tiene la tarea tomada y le recuerda cerrarla si ya terminó."""
        n = 0
        for tid in await self.run(edb.due_reminders, now):
            t = await self.run(edb.get_task, tid)
            if t is None or t.status != "taken" or not t.taker_id:
                continue
            target = await self._channel(t.thread_id) if t.thread_id else None
            if target is None:
                target = await self._channel(t.channel_id)
            if target is None:
                continue
            try:
                link = f"https://discord.com/channels/{t.guild_id}/{t.channel_id}/{t.message_id}" if t.message_id else ""
                await target.send(f"⏰ <@{t.taker_id}> la **tarea #{t.id}** sigue abierta. Si ya terminaron, cerrala con el botón "
                                  f"**Tarea terminada**{' (' + link + ')' if link else ''}.\n> {t.text[:200]}",
                                  allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=t.taker_id)], roles=False, everyone=False))
                n += 1
            except Exception as exc:
                print(f"Tarea {tid}: no pude mandar el recordatorio: {exc!r}")
            await self.run(edb.mark_reminded, tid, now)             # aunque falle, no insistir cada 10 s
        return n

    async def on_release(self, interaction: discord.Interaction):
        t = await self._task(interaction)
        if t:
            await self._after(interaction, await self.run(edb.release, t.id, interaction.user.id), "↩️ Soltaste la tarea.")

    async def on_done(self, interaction: discord.Interaction):
        t = await self._task(interaction)
        if t:
            # las devoluciones de coaching las puede cerrar cualquiera que pueda tomarlas (Coaching, Opinólogo, Staff, Council)
            if t.for_role:
                override = can_work_on(interaction.user, t)
            else:
                override = is_staff_or_council(interaction.user) or ((t.source or "").startswith("coach:") and can_take(interaction.user))
            await self._after(interaction, await self.run(edb.finish, t.id, interaction.user.id, override), "✅ Tarea terminada.")

    async def record_done(self, text: str, actor_id: int, source: str) -> bool:
        """Cuenta como tarea hecha (para /top tareas) algo que el Staff resolvió sin pasar por el panel de tareas. Nunca rompe a quien la llama."""
        try:
            return await self.run(edb.record_done, guild_id=self.guild_id, channel_id=TASKS_CHANNEL_ID, text=text, actor_id=actor_id, source=source)
        except Exception:
            traceback.print_exc()
            return False

    # ---------- cancelar tareas ----------
    async def on_cancel(self, interaction: discord.Interaction):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden cancelar tareas.")
        t = await self._task(interaction)
        if t is None:
            return
        if t.status in ("done", "cancelled"):
            return await _say(interaction, "ℹ️ Esta tarea ya está cerrada.")
        await interaction.response.send_message(f"¿Cancelar la **tarea #{t.id}**? Se borra el panel y no cuenta como terminada.\n> {t.text[:200]}",
                                                view=ConfirmCancelTaskView(self, t.id, interaction.user.id), ephemeral=True,
                                                allowed_mentions=NO_MENTIONS)

    async def confirm_cancel(self, interaction: discord.Interaction, task_id: int):
        if not is_staff_or_council(interaction.user):                       # se revisa de nuevo al confirmar
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden cancelar tareas.")
        res = await self.run(edb.cancel, task_id, interaction.user.id)
        if res.status == "closed":
            return await interaction.response.edit_message(content="ℹ️ Esa tarea ya estaba cerrada.", view=None)
        if res.status != "ok":
            return await interaction.response.edit_message(content="❌ No encuentro esa tarea.", view=None)
        await self.after_cancel(res.task, f"cancelada por <@{interaction.user.id}>")
        await interaction.response.edit_message(content="🗑️ Tarea cancelada.", view=None)

    async def after_cancel(self, t: edb.Task, who: str) -> None:
        """Borra el panel y el hilo de una tarea ya cancelada y deja el registro en el canal de logs."""
        await self.drop_thread(t)
        if t.message_id:
            try:
                ch = await self._channel(t.channel_id)
                msg = await ch.fetch_message(t.message_id)
                await msg.delete()
            except Exception as exc:
                print(f"Tarea {t.id}: cancelada pero no pude borrar el panel: {exc!r}")
        try:
            log = await self._channel(bs.get("task_log"))
            if log is not None:
                await log.send(f"🗑️ Tarea #{t.id} {who}.\n> {t.text[:300]}", allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def cancel_by_prefix(self, prefix: str, actor_id: int, why: str) -> int:
        """Cancela todas las tareas abiertas cuyo `source` empieza con `prefix` (p. ej. las de una CTA cancelada). Devuelve cuántas."""
        n = 0
        for tid in await self.run(edb.open_by_source_prefix, prefix):
            res = await self.run(edb.cancel, tid, actor_id)
            if res.status == "ok":
                await self.after_cancel(res.task, why)
                n += 1
        return n

    async def log_done(self, t: edb.Task) -> bool:
        """Deja el registro de la tarea terminada en el canal de logs. False si no se pudo (entonces el panel NO se borra)."""
        try:
            ch = await self._channel(bs.get("task_log"))
            if ch is None:
                return False
            await ch.send(content=f"✅ Tarea #{t.id} terminada por <@{t.done_by}>", embed=self.embed(t), allowed_mentions=NO_MENTIONS)
            return True
        except Exception:
            traceback.print_exc()
            return False

    async def update_text_by_source(self, source: str, text: str) -> bool:
        """Cambia el texto de una tarea abierta (y su panel). False si no hay tarea o ya está terminada."""
        t = await self.run(edb.find_by_source, source)
        if t is None or t.status in ("done", "cancelled"):
            return False
        await self.run(edb.set_text, t.id, text)
        t = await self.run(edb.get_task, t.id)
        if t.message_id:
            try:
                ch = await self._channel(t.channel_id)
                msg = await ch.fetch_message(t.message_id)
                await msg.edit(embed=self.embed(t), allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"Tarea {t.id}: no pude actualizar el panel: {exc!r}")
        return True

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
                await self.drop_thread(res.task)
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
        try:
            await self.remind_taken()
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
