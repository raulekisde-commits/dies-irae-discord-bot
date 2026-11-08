"""/tarea — tareas para el staff: panel con botones Terminada / Cancelar, y publicación programada."""
import asyncio
import os
import re
import traceback
import uuid
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
bs.register("task_log_cmd", "Registro de tareas de /tarea", lambda: bs.get("task_log"),
            "Registro de las tareas creadas con /tarea crear (si no se configura, van al registro de tareas)")
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


TAGGED_PREFIX = "tarea:"                                    # source de las tareas de /tarea con tags: solo los etiquetados + Council
TAG_RE = re.compile(r"<@([!&]?)(\d+)>")


PHOTO_SUPPORTED = hasattr(discord.ui, "FileUpload") and hasattr(discord.ui, "Label")     # subir archivos en un formulario: discord.py 2.6+


def is_tagged(t) -> bool:
    return (getattr(t, "source", None) or "").startswith(TAGGED_PREFIX) and bool(t.ping_users or t.ping_roles)


def from_command(t) -> bool:
    """Creada a mano (/tarea crear, o la vieja /encargar): su registro va al canal de /canal logs tareas."""
    src = getattr(t, "source", None) or ""
    return not src or src.startswith(TAGGED_PREFIX)


def is_auto(t) -> bool:
    """Automáticas: se cierran solas cuando el bot detecta que se hicieron (sin botón). Revisar el combat log no tiene detector: sigue a mano."""
    src = getattr(t, "source", None) or ""
    return bool(src) and not src.startswith(TAGGED_PREFIX) and not src.endswith(":combatlog_review")


def can_work_on(member, t) -> bool:
    """Quién puede terminar una tarea: las comunes, el Staff/Council/Opinólogo/Coaching; las que son «para» un rol, ese rol y el Council;
    las de /tarea con tags, las personas y roles etiquetados y el Council."""
    if is_tagged(t):
        return can_manage(member) or member.id in t.ping_users or bool(set(t.ping_roles) & _roles(member))
    if getattr(t, "for_role", None):
        return can_manage(member) or t.for_role in _roles(member)
    return can_take(member)


class TaskView(discord.ui.View):
    """Botones persistentes: la tarea se busca por el id del mensaje, así sirven aunque el bot se reinicie."""
    def __init__(self, cog: "EncargosCog", done: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        if not done:                                   # tareas automáticas: sin botón «Tarea terminada»
            self.remove_item(self.done)

    @discord.ui.button(label="Tarea terminada", style=discord.ButtonStyle.primary, custom_id="task:done", emoji="✅")
    async def done(self, interaction: discord.Interaction, button):
        await self.cog.on_done(interaction)

    @discord.ui.button(label="Cancelar tarea", style=discord.ButtonStyle.danger, custom_id="task:cancel", emoji="🗑️")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel(interaction)


class PhotoModal(discord.ui.Modal):
    """Formulario para terminar una tarea que pide foto (solo se usa si PHOTO_SUPPORTED)."""
    def __init__(self, cog: "EncargosCog", task_id: int):
        super().__init__(title=f"Terminar tarea #{task_id}"[:45], timeout=600)
        self.cog, self.task_id = cog, task_id
        self.upload = discord.ui.FileUpload(min_values=1, max_values=1, required=True)
        self.add_item(discord.ui.Label(text="Foto de la tarea terminada", component=self.upload))

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.finish_with_photo(interaction, self.task_id, list(getattr(self.upload, "values", None) or []))


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
        elif is_tagged(t):
            e.add_field(name="Para", value=" ".join([f"<@&{r}>" for r in t.ping_roles] + [f"<@{u}>" for u in t.ping_users])[:1024] or "—", inline=True)
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
            msg = await ch.send(content=f"{tags} nueva tarea{note}".strip(), embed=self.embed(t), view=TaskView(self, done=not is_auto(t)),
                                allowed_mentions=allowed)
        except Exception:
            traceback.print_exc()
            await self.run(edb.unclaim_publish, task_id)          # se reintenta en el próximo ciclo
            return False
        await self.run(edb.mark_published, task_id, msg.id)
        return True

    async def create(self, *, text: str, created_by: int, publish_at: Optional[float] = None, source: Optional[str] = None,
                     link: Optional[str] = None, channel_id: Optional[int] = None, ping_roles=(), ping_users=(), ping_staff: bool = True,
                     for_role: Optional[int] = None, require_photo: bool = False) -> edb.Task:
        """Crea una tarea (la usan /tarea y los avisos automáticos, como el del combatlog de una CTA).
        Etiqueta al Staff (salvo ping_staff=False) y, además, a los roles y personas indicados."""
        t = await self.run(edb.create_task, guild_id=self.guild_id, channel_id=channel_id or bs.get("tasks"), text=text, created_by=created_by,
                           publish_at=publish_at, source=source, link=link, ping_roles=ping_roles, ping_users=ping_users, ping_staff=ping_staff,
                           for_role=for_role, require_photo=require_photo)
        if t.status == "scheduled" and (t.publish_at or 0) <= datetime.now(timezone.utc).timestamp():
            await self.publish(t.id)
        return await self.run(edb.get_task, t.id)

    # ---------- comando ----------
    tarea = app_commands.Group(name="tarea", description="Tareas del staff", guild_only=True)

    @tarea.command(name="crear", description="(Council) Crea una tarea: panel con botón Tarea terminada")
    @app_commands.describe(titulo="Título de la tarea", descripcion="Qué hay que hacer",
                           tags="(Opcional) Personas y/o roles: @persona @rol … Solo ellos (y el Council) la pueden terminar. Sin tags se etiqueta al Staff",
                           foto="(Opcional) Para terminarla hay que subir una foto de la tarea hecha (queda en el log)",
                           cuando="(Opcional) Cuándo publicarla: HH:MM en UTC (ej: 22:30) o +90m / +2h")
    async def crear(self, interaction: discord.Interaction, titulo: str, descripcion: str, tags: Optional[str] = None,
                    foto: bool = False, cuando: Optional[str] = None):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede crear tareas.")
        if foto and not PHOTO_SUPPORTED:
            return await _say(interaction, "❌ La opción `foto` necesita discord.py 2.6 o más nuevo en el servidor del bot. No creé la tarea.")
        roles, users = [], []
        for kind, raw in TAG_RE.findall(tags or ""):
            if kind == "&":
                role = interaction.guild.get_role(int(raw)) if interaction.guild else None
                if role is None or role.id == interaction.guild.id:              # el id de @everyone es el del servidor
                    return await _say(interaction, "❌ Uno de los roles no existe en el servidor (o es @everyone).")
                roles.append(role.id)
            else:
                users.append(int(raw))
        if tags and tags.strip() and not (roles or users):
            return await _say(interaction, "❌ En `tags` mencioná personas o roles (ej: `@Juan @Council`).")
        roles, users = list(dict.fromkeys(roles)), list(dict.fromkeys(users))
        tagged = bool(roles or users)
        try:
            at = edb.parse_when(cuando) if cuando and cuando.strip() else None
            t = await self.create(text=f"**{titulo.strip()}** — {descripcion.strip()}", created_by=interaction.user.id, publish_at=at,
                                  ping_roles=roles, ping_users=users, ping_staff=not tagged, require_photo=foto,
                                  source=f"{TAGGED_PREFIX}{uuid.uuid4().hex[:12]}")
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
                                                    view=None if res.task.status == "done" else TaskView(self, done=not is_auto(res.task)),
                                                    allowed_mentions=NO_MENTIONS)
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
            # sin botón Tomar: la termina directamente quien tenga permiso sobre la tarea
            if is_auto(t):
                return await _say(interaction, "🤖 Esta tarea se cierra sola cuando el bot detecta que se hizo.")
            if not can_work_on(interaction.user, t):
                return await _say(interaction, "🔒 No podés terminar esta tarea.")
            if t.require_photo and t.status not in ("done", "cancelled"):
                if not PHOTO_SUPPORTED:
                    return await _say(interaction, "❌ Esta tarea pide foto y el bot no puede recibir archivos (falta actualizar discord.py).")
                return await interaction.response.send_modal(PhotoModal(self, t.id))
            await self._after(interaction, await self.run(edb.finish, t.id, interaction.user.id, True), "✅ Tarea terminada.")

    async def finish_with_photo(self, interaction: discord.Interaction, task_id: int, files) -> None:
        """Envío del formulario de foto: valida la imagen, cierra la tarea y la deja en el log con la foto."""
        img = next((a for a in files if (getattr(a, "content_type", None) or "").startswith("image/")), None)
        if img is None:
            return await _say(interaction, "❌ Tiene que ser una imagen. La tarea sigue abierta.")
        t = await self.run(edb.get_task, task_id)
        if t is None:
            return await _say(interaction, "❌ No encuentro esa tarea.")
        if not can_work_on(interaction.user, t):
            return await _say(interaction, "🔒 No podés terminar esta tarea.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ext = os.path.splitext(getattr(img, "filename", "") or "")[1].lower() or ".png"
            photo = await img.to_file(filename=f"tarea_{task_id}{ext}")
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude descargar la foto. Probá de nuevo; la tarea sigue abierta.")
        res = await self.run(edb.finish, task_id, interaction.user.id, True)
        if res.status != "ok":
            return await _say(interaction, {"closed": "ℹ️ Esta tarea ya está terminada."}.get(res.status, "❌ No se pudo."))
        if await self.log_done(res.task, photo):
            await self.drop_thread(res.task)
            try:
                if interaction.message is not None:
                    await interaction.message.delete()
            except Exception:
                traceback.print_exc()
            return await _say(interaction, "✅ Tarea terminada. Quedó registrada con la foto en el canal de logs.")
        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=self.embed(res.task), view=None, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        await _say(interaction, "✅ Tarea terminada, pero no pude dejarla en el canal de logs.")

    async def canal_log_tareas(self, interaction: discord.Interaction, canal) -> None:
        """/canal logs tareas: dónde se registran las tareas creadas con /tarea crear."""
        if interaction.guild is None or not can_manage(interaction.user):
            return await _say(interaction, "❌ Solo el rol **Council**, el propietario o los administradores pueden usar este comando.")
        channel = canal or interaction.channel
        if channel is None or not hasattr(channel, "send"):
            return await _say(interaction, "❌ No puedo usar ese canal.")
        try:
            await channel.send("✅ Canal de registro de **tareas** configurado: acá se anota cada tarea de `/tarea crear` terminada o cancelada.",
                               allowed_mentions=NO_MENTIONS)
        except Exception:
            return await _say(interaction, f"❌ No tengo permiso para escribir en {channel.mention}. No cambié nada.")
        try:
            await asyncio.to_thread(bs.set_value, "task_log_cmd", channel.id, interaction.user.id)
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude guardar el canal.")
        await _say(interaction, f"✅ Registro de tareas de `/tarea crear` configurado en {channel.mention}.")

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
            log = await self._channel(bs.get("task_log_cmd" if from_command(t) else "task_log"))
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

    async def log_done(self, t: edb.Task, photo=None) -> bool:
        """Deja el registro de la tarea terminada en el canal de logs (las de /tarea crear, en el suyo). False si no se pudo
        (entonces el panel NO se borra)."""
        try:
            ch = await self._channel(bs.get("task_log_cmd" if from_command(t) else "task_log"))
            if ch is None:
                return False
            reward = await self.grant_task_spin(t)
            emb = self.embed(t)
            kw = {}
            if photo is not None:
                emb.set_image(url=f"attachment://{photo.filename}")
                kw["file"] = photo
            await ch.send(content=f"✅ Tarea #{t.id} terminada por <@{t.done_by}>{reward}", embed=emb, allowed_mentions=NO_MENTIONS, **kw)
            return True
        except Exception:
            traceback.print_exc()
            return False

    async def grant_task_spin(self, t: edb.Task) -> str:
        """1 tirada de Wujupón a quien terminó la tarea. Idempotente por tarea (request_key task:<id>). Nunca rompe el cierre."""
        if not t.done_by:
            return ""
        try:
            cog = self.bot.get_cog("WujuponCog")
            if cog is None:
                return " · ⚠️ sin tirada (Wujupón no cargado)"
            import wujupon_db as wdb
            actor = self.bot.user.id if getattr(self.bot, "user", None) else 0
            after = await cog.run(wdb.gift_spins, actor_id=actor, user_id=t.done_by, count=1,
                                  request_key=f"task:{t.id}", reason=f"Tarea #{t.id} terminada")
            return f" · 🎰 +1 tirada de Wujupón (ahora tiene {after})"
        except Exception:
            traceback.print_exc()
            return " · ⚠️ no se pudo dar la tirada"

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
