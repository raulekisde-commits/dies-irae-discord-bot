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
    env_path = "/root/discordbot/.env"
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

CLOCK_CHANNEL_ID = 1462464849463214395

COOLDOWN_SECONDS = 60

# ✅ Cooldown reloj 15 min (aunque el task corra cada 5)
CLOCK_COOLDOWN_SECONDS = 15 * 60
_last_clock_edit_ts = 0.0

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
    emb.set_footer(text="Dies-Irae Sorteos System")
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

# ---------- RELOJ UTC ----------
@tasks.loop(minutes=5)
async def utc_clock():
    global _last_clock_edit_ts

    if not CLOCK_CHANNEL_ID or CLOCK_CHANNEL_ID == 0:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(CLOCK_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(CLOCK_CHANNEL_ID)
        except Exception:
            return

    now = datetime.now(timezone.utc)
    new_name = f"🕒 UTC {now:%H:%M}"

    if getattr(channel, "name", None) == new_name:
        return

    now_ts = time.time()
    if now_ts - _last_clock_edit_ts < CLOCK_COOLDOWN_SECONDS:
        return

    try:
        await channel.edit(name=new_name, reason="UTC clock update (15m cooldown)")
        _last_clock_edit_ts = now_ts
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            _last_clock_edit_ts = now_ts
        print("❌ HTTPException editando canal:", e)
    except discord.Forbidden:
        print("❌ No tengo permisos para editar el canal (Manage Channels).")
    except Exception as e:
        print("❌ Error editando canal:", e)

@utc_clock.before_loop
async def before_utc_clock():
    await bot.wait_until_ready()

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

    item = TimerItem(material=material.value, tier=tier.value, map_name=mapa.strip(), end_at=end_at, created_by_id=interaction.user.id)
    
    # actualizar tablero
    timers.append(item)
    await update_timers_board(interaction.guild)

    tier_txt = TIER_LABELS_SPECIAL.get(item.tier, f"T{item.tier}")
    await respond_ephemeral(
        interaction,
        f"✅ Timer creado: 🧱 **{item.material.title()}** | ⭐ **{tier_txt}** | 🗺️ **{item.map_name}**\n"
        f"🕒 Sale a **{fmt_utc(item.end_at)}** (en {time_left_str(item.end_at)})"
    )

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

@bot.tree.command(name="timerslist", description="Listar timers (ordenados)", guild=discord.Object(id=GUILD_ID))
async def timerslist_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not timers:
        return await respond_ephemeral(interaction, "📭 No hay timers activos.")

    sorted_timers = sorted(timers, key=lambda t: t.end_at)
    lines = []
    for i, t in enumerate(sorted_timers, start=1):
        tier_txt = TIER_LABELS_SPECIAL.get(t.tier, f"T{t.tier}")
        lines.append(
            f"**{i}.** 🧱 {t.material.title()} | ⭐ {tier_txt} | 🗺️ {t.map_name} → 🕒 **{fmt_utc(t.end_at)}** (en {time_left_str(t.end_at)})"
        )

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…"

    await respond_ephemeral(interaction, msg)

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

@bot.command(name="delrole")
@staff_only()
@commands.guild_only()
async def delrole(ctx: commands.Context, *, role_query: str = None):
    if not role_query and not ctx.message.role_mentions:
        return await ctx.reply("Uso: `!delrole NombreDelRol` o `!delrole @Rol`")

    if ctx.message.role_mentions:
        role = ctx.message.role_mentions[0]
    else:
        role = discord.utils.get(ctx.guild.roles, name=role_query.strip()) if role_query else None

    if role is None:
        return await ctx.reply("❌ No encontré ese rol.")

    if role.id == STAFF_ROLE_ID or role.managed:
        return await ctx.reply("❌ No se puede eliminar ese rol.")

    try:
        name = role.name
        await role.delete(reason=f"Eliminado por {ctx.author} (Staff)")
        await ctx.reply(f"🗑️ Rol **{name}** eliminado correctamente.")
    except discord.Forbidden:
        await ctx.reply("❌ No pude eliminar el rol. Me falta permiso **Manage Roles** o jerarquía.")
    except Exception:
        await ctx.reply("❌ Error inesperado eliminando el rol.")

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
            await interaction.channel.send(f"✅ {member.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️")
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
                await member.send("❌ Tu postulación en **Dies-Irae** fue rechazada. Podés volver a aplicar más adelante.")
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
        embed.set_footer(text="Dies-Irae Foco Donor System")

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
            title="⚔️ Reclutamiento Dies-Irae",
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
        title="⚔️ Dies-Irae Reclutamiento",
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
    embed.set_footer(text="Dies-Irae Foco Donor System")
    await ctx.send(embed=embed, view=FocoPanelView())

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
        bot._views_registered = True
        print("✅ Views persistentes registradas")

    # Tasks
    if not utc_clock.is_running():
        utc_clock.start()
        print("✅ Reloj UTC iniciado")

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



