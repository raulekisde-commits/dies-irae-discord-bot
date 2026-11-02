"""Coaching: panel con botón que abre un post por persona en el foro de VODs, tareas de devolución para el rol Coaching
y control de los 3 VODs semanales del rol Observación (/coaching-reporte, /mis-vods)."""
import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import coaching_db as cdb
import ctautil as cu
import encargos_db as edb
from encargos import can_take
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


class CoachRoleView(discord.ui.View):
    """Menú persistente que acompaña a un VOD sin rol indicado: el mensaje del VOD es al que responde el bot."""
    def __init__(self, cog: "CoachingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(custom_id="coach:rolesel", placeholder="¿Con qué rol jugás en este VOD?", min_values=1, max_values=1,
                       options=[discord.SelectOption(label=n, value=n) for n in ROLE_LABELS])
    async def pick(self, interaction: discord.Interaction, select):
        await self.cog.on_role_pick(interaction, interaction.data["values"][0])


class CoachingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self.run(cdb.init_schema)
        self.bot.add_view(CoachPanelView(self))
        self.bot.add_view(CoachRoleView(self))

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
    @app_commands.command(name="coaching-panel", description="(Council) Publica el panel para abrir el post de VODs del coaching")
    @app_commands.guild_only()
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
                         f"• Con `/mis-vods` ves cuántos llevás y cuántos te faltan."),
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
            quota = (f"Como tenés el rol **Observación**, tenés que subir **mínimo {cdb.WEEKLY_MIN} VODs por semana** (lunes a domingo, hora UTC)."
                     if is_obs(user) else "Subí tus VODs acá para pedir devolución.")
            content = (f"{user.mention} este es tu post de VODs. {quota}\n"
                       "Pegá el link (YouTube, Twitch, Medal…) o subí el video. Si podés, decí con qué rol jugás; si no, el bot te lo pregunta.")
            name = f"{user.display_name}"[:90] or f"VODs {user.id}"
            allowed = discord.AllowedMentions(users=[user], roles=False, everyone=False)
            try:
                try:
                    res = await forum.create_thread(name=name, content=content, allowed_mentions=allowed)
                except discord.HTTPException:
                    tags = list(getattr(forum, "available_tags", []) or [])[:1]       # foros que obligan a elegir una etiqueta
                    if not tags:
                        raise
                    res = await forum.create_thread(name=name, content=content, applied_tags=tags, allowed_mentions=allowed)
                thread = getattr(res, "thread", res)
            except Exception as exc:
                print(f"coaching: no pude crear el post de {user.id}: {exc!r}")
                return await interaction.followup.send("❌ No pude crear tu post (el bot necesita permisos de *Crear posts* y *Gestionar hilos* en el foro).", ephemeral=True)
            await self.run(cdb.set_post, user.id, thread.id)
            try:
                await thread.add_user(user)
            except Exception:
                pass
        await interaction.followup.send(f"🎥 Listo, este es tu post: {thread.mention}", ephemeral=True)

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
        done = 0
        for mid in pending:
            try:
                if await enc.complete_by_source(f"coach:{mid}", message.author.id):
                    done += 1
                    await self.run(cdb.mark_done, mid)
                else:
                    t = await enc.run(edb.find_by_source, f"coach:{mid}")
                    if t is None or t.status == "done":              # ya la habían cerrado con el botón
                        await self.run(cdb.mark_done, mid)
            except Exception as exc:
                print(f"coaching: no pude completar la tarea del VOD {mid}: {exc!r}")
        if done:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

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

    @app_commands.command(name="coaching-reporte", description="(Staff/Coaching) Quién cumplió los 3 VODs semanales y cuántos subió cada uno")
    @app_commands.describe(semana="Semana en curso o la anterior (lunes a domingo, hora UTC)")
    @app_commands.choices(semana=[app_commands.Choice(name="En curso", value="actual"), app_commands.Choice(name="Anterior", value="anterior")])
    @app_commands.guild_only()
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

    @app_commands.command(name="mis-vods", description="Cuántos VODs subiste esta semana y cuántos te faltan")
    @app_commands.guild_only()
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
