# ================== BOT ==================
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

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

PUBLIC_ROLE_ID = 1266805315547041902
STAFF_ROLE_ID = 1257896709246423083

# ✅ RELOJ UTC (canal de VOZ)
CLOCK_CHANNEL_ID = 1462464849463214395

# ✅ Cooldown general panel
COOLDOWN_SECONDS = 60

# ✅ Cooldown reloj (15 minutos)
CLOCK_COOLDOWN_SECONDS = 15 * 60
_last_clock_edit_ts = 0.0

# ✅ Timers system
TIMERS_ROLE_ID = 1462515835326169159
TIMERS_ROLE_NAME_FALLBACK = "timers"

TIMER_ALERT_CHANNEL_ID = 1462184630835740732
TIMER_ALERT_MINUTES_BEFORE = 30

active_applications = {}
cooldowns = {}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================== TIMERS DATA ==================

ALLOWED_MATERIALS = {"fibra", "cuero", "mineral", "madera"}
ALLOWED_TIERS = {"4.4", "5.4", "6.4", "7.4", "8.4"}

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

# ✅ helper para responder interacciones sin 40060
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

# ---------- RELOJ UTC (cada 5 min, pero edita cada 15) ----------

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

    # si no cambió, no toques nada
    if getattr(channel, "name", None) == new_name:
        return

    # ✅ COOLDOWN 15 MIN
    now_ts = time.time()
    if now_ts - _last_clock_edit_ts < CLOCK_COOLDOWN_SECONDS:
        return

    try:
        await channel.edit(name=new_name, reason="UTC clock update (15m cooldown)")
        _last_clock_edit_ts = now_ts
    except discord.HTTPException as e:
        # ✅ si Discord te rate-limitea, también frenamos 15 min
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

# ---------- TIMERS HOUSEKEEPING (cada 30s) ----------

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

    for t in timers:
        if t.warned_30:
            continue
        seconds_left = (t.end_at - now).total_seconds()
        if 0 < seconds_left <= (TIMER_ALERT_MINUTES_BEFORE * 60):
            t.warned_30 = True
            msg = (
                f"⏰ **Faltan {TIMER_ALERT_MINUTES_BEFORE} min**\n"
                f"🧱 **{t.material.title()}** | ⭐ **T{t.tier}** | 🗺️ **{t.map_name}**\n"
                f"🕒 Sale a **{fmt_utc(t.end_at)}**"
            )
            try:
                await channel.send(msg)
            except Exception:
                pass

@timers_housekeeping.before_loop
async def before_timers_housekeeping():
    await bot.wait_until_ready()

# ---------- READY ----------

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    bot.add_view(PanelView())
    print("✅ Panel persistente cargado")

    if not utc_clock.is_running():
        utc_clock.start()
        print("✅ Reloj UTC iniciado (loop 5m, cooldown 15m)")

    if not timers_housekeeping.is_running():
        timers_housekeeping.start()
        print("✅ Timers housekeeping iniciado")

    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Slash commands sincronizados (guild)")
    except Exception as e:
        print("❌ Error sync slash commands:", e)

# ✅ ignorar comandos desconocidos (ej: !bal)
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

# ================== SLASH COMMANDS TIMERS ==================

@bot.tree.command(name="timeradd", description="Agregar timer (solo rol Timers)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    material="Material",
    tier="Tier",
    mapa="Nombre del mapa",
    tiempo="Tiempo (H:M) ej 6:10"
)
@app_commands.choices(
    material=[
        app_commands.Choice(name="Fibra", value="fibra"),
        app_commands.Choice(name="Cuero", value="cuero"),
        app_commands.Choice(name="Mineral", value="mineral"),
        app_commands.Choice(name="Madera", value="madera"),
    ],
    tier=[
        app_commands.Choice(name="4.4", value="4.4"),
        app_commands.Choice(name="5.4", value="5.4"),
        app_commands.Choice(name="6.4", value="6.4"),
        app_commands.Choice(name="7.4", value="7.4"),
        app_commands.Choice(name="8.4", value="8.4"),
    ]
)
async def timeradd_slash(
    interaction: discord.Interaction,
    material: app_commands.Choice[str],
    tier: app_commands.Choice[str],
    mapa: str,
    tiempo: str
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not is_timers_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el rol **timers** puede usar este comando.")

    if not mapa.strip():
        return await respond_ephemeral(interaction, "❌ El nombre del mapa no puede estar vacío.")

    dur = parse_duration_hhmm(tiempo.strip())
    if dur is None:
        return await respond_ephemeral(interaction, '❌ Tiempo inválido. Usá `H:M` ej: `6:10`.')

    h, m = dur
    now = datetime.now(timezone.utc)
    end_at = now + timedelta(hours=h, minutes=m)

    item = TimerItem(
        material=material.value,
        tier=tier.value,
        map_name=mapa.strip(),
        end_at=end_at,
        created_by_id=interaction.user.id
    )
    timers.append(item)

    await respond_ephemeral(
        interaction,
        f"✅ Timer creado: 🧱 **{item.material.title()}** | ⭐ **T{item.tier}** | 🗺️ **{item.map_name}**\n"
        f"🕒 Sale a **{fmt_utc(item.end_at)}** (en {time_left_str(item.end_at)})"
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
        lines.append(
            f"**{i}.** 🧱 {t.material.title()} | ⭐ T{t.tier} | 🗺️ {t.map_name} "
            f"→ 🕒 **{fmt_utc(t.end_at)}** (en {time_left_str(t.end_at)})"
        )

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…"

    await respond_ephemeral(interaction, msg)

# ---------- COMANDOS STAFF ----------
# (El resto de tu código sigue igual acá abajo: addroll-list, delrole, RecruitView, PanelView, panel, etc.)
# Pegá tus clases y comandos tal cual los tenías.

# ---------- RUN ----------
bot.run(TOKEN)
