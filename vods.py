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
ROLE_IDS = {"tank": 1260755129754189854, "healer": 1260755151296266331, "supp": 1260755342472646656, "dps": 1260755289062248458}   # roles de Discord de combate
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
    mentor_channel_id INTEGER,
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
CREATE TABLE IF NOT EXISTS vod_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
    role_label TEXT NOT NULL DEFAULT '', channel_id INTEGER, message_id INTEGER, videos_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open', mentor_id INTEGER, feedback_id INTEGER,
    created_at TEXT NOT NULL, taken_at TEXT, done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_vod_tickets_user ON vod_tickets(guild_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_vod_tickets_msg ON vod_tickets(message_id);
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
        try:
            conn.execute("ALTER TABLE vod_config ADD COLUMN mentor_channel_id INTEGER")
        except Exception:
            pass                                             # ya existe
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
    allowed = {"forum_id", "min_per_week", "notice_channel_id", "mentor_channel_id", "last_week_checked", "last_reminder_week"}
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


import json  # noqa: E402


def _tk(conn, row):
    if row is None:
        return None
    d = dict(row)
    d["videos"] = json.loads(d.pop("videos_json") or "[]")
    return d


def get_open_ticket(db_path: str, guild_id: int, user_id: int) -> Optional[dict]:
    """Ticket de esa persona que todavía no se respondió (pendiente o tomado)."""
    conn = connect(db_path)
    try:
        return _tk(conn, conn.execute("SELECT * FROM vod_tickets WHERE guild_id=? AND user_id=? AND status IN ('open','taken') ORDER BY id DESC LIMIT 1", (guild_id, user_id)).fetchone())
    finally:
        conn.close()


def get_ticket(db_path: str, ticket_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _tk(conn, conn.execute("SELECT * FROM vod_tickets WHERE id=?", (ticket_id,)).fetchone())
    finally:
        conn.close()


def get_ticket_by_message(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _tk(conn, conn.execute("SELECT * FROM vod_tickets WHERE message_id=?", (message_id,)).fetchone())
    finally:
        conn.close()


def create_ticket(db_path: str, guild_id: int, user_id: int, thread_id: int, role_label: str, video_url: Optional[str]) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return int(conn.execute("INSERT INTO vod_tickets(guild_id,user_id,thread_id,role_label,videos_json,created_at) VALUES (?,?,?,?,?,?)",
                                    (guild_id, user_id, thread_id, role_label, json.dumps([video_url] if video_url else []), utcnow_iso())).lastrowid)
    finally:
        conn.close()


def ticket_add_video(db_path: str, ticket_id: int, url: str) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT videos_json FROM vod_tickets WHERE id=?", (ticket_id,)).fetchone()
            vids = json.loads(r["videos_json"] or "[]") if r else []
            if url not in vids:
                vids.append(url)
                conn.execute("UPDATE vod_tickets SET videos_json=? WHERE id=?", (json.dumps(vids[-20:]), ticket_id))
    finally:
        conn.close()


def ticket_set_message(db_path: str, ticket_id: int, channel_id: int, message_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute("UPDATE vod_tickets SET channel_id=?, message_id=? WHERE id=?", (channel_id, message_id, ticket_id))
    finally:
        conn.close()


def ticket_take(db_path: str, ticket_id: int, mentor_id: int) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("UPDATE vod_tickets SET status='taken', mentor_id=?, taken_at=? WHERE id=? AND status='open'", (mentor_id, utcnow_iso(), ticket_id)).rowcount > 0
    finally:
        conn.close()


def ticket_release(db_path: str, ticket_id: int, mentor_id: int, force: bool = False) -> bool:
    conn = connect(db_path)
    try:
        q = "UPDATE vod_tickets SET status='open', mentor_id=NULL, taken_at=NULL WHERE id=? AND status='taken'" + ("" if force else " AND mentor_id=?")
        return conn.execute(q, (ticket_id,) if force else (ticket_id, mentor_id)).rowcount > 0
    finally:
        conn.close()


def ticket_finish(db_path: str, ticket_id: int, mentor_id: int, feedback_id: int) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("UPDATE vod_tickets SET status='done', mentor_id=?, feedback_id=?, done_at=?, taken_at=COALESCE(taken_at, ?) WHERE id=? AND status IN ('open','taken')",
                            (mentor_id, feedback_id, utcnow_iso(), utcnow_iso(), ticket_id)).rowcount > 0
    finally:
        conn.close()


def top_mentors(db_path: str, guild_id: int, since_iso: Optional[str] = None, limit: int = 10) -> List[dict]:
    """Mentores con más devoluciones dadas (y el tiempo promedio que tardan desde que se pidió, si hay ticket)."""
    conn = connect(db_path)
    try:
        q = ("SELECT f.mentor_id, COUNT(*) n, AVG((julianday(t.done_at) - julianday(t.created_at)) * 24) hours "
             "FROM vod_feedback f LEFT JOIN vod_tickets t ON t.feedback_id=f.id WHERE f.guild_id=?")
        p: list = [guild_id]
        if since_iso:
            q, p = q + " AND f.created_at>=?", p + [since_iso]
        return [dict(r) for r in conn.execute(q + " GROUP BY f.mentor_id ORDER BY n DESC, f.mentor_id LIMIT ?", p + [limit])]
    finally:
        conn.close()


def combat_role_of(member) -> str:
    """Rol de combate de la persona según sus roles de Discord ('' si no tiene ninguno)."""
    ids = {getattr(r, "id", None) for r in getattr(member, "roles", [])}
    for label in MENTOR_ROLES:
        if ROLE_IDS[label] in ids:
            return label
    return ""


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

    def __init__(self, cog, thread_id: int, target_id: int, ticket_id: Optional[int] = None):
        super().__init__()
        self.cog, self.thread_id, self.target_id, self.ticket_id = cog, thread_id, target_id, ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.save_feedback(interaction, self.thread_id, self.target_id, str(self.good), str(self.improve), str(self.note or ""), self.ticket_id)


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


class TicketView(discord.ui.View):
    """Botones del ticket en el canal de mentores (persistentes)."""
    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🙋 Tomar", style=discord.ButtonStyle.primary, custom_id="vods:t:take")
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_take_button(interaction)

    @discord.ui.button(label="📝 Dar devolución", style=discord.ButtonStyle.success, custom_id="vods:t:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_done_button(interaction)

    @discord.ui.button(label="↩️ Soltar", style=discord.ButtonStyle.secondary, custom_id="vods:t:release")
    async def release(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_release_button(interaction)


# ---------------------------------------------------------------- cog
class VodsCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.group = app_commands.Group(name="vods", description="Seguimiento de VODs y mentores")
        self._build_commands()

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)
        self.bot.add_view(VodPanelView(self))
        self.bot.add_view(TicketView(self))
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
                await self.ticket_for_video(message)
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
        t = await self.upsert_ticket(interaction.guild, interaction.user, interaction.channel_id, role, None)
        if t is None:
            return await interaction.response.edit_message(content="El canal de mentores todavía no está configurado. Avisale al Staff.", view=None)
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
        t = await self.run(get_open_ticket, owner[0], owner[1])
        if t and t["status"] == "taken" and t["mentor_id"] not in (None, interaction.user.id) and not is_staff_or_council(interaction.user):
            return await self.say(interaction, f"Este VOD ya lo tomó <@{t['mentor_id']}>.")
        await interaction.response.send_modal(FeedbackModal(self, interaction.channel_id, owner[1], t["id"] if t else None))

    async def save_feedback(self, interaction, thread_id: int, target_id: int, good: str, improve: str, note: str, ticket_id: Optional[int] = None):
        guild = interaction.guild
        fid = await self.run(add_feedback, guild.id, target_id, interaction.user.id, thread_id, good.strip(), improve.strip(), note.strip())
        emb = discord.Embed(title="📝 Devolución", color=discord.Color.green(), description=f"Mentor: {interaction.user.mention} → <@{target_id}>")
        emb.add_field(name="✅ Qué estuvo bien", value=good.strip()[:1000], inline=False)
        emb.add_field(name="🔧 Qué corregir", value=improve.strip()[:1000], inline=False)
        if note.strip():
            emb.add_field(name="💬 Nota", value=note.strip()[:600], inline=False)
        th = guild.get_thread(thread_id) or interaction.channel
        try:
            await th.send(embed=emb, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=target_id)]))
        except Exception:
            traceback.print_exc()
        if ticket_id:
            if await self.run(ticket_finish, ticket_id, interaction.user.id, fid):
                await self.refresh_ticket(guild, await self.run(get_ticket, ticket_id))
        await self.say(interaction, f"✅ Devolución enviada en <#{thread_id}>.")

    # ---------- tickets de mentores ----------
    async def mentor_channel(self, guild):
        cfg = await self.run(get_config, guild.id)
        if not cfg["mentor_channel_id"]:
            return None
        ch = guild.get_channel(int(cfg["mentor_channel_id"]))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(cfg["mentor_channel_id"]))
            except Exception:
                return None
        return ch

    @staticmethod
    def ticket_embed(guild_id: int, t: dict) -> discord.Embed:
        state = {"open": "🟡 Pendiente", "taken": f"🔵 Tomado por <@{t['mentor_id']}>", "done": f"✅ Respondido por <@{t['mentor_id']}>"}[t["status"]]
        color = {"open": discord.Color.gold(), "taken": discord.Color.blue(), "done": discord.Color.green()}[t["status"]]
        emb = discord.Embed(title=f"🎬 VOD de {(t['role_label'] or 'sin rol').capitalize()}", color=color, description=f"<@{t['user_id']}> subió un video.")
        emb.add_field(name="Estado", value=state, inline=True)
        emb.add_field(name="Post", value=f"<#{t['thread_id']}>", inline=True)
        vids = "\n".join(f"[Video {i}]({u})" for i, u in enumerate(t["videos"][-10:], start=max(1, len(t["videos"]) - 9))) or "—"
        emb.add_field(name=f"Videos ({len(t['videos'])})", value=vids[:1000], inline=False)
        emb.set_footer(text=f"Ticket #{t['id']}")
        return emb

    async def refresh_ticket(self, guild, t: Optional[dict]):
        if not t or not t["message_id"] or not t["channel_id"]:
            return
        try:
            ch = guild.get_channel(int(t["channel_id"])) or await self.bot.fetch_channel(int(t["channel_id"]))
            msg = await ch.fetch_message(int(t["message_id"]))
            await msg.edit(embed=self.ticket_embed(guild.id, t), view=TicketView(self) if t["status"] != "done" else None, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def upsert_ticket(self, guild, member, thread_id: int, role: str, video_url: Optional[str]) -> Optional[dict]:
        """Un ticket abierto por persona: los videos nuevos se suman al que sigue pendiente; si no hay, se crea y se avisa a los mentores del rol."""
        ch = await self.mentor_channel(guild)
        if ch is None:
            return None
        t = await self.run(get_open_ticket, guild.id, member.id)
        if t:
            if video_url:
                await self.run(ticket_add_video, t["id"], video_url)
                t = await self.run(get_ticket, t["id"])
                await self.refresh_ticket(guild, t)
            return t
        tid = await self.run(create_ticket, guild.id, member.id, thread_id, role, video_url)
        t = await self.run(get_ticket, tid)
        ms = [u for u, _ in await self.run(mentors, guild.id, role)] if role else []
        ms = [u for u in ms if u != member.id]
        if ms:
            ping = " ".join(f"<@{u}>" for u in ms)
            allowed = discord.AllowedMentions(users=[discord.Object(id=u) for u in ms])
        else:
            ping = f"<@&{STAFF_ROLE_ID}> no hay mentores de **{role or 'ese rol'}** cargados."
            allowed = discord.AllowedMentions(roles=[discord.Object(id=STAFF_ROLE_ID)])
        msg = await ch.send(ping, embed=self.ticket_embed(guild.id, t), view=TicketView(self), allowed_mentions=allowed)
        await self.run(ticket_set_message, tid, ch.id, msg.id)
        return await self.run(get_ticket, tid)

    async def ticket_for_video(self, message):
        try:
            role = combat_role_of(message.author)
            url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
            await self.upsert_ticket(message.guild, message.author, message.channel.id, role, url)
        except Exception:
            traceback.print_exc()

    async def _ticket_from(self, interaction) -> Optional[dict]:
        return await self.run(get_ticket_by_message, interaction.message.id)

    def _can_mentor(self, interaction, t: dict, mentor_rows) -> bool:
        if interaction.user.id == t["user_id"]:
            return False
        if is_staff_or_council(interaction.user):
            return True
        return any(u == interaction.user.id and (not t["role_label"] or r == t["role_label"]) for u, r in mentor_rows)

    async def ticket_take_button(self, interaction):
        t = await self._ticket_from(interaction)
        if not t:
            return await self.say(interaction, "No encontré este ticket.")
        if not self._can_mentor(interaction, t, await self.run(mentors, interaction.guild_id)):
            return await self.say(interaction, "🔒 Solo los mentores de ese rol (o el Staff) pueden tomarlo, y no el dueño del video.")
        if not await self.run(ticket_take, t["id"], interaction.user.id):
            return await self.say(interaction, "Ya lo tomó otra persona (o ya está respondido).")
        t = await self.run(get_ticket, t["id"])
        await interaction.response.edit_message(embed=self.ticket_embed(interaction.guild_id, t), view=TicketView(self))
        th = interaction.guild.get_thread(t["thread_id"])
        if th is not None:
            try:
                await th.send(f"🔵 {interaction.user.mention} tomó tu VOD, <@{t['user_id']}>: te va a dar la devolución acá.",
                              allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=t["user_id"])]))
            except Exception:
                pass

    async def ticket_release_button(self, interaction):
        t = await self._ticket_from(interaction)
        if not t or t["status"] != "taken":
            return await self.say(interaction, "Este ticket no está tomado.")
        if not await self.run(ticket_release, t["id"], interaction.user.id, is_staff_or_council(interaction.user)):
            return await self.say(interaction, "Solo quien lo tomó (o el Staff) puede soltarlo.")
        t = await self.run(get_ticket, t["id"])
        await interaction.response.edit_message(embed=self.ticket_embed(interaction.guild_id, t), view=TicketView(self))

    async def ticket_done_button(self, interaction):
        t = await self._ticket_from(interaction)
        if not t or t["status"] == "done":
            return await self.say(interaction, "Este ticket ya está respondido.")
        if t["status"] == "open":
            return await self.say(interaction, "Primero tomalo con **🙋 Tomar**.")
        if t["mentor_id"] != interaction.user.id and not is_staff_or_council(interaction.user):
            return await self.say(interaction, f"Lo tomó <@{t['mentor_id']}>.")
        await interaction.response.send_modal(FeedbackModal(self, t["thread_id"], t["user_id"], t["id"]))

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
        @app_commands.describe(foro="Canal de foro donde el bot crea los posts", minimo="Videos mínimos por semana", avisos="Canal donde se publican los resúmenes semanales",
                               canal_mentores="Canal de tickets donde los mentores toman los VODs")
        async def config(interaction: discord.Interaction, foro: Optional[discord.ForumChannel] = None, minimo: Optional[app_commands.Range[int, 1, 50]] = None,
                         avisos: Optional[discord.TextChannel] = None, canal_mentores: Optional[discord.TextChannel] = None):
            if not await gate_admin(interaction):
                return
            fields = {}
            if foro is not None:
                fields["forum_id"] = foro.id
            if minimo is not None:
                fields["min_per_week"] = int(minimo)
            if avisos is not None:
                fields["notice_channel_id"] = avisos.id
            if canal_mentores is not None:
                fields["mentor_channel_id"] = canal_mentores.id
            cfg = await cog.run(set_config, interaction.guild_id, **fields)
            await cog.say(interaction, "⚙️ **VODs**\n"
                          f"Foro: {'<#%s>' % cfg['forum_id'] if cfg['forum_id'] else 'sin configurar'}\n"
                          f"Mínimo por semana: **{cfg['min_per_week']}**\n"
                          f"Avisos: {'<#%s>' % cfg['notice_channel_id'] if cfg['notice_channel_id'] else 'sin configurar'}\n"
                          f"Canal de mentores: {'<#%s>' % cfg['mentor_channel_id'] if cfg['mentor_channel_id'] else 'sin configurar'}")

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

        @g.command(name="top-mentores", description="Mentores que más devoluciones dieron")
        @app_commands.describe(periodo="Período (por defecto, todo el tiempo)")
        @app_commands.choices(periodo=[app_commands.Choice(name="Esta semana", value="semana"), app_commands.Choice(name="Últimos 30 días", value="mes"),
                                       app_commands.Choice(name="Siempre", value="todo")])
        async def top_mentores(interaction: discord.Interaction, periodo: Optional[app_commands.Choice[str]] = None):
            p = getattr(periodo, "value", periodo) or "todo"
            now = datetime.now(timezone.utc)
            since = {"semana": week_start_of(now).astimezone(timezone.utc).isoformat(), "mes": (now - timedelta(days=30)).isoformat(), "todo": None}[p]
            rows = await cog.run(top_mentors, interaction.guild_id, since)
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = []
            for i, r in enumerate(rows, start=1):
                avg = f" · responde en ~{r['hours']:.0f} h" if r["hours"] is not None else ""
                lines.append(f"{medals.get(i, f'`{i:>2}.`')} <@{r['mentor_id']}> — **{r['n']}** devolución{'es' if r['n'] != 1 else ''}{avg}")
            emb = discord.Embed(title=f"🎓 Top mentores · {dict(semana='esta semana', mes='30 días', todo='siempre')[p]}", color=discord.Color.green(),
                                description="\n".join(lines) or "Todavía nadie dio devoluciones.")
            await interaction.response.send_message(embed=emb, allowed_mentions=NO_MENTIONS)

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
