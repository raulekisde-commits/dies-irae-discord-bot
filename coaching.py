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
import guildcheck_db as gdb
import ctautil as cu
import encargos_db as edb
from encargos import can_take, is_staff_or_council
from guildshop import COUNCIL_ROLE_ID, can_manage

NO_MENTIONS = discord.AllowedMentions.none()
OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
COACHING_ROLE_ID = int(os.getenv("COACHING_ROLE_ID", "1566691393726980167"))
FORUM_CHANNEL_ID = int(os.getenv("COACHING_FORUM_ID", "1566695879656607924"))
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
COACHING_TASKS_CHANNEL_ID = int(os.getenv("COACHING_TASKS_CHANNEL_ID", "1566710577173102602"))     # canal donde salen las tareas de devolución
# Coaching dividido en 4 roles: rol de juego del VOD -> (rol de coach que lo revisa, canal donde sale la tarea pendiente)
COACH_ROLES = {
    "Dps": (int(os.getenv("COACH_DPS_ROLE_ID", "1566691393726980167")), int(os.getenv("COACH_DPS_CHANNEL_ID", "1574275341214556284"))),
    "Tank": (int(os.getenv("COACH_TANK_ROLE_ID", "1574277143788265492")), int(os.getenv("COACH_TANK_CHANNEL_ID", "1574275411469139999"))),
    "Supp": (int(os.getenv("COACH_SUPP_ROLE_ID", "1574277132694323281")), int(os.getenv("COACH_SUPP_CHANNEL_ID", "1574275557972254804"))),
    "Healer": (int(os.getenv("COACH_HEALER_ROLE_ID", "1574277110342746232")), int(os.getenv("COACH_HEALER_CHANNEL_ID", "1574276898538922035"))),
}
COACH_ROLE_IDS = {rid for rid, _ in COACH_ROLES.values()}
ROLE_LABELS = list(COACH_ROLES)
_LABEL_ALIASES = {"dps": "Dps", "tank": "Tank", "supp": "Supp", "support": "Supp", "healer": "Healer"}


def coach_label(label: Optional[str]) -> Optional[str]:
    """Normaliza el rol guardado del VOD a uno de los 4 de coaching (los VODs viejos pueden decir «DPS» o «Support»). None si no es ninguno."""
    return _LABEL_ALIASES.get((label or "").strip().casefold())


def can_review(member, label: Optional[str]) -> bool:
    """Solo el coach de ese rol da la devolución. VODs viejos con un rol que ya no existe: cualquiera de los 4 coaches."""
    key = coach_label(label)
    if key is None:
        return bool(label) and bool(COACH_ROLE_IDS & _roles(member))
    return COACH_ROLES[key][0] in _roles(member)
STAFF_PING = discord.AllowedMentions(users=False, roles=[discord.Object(id=STAFF_ROLE_ID)], everyone=False)
# Revisión única al subir el cambio: devoluciones ya cobradas desde esta hora (UTC) que tienen una «X» en el texto
RETRO_FLAG = "retro_x_review_2026_11"
RATE_PANELS_FLAG = "rate_panels_2026_11"          # una sola vez: panel «Puntuar» en los VODs pendientes subidos antes del cambio
RETRO_SINCE = datetime(2026, 11, 16, 1, 13, tzinfo=timezone.utc)
X_RE = re.compile(r"(?<!\w)[xX❌✖❎](?!\w)")
GRADE_SCALE = ("**Escala:** 1-6 · no llegó a lo esperado · **7** · cumplió su papel · **8-9** · hizo más de lo esperado · "
               "**10** · gameplay de core member")
GRADE_HINTS = {7: "Cumplió su papel", 8: "Hizo más de lo esperado", 9: "Hizo más de lo esperado", 10: "Gameplay de core member"}


def _roles(m) -> set:
    return {r.id for r in getattr(m, "roles", [])}


def can_report(m) -> bool:
    """Ven el reporte del coaching: Council/administradores, Staff, Coaching y Opinólogo."""
    return can_manage(m) or bool(({STAFF_ROLE_ID, COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} | COACH_ROLE_IDS) & _roles(m))


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


class CoachConfirmView(discord.ui.View):
    """Pregunta persistente al coach: ¿esta es la review completa del VOD? (la review puede ir en varios mensajes)."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Sí, está completa", style=discord.ButtonStyle.success, custom_id="coach:rev_done", emoji="✅")
    async def done(self, interaction: discord.Interaction, button):
        await self.cog.on_confirm(interaction, True)

    @discord.ui.button(label="Todavía no", style=discord.ButtonStyle.secondary, custom_id="coach:rev_more", emoji="✍️")
    async def more(self, interaction: discord.Interaction, button):
        await self.cog.on_confirm(interaction, False)


class CoachGradeView(discord.ui.View):
    """Menú persistente para que el coach le ponga la nota (1 a 10) al VOD que devolvió. Responde al mensaje del coach."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(custom_id="coach:grade", placeholder="Nota del VOD (1 a 10)", min_values=1, max_values=1,
                       options=[discord.SelectOption(label=f"{n}/10", value=str(n), description=GRADE_HINTS.get(n, "No llegó a lo esperado"))
                                for n in range(10, 0, -1)])
    async def pick(self, interaction: discord.Interaction, select):
        await self.cog.on_grade_pick(interaction, int(interaction.data["values"][0]))


def rate_panel_text(role: Optional[str]) -> str:
    return (f"⭐ **VOD para puntuar** · rol **{role}**\nEl coach de {role} lo puntúa con el botón (nota del 1 al 10 + devolución). "
            "Al puntuarlo queda listo y el coach cobra su tirada de Wujupón.")


class CoachRateView(discord.ui.View):
    """Panel persistente debajo del VOD (responde al mensaje del video): el coach del rol lo puntúa con el botón.
    Ya puntuado (graded=True), el jugador puede pedir una segunda opinión."""
    def __init__(self, cog: "CoachingCog", graded: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if not graded:
            self.remove_item(self.appeal)

    @discord.ui.button(label="Puntuar", style=discord.ButtonStyle.success, custom_id="coach:rate", emoji="⭐")
    async def rate(self, interaction: discord.Interaction, button):
        await self.cog.on_rate_click(interaction)

    @discord.ui.button(label="Pedir segunda opinión", style=discord.ButtonStyle.secondary, custom_id="coach:appeal", emoji="🔁")
    async def appeal(self, interaction: discord.Interaction, button):
        await self.cog.on_appeal_click(interaction)


class CoachAppealView(discord.ui.View):
    """Pedido de segunda opinión (responde al VOD): otro coach del rol propone una nota nueva."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Proponer nueva nota", style=discord.ButtonStyle.primary, custom_id="coach:appeal_propose", emoji="📝")
    async def propose(self, interaction: discord.Interaction, button):
        await self.cog.on_appeal_propose_click(interaction)


class CoachAppealDecisionView(discord.ui.View):
    """El Council autoriza o rechaza la nota propuesta en la segunda opinión."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Autorizar cambio", style=discord.ButtonStyle.success, custom_id="coach:appeal_ok", emoji="✅")
    async def ok(self, interaction: discord.Interaction, button):
        await self.cog.on_appeal_decide(interaction, True)

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, custom_id="coach:appeal_no", emoji="❌")
    async def no(self, interaction: discord.Interaction, button):
        await self.cog.on_appeal_decide(interaction, False)


class CoachRateModal(discord.ui.Modal):
    """Formulario del coach: nota (1 a 10) y devolución escrita (obligatoria)."""
    TITLE = "Puntuar VOD"

    def __init__(self, cog: "CoachingCog", vod_mid: int, grade: Optional[int] = None):
        super().__init__(title=self.TITLE, timeout=900)
        self.cog, self.vod_mid = cog, vod_mid
        self.nota = discord.ui.TextInput(label="Nota (1 a 10)", placeholder="7 = cumplió su papel · 8-9 más de lo esperado · 10 core",
                                         default=str(grade) if grade else None, min_length=1, max_length=2)
        self.comentario = discord.ui.TextInput(label="Devolución", style=discord.TextStyle.paragraph, min_length=10, max_length=1500,
                                               placeholder="Qué hizo bien, qué tiene que mejorar…")
        self.add_item(self.nota)
        self.add_item(self.comentario)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_rate_submit(interaction, self.vod_mid, str(self.nota.value or ""), str(self.comentario.value or ""))


class CoachAppealModal(CoachRateModal):
    """Mismo formulario, para la nota que propone el segundo coach (no se aplica hasta que la autorice el Council)."""
    TITLE = "Segunda opinión"

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_appeal_propose_submit(interaction, self.vod_mid, str(self.nota.value or ""), str(self.comentario.value or ""))


class CoachingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._locks: Dict[int, asyncio.Lock] = {}
        self._confirm_prompts: Dict[tuple, discord.Message] = {}      # (post, coach) -> última pregunta «¿review completa?»

    async def cog_load(self):
        await self.run(cdb.init_schema)
        self.bot.add_view(CoachPanelView(self))
        self.bot.add_view(CoachRoleView(self))
        self.bot.add_view(CoachReviewView(self))
        self.bot.add_view(CoachGradeView(self))
        self.bot.add_view(CoachConfirmView(self))
        self.bot.add_view(CoachRateView(self, graded=True))
        self.bot.add_view(CoachAppealView(self))
        self.bot.add_view(CoachAppealDecisionView(self))
        if hasattr(self.bot, "wait_until_ready"):
            asyncio.get_running_loop().create_task(self._rate_panels_when_ready())

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
            return                                               # la devolución se da con el botón «Puntuar» del panel del VOD
        vods = cdb.extract_vods(message.content, getattr(message, "attachments", []))
        if not vods:
            return
        obs = is_obs(message.author)
        role = cdb.guess_role(message.content)
        existed = await self.run(cdb.get_msg, message.id) is not None
        new = await self.run(cdb.add_vods, message_id=message.id, thread_id=message.channel.id, user_id=message.author.id, is_obs=obs,
                             vods=vods, created_at=message.created_at, role_label=role)
        if not new:
            return
        row = await self.run(cdb.get_msg, message.id)
        if role and not row.get("role_label"):
            await self.run(cdb.set_role, message.id, role)
        role = row.get("role_label") or role
        text = task_text(message.author.id, bool(row["is_obs"]), role, int(row["vods"]))
        fresh = not existed
        try:
            await self._upsert_task(message.id, message.author.id, text, role, message.jump_url)
        except Exception as exc:
            print(f"coaching: no pude crear la tarea del VOD {message.id}: {exc!r}")
        try:
            await message.add_reaction("🎥")
        except Exception:
            pass
        try:
            if await self.run(cdb.track_mark, message.author.id, message.id, message.created_at):
                await message.add_reaction("📌")                  # VOD de la CTA del track: queda marcado como enviado
        except Exception as exc:
            print(f"coaching: no pude marcar el track de {message.author.id}: {exc!r}")
        if fresh and coach_label(role):
            try:
                await message.reply(rate_panel_text(coach_label(role)), view=CoachRateView(self), mention_author=False,
                                    allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"coaching: no pude poner el panel de puntuar: {exc!r}")
        elif fresh and not role:
            try:
                await message.reply("🎭 ¿Con qué rol jugás en este VOD? Elegilo para que el coach lo sepa.", view=CoachRoleView(self),
                                    mention_author=False, allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"coaching: no pude preguntar el rol: {exc!r}")

    async def _upsert_task(self, mid: int, uid: int, text: str, role: Optional[str], link: str) -> bool:
        """Actualiza la tarea del VOD si ya existe (devuelve True). Si no existe y el VOD ya tiene rol, la crea en el canal de ese
        rol de coach, etiquetando solo a ese coach. Sin rol no se crea: se crea cuando el jugador lo elige."""
        enc = self.bot.get_cog("EncargosCog")
        if enc is None:
            return False
        if await enc.update_text_by_source(f"coach:{mid}", text):          # mensaje editado con más videos / rol elegido
            return True
        key = coach_label(role)
        if key is None:
            return False
        coach_role, channel_id = COACH_ROLES[key]
        await enc.create(text=text, created_by=uid, source=f"coach:{mid}", link=link, ping_roles=[coach_role], ping_staff=False,
                         for_role=coach_role, channel_id=channel_id, reward_spins=0)   # la tirada la cobra el coach al poner nota
        return False

    async def complete_on_reply(self, message: discord.Message):
        """Un coach/opinólogo/staff/council responde en el post. La review puede ir en varios mensajes: a cada uno el bot le pregunta
        si la review está completa. Recién con «Sí» se cierra la tarea de ESE VOD (al que responde o el más viejo pendiente) y se
        pide la nota. Si en el texto ya puso la nota («Nota: 7», «8/10»), se toma como completa sin preguntar."""
        if not ({COACHING_ROLE_ID} | COACH_ROLE_IDS) & _roles(message.author):
            return
        if self.bot.get_cog("EncargosCog") is None:
            return
        pending = await self.run(cdb.pending_msgs, message.channel.id)
        ref = getattr(getattr(message, "reference", None), "message_id", None)
        if ref is not None and ref not in pending and await self.run(cdb.get_msg, ref) is not None:
            return                                                   # sigue comentando un VOD que ya tiene su review cerrada
        if ref in pending:                                           # la review empieza respondiendo (reply) al mensaje del video
            target = ref
        else:                                                        # sin reply: solo cuenta si continúa una review que ya empezó
            target = await self.run(cdb.last_review_target, message.author.id, pending)
        if target is None:
            return
        vod = await self.run(cdb.get_msg, target)
        if vod is None or not can_review(message.author, vod.get("role_label")):
            return                                                   # VOD de otro rol (o todavía sin rol): no es de este coach
        await self.run(cdb.add_grade_prompt, message.id, message.author.id, [target])
        key = (message.channel.id, message.author.id)
        old = self._confirm_prompts.pop(key, None)                   # la pregunta queda solo en el último mensaje de la review
        if old is not None:
            try:
                await old.delete()
            except Exception:
                pass
        grade = cdb.grade_in_text(message.content)
        if grade is not None:
            return await self.finish_review(message.author.id, message.id, [target], grade=grade, message=message)
        try:
            self._confirm_prompts[key] = await message.reply(
                f"📝 <@{message.author.id}>, ¿**esta es la review completa** de este VOD? Si vas a mandar más mensajes, apretá "
                f"«Todavía no» (o seguí escribiendo y te pregunto en el último).", view=CoachConfirmView(self), mention_author=False,
                allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False))
        except Exception as exc:
            print(f"coaching: no pude preguntar si la review está completa: {exc!r}")

    # ---------- botón «Puntuar» ----------
    async def _rate_target(self, interaction: discord.Interaction):
        """VOD del panel (el panel responde al mensaje del video) si quien aprieta puede puntuarlo; si no, avisa y devuelve None."""
        ref = getattr(getattr(interaction, "message", None), "reference", None)
        mid = getattr(ref, "message_id", None)
        row = await self.run(cdb.get_msg, mid) if mid else None
        if row is None:
            await _say(interaction, "❌ Ese VOD ya no existe (lo borraron).")
            return None
        role = row.get("role_label")
        if not can_review(interaction.user, role):
            await _say(interaction, f"🔒 Solo el coach de **{coach_label(role) or role or '?'}** puede puntuar este VOD.")
            return None
        g = await self.run(cdb.get_grade, mid)
        if g and await self.run(cdb.appeal_get, vod_msg_id=mid):
            await _say(interaction, "🔒 Este VOD tiene una segunda opinión: la nota solo cambia si el Council autoriza la propuesta.")
            return None
        if g and int(g["coach_id"]) != interaction.user.id:
            await _say(interaction, f"🔒 Este VOD ya lo puntuó <@{g['coach_id']}>: solo él puede cambiar la nota.")
            return None
        return row, g

    async def on_rate_click(self, interaction: discord.Interaction):
        res = await self._rate_target(interaction)
        if res is None:
            return
        row, g = res
        await interaction.response.send_modal(CoachRateModal(self, int(row["message_id"]), int(g["grade"]) if g else None))

    async def on_rate_submit(self, interaction: discord.Interaction, vod_mid: int, nota: str, comentario: str):
        try:
            grade = int(nota.strip())
        except ValueError:
            grade = 0
        if not 1 <= grade <= 10:
            return await _say(interaction, "❌ La nota tiene que ser un número del 1 al 10.")
        comentario = comentario.strip()
        if len(comentario) < 10:
            return await _say(interaction, "❌ Escribí la devolución (mínimo 10 caracteres).")
        res = await self._rate_target(interaction)
        if res is None:
            return
        row, g = res
        coach_id = interaction.user.id
        if not int(row.get("done") or 0):                           # primera vez: el VOD queda listo y se cierra su tarea
            await self._complete([vod_mid], coach_id)
            await self.run(cdb.mark_done, vod_mid)
        await self.run(cdb.set_grades, [vod_mid], coach_id, grade)
        paid = await self.pay_grades([vod_mid], coach_id)          # una sola vez por VOD: cambiar la nota no vuelve a pagar
        role = coach_label(row.get("role_label")) or row.get("role_label")
        text = (f"✅ **VOD puntuado** por <@{coach_id}> · rol **{role}** · nota **{grade}/10**"
                + (" (cambiada)" if g else "") + "\nEl coach puede cambiar la nota con el botón.")
        if paid:
            text += "\n" + " · ".join(f"🎰 <@{uid}> +{n} tirada(s) de Wujupón" for uid, n in paid.items())
        text += await self._next_pending_line(vod_mid, getattr(interaction, "guild_id", None))
        emb = discord.Embed(title=f"⭐ {grade}/10 · {GRADE_HINTS.get(grade, 'No llegó a lo esperado')}", description=comentario[:4000],
                            color=discord.Color.gold())
        emb.set_footer(text=f"Devolución de {getattr(interaction.user, 'display_name', coach_id)}")
        try:
            await interaction.response.edit_message(content=text[:2000], embed=emb, view=CoachRateView(self, graded=True),
                                                    allowed_mentions=NO_MENTIONS)
        except Exception:
            await _say(interaction, text[:2000], embed=emb)

    # ---------- segunda opinión ----------
    @staticmethod
    def _parse_rating(nota: str, comentario: str):
        try:
            grade = int(nota.strip())
        except ValueError:
            return None, "❌ La nota tiene que ser un número del 1 al 10."
        if not 1 <= grade <= 10:
            return None, "❌ La nota tiene que ser un número del 1 al 10."
        if len(comentario.strip()) < 10:
            return None, "❌ Escribí la devolución (mínimo 10 caracteres)."
        return grade, None

    async def on_appeal_click(self, interaction: discord.Interaction):
        """El jugador pide una segunda opinión sobre la nota (una vez por VOD): se etiqueta al coach del rol en el post."""
        panel = getattr(interaction, "message", None)
        mid = getattr(getattr(panel, "reference", None), "message_id", None)
        row = await self.run(cdb.get_msg, mid) if mid else None
        if row is None:
            return await _say(interaction, "❌ Ese VOD ya no existe (lo borraron).")
        if interaction.user.id != int(row["user_id"]):
            return await _say(interaction, "🔒 Solo quien subió el VOD puede pedir una segunda opinión.")
        g = await self.run(cdb.get_grade, mid)
        if g is None:
            return await _say(interaction, "ℹ️ Este VOD todavía no tiene nota.")
        if not await self.run(cdb.appeal_claim, mid, int(row["thread_id"]), getattr(panel, "id", None), interaction.user.id):
            return await _say(interaction, "ℹ️ Ya pediste una segunda opinión para este VOD (es una sola por VOD).")
        label = coach_label(row.get("role_label")) or row.get("role_label")
        coach_role = COACH_ROLES[label][0] if label in COACH_ROLES else None
        tag = f"<@&{coach_role}> " if coach_role else ""
        body = (f"{tag}🔁 <@{row['user_id']}> pide una **segunda opinión** sobre este VOD (rol **{label}**). Nota actual: **{g['grade']}/10** "
                f"puesta por <@{g['coach_id']}>.\nOtro coach de {label} puede proponer una nota nueva; solo se cambia si la autoriza el Council.")
        try:
            vod = await interaction.channel.fetch_message(int(mid))
            sent = await vod.reply(body, view=CoachAppealView(self), mention_author=False,
                                   allowed_mentions=discord.AllowedMentions(users=False, roles=[discord.Object(id=coach_role)] if coach_role else False,
                                                                            everyone=False))
            await self.run(cdb.appeal_set_msg, mid, sent.id)
        except Exception as exc:
            print(f"coaching: no pude pedir la segunda opinión del VOD {mid}: {exc!r}")
            return await _say(interaction, "❌ No pude publicar el pedido en el post. Avisale a un coach.")
        await _say(interaction, "✅ Listo: pedí la segunda opinión y etiqueté a los coaches de tu rol.")

    async def _appeal_target(self, interaction: discord.Interaction):
        """Pedido de segunda opinión del mensaje (el pedido responde al VOD) si quien aprieta es otro coach del rol."""
        mid = getattr(getattr(getattr(interaction, "message", None), "reference", None), "message_id", None)
        ap = await self.run(cdb.appeal_get, vod_msg_id=mid) if mid else None
        row = await self.run(cdb.get_msg, mid) if mid else None
        if ap is None or row is None:
            await _say(interaction, "❌ No encuentro este pedido de segunda opinión (¿borraron el VOD?).")
            return None
        if ap["status"] != "open":
            await _say(interaction, "ℹ️ Esta segunda opinión ya se cerró.")
            return None
        role = row.get("role_label")
        if not can_review(interaction.user, role):
            await _say(interaction, f"🔒 Solo un coach de **{coach_label(role) or role}** puede dar la segunda opinión.")
            return None
        g = await self.run(cdb.get_grade, mid)
        if g and int(g["coach_id"]) == interaction.user.id:
            await _say(interaction, "🔒 La segunda opinión la tiene que dar otro coach, no el que puso la nota.")
            return None
        return row, g, ap

    async def on_appeal_propose_click(self, interaction: discord.Interaction):
        res = await self._appeal_target(interaction)
        if res is None:
            return
        row = res[0]
        await interaction.response.send_modal(CoachAppealModal(self, int(row["message_id"])))

    async def on_appeal_propose_submit(self, interaction: discord.Interaction, vod_mid: int, nota: str, comentario: str):
        grade, err = self._parse_rating(nota, comentario)
        if err:
            return await _say(interaction, err)
        res = await self._appeal_target(interaction)
        if res is None:
            return
        row, g, ap = res
        label = coach_label(row.get("role_label")) or row.get("role_label")
        emb = discord.Embed(title=f"📝 Nota propuesta: {grade}/10 · {GRADE_HINTS.get(grade, 'No llegó a lo esperado')}",
                            description=comentario.strip()[:4000], color=discord.Color.orange())
        emb.set_footer(text=f"Segunda opinión de {getattr(interaction.user, 'display_name', interaction.user.id)}")
        body = (f"<@&{COUNCIL_ROLE_ID}> 🔁 Segunda opinión de <@{interaction.user.id}> (coach de {label}) para el VOD de <@{row['user_id']}>: "
                f"propone **{grade}/10** (nota actual **{g['grade'] if g else '?'}/10**). **¿Autorizan el cambio?**")
        try:
            sent = await interaction.channel.send(body, embed=emb, view=CoachAppealDecisionView(self),
                                                  allowed_mentions=discord.AllowedMentions(users=False, roles=[discord.Object(id=COUNCIL_ROLE_ID)],
                                                                                           everyone=False))
        except Exception as exc:
            print(f"coaching: no pude publicar la propuesta de segunda opinión del VOD {vod_mid}: {exc!r}")
            return await _say(interaction, "❌ No pude publicar la propuesta en el post.")
        if not await self.run(cdb.appeal_propose, vod_mid, interaction.user.id, grade, comentario.strip(), sent.id):
            return await _say(interaction, "ℹ️ Esta segunda opinión ya se cerró.")
        await _say(interaction, "✅ Propuesta enviada: el Council tiene que autorizar el cambio de nota.")

    async def on_appeal_decide(self, interaction: discord.Interaction, approve: bool):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede autorizar o rechazar el cambio de nota.")
        msg = getattr(interaction, "message", None)
        ap = await self.run(cdb.appeal_get, proposal_msg_id=msg.id) if msg else None
        if ap is None or not await self.run(cdb.appeal_decide, msg.id, approve, interaction.user.id):
            try:
                return await interaction.response.edit_message(view=None)
            except Exception:
                return await _say(interaction, "ℹ️ Esta propuesta ya no está vigente.")
        vod_mid, who = int(ap["vod_msg_id"]), f"<@{interaction.user.id}>"
        if not approve:
            result = f"❌ {who} **rechazó** el cambio. La nota sigue igual; otro coach puede proponer una nueva."
        else:
            old = await self.run(cdb.get_grade, vod_mid)
            grade, coach_id = int(ap["proposed_grade"]), int(ap["proposed_by"])
            await self.run(cdb.set_grades, [vod_mid], coach_id, grade)              # no paga: la tirada del VOD ya se dio
            result = f"✅ {who} **autorizó** el cambio: la nota pasa de **{old['grade'] if old else '?'}/10** a **{grade}/10**."
            await self._appeal_update_panel(ap, grade, coach_id, interaction.user.id, old)
        content = ((getattr(msg, "content", "") or "") + "\n\n" + result)[:2000]
        try:
            await interaction.response.edit_message(content=content, view=None, allowed_mentions=NO_MENTIONS)
        except Exception:
            await _say(interaction, result)

    async def _appeal_update_panel(self, ap: dict, grade: int, coach_id: int, council_id: int, old: Optional[dict]):
        """Refleja la nota nueva en el panel del VOD y cierra el pedido de segunda opinión."""
        th = await self._channel(int(ap["thread_id"]))
        if th is None:
            return
        emb = discord.Embed(title=f"⭐ {grade}/10 · {GRADE_HINTS.get(grade, 'No llegó a lo esperado')}",
                            description=(ap.get("proposed_comment") or "")[:4000], color=discord.Color.gold())
        emb.set_footer(text="Nota cambiada por segunda opinión")
        text = (f"✅ **VOD puntuado** · nota **{grade}/10** (segunda opinión de <@{coach_id}>, autorizada por <@{council_id}>; "
                f"antes {old['grade'] if old else '?'}/10 de <@{old['coach_id'] if old else '?'}>)")
        for mid, kw in ((ap.get("panel_msg_id"), {"content": text, "embed": emb, "view": None}),
                        (ap.get("appeal_msg_id"), {"view": None})):
            if not mid:
                continue
            try:
                m = await th.fetch_message(int(mid))
                await m.edit(allowed_mentions=NO_MENTIONS, **kw)
            except Exception as exc:
                print(f"coaching: no pude actualizar el mensaje {mid} de la segunda opinión: {exc!r}")

    async def _rate_panels_when_ready(self):
        try:
            await self.bot.wait_until_ready()
            await self.post_missing_rate_panels()
        except Exception:
            traceback.print_exc()

    async def post_missing_rate_panels(self) -> int:
        """Una sola vez: panel «Puntuar» en los VODs pendientes (con rol) que se subieron antes de este cambio. Devuelve cuántos puso."""
        if not await self.run(cdb.take_flag, RATE_PANELS_FLAG):
            return 0
        n = 0
        for mid, thread_id, role in await self.run(cdb.pending_with_role):
            label = coach_label(role) or role
            th = await self._channel(thread_id)
            try:
                msg = await th.fetch_message(mid)
                await msg.reply(rate_panel_text(label), view=CoachRateView(self), mention_author=False, allowed_mentions=NO_MENTIONS)
                n += 1
            except Exception as exc:
                print(f"coaching: no pude poner el panel de puntuar en el VOD {mid}: {exc!r}")
            await asyncio.sleep(self.CREATE_DELAY)
        print(f"coaching: paneles de puntuar en VODs viejos: {n}")
        return n

    async def _next_pending_line(self, vod_mid: int, guild_id: Optional[int]) -> str:
        row = await self.run(cdb.get_msg, vod_mid)
        if row is None:
            return ""
        rest = await self.run(cdb.pending_msgs, int(row["thread_id"]))
        if not rest:
            return ""
        link = f"https://discord.com/channels/{guild_id or self.guild_id}/{int(row['thread_id'])}/{rest[0]}"
        return f"\n📌 Queda{'n' if len(rest) > 1 else ''} **{len(rest)}** VOD(s) sin review en este post. El que sigue: {link}"

    async def finish_review(self, coach_id: int, reply_id: int, vod_msgs: List[int], grade: Optional[int] = None, message=None) -> None:
        """Cierra la review: completa la tarea de esos VODs a nombre del coach. Con `grade` además pone la nota y paga."""
        await self._complete(vod_msgs, coach_id)
        if message is not None:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        if grade is None:
            return
        await self.run(cdb.set_grades, vod_msgs, coach_id, grade)
        paid = await self.pay_grades(vod_msgs, coach_id)
        if message is not None:
            for e in ("⭐", "🎰") if paid else ("⭐",):
                try:
                    await message.add_reaction(e)
                except Exception:
                    pass
            nxt = await self._next_pending_line(vod_msgs[0], getattr(getattr(message, "guild", None), "id", None))
            if nxt:
                try:
                    await message.reply(f"✅ Review completa · nota **{grade}/10**.{nxt}", mention_author=False, allowed_mentions=NO_MENTIONS)
                except Exception:
                    pass

    async def on_confirm(self, interaction: discord.Interaction, done: bool):
        ref = getattr(getattr(interaction, "message", None), "reference", None)
        mid = getattr(ref, "message_id", None)
        row = await self.run(cdb.get_grade_prompt, mid) if mid else None
        if row is None:
            return await _say(interaction, "❌ No encuentro la review de este mensaje.")
        if interaction.user.id != int(row["coach_id"]) and not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el coach que está haciendo la review (o el Staff) puede responder esto.")
        channel_id = getattr(getattr(interaction, "channel", None), "id", None)
        if channel_id is not None:
            self._confirm_prompts.pop((channel_id, int(row["coach_id"])), None)
        if not done:
            try:
                return await interaction.response.edit_message(content="✍️ Dale, seguí con la review. Te vuelvo a preguntar en tu próximo mensaje.",
                                                               view=None, allowed_mentions=NO_MENTIONS)
            except Exception:
                return await _say(interaction, "✍️ Dale, seguí con la review.")
        vods = row["vod_msgs"]
        vrow = await self.run(cdb.get_msg, vods[0]) if vods else None
        if vrow is None:
            return await interaction.response.edit_message(content="❌ Ese VOD ya no existe (lo borraron).", view=None)
        if vods[0] not in await self.run(cdb.pending_msgs, int(vrow["thread_id"])):
            return await interaction.response.edit_message(content="ℹ️ Ese VOD ya tenía su review cerrada.", view=None)
        coach_msg = None
        try:                                                         # ✅ en el último mensaje de la review
            coach_msg = await interaction.channel.fetch_message(int(mid))
        except Exception:
            pass
        await self.finish_review(int(row["coach_id"]), int(mid), vods, message=coach_msg)
        text = f"✅ Review completa. <@{row['coach_id']}>, ahora ponele una **nota del 1 al 10** a este VOD.\n{GRADE_SCALE}"
        try:
            await interaction.response.edit_message(content=text, view=CoachGradeView(self), allowed_mentions=NO_MENTIONS)
        except Exception:
            await _say(interaction, text)

    async def pay_grades(self, vod_msgs: List[int], coach_id: int) -> Dict[int, int]:
        """Por cada video con nota, 1 tirada de Wujupón para el coach que hizo la review. Una sola vez por VOD
        (request_key coachgrade:<mensaje>): cambiar la nota no vuelve a pagar. Devuelve {coach: tiradas dadas ahora}."""
        cog = self.bot.get_cog("WujuponCog")
        if cog is None:
            return {}
        import wujupon_db as wdb
        out: Dict[int, int] = {}
        for mid in vod_msgs:
            try:
                row = await self.run(cdb.get_msg, mid)
                if row is None or not int(row["vods"]):
                    continue
                uid, n = int(coach_id), int(row["vods"])
                before = await cog.run(wdb.get_spins, uid)
                after = await cog.run(wdb.gift_spins, actor_id=uid, user_id=uid, count=n, request_key=f"coachgrade:{mid}",
                                      reason=f"VOD con nota de coaching ({n} video(s))")
                if after > before:
                    out[uid] = out.get(uid, 0) + (after - before)
            except Exception as exc:
                print(f"coaching: no pude dar la tirada por la nota del VOD {mid}: {exc!r}")
        return out

    async def on_grade_pick(self, interaction: discord.Interaction, grade: int):
        ref = getattr(getattr(interaction, "message", None), "reference", None)
        mid = getattr(ref, "message_id", None)
        row = await self.run(cdb.get_grade_prompt, mid) if mid else None
        if row is None:
            return await _say(interaction, "❌ No encuentro la devolución de este mensaje.")
        if interaction.user.id != int(row["coach_id"]) and not is_staff_or_council(interaction.user):
            return await _say(interaction, "🔒 Solo el coach que hizo la devolución (o el Staff) puede poner la nota.")
        n = await self.run(cdb.set_grades, row["vod_msgs"], interaction.user.id, grade)
        if not n:
            return await _say(interaction, "❌ Ese VOD ya no existe (lo borraron).")
        paid = await self.pay_grades(row["vod_msgs"], int(row["coach_id"]))      # cobra quien hizo la review (aunque la nota la ponga el Staff)
        text = f"⭐ Nota del VOD: **{grade}/10** · {GRADE_HINTS.get(grade, 'No llegó a lo esperado')} (puesta por <@{interaction.user.id}>). Podés cambiarla con el menú."
        if paid:
            text += "\n" + " · ".join(f"🎰 <@{uid}> +{n} tirada(s) de Wujupón" for uid, n in paid.items())
        text += await self._next_pending_line(row["vod_msgs"][0], getattr(interaction, "guild_id", None))
        try:
            await interaction.response.edit_message(content=text, allowed_mentions=NO_MENTIONS)
        except Exception:
            await _say(interaction, text)

    async def _complete(self, pending: List[int], user_id: int) -> int:
        """Completa las tareas de esos VODs a nombre de user_id. Devuelve cuántas cerró.
        No se acumulan: los VODs que el jugador subió sin respuesta dan **una sola** tirada en total. Cuando una devolución
        cobra, las demás tareas pendientes del post quedan sin tirada (siguen abiertas para responderlas).
        Si está penalizado, esa única tirada no se da y se le levanta la penalización."""
        enc = self.bot.get_cog("EncargosCog")
        if enc is None:
            return 0
        penalized = False                                     # ya no hay penalizaciones por devoluciones cortas
        paid = False                                          # ya se usó la tirada de esta tanda (cobrada o perdida por la penalización)
        done = 0
        for mid in pending:
            try:
                t = await enc.run(edb.find_by_source, f"coach:{mid}")
                no_spin = t is not None and t.status not in ("done", "cancelled") and t.reward_spins > 0   # la devolución ya no paga
                if no_spin:
                    await enc.run(edb.set_reward_spins, t.id, 0)
                if await enc.complete_by_source(f"coach:{mid}", user_id):
                    done += 1
                    await self.run(cdb.mark_done, mid)
                    if t is not None and t.reward_spins > 0:
                        paid = True
                    if no_spin and penalized:
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
        if paid and pending:
            await self._unpay_pending(enc, pending[0])
        return done

    async def _unpay_pending(self, enc, mid: int):
        """La tanda ya cobró: las tareas que siguen pendientes en ese post pasan a 0 tiradas (los VODs nuevos sí cobran)."""
        try:
            row = await self.run(cdb.get_msg, mid)
            if row is None:
                return
            for other in await self.run(cdb.pending_msgs, int(row["thread_id"])):
                t = await enc.run(edb.find_by_source, f"coach:{other}")
                if t is not None and t.status not in ("done", "cancelled") and t.reward_spins > 0:
                    await enc.run(edb.set_reward_spins, t.id, 0)
        except Exception as exc:
            print(f"coaching: no pude sacar la tirada de las pendientes del VOD {mid}: {exc!r}")

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
        if bad:                                                          # revisiones viejas: ya no se penaliza
            result = (f"❌ {who}: **mal hecha**. " + ("" if row.get("kind") == "retro" else "El VOD sigue pendiente."))
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
        if coach_label(label) is None:                               # menú viejo con un rol que ya no se usa
            try:
                return await interaction.response.edit_message(content="🎭 Ese rol ya no se usa. Elegí uno de estos:", view=CoachRoleView(self))
            except Exception:
                return await _say(interaction, "🎭 Ese rol ya no se usa: elegí Dps, Tank, Supp o Healer.")
        label = coach_label(label)
        await self.run(cdb.set_role, mid, label)
        try:
            link = f"https://discord.com/channels/{getattr(interaction, 'guild_id', None) or self.guild_id}/{int(row['thread_id'])}/{mid}"
            await self._upsert_task(mid, int(row["user_id"]), task_text(int(row["user_id"]), bool(row["is_obs"]), label, int(row["vods"])),
                                    label, link)
        except Exception as exc:
            print(f"coaching: no pude crear/actualizar la tarea con el rol: {exc!r}")
        try:
            await interaction.response.edit_message(content=f"🎭 Rol del VOD: **{label}**\n" + rate_panel_text(label),
                                                    view=CoachRateView(self), allowed_mentions=NO_MENTIONS)   # el mismo mensaje pasa a ser el panel
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
            await self.run(cdb.track_unmark_message, payload.message_id)
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

    coaching_g = app_commands.Group(name="coaching", description="Coaching: reporte semanal, verificación de posts, track de CTA y tus VODs", guild_only=True)

    async def _send_embeds(self, interaction: discord.Interaction, head: str, embeds: List[discord.Embed], public: bool) -> None:
        """Manda el reporte en uno o más mensajes: Discord rechaza más de 10 embeds o más de 6000 caracteres por mensaje."""
        groups, cur, size = [], [], len(head)
        for e in embeds:
            n = len(e.title or "") + len(e.description or "")
            if cur and (len(cur) >= 10 or size + n > 5800):
                groups.append(cur)
                cur, size = [], 0
            cur.append(e)
            size += n
        groups.append(cur)
        for i, group in enumerate(groups):
            await interaction.followup.send(head if i == 0 else None, embeds=group, ephemeral=not public, allowed_mentions=NO_MENTIONS)

    def _posts_for(self, user_ids: List[int]) -> Dict[int, Optional[dict]]:
        return {u: cdb.get_post(self.db_path, u) for u in user_ids}

    @coaching_g.command(name="reporte", description="(Staff/Coaching) Quién cumplió los 3 VODs semanales y cuántos subió cada uno")
    @app_commands.describe(semana="Semana en curso o la anterior (lunes a domingo, hora UTC)", publico="Mostrar la respuesta a todos en el canal")
    @app_commands.choices(semana=[app_commands.Choice(name="En curso", value="actual"), app_commands.Choice(name="Anterior", value="anterior")])
    async def coaching_reporte(self, interaction: discord.Interaction, semana: Optional[app_commands.Choice[str]] = None,
                               publico: bool = False):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council, Coaching y Opinólogo pueden ver el reporte.")
        current = (getattr(semana, "value", semana) or "actual") == "actual"
        start = cdb.week_start(offset=0 if current else -1)
        end = start + timedelta(weeks=1)
        await interaction.response.defer(ephemeral=not publico, thinking=True)
        try:
            role = interaction.guild.get_role(OBSERVACION_ROLE_ID)
            observers = [m for m in (role.members if role else []) if not m.bot]
            counts = await self.run(cdb.count_between, start, end, only_obs=True)
            others = await self.run(cdb.count_between, start, end, only_obs=False)
            totals = await self.run(cdb.totals)
            posts = await asyncio.to_thread(self._posts_for, [m.id for m in observers])     # un solo hilo, no uno por miembro
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
            await self._send_embeds(interaction, head, embeds, publico)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ No pude armar el reporte. Probá de nuevo o avisale a un admin.")

    @coaching_g.command(name="sin-vods", description="(Staff/Coaching) Los de Observación que nunca subieron un VOD")
    @app_commands.describe(publico="Mostrar la respuesta a todos en el canal")
    async def coaching_sin_vods(self, interaction: discord.Interaction, publico: bool = False):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council, Coaching y Opinólogo pueden ver esta lista.")
        await interaction.response.defer(ephemeral=not publico, thinking=True)
        try:
            role = interaction.guild.get_role(OBSERVACION_ROLE_ID)
            if role is None:
                return await interaction.followup.send("⚠️ No encuentro el rol Observación en este servidor.", ephemeral=True)
            observers = [m for m in role.members if not m.bot]
            totals = await self.run(cdb.totals)
            never = [m for m in observers if not totals.get(m.id)]
            posts = await asyncio.to_thread(self._posts_for, [m.id for m in never])
            regs = await self.run(gdb.registered_at_by_user)
            chars = await self.run(gdb.chars_by_user)
            kb = await self.run(gdb.kb_cta_presence, interaction.guild.id, chars)
            signups = await self.run(gdb.cta_signups, interaction.guild.id)
            esc = discord.utils.escape_markdown
            pct = lambda a, b: f"{a * 100 // b}%" if b else "—"

            def since_of(m) -> Optional[datetime]:
                """Ingreso = cuando lo reclutaron o vinculó su cuenta de Albion; si no tiene, cuando entró al Discord."""
                when = regs.get(m.id, (None, None))[0] or getattr(m, "joined_at", None)
                return when.replace(tzinfo=timezone.utc) if when is not None and when.tzinfo is None else when

            def line(m) -> str:
                since, char = since_of(m), regs.get(m.id, (None, None))[1]
                out = f"{m.mention} · " + (f"**{esc(char)}**" if char else "*sin Albion vinculado*")
                if since is None:
                    return out + " · ingreso desconocido" + ("" if posts.get(m.id) else " · sin post")
                kb_tot = sum(1 for when, _p in kb if when >= since)
                kb_in = sum(1 for when, present in kb if when >= since and m.id in present)
                cta_tot = sum(1 for when, _s in signups if when >= since)
                cta_in = sum(1 for when, signed in signups if when >= since and m.id in signed)
                return (out + f" · en la guild <t:{int(since.timestamp())}:R>"
                        + f"\n   🗡️ killboard **{kb_in}/{kb_tot}** ({pct(kb_in, kb_tot)}) · 📝 anotado **{cta_in}/{cta_tot}** ({pct(cta_in, cta_tot)})"
                        + ("" if posts.get(m.id) else " · sin post"))

            far = datetime.max.replace(tzinfo=timezone.utc)
            never.sort(key=lambda m: (since_of(m) or far, m.display_name.casefold()))          # los más antiguos primero
            lines = [line(m) for m in never]
            head = (f"🚫 **Coaching · nunca subieron VODs** — **{len(never)}** de **{len(observers)}** en Observación\n"
                    "Ingreso = cuando lo reclutaron o vinculó su cuenta de Albion. Asistencia desde que entró: "
                    "🗡️ CTA con killboard en las que aparece · 📝 CTA a las que se anotó.")
            embeds = [discord.Embed(title="🚫 Nunca subieron" if i == 0 else "🚫 Nunca subieron (cont.)", description=chunk, color=discord.Color.red())
                      for i, chunk in enumerate(self._chunks(lines) or ["*Nadie: todos subieron al menos uno.* 🎉"])]
            await self._send_embeds(interaction, head, embeds, publico)
        except Exception:
            traceback.print_exc()
            await _say(interaction, "❌ No pude armar la lista. Probá de nuevo o avisale a un admin.")

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

    # ---------- notas (1 a 10) ----------
    @coaching_g.command(name="notas", description="Promedio de las notas de coaching de un jugador (sin usuario: las tuyas)")
    @app_commands.describe(usuario="Jugador (ver las de otro: Staff/Coaching/Opinólogo/Council)")
    async def coaching_notas(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        target = usuario or interaction.user
        if target.id != interaction.user.id and not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, Coaching, Opinólogo y el Council pueden ver las notas de otro jugador.")
        avg, n = await self.run(cdb.player_avg, target.id)
        if not n:
            return await _say(interaction, f"⭐ {target.mention} todavía no tiene VODs con nota.")
        last = await self.run(cdb.player_grades, target.id, 10)
        lines = [f"• **{g['grade']}/10** · por <@{g['coach_id']}>" for g in last]
        await _say(interaction, f"⭐ **Notas de coaching de {target.mention}:** promedio **{avg:.1f}/10** en **{n}** VOD(s).\n"
                                f"{GRADE_SCALE}\n\nÚltimas notas:\n" + "\n".join(lines))

    @coaching_g.command(name="notas-coaches", description="(Staff/Coaching) Promedio de las notas que pone cada coach")
    @app_commands.describe(coach="Ver solo un coach")
    async def coaching_notas_coaches(self, interaction: discord.Interaction, coach: Optional[discord.Member] = None):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, Coaching, Opinólogo y el Council pueden ver esto.")
        rows = await self.run(cdb.coach_avgs, coach.id if coach else None)
        if not rows:
            return await _say(interaction, "⭐ Todavía no hay notas" + (f" puestas por {coach.mention}." if coach else "."))
        lines = [f"<@{cid}> — promedio **{avg:.1f}/10** · {n} nota(s)" for cid, avg, n in rows]
        for k, chunk in enumerate(self._chunks(lines)):
            emb = discord.Embed(title="⭐ Notas que pone cada coach" if k == 0 else None, description=chunk, color=discord.Color.gold())
            await _say(interaction, None, embed=emb)

    @coaching_g.command(name="nota-lista", description="(Staff/Coaching) Jugadores con promedio de nota entre dos números")
    @app_commands.describe(mayor_a="Promedio mínimo (incluido), de 1 a 10", menor_a="Promedio máximo (incluido), de 1 a 10")
    async def coaching_nota_lista(self, interaction: discord.Interaction,
                                  mayor_a: app_commands.Range[float, 1, 10], menor_a: app_commands.Range[float, 1, 10]):
        if not can_report(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, Coaching, Opinólogo y el Council pueden ver esto.")
        low, high = sorted((float(mayor_a), float(menor_a)))
        rows = await self.run(cdb.players_between, low, high)
        rango = f"{low:g} y {high:g}"
        if not rows:
            return await _say(interaction, f"⭐ Nadie tiene un promedio de nota entre {rango}.")
        lines = [f"<@{pid}> — **{avg:.1f}/10** · {n} VOD(s)" for pid, avg, n in rows]
        for k, chunk in enumerate(self._chunks(lines)):
            emb = discord.Embed(title=f"⭐ Promedio de nota entre {rango} · {len(rows)} jugador(es)" if k == 0 else None,
                                description=chunk, color=discord.Color.gold())
            await _say(interaction, None, embed=emb)

    # ---------- track: quiénes tienen que mandar el VOD de una CTA ----------
    track_g = app_commands.Group(name="track", description="(Staff/Coaching) Lista de quiénes tienen que mandar el VOD de una CTA", parent=coaching_g)
    MENTION_RE = re.compile(r"<@!?(\d+)>|<@&(\d+)>|\b(\d{17,20})\b")

    def _parse_members(self, guild, text: str) -> List[int]:
        """IDs de las menciones de usuarios (o IDs sueltos) y de los miembros de los roles mencionados. Sin bots ni repetidos."""
        out: Dict[int, None] = {}
        for u, r, raw in self.MENTION_RE.findall(text or ""):
            if r:
                role = guild.get_role(int(r)) if guild else None
                for m in (role.members if role else []):
                    if not m.bot:
                        out[m.id] = None
            else:
                out[int(u or raw)] = None
        return list(out)

    async def _track_guard(self, interaction) -> bool:
        if can_report(interaction.user):
            return True
        await _say(interaction, "🔒 Solo el Staff, el Council, Coaching y Opinólogo pueden manejar el track.")
        return False

    @track_g.command(name="crear", description="Crea la lista: quiénes tienen que mandar el VOD de la última CTA (solo una a la vez)")
    @app_commands.describe(titulo="Nombre de la CTA (ej: CTA martes 22 UTC)", miembros="Menciones de las personas o de un rol (@a @b @Rol)")
    async def track_crear(self, interaction: discord.Interaction, titulo: str, miembros: str):
        if not await self._track_guard(interaction):
            return
        ids = self._parse_members(interaction.guild, miembros)
        if not ids:
            return await _say(interaction, "⚠️ No encontré a nadie en `miembros`: mencioná a las personas o a un rol.")
        titulo = titulo.strip()[:100]
        if not await self.run(cdb.create_track, titulo, interaction.user.id, ids):
            t = await self.run(cdb.get_track)
            return await _say(interaction, f"⚠️ Ya hay un track activo: **{t['title'] if t else '?'}**. Cerralo con `/coaching track cerrar` antes de crear otro.")
        await _say(interaction, f"✅ Track **{titulo}** creado con **{len(ids)}** persona(s). Se marca solo cuando suben un VOD a su post.\n"
                                "Mirá cómo va con `/coaching track ver`.")

    @track_g.command(name="desde-cta", description="Usalo en el hilo de una CTA: arma el track con los anotados que tienen ese rol")
    @app_commands.describe(rol="Solo entran los anotados de la CTA que tienen este rol", titulo="Nombre del track (por defecto, el del hilo)",
                           modo="Crear un track nuevo o agregar a los del track activo")
    @app_commands.choices(modo=[app_commands.Choice(name="Crear track", value="crear"), app_commands.Choice(name="Agregar al track activo", value="agregar")])
    async def track_desde_cta(self, interaction: discord.Interaction, rol: discord.Role, titulo: Optional[str] = None,
                              modo: Optional[app_commands.Choice[str]] = None):
        if not await self._track_guard(interaction):
            return
        add = (getattr(modo, "value", modo) or "crear") == "agregar"
        found = await self.run(cdb.cta_signups_by_thread, getattr(interaction.channel, "id", 0))
        if found is None:
            return await _say(interaction, "⚠️ Este comando se usa **dentro del hilo de una CTA**.")
        _, signed = found
        guild = interaction.guild
        ids = [u for u in signed if (m := guild.get_member(u)) is not None and not m.bot and rol.id in _roles(m)]
        if not ids:
            return await _say(interaction, f"⚠️ Ninguno de los **{len(signed)}** anotados de esta CTA tiene el rol {rol.mention}.")
        track = await self.run(cdb.get_track)
        if add:
            if not track:
                return await _say(interaction, "⚠️ No hay ningún track activo. Usá el modo *Crear track*.")
            n = await self.run(cdb.track_add, ids)
            return await _say(interaction, f"✅ Track **{track['title']}**: agregué **{n}** con {rol.mention} (ya estaban: {len(ids) - n}).")
        name = (titulo or getattr(interaction.channel, "name", "") or "CTA").strip()[:100]
        if not await self.run(cdb.create_track, name, interaction.user.id, ids):
            return await _say(interaction, f"⚠️ Ya hay un track activo: **{track['title'] if track else '?'}**. Cerralo con `/coaching track cerrar` "
                                           "o usá el modo *Agregar al track activo*.")
        await _say(interaction, f"✅ Track **{name}** creado con **{len(ids)}** anotado(s) con {rol.mention} (de {len(signed)} en la CTA).\n"
                                "Se marca solo cuando suben un VOD a su post. Para sumar más gente: `/coaching track agregar`.")

    @track_g.command(name="agregar", description="Agrega personas al track activo")
    @app_commands.describe(miembros="Menciones de las personas o de un rol")
    async def track_agregar(self, interaction: discord.Interaction, miembros: str):
        if not await self._track_guard(interaction):
            return
        if not await self.run(cdb.get_track):
            return await _say(interaction, "⚠️ No hay ningún track activo. Crealo con `/coaching track crear`.")
        ids = self._parse_members(interaction.guild, miembros)
        if not ids:
            return await _say(interaction, "⚠️ No encontré a nadie: mencioná a las personas o a un rol.")
        n = await self.run(cdb.track_add, ids)
        await _say(interaction, f"✅ Agregados: **{n}** (ya estaban: {len(ids) - n}).")

    @track_g.command(name="quitar", description="Saca a una persona del track activo")
    async def track_quitar(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._track_guard(interaction):
            return
        ok = await self.run(cdb.track_remove, usuario.id)
        await _say(interaction, f"✅ Saqué a {usuario.mention} del track." if ok else f"⚠️ {usuario.mention} no está en el track.")

    @track_g.command(name="ver", description="Quién ya mandó el VOD de la CTA del track y quién falta")
    async def track_ver(self, interaction: discord.Interaction):
        if not await self._track_guard(interaction):
            return
        t = await self.run(cdb.get_track)
        if not t:
            return await _say(interaction, "ℹ️ No hay ningún track activo. Crealo con `/coaching track crear`.")
        rows = await self.run(cdb.track_members)
        done, pending = [], []
        for r in rows:
            uid = int(r["user_id"])
            if r["done_at"]:
                post = await self.run(cdb.get_post, uid)
                link = (f" · [VOD](https://discord.com/channels/{interaction.guild.id}/{post['thread_id']}/{r['message_id']})"
                        if post and r["message_id"] else "")
                when = int(datetime.fromisoformat(r["done_at"]).timestamp())
                done.append(f"<@{uid}> — <t:{when}:R>{link}")
            else:
                post = await self.run(cdb.get_post, uid)
                pending.append(f"<@{uid}>" + ("" if post else " · sin post"))
        since = int(datetime.fromisoformat(t["created_at"]).timestamp())
        head = (f"📌 **Track: {t['title']}** (desde <t:{since}:f>, por <@{t['created_by']}>)\n"
                f"Enviaron: **{len(done)}/{len(rows)}** · faltan: **{len(pending)}**")
        embeds = []
        for title, lines, color in (("⏳ Faltan", pending, discord.Color.red()), ("✅ Enviaron", done, discord.Color.green())):
            for i, chunk in enumerate(self._chunks(lines) or ["*Nadie.*"]):
                embeds.append(discord.Embed(title=title if i == 0 else f"{title} (cont.)", description=chunk, color=color))
        await interaction.response.send_message(head, embeds=embeds[:10], ephemeral=True, allowed_mentions=NO_MENTIONS)

    @track_g.command(name="cerrar", description="Cierra el track activo (para poder crear otro)")
    async def track_cerrar(self, interaction: discord.Interaction):
        if not await self._track_guard(interaction):
            return
        t = await self.run(cdb.get_track)
        if not t or not await self.run(cdb.close_track):
            return await _say(interaction, "ℹ️ No hay ningún track activo.")
        await _say(interaction, f"✅ Track **{t['title']}** cerrado.")


async def setup_coaching(bot: commands.Bot, db_path: str, guild_id: int) -> CoachingCog:
    cog = CoachingCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
