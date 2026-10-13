# ================== BOT ==================
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import time
import re
import io
import aiohttp
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv
import asyncio
import random
import sqlite3
from decimal import Decimal

# ================== TOKEN (env o /root/discordbot/.env) ==================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    env_path = "token.env"
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno o en /root/discordbot/.env")

# ================== BASE DE DATOS PERSISTENTE ==================
# Por defecto queda junto a bot.py. Se puede cambiar con BOT_DB_PATH.
DB_PATH = os.getenv("BOT_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.sqlite3"))


def db_execute(query: str, params: tuple = ()):
    """Ejecuta una escritura SQLite en una conexión corta y segura."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(query, params)
        conn.commit()


def db_fetchall(query: str, params: tuple = ()):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def init_database():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                prize TEXT NOT NULL,
                end_at TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS giveaway_entrants (
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (message_id, user_id),
                FOREIGN KEY (message_id) REFERENCES giveaways(message_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material TEXT NOT NULL,
                tier TEXT NOT NULL,
                map_name TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                warned_30 INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ticket_images (
                user_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                filename TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS split_log_ids (
                panel_message_id INTEGER PRIMARY KEY,
                log_message_id INTEGER NOT NULL
            );
        """)
        conn.commit()


init_database()

# ================== CONFIG ==================
GUILD_ID = 1257878770841288724
CATEGORY_ID = 1257902293609742346
LOG_CHANNEL_ID = 1462207061902496037

RECRUITER_ROLE_ID = 1257896905099444354
MIEMBRO_ROLE_ID = 1257896455860129822
TANK_ROLE_ID = 1260755129754189854
HEALER_ROLE_ID = 1260755151296266331
SUPP_ROLE_ID = 1260755342472646656
DPS_ROLE_ID = 1260755289062248458
BATTLE_MOUNT_ROLE_ID = 1469369363739181087

PUBLIC_ROLE_ID = 1266805315547041902
STAFF_ROLE_ID = 1257896709246423083

COOLDOWN_SECONDS = 60

# ✅ Timers
TIMERS_ROLE_ID = 1462515835326169159
TIMERS_ROLE_NAME_FALLBACK = "timers"

TIMER_ALERT_CHANNEL_ID = 1462184630835740732
TIMER_ALERT_MINUTES_BEFORE = 60

TIMERS_ALERT_ROLE_ID = 1258562816512884808

TIMERS_BOARD_CHANNEL_ID = 1462516135361777875
TIMERS_BOARD_TITLE = "⏱ Timers Activos"
timers_board_message_id: Optional[int] = None

MATERIALS_NORMAL = {"fibra", "cuero", "mineral", "madera"}
MATERIALS_SPECIAL = {"vortex", "core"}
MATERIALS_NO_TIER = {"dragon"}   # materiales que se publican sin tier

# Iconos centralizados por material (se usan en el anuncio de /timeradd)
MATERIAL_ICONS = {
    "fibra": "🌿",
    "cuero": "🐄",
    "mineral": "🪨",
    "madera": "🪵",
    "vortex": "🌀",
    "core": "💎",
    "dragon": "🐉",
}
DEFAULT_MATERIAL_ICON = "📦"

TIERS_NORMAL = {"4.4", "5.4", "6.4", "7.4", "8.4"}
TIERS_SPECIAL = {"common", "rare", "epic", "legendary"}

TIER_LABELS_SPECIAL = {
    "common": "Common (Verde)",
    "rare": "Rare (Azul)",
    "epic": "Epic (Violeta)",
    "legendary": "Legendary (Amarillo)",
}

ALBIONBB_REGION = os.getenv("ALBIONBB_REGION", "eu")

ALBIONBB_BASE_URL = f"https://api.albionbb.com/{ALBIONBB_REGION}"
ALBIONBB_BATTLE_URL = f"{ALBIONBB_BASE_URL}/battles"
ALBIONBB_KILLS_URL = f"{ALBIONBB_BASE_URL}/battles/kills"
# ================== FOCO DONOR TICKETS (NUEVO) ==================
FOCO_CATEGORY_ID = 1468340293571973273        
FOCO_LOG_CHANNEL_ID = 1468345144502915313      

FOCO_TOPIC_PREFIX = "FOCO_DONOR"

ticket_images = {}         
active_applications = {}
cooldowns = {}

# NUEVO: foco tickets
active_foco_tickets = {}   # user_id -> channel_id (ayuda rápida, pero además validamos por topic)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

GUILD_OBJ = discord.Object(id=GUILD_ID)

class MyBot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        try:
            await self.tree.sync(guild=GUILD_OBJ)
            print("✅ Slash commands sincronizados (guild) [setup_hook]")
        except Exception as e:
            print("❌ Error sync slash commands [setup_hook]:", e)

    async def close(self):
        if hasattr(self, "http_session"):
            await self.http_session.close()
        await super().close()
        
bot = MyBot(command_prefix="!", intents=intents)

# ================== TIMERS DATA ==================
@dataclass
class TimerItem:
    material: str
    tier: str
    map_name: str
    end_at: datetime
    created_by_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warned_30: bool = False
    db_id: Optional[int] = None

timers: List[TimerItem] = []

# ================== SORTEOS (NUEVO) ==================
@dataclass
class GiveawayItem:
    prize: str
    end_at: datetime
    channel_id: int
    message_id: int
    creator_id: int
    entrants: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

giveaways: dict[int, GiveawayItem] = {}  # message_id -> GiveawayItem


def save_giveaway(g: GiveawayItem, guild_id: int = GUILD_ID):
    db_execute(
        """INSERT INTO giveaways(message_id, prize, end_at, channel_id, creator_id, guild_id)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO UPDATE SET prize=excluded.prize, end_at=excluded.end_at,
             channel_id=excluded.channel_id, creator_id=excluded.creator_id, guild_id=excluded.guild_id""",
        (g.message_id, g.prize, g.end_at.astimezone(timezone.utc).isoformat(),
         g.channel_id, g.creator_id, guild_id),
    )


def save_giveaway_entrant(message_id: int, user_id: int):
    db_execute("INSERT OR IGNORE INTO giveaway_entrants(message_id, user_id) VALUES (?, ?)",
               (message_id, user_id))


def remove_giveaway(message_id: int):
    giveaways.pop(message_id, None)
    db_execute("DELETE FROM giveaway_entrants WHERE message_id = ?", (message_id,))
    db_execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))


def save_timer(t: TimerItem):
    values = (t.material, t.tier, t.map_name, t.end_at.astimezone(timezone.utc).isoformat(),
              t.created_by_id, t.created_at.astimezone(timezone.utc).isoformat(), int(t.warned_30))
    if t.db_id is None:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.execute(
                "INSERT INTO timers(material, tier, map_name, end_at, created_by_id, created_at, warned_30) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            t.db_id = cur.lastrowid
            conn.commit()
    else:
        db_execute(
            "UPDATE timers SET material=?, tier=?, map_name=?, end_at=?, created_by_id=?, created_at=?, warned_30=? WHERE id=?",
            values + (t.db_id,),
        )


def delete_timer(t: TimerItem):
    if t.db_id is not None:
        db_execute("DELETE FROM timers WHERE id = ?", (t.db_id,))


def save_ticket_image(user_id: int, url: str, filename: str):
    db_execute(
        "INSERT INTO ticket_images(user_id, url, filename) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET url=excluded.url, filename=excluded.filename",
        (user_id, url, filename),
    )
    ticket_images[user_id] = {"url": url, "filename": filename}


def delete_ticket_image(user_id: int):
    ticket_images.pop(user_id, None)
    db_execute("DELETE FROM ticket_images WHERE user_id = ?", (user_id,))


def save_split_log_mapping(panel_message_id: int, log_message_id: int):
    split_log_ids[panel_message_id] = log_message_id
    db_execute(
        "INSERT INTO split_log_ids(panel_message_id, log_message_id) VALUES (?, ?) "
        "ON CONFLICT(panel_message_id) DO UPDATE SET log_message_id=excluded.log_message_id",
        (panel_message_id, log_message_id),
    )


def restore_persistent_state(guild: discord.Guild):
    """Restaura sorteos, participantes, timers y referencias locales desde SQLite."""
    giveaways.clear()
    for row in db_fetchall("SELECT * FROM giveaways"):
        try:
            end_at = datetime.fromisoformat(row["end_at"])
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            g = GiveawayItem(
                prize=row["prize"], end_at=end_at.astimezone(timezone.utc),
                channel_id=int(row["channel_id"]), message_id=int(row["message_id"]),
                creator_id=int(row["creator_id"]),
            )
            g.entrants = {int(r["user_id"]) for r in db_fetchall(
                "SELECT user_id FROM giveaway_entrants WHERE message_id = ?", (g.message_id,)
            )}
            giveaways[g.message_id] = g
        except Exception as exc:
            print(f"⚠️ No pude restaurar el sorteo {row['message_id']}: {exc}")

    timers.clear()
    for row in db_fetchall("SELECT * FROM timers ORDER BY end_at"):
        try:
            end_at = datetime.fromisoformat(row["end_at"])
            created_at = datetime.fromisoformat(row["created_at"])
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            timers.append(TimerItem(
                material=row["material"], tier=row["tier"], map_name=row["map_name"],
                end_at=end_at.astimezone(timezone.utc), created_by_id=int(row["created_by_id"]),
                created_at=created_at.astimezone(timezone.utc), warned_30=bool(row["warned_30"]),
                db_id=int(row["id"]),
            ))
        except Exception as exc:
            print(f"⚠️ No pude restaurar un timer guardado: {exc}")

    ticket_images.clear()
    for row in db_fetchall("SELECT user_id, url, filename FROM ticket_images"):
        ticket_images[int(row["user_id"])] = {"url": row["url"], "filename": row["filename"]}

    split_log_ids.clear()
    for row in db_fetchall("SELECT panel_message_id, log_message_id FROM split_log_ids"):
        split_log_ids[int(row["panel_message_id"])] = int(row["log_message_id"])

    # Los canales con tickets abiertos ya existen en Discord: reconstruir los índices en memoria.
    active_applications.clear()
    active_foco_tickets.clear()
    for channel in guild.text_channels:
        topic = channel.topic or ""
        if topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
            uid = parse_recruit_topic(topic).get("uid")
            if uid:
                active_applications[int(uid)] = channel.id
        elif topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            uid = parse_foco_topic(topic).get("uid")
            if uid:
                active_foco_tickets[int(uid)] = channel.id


def start_giveaway_task(guild_id: int, g: GiveawayItem):
    tasks_by_id = getattr(bot, "_giveaway_tasks", None)
    if tasks_by_id is None:
        tasks_by_id = {}
        bot._giveaway_tasks = tasks_by_id
    current = tasks_by_id.get(g.message_id)
    if current is not None and not current.done():
        return
    tasks_by_id[g.message_id] = asyncio.create_task(run_giveaway_flow(guild_id, g))

def is_staff_member(member: discord.Member) -> bool:
    staff_role = member.guild.get_role(STAFF_ROLE_ID)
    return (staff_role is not None) and (staff_role in member.roles)

def build_giveaway_embed(g: GiveawayItem) -> discord.Embed:
    end_unix = int(g.end_at.timestamp())
    emb = discord.Embed(
        title="🕳️ Sorteo del topo",
        description="Apretá el botón para anotarte. Después no llores.",
        color=discord.Color.gold()
    )
    emb.add_field(name="🎁 Premio", value=f"`{g.prize}`", inline=False)
    emb.add_field(name="⏳ Termina", value=f"<t:{end_unix}:t> (<t:{end_unix}:R>)", inline=False)
    emb.add_field(name="👥 Anotados", value=str(len(g.entrants)), inline=True)
    emb.set_footer(text="Smogg Sorteos System")
    return emb

async def disable_giveaway_button(guild: discord.Guild, g: GiveawayItem):
    ch = guild.get_channel(g.channel_id)
    if ch is None:
        ch = await guild.fetch_channel(g.channel_id)

    if not isinstance(ch, discord.TextChannel):
        return

    try:
        msg = await ch.fetch_message(g.message_id)
    except Exception:
        return

    view = GiveawayJoinView()
    for child in view.children:
        child.disabled = True

    try:
        await msg.edit(embed=build_giveaway_embed(g), view=view)
    except Exception:
        pass

async def run_giveaway_flow(guild_id: int, g: GiveawayItem):
    # Esperar hasta el final
    now = datetime.now(timezone.utc)
    delay = (g.end_at - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    guild = bot.get_guild(guild_id)
    if not guild:
        # No borrar el registro persistente por un fallo temporal de caché/conexión.
        return

    # Deshabilitar botón
    await disable_giveaway_button(guild, g)

    ch = guild.get_channel(g.channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(g.channel_id)
        except Exception:
            # Se conserva en SQLite para reintentar en la próxima reconexión.
            return

    if not isinstance(ch, discord.TextChannel):
        return

    # Tomar participantes de forma segura
    async with g.lock:
        pool = list(g.entrants)

    if not pool:
        try:
            await ch.send("📭 No se anotó nadie. Sorteo cancelado por falta de calle.")
        except Exception:
            pass
        remove_giveaway(g.message_id)
        return

    random.shuffle(pool)

    # Querés: 3 “falsos” + ganador. Si hay pocos, se adapta.
    lines = [
        "Este no gano nada.",
        "Este tampoco gano nada.",
        "Este es terrible topo.",
        "Mira el orto que tiene este loco gano el sorteo."
    ]

    # Picks únicos hasta 4 o menos según gente
    picks_count = min(len(pool), 4)
    picks = pool[:picks_count]

    # Si hay 1 solo, directamente ganador (última línea)
    if picks_count == 1:
        uid = picks[0]
        try:
            await ch.send(f"<@{uid}> — {lines[-1]}")
        except Exception:
            pass
        remove_giveaway(g.message_id)
        return

    # Si hay 2: 1 falso + ganador
    if picks_count == 2:
        uid1, uid2 = picks
        try:
            await ch.send(f"<@{uid1}> — {lines[0]}")
        except Exception:
            pass
        await asyncio.sleep(60)
        try:
            await ch.send(f"<@{uid2}> — {lines[-1]}")
        except Exception:
            pass
        remove_giveaway(g.message_id)
        return

    # Si hay 3: 2 falsos + ganador
    if picks_count == 3:
        uid1, uid2, uid3 = picks
        try:
            await ch.send(f"<@{uid1}> — {lines[0]}")
        except Exception:
            pass
        await asyncio.sleep(60)
        try:
            await ch.send(f"<@{uid2}> — {lines[1]}")
        except Exception:
            pass
        await asyncio.sleep(60)
        try:
            await ch.send(f"<@{uid3}> — {lines[-1]}")
        except Exception:
            pass
        remove_giveaway(g.message_id)
        return

    # 4 o más: 3 falsos + ganador (tu guion completo)
    uid1, uid2, uid3, uid4 = picks
    try:
        await ch.send(f"<@{uid1}> — {lines[0]}")
    except Exception:
        pass
    await asyncio.sleep(60)
    try:
        await ch.send(f"<@{uid2}> — {lines[1]}")
    except Exception:
        pass
    await asyncio.sleep(60)
    try:
        await ch.send(f"<@{uid3}> — {lines[2]}")
    except Exception:
        pass
    await asyncio.sleep(60)
    try:
        await ch.send(f"<@{uid4}> — {lines[3]}")
    except Exception:
        pass

    remove_giveaway(g.message_id)


# ---------- UTILIDADES ----------
async def send_log(guild: discord.Guild, message: str):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(message)
    except Exception:
        pass

async def create_transcript(channel: discord.TextChannel):
    messages = []
    async for msg in channel.history(limit=200, oldest_first=True):
        content = msg.content if msg.content else ""
        messages.append(f"[{msg.author}] {content}")
    return "\n".join(messages)

def staff_only():
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            return False
        staff_role = ctx.guild.get_role(STAFF_ROLE_ID)
        if staff_role is None:
            await ctx.reply("❌ STAFF_ROLE_ID mal configurado (no encuentro el rol).")
            return False
        if staff_role not in ctx.author.roles:
            await ctx.reply("❌ Solo los miembros con el rol **Staff** pueden usar este comando.")
            return False
        return True
    return commands.check(predicate)

def _get_timers_role(guild: discord.Guild) -> Optional[discord.Role]:
    if TIMERS_ROLE_ID and TIMERS_ROLE_ID != 0:
        return guild.get_role(TIMERS_ROLE_ID)
    return discord.utils.get(guild.roles, name=TIMERS_ROLE_NAME_FALLBACK)

def parse_duration_hhmm(s: str) -> Optional[tuple[int, int]]:
    if ":" not in s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    if h < 0 or m < 0 or m > 59:
        return None
    return h, m

def fmt_utc(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.date() != now.date():
        return dt.strftime("%H:%M UTC (%d/%m)")
    return dt.strftime("%H:%M UTC")

def time_left_str(end_at: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = end_at - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0m"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def is_timers_member(member: discord.Member) -> bool:
    role = _get_timers_role(member.guild)
    if role is None:
        return False
    return role in member.roles

# ✅ helper anti-40060
async def respond_ephemeral(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True)
        return await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        try:
            return await interaction.followup.send(content, ephemeral=True)
        except Exception:
            return

def safe_channel_name(user_name: str, user_id: int) -> str:
    base = f"postulacion-{user_name}-{user_id}".lower()
    base = re.sub(r"[^a-z0-9\-]", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base[:90]

def _format_timer_line(t: TimerItem) -> str:
    # Mostrar tier lindo si es special
    tier_txt = TIER_LABELS_SPECIAL.get(t.tier, f"T{t.tier}")
    return f"• 🧱 **{t.material.title()}** | ⭐ **{tier_txt}** | 🗺️ **{t.map_name}** → 🕒 **{fmt_utc(t.end_at)}** (en {time_left_str(t.end_at)})"

async def ensure_timers_board_message(guild: discord.Guild):
    global timers_board_message_id

    ch = guild.get_channel(TIMERS_BOARD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await guild.fetch_channel(TIMERS_BOARD_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    # 1) Buscar en pins un mensaje del bot con el título esperado
    try:
        pins = await ch.pins()
        for m in pins:
            if m.author.id == bot.user.id and m.embeds:
                emb = m.embeds[0]
                if emb.title == TIMERS_BOARD_TITLE:
                    timers_board_message_id = m.id
                    return
    except Exception:
        pass

    # 2) Si no existe, crear uno nuevo y pinearlo
    embed = discord.Embed(title=TIMERS_BOARD_TITLE, description="(cargando...)", color=discord.Color.blurple())
    try:
        m = await ch.send(embed=embed)
        try:
            await m.pin(reason="Timers board message")
        except Exception:
            pass
        timers_board_message_id = m.id
    except Exception:
        return

async def update_timers_board(guild: discord.Guild):
    global timers_board_message_id

    ch = guild.get_channel(TIMERS_BOARD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await guild.fetch_channel(TIMERS_BOARD_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    if timers_board_message_id is None:
        await ensure_timers_board_message(guild)

    if timers_board_message_id is None:
        return

    # Ordenar por tiempo
    sorted_timers = sorted(timers, key=lambda x: x.end_at)

    if not sorted_timers:
        desc = "📭 No hay timers activos."
    else:
        lines = [_format_timer_line(t) for t in sorted_timers]
        desc = "\n".join(lines)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n…"

    embed = discord.Embed(
        title=TIMERS_BOARD_TITLE,
        description=desc,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    try:
        msg = await ch.fetch_message(timers_board_message_id)
        await msg.edit(embed=embed)
    except Exception:
        # si se borró el mensaje, recrear
        timers_board_message_id = None
        await ensure_timers_board_message(guild)
        if timers_board_message_id:
            try:
                msg = await ch.fetch_message(timers_board_message_id)
                await msg.edit(embed=embed)
            except Exception:
                pass

RECRUIT_TOPIC_PREFIX = "RECRUIT"
def make_recruit_topic(user_id: int) -> str:
    return f"{RECRUIT_TOPIC_PREFIX}|uid={user_id}"
def parse_recruit_topic(topic: Optional[str]) -> dict:
    if not topic or not topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
        return {}
    data = {}
    for p in topic.split("|")[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            data[k.strip()] = v.strip()
    return data

async def download_as_file(url: str, filename: str) -> Optional[discord.File]:
    try:
        async with bot.http_session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        return discord.File(fp=io.BytesIO(data), filename=filename)
    except Exception:
        return None
NO_MENTIONS = discord.AllowedMentions.none()
# ================== FOCO DONOR HELPERS (NUEVO) ==================
def _sanitize_topic_value(s: str, max_len: int = 200) -> str:
    s = (s or "").strip()
    s = s.replace("|", "/").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s{2,}", " ", s)
    if len(s) > max_len:
        s = s[:max_len - 1] + "…"
    return s

def make_foco_topic(user_id: int, foco: str, item_spec: str) -> str:
    foco_v = _sanitize_topic_value(foco, 60)
    item_v = _sanitize_topic_value(item_spec, 300)
    return f"{FOCO_TOPIC_PREFIX}|uid={user_id}|foco={foco_v}|item={item_v}"

def parse_foco_topic(topic: Optional[str]) -> dict:
    data = {}
    if not topic:
        return data
    if not topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
        return data
    parts = topic.split("|")
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            data[k.strip()] = v.strip()
    return data

def safe_foco_channel_name(display_name: str) -> str:
    # pedido: "Apodo - Foco Donor" (en canal, sin espacios -> guiones)
    base = f"{display_name}-foco-donor".lower()
    base = re.sub(r"[^a-z0-9\-]", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base[:90]

def find_open_foco_channel(guild: discord.Guild, user_id: int) -> Optional[discord.TextChannel]:
    # Busca por topic (sirve incluso si el bot reinicia)
    uid_str = str(user_id)
    for ch in guild.text_channels:
        if not ch.topic:
            continue
        if ch.topic.startswith(f"{FOCO_TOPIC_PREFIX}|uid={uid_str}|"):
            return ch
        # formato normal: FOCO_DONOR|uid=...|...
        if ch.topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            info = parse_foco_topic(ch.topic)
            if info.get("uid") == uid_str:
                return ch
    return None

async def send_foco_log(guild: discord.Guild, message: str):
    target_id = FOCO_LOG_CHANNEL_ID if FOCO_LOG_CHANNEL_ID and FOCO_LOG_CHANNEL_ID != 0 else LOG_CHANNEL_ID
    channel = guild.get_channel(target_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(target_id)
        except Exception:
            return
    try:
        await channel.send(message)
    except Exception:
        pass

def get_foco_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    foco_cat_id = FOCO_CATEGORY_ID if FOCO_CATEGORY_ID and FOCO_CATEGORY_ID != 0 else CATEGORY_ID
    return discord.utils.get(guild.categories, id=foco_cat_id)


# ---------- TIMERS HOUSEKEEPING ----------
@tasks.loop(seconds=30)
async def timers_housekeeping():
    now = datetime.now(timezone.utc)

    expired = [t for t in timers if now >= t.end_at]
    for t in expired:
        try:
            timers.remove(t)
            delete_timer(t)
        except ValueError:
            pass

    if not TIMER_ALERT_CHANNEL_ID or TIMER_ALERT_CHANNEL_ID == 0:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(TIMER_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(TIMER_ALERT_CHANNEL_ID)
        except Exception:
            return

    if expired:
        await update_timers_board(guild)

    for t in timers:
        if t.warned_30:
            continue
        seconds_left = (t.end_at - now).total_seconds()
        if 0 < seconds_left <= (TIMER_ALERT_MINUTES_BEFORE * 60):
            t.warned_30 = True
            save_timer(t)
            
            tier_txt = TIER_LABELS_SPECIAL.get(t.tier, f"T{t.tier}")

            alert_role_mention = f"<@&{TIMERS_ALERT_ROLE_ID}>"
            msg = (
                f"{alert_role_mention}\n"
                f"**Las inteadas de daitza no se pagan solas, armen la party**\n"
                f"⏰ **Faltan {TIMER_ALERT_MINUTES_BEFORE} min**\n"
                f"🧱 **{t.material.title()}** | ⭐ **{tier_txt}**\n"
                f"🗺️ **{t.map_name}**\n"
                f"🕒 Sale a **{fmt_utc(t.end_at)}**"
            )

            try:
                await channel.send(msg)
            except Exception:
                pass

@timers_housekeeping.before_loop
async def before_timers_housekeeping():
    await bot.wait_until_ready()

# ================== TIMER POSTS (/timeradd) ==================
TIMER_POST_CHANNEL_ID = 1462184630835740732
TIMER_POST_DELETE_AFTER_MINUTES = 15
ARGENTINA_TZ = timezone(timedelta(hours=-3))   # UTC-3 fijo (Argentina no usa horario de verano)

# Lee el timestamp de "Falta" del propio mensaje (formato nuevo con ** y formato anterior sin **)
_TIMER_POST_TS_RE = re.compile(r"Falta:\**\s*<t:(\d+):R>")
_CLOCK_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_clock_hhmm(s: str) -> Optional[tuple[int, int]]:
    """'16:10' / '6:05' -> (16, 10) / (6, 5). Hora de reloj válida (0-23 : 0-59) o None."""
    match = _CLOCK_HHMM_RE.match(s or "")
    if not match:
        return None
    h, m = int(match.group(1)), int(match.group(2))
    if h > 23 or m > 59:
        return None
    return h, m


def next_utc_occurrence(h: int, m: int, now: Optional[datetime] = None) -> datetime:
    """Próxima vez (UTC) que el reloj marque h:m. Si ya pasó hoy, es mañana (cruza medianoche sin problemas)."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def build_timer_post(material: str, tier: Optional[str], mapa: str, target_utc: datetime, user_mention: str) -> str:
    """Arma el texto del anuncio. Si no hay tier (Dragon) la línea se omite por completo."""
    icon = MATERIAL_ICONS.get(material, DEFAULT_MATERIAL_ICON)
    unix = int(target_utc.timestamp())
    lines = [f"{icon} **Material:** {material.title()}"]
    if tier:
        lines.append(f"🔹 **Tier:** {TIER_LABELS_SPECIAL.get(tier, tier)}")
    lines += [
        f"🗺️ **Mapa:** {mapa}",
        f"🕒 **Tiempo:** {target_utc.strftime('%H:%M')} UTC",
        f"🌎 **Horario:** {target_utc.astimezone(ARGENTINA_TZ).strftime('%H:%M')}",
        f"⏳ **Falta:** <t:{unix}:R>",
        f"👤 **Timeado por:** {user_mention}",
    ]
    return "\n".join(lines)


class TimerDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🗑️ Eliminar", style=discord.ButtonStyle.danger, custom_id="timer_post_delete")
    async def delete_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        if not (is_timers_member(interaction.user) or is_staff_member(interaction.user)):
            return await respond_ephemeral(interaction, "❌ No tenés permiso para eliminar este timer.")

        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except Exception:
            return await respond_ephemeral(interaction, "❌ No pude eliminar el mensaje.")

        await respond_ephemeral(interaction, "🗑️ Timer eliminado.")


@tasks.loop(seconds=60)
async def timer_posts_cleanup():
    """Borra los posts de /timeradd 15 min después de la hora indicada.
    Lee la hora del propio mensaje, así funciona aunque el bot se reinicie."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(TIMER_POST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(TIMER_POST_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(channel, discord.TextChannel):
        return

    limit_unix = int(datetime.now(timezone.utc).timestamp()) - TIMER_POST_DELETE_AFTER_MINUTES * 60

    try:
        async for msg in channel.history(limit=100):
            if bot.user is None or msg.author.id != bot.user.id:
                continue
            match = _TIMER_POST_TS_RE.search(msg.content or "")
            if not match:
                continue
            if int(match.group(1)) <= limit_unix:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

@timer_posts_cleanup.before_loop
async def before_timer_posts_cleanup():
    await bot.wait_until_ready()


# ================== SLASH COMMANDS TIMERS ==================
@bot.tree.command(name="timeradd", description="Agregar timer (solo rol Timers)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    material="Material",
    mapa="Nombre del mapa",
    tiempo="Horario UTC en que sale (HH:MM) ej 16:10",
    tier="Tier (obligatorio salvo para Dragon)",
)
@app_commands.choices(
    material=[
        app_commands.Choice(name="Fibra", value="fibra"),
        app_commands.Choice(name="Cuero", value="cuero"),
        app_commands.Choice(name="Mineral", value="mineral"),
        app_commands.Choice(name="Madera", value="madera"),
        app_commands.Choice(name="Vortex", value="vortex"),
        app_commands.Choice(name="Core", value="core"),
        app_commands.Choice(name="Dragon", value="dragon"),
    ],
    tier=[
        app_commands.Choice(name="4.4", value="4.4"),
        app_commands.Choice(name="5.4", value="5.4"),
        app_commands.Choice(name="6.4", value="6.4"),
        app_commands.Choice(name="7.4", value="7.4"),
        app_commands.Choice(name="8.4", value="8.4"),
        app_commands.Choice(name="Common (Verde)", value="common"),
        app_commands.Choice(name="Rare (Azul)", value="rare"),
        app_commands.Choice(name="Epic (Violeta)", value="epic"),
        app_commands.Choice(name="Legendary (Amarillo)", value="legendary"),
    ],
)
async def timeradd_slash(
    interaction: discord.Interaction,
    material: app_commands.Choice[str],
    mapa: str,
    tiempo: str,
    tier: Optional[app_commands.Choice[str]] = None,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not is_timers_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el rol **timers** puede usar este comando.")

    mapa = mapa.strip()
    if not mapa:
        return await respond_ephemeral(interaction, "❌ El nombre del mapa no puede estar vacío.")

    mat = material.value
    tr: Optional[str] = tier.value if tier is not None else None

    # Validación material/tier (Dragon no lleva tier; si lo mandan se ignora)
    if mat in MATERIALS_NO_TIER:
        tr = None
    elif tr is None:
        return await respond_ephemeral(
            interaction,
            "❌ Falta el **tier**. Solo **Dragon** se puede publicar sin tier."
        )
    elif mat in MATERIALS_SPECIAL:
        if tr not in TIERS_SPECIAL:
            return await respond_ephemeral(
                interaction,
                "❌ Para **Vortex/Core** el tier tiene que ser: Common/Rare/Epic/Legendary."
            )
    else:
        if tr not in TIERS_NORMAL:
            return await respond_ephemeral(
                interaction,
                "❌ Para **Fibra/Cuero/Mineral/Madera** el tier tiene que ser: 4.4/5.4/6.4/7.4/8.4."
            )

    clock = parse_clock_hhmm(tiempo)
    if clock is None:
        return await respond_ephemeral(
            interaction,
            "❌ Tiempo inválido. Usá el horario UTC en formato `HH:MM` (00:00 a 23:59), ej: `16:10`."
        )

    target_utc = next_utc_occurrence(*clock)

    await interaction.response.defer(ephemeral=True)

    channel = interaction.guild.get_channel(TIMER_POST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(TIMER_POST_CHANNEL_ID)
        except Exception:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de timers.", ephemeral=True)

    content = build_timer_post(mat, tr, mapa, target_utc, interaction.user.mention)

    try:
        post = await channel.send(
            content,
            view=TimerDeleteView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permisos para escribir en el canal de timers.", ephemeral=True)
    except Exception:
        return await interaction.followup.send("❌ No pude publicar el timer.", ephemeral=True)

    await interaction.followup.send(f"✅ Timer publicado: {post.jump_url}", ephemeral=True)

@bot.tree.command(name="sorteo", description="Crear sorteo (solo Staff)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    premio="Qué se sortea (texto libre)",
    tiempo="Duración en H:M (ej: 0:30, 2:00, 6:10)"
)
async def sorteo_slash(interaction: discord.Interaction, premio: str, tiempo: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not is_staff_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo **Staff** puede crear sorteos.")

    if not premio or not premio.strip():
        return await respond_ephemeral(interaction, "❌ El premio no puede estar vacío.")

    dur = parse_duration_hhmm(tiempo.strip())
    if dur is None:
        return await respond_ephemeral(
            interaction,
            "❌ Tiempo inválido.\nUsá `H:M` (ej: `0:30`, `2:00`, `6:10`)."
        )

    h, m = dur
    if h == 0 and m == 0:
        return await respond_ephemeral(interaction, "❌ La duración tiene que ser mayor a `00:00`.")
    end_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=m)

    await interaction.response.defer(ephemeral=True)

    # Creamos mensaje panel
    temp_g = GiveawayItem(
        prize=premio.strip(),
        end_at=end_at,
        channel_id=interaction.channel.id,
        message_id=0,
        creator_id=interaction.user.id
    )

    view = GiveawayJoinView()
    embed = build_giveaway_embed(temp_g)

    try:
        msg = await interaction.channel.send(embed=embed, view=view)
    except Exception:
        return await interaction.followup.send("❌ No pude enviar el panel del sorteo.", ephemeral=True)

    # Guardar sorteo real
    temp_g.message_id = msg.id
    giveaways[msg.id] = temp_g
    save_giveaway(temp_g, interaction.guild.id)

    # Arrancar flujo en background (se vuelve a iniciar desde SQLite tras un reinicio)
    start_giveaway_task(interaction.guild.id, temp_g)

    await interaction.followup.send(
        f"✅ Sorteo creado. Premio: `{temp_g.prize}` | Termina: <t:{int(end_at.timestamp())}:R>",
        ephemeral=True
    )

def staff_only_slash():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return is_staff_member(interaction.user)
    return app_commands.check(predicate)


@bot.tree.command(
    name="list_role",
    description="(Staff) Lista los miembros (apodo del server) que tienen un rol",
    guild=discord.Object(id=GUILD_ID)
)
@staff_only_slash()
@app_commands.describe(rol="Rol del que querés listar miembros")
async def list_role_slash(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    members = [m for m in guild.members if rol in m.roles]

    if not members:
        return await interaction.followup.send(f"📭 No hay nadie con el rol **{rol.name}**.", ephemeral=True)

    lines = [m.nick or m.display_name or m.name for m in members]
    lines.sort(key=lambda s: s.lower())

    text = "\n".join(lines)
    header = f"Rol: {rol.name} ({len(lines)} miembros)\n" + ("-" * 32) + "\n"
    full = header + text

    if len(full) > 1800:
        file = discord.File(fp=io.BytesIO(full.encode("utf-8")), filename=f"list_role_{rol.name}.txt")
        return await interaction.followup.send(
            content=f"📄 Lista para **{rol.name}** ({len(lines)}):",
            file=file,
            ephemeral=True
        )

    await interaction.followup.send(f"```{full}```", ephemeral=True)

@list_role_slash.error
async def list_role_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return await respond_ephemeral(interaction, "❌ Solo **Staff** puede usar `/list_role`.")
    return await respond_ephemeral(interaction, "❌ Error ejecutando el comando.")

@bot.tree.command(
    name="delrole",
    description="(Staff) Elimina un rol (sin tagear a nadie)",
    guild=discord.Object(id=GUILD_ID)
)
@staff_only_slash()
@app_commands.describe(rol="Rol que querés borrar")
async def delrole_slash(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    # Bloqueos de seguridad
    if rol.id == STAFF_ROLE_ID or rol.managed:
        return await respond_ephemeral(interaction, "❌ No se puede eliminar ese rol (Staff o rol administrado).")

    # Evitar borrar @everyone
    if interaction.guild.default_role and rol.id == interaction.guild.default_role.id:
        return await respond_ephemeral(interaction, "❌ No se puede eliminar @everyone.")

    # Intentar borrar
    try:
        role_name = rol.name
        await rol.delete(reason=f"Eliminado por {interaction.user} (Staff)")

        # Ephemeral + sin mentions
        if interaction.response.is_done():
            await interaction.followup.send(
                f"🗑️ Rol **{role_name}** eliminado correctamente.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                f"🗑️ Rol **{role_name}** eliminado correctamente.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )

    except discord.Forbidden:
        return await respond_ephemeral(interaction, "❌ No pude eliminar el rol. Me falta **Manage Roles** o jerarquía.")
    except Exception:
        return await respond_ephemeral(interaction, "❌ Error inesperado eliminando el rol.")

# ---------- COMANDOS STAFF ----------
@bot.command(name="addroll-list")
@staff_only()
@commands.guild_only()
async def addroll_list(ctx: commands.Context, *, args: str = None):
    if not args:
        return await ctx.reply("Uso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...`")

    mentioned_members = ctx.message.mentions
    if not mentioned_members:
        return await ctx.reply("Tenés que mencionar al menos 1 usuario.\nUso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...`")

    role_name = args
    for m in mentioned_members:
        role_name = role_name.replace(m.mention, "").strip()
    role_name = " ".join(role_name.split())

    if not role_name:
        return await ctx.reply("Falta el nombre del rol.\nUso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...`")

    role = discord.utils.get(ctx.guild.roles, name=role_name)

    if role is None:
        try:
            role = await ctx.guild.create_role(name=role_name, reason=f"Rol creado automáticamente por {ctx.author} (Staff)")
        except discord.Forbidden:
            return await ctx.reply("❌ No pude crear el rol. Me falta permiso **Manage Roles** o jerarquía.")
        except Exception:
            return await ctx.reply("❌ Error inesperado creando el rol.")

    ok, fail = 0, 0
    for member in mentioned_members:
        try:
            await member.add_roles(role, reason=f"Asignado por {ctx.author} (Staff)")
            ok += 1
        except Exception:
            fail += 1

    await ctx.reply(f"✅ Rol **{role.name}** asignado. OK: {ok} | Fallos: {fail}")

# ---------- RECRUIT VIEW ----------
class RecruitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False

        recruiter_role = interaction.guild.get_role(RECRUITER_ROLE_ID)
        if recruiter_role is None or recruiter_role not in interaction.user.roles:
            await respond_ephemeral(interaction, "❌ Solo reclutadores pueden usar estos botones.")
            return False

        # Validar que el canal sea un ticket válido
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await respond_ephemeral(interaction, "❌ Esto solo funciona en un canal de ticket.")
            return False

        info = parse_recruit_topic(ch.topic)
        if not info.get("uid"):
            await respond_ephemeral(interaction, "❌ Este canal no tiene UID en el topic (ticket viejo o mal creado).")
            return False

        return True

    async def _get_applicant(self, interaction: discord.Interaction) -> Optional[discord.Member]:
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return None

        info = parse_recruit_topic(ch.topic)
        uid = info.get("uid")
        if not uid:
            return None

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None
        return member

    async def accept_player(self, interaction: discord.Interaction, role_id: int, role_name: str):
        member = await self._get_applicant(interaction)
        if member is None:
            return await respond_ephemeral(interaction, "❌ No pude encontrar al postulante (se fue del server o UID inválido).")

        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
        public_role = interaction.guild.get_role(PUBLIC_ROLE_ID)

        if role is None or member_role is None:
            return await respond_ephemeral(interaction, "❌ Error: roles mal configurados (IDs incorrectos).")

        try:
            await member.add_roles(member_role, role, reason=f"Aceptado como {role_name}")
            if public_role and public_role in member.roles:
                await member.remove_roles(public_role, reason="Aceptado: se quita rol Public")
        except discord.Forbidden:
            return await respond_ephemeral(interaction, "❌ No tengo permisos para asignar/quitar roles (jerarquía/permisos).")
        except Exception:
            return await respond_ephemeral(interaction, "❌ Error inesperado asignando roles.")

        try:
            await interaction.channel.send(f"✅ {member.mention} aceptado como **{role_name}** en **Smogg** ⚔️")
        except Exception:
            pass

        # LOG
        try:
            log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID) or await interaction.guild.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            log_channel = None

        if log_channel:
            embed = discord.Embed(
                title="✅ POSTULACIÓN ACEPTADA",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="👤 Postulante", value=member.mention, inline=True)
            embed.add_field(name="🧑‍💼 Reclutador", value=interaction.user.mention, inline=True)
            embed.add_field(name="🎭 Rol asignado", value=role_name, inline=False)
            embed.add_field(name="📍 Ticket", value=interaction.channel.name, inline=False)

            # Imagen: la guardás por uid
            img_data = ticket_images.get(member.id)
            try:
                if img_data and img_data.get("url"):
                    filename = img_data.get("filename", "image.png")
                    file = await download_as_file(img_data["url"], filename)
                    if file:
                        embed.set_image(url=f"attachment://{filename}")
                        await log_channel.send(embed=embed, file=file)
                    else:
                        await log_channel.send(embed=embed)
                        await log_channel.send(img_data["url"])
                else:
                    await log_channel.send(embed=embed)
            except Exception:
                pass

        # Cleanup (aunque reinicies, esto solo limpia memoria)
        active_applications.pop(member.id, None)
        delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Postulación aceptada por {interaction.user}")
        except Exception:
            pass

    async def reject_player(self, interaction: discord.Interaction):
        member = await self._get_applicant(interaction)
        if member:
            try:
                await member.send("❌ Tu postulación en **Smogg** fue rechazada. Podés volver a aplicar más adelante.")
            except Exception:
                pass

        try:
            await send_log(interaction.guild, f"❌ **RECHAZADO** {member} | Ticket: #{interaction.channel.name}")
        except Exception:
            pass

        if member:
            active_applications.pop(member.id, None)
            delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Postulación rechazada por {interaction.user}")
        except Exception:
            pass

    async def close_ticket(self, interaction: discord.Interaction):
        member = await self._get_applicant(interaction)
        transcript = ""
        try:
            transcript = await create_transcript(interaction.channel)
        except Exception:
            pass

        try:
            await send_log(
                interaction.guild,
                f"🔒 **POSTULACIÓN CERRADA** {member}\n```\n{transcript[:1800]}\n```"
            )
        except Exception:
            pass

        if member:
            active_applications.pop(member.id, None)
            delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Ticket cerrado por {interaction.user}")
        except Exception:
            pass

    # BOTONES (con custom_id fijo => persistente)
    @discord.ui.button(label="✔ Miembro", style=discord.ButtonStyle.success, custom_id="recruit_accept_miembro")
    async def miembro(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, MIEMBRO_ROLE_ID, "Miembro")

    @discord.ui.button(label="🛡 Tank", style=discord.ButtonStyle.primary, custom_id="recruit_accept_tank")
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, TANK_ROLE_ID, "Tank")

    @discord.ui.button(label="✨ Healer", style=discord.ButtonStyle.primary, custom_id="recruit_accept_healer")
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, HEALER_ROLE_ID, "Healer")

    @discord.ui.button(label="🧙 Support", style=discord.ButtonStyle.primary, custom_id="recruit_accept_supp")
    async def supp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, SUPP_ROLE_ID, "Support")

    @discord.ui.button(label="⚔ DPS", style=discord.ButtonStyle.primary, custom_id="recruit_accept_dps")
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, DPS_ROLE_ID, "DPS")

    @discord.ui.button(label="🐎 Battle Mount", style=discord.ButtonStyle.primary, custom_id="recruit_accept_bm")
    async def battle_mount(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, BATTLE_MOUNT_ROLE_ID, "Battle Mount")

    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.secondary, custom_id="recruit_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.reject_player(interaction)

    @discord.ui.button(label="🔒 Cerrar Postulación", style=discord.ButtonStyle.danger, custom_id="recruit_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.close_ticket(interaction)


# ================== FOCO DONOR SYSTEM ==================
class FocoDonorModal(discord.ui.Modal, title="Foco Donor"):
    foco = discord.ui.TextInput(
        label="Cuanto foco tenes actualmente",
        placeholder="Ej: 30000",
        required=True,
        max_length=60
    )
    item_spec = discord.ui.TextInput(
        label="Que item podes craftear y spec en ese item",
        placeholder="Ej: Hellion Jacket - Spec 100",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=300
    )

    def __init__(self, opener: discord.Member):
        super().__init__(timeout=None)
        self.opener = opener

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        guild = interaction.guild
        user_id = interaction.user.id

        # Anti-duplicado (incluye reinicios)
        existing = find_open_foco_channel(guild, user_id)
        if existing is not None:
            return await respond_ephemeral(interaction, f"❌ Ya tenés un ticket de **Foco Donor** abierto: {existing.mention}")

        category = get_foco_category(guild)
        if category is None:
            return await respond_ephemeral(
                interaction,
                "❌ No encontré la categoría de tickets de Foco Donor.\n"
                "👉 Seteá `FOCO_CATEGORY_ID` (o revisá el `CATEGORY_ID`)."
            )

        # Crear canal
        display_name = interaction.user.display_name
        channel_name = safe_foco_channel_name(display_name)

        bot_member = guild.get_member(bot.user.id) if bot.user else None
        staff_role = guild.get_role(STAFF_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        topic = make_foco_topic(user_id, str(self.foco.value), str(self.item_spec.value))

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic
            )
        except discord.Forbidden:
            return await respond_ephemeral(interaction, "❌ No tengo permisos para crear canales / setear permisos.")
        except Exception:
            return await respond_ephemeral(interaction, "❌ Error creando el canal del ticket.")

        active_foco_tickets[user_id] = channel.id

        # Mensaje inicial en el canal
        embed = discord.Embed(
            title="💠 Foco Donor",
            description=(
                f"**Foco declarado:** `{self.foco.value}`\n"
                f"**Item + spec:** `{self.item_spec.value}`\n\n"
                "Un Staff va a revisar tu donación."
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Smogg Foco Donor System")

        staff_mention = staff_role.mention if staff_role else f"<@&{STAFF_ROLE_ID}>"

        await channel.send(
            content=f"{interaction.user.mention} {staff_mention}",
            embed=embed,
            view=FocoTicketActionView()
        )

        await respond_ephemeral(interaction, f"✅ Ticket creado: {channel.mention}")

class FocoTicketActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role is None or staff_role not in interaction.user.roles:
            await respond_ephemeral(interaction, "❌ Solo **Staff** puede aceptar/rechazar donaciones.")
            return False
        return True

    @discord.ui.button(
        label="✅ Cerrar Exitoso",
        style=discord.ButtonStyle.success,
        custom_id="foco_ticket_success"
    )
    async def foco_success(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return await respond_ephemeral(interaction, "❌ Error: canal inválido.")

        info = parse_foco_topic(ch.topic)
        uid = info.get("uid")
        foco = info.get("foco", "N/D")
        item = info.get("item", "N/D")

        if not uid:
            return await respond_ephemeral(interaction, "❌ No pude leer los datos del ticket (topic vacío).")

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None

        if member:
            # Log de foco (tag a la persona + cantidad)
            await send_foco_log(
                interaction.guild,
                f"✅ **FOCO DONADO (OK)** {member.mention} → **{foco}** foco | Item: **{item}**"
            )

            try:
                await ch.send(f"✅ Donación aprobada. Gracias {member.mention} 💚")
            except Exception:
                pass

            active_foco_tickets.pop(member.id, None)

        try:
            await ch.delete(reason=f"Foco donor ticket cerrado exitoso por {interaction.user}")
        except Exception:
            pass

        await respond_ephemeral(interaction, "✅ Ticket cerrado como exitoso.")

    @discord.ui.button(
        label="❌ Rechazar Donación",
        style=discord.ButtonStyle.danger,
        custom_id="foco_ticket_reject"
    )
    async def foco_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return await respond_ephemeral(interaction, "❌ Error: canal inválido.")

        info = parse_foco_topic(ch.topic)
        uid = info.get("uid")
        item = info.get("item", "ese ítem")

        if not uid:
            return await respond_ephemeral(interaction, "❌ No pude leer los datos del ticket (topic vacío).")

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None

        if member:
            try:
                await member.send(
                    "❌ Donación rechazada.\n"
                    f"En este momento no necesitamos craftear **{item}**."
                )
            except Exception:
                pass

            try:
                await ch.send(f"❌ Donación rechazada. Le avisé por DM a {member.mention}.")
            except Exception:
                pass

            active_foco_tickets.pop(member.id, None)

        try:
            await ch.delete(reason=f"Foco donor ticket rechazado por {interaction.user}")
        except Exception:
            pass

        await respond_ephemeral(interaction, "✅ Ticket rechazado y cerrado.")

class FocoPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💠 Abrir Foco Donor",
        style=discord.ButtonStyle.success,
        custom_id="open_foco_donor"
    )
    async def open_foco(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        user_id = interaction.user.id
        now = time.time()

        # Anti-spam cooldown
        if user_id in cooldowns and (now - cooldowns[user_id] < COOLDOWN_SECONDS):
            return await respond_ephemeral(interaction, "⏳ Esperá un momento antes de volver a intentar.")
        cooldowns[user_id] = now

        # Anti-duplicado (incluye reinicios)
        existing = find_open_foco_channel(interaction.guild, user_id)
        if existing is not None:
            return await respond_ephemeral(interaction, f"❌ Ya tenés un ticket de **Foco Donor** abierto: {existing.mention}")

        # Abrir modal con 2 preguntas antes de crear el canal
        try:
            await interaction.response.send_modal(FocoDonorModal(interaction.user))
        except Exception:
            return await respond_ephemeral(interaction, "❌ No pude abrir el formulario (modal).")

class GiveawayJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎟️ Anotarme", style=discord.ButtonStyle.success, custom_id="giveaway_join_btn")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        msg = interaction.message
        if msg is None:
            return await respond_ephemeral(interaction, "❌ No pude leer el mensaje del sorteo.")

        g = giveaways.get(msg.id)
        if g is None:
            return await respond_ephemeral(interaction, "❌ Este sorteo ya no existe (o el bot reinició).")

        if datetime.now(timezone.utc) >= g.end_at:
            return await respond_ephemeral(interaction, "⏰ Llegaste tarde, ya terminó.")

        async with g.lock:
            if interaction.user.id in g.entrants:
                return await respond_ephemeral(interaction, "✅ Ya estabas anotado. Te querés anotar dos veces, picarón.")
            g.entrants.add(interaction.user.id)
            save_giveaway_entrant(g.message_id, interaction.user.id)

        # No editamos el embed en cada click (evita rate-limit cuando 200 monos spamean el botón)
        return await respond_ephemeral(interaction, "✅ Listo, quedaste anotado.")


# Limpieza si borran el canal manualmente (por si acaso)
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    try:
        if not isinstance(channel, discord.TextChannel) or not channel.topic:
            return

        # FOCO
        if channel.topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            info = parse_foco_topic(channel.topic)
            uid = info.get("uid")
            if uid:
                active_foco_tickets.pop(int(uid), None)

        # RECRUIT
        if channel.topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
            info = parse_recruit_topic(channel.topic)
            uid = info.get("uid")
            if uid:
                active_applications.pop(int(uid), None)
                delete_ticket_image(int(uid))

    except Exception:
        pass

# ---------- PANEL VIEW (RECLUTAMIENTO) ----------
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚡ Abrir Postulación", style=discord.ButtonStyle.success, custom_id="open_postulacion")
    async def open_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return

        user_id = interaction.user.id
        now = time.time()

        if user_id in active_applications:
            return await respond_ephemeral(interaction, "❌ Ya tenés una postulación activa.")

        if user_id in cooldowns and (now - cooldowns[user_id] < COOLDOWN_SECONDS):
            return await respond_ephemeral(interaction, "⏳ Esperá un momento antes de volver a intentar.")

        cooldowns[user_id] = now
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        if category is None:
            return await interaction.followup.send("❌ Error: no encontré la categoría configurada (CATEGORY_ID mal).", ephemeral=True)

        if recruiter_role is None:
            return await interaction.followup.send("❌ Error: no encontré el rol de reclutador (RECRUITER_ROLE_ID mal).", ephemeral=True)

        channel_name = safe_channel_name(interaction.user.name, interaction.user.id)
        bot_member = guild.get_member(bot.user.id) if bot.user else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            recruiter_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        topic = make_recruit_topic(user_id)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=topic
        )

        active_applications[user_id] = channel.id

        embed = discord.Embed(
            title="⚔️ Reclutamiento Smogg",
            description=(
                "**Enviá lo siguiente:**\n\n"
                "📸 Screenshot perfil Albion\n"
                "⚔ Rol ZvZ\n"
                "🕒 Horarios\n"
                "📜 Gremios anteriores en los que estuviste\n\n"
                "Un reclutador revisará tu postulación."
            ),
            color=discord.Color.gold()
        )

        recruiter_mention = recruiter_role.mention if recruiter_role else "@Reclutadores"

        await channel.send(
            content=f"{interaction.user.mention} {recruiter_mention}",
            embed=embed,
            view=RecruitView()
        )

        await send_log(guild, f"📥 **NUEVA POSTULACIÓN** {interaction.user} → {channel.mention}")
        await interaction.followup.send(f"✅ Postulación creada: {channel.mention}", ephemeral=True)

# ---------- COMANDO PANEL (RECLUTAMIENTO) ----------
@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="⚔️ Smogg Reclutamiento",
        description="Presioná el botón para abrir tu **postulación oficial**.",
        color=discord.Color.orange()
    )
    embed.set_footer(text="Albion Online Recruitment System")
    await ctx.send(embed=embed, view=PanelView())

# ---------- COMANDO PANEL (FOCO DONOR) NUEVO ----------
@bot.command(name="panel_foco")
@commands.has_permissions(administrator=True)
async def panel_foco(ctx: commands.Context):
    embed = discord.Embed(
        title="Foco Donor",
        description="Presioná el botón para donar foco a la guild.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Smogg Foco Donor System")
    await ctx.send(embed=embed, view=FocoPanelView())


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def normalizar_nombre(nombre: str) -> str:
    """
    Normaliza un nombre para comparar ignorando MAYÚSCULAS/minúsculas.

    NO elimina:
    - espacios
    - guiones
    - números
    - símbolos
    - caracteres especiales

    Ejemplos:

    Carlos == carlos       -> True
    CARLOS == Carlos       -> True
    Carlos123 == Carlos    -> False
    Carlitos == Carlos     -> False
    """

    return nombre.casefold()


def extraer_ids(texto: str) -> list[str]:
    """
    Acepta:

    1461052597
    1461052597,1461051559,1461048629

    https://albionbb.com/battles/1461052597

    https://albionbb.com/battles/multi?ids=1461052597,1461051559,1461048629

    y devuelve únicamente los IDs.
    """

    texto = texto.strip()

    # Si es una URL con ?ids=
    match = re.search(r"[?&]ids=([^&\s]+)", texto, re.IGNORECASE)

    if match:
        texto = match.group(1)

    # Extraer todos los números
    ids = re.findall(r"\d+", texto)

    # Eliminar duplicados manteniendo el orden
    resultado = []

    for battle_id in ids:
        if battle_id not in resultado:
            resultado.append(battle_id)

    return resultado


async def obtener_jugadores_batalla_albion(
    session: aiohttp.ClientSession,
    battle_id: str,
) -> list[dict[str, str]]:
    """
    Obtiene los participantes de UNA batalla desde el endpoint de detalle
    de AlbionBB: /battles/{id}.

    AlbionBB puede cambiar ligeramente la estructura JSON, por eso el
    extractor acepta las variantes habituales de Name/GuildName/Guild.
    """
    url = f"{ALBIONBB_BATTLE_URL}/{battle_id}"

    async with session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(
                f"AlbionBB batalla {battle_id}: HTTP {response.status}"
            )
        data = await response.json(content_type=None)

    encontrados: dict[tuple[str, str], dict[str, str]] = {}

    def extraer(obj):
        if isinstance(obj, dict):
            nombre = obj.get("Name") or obj.get("name")

            guild_name = obj.get("GuildName") or obj.get("guildName")
            guild_obj = obj.get("Guild") or obj.get("guild")

            if not guild_name and isinstance(guild_obj, dict):
                guild_name = guild_obj.get("Name") or guild_obj.get("name")
            elif not guild_name and isinstance(guild_obj, str):
                guild_name = guild_obj

            if isinstance(nombre, str) and isinstance(guild_name, str) and guild_name.strip():
                clave = (normalizar_nombre(nombre), normalizar_nombre(guild_name))
                encontrados.setdefault(
                    clave,
                    {"name": nombre, "guild": guild_name}
                )

            for value in obj.values():
                extraer(value)

        elif isinstance(obj, list):
            for value in obj:
                extraer(value)

    extraer(data)
    return list(encontrados.values())


async def obtener_jugadores_albion(
    battle_ids: list[str],
) -> dict[str, dict[str, str]]:
    """
    Obtiene los participantes de todas las batallas solicitadas.

    Primero usa /battles/{id}, que es la fuente adecuada para obtener la
    tabla de participantes de una batalla. Si una batalla no devuelve
    participantes, usa /battles/kills como respaldo.

    El resultado se indexa por nombre normalizado y elimina duplicados.
    """
    timeout = aiohttp.ClientTimeout(total=45)
    jugadores: dict[str, dict[str, str]] = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for battle_id in battle_ids:
            try:
                participantes = await obtener_jugadores_batalla_albion(
                    session, battle_id
                )
            except Exception as error:
                print(f"⚠️ Error obteniendo detalle de batalla {battle_id}: {error}")
                participantes = []

            for jugador in participantes:
                clave = normalizar_nombre(jugador["name"])
                jugadores.setdefault(clave, jugador)

        # Fallback: si el detalle no entregó jugadores, intentamos el endpoint
        # de kills para no dejar el comando inutilizable ante cambios de API.
        if not jugadores:
            ids = ",".join(battle_ids)
            async with session.get(ALBIONBB_KILLS_URL, params={"ids": ids}) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"AlbionBB fallback kills: HTTP {response.status}"
                    )
                data = await response.json(content_type=None)

            if not isinstance(data, list):
                raise RuntimeError("La respuesta de AlbionBB no tiene el formato esperado.")

            def agregar_participante(jugador):
                if not isinstance(jugador, dict):
                    return

                nombre = jugador.get("Name") or jugador.get("name")
                guild = jugador.get("GuildName") or jugador.get("guildName")

                if not nombre or not guild:
                    return

                clave = normalizar_nombre(nombre)
                jugadores.setdefault(
                    clave,
                    {"name": nombre, "guild": guild}
                )

            for evento in data:
                agregar_participante(evento.get("Killer", {}))
                agregar_participante(evento.get("Victim", {}))
                for participante in evento.get("Participants", []) or []:
                    agregar_participante(participante)
                for participante in evento.get("GroupMembers", []) or []:
                    agregar_participante(participante)

    return jugadores


# ============================================================
# MATCHING DISCORD
# ============================================================

def construir_indice_discord(
    guild: discord.Guild
) -> dict[str, list[discord.Member]]:

    """
    Construye un índice:

    nombre -> miembros Discord

    Busca tanto:
    - nickname/apodo
    - username

    Todo comparado ignorando mayúsculas.
    """

    indice = {}

    for member in guild.members:

        # ----------------------------------------------------
        # Username
        # ----------------------------------------------------

        username = normalizar_nombre(member.name)

        indice.setdefault(username, [])
        indice[username].append(member)

        # ----------------------------------------------------
        # Nickname / apodo
        # ----------------------------------------------------

        if member.nick:

            nickname = normalizar_nombre(member.nick)

            indice.setdefault(nickname, [])
            indice[nickname].append(member)

    return indice


# ============================================================
# SLASH COMMAND: ROL DESDE ALBIONBB
# ============================================================

@bot.tree.command(
    name="rolalbion",
    description="Crea un rol y lo asigna a participantes de AlbionBB.",
    guild=GUILD_OBJ,
)
@app_commands.describe(
    ids="IDs o link de AlbionBB. Ej: 1461052597,1461051559,1461048629",
    guild="Nombre exacto de la guild de Albion. Ej: Smogg",
    rol="Nombre del rol que se creará. Ej: Smogg ZvZ"
)
async def rolalbion(
    interaction: discord.Interaction,
    ids: str,
    guild: str,
    rol: str,
):
    """Crea un rol y lo asigna mediante coincidencia exacta de nombres."""
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send(
            "❌ Este comando solo puede usarse dentro del servidor.",
            ephemeral=True,
        )

    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.followup.send(
            "❌ Necesitás el permiso **Gestionar roles** para usar este comando.",
            ephemeral=True,
        )

    battle_ids = extraer_ids(ids)
    if not battle_ids:
        return await interaction.followup.send(
            "❌ No encontré ningún ID de batalla.\n\n"
            "Ejemplo:\n"
            "`/rolalbion ids:1461052597,1461051559,1461048629 guild:Smogg rol:Smogg ZvZ`",
            ephemeral=True,
        )

    if len(battle_ids) > 50:
        return await interaction.followup.send(
            "❌ Podés consultar como máximo 50 batallas por comando.",
            ephemeral=True,
        )

    guild_name = guild.strip()
    role_name = rol.strip()
    if not guild_name or not role_name:
        return await interaction.followup.send(
            "❌ La guild y el nombre del rol no pueden estar vacíos.",
            ephemeral=True,
        )

    # Obtener participantes reales de las batallas.
    try:
        jugadores = await obtener_jugadores_albion(battle_ids)
    except Exception as error:
        print(f"❌ /rolalbion AlbionBB: {error!r}")
        return await interaction.followup.send(
            "❌ No pude obtener los participantes de AlbionBB.\n"
            "Revisá los IDs y que AlbionBB esté disponible.",
            ephemeral=True,
        )

    guild_key = normalizar_nombre(guild_name)
    jugadores_guild = []

    for datos in jugadores.values():
        if normalizar_nombre(datos["guild"]) == guild_key:
            jugadores_guild.append(datos["name"])

    # Deduplicar jugadores que aparecieron en varias batallas.
    jugadores_guild = list(dict.fromkeys(jugadores_guild))

    if not jugadores_guild:
        return await interaction.followup.send(
            f"❌ No encontré participantes de **{guild_name}** en las "
            f"{len(battle_ids)} batallas consultadas.",
            ephemeral=True,
        )

    # Discord necesita la lista de miembros para poder comparar apodos.
    try:
        await interaction.guild.chunk(cache=True)
    except Exception:
        pass

    indice_discord: dict[str, dict[int, discord.Member]] = {}

    for member in interaction.guild.members:
        # Username actual de Discord.
        username_key = normalizar_nombre(member.name)
        indice_discord.setdefault(username_key, {})[member.id] = member

        # Apodo dentro del servidor.
        if member.nick:
            nickname_key = normalizar_nombre(member.nick)
            indice_discord.setdefault(nickname_key, {})[member.id] = member

    encontrados: list[tuple[str, discord.Member]] = []
    no_encontrados: list[str] = []
    ambiguos: list[tuple[str, list[discord.Member]]] = []

    for jugador in jugadores_guild:
        candidatos = list(indice_discord.get(normalizar_nombre(jugador), {}).values())

        if len(candidatos) == 0:
            no_encontrados.append(jugador)
        elif len(candidatos) == 1:
            encontrados.append((jugador, candidatos[0]))
        else:
            ambiguos.append((jugador, candidatos))

    # Crear o reutilizar el rol.
    role = discord.utils.get(interaction.guild.roles, name=role_name)

    if role is None:
        try:
            role = await interaction.guild.create_role(
                name=role_name,
                reason=f"Creado por /rolalbion para la guild {guild_name}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ No puedo crear roles. Necesito **Gestionar roles**.",
                ephemeral=True,
            )
        except discord.HTTPException as error:
            return await interaction.followup.send(
                f"❌ Discord rechazó la creación del rol: `{error}`",
                ephemeral=True,
            )

    bot_member = interaction.guild.me
    if bot_member is None or role >= bot_member.top_role:
        return await interaction.followup.send(
            "❌ El rol está por encima o al mismo nivel que el rol del bot.\n"
            "Mové el rol del bot por encima del rol que querés asignar.",
            ephemeral=True,
        )

    asignados = []
    ya_tenian = []
    errores = []

    for jugador, member in encontrados:
        try:
            if role in member.roles:
                ya_tenian.append((jugador, member))
            else:
                await member.add_roles(
                    role,
                    reason=f"Participación AlbionBB - {guild_name}",
                )
                asignados.append((jugador, member))
        except discord.Forbidden:
            errores.append((jugador, member))
        except discord.HTTPException as error:
            print(f"⚠️ Error asignando {role_name} a {member}: {error}")
            errores.append((jugador, member))

    # Resultado compacto. Los detalles se mandan en mensajes efímeros separados
    # para no superar el límite de 2000 caracteres de Discord.
    resumen = (
        "## ⚔️ Rol AlbionBB\n"
        f"**Guild:** `{guild_name}`\n"
        f"**Batallas:** `{len(battle_ids)}`\n"
        f"**Jugadores de la guild:** `{len(jugadores_guild)}`\n"
        f"**Encontrados:** `{len(encontrados)}`\n"
        f"**Asignados ahora:** `{len(asignados)}`\n"
        f"**Ya tenían el rol:** `{len(ya_tenian)}`\n"
        f"**No encontrados:** `{len(no_encontrados)}`\n"
        f"**Ambiguos:** `{len(ambiguos)}`\n"
        f"**Errores:** `{len(errores)}`\n\n"
        f"**Rol:** {role.mention}"
    )

    await interaction.followup.send(
        resumen,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )

    detalles: list[str] = []
    detalles.extend(f"✓ `{j}` → {m.display_name}" for j, m in asignados)
    detalles.extend(f"↪ `{j}` → {m.display_name} (ya lo tenía)" for j, m in ya_tenian)
    detalles.extend(f"✗ `{j}` → NO ENCONTRADO" for j in no_encontrados)
    detalles.extend(
        f"⚠ `{j}` → {', '.join(m.display_name for m in miembros)}"
        for j, miembros in ambiguos
    )
    detalles.extend(f"🚫 `{j}` → {m.display_name} (sin permisos)" for j, m in errores)

    if detalles:
        for inicio in range(0, len(detalles), 25):
            bloque = "\n".join(detalles[inicio:inicio + 25])
            await interaction.followup.send(
                bloque,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

# ============================================================
# TAB SELL / CALCULAR SPLIT
# ============================================================
SPLIT_PANEL_CHANNEL_ID = 1557432820257923183
SPLIT_LOG_CHANNEL_ID = 1557434386775932969
SPLIT_SELLER_ROLE_IDS = {1257896709246423083, 1257896977220501557}
SPLIT_PING_ROLE_ID = 1468314531825713338
SPLIT_DEFAULT_DISCOUNT = 15.0

split_log_ids: dict[int, int] = {}   # panel_message_id -> log_message_id
split_locks: dict[int, asyncio.Lock] = {}


def fmt_num(n: float) -> str:
    """1234567.0 -> '1,234,567' | 15.5 -> '15.5'"""
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def build_split_log(
    panel_url: str, city: str, tab: str, creator_id: int,
    total: float, reparacion: float, pct: float, tax: float, precio_venta: float,
) -> str:
    return (
        f"Link al mensaje: {panel_url}\n"
        f"City: {city}\n"
        f"Nombre tab: {tab}\n"
        f"Creador de la tab: <@{creator_id}>\n"
        f"Vendedor: \n"
        f"Comprador: \n"
        f"Total: {fmt_num(total)}\n"
        f"Reparacion: {fmt_num(reparacion)}\n"
        f"Tax venta: {fmt_num(pct)}% ({fmt_num(tax)})\n"
        f"Precio de venta: {fmt_num(precio_venta)}"
    )


async def _get_channel(guild: discord.Guild, channel_id: int):
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            return None
    return ch


async def find_split_log_message(guild: discord.Guild, panel_msg: discord.Message) -> Optional[discord.Message]:
    log_ch = await _get_channel(guild, SPLIT_LOG_CHANNEL_ID)
    if not isinstance(log_ch, discord.TextChannel):
        return None

    log_id = split_log_ids.get(panel_msg.id)
    if log_id:
        try:
            return await log_ch.fetch_message(log_id)
        except Exception:
            pass

    # Fallback (por si el bot reinició): buscar por el link del panel
    needle = f"Link al mensaje: {panel_msg.jump_url}"
    try:
        async for m in log_ch.history(limit=500):
            if m.author.id == bot.user.id and m.content.startswith(needle):
                save_split_log_mapping(panel_msg.id, m.id)
                return m
    except Exception:
        pass
    return None


class SplitBuyerModal(discord.ui.Modal, title="Marcar tab como vendido"):
    comprador = discord.ui.TextInput(
        label="¿A quién se lo vendiste?",
        placeholder="Nombre del comprador",
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(self, panel_msg: discord.Message):
        super().__init__()
        self.panel_msg = panel_msg

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        buyer = self.comprador.value.strip()
        if not buyer:
            return await respond_ephemeral(interaction, "❌ Tenés que indicar el comprador.")

        await interaction.response.defer(ephemeral=True)
        lock = split_locks.setdefault(self.panel_msg.id, asyncio.Lock())

        async with lock:
            # Releer el panel para saber si alguien ya lo vendió
            try:
                panel = await self.panel_msg.channel.fetch_message(self.panel_msg.id)
            except Exception:
                return await interaction.followup.send("❌ No pude leer el panel.", ephemeral=True)

            if "Vendido por" in (panel.content or ""):
                return await interaction.followup.send("❌ Este tab ya fue marcado como vendido.", ephemeral=True)

            log_msg = await find_split_log_message(guild, panel)
            if log_msg is None:
                return await interaction.followup.send("❌ No encontré el registro de este tab en el canal de logs.", ephemeral=True)

            # Editar log: completar Vendedor y Comprador
            new_content = re.sub(
                r"^Vendedor:.*$", lambda _m: f"Vendedor: {interaction.user.mention}",
                log_msg.content, count=1, flags=re.MULTILINE,
            )
            new_content = re.sub(
                r"^Comprador:.*$", lambda _m: f"Comprador: {buyer}",
                new_content, count=1, flags=re.MULTILINE,
            )
            try:
                await log_msg.edit(content=new_content, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                return await interaction.followup.send("❌ No pude editar el registro.", ephemeral=True)

            # Editar panel: marcar vendido, agregar comprador y deshabilitar botón
            try:
                await panel.edit(
                    content=(
                        f"{panel.content}\n\n"
                        f"✅ **Vendido por** {interaction.user.display_name}\n"
                        f"Vendido a: {buyer}"
                    ),
                    view=SplitSoldView(disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

        await interaction.followup.send("✅ Registrado como vendido.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            await respond_ephemeral(interaction, "❌ Ocurrió un error al registrar la venta.")
        except Exception:
            pass


class SplitSoldView(discord.ui.View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(label="💰 Vendido", style=discord.ButtonStyle.success, custom_id="split_sold_btn")
    async def sold(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        user_role_ids = {r.id for r in interaction.user.roles}
        if not (user_role_ids & SPLIT_SELLER_ROLE_IDS):
            return await respond_ephemeral(interaction, "❌ No tenés permiso para marcar este tab como vendido.")

        msg = interaction.message
        if msg is None:
            return await respond_ephemeral(interaction, "❌ No pude leer el panel.")

        if "Vendido por" in (msg.content or ""):
            return await respond_ephemeral(interaction, "❌ Este tab ya fue marcado como vendido.")

        await interaction.response.send_modal(SplitBuyerModal(msg))


@bot.tree.command(
    name="tabsell",
    description="Calcula el precio de venta de un tab y crea el panel.",
    guild=GUILD_OBJ,
)
@app_commands.describe(
    nombre_tab="Nombre del tab",
    city="Ciudad donde se vende",
    total="Total",
    reparacion="Costo de reparación",
    descuento="Porcentaje de descuento/tax de venta (opcional, default 15)",
)
async def tabsell(
    interaction: discord.Interaction,
    nombre_tab: str,
    city: str,
    total: float,
    reparacion: float,
    descuento: Optional[app_commands.Range[float, 0, 100]] = None,
):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    await interaction.response.defer(ephemeral=True)

    panel_ch = await _get_channel(interaction.guild, SPLIT_PANEL_CHANNEL_ID)
    if not isinstance(panel_ch, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de paneles.", ephemeral=True)

    log_ch = await _get_channel(interaction.guild, SPLIT_LOG_CHANNEL_ID)
    if not isinstance(log_ch, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de logs.", ephemeral=True)

    nombre_tab = nombre_tab.strip()
    city = city.strip()
    pct = SPLIT_DEFAULT_DISCOUNT if descuento is None else descuento

    tax = round(total * pct / 100)
    precio_venta = round(total - tax - reparacion)

    content = (
        f"<@&{SPLIT_PING_ROLE_ID}>\n"
        "Tab Sell\n"
        f"Nombre tab: {nombre_tab}\n"
        f"City: {city}\n"
        f"Precio de venta (-{fmt_num(pct)}%): {fmt_num(precio_venta)}"
    )

    try:
        panel_msg = await panel_ch.send(
            content,
            view=SplitSoldView(),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permisos para escribir en el canal de paneles.", ephemeral=True)
    except Exception:
        return await interaction.followup.send("❌ No pude crear el panel.", ephemeral=True)

    # Log creado al ejecutar el comando
    log_text = build_split_log(
        panel_msg.jump_url, city, nombre_tab, interaction.user.id,
        total, reparacion, pct, tax, precio_venta,
    )
    try:
        log_msg = await log_ch.send(log_text, allowed_mentions=discord.AllowedMentions.none())
        save_split_log_mapping(panel_msg.id, log_msg.id)
    except Exception:
        await interaction.followup.send(
            "⚠️ Panel creado pero no pude escribir el registro en el canal de logs.",
            ephemeral=True,
        )

    thread_warning = ""
    try:
        await panel_msg.create_thread(name=nombre_tab[:100] or "Tab Sell")
    except Exception:
        thread_warning = "\n⚠️ No pude crear el hilo (revisá el permiso **Crear hilos públicos**)."

    await interaction.followup.send(
        f"✅ Panel creado: {panel_msg.jump_url}\n"
        f"Total {fmt_num(total)} - {fmt_num(pct)}% ({fmt_num(tax)}) - reparación {fmt_num(reparacion)}\n"
        f"Precio de venta: **{fmt_num(precio_venta)}**"
        f"{thread_warning}",
        ephemeral=True,
    )


# ============================================================
# /calcularsplit (solo Staff)
# ============================================================
SPLIT_CALC_DEFAULT_SALE_TAX = 15.0
SPLIT_CALC_DEFAULT_GUILD_TAX = 5.0
SPLIT_CALC_MAX_NUMBERS = 50
_SPLIT_CALC_SEPARATORS_RE = re.compile(r"[\s+;]+")
_SPLIT_CALC_NUMBER_RE = re.compile(r"^\d{1,15}(?:\.\d{1,4})?$")


def parse_number_sum(text: str) -> Optional[float]:
    """'1000000 500 23333' -> 1023833.0

    Los números se separan con espacios (también se aceptan '+' y ';').
    La coma NO es separador: '1,000,000' es inválido en vez de sumarse mal en silencio.
    Devuelve None si no hay números, hay demasiados o algún valor no es válido.
    """
    tokens = [t for t in _SPLIT_CALC_SEPARATORS_RE.split((text or "").strip()) if t]
    if not tokens or len(tokens) > SPLIT_CALC_MAX_NUMBERS:
        return None
    total = Decimal(0)
    for token in tokens:
        if not _SPLIT_CALC_NUMBER_RE.match(token):
            return None
        total += Decimal(token)
    return float(total)


def calc_split_final(total: float, sale_tax: float, guild_tax: float, reparacion: float) -> int:
    """Split final = total - (total × (tax venta + tax guild) / 100) - reparación."""
    return round(total - (total * (sale_tax + guild_tax) / 100) - reparacion)


def build_split_calc_panel(
    total: float, reparacion: float, bolsitas: float,
    sale_tax: float, guild_tax: float, split_final: float,
) -> str:
    return (
        "Split:\n"
        f"Total: {fmt_num(total)}\n"
        f"Reparacion: {fmt_num(reparacion)}\n"
        f"Bolsitas: {fmt_num(bolsitas)}\n"
        f"Tax de venta: {fmt_num(sale_tax)}%\n"
        f"Tax de guild: {fmt_num(guild_tax)}%\n\n"
        f"Split final: {fmt_num(split_final)}"
    )


@bot.tree.command(
    name="calcularsplit",
    description="(Staff) Calcula el split y publica el panel en este canal.",
    guild=GUILD_OBJ,
)
@staff_only_slash()
@app_commands.describe(
    total="Total. Podés poner varios números separados por espacio. Ej: 1000000 500 23333",
    reparacion="Reparación. Podés poner varios números separados por espacio. Ej: 15000 2500",
    bolsitas="Bolsitas (opcional, default 0)",
    tax_de_venta="Tax de venta en % (opcional, default 15)",
    tax_de_guild="Tax de guild en % (opcional, default 5)",
)
async def calcularsplit_slash(
    interaction: discord.Interaction,
    total: str,
    reparacion: str,
    bolsitas: Optional[app_commands.Range[float, 0.0, 1e15]] = None,
    tax_de_venta: Optional[app_commands.Range[float, 0, 100]] = None,
    tax_de_guild: Optional[app_commands.Range[float, 0, 100]] = None,
):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    total_sum = parse_number_sum(total)
    if total_sum is None:
        return await respond_ephemeral(
            interaction,
            "❌ **Total** inválido. Usá solo números separados por espacios, ej: `1000000 500 23333` "
            "(sin comas ni puntos de miles)."
        )

    rep_sum = parse_number_sum(reparacion)
    if rep_sum is None:
        return await respond_ephemeral(
            interaction,
            "❌ **Reparacion** inválida. Usá solo números separados por espacios, ej: `15000 2500` "
            "(sin comas ni puntos de miles)."
        )

    bag = 0.0 if bolsitas is None else bolsitas
    sale_tax = SPLIT_CALC_DEFAULT_SALE_TAX if tax_de_venta is None else tax_de_venta
    guild_tax = SPLIT_CALC_DEFAULT_GUILD_TAX if tax_de_guild is None else tax_de_guild

    if sale_tax + guild_tax > 100:
        return await respond_ephemeral(interaction, "❌ La suma de **tax de venta** y **tax de guild** no puede superar 100%.")

    final = calc_split_final(total_sum, sale_tax, guild_tax, rep_sum)

    # Mensaje público en el canal donde se ejecutó el comando
    await interaction.response.send_message(
        build_split_calc_panel(total_sum, rep_sum, bag, sale_tax, guild_tax, final),
        allowed_mentions=discord.AllowedMentions.none(),
    )

@calcularsplit_slash.error
async def calcularsplit_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return await respond_ephemeral(interaction, "❌ Solo **Staff** puede usar `/calcularsplit`.")
    return await respond_ephemeral(interaction, "❌ Error ejecutando el comando.")


# ================== SISTEMA CTA SMOGG ==================
# Las composiciones, CTA y reservas se guardan en SQLite usando la base existente.
import json
import copy
import contextlib

CTA_EMOJIS_DEFAULT = {
    "Golem": "🔨", "Heavy Mace": "🛡️", "Monarch": "🛡️", "GA": "🛡️",
    "Carving": "🔮", "Shadowcaller": "🔮", "Rootbound": "🔮", "Locus": "🔮",
    "Hallow Fall": "✨", "Exalted": "✨", "1h Nature": "✨", "Longbow": "🏹",
    "Spiked": "🐻", "Realmbraker": "✨", "Dawnsong": "🏹", "HoJ": "🛡️",
}
CTA_BRAWL_SLOTS = [
    (1, "Golem"), (2, "Heavy Mace"), (3, "Monarch"), (4, "GA"),
    (5, "Carving"), (6, "Shadowcaller"), (7, "Rootbound"), (8, "Locus"),
    (9, "Hallow Fall"), (10, "Hallow Fall"), (11, "Exalted"), (12, "1h Nature"),
    (13, "Longbow"), (14, "Longbow"), (15, "Longbow"), (16, "Longbow"),
    (17, "Spiked"), (18, "Realmbraker"), (19, "Dawnsong"), (20, "HoJ"),
]


def cta_db_init():
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cta_compositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            created_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        );
        CREATE TABLE IF NOT EXISTS cta_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            composition_id INTEGER NOT NULL,
            party_no INTEGER NOT NULL DEFAULT 1,
            position_no INTEGER NOT NULL,
            role_name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '⚔️',
            access_mode TEXT NOT NULL DEFAULT 'any',
            role_ids_json TEXT NOT NULL DEFAULT '[]',
            access_match TEXT NOT NULL DEFAULT 'any',
            UNIQUE(composition_id, party_no, position_no),
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_party_access (
            composition_id INTEGER NOT NULL,
            party_no INTEGER NOT NULL,
            access_mode TEXT NOT NULL DEFAULT 'any',
            role_ids_json TEXT NOT NULL DEFAULT '[]',
            access_match TEXT NOT NULL DEFAULT 'any',
            PRIMARY KEY(composition_id, party_no),
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            composition_id INTEGER,
            composition_name TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            thread_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            closes_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS cta_signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cta_id INTEGER NOT NULL,
            slot_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            assigned_by INTEGER,
            override_reason TEXT,
            signed_at TEXT NOT NULL,
            UNIQUE(cta_id, slot_key),
            UNIQUE(cta_id, user_id),
            FOREIGN KEY(cta_id) REFERENCES cta_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            cta_id INTEGER,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cta_settings (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_cta_runs_status ON cta_runs(guild_id, status, closes_at);
        CREATE INDEX IF NOT EXISTS idx_cta_signups_cta ON cta_signups(cta_id);
        """)
        conn.commit()


cta_db_init()


def cta_now_iso():
    return datetime.now(timezone.utc).isoformat()


def cta_audit(guild_id: int, actor_id: int, action: str, cta_id=None, details=""):
    db_execute("INSERT INTO cta_audit(guild_id, cta_id, actor_id, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
               (guild_id, cta_id, actor_id, action, str(details)[:1500], cta_now_iso()))


def cta_get_composition(guild_id: int, name: str):
    rows = db_fetchall("SELECT * FROM cta_compositions WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, name.strip()))
    if not rows:
        return None
    comp = dict(rows[0])
    comp["slots"] = [dict(r) for r in db_fetchall("SELECT * FROM cta_slots WHERE composition_id=? ORDER BY party_no, position_no", (comp["id"],))]
    for slot in comp["slots"]:
        slot["role_ids"] = json.loads(slot.pop("role_ids_json", "[]"))
    return comp


def cta_ensure_brawl(guild_id: int, creator_id: int):
    if cta_get_composition(guild_id, "brawl"):
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO cta_compositions(guild_id,name,created_by,updated_at) VALUES (?,?,?,?)",
                           (guild_id, "brawl", creator_id, cta_now_iso()))
        row = conn.execute("SELECT id FROM cta_compositions WHERE guild_id=? AND name='brawl' COLLATE NOCASE", (guild_id,)).fetchone()
        if not row:
            return
        cid = row[0]
        for pos, role_name in CTA_BRAWL_SLOTS:
            conn.execute("INSERT OR IGNORE INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,?)",
                         (cid, 1, pos, role_name, CTA_EMOJIS_DEFAULT.get(role_name, "⚔️")))
        conn.commit()


def cta_slot_key(slot: dict) -> str:
    return f"{int(slot['party_no'])}:{int(slot['position_no'])}"


def cta_run_get(run_id: int):
    rows = db_fetchall("SELECT * FROM cta_runs WHERE id=?", (run_id,))
    return dict(rows[0]) if rows else None


def cta_get_signups(run_id: int):
    return [dict(r) for r in db_fetchall("SELECT * FROM cta_signups WHERE cta_id=? ORDER BY signed_at", (run_id,))]


def cta_user_has_role(member: discord.Member, role_ids: list[int], mode: str) -> bool:
    member_roles = {r.id for r in member.roles}
    required = {int(x) for x in role_ids}
    if not required:
        return True
    return required.issubset(member_roles) if mode == "all" else bool(required & member_roles)


def cta_access_error(member: discord.Member, slot: dict, guild: discord.Guild) -> Optional[str]:
    party_restriction = slot.get("party_access") or {"mode": "any", "role_ids": [], "match": "any"}
    checks = [
        (party_restriction.get("mode", "any"), party_restriction.get("role_ids", []), party_restriction.get("match", "any"), "la party"),
        (slot.get("access_mode", "any"), slot.get("role_ids", []), slot.get("access_match", "any"), "esta posición"),
    ]
    for mode, role_ids, match, where in checks:
        if mode != "roles" or not role_ids:
            continue
        if not cta_user_has_role(member, role_ids, match):
            names = [guild.get_role(int(rid)).mention if guild.get_role(int(rid)) else f"rol eliminado ({rid})" for rid in role_ids]
            condition = "todos estos roles" if match == "all" else "uno de estos roles"
            return f"No podés anotarte en {where}: necesitás {condition}: {' '.join(names)}."
    return None


def cta_snapshot_from_comp(comp: dict):
    party_rows = db_fetchall("SELECT party_no, access_mode, role_ids_json, access_match FROM cta_party_access WHERE composition_id=?", (comp["id"],))
    party_access = {int(r["party_no"]): {"mode": r["access_mode"], "role_ids": json.loads(r["role_ids_json"] or "[]"), "match": r["access_match"]} for r in party_rows}
    slots = []
    for s in comp["slots"]:
        slots.append({
            "party_no": int(s["party_no"]), "position_no": int(s["position_no"]),
            "role_name": s["role_name"], "emoji": s["emoji"],
            "access_mode": s.get("access_mode", "any"), "role_ids": s.get("role_ids", []),
            "access_match": s.get("access_match", "any"),
            "party_access": party_access.get(int(s["party_no"]), {"mode": "any", "role_ids": [], "match": "any"})
        })
    return {"name": comp["name"], "slots": slots, "party_access": party_access}


def cta_build_embed(run: dict, guild: discord.Guild):
    snapshot = json.loads(run["snapshot_json"])
    signups = cta_get_signups(run["id"])
    assigned = {s["slot_key"]: int(s["user_id"]) for s in signups}
    closes = datetime.fromisoformat(run["closes_at"])
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    remaining = max(0, int((closes - datetime.now(timezone.utc)).total_seconds())) if run["status"] == "open" else 0
    mm, ss = divmod(remaining, 60)
    hh, mm = divmod(mm, 60)
    status_text = "🟢 INSCRIPCIONES ABIERTAS" if run["status"] == "open" else "🔒 CTA CERRADA"
    embed = discord.Embed(title=f"⚔️ SMOGG · CTA {run['composition_name'].upper()}", description=f"{status_text}\nCierre: <t:{int(closes.timestamp())}:F> · " + (f"Restante: **{hh:02d}:{mm:02d}:{ss:02d}**" if run["status"] == "open" else "Inscripciones finalizadas"), color=discord.Color.green() if run["status"] == "open" else discord.Color.dark_grey())
    parties = {}
    for slot in snapshot["slots"]:
        parties.setdefault(int(slot["party_no"]), []).append(slot)
    for party_no, slots in sorted(parties.items()):
        lines = []
        for slot in sorted(slots, key=lambda x: int(x["position_no"])):
            key = cta_slot_key(slot)
            user_id = assigned.get(key)
            who = f"<@{user_id}>" if user_id else "*Libre*"
            restricted = " 🔒" if slot.get("access_mode") == "roles" or slot.get("party_access", {}).get("mode") == "roles" else ""
            lines.append(f"**{slot['position_no']}.** {slot.get('emoji','⚔️')} **{slot['role_name']}** — {who}{restricted}")
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1000] + "\n…"
        embed.add_field(name=f"PARTY {party_no}", value=value or "Sin posiciones", inline=False)
    embed.set_footer(text=f"Plazas ocupadas: {len(signups)}/{len(snapshot['slots'])} · CTA #{run['id']}")
    return embed


async def cta_refresh_panel(bot_obj, run_id: int):
    run = cta_run_get(run_id)
    if not run or not run.get("message_id"):
        return
    guild = bot_obj.get_guild(int(run["guild_id"]))
    if not guild:
        return
    channel = guild.get_channel(int(run["channel_id"]))
    if channel is None:
        try:
            channel = await bot_obj.fetch_channel(int(run["channel_id"]))
        except Exception:
            return
    try:
        msg = await channel.fetch_message(int(run["message_id"]))
        await msg.edit(embed=cta_build_embed(run, guild), view=CTAPanelView(run_id=int(run_id), closed=run["status"] != "open"), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        print(f"CTA #{run_id}: no se pudo actualizar el panel: {exc}")


async def cta_close_run(run_id: int, reason="timeout"):
    run = cta_run_get(run_id)
    if not run or run["status"] != "open":
        return
    db_execute("UPDATE cta_runs SET status='closed', closed_at=? WHERE id=? AND status='open'", (cta_now_iso(), run_id))
    cta_audit(int(run["guild_id"]), int(run["created_by"]), "cta_closed", run_id, reason)
    await cta_refresh_panel(bot, run_id)


class CTASlotPicker(discord.ui.View):
    def __init__(self, run_id: int, user_id: int, page: int = 0):
        super().__init__(timeout=180)
        self.run_id, self.user_id, self.page = run_id, user_id, page
        run = cta_run_get(run_id)
        if not run or run["status"] != "open":
            return
        snapshot = json.loads(run["snapshot_json"])
        signups = cta_get_signups(run_id)
        occupied = {s["slot_key"] for s in signups}
        guild = bot.get_guild(int(run["guild_id"]))
        member = guild.get_member(user_id) if guild else None
        available = []
        for slot in snapshot["slots"]:
            key = cta_slot_key(slot)
            if key in occupied:
                continue
            if member and not cta_access_error(member, slot, guild):
                available.append(slot)
        # Discord select menu admite como máximo 25 opciones; paginar si hace falta.
        self.pages = [available[i:i + 25] for i in range(0, len(available), 25)] or [[]]
        self.page = max(0, min(self.page, len(self.pages) - 1))
        page_slots = self.pages[self.page]
        if page_slots:
            opts = [discord.SelectOption(label=f"P{int(s['party_no'])} · {s['position_no']} · {s['role_name']}"[:100], value=cta_slot_key(s), description=f"Plaza {s['position_no']} de Party {s['party_no']}"[:100]) for s in page_slots]
            select = discord.ui.Select(placeholder="Elegí una plaza libre", min_values=1, max_values=1, options=opts, custom_id=f"cta:pick:{run_id}:{user_id}:{self.page}")
            select.callback = self._select_callback
            self.add_item(select)
        else:
            self.add_item(discord.ui.Button(label="No hay plazas disponibles para vos", style=discord.ButtonStyle.secondary, disabled=True))
        if len(self.pages) > 1:
            prev = discord.ui.Button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            nxt = discord.ui.Button(label="Siguiente", style=discord.ButtonStyle.secondary, disabled=self.page >= len(self.pages) - 1)
            prev.callback = self._prev
            nxt.callback = self._next
            self.add_item(prev)
            self.add_item(nxt)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Este selector pertenece a otro jugador.", ephemeral=True)
            return False
        return True

    async def _select_callback(self, interaction: discord.Interaction):
        selected = interaction.data.get("values", [None])[0]
        if not selected:
            return await interaction.response.send_message("No se seleccionó ninguna plaza.", ephemeral=True)
        run = cta_run_get(self.run_id)
        if not run or run["status"] != "open":
            return await interaction.response.send_message("Esta CTA ya está cerrada.", ephemeral=True)
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member or not guild:
            return await interaction.response.send_message("No pude verificar tus roles de Discord.", ephemeral=True)
        snapshot = json.loads(run["snapshot_json"])
        slot = next((s for s in snapshot["slots"] if cta_slot_key(s) == selected), None)
        if not slot:
            return await interaction.response.send_message("La plaza ya no existe.", ephemeral=True)
        error = cta_access_error(member, slot, guild)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        try:
            with sqlite3.connect(DB_PATH, timeout=30, isolation_level=None) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT status, closes_at FROM cta_runs WHERE id=?", (self.run_id,)).fetchone()
                if not current or current[0] != "open" or datetime.fromisoformat(current[1]) <= datetime.now(timezone.utc):
                    conn.execute("ROLLBACK")
                    return await interaction.response.send_message("La inscripción ya está cerrada.", ephemeral=True)
                existing = conn.execute("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?", (self.run_id, member.id)).fetchone()
                if existing:
                    conn.execute("ROLLBACK")
                    return await interaction.response.send_message("Ya estás anotado en esta CTA. Cancelá tu plaza antes de elegir otra.", ephemeral=True)
                conn.execute("INSERT INTO cta_signups(cta_id,slot_key,user_id,signed_at) VALUES (?,?,?,?)", (self.run_id, selected, member.id, cta_now_iso()))
                conn.commit()
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Esa plaza acaba de ser ocupada o ya estás anotado. Elegí otra.", ephemeral=True)
        except sqlite3.Error as exc:
            print(f"Error reservando CTA: {exc}")
            return await interaction.response.send_message("No se pudo guardar la inscripción. Intentá de nuevo.", ephemeral=True)
        cta_audit(guild.id, member.id, "signup", self.run_id, selected)
        await interaction.response.send_message(f"✅ Te anotaste en **Party {slot['party_no']} · {slot['position_no']} — {slot['role_name']}**.", ephemeral=True)
        await cta_refresh_panel(bot, self.run_id)

    async def _prev(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=CTASlotPicker(self.run_id, self.user_id, self.page - 1))

    async def _next(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=CTASlotPicker(self.run_id, self.user_id, self.page + 1))


class CTAPanelView(discord.ui.View):
    def __init__(self, run_id: Optional[int] = None, closed: bool = False):
        super().__init__(timeout=None)
        self.run_id = run_id
        self.closed = closed
        choose = discord.ui.Button(label="Elegir posición", emoji="📝", style=discord.ButtonStyle.success, custom_id="cta:choose" if run_id is None else f"cta:choose:{run_id}", disabled=closed)
        cancel = discord.ui.Button(label="Cancelar mi plaza", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="cta:cancel" if run_id is None else f"cta:cancel:{run_id}", disabled=closed)
        refresh = discord.ui.Button(label="Actualizar panel", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="cta:refresh" if run_id is None else f"cta:refresh:{run_id}")
        choose.callback = self.choose_callback
        cancel.callback = self.cancel_callback
        refresh.callback = self.refresh_callback
        self.add_item(choose)
        self.add_item(cancel)
        self.add_item(refresh)

    def _resolve_run_id(self, interaction: discord.Interaction):
        if self.run_id:
            return self.run_id
        msg_id = interaction.message.id if interaction.message else None
        if not msg_id:
            return None
        rows = db_fetchall("SELECT id FROM cta_runs WHERE message_id=?", (msg_id,))
        return int(rows[0]["id"]) if rows else None

    async def choose_callback(self, interaction: discord.Interaction):
        run_id = self._resolve_run_id(interaction)
        run = cta_run_get(run_id) if run_id else None
        if not run or run["status"] != "open":
            return await interaction.response.send_message("Esta CTA ya está cerrada.", ephemeral=True)
        await interaction.response.send_message("Elegí una de las plazas disponibles para tus roles.", view=CTASlotPicker(run_id, interaction.user.id), ephemeral=True)

    async def cancel_callback(self, interaction: discord.Interaction):
        run_id = self._resolve_run_id(interaction)
        run = cta_run_get(run_id) if run_id else None
        if not run or run["status"] != "open":
            return await interaction.response.send_message("Esta CTA ya está cerrada.", ephemeral=True)
        rows = db_fetchall("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?", (run_id, interaction.user.id))
        if not rows:
            return await interaction.response.send_message("No tenés una plaza asignada en esta CTA.", ephemeral=True)
        db_execute("DELETE FROM cta_signups WHERE cta_id=? AND user_id=?", (run_id, interaction.user.id))
        cta_audit(int(run["guild_id"]), interaction.user.id, "cancel_signup", run_id, rows[0]["slot_key"])
        await interaction.response.send_message("Tu plaza fue liberada.", ephemeral=True)
        await cta_refresh_panel(bot, run_id)

    async def refresh_callback(self, interaction: discord.Interaction):
        run_id = self._resolve_run_id(interaction)
        if not run_id:
            return await interaction.response.send_message("No encontré esta CTA en la base de datos.", ephemeral=True)
        await cta_refresh_panel(bot, run_id)
        await interaction.response.send_message("Panel actualizado.", ephemeral=True)


class CTAAddSlotModal(discord.ui.Modal, title="Agregar posición"):
    party_no = discord.ui.TextInput(label="Número de party", placeholder="1", default="1", max_length=3)
    position_no = discord.ui.TextInput(label="Número de posición dentro de la party", placeholder="21", max_length=3)
    role_name = discord.ui.TextInput(label="Nombre del rol/build", placeholder="Ej. Longbow", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", placeholder="🏹", default="⚔️", max_length=30, required=False)

    def __init__(self, composition_id: int, guild_id: int, actor_id: int, party_default: int = 1, position_default: int = 1):
        super().__init__()
        self.composition_id, self.guild_id, self.actor_id = composition_id, guild_id, actor_id
        self.party_no.default = str(party_default)
        self.position_no.default = str(position_default)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            party = int(str(self.party_no.value).strip())
            pos = int(str(self.position_no.value).strip())
            role = str(self.role_name.value).strip()
            emoji = str(self.emoji.value).strip() or "⚔️"
            if party < 1 or pos < 1 or not role or party > 25 or pos > 25:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("El rol no puede estar vacío. Party y posición deben estar entre 1 y 25 (límite de opciones de Discord por menú/party).", ephemeral=True)
        count = db_fetchall("SELECT COUNT(*) AS n FROM cta_slots WHERE composition_id=? AND party_no=?", (self.composition_id, party))[0]["n"]
        if pos > count + 1:
            return await interaction.response.send_message(f"Para evitar huecos, la próxima posición de Party {party} es {count + 1}.", ephemeral=True)
        try:
            db_execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,?)", (self.composition_id, party, pos, role, emoji))
            db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition_id))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Ya existe esa posición. Elegí otro número.", ephemeral=True)
        cta_audit(self.guild_id, self.actor_id, "composition_add_slot", details=f"comp={self.composition_id}; party={party}; pos={pos}; role={role}")
        await interaction.response.send_message(f"✅ Agregué **{role}** como posición **{pos}** de Party **{party}**.", ephemeral=True)


class CTAEditSlotModal(discord.ui.Modal, title="Editar posición"):
    role_name = discord.ui.TextInput(label="Nombre del rol/build", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", max_length=30, required=False)

    def __init__(self, composition_id: int, slot: dict, guild_id: int, actor_id: int):
        super().__init__()
        self.composition_id, self.slot, self.guild_id, self.actor_id = composition_id, slot, guild_id, actor_id
        self.role_name.default = slot["role_name"]
        self.emoji.default = slot["emoji"]

    async def on_submit(self, interaction: discord.Interaction):
        role = str(self.role_name.value).strip()
        emoji = str(self.emoji.value).strip() or "⚔️"
        if not role:
            return await interaction.response.send_message("El nombre del rol no puede quedar vacío.", ephemeral=True)
        db_execute("UPDATE cta_slots SET role_name=?, emoji=? WHERE id=? AND composition_id=?", (role, emoji, self.slot["id"], self.composition_id))
        db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition_id))
        cta_audit(self.guild_id, self.actor_id, "composition_edit_slot", details=f"comp={self.composition_id}; slot={self.slot['id']}; role={role}")
        await interaction.response.send_message("✅ Posición actualizada.", ephemeral=True)


class CTAAccessRoleView(discord.ui.View):
    def __init__(self, composition_id: int, slot: dict, guild_id: int, actor_id: int):
        super().__init__(timeout=180)
        self.composition_id, self.slot, self.guild_id, self.actor_id = composition_id, slot, guild_id, actor_id
        self.add_item(CTAAccessRoleSelect(self))
        self.add_item(CTAAccessModeSelect(self))


class CTAAccessRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent_view: CTAAccessRoleView):
        super().__init__(placeholder="Elegí los roles permitidos (vacío = todos)", min_values=0, max_values=25)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        role_ids = [str(r.id) for r in self.values]
        mode = getattr(self.parent_view, "mode", "any")
        match = getattr(self.parent_view, "match", "any")
        db_execute("UPDATE cta_slots SET access_mode=?, role_ids_json=?, access_match=? WHERE id=?", ("roles" if role_ids else "any", json.dumps(role_ids), match, self.parent_view.slot["id"]))
        db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.parent_view.composition_id))
        cta_audit(self.parent_view.guild_id, self.parent_view.actor_id, "composition_access", details=f"slot={self.parent_view.slot['id']}; roles={','.join(role_ids)}; match={match}")
        await interaction.response.send_message("✅ Restricción de la posición guardada. Si elegiste varios roles, el modo de coincidencia se configura en el selector siguiente.", ephemeral=True)


class CTAAccessModeSelect(discord.ui.Select):
    def __init__(self, parent_view: CTAAccessRoleView):
        self.parent_view = parent_view
        super().__init__(placeholder="Cómo combinar los roles seleccionados", min_values=1, max_values=1, options=[
            discord.SelectOption(label="Cualquiera de los roles", value="any", description="El jugador necesita al menos uno"),
            discord.SelectOption(label="Todos los roles", value="all", description="El jugador necesita todos"),
        ])

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.match = self.values[0]
        await interaction.response.send_message("Modo seleccionado. Volvé a elegir los roles en el selector para guardar la restricción con este modo.", ephemeral=True)


class CTAPartyAccessView(discord.ui.View):
    def __init__(self, composition: dict, parties: list[int], actor_id: int):
        super().__init__(timeout=180)
        self.composition, self.parties, self.actor_id = composition, parties, actor_id
        self.selected_party = parties[0]
        self.match = "any"
        party_select = discord.ui.Select(placeholder="Elegí una party", options=[discord.SelectOption(label=f"Party {p}", value=str(p), default=(p == parties[0])) for p in parties[:25]])
        party_select.callback = self.select_party
        mode_select = discord.ui.Select(placeholder="Cómo combinar los roles", options=[discord.SelectOption(label="Cualquiera de los roles", value="any"), discord.SelectOption(label="Todos los roles", value="all")])
        mode_select.callback = self.select_mode
        self.role_select = discord.ui.RoleSelect(placeholder="Roles permitidos para esta party", min_values=0, max_values=25)
        self.role_select.callback = self.save_roles
        self.add_item(party_select); self.add_item(mode_select); self.add_item(self.role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Este editor pertenece a otro administrador.", ephemeral=True)
            return False
        return True

    async def select_party(self, interaction: discord.Interaction):
        self.selected_party = int(interaction.data["values"][0])
        await interaction.response.send_message(f"Seleccionada Party {self.selected_party}. Ahora elegí los roles y guardalos con el selector de roles.", ephemeral=True)

    async def select_mode(self, interaction: discord.Interaction):
        self.match = interaction.data["values"][0]
        await interaction.response.send_message("Modo guardado en el editor. Volvé a elegir los roles en el selector para aplicar la configuración.", ephemeral=True)

    async def save_roles(self, interaction: discord.Interaction):
        role_ids = [str(r.id) for r in self.role_select.values]
        db_execute("INSERT INTO cta_party_access(composition_id,party_no,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,?) ON CONFLICT(composition_id,party_no) DO UPDATE SET access_mode=excluded.access_mode, role_ids_json=excluded.role_ids_json, access_match=excluded.access_match", (self.composition["id"], self.selected_party, "roles" if role_ids else "any", json.dumps(role_ids), self.match))
        db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition["id"]))
        cta_audit(self.composition["guild_id"], self.actor_id, "composition_party_access", details=f"comp={self.composition['id']}; party={self.selected_party}; roles={','.join(role_ids)}; match={self.match}")
        await interaction.response.send_message(f"✅ Restricción guardada para Party {self.selected_party}. {'Roles: ' + ', '.join(r.mention for r in self.role_select.values) if role_ids else 'La party queda abierta para todos.'}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CTAMoveSlotModal(discord.ui.Modal, title="Mover / reordenar posición"):
    party_no = discord.ui.TextInput(label="Party destino", placeholder="1", max_length=3)
    position_no = discord.ui.TextInput(label="Posición destino", placeholder="1", max_length=3)

    def __init__(self, composition: dict, slot: dict, actor_id: int):
        super().__init__()
        self.composition, self.slot, self.actor_id = composition, slot, actor_id
        self.party_no.default = str(slot["party_no"])
        self.position_no.default = str(slot["position_no"])

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dest_party, dest_pos = int(str(self.party_no.value)), int(str(self.position_no.value))
            if dest_party < 1 or dest_pos < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Party y posición deben ser números positivos.", ephemeral=True)
        comp_id = self.composition["id"]
        slot_id = self.slot["id"]
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            source_party = int(self.slot["party_no"])
            source_rows = conn.execute("SELECT id FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (comp_id, source_party)).fetchall()
            dest_rows = conn.execute("SELECT id FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (comp_id, dest_party)).fetchall()
            source_ids = [int(r[0]) for r in source_rows if int(r[0]) != int(slot_id)]
            dest_ids = [int(r[0]) for r in dest_rows if int(r[0]) != int(slot_id)]
            target_list = source_ids if source_party == dest_party else dest_ids
            if dest_pos > len(target_list) + 1:
                conn.rollback()
                return await interaction.response.send_message(f"La posición destino máxima es {len(target_list) + 1}.", ephemeral=True)
            if source_party == dest_party:
                target_list.insert(dest_pos - 1, int(slot_id))
                conn.execute("UPDATE cta_slots SET position_no=-id WHERE composition_id=? AND party_no=?", (comp_id, source_party))
                for new_pos, sid in enumerate(target_list, start=1):
                    conn.execute("UPDATE cta_slots SET position_no=?, party_no=? WHERE id=?", (new_pos, dest_party, sid))
            else:
                conn.execute("UPDATE cta_slots SET position_no=-id WHERE composition_id=? AND party_no IN (?,?)", (comp_id, source_party, dest_party))
                dest_ids.insert(dest_pos - 1, int(slot_id))
                conn.execute("UPDATE cta_slots SET party_no=?, position_no=-id WHERE id=?", (dest_party, slot_id))
                for new_pos, sid in enumerate(source_ids, start=1):
                    conn.execute("UPDATE cta_slots SET position_no=? WHERE id=?", (new_pos, sid))
                for new_pos, sid in enumerate(dest_ids, start=1):
                    conn.execute("UPDATE cta_slots SET party_no=?, position_no=? WHERE id=?", (dest_party, new_pos, sid))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), comp_id))
            conn.commit()
        cta_audit(self.composition["guild_id"], self.actor_id, "composition_move_slot", details=f"slot={slot_id}; party={dest_party}; position={dest_pos}")
        await interaction.response.send_message(f"✅ Posición movida a Party {dest_party}, posición {dest_pos}.", ephemeral=True)


class CTACompositionEditorView(discord.ui.View):
    def __init__(self, composition: dict, actor_id: int, party_filter: Optional[int] = None):
        super().__init__(timeout=300)
        self.composition = composition
        self.actor_id = actor_id
        self.party_filter = party_filter
        parties = sorted({int(s["party_no"]) for s in composition["slots"]})
        if parties and party_filter is None:
            self.party_filter = parties[0]
        if len(parties) > 1:
            pselect = discord.ui.Select(placeholder="Elegí una party para editar", options=[discord.SelectOption(label=f"Party {p}", value=str(p), default=(p == self.party_filter)) for p in parties[:25]])
            pselect.callback = self.select_party
            self.add_item(pselect)
        visible_slots = [s for s in composition["slots"] if self.party_filter is None or int(s["party_no"]) == int(self.party_filter)]
        self.selected_slot_id = visible_slots[0]["id"] if visible_slots else None
        options = [discord.SelectOption(label=f"P{s['party_no']} · {s['position_no']} · {s['role_name']}"[:100], value=str(s["id"])) for s in visible_slots[:25]]
        if options:
            select = discord.ui.Select(placeholder="Seleccioná una posición para editar", options=options)
            select.callback = self.select_slot
            self.add_item(select)
        add = discord.ui.Button(label="Agregar posición", style=discord.ButtonStyle.success, emoji="➕")
        edit = discord.ui.Button(label="Editar nombre/icono", style=discord.ButtonStyle.primary, emoji="✏️", disabled=not options)
        access = discord.ui.Button(label="Restringir por roles", style=discord.ButtonStyle.secondary, emoji="🔒", disabled=not options)
        delete = discord.ui.Button(label="Eliminar posición", style=discord.ButtonStyle.danger, emoji="🗑️", disabled=not options)
        preview = discord.ui.Button(label="Actualizar vista", style=discord.ButtonStyle.secondary, emoji="🔄")
        party_access = discord.ui.Button(label="Restringir party", style=discord.ButtonStyle.secondary, emoji="🛡️")
        move = discord.ui.Button(label="Mover / reordenar", style=discord.ButtonStyle.primary, emoji="↕️", disabled=not options)
        add.callback = self.add_slot
        edit.callback = self.edit_slot
        access.callback = self.access_slot
        delete.callback = self.delete_slot
        preview.callback = self.preview
        party_access.callback = self.configure_party_access
        move.callback = self.move_slot
        for item in (add, edit, access, delete, preview, party_access, move):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Este editor pertenece a otro administrador.", ephemeral=True)
            return False
        if not interaction.user.guild_permissions.manage_guild and not any(r.id == STAFF_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
            return False
        return True

    async def select_party(self, interaction: discord.Interaction):
        party = int(interaction.data["values"][0])
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        await interaction.response.edit_message(content=f"**Editor de composición: {fresh['name']} · Party {party}**\nElegí una posición y después una acción.", view=CTACompositionEditorView(fresh, self.actor_id, party_filter=party))

    async def select_slot(self, interaction: discord.Interaction):
        self.selected_slot_id = int(interaction.data["values"][0])
        await interaction.response.send_message(f"Seleccionaste la posición ID {self.selected_slot_id}. Ahora elegí una acción.", ephemeral=True)

    def _current_slot(self):
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        return next((s for s in fresh["slots"] if s["id"] == self.selected_slot_id), None) if fresh else None

    async def add_slot(self, interaction: discord.Interaction):
        party = int(self.party_filter or 1)
        count = db_fetchall("SELECT COUNT(*) AS n FROM cta_slots WHERE composition_id=? AND party_no=?", (self.composition["id"], party))[0]["n"]
        await interaction.response.send_modal(CTAAddSlotModal(self.composition["id"], self.composition["guild_id"], interaction.user.id, party_default=party, position_default=int(count) + 1))

    async def edit_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Seleccioná una posición válida.", ephemeral=True)
        await interaction.response.send_modal(CTAEditSlotModal(self.composition["id"], slot, self.composition["guild_id"], interaction.user.id))

    async def access_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Seleccioná una posición válida.", ephemeral=True)
        await interaction.response.send_message(f"Configurá los roles permitidos para **P{slot['party_no']} · {slot['position_no']} · {slot['role_name']}**. Si no elegís roles, queda abierta para todos.", view=CTAAccessRoleView(self.composition["id"], slot, self.composition["guild_id"], interaction.user.id), ephemeral=True)

    async def move_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Seleccioná una posición válida.", ephemeral=True)
        await interaction.response.send_modal(CTAMoveSlotModal(self.composition, slot, interaction.user.id))

    async def delete_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Seleccioná una posición válida.", ephemeral=True)
        view = CTAConfirmDeleteSlotView(self.composition, slot, interaction.user.id)
        await interaction.response.send_message(f"¿Eliminar **P{slot['party_no']} · {slot['position_no']} — {slot['role_name']}**? Las posiciones posteriores de esa party se renumerarán.", view=view, ephemeral=True)

    async def configure_party_access(self, interaction: discord.Interaction):
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        parties = sorted({int(s["party_no"]) for s in fresh["slots"]}) if fresh else []
        if not parties:
            return await interaction.response.send_message("Agregá al menos una posición antes de configurar una party.", ephemeral=True)
        await interaction.response.send_message("Elegí la party y los roles que pueden anotarse en cualquiera de sus posiciones.", view=CTAPartyAccessView(self.composition, parties, interaction.user.id), ephemeral=True)

    async def preview(self, interaction: discord.Interaction):
        comp = cta_get_composition(self.composition["guild_id"], self.composition["name"])
        if not comp:
            return await interaction.response.send_message("No encontré la composición.", ephemeral=True)
        lines = []
        for s in comp["slots"]:
            roles = "Todos" if s["access_mode"] != "roles" else ", ".join(f"<@&{r}>" for r in s["role_ids"])
            lines.append(f"**P{s['party_no']} · {s['position_no']}** {s['emoji']} {s['role_name']} · Acceso: {roles}")
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3850] + "\n… (vista previa recortada)"
        await interaction.response.send_message(f"**Composición: {comp['name']}**\n{text}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CTAConfirmDeleteSlotView(discord.ui.View):
    def __init__(self, composition: dict, slot: dict, actor_id: int):
        super().__init__(timeout=60)
        self.composition, self.slot, self.actor_id = composition, slot, actor_id
        yes = discord.ui.Button(label="Confirmar eliminación", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        yes.callback = self.confirm
        no.callback = self.cancel
        self.add_item(yes); self.add_item(no)

    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien abrió la confirmación puede usarla.", ephemeral=True)
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM cta_slots WHERE id=? AND composition_id=?", (self.slot["id"], self.composition["id"]))
            rows = conn.execute("SELECT id, party_no FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (self.composition["id"], self.slot["party_no"])).fetchall()
            for new_pos, (sid, _party) in enumerate(rows, start=1):
                conn.execute("UPDATE cta_slots SET position_no=? WHERE id=?", (new_pos, sid))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition["id"]))
            conn.commit()
        cta_audit(self.composition["guild_id"], interaction.user.id, "composition_delete_slot", details=f"slot={self.slot['id']}")
        await interaction.response.edit_message(content="✅ Posición eliminada y numeración actualizada.", view=None)

    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien abrió la confirmación puede usarla.", ephemeral=True)
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


class CTACompositionGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="compo", description="Administrar composiciones de Smogg")

    async def _admin_check(self, interaction: discord.Interaction):
        member = interaction.user
        return isinstance(member, discord.Member) and (member.guild_permissions.manage_guild or any(r.id == STAFF_ROLE_ID for r in member.roles))

    @app_commands.command(name="crear", description="Crear una composición vacía")
    @app_commands.describe(nombre="Nombre único de la composición")
    async def crear(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("Solo administradores u organizadores autorizados pueden crear composiciones.", ephemeral=True)
        name = nombre.strip()
        if not re.fullmatch(r"[\w -]{2,40}", name, re.UNICODE):
            return await interaction.response.send_message("Usá un nombre de 2 a 40 caracteres (letras, números, espacios, guion o guion bajo).", ephemeral=True)
        try:
            db_execute("INSERT INTO cta_compositions(guild_id,name,created_by,updated_at) VALUES (?,?,?,?)", (interaction.guild_id, name, interaction.user.id, cta_now_iso()))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Ya existe una composición con ese nombre.", ephemeral=True)
        cta_audit(interaction.guild_id, interaction.user.id, "composition_create", details=name)
        await interaction.response.send_message(f"✅ Composición **{name}** creada. Usá `/compo editar` para agregar sus posiciones.", ephemeral=True)

    @app_commands.command(name="listar", description="Listar composiciones guardadas")
    async def listar(self, interaction: discord.Interaction):
        rows = db_fetchall("SELECT name FROM cta_compositions WHERE guild_id=? ORDER BY name COLLATE NOCASE", (interaction.guild_id,))
        names = [r["name"] for r in rows]
        await interaction.response.send_message("**Composiciones guardadas**\n" + ("\n".join(f"• {n}" for n in names) if names else "Todavía no hay composiciones."), ephemeral=True)

    @app_commands.command(name="ver", description="Ver una composición guardada")
    async def ver(self, interaction: discord.Interaction, nombre: str):
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición.", ephemeral=True)
        lines = [f"**P{s['party_no']} · {s['position_no']}** {s['emoji']} {s['role_name']}" for s in comp["slots"]]
        body = "\n".join(lines) or "No tiene posiciones todavía."
        await interaction.response.send_message(f"**{comp['name']}**\n{body[:3900]}", ephemeral=True)

    @app_commands.command(name="editar", description="Abrir el editor interactivo de una composición")
    async def editar(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Podés crearla con `/compo crear`.", ephemeral=True)
        await interaction.response.send_message(f"**Editor de composición: {comp['name']}**\nElegí una posición en el menú y después una acción. Los cambios se guardan en la base de datos.", view=CTACompositionEditorView(comp, interaction.user.id), ephemeral=True)

    @app_commands.command(name="duplicar", description="Duplicar una composición")
    async def duplicar(self, interaction: discord.Interaction, nombre: str, nuevo_nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para duplicar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe la composición original.", ephemeral=True)
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                cur = conn.execute("INSERT INTO cta_compositions(guild_id,name,created_by,updated_at) VALUES (?,?,?,?)", (interaction.guild_id, nuevo_nombre.strip(), interaction.user.id, cta_now_iso()))
                new_id = cur.lastrowid
                for s in comp["slots"]:
                    conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,?,?,?,?)", (new_id, s["party_no"], s["position_no"], s["role_name"], s["emoji"], s["access_mode"], json.dumps(s["role_ids"]), s["access_match"]))
                conn.commit()
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("El nuevo nombre ya existe o no es válido.", ephemeral=True)
        cta_audit(interaction.guild_id, interaction.user.id, "composition_duplicate", details=f"{nombre} -> {nuevo_nombre}")
        await interaction.response.send_message(f"✅ **{nombre}** se duplicó como **{nuevo_nombre}**.", ephemeral=True)

    @app_commands.command(name="eliminar", description="Eliminar una composición guardada")
    async def eliminar(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para eliminar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición.", ephemeral=True)
        view = CTAConfirmDeleteCompositionView(comp, interaction.user.id)
        await interaction.response.send_message(f"¿Eliminar **{comp['name']}**? Las CTA ya iniciadas conservarán su copia histórica.", view=view, ephemeral=True)


class CTAConfirmDeleteCompositionView(discord.ui.View):
    def __init__(self, comp: dict, actor_id: int):
        super().__init__(timeout=60); self.comp, self.actor_id = comp, actor_id
        yes = discord.ui.Button(label="Eliminar composición", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        yes.callback = self.confirm; no.callback = self.cancel
        self.add_item(yes); self.add_item(no)
    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien inició la operación puede confirmarla.", ephemeral=True)
        db_execute("DELETE FROM cta_compositions WHERE id=?", (self.comp["id"],))
        cta_audit(self.comp["guild_id"], interaction.user.id, "composition_delete", details=self.comp["name"])
        await interaction.response.edit_message(content="Composición eliminada. Las CTA históricas conservan su copia.", view=None)
    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien inició la operación puede cancelarla.", ephemeral=True)
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


compo_group = CTACompositionGroup()
bot.tree.add_command(compo_group, guild=GUILD_OBJ)


@bot.tree.command(name="cta", description="Publicar una CTA usando una composición guardada", guild=GUILD_OBJ)
@app_commands.describe(nombre="Nombre de la composición", tiempo="Duración HH:MM; ejemplo 00:30 para 30 minutos")
async def cta_command(interaction: discord.Interaction, nombre: str, tiempo: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Este comando solo funciona dentro del servidor.", ephemeral=True)
    member = interaction.user
    if not (member.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in member.roles)):
        return await interaction.response.send_message("No tenés permisos para iniciar una CTA.", ephemeral=True)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", tiempo.strip())
    if not match:
        return await interaction.response.send_message("Formato inválido. Usá HH:MM, por ejemplo `00:30`.", ephemeral=True)
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes > 59 or (hours == 0 and minutes == 0) or hours > 23:
        return await interaction.response.send_message("El tiempo debe ser mayor a cero y no superar 23:59.", ephemeral=True)
    comp = cta_get_composition(interaction.guild_id, nombre)
    if not comp and nombre.casefold() == "brawl":
        cta_ensure_brawl(interaction.guild_id, interaction.user.id)
        comp = cta_get_composition(interaction.guild_id, nombre)
    if not comp:
        return await interaction.response.send_message(f"No existe la composición **{nombre}**. Usá `/compo listar` para ver las guardadas.", ephemeral=True)
    if not comp["slots"]:
        return await interaction.response.send_message("Esta composición no tiene posiciones. Agregalas desde `/compo editar`.", ephemeral=True)
    if len(comp["slots"]) > 250:
        return await interaction.response.send_message("Una CTA no puede superar 250 posiciones en este sistema.", ephemeral=True)
    duration = timedelta(hours=hours, minutes=minutes)
    now = datetime.now(timezone.utc)
    close_at = now + duration
    snapshot = cta_snapshot_from_comp(comp)
    # Las restricciones de party se copian en cada slot. La estructura permite añadirlas al editor de party.
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("INSERT INTO cta_runs(guild_id,composition_id,composition_name,snapshot_json,channel_id,created_by,created_at,closes_at,status) VALUES (?,?,?,?,?,?,?,?, 'open')",
                           (interaction.guild_id, comp["id"], comp["name"], json.dumps(snapshot, ensure_ascii=False), interaction.channel_id, interaction.user.id, now.isoformat(), close_at.isoformat()))
        run_id = cur.lastrowid
        conn.commit()
    run = cta_run_get(run_id)
    try:
        await interaction.response.send_message(embed=cta_build_embed(run, interaction.guild), view=CTAPanelView(run_id=run_id), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        message = await interaction.original_response()
        db_execute("UPDATE cta_runs SET message_id=? WHERE id=?", (message.id, run_id))
        try:
            thread = await message.create_thread(name=f"CTA {comp['name']} #{run_id}"[:100], auto_archive_duration=1440)
            db_execute("UPDATE cta_runs SET thread_id=? WHERE id=?", (thread.id, run_id))
            await thread.send(f"Hilo de coordinación para la CTA **{comp['name']}**. Las inscripciones se gestionan desde el panel principal.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"CTA #{run_id}: no se pudo crear el hilo: {exc}")
        cta_audit(interaction.guild_id, interaction.user.id, "cta_created", run_id, f"composition={comp['name']}; closes={close_at.isoformat()}")
    except Exception as exc:
        print(f"Error publicando CTA #{run_id}: {exc}")
        db_execute("UPDATE cta_runs SET status='error' WHERE id=?", (run_id,))
        if not interaction.response.is_done():
            await interaction.response.send_message("No pude publicar el panel, pero guardé el registro para poder diagnosticarlo. Revisá los permisos del canal.", ephemeral=True)
        return


@bot.tree.command(name="cta_activas", description="Consultar las CTA activas", guild=GUILD_OBJ)
async def cta_activas(interaction: discord.Interaction):
    rows = db_fetchall("SELECT id, composition_name, closes_at, channel_id FROM cta_runs WHERE guild_id=? AND status='open' ORDER BY closes_at", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message("No hay CTA activas.", ephemeral=True)
    lines = []
    for r in rows:
        closes = datetime.fromisoformat(r["closes_at"])
        lines.append(f"• CTA **#{r['id']} — {r['composition_name']}** · cierre <t:{int(closes.timestamp())}:R> · <#{r['channel_id']}>")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="cta_cerrar", description="Cerrar anticipadamente una CTA", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cta_id="Identificador numérico de la CTA")
async def cta_cerrar(interaction: discord.Interaction, cta_id: int):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para cerrar CTA.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("No encontré una CTA abierta con ese ID.", ephemeral=True)
    await cta_close_run(cta_id, reason=f"closed_by:{interaction.user.id}")
    await interaction.response.send_message(f"CTA **#{cta_id}** cerrada. El panel se conserva publicado.", ephemeral=True)


@bot.tree.command(name="cta_asignar", description="Asignar manualmente un jugador a una plaza", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cta_id="ID de CTA", miembro="Jugador", party="Número de party", posicion="Posición dentro de la party", excepcion="Permitir asignación aunque no cumpla roles", motivo="Motivo si se autoriza una excepción")
async def cta_asignar(interaction: discord.Interaction, cta_id: int, miembro: discord.Member, party: app_commands.Range[int, 1, 50], posicion: app_commands.Range[int, 1, 250], excepcion: bool = False, motivo: Optional[str] = None):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para administrar plazas.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("CTA inexistente o cerrada.", ephemeral=True)
    snapshot = json.loads(run["snapshot_json"])
    slot = next((s for s in snapshot["slots"] if int(s["party_no"]) == party and int(s["position_no"]) == posicion), None)
    if not slot:
        return await interaction.response.send_message("Esa posición no existe en la CTA.", ephemeral=True)
    error = cta_access_error(miembro, slot, interaction.guild)
    if error and not excepcion:
        return await interaction.response.send_message(error + " Para ignorar la restricción, marcá `excepcion` y proporcioná un motivo.", ephemeral=True)
    if excepcion and not (motivo and motivo.strip()):
        return await interaction.response.send_message("Para una excepción necesitás indicar el motivo.", ephemeral=True)
    try:
        with sqlite3.connect(DB_PATH, timeout=30, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO cta_signups(cta_id,slot_key,user_id,assigned_by,override_reason,signed_at) VALUES (?,?,?,?,?,?)", (cta_id, cta_slot_key(slot), miembro.id, interaction.user.id, motivo.strip() if excepcion and motivo else None, cta_now_iso()))
            conn.commit()
    except sqlite3.IntegrityError:
        return await interaction.response.send_message("El jugador ya está anotado o la plaza ya está ocupada.", ephemeral=True)
    cta_audit(interaction.guild_id, interaction.user.id, "manual_assign_override" if excepcion else "manual_assign", cta_id, f"member={miembro.id}; slot={cta_slot_key(slot)}; reason={motivo or ''}")
    await cta_refresh_panel(bot, cta_id)
    await interaction.response.send_message(f"✅ {miembro.mention} asignado a Party {party}, posición {posicion}.", ephemeral=True)


@bot.tree.command(name="cta_liberar", description="Liberar la plaza de un jugador en una CTA", guild=discord.Object(id=GUILD_ID))
async def cta_liberar(interaction: discord.Interaction, cta_id: int, miembro: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para liberar plazas.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("CTA inexistente o cerrada.", ephemeral=True)
    rows = db_fetchall("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?", (cta_id, miembro.id))
    if not rows:
        return await interaction.response.send_message("Ese jugador no está anotado en esta CTA.", ephemeral=True)
    db_execute("DELETE FROM cta_signups WHERE cta_id=? AND user_id=?", (cta_id, miembro.id))
    cta_audit(interaction.guild_id, interaction.user.id, "admin_release", cta_id, f"member={miembro.id}; slot={rows[0]['slot_key']}")
    await cta_refresh_panel(bot, cta_id)
    await interaction.response.send_message(f"Plaza de {miembro.mention} liberada.", ephemeral=True)


@bot.tree.command(name="cta_historial", description="Consultar las últimas CTA cerradas", guild=discord.Object(id=GUILD_ID))
async def cta_historial(interaction: discord.Interaction):
    rows = db_fetchall("SELECT id, composition_name, created_at, closed_at, status FROM cta_runs WHERE guild_id=? AND status IN ('closed','error') ORDER BY id DESC LIMIT 10", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message("Todavía no hay CTA cerradas en el historial.", ephemeral=True)
    lines = []
    for r in rows:
        created = datetime.fromisoformat(r["created_at"])
        lines.append(f"• **#{r['id']} {r['composition_name']}** · iniciada <t:{int(created.timestamp())}:R> · `{r['status']}`")
    await interaction.response.send_message("**Últimas CTA**\n" + "\n".join(lines), ephemeral=True)


@tasks.loop(seconds=30)
async def cta_housekeeping():
    rows = db_fetchall("SELECT id, closes_at FROM cta_runs WHERE status='open'")
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            close_at = datetime.fromisoformat(row["closes_at"])
            if close_at.tzinfo is None:
                close_at = close_at.replace(tzinfo=timezone.utc)
            if close_at <= now:
                await cta_close_run(int(row["id"]), reason="timeout")
            else:
                # El panel se actualiza de forma periódica para reflejar la cuenta regresiva.
                await cta_refresh_panel(bot, int(row["id"]))
        except Exception as exc:
            print(f"Error en housekeeping CTA #{row['id']}: {exc}")


@cta_housekeeping.before_loop
async def before_cta_housekeeping():
    await bot.wait_until_ready()


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Si pierde un rol exigido por una posición ya ocupada, avisar al organizador; no expulsarlo automáticamente.
    if before.roles == after.roles or not after.guild:
        return
    rows = db_fetchall("SELECT s.cta_id, s.slot_key, r.created_by, r.channel_id, r.thread_id, r.snapshot_json FROM cta_signups s JOIN cta_runs r ON r.id=s.cta_id WHERE s.user_id=? AND r.guild_id=? AND r.status='open'", (after.id, after.guild.id))
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"])
            slot = next((x for x in snapshot["slots"] if cta_slot_key(x) == row["slot_key"]), None)
            if not slot or not cta_access_error(after, slot, after.guild):
                continue
            target = after.guild.get_channel(int(row["thread_id"])) if row["thread_id"] else None
            if target is None:
                target = after.guild.get_channel(int(row["channel_id"]))
            if target:
                await target.send(f"<@{row['created_by']}> ⚠️ {after.mention} ya no cumple la restricción de roles de **Party {slot['party_no']} · {slot['position_no']} — {slot['role_name']}** en la CTA #{row['cta_id']}. No se modificó su plaza automáticamente.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception as exc:
            print(f"Error verificando cambio de roles para CTA #{row['cta_id']}: {exc}")


# ---------- READY (AL FINAL, así PanelView existe) ----------
@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")

    # Views persistentes (1 vez)
    if not hasattr(bot, "_views_registered"):
        bot.add_view(PanelView())
        bot.add_view(FocoPanelView())
        bot.add_view(FocoTicketActionView())
        bot.add_view(RecruitView())
        bot.add_view(GiveawayJoinView())
        bot.add_view(SplitSoldView())
        bot.add_view(TimerDeleteView())
        bot.add_view(CTAPanelView())
        bot._views_registered = True
        print("✅ Views persistentes registradas")

    # Restaurar datos locales una sola vez por proceso. Los sorteos y sus anotados
    # se cargan antes de volver a programar sus finales.
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            cta_ensure_brawl(guild.id, bot.user.id if bot.user else 0)
        except Exception as exc:
            print(f"❌ No se pudo inicializar la composición Brawl: {exc}")
    if guild and not getattr(bot, "_persistent_state_loaded", False):
        try:
            restore_persistent_state(guild)
            bot._persistent_state_loaded = True
            print(f"✅ SQLite restaurado: {len(giveaways)} sorteos, {len(timers)} timers y {len(ticket_images)} imágenes de postulación")
        except Exception as exc:
            print(f"❌ Error restaurando SQLite: {exc}")

    # Reanudar también en reconexiones: si un intento anterior no pudo acceder al canal,
    # el registro sigue en SQLite y se vuelve a programar.
    if guild:
        for g in list(giveaways.values()):
            start_giveaway_task(guild.id, g)

    # CTA: recuperar paneles persistentes y reanudar cierres después de reinicios.
    if not cta_housekeeping.is_running():
        cta_housekeeping.start()
        print("✅ CTA housekeeping iniciado")
    if not getattr(bot, "_cta_views_restored", False):
        for cta_row in db_fetchall("SELECT id, status FROM cta_runs WHERE message_id IS NOT NULL"):
            try:
                bot.add_view(CTAPanelView(run_id=int(cta_row["id"]), closed=cta_row["status"] != "open"), message_id=int(db_fetchall("SELECT message_id FROM cta_runs WHERE id=?", (cta_row["id"],))[0]["message_id"]))
            except Exception as exc:
                print(f"CTA #{cta_row['id']}: no se pudo restaurar la vista: {exc}")
        bot._cta_views_restored = True

    # Tasks
    if not timer_posts_cleanup.is_running():
        timer_posts_cleanup.start()
        print("✅ Timer posts cleanup iniciado")

    if not timers_housekeeping.is_running():
        timers_housekeeping.start()
        print("✅ Timers housekeeping iniciado")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        await ensure_timers_board_message(guild)
        await update_timers_board(guild)

# ✅ Ignorar comandos desconocidos (!bal, etc.)
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if ct.startswith("image/"):
        return True
    # fallback por extensión, por si content_type viene vacío
    name = (att.filename or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))

def _get_recruit_uid_from_channel(ch: discord.abc.GuildChannel) -> Optional[int]:
    if not isinstance(ch, discord.TextChannel):
        return None
    info = parse_recruit_topic(ch.topic)
    uid = info.get("uid")
    if not uid:
        return None
    try:
        return int(uid)
    except ValueError:
        return None

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.guild is None or message.author.bot:
        return

    # ✅ RECRUIT: guardar screenshot aunque el bot haya reiniciado (por topic)
    recruit_uid = _get_recruit_uid_from_channel(message.channel)
    if recruit_uid is not None:
        # Solo guardamos si el que manda la imagen es el postulante del ticket
        if message.author.id != recruit_uid:
            return

        if not message.attachments:
            return

        img = None
        for att in message.attachments:
            if _is_image_attachment(att):
                img = att
                break
        if img is None:
            return

        save_ticket_image(message.author.id, img.url, img.filename or "image.png")
        return

    # (Opcional) Si querés conservar tu modo viejo para tickets recién creados:
    opener_channel_id = active_applications.get(message.author.id)
    if opener_channel_id is None or opener_channel_id != message.channel.id:
        return

    if not message.attachments:
        return

    img = None
    for att in message.attachments:
        if _is_image_attachment(att):
            img = att
            break
    if img is None:
        return

    ticket_images[message.author.id] = {
        "url": img.url,
        "filename": img.filename or "image.png",
    }
# ---------- RUN ----------
bot.run(TOKEN)




