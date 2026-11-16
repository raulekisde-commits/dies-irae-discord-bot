"""Coaching: panel con botón que abre un post por persona en el foro de VODs, tareas de devolución para el rol Coaching
y control de los 3 VODs semanales del rol Observación (/coaching reporte, /coaching mis-vods)."""
import asyncio
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import coaching_db as cdb
import ctautil as cu
import encargos_db as edb
from encargos import can_take, is_staff_or_council
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
COACHING_ROLE_ID = int(os.getenv("COACHING_ROLE_ID", "1566691393726980167"))
FORUM_CHANNEL_ID = int(os.getenv("COACHING_FORUM_ID", "1566695879656607924"))
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
COACHING_TASKS_CHANNEL_ID = int(os.getenv("COACHING_TASKS_CHANNEL_ID", "1566710577173102602"))     # canal donde salen las tareas de devolución
ROLE_LABELS = [n for n in cu.PLAYED_ROLES.values() if n != "Bomb"]
SHORT_REPLY_MIN = 100                                      # devoluciones con menos caracteres: las revisa el Staff antes de dar la tirada
STAFF_PING = discord.AllowedMentions(users=False, roles=[discord.Object(id=STAFF_ROLE_ID)], everyone=False)
# Revisión única al subir el cambio: devoluciones ya cobradas desde esta hora (UTC) que tienen una «X» en el texto
RETRO_FLAG = "retro_x_review_2026_11"
RETRO_SINCE = datetime(2026, 11, 16, 1, 13, tzinfo=timezone.utc)
X_RE = re.compile(r"(?<!\w)[xX❌✖❎](?!\w)")


def _roles(m) -> set:
    return {r.id for r in getattr(m, "roles", [])}


def can_report(m) -> bool:
    """Ven el reporte del coaching: Council/administradores, Staff, Coaching y Opinólogo."""
    return can_manage(m) or bool({STAFF_ROLE_ID, COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} & _roles(m))


def is_obs(m) -> bool:
    return OBSERVACION_ROLE_ID in _roles(m)


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def task_text(uid: int, obs: bool, role: Optional[str], n: int) -> str:
    kind = "Observación" if obs else "**Miembro** (no es Observación: no cuenta para el mínimo semanal)"
    return (f"Dar devolución de {n} VOD(s) de <@{uid}> · {kind} · Rol que juega: **{role or 'sin indicar (esperando que lo elija)'}**")


class CoachPanelView(discord.ui.View):
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Abrir mi post de VODs", style=discord.ButtonStyle.primary, custom_id="coach:open", emoji="🎥")
    async def open(self, interaction: discord.Interaction, button):
        await self.cog.open_post(interaction)


class CreateMissingView(discord.ui.View):
    """Botón de /coaching verificar-posts: crea los posts faltantes (solo quien corrió el comando, una vez)."""
    def __init__(self, cog, owner_id: int, members: list):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.members = cog, owner_id, members

    @discord.ui.button(label="Crear posts faltantes", style=discord.ButtonStyle.success, emoji="🎥")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("🔒 Este botón es de quien corrió el comando.", ephemeral=True)
        button.disabled = True
        self.stop()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await self.cog.create_missing(interaction, self.members)


class CoachRoleView(discord.ui.View):
    """Menú persistente que acompaña a un VOD sin rol indicado: el mensaje del VOD es al que responde el bot."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(custom_id="coach:rolesel", placeholder="¿Con qué rol jugás en este VOD?", min_values=1, max_values=1,
                       options=[discord.SelectOption(label=n, value=n) for n in ROLE_LABELS])
    async def pick(self, interaction: discord.Interaction, select):
        await self.cog.on_role_pick(interaction, interaction.data["values"][0])


class CoachReviewView(discord.ui.View):
    """Votación persistente del Staff sobre una devolución corta: hasta que se vote, la tarea sigue abierta y no hay tirada."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Sí, está mal hecha", style=discord.ButtonStyle.danger, custom_id="coach:rev_bad", emoji="❌")
    async def bad(self, interaction: discord.Interaction, button):
        await self.cog.on_review_vote(interaction, bad=True)

    @discord.ui.button(label="No, está bien", style=discord.ButtonStyle.success, custom_id="coach:rev_ok", emoji="✅")
    async def ok(self, interaction: discord.Interaction, button):
        await self.cog.on_review_vote(interaction, bad=False)


class CoachingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self.run(cdb.init_schema)
        self.bot.add_view(CoachPanelView(self))
        self.bot.add_view(CoachRoleView(self))
        self.bot.add_view(CoachReviewView(self))
        self._retro_task = asyncio.create_task(self._retro_when_ready())

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    async def _channel(self, cid: int):
        ch = self.bot.get_channel(cid)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(cid)
        except Exception:
            return None

    # ---------- panel y post ----------
    async def coaching_panel(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden publicar este panel.")
        emb = discord.Embed(
            title="🎥 Coaching · tus VODs",
            description=(f"Apretá el botón para abrir **tu post** en <#{FORUM_CHANNEL_ID}> (es uno solo por persona).\n"
                         f"Ahí subís tus VODs (links o videos) y el equipo de coaching te da la devolución.\n\n"
                         f"• Quienes tienen el rol <@&{OBSERVACION_ROLE_ID}> tienen que subir **mínimo {cdb.WEEKLY_MIN} VODs por semana** "
                         f"(la semana va de lunes a domingo, hora UTC).\n"
                         f"• Los miembros también pueden subir VODs para pedir devolución; a ellos no se les exige el mínimo.\n"
                         f"• Con `/coaching mis-vods` ves cuántos llevás y cuántos te faltan."),
            color=discord.Color.blurple())
        await interaction.channel.send(embed=emb, view=CoachPanelView(self), allowed_mentions=NO_MENTIONS)
        await _say(interaction, "✅ Panel publicado.")

    def _lock(self, uid: int) -> asyncio.Lock:
        return self._locks.setdefault(uid, asyncio.Lock())

    async def open_post(self, interaction: discord.Interaction):
        user = interaction.user
        if not isinstance(user, discord.Member) or not ({OBSERVACION_ROLE_ID, MIEMBRO_ROLE_ID} & _roles(user)):
            return await _say(interaction, "🔒 Solo los miembros y quienes tienen el rol Observación pueden abrir un post.")
        await interaction.response.defer(ephemeral=True)
        async with self._lock(user.id):
            post = await self.run(cdb.get_post, user.id)
            if post:
                th = await self._channel(post["thread_id"])
                if th is not None:
                    try:
                        if getattr(th, "archived", False):
                            await th.edit(archived=False)
                    except Exception:
                        pass
                    return await interaction.followup.send(f"🎥 Ya tenés tu post: {th.mention}", ephemeral=True)
            forum = await self._channel(FORUM_CHANNEL_ID)
            if forum is None or not hasattr(forum, "create_thread"):
                return await interaction.followup.send("❌ No encuentro el foro de VODs. Avisale a un administrador.", ephemeral=True)
            try:
                thread = await self._create_post(forum, user)
            except Exception as exc:
                print(f"coaching: no pude crear el post de {user.id}: {exc!r}")
                return await interaction.followup.send("❌ No pude crear tu post (el bot necesita permisos de *Crear posts* y *Gestionar hilos* en el foro).", ephemeral=True)
        await interaction.followup.send(f"🎥 Listo, este es tu post: {thread.mention}", ephemeral=True)

    async def _create_post(self, forum, user):
        """Crea el post de VODs de `user` (el primer mensaje lo menciona), lo guarda y lo agrega al hilo. Lanza si no puede crearlo."""
        quota = (f"Como tenés el rol **Observación**, tenés que subir **mínimo {cdb.WEEKLY_MIN} VODs por semana** (lunes a domingo, hora UTC)."
                 if is_obs(user) else "Subí tus VODs acá para pedir devolución.")
        content = (f"{user.mention} este es tu post de VODs. {quota}\n"
                   "Pegá el link (YouTube, Twitch, Medal…) o subí el video. Si podés, decí con qué rol jugás; si no, el bot te lo pregunta.")
        name = f"{user.display_name}"[:90] or f"VODs {user.id}"
        allowed = discord.AllowedMentions(users=[user], roles=False, everyone=False)
        try:
            res = await forum.create_thread(name=name, content=content, allowed_mentions=allowed)
        except discord.HTTPException:
            tags = list(getattr(forum, "available_tags", []) or [])[:1]       # foros que obligan a elegir una etiqueta
            if not tags:
                raise
            res = await forum.create_thread(name=name, content=content, applied_tags=tags, allowed_mentions=allowed)
        thread = getattr(res, "thread", res)
        await self.run(cdb.set_post, user.id, thread.id)
        try:
            await thread.add_user(user)
        except Exception:
            pass
        return thread

    CREATE_DELAY = 1.5          # segundos entre post y post al crearlos en lote (límite de Discord)

    async def create_missing(self, interaction: discord.Interaction, members: list):
        """Crea los posts faltantes (botón de /coaching verificar-posts). Se vuelve a chequear cada uno antes de crearlo."""
        if not can_report(interaction.user):
            return await interaction.response.send_message("🔒 No tenés permiso para crear posts.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        forum = await self._channel(FORUM_CHANNEL_ID)
        if forum is None or not hasattr(forum, "create_thread"):
            return await interaction.followup.send("❌ No encuentro el foro de VODs.", ephemeral=True)
        created, skipped, failed = [], 0, []
        for m in members:
            async with self._lock(m.id):
                post = await self.run(cdb.get_post, m.id)
                if post and await self._channel(post["thread_id"]) is not None:
                    skipped += 1
                    continue
                try:
                    th = await self._create_post(forum, m)
                    created.append(f"{m.mention} → {th.mention}")
                except Exception as exc:
                    print(f"coaching: no pude crear el post de {m.id}: {exc!r}")
                    failed.append(m.mention)
            await asyncio.sleep(self.CREATE_DELAY)
        out = f"✅ Posts creados: **{len(created)}** · ya tenían: **{skipped}** · fallaron: **{len(failed)}**"
        if created:
            out += "\n" + "\n".join(created)
        if failed:
            out += "\n❌ No pude crear: " + ", ".join(failed) + " (el bot necesita *Crear posts* y *Gestionar hilos* en el foro)."
        await interaction.followup.send(out[:1900], ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- VODs ----------
    async def process_message(self, message: discord.Message, edited: bool = False):
        if getattr(message.author, "bot", False) or getattr(message.channel, "parent_id", None) != FORUM_CHANNEL_ID:
            return
        post = await self.run(cdb.post_by_thread, message.channel.id)
        if post is None:
            return
        if int(post["user_id"]) != message.author.id:          # solo cuentan los videos que sube el dueño del post
            if not edited:
                await self.complete_on_reply(message)            # pero si responde alguien del equipo, la devolución queda dada
            return
        vods = cdb.extract_vods(message.content, getattr(message, "attachments", []))
        if not vods:
            return
        obs = is_obs(message.author)
        role = cdb.guess_role(message.content)
        new = await self.run(cdb.add_vods, message_id=message.id, thread_id=message.channel.id, user_id=message.author.id, is_obs=obs,
                             vods=vods, created_at=message.created_at, role_label=role)
        if not new:
            return
        row = await self.run(cdb.get_msg, message.id)
        role = row.get("role_label") or role
        text = task_text(message.author.id, bool(row["is_obs"]), role, int(row["vods"]))
        enc = self.bot.get_cog("EncargosCog")
        fresh = True
        if enc is not None:
            try:
                if await enc.update_text_by_source(f"coach:{message.id}", text):          # mensaje editado con más videos
                    fresh = False
                else:
                    await enc.create(text=text, created_by=message.author.id, source=f"coach:{message.id}", link=message.jump_url,
                                     ping_roles=[COACHING_ROLE_ID, OPINOLOGO_ROLE_ID], ping_staff=False,
                                     channel_id=COACHING_TASKS_CHANNEL_ID)
            except Exception as exc:
                print(f"coaching: no pude crear la tarea del VOD {message.id}: {exc!r}")
        try:
            await message.add_reaction("🎥")
        except Exception:
            pass
        if fresh and not role:
            try:
                await message.reply("🎭 ¿Con qué rol jugás en este VOD? Elegilo para que el coach lo sepa.", view=CoachRoleView(self),
                                    mention_author=False, allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"coaching: no pude preguntar el rol: {exc!r}")

    async def complete_on_reply(self, message: discord.Message):
        """Un coach/opinólogo/staff/council responde en el post: se completa la tarea del VOD al que responde, o todas las pendientes del post."""
        if not can_take(message.author):
            return
        enc = self.bot.get_cog("EncargosCog")
        if enc is None:
            return
        pending = await self.run(cdb.pending_msgs, message.channel.id)
        ref = getattr(getattr(message, "reference", None), "message_id", None)
        if ref in pending:
            pending = [ref]
        if not pending:
            return
        text = (message.content or "").strip()
        if len(text) < SHORT_REPLY_MIN and not is_staff_or_council(message.author):
            return await self.ask_review(message, pending, text)
        if await self._complete(pending, message.author.id):
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

    async def _complete(self, pending: List[int], user_id: int) -> int:
        """Completa las tareas de esos VODs a nombre de user_id (con su tirada). Devuelve cuántas cerró.
        Si está penalizado, la primera que cierra no da tirada y se le levanta la penalización."""
        enc = self.bot.get_cog("EncargosCog")
        if enc is None:
            return 0
        penalized = await self.run(cdb.has_penalty, user_id)
        done = 0
        for mid in pending:
            try:
                t = await enc.run(edb.find_by_source, f"coach:{mid}")
                no_spin = penalized and t is not None and t.status not in ("done", "cancelled") and t.reward_spins > 0
                if no_spin:
                    await enc.run(edb.set_reward_spins, t.id, 0)
                if await enc.complete_by_source(f"coach:{mid}", user_id):
                    done += 1
                    await self.run(cdb.mark_done, mid)
                    if no_spin:
                        penalized = False
                        await self.run(cdb.clear_penalty, user_id)
                else:
                    if no_spin:
                        await enc.run(edb.set_reward_spins, t.id, t.reward_spins)
                    t = await enc.run(edb.find_by_source, f"coach:{mid}")
                    if t is None or t.status == "done":              # ya la habían cerrado con el botón
                        await self.run(cdb.mark_done, mid)
            except Exception as exc:
                print(f"coaching: no pude completar la tarea del VOD {mid}: {exc!r}")
        return done

    async def ask_review(self, message: discord.Message, pending: List[int], text: str, kind: str = "live", channel=None):
        """Devolución corta: se le pregunta al Staff si está mal hecha. La tarea queda abierta y sin tirada hasta el voto.
        kind='retro': devolución ya cobrada (revisión única); si está mal, solo se penaliza."""
        ch = channel or await self._channel(COACHING_TASKS_CHANNEL_ID)
        if ch is None:
            return
        post = await self.run(cdb.post_by_thread, message.channel.id)
        owner = f" de <@{post['user_id']}>" if post else ""
        quote = text[:300].replace("\n", "\n> ") or "(sin texto)"
        if kind == "retro":
            body = (f"<@&{STAFF_ROLE_ID}> 🔎 Revisión de una devolución ya cobrada: <@{message.author.id}> respondió {len(pending)} VOD(s)"
                    f"{owner} y el mensaje tiene una X. **¿Está mal hecha la VOD review?**\n> {quote}\n{message.jump_url}\n"
                    f"Si está mal, queda penalizado: su próxima VOD review bien hecha no da tirada.")
        else:
            body = (f"<@&{STAFF_ROLE_ID}> ⚠️ <@{message.author.id}> respondió {len(pending)} VOD(s){owner} con una devolución muy corta "
                    f"({len(text)} caracteres). **¿Está mal hecha la VOD review?**\n> {quote}\n{message.jump_url}\n"
                    f"Hasta que el Staff vote, no se da la tirada de Wujupón.")
        try:
            sent = await ch.send(body, view=CoachReviewView(self), allowed_mentions=STAFF_PING)
            await self.run(cdb.add_review, review_msg_id=sent.id, reply_msg_id=message.id, thread_id=message.channel.id,
                           coach_id=message.author.id, vod_msgs=pending, kind=kind)
        except Exception as exc:
            print(f"coaching: no pude pedir la revisión de la devolución {message.id}: {exc!r}")
            return
        if kind == "retro":
            return
        try:
            await message.add_reaction("⏳")
        except Exception:
            pass

    async def on_review_vote(self, interaction: discord.Interaction, bad: bool):
        if not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff puede votar esta revisión.")
        msg = getattr(interaction, "message", None)
        row = await self.run(cdb.get_review, msg.id) if msg else None
        if row is None:
            return await _say(interaction, "❌ No encuentro esta revisión.")
        if not await self.run(cdb.decide_review, msg.id, "bad" if bad else "ok", interaction.user.id):
            return await _say(interaction, "Esta revisión ya fue votada.")
        coach_id = int(row["coach_id"])
        coach, who = f"<@{coach_id}>", f"<@{interaction.user.id}>"
        if bad:
            await self.run(cdb.add_penalty, coach_id)                    # no acumulable
            lost = "ya cobró esa tirada, así que" if row.get("kind") == "retro" else "no cobra la tirada y el VOD sigue pendiente;"
            result = (f"❌ {who}: **mal hecha**. {coach} {lost} queda penalizado: su próxima VOD review bien hecha "
                      f"no da tirada (la penalización no se acumula).")
        elif row.get("kind") == "retro":
            result = f"✅ {who}: **está bien**. {coach} se queda con su tirada."
        else:
            done = await self._complete(row["vod_msgs"], coach_id)
            result = (f"✅ {who}: **está bien**. Se dio la devolución de {done} VOD(s) a nombre de {coach}."
                      if done else f"✅ {who}: **está bien**, pero esos VODs ya los había devuelto otro coach: sin tirada.")
        content = (getattr(msg, "content", "") or "") + "\n\n" + result
        try:
            await interaction.response.edit_message(content=content[:2000], view=None, allowed_mentions=NO_MENTIONS)
        except Exception:
            await _say(interaction, result)

    # ---------- revisión única de devoluciones viejas con «X» ----------
    async def _retro_when_ready(self):
        try:
            await self.bot.wait_until_ready()
            await self.retro_x_review()
        except Exception:
            traceback.print_exc()

    async def retro_x_review(self) -> int:
        """Una sola vez: devoluciones desde RETRO_SINCE con una «X» en el texto que ya cobraron su tirada → votación del Staff.
        Si el Staff dice que está mal, el coach queda penalizado. Devuelve cuántas revisiones abrió."""
        if not await self.run(cdb.take_flag, RETRO_FLAG):
            return 0
        enc = self.bot.get_cog("EncargosCog")
        out = await self._channel(COACHING_TASKS_CHANNEL_ID)
        if enc is None or out is None:
            return 0
        n = 0
        for thread_id, owner_id in await self.run(cdb.all_threads):
            done = await self.run(cdb.done_msgs, thread_id)
            if not done:
                continue
            paid: Dict[int, List[int]] = {}                               # coach -> VODs que cerró y cobró en este post
            for mid in done:
                t = await enc.run(edb.find_by_source, f"coach:{mid}")
                if t is not None and t.status == "done" and t.done_by and t.reward_spins > 0:
                    paid.setdefault(int(t.done_by), []).append(mid)
            if not paid:
                continue
            th = await self._channel(thread_id)
            if th is None:
                continue
            try:
                async for m in th.history(limit=200):
                    created = getattr(m, "created_at", None)
                    author = getattr(m, "author", None)
                    if created is None or created < RETRO_SINCE or author is None or getattr(author, "bot", False):
                        continue
                    if author.id == owner_id or author.id not in paid or not X_RE.search(m.content or ""):
                        continue
                    if await self.run(cdb.has_review_for_reply, m.id):
                        continue
                    await self.ask_review(m, paid[author.id], (m.content or "").strip(), kind="retro", channel=out)
                    n += 1
            except Exception as exc:
                print(f"coaching: revisión retro, no pude leer el post {thread_id}: {exc!r}")
        print(f"coaching: revisión retro de devoluciones con X: {n} enviadas al Staff")
        return n

    async def on_role_pick(self, interaction: discord.Interaction, label: str):
        ref = getattr(getattr(interaction, "message", None), "reference", None)
        mid = getattr(ref, "message_id", None)
        row = await self.run(cdb.get_msg, mid) if mid else None
        if row is None:
            return await _say(interaction, "❌ No encuentro el VOD de este mensaje.")
        if interaction.user.id != int(row["user_id"]) and not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo quien subió el VOD (o el staff) puede elegir el rol.")
        await self.run(cdb.set_role, mid, label)
        enc = self.bot.get_cog("EncargosCog")
        if enc is not None:
            try:
                await enc.update_text_by_source(f"coach:{mid}", task_text(int(row["user_id"]), bool(row["is_obs"]), label, int(row["vods"])))
            except Exception as exc:
                print(f"coaching: no pude actualizar el rol en la tarea: {exc!r}")
        try:
            await interaction.response.edit_message(content=f"🎭 Rol del VOD: **{label}**", view=None)
        except Exception:
            await _say(interaction, f"✅ Rol: {label}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self.process_message(message)
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        try:
            ch = await self._channel(payload.channel_id)
            if getattr(ch, "parent_id", None) != FORUM_CHANNEL_ID:
                return
            msg = payload.cached_message or await ch.fetch_message(payload.message_id)
            await self.process_message(msg, edited=True)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        try:
            if await self.run(cdb.remove_message, payload.message_id):
                enc = self.bot.get_cog("EncargosCog")
                if enc is not None:
                    await enc.update_text_by_source(f"coach:{payload.message_id}", "(El jugador borró el mensaje con el VOD: ya no hay nada para devolver. Podés marcarla como terminada.)")
        except Exception:
            traceback.print_exc()

    # ---------- reportes ----------
    @staticmethod
    def _chunks(lines: List[str], limit: int = 3800) -> List[str]:
        out, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > limit:
                out.append(cur)
                cur = ""
            cur += ln + "\n"
        if cur:
            out.append(cur)
        return out

    coaching_g = app_commands.Group(name="coaching", description="Coaching: reporte semanal, verificación de posts y tus VODs", guild_only=True)

    @coaching_g.command(name="reporte", description="(Staff/Coaching) Quién cumplió los 3 VODs semanales y cuántos subió cada uno")
    @app_commands.describe(semana="Semana en curso o la anterior (lunes a domingo, hora UTC)")
    @app_commands.choices(semana=[app_commands.Choice(name="En curso", value="actual"), app_commands.Choice(name="Anterior", value="anterior")])
    async def coaching_reporte(self, interaction: discord.Interaction, semana: Optional[app_commands.Choice[str]] = None):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council, Coaching y Opinólogo pueden ver el reporte.")
        current = (getattr(semana, "value", semana) or "actual") == "actual"
        start = cdb.week_start(offset=0 if current else -1)
        end = start + timedelta(weeks=1)
        await interaction.response.defer(ephemeral=True)
        role = interaction.guild.get_role(OBSERVACION_ROLE_ID)
        observers = [m for m in (role.members if role else []) if not m.bot]
        counts = await self.run(cdb.count_between, start, end, only_obs=True)
        others = await self.run(cdb.count_between, start, end, only_obs=False)
        totals = await self.run(cdb.totals)
        posts = {m.id: await self.run(cdb.get_post, m.id) for m in observers}
        ok, bad = [], []
        for m in sorted(observers, key=lambda x: (counts.get(x.id, 0), x.display_name.casefold())):
            n = counts.get(m.id, 0)
            extra = "" if posts.get(m.id) else " · sin post"
            line = f"{m.mention} — **{n}/{cdb.WEEKLY_MIN}** · total {totals.get(m.id, 0)}{extra}"
            (ok if n >= cdb.WEEKLY_MIN else bad).append(line)
        label = f"<t:{int(start.timestamp())}:D> → <t:{int((end - timedelta(seconds=1)).timestamp())}:D>"
        head = f"📋 **Coaching · semana {'en curso' if current else 'anterior'}** ({label})\nObservación: **{len(observers)}** · cumplieron: **{len(ok)}** · " + (
            f"les falta: **{len(bad)}**" if current else f"no cumplieron: **{len(bad)}**")
        embeds = []
        title_bad = "⏳ Todavía no llegan a 3" if current else "❌ No cumplieron"
        for title, lines, color in ((title_bad, bad, discord.Color.red()), ("✅ Cumplieron", ok, discord.Color.green())):
            for i, chunk in enumerate(self._chunks(lines) or ["*Nadie.*"]):
                embeds.append(discord.Embed(title=title if i == 0 else f"{title} (cont.)", description=chunk, color=color))
        obs_ids = {m.id for m in observers}
        extra_lines = [f"<@{u}> — {n} VOD(s) · total {totals.get(u, 0)}" for u, n in sorted(others.items(), key=lambda kv: -kv[1])]
        extra_lines += [f"<@{u}> (ya sin Observación) — {n} VOD(s)" for u, n in sorted(counts.items(), key=lambda kv: -kv[1]) if u not in obs_ids]
        if extra_lines:
            for i, chunk in enumerate(self._chunks(extra_lines)):
                embeds.append(discord.Embed(title="👥 Otros miembros (no se les exige el mínimo)" if i == 0 else "👥 Otros (cont.)",
                                            description=chunk, color=discord.Color.blurple()))
        await interaction.followup.send(head, embeds=embeds[:10], ephemeral=True, allowed_mentions=NO_MENTIONS)

    @coaching_g.command(name="verificar-posts", description="(Staff/Coaching) Verifica que todos los de Observación tengan su post de VODs")
    async def coaching_verificar_posts(self, interaction: discord.Interaction):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council, Coaching y Opinólogo pueden verificar los posts.")
        await interaction.response.defer(ephemeral=True)
        role = interaction.guild.get_role(OBSERVACION_ROLE_ID)
        if role is None:
            return await interaction.followup.send("⚠️ No encuentro el rol Observación en este servidor.", ephemeral=True)
        observers = sorted((m for m in role.members if not m.bot), key=lambda x: x.display_name.casefold())
        missing, broken, ok, todo = [], [], 0, []
        for m in observers:
            post = await self.run(cdb.get_post, m.id)
            if not post:
                missing.append(f"{m.mention} — no tiene post")
                todo.append(m)
                continue
            th = await self._channel(post["thread_id"])
            if th is None or getattr(th, "parent_id", FORUM_CHANNEL_ID) != FORUM_CHANNEL_ID:
                broken.append(f"{m.mention} — el post guardado ya no existe")
                todo.append(m)
            else:
                ok += 1
        head = (f"🔎 **Posts de VODs · Observación** ({len(observers)})\n"
                f"✅ Con post: **{ok}** · ❌ Sin post: **{len(missing)}** · ⚠️ Post borrado: **{len(broken)}**")
        embeds = []
        for title, lines, color in (("❌ Sin post", missing, discord.Color.red()), ("⚠️ Post borrado", broken, discord.Color.orange())):
            for i, chunk in enumerate(self._chunks(lines)):
                embeds.append(discord.Embed(title=title if i == 0 else f"{title} (cont.)", description=chunk, color=color))
        if not embeds:
            head += "\n🎉 Todos tienen su post."
        else:
            head += (f"\nCon el botón de abajo se crean los **{len(todo)}** posts faltantes (el bot menciona a cada uno en el primer mensaje). "
                     "También pueden abrirlo ellos con el panel.")
        extra = {"view": CreateMissingView(self, interaction.user.id, todo)} if todo else {}
        await interaction.followup.send(head, embeds=embeds[:10], ephemeral=True, allowed_mentions=NO_MENTIONS, **extra)

    @coaching_g.command(name="mis-vods", description="Cuántos VODs subiste esta semana y cuántos te faltan")
    async def mis_vods(self, interaction: discord.Interaction):
        user = interaction.user
        start = cdb.week_start()
        end = start + timedelta(weeks=1)
        counts = await self.run(cdb.count_between, start, end)
        total = (await self.run(cdb.totals)).get(user.id, 0)
        post = await self.run(cdb.get_post, user.id)
        n = counts.get(user.id, 0)
        link = f"\n🎥 Tu post: <#{post['thread_id']}>" if post else "\n🎥 Todavía no abriste tu post: usá el botón del panel de coaching."
        if is_obs(user):
            missing = max(cdb.WEEKLY_MIN - n, 0)
            state = "✅ ¡Ya cumpliste la semana!" if not missing else f"⏳ Te faltan **{missing}** VOD(s) para llegar a {cdb.WEEKLY_MIN}."
            text = (f"📊 **Tus VODs esta semana:** {n}/{cdb.WEEKLY_MIN}\n{state}\nLa semana termina <t:{int(end.timestamp())}:R> (domingo 23:59 UTC).\n"
                    f"Total histórico: **{total}**{link}")
        else:
            text = (f"📊 **Tus VODs esta semana:** {n} · total histórico: **{total}**\n"
                    f"No tenés mínimo semanal: subís cuando quieras una devolución.{link}")
        await _say(interaction, text)


async def setup_coaching(bot: commands.Bot, db_path: str, guild_id: int) -> CoachingCog:
    cog = CoachingCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
