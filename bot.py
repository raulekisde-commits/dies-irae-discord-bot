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

# ================== TOKEN (env o /root/discordbot/.env) ==================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    env_path = "token.env"
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno o en /root/discordbot/.env")

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
        giveaways.pop(g.message_id, None)
        return

    # Deshabilitar botón
    await disable_giveaway_button(guild, g)

    ch = guild.get_channel(g.channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(g.channel_id)
        except Exception:
            giveaways.pop(g.message_id, None)
            return

    if not isinstance(ch, discord.TextChannel):
        giveaways.pop(g.message_id, None)
        return

    # Tomar participantes de forma segura
    async with g.lock:
        pool = list(g.entrants)

    if not pool:
        try:
            await ch.send("📭 No se anotó nadie. Sorteo cancelado por falta de calle.")
        except Exception:
            pass
        giveaways.pop(g.message_id, None)
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
        giveaways.pop(g.message_id, None)
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
        giveaways.pop(g.message_id, None)
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
        giveaways.pop(g.message_id, None)
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

    giveaways.pop(g.message_id, None)


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
_TIMER_POST_TS_RE = re.compile(r"^Material:.*\nTier:.*\nMapa:.*\nTiempo:.*\nHorario: <t:(\d+):t>", re.DOTALL)


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
            match = _TIMER_POST_TS_RE.match(msg.content or "")
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
@app_commands.describe(material="Material", tier="Tier", mapa="Nombre del mapa", tiempo="Tiempo (H:M) ej 6:10")
@app_commands.choices(
    material=[
        app_commands.Choice(name="Fibra", value="fibra"),
        app_commands.Choice(name="Cuero", value="cuero"),
        app_commands.Choice(name="Mineral", value="mineral"),
        app_commands.Choice(name="Madera", value="madera"),
        app_commands.Choice(name="Vortex", value="vortex"),
        app_commands.Choice(name="Core", value="core"),
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
async def timeradd_slash(interaction: discord.Interaction, material: app_commands.Choice[str], tier: app_commands.Choice[str], mapa: str, tiempo: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not is_timers_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el rol **timers** puede usar este comando.")

    if not mapa.strip():
        return await respond_ephemeral(interaction, "❌ El nombre del mapa no puede estar vacío.")

    mat = material.value
    tr = tier.value

    # Validación material/tier
    if mat in MATERIALS_SPECIAL:
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

    
    dur = parse_duration_hhmm(tiempo.strip())
    if dur is None:
        return await respond_ephemeral(interaction, '❌ Tiempo inválido. Usá `H:M` ej: `6:10`.')

    h, m = dur
    end_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=m)
    end_unix = int(end_at.timestamp())

    await interaction.response.defer(ephemeral=True)

    channel = interaction.guild.get_channel(TIMER_POST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(TIMER_POST_CHANNEL_ID)
        except Exception:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de timers.", ephemeral=True)

    tier_txt = TIER_LABELS_SPECIAL.get(tr, tr)
    content = (
        f"Material: {mat.title()}\n"
        f"Tier: {tier_txt}\n"
        f"Mapa: {mapa.strip()}\n"
        f"Tiempo: {end_at.strftime('%H:%M')} UTC\n"
        f"Horario: <t:{end_unix}:t>\n"
        f"Falta: <t:{end_unix}:R>\n"
        f"Timeado por: {interaction.user.mention}"
    )

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

    # Arrancar flujo en background
    asyncio.create_task(run_giveaway_flow(GUILD_ID, temp_g))

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
        ticket_images.pop(member.id, None)

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
            ticket_images.pop(member.id, None)

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
            ticket_images.pop(member.id, None)

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
                ticket_images.pop(int(uid), None)

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
                "🕒 Horarios\n\n"
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
SPLIT_REPARTIR_TAX = 5.0

split_log_ids: dict[int, int] = {}   # panel_message_id -> log_message_id
split_locks: dict[int, asyncio.Lock] = {}


def fmt_num(n: float) -> str:
    """1234567.0 -> '1,234,567' | 15.5 -> '15.5'"""
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def build_split_log(
    panel_url: str, city: str, tab: str, creator_id: int,
    total: float, reparacion: float, pct: float, bolsitas: float,
    precio_venta: float, repartir: float,
) -> str:
    return (
        f"Link al mensaje: {panel_url}\n"
        f"City: {city}\n"
        f"Nombre tab: {tab}\n"
        f"Creador de la tab: <@{creator_id}>\n"
        f"Vendedor: \n"
        f"Total: {fmt_num(total)}\n"
        f"Reparacion: {fmt_num(reparacion)}\n"
        f"Tax venta: {fmt_num(pct)}%\n"
        f"Bolsitas: {fmt_num(bolsitas)}\n"
        f"Precio de venta: {fmt_num(precio_venta)}\n"
        f"Total a repartir (-{fmt_num(SPLIT_REPARTIR_TAX)}%): {fmt_num(repartir)}"
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
                split_log_ids[panel_msg.id] = m.id
                return m
    except Exception:
        pass
    return None


class SplitConfirmView(discord.ui.View):
    def __init__(self, panel_msg: discord.Message, user: discord.Member):
        super().__init__(timeout=60)
        self.panel_msg = panel_msg
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await respond_ephemeral(interaction, "❌ Esta confirmación no es tuya.")
            return False
        return True

    @discord.ui.button(label="✅ Sí, lo vendí", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        lock = split_locks.setdefault(self.panel_msg.id, asyncio.Lock())

        async with lock:
            # Releer el panel para saber si alguien ya lo vendió
            try:
                panel = await self.panel_msg.channel.fetch_message(self.panel_msg.id)
            except Exception:
                return await interaction.response.edit_message(content="❌ No pude leer el panel.", view=None)

            if "Vendido por" in (panel.content or ""):
                return await interaction.response.edit_message(content="❌ Este tab ya fue marcado como vendido.", view=None)

            log_msg = await find_split_log_message(guild, panel)
            if log_msg is None:
                return await interaction.response.edit_message(content="❌ No encontré el registro de este tab en el canal de logs.", view=None)

            # Editar log: completar Vendedor
            new_content = re.sub(
                r"^Vendedor:.*$",
                f"Vendedor: {interaction.user.mention}",
                log_msg.content,
                count=1,
                flags=re.MULTILINE,
            )
            try:
                await log_msg.edit(content=new_content, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                return await interaction.response.edit_message(content="❌ No pude editar el registro.", view=None)

            # Editar panel: marcar vendido y deshabilitar botón
            try:
                await panel.edit(
                    content=f"{panel.content}\n\n✅ **Vendido por** {interaction.user.display_name}",
                    view=SplitSoldView(disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

        self.stop()
        await interaction.response.edit_message(content="✅ Registrado como vendido.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelado.", view=None)


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

        await interaction.response.send_message(
            "⚠️ ¿Estás seguro de que vendiste este tab? Esta acción no se puede deshacer.",
            view=SplitConfirmView(msg, interaction.user),
            ephemeral=True,
        )


@bot.tree.command(
    name="calcularsplit",
    description="Calcula el precio de venta de un tab y crea el panel.",
    guild=GUILD_OBJ,
)
@app_commands.describe(
    nombre_tab="Nombre del tab",
    city="Ciudad donde se vende",
    total="Total",
    reparacion="Costo de reparación",
    silver_bolsitas="Silver de bolsitas (opcional)",
    descuento="Porcentaje de descuento/tax de venta (opcional, default 15)",
)
async def calcularsplit(
    interaction: discord.Interaction,
    nombre_tab: str,
    city: str,
    total: float,
    reparacion: float,
    silver_bolsitas: Optional[float] = None,
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
    bolsitas = silver_bolsitas or 0
    pct = SPLIT_DEFAULT_DISCOUNT if descuento is None else descuento

    precio_venta = round((total - reparacion) * (1 - pct / 100))
    repartir = round((precio_venta + bolsitas) * (1 - SPLIT_REPARTIR_TAX / 100))

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
        total, reparacion, pct, bolsitas, precio_venta, repartir,
    )
    try:
        log_msg = await log_ch.send(log_text, allowed_mentions=discord.AllowedMentions.none())
        split_log_ids[panel_msg.id] = log_msg.id
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
        f"Precio de venta (-{fmt_num(pct)}%): **{fmt_num(precio_venta)}**\n"
        f"Total a repartir (-{fmt_num(SPLIT_REPARTIR_TAX)}%): **{fmt_num(repartir)}**"
        f"{thread_warning}",
        ephemeral=True,
    )


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
        bot._views_registered = True
        print("✅ Views persistentes registradas")

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

        ticket_images[message.author.id] = {
            "url": img.url,
            "filename": img.filename or "image.png",
        }
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




