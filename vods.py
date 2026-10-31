"""Seguimiento de VODs: un post por miembro en un foro (lo crea el bot), mínimo de videos por semana con warning automático,
mentores por rol de combate y devoluciones con formulario."""
import asyncio
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from dbutil import connect, write_tx, utcnow_iso
from guildshop import can_manage
from warnings_sys import add_warning, init_schema as init_warnings

NO_MENTIONS = discord.AllowedMentions.none()
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
ART = timezone(timedelta(hours=-3))                         # Argentina, sin horario de verano
MENTOR_ROLES = ("tank", "healer", "supp", "dps")
DEFAULT_MIN = 3
REMINDER_WEEKDAY, REMINDER_HOUR = 5, 18                      # sábado 18:00 (hora argentina): aviso a quien le faltan videos
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
URL_RE = re.compile(r"https?://(?:www\.|m\.|clips\.)?(?:youtube\.com|youtu\.be|twitch\.tv|medal\.tv|streamable\.com|kick\.com|vimeo\.com)/\S+", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vod_config (
    guild_id INTEGER PRIMARY KEY,
    forum_id INTEGER,
    min_per_week INTEGER NOT NULL DEFAULT 3,
    notice_channel_id INTEGER,
    last_week_checked TEXT,
    last_reminder_week TEXT
);
CREATE TABLE IF NOT EXISTS vod_posts (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, thread_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS vod_videos (
    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vod_videos_user ON vod_videos(guild_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS vod_mentors (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_label TEXT NOT NULL, PRIMARY KEY (guild_id, user_id, role_label)
);
CREATE TABLE IF NOT EXISTS vod_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, mentor_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL, good TEXT NOT NULL, improve TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vod_week_results (
    guild_id INTEGER NOT NULL, week_start TEXT NOT NULL, user_id INTEGER NOT NULL, videos INTEGER NOT NULL, warned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, week_start, user_id)
);
"""


# ---------------------------------------------------------------- tiempo
def week_start_of(dt: datetime) -> datetime:
    """Lunes 00:00 (hora argentina) de la semana de dt, como datetime con zona ART."""
    d = dt.astimezone(ART)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def week_key(dt: datetime) -> str:
    return week_start_of(dt).strftime("%Y-%m-%d")


def _parse(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def is_video_message(attachments, content: str) -> bool:
    for a in attachments or []:
        ct = (getattr(a, "content_type", None) or "").lower()
        name = (getattr(a, "filename", "") or "").lower()
        if ct.startswith("video/") or name.endswith(VIDEO_EXT):
            return True
    return bool(URL_RE.search(content or ""))


# ---------------------------------------------------------------- base de datos
def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    init_warnings(db_path)


def get_config(db_path: str, guild_id: int) -> dict:
    conn = connect(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO vod_config(guild_id, min_per_week) VALUES (?,?)", (guild_id, DEFAULT_MIN))
        return dict(conn.execute("SELECT * FROM vod_config WHERE guild_id=?", (guild_id,)).fetchone())
    finally:
        conn.close()


def set_config(db_path: str, guild_id: int, **fields) -> dict:
    get_config(db_path, guild_id)
    allowed = {"forum_id", "min_per_week", "notice_channel_id", "last_week_checked", "last_reminder_week"}
    conn = connect(db_path)
    try:
        for k, v in fields.items():
            if k in allowed:
                conn.execute(f"UPDATE vod_config SET {k}=? WHERE guild_id=?", (v, guild_id))
        return dict(conn.execute("SELECT * FROM vod_config WHERE guild_id=?", (guild_id,)).fetchone())
    finally:
        conn.close()


def get_post(db_path: str, guild_id: int, user_id: int) -> Optional[int]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT thread_id FROM vod_posts WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return int(r["thread_id"]) if r else None
    finally:
        conn.close()


def owner_of_thread(db_path: str, thread_id: int) -> Optional[Tuple[int, int]]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT guild_id, user_id FROM vod_posts WHERE thread_id=?", (thread_id,)).fetchone()
        return (int(r["guild_id"]), int(r["user_id"])) if r else None
    finally:
        conn.close()


def save_post(db_path: str, guild_id: int, user_id: int, thread_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM vod_posts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            conn.execute("INSERT INTO vod_posts(guild_id,user_id,thread_id,created_at) VALUES (?,?,?,?)", (guild_id, user_id, thread_id, utcnow_iso()))
    finally:
        conn.close()


def post_created_at(db_path: str, guild_id: int, user_id: int) -> Optional[str]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT created_at FROM vod_posts WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return r["created_at"] if r else None
    finally:
        conn.close()


def delete_post_by_thread(db_path: str, thread_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM vod_posts WHERE thread_id=?", (thread_id,))
    finally:
        conn.close()


def add_video(db_path: str, guild_id: int, user_id: int, thread_id: int, message_id: int, created_at: datetime) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO vod_videos(message_id,guild_id,user_id,thread_id,created_at) VALUES (?,?,?,?,?)",
                           (message_id, guild_id, user_id, thread_id, _iso(created_at)))
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_video(db_path: str, message_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM vod_videos WHERE message_id=?", (message_id,))
    finally:
        conn.close()


def count_videos(db_path: str, guild_id: int, user_id: int, start: datetime, end: datetime) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM vod_videos WHERE guild_id=? AND user_id=? AND created_at>=? AND created_at<?",
                                (guild_id, user_id, _iso(start), _iso(end))).fetchone()[0])
    finally:
        conn.close()


def counts_between(db_path: str, guild_id: int, start: datetime, end: datetime) -> dict:
    conn = connect(db_path)
    try:
        return {int(r["user_id"]): int(r["n"]) for r in conn.execute(
            "SELECT user_id, COUNT(*) n FROM vod_videos WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY user_id", (guild_id, _iso(start), _iso(end)))}
    finally:
        conn.close()


def user_stats(db_path: str, guild_id: int, user_id: int, now: datetime, weeks: int = 4) -> dict:
    ws = week_start_of(now)
    per_week = []
    for i in range(weeks):
        s = ws - timedelta(weeks=i)
        per_week.append((s, count_videos(db_path, guild_id, user_id, s, s + timedelta(weeks=1))))
    conn = connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM vod_videos WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()[0]
        last = conn.execute("SELECT MAX(created_at) FROM vod_videos WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()[0]
        fb = conn.execute("SELECT COUNT(*) FROM vod_feedback WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()[0]
        warned = conn.execute("SELECT COUNT(*) FROM vod_week_results WHERE guild_id=? AND user_id=? AND warned=1", (guild_id, user_id)).fetchone()[0]
    finally:
        conn.close()
    return {"weeks": per_week, "total": int(total), "last": last, "feedback": int(fb), "warned": int(warned)}


def add_mentor(db_path: str, guild_id: int, user_id: int, role_label: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("INSERT OR IGNORE INTO vod_mentors(guild_id,user_id,role_label) VALUES (?,?,?)", (guild_id, user_id, role_label)).rowcount > 0
    finally:
        conn.close()


def remove_mentor(db_path: str, guild_id: int, user_id: int, role_label: Optional[str] = None) -> int:
    conn = connect(db_path)
    try:
        if role_label:
            return conn.execute("DELETE FROM vod_mentors WHERE guild_id=? AND user_id=? AND role_label=?", (guild_id, user_id, role_label)).rowcount
        return conn.execute("DELETE FROM vod_mentors WHERE guild_id=? AND user_id=?", (guild_id, user_id)).rowcount
    finally:
        conn.close()


def mentors(db_path: str, guild_id: int, role_label: Optional[str] = None) -> List[Tuple[int, str]]:
    conn = connect(db_path)
    try:
        q, p = "SELECT user_id, role_label FROM vod_mentors WHERE guild_id=?", [guild_id]
        if role_label:
            q, p = q + " AND role_label=?", p + [role_label]
        return [(int(r["user_id"]), r["role_label"]) for r in conn.execute(q + " ORDER BY role_label, user_id", p)]
    finally:
        conn.close()


def add_feedback(db_path: str, guild_id: int, user_id: int, mentor_id: int, thread_id: int, good: str, improve: str, note: str) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return int(conn.execute("INSERT INTO vod_feedback(guild_id,user_id,mentor_id,thread_id,good,improve,note,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                    (guild_id, user_id, mentor_id, thread_id, good, improve, note, utcnow_iso())).lastrowid)
    finally:
        conn.close()


def missing_for_week(db_path: str, guild_id: int, member_ids: List[int], week_start: datetime, minimum: int) -> List[Tuple[int, int]]:
    """(user_id, videos) de quienes no llegaron al mínimo en esa semana. No cuentan quienes no tienen post o cuyo post se creó durante esa semana (o después)."""
    end = week_start + timedelta(weeks=1)
    counts = counts_between(db_path, guild_id, week_start, end)
    out = []
    for uid in member_ids:
        created = post_created_at(db_path, guild_id, uid)
        if not created or _parse(created) >= week_start:
            continue                                         # sin post todavía, o recién ingresó: no se le exige esa semana
        n = counts.get(uid, 0)
        if n < minimum:
            out.append((uid, n))
    return out


def claim_week_result(db_path: str, guild_id: int, week_key_: str, user_id: int, videos: int) -> bool:
    """Registra que ya se evaluó a esa persona en esa semana. False si ya estaba (así no se le pone dos veces el warning)."""
    conn = connect(db_path)
    try:
        return conn.execute("INSERT OR IGNORE INTO vod_week_results(guild_id,week_start,user_id,videos,warned) VALUES (?,?,?,?,1)",
                            (guild_id, week_key_, user_id, videos)).rowcount > 0
    finally:
        conn.close()


def is_staff_or_council(member) -> bool:
    if can_manage(member):
        return True
    return any(getattr(r, "id", None) == STAFF_ROLE_ID for r in getattr(member, "roles", []))


# ---------------------------------------------------------------- vistas
class RoleSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(placeholder="¿De qué rol es el video?", options=[discord.SelectOption(label=r.capitalize(), value=r) for r in MENTOR_ROLES])

    async def callback(self, interaction: discord.Interaction):
        await self.cog.request_feedback(interaction, self.values[0])


class RoleSelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.add_item(RoleSelect(cog))


class FeedbackModal(discord.ui.Modal, title="Devolución de VOD"):
    good = discord.ui.TextInput(label="Qué estuvo bien", style=discord.TextStyle.paragraph, max_length=800)
    improve = discord.ui.TextInput(label="Qué corregir", style=discord.TextStyle.paragraph, max_length=800)
    note = discord.ui.TextInput(label="Nota extra (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, cog, thread_id: int, target_id: int):
        super().__init__()
        self.cog, self.thread_id, self.target_id = cog, thread_id, target_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.save_feedback(interaction, self.thread_id, self.target_id, str(self.good), str(self.improve), str(self.note or ""))


class VodPanelView(discord.ui.View):
    """Botones fijos dentro de cada post (persistentes: funcionan después de reiniciar el bot)."""
    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🙋 Pedir devolución", style=discord.ButtonStyle.primary, custom_id="vods:pedir")
    async def pedir(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ask_role(interaction)

    @discord.ui.button(label="📝 Dar devolución", style=discord.ButtonStyle.secondary, custom_id="vods:dar")
    async def dar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_feedback(interaction)


# ---------------------------------------------------------------- cog
class VodsCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.group = app_commands.Group(name="vods", description="Seguimiento de VODs y mentores")
        self._build_commands()

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)
        self.bot.add_view(VodPanelView(self))
        self.bot.tree.add_command(self.group, guild=discord.Object(id=self.guild_id))
        if not self.weekly.is_running():
            self.weekly.start()

    async def cog_unload(self):
        self.weekly.cancel()
        self.bot.tree.remove_command("vods", guild=discord.Object(id=self.guild_id))

    async def run(self, fn, *a, **k):
        return await asyncio.to_thread(fn, self.db_path, *a, **k)

    @staticmethod
    async def say(interaction, text, **kw):
        kw.setdefault("allowed_mentions", NO_MENTIONS)
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, **kw)
        return await interaction.response.send_message(text, ephemeral=True, **kw)

    # ---------- posts ----------
    async def get_forum(self, guild) -> Optional[discord.ForumChannel]:
        cfg = await self.run(get_config, guild.id)
        if not cfg["forum_id"]:
            return None
        ch = guild.get_channel(int(cfg["forum_id"]))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(cfg["forum_id"]))
            except Exception:
                return None
        return ch if isinstance(ch, discord.ForumChannel) else None

    async def ensure_post(self, guild, member) -> Optional[discord.Thread]:
        """Devuelve el post de la persona, creándolo si no existe (o si lo borraron)."""
        forum = await self.get_forum(guild)
        if forum is None or member.bot:
            return None
        tid = await self.run(get_post, guild.id, member.id)
        if tid:
            th = guild.get_thread(tid)
            if th is None:
                try:
                    th = await self.bot.fetch_channel(tid)
                except Exception:
                    th = None
            if th is not None:
                return th
        cfg = await self.run(get_config, guild.id)
        text = (f"👋 {member.mention}, este es **tu post de VODs**. Subí acá **todos** tus videos (archivo o link de YouTube/Twitch): "
                f"el mínimo es **{cfg['min_per_week']} por semana** (semana de lunes a domingo, hora de Argentina).\n"
                f"Con **🙋 Pedir devolución** avisás a los mentores de tu rol; ellos responden con **📝 Dar devolución**.")
        created = await forum.create_thread(name=f"VODs · {member.display_name}"[:100], content=text, view=VodPanelView(self),
                                            allowed_mentions=discord.AllowedMentions(users=[member]))
        thread = created.thread if hasattr(created, "thread") else created
        await self.run(save_post, guild.id, member.id, thread.id)
        try:
            await thread.add_user(member)
        except Exception:
            pass
        return thread

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if MIEMBRO_ROLE_ID in {r.id for r in after.roles} and MIEMBRO_ROLE_ID not in {r.id for r in before.roles} and after.guild.id == self.guild_id:
            try:
                await self.ensure_post(after.guild, after)
            except Exception:
                traceback.print_exc()

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        """Red de seguridad: si alguien (que no sea el bot ni el Staff) abre un post nuevo en el foro, se borra y se le avisa."""
        try:
            if thread.guild.id != self.guild_id:
                return
            cfg = await self.run(get_config, thread.guild.id)
            if not cfg["forum_id"] or thread.parent_id != int(cfg["forum_id"]) or thread.owner_id == self.bot.user.id:
                return
            owner = thread.guild.get_member(thread.owner_id)
            if owner is not None and is_staff_or_council(owner):
                return
            await thread.delete()
            if owner is not None:
                mine = await self.run(get_post, thread.guild.id, owner.id)
                where = f"<#{mine}>" if mine else "el que te crea el bot"
                try:
                    await owner.send(f"En el foro de VODs no se pueden crear posts nuevos: subí tus videos en tu post ({where}).")
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload):
        try:
            await self.run(delete_post_by_thread, payload.thread_id)
        except Exception:
            traceback.print_exc()

    # ---------- videos ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.guild.id != self.guild_id or message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        try:
            owner = await self.run(owner_of_thread, message.channel.id)
            if not owner or owner[1] != message.author.id or not is_video_message(message.attachments, message.content):
                return
            if await self.run(add_video, message.guild.id, message.author.id, message.channel.id, message.id, message.created_at):
                try:
                    await message.add_reaction("✅")
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        try:
            await self.run(remove_video, payload.message_id)
        except Exception:
            traceback.print_exc()

    # ---------- botones ----------
    async def _thread_owner(self, interaction) -> Optional[Tuple[int, int]]:
        return await self.run(owner_of_thread, interaction.channel_id)

    async def ask_role(self, interaction):
        owner = await self._thread_owner(interaction)
        if not owner:
            return await self.say(interaction, "Este botón solo funciona dentro de un post de VODs.")
        if owner[1] != interaction.user.id:
            return await self.say(interaction, "Solo la persona dueña del post puede pedir devolución.")
        await interaction.response.send_message("Elegí el rol del video:", view=RoleSelectView(self), ephemeral=True)

    async def request_feedback(self, interaction, role: str):
        owner = await self._thread_owner(interaction)
        if not owner or owner[1] != interaction.user.id:
            return await self.say(interaction, "Solo la persona dueña del post puede pedir devolución.")
        ms = [u for u, _ in await self.run(mentors, owner[0], role) if u != interaction.user.id]
        if not ms:
            return await interaction.response.edit_message(content=f"Todavía no hay mentores de **{role}**. Avisale al Staff.", view=None)
        mentions = " ".join(f"<@{u}>" for u in ms)
        await interaction.channel.send(f"🙋 {interaction.user.mention} pide devolución de un VOD de **{role}**. Mentores: {mentions}",
                                       allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=u) for u in ms]))
        await interaction.response.edit_message(content="✅ Avisé a los mentores.", view=None)

    async def open_feedback(self, interaction):
        owner = await self._thread_owner(interaction)
        if not owner:
            return await self.say(interaction, "Este botón solo funciona dentro de un post de VODs.")
        is_mentor = bool([1 for u, _ in await self.run(mentors, owner[0]) if u == interaction.user.id])
        if not (is_mentor or is_staff_or_council(interaction.user)):
            return await self.say(interaction, "🔒 Solo los mentores (o el Staff) pueden dar devoluciones.")
        if owner[1] == interaction.user.id:
            return await self.say(interaction, "No podés darte devolución a vos mismo.")
        await interaction.response.send_modal(FeedbackModal(self, interaction.channel_id, owner[1]))

    async def save_feedback(self, interaction, thread_id: int, target_id: int, good: str, improve: str, note: str):
        guild_id = interaction.guild_id
        await self.run(add_feedback, guild_id, target_id, interaction.user.id, thread_id, good.strip(), improve.strip(), note.strip())
        emb = discord.Embed(title="📝 Devolución", color=discord.Color.green(), description=f"Mentor: {interaction.user.mention} → <@{target_id}>")
        emb.add_field(name="✅ Qué estuvo bien", value=good.strip()[:1000], inline=False)
        emb.add_field(name="🔧 Qué corregir", value=improve.strip()[:1000], inline=False)
        if note.strip():
            emb.add_field(name="💬 Nota", value=note.strip()[:600], inline=False)
        await interaction.response.send_message(embed=emb, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=target_id)]))

    # ---------- semana ----------
    async def member_ids(self, guild) -> List[int]:
        role = guild.get_role(MIEMBRO_ROLE_ID)
        return [m.id for m in role.members if not m.bot] if role else []

    async def post_notice(self, guild, text: str):
        cfg = await self.run(get_config, guild.id)
        if cfg["notice_channel_id"]:
            ch = guild.get_channel(int(cfg["notice_channel_id"]))
            if ch is not None:
                try:
                    await ch.send(text[:1990], allowed_mentions=NO_MENTIONS)
                except Exception:
                    traceback.print_exc()

    async def check_week(self, guild, now: datetime):
        """Lunes: evalúa la semana anterior y pone warning a quien no llegó al mínimo."""
        cfg = await self.run(get_config, guild.id)
        if not cfg["forum_id"]:
            return
        this_week = week_start_of(now)
        prev = this_week - timedelta(weeks=1)
        pk = prev.strftime("%Y-%m-%d")
        if cfg["last_week_checked"] == pk or (now - this_week) < timedelta(minutes=5):
            return
        members = await self.member_ids(guild)
        if not members:
            return
        missing = await self.run(missing_for_week, guild.id, members, prev, int(cfg["min_per_week"]))
        lines = []
        for uid, n in missing:
            if await self.run(claim_week_result, guild.id, pk, uid, n):
                reason = f"No subió los VODs mínimos de la semana {prev:%d/%m}–{(prev + timedelta(days=6)):%d/%m} ({n}/{cfg['min_per_week']})"
                try:
                    await self.run(add_warning, guild.id, uid, reason, self.bot.user.id)
                except Exception:
                    traceback.print_exc()
                tid = await self.run(get_post, guild.id, uid)
                th = guild.get_thread(tid) if tid else None
                if th is not None:
                    try:
                        await th.send(f"⚠️ <@{uid}> la semana pasada subiste **{n}/{cfg['min_per_week']}** videos. Se te cargó un warning.",
                                      allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid)]))
                    except Exception:
                        pass
                lines.append(f"• <@{uid}> — {n}/{cfg['min_per_week']}")
        await self.run(set_config, guild.id, last_week_checked=pk)
        await self.post_notice(guild, f"📹 **VODs — semana {prev:%d/%m}–{(prev + timedelta(days=6)):%d/%m}**: "
                                      + (f"{len(lines)} con warning por no llegar al mínimo ({cfg['min_per_week']}):\n" + "\n".join(lines) if lines else "todos cumplieron. 🎉"))

    async def remind_week(self, guild, now: datetime):
        """Sábado por la tarde: avisa a quien todavía no llegó al mínimo de la semana en curso."""
        cfg = await self.run(get_config, guild.id)
        if not cfg["forum_id"]:
            return
        ws = week_start_of(now)
        key = ws.strftime("%Y-%m-%d")
        a = now.astimezone(ART)
        if cfg["last_reminder_week"] == key or a.weekday() < REMINDER_WEEKDAY or (a.weekday() == REMINDER_WEEKDAY and a.hour < REMINDER_HOUR):
            return
        members = await self.member_ids(guild)
        missing = await self.run(missing_for_week, guild.id, members, ws, int(cfg["min_per_week"]))
        await self.run(set_config, guild.id, last_reminder_week=key)
        lines = []
        for uid, n in missing:
            tid = await self.run(get_post, guild.id, uid)
            th = guild.get_thread(tid) if tid else None
            if th is not None:
                try:
                    await th.send(f"⏰ <@{uid}> esta semana llevás **{n}/{cfg['min_per_week']}** videos. Te quedan hasta el domingo para llegar al mínimo.",
                                  allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid)]))
                except Exception:
                    pass
            lines.append(f"• <@{uid}> — {n}/{cfg['min_per_week']}")
        if lines:
            await self.post_notice(guild, f"⏰ **VODs — faltan videos esta semana** ({len(lines)}):\n" + "\n".join(lines))

    @tasks.loop(minutes=10)
    async def weekly(self):
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return
        now = datetime.now(timezone.utc)
        for fn in (self.check_week, self.remind_week):
            try:
                await fn(guild, now)
            except Exception:
                traceback.print_exc()

    @weekly.before_loop
    async def _before_weekly(self):
        await self.bot.wait_until_ready()

    # ---------- comandos ----------
    def _build_commands(self):
        g = self.group
        cog = self

        async def gate_admin(interaction) -> bool:
            if interaction.guild is None or not can_manage(interaction.user):
                await cog.say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
                return False
            return True

        async def gate_staff(interaction) -> bool:
            if interaction.guild is None or not is_staff_or_council(interaction.user):
                await cog.say(interaction, "🔒 Solo el Staff o el Council pueden usar este comando.")
                return False
            return True

        @g.command(name="config", description="(Council) Foro de VODs, mínimo por semana y canal de avisos")
        @app_commands.describe(foro="Canal de foro donde el bot crea los posts", minimo="Videos mínimos por semana", avisos="Canal donde se publican los resúmenes semanales")
        async def config(interaction: discord.Interaction, foro: Optional[discord.ForumChannel] = None, minimo: Optional[app_commands.Range[int, 1, 50]] = None,
                         avisos: Optional[discord.TextChannel] = None):
            if not await gate_admin(interaction):
                return
            fields = {}
            if foro is not None:
                fields["forum_id"] = foro.id
            if minimo is not None:
                fields["min_per_week"] = int(minimo)
            if avisos is not None:
                fields["notice_channel_id"] = avisos.id
            cfg = await cog.run(set_config, interaction.guild_id, **fields)
            await cog.say(interaction, "⚙️ **VODs**\n"
                          f"Foro: {'<#%s>' % cfg['forum_id'] if cfg['forum_id'] else 'sin configurar'}\n"
                          f"Mínimo por semana: **{cfg['min_per_week']}**\n"
                          f"Avisos: {'<#%s>' % cfg['notice_channel_id'] if cfg['notice_channel_id'] else 'sin configurar'}")

        @g.command(name="permisos", description="(Council) Que los miembros no puedan crear posts en el foro, solo escribir en el suyo")
        async def permisos(interaction: discord.Interaction):
            if not await gate_admin(interaction):
                return
            forum = await cog.get_forum(interaction.guild)
            role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
            if forum is None or role is None:
                return await cog.say(interaction, "Primero configurá el foro con `/vods config`.")
            try:
                await forum.set_permissions(role, view_channel=True, send_messages=False, create_public_threads=False, send_messages_in_threads=True,
                                            attach_files=True, embed_links=True, add_reactions=True, reason="VODs: un post por miembro")
            except Exception as exc:
                return await cog.say(interaction, f"No pude cambiar los permisos ({exc.__class__.__name__}). Revisá que el bot tenga «Administrar canales/roles».")
            await cog.say(interaction, f"✅ En {forum.mention} los Miembros ya no pueden crear posts; solo escriben en el suyo.")

        @g.command(name="crear-posts", description="(Council) Crear el post de VODs de todos los miembros que todavía no tienen")
        async def crear_posts(interaction: discord.Interaction):
            if not await gate_admin(interaction):
                return
            if await cog.get_forum(interaction.guild) is None:
                return await cog.say(interaction, "Primero configurá el foro con `/vods config`.")
            await interaction.response.defer(ephemeral=True)
            made = failed = 0
            for uid in await cog.member_ids(interaction.guild):
                if await cog.run(get_post, interaction.guild_id, uid):
                    continue
                m = interaction.guild.get_member(uid)
                try:
                    if m is not None and await cog.ensure_post(interaction.guild, m):
                        made += 1
                except Exception:
                    traceback.print_exc()
                    failed += 1
                await asyncio.sleep(1.2)                    # no pasarse del límite de Discord
            await cog.say(interaction, f"✅ Posts creados: **{made}**" + (f" · con error: **{failed}**" if failed else ""))

        @g.command(name="ver", description="Ver los videos de una persona (por defecto, los tuyos)")
        @app_commands.describe(usuario="De quién (solo Staff/Council/mentores pueden ver a otros)")
        async def ver(interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
            target = usuario or interaction.user
            if target.id != interaction.user.id and not (is_staff_or_council(interaction.user) or any(u == interaction.user.id for u, _ in await cog.run(mentors, interaction.guild_id))):
                return await cog.say(interaction, "🔒 Solo podés ver tus propios VODs.")
            cfg = await cog.run(get_config, interaction.guild_id)
            st = await cog.run(user_stats, interaction.guild_id, target.id, datetime.now(timezone.utc))
            mn = int(cfg["min_per_week"])
            rows = [f"{'✅' if n >= mn else '⏳' if i == 0 else '❌'} {s:%d/%m}–{(s + timedelta(days=6)):%d/%m}: **{n}/{mn}**" + (" (en curso)" if i == 0 else "")
                    for i, (s, n) in enumerate(st["weeks"])]
            tid = await cog.run(get_post, interaction.guild_id, target.id)
            emb = discord.Embed(title=f"📹 VODs de {target.display_name}", color=discord.Color.blurple(), description="\n".join(rows))
            emb.add_field(name="Total", value=str(st["total"]), inline=True)
            emb.add_field(name="Devoluciones", value=str(st["feedback"]), inline=True)
            emb.add_field(name="Semanas con warning", value=str(st["warned"]), inline=True)
            if st["last"]:
                emb.add_field(name="Último video", value=f"<t:{int(_parse(st['last']).timestamp())}:R>", inline=True)
            if tid:
                emb.add_field(name="Su post", value=f"<#{tid}>", inline=True)
            await interaction.response.send_message(embed=emb, ephemeral=True, allowed_mentions=NO_MENTIONS)

        @g.command(name="resumen", description="(Staff/Council) Quién todavía no llegó al mínimo esta semana")
        async def resumen(interaction: discord.Interaction):
            if not await gate_staff(interaction):
                return
            cfg = await cog.run(get_config, interaction.guild_id)
            ws = week_start_of(datetime.now(timezone.utc))
            members = await cog.member_ids(interaction.guild)
            missing = await cog.run(missing_for_week, interaction.guild_id, members, ws, int(cfg["min_per_week"]))
            ok = len(members) - len(missing)
            lines = [f"• <@{u}> — {n}/{cfg['min_per_week']}" for u, n in sorted(missing, key=lambda x: x[1])]
            text = f"📹 **Semana {ws:%d/%m}** · cumplen: **{ok}** · les falta: **{len(missing)}**\n" + ("\n".join(lines) if lines else "Todos cumplen. 🎉")
            await cog.say(interaction, text[:1990])

        @g.command(name="mentor-agregar", description="(Council) Hacer mentor a alguien de un rol")
        @app_commands.describe(usuario="Quién", rol="Rol que mentorea")
        @app_commands.choices(rol=[app_commands.Choice(name=r.capitalize(), value=r) for r in MENTOR_ROLES])
        async def mentor_agregar(interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str]):
            if not await gate_admin(interaction):
                return
            role = getattr(rol, "value", rol)
            new = await cog.run(add_mentor, interaction.guild_id, usuario.id, role)
            await cog.say(interaction, f"✅ {usuario.mention} es mentor de **{role}**." if new else f"{usuario.mention} ya era mentor de **{role}**.")

        @g.command(name="mentor-quitar", description="(Council) Sacar a alguien como mentor")
        @app_commands.describe(usuario="Quién", rol="Un rol puntual; vacío = todos")
        @app_commands.choices(rol=[app_commands.Choice(name=r.capitalize(), value=r) for r in MENTOR_ROLES])
        async def mentor_quitar(interaction: discord.Interaction, usuario: discord.Member, rol: Optional[app_commands.Choice[str]] = None):
            if not await gate_admin(interaction):
                return
            n = await cog.run(remove_mentor, interaction.guild_id, usuario.id, getattr(rol, "value", rol) if rol else None)
            await cog.say(interaction, f"✅ Listo, se quitó a {usuario.mention} como mentor." if n else f"{usuario.mention} no era mentor.")

        @g.command(name="mentores", description="Lista de mentores por rol")
        async def mentores(interaction: discord.Interaction):
            rows = await cog.run(mentors, interaction.guild_id)
            by = {}
            for u, r in rows:
                by.setdefault(r, []).append(f"<@{u}>")
            text = "\n".join(f"**{r.capitalize()}**: {', '.join(by.get(r, [])) or '—'}" for r in MENTOR_ROLES)
            await cog.say(interaction, "🎓 **Mentores**\n" + text)


async def setup_vods(bot, db_path: str, guild_id: int) -> VodsCog:
    cog = VodsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
