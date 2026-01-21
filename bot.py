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

CLOCK_CHANNEL_ID = 1462464849463214395

COOLDOWN_SECONDS = 60

# ✅ Cooldown reloj 15 min (aunque el task corra cada 5)
CLOCK_COOLDOWN_SECONDS = 15 * 60
_last_clock_edit_ts = 0.0

# ✅ Timers
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

# ================== SLASH COMMANDS TIMERS ==================
@bot.tree.command(name="timeradd", description="Agregar timer (solo rol Timers)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(material="Material", tier="Tier", mapa="Nombre del mapa", tiempo="Tiempo (H:M) ej 6:10")
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
    ],
)
async def timeradd_slash(interaction: discord.Interaction, material: app_commands.Choice[str], tier: app_commands.Choice[str], mapa: str, tiempo: str):
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
    end_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=m)

    item = TimerItem(material=material.value, tier=tier.value, map_name=mapa.strip(), end_at=end_at, created_by_id=interaction.user.id)
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
            f"**{i}.** 🧱 {t.material.title()} | ⭐ T{t.tier} | 🗺️ {t.map_name} → 🕒 **{fmt_utc(t.end_at)}** (en {time_left_str(t.end_at)})"
        )

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…"

    await respond_ephemeral(interaction, msg)

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
    def __init__(self, user: discord.Member):
        super().__init__(timeout=None)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        recruiter_role = interaction.guild.get_role(RECRUITER_ROLE_ID)
        if recruiter_role not in interaction.user.roles:
            await respond_ephemeral(interaction, "❌ Solo reclutadores pueden usar estos botones.")
            return False
        return True

    async def accept_player(self, interaction: discord.Interaction, role_id: int, role_name: str):
        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
        public_role = interaction.guild.get_role(PUBLIC_ROLE_ID)

        if role is None or member_role is None:
            try:
                await interaction.channel.send("❌ Error: no encontré uno de los roles configurados (IDs mal).")
            except Exception:
                pass
            return

        try:
            await self.user.add_roles(member_role, role, reason=f"Aceptado como {role_name}")
            if public_role and public_role in self.user.roles:
                await self.user.remove_roles(public_role, reason="Aceptado: se quita rol Public")
        except discord.Forbidden:
            try:
                await interaction.channel.send(
                    "❌ No tengo permisos para asignar/quitar roles.\n"
                    "✅ Revisá: permisos del bot y que los roles estén *debajo* del rol del bot."
                )
            except Exception:
                pass
            return
        except Exception:
            try:
                await interaction.channel.send("❌ Error inesperado asignando roles.")
            except Exception:
                pass
            return

        try:
            await interaction.channel.send(f"✅ {self.user.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️")
        except Exception:
            pass

        await send_log(interaction.guild, f"✅ **ACEPTADO** {self.user} → {role_name} (Public removido)")

        active_applications.pop(self.user.id, None)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    @discord.ui.button(label="✔ Miembro", style=discord.ButtonStyle.success, custom_id="accept_miembro")
    async def miembro(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, MIEMBRO_ROLE_ID, "Miembro")

    @discord.ui.button(label="🛡 Tank", style=discord.ButtonStyle.primary, custom_id="accept_tank")
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, TANK_ROLE_ID, "Tank")

    @discord.ui.button(label="✨ Healer", style=discord.ButtonStyle.primary, custom_id="accept_healer")
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, HEALER_ROLE_ID, "Healer")

    @discord.ui.button(label="🧙 Support", style=discord.ButtonStyle.primary, custom_id="accept_support")
    async def supp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, SUPP_ROLE_ID, "Support")

    @discord.ui.button(label="⚔ DPS", style=discord.ButtonStyle.primary, custom_id="accept_dps")
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, DPS_ROLE_ID, "DPS")

    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.secondary, custom_id="reject_postulacion")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        try:
            await self.user.send(
                "❌ Tu postulación en **Dies-Irae** fue rechazada.\n"
                "Podés volver a aplicar más adelante."
            )
        except Exception:
            pass

        await send_log(interaction.guild, f"❌ **RECHAZADO** {self.user}")

        active_applications.pop(self.user.id, None)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    @discord.ui.button(label="🔒 Cerrar Postulación", style=discord.ButtonStyle.danger, custom_id="close_postulacion")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        transcript = await create_transcript(interaction.channel)

        await send_log(
            interaction.guild,
            f"🔒 **POSTULACIÓN CERRADA** {self.user}\n```\n{transcript[:1800]}\n```"
        )

        active_applications.pop(self.user.id, None)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

# ---------- PANEL VIEW ----------
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

        channel_name = f"postulacion-{interaction.user.name}-{interaction.user.id}"
        bot_member = guild.get_member(bot.user.id) if bot.user else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            recruiter_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites
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
            view=RecruitView(interaction.user)
        )

        await send_log(guild, f"📥 **NUEVA POSTULACIÓN** {interaction.user} → {channel.mention}")
        await interaction.followup.send(f"✅ Postulación creada: {channel.mention}", ephemeral=True)

# ---------- COMANDO PANEL ----------
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

# ---------- READY (AL FINAL, así PanelView existe) ----------
@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")

    # Panel persistente (views con timeout=None)
    bot.add_view(PanelView())
    print("✅ Panel persistente cargado")

    if not utc_clock.is_running():
        utc_clock.start()
        print("✅ Reloj UTC iniciado (loop 5m, cooldown 15m)")

    if not timers_housekeeping.is_running():
        timers_housekeeping.start()
        print("✅ Timers housekeeping iniciado")

    # Sync slash commands en el guild
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Slash commands sincronizados (guild)")
    except Exception as e:
        print("❌ Error sync slash commands:", e)

# ✅ Ignorar comandos desconocidos (!bal, etc.)
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

# ---------- RUN ----------
bot.run(TOKEN)
