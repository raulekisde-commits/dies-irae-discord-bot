# ================== WEB PING (UptimeRobot) ==================
from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_web, daemon=True).start()

# ================== BOT ==================
import discord
from discord.ext import commands, tasks
import os
import time
from datetime import datetime, timezone

# ================== CONFIG ==================

TOKEN = os.getenv("DISCORD_TOKEN")  # Replit Secrets: DISCORD_TOKEN

GUILD_ID = 1257878770841288724
CATEGORY_ID = 1257902293609742346
LOG_CHANNEL_ID = 1462207061902496037

RECRUITER_ROLE_ID = 1257896905099444354
MIEMBRO_ROLE_ID = 1257896455860129822
TANK_ROLE_ID = 1260755129754189854
HEALER_ROLE_ID = 1260755151296266331
SUPP_ROLE_ID = 1260755342472646656
DPS_ROLE_ID = 1260755289062248458

# ✅ NUEVO: rol Public (se saca al aceptar)
PUBLIC_ROLE_ID = 1266805315547041902

# ✅ STAFF
STAFF_ROLE_ID = 1257896709246423083

# ✅ RELOJ UTC (poné acá el canal que querés renombrar)
CLOCK_CHANNEL_ID = 1462464849463214395

COOLDOWN_SECONDS = 60

active_applications = {}
cooldowns = {}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

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

# ---------- RELOJ UTC (cada 5 min) ----------

@tasks.loop(minutes=5)
async def utc_clock():
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
    new_name = f"🕒 UTC {now:%H%M}"

    if channel.name == new_name:
        return

    try:
        await channel.edit(name=new_name, reason="UTC clock update (cada 5 min)")
    except discord.Forbidden:
        print("❌ No tengo permisos para editar el canal (Manage Channels).")
    except Exception as e:
        print("❌ Error editando canal:", e)

@utc_clock.before_loop
async def before_utc_clock():
    await bot.wait_until_ready()

# ---------- READY ----------

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    bot.add_view(PanelView())
    print("✅ Panel persistente cargado")

    if not utc_clock.is_running():
        utc_clock.start()
        print("✅ Reloj UTC iniciado (cada 5 min)")

# ---------- COMANDOS STAFF (los tuyos) ----------

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
            role = await ctx.guild.create_role(
                name=role_name,
                reason=f"Rol creado automáticamente por {ctx.author} (Staff)"
            )
        except discord.Forbidden:
            return await ctx.reply("❌ No pude crear el rol. Me falta permiso **Manage Roles** o jerarquía.")
        except Exception:
            return await ctx.reply("❌ Error inesperado creando el rol.")

    ok = 0
    fail = 0
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

    role = None
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
            await interaction.response.send_message(
                "❌ Solo reclutadores pueden usar estos botones.",
                ephemeral=True
            )
            return False
        return True

    async def accept_player(self, interaction: discord.Interaction, role_id: int, role_name: str):
        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
        public_role = interaction.guild.get_role(PUBLIC_ROLE_ID)  # ✅ NUEVO

        if role is None or member_role is None:
            await interaction.channel.send("❌ Error: no encontré uno de los roles configurados (IDs mal).")
            return

        try:
            # ✅ Da roles nuevos
            await self.user.add_roles(member_role, role, reason=f"Aceptado como {role_name}")

            # ✅ NUEVO: saca rol Public si lo tiene
            if public_role and public_role in self.user.roles:
                await self.user.remove_roles(public_role, reason="Aceptado: se quita rol Public")

        except discord.Forbidden:
            await interaction.channel.send(
                "❌ No tengo permisos para asignar/quitar roles.\n"
                "✅ Revisá: permisos del bot y que los roles estén *debajo* del rol del bot."
            )
            return
        except Exception:
            await interaction.channel.send("❌ Error inesperado asignando roles.")
            return

        await interaction.channel.send(
            f"✅ {self.user.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️"
        )

        await send_log(
            interaction.guild,
            f"✅ **ACEPTADO** {self.user} → {role_name} (Public removido)"
        )

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
            await interaction.response.send_message("❌ Ya tenés una postulación activa.", ephemeral=True)
            return

        if user_id in cooldowns and (now - cooldowns[user_id] < COOLDOWN_SECONDS):
            await interaction.response.send_message("⏳ Esperá un momento antes de volver a intentar.", ephemeral=True)
            return

        cooldowns[user_id] = now
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        if category is None:
            await interaction.followup.send("❌ Error: no encontré la categoría configurada (CATEGORY_ID mal).", ephemeral=True)
            return

        if recruiter_role is None:
            await interaction.followup.send("❌ Error: no encontré el rol de reclutador (RECRUITER_ROLE_ID mal).", ephemeral=True)
            return

        channel_name = f"postulacion-{interaction.user.name}-{interaction.user.id}"

        bot_member = guild.me or guild.get_member(bot.user.id)

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

        # ✅ NUEVO: tag al rol reclutador cuando abre ticket
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

# ---------- RUN ----------

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno.")

bot.run(TOKEN)
