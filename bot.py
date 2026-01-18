from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200

def run_web():
    app.run(host="0.0.0.0", port=3000)

Thread(target=run_web, daemon=True).start()

import discord
from discord.ext import commands
import os
import time

# ================== CONFIG ==================

TOKEN = os.getenv("DISCORD_TOKEN")  # Railway: Variables -> DISCORD_TOKEN

GUILD_ID = 1257878770841288724
CATEGORY_ID = 1257902293609742346
LOG_CHANNEL_ID = 1462207061902496037

RECRUITER_ROLE_ID = 1257896905099444354
MIEMBRO_ROLE_ID = 1257896455860129822
TANK_ROLE_ID = 1260755129754189854
HEALER_ROLE_ID = 1260755151296266331
SUPP_ROLE_ID = 1260755342472646656
DPS_ROLE_ID = 1260755289062248458

COOLDOWN_SECONDS = 60

active_applications = {}
cooldowns = {}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- READY ----------

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    bot.add_view(PanelView())  # view persistente
    print("✅ Panel persistente cargado")


# ---------- UTILIDADES ----------

async def send_log(guild: discord.Guild, message: str):
    """Manda logs al canal aunque no esté cacheado."""
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

        if role is None or member_role is None:
            await interaction.channel.send("❌ Error: no encontré uno de los roles configurados (IDs mal).")
            return

        try:
            await self.user.add_roles(member_role, role, reason=f"Aceptado como {role_name}")
        except discord.Forbidden:
            await interaction.channel.send(
                "❌ No tengo permisos para asignar roles.\n"
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
            f"✅ **ACEPTADO** {self.user} → {role_name}"
        )

        # ✅ Cierra y libera la postulación
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

        # ✅ nombre estable y único (no depende del discriminator)
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

        await channel.send(
            content=interaction.user.mention,
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

