import discord
from discord.ext import commands
import os
import time

TOKEN = os.getenv("TOKEN")

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
    bot.add_view(PanelView())
    print("✅ Panel persistente cargado")


# ---------- UTILIDADES ----------

async def send_log(guild, message):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(message)


async def create_transcript(channel):
    messages = []
    async for msg in channel.history(limit=200):
        content = msg.content if msg.content else ""
        messages.append(f"[{msg.author}] {content}")

    transcript = "\n".join(reversed(messages))
    return transcript


# ---------- RECRUIT VIEW ----------

class RecruitView(discord.ui.View):

    def __init__(self, user):
        super().__init__(timeout=None)
        self.user = user


    async def interaction_check(self, interaction):

        recruiter_role = interaction.guild.get_role(RECRUITER_ROLE_ID)

        if recruiter_role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ Solo reclutadores pueden usar estos botones.",
                ephemeral=True
            )
            return False

        return True


    async def accept_player(self, interaction, role_id, role_name):

        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)

        await self.user.add_roles(member_role, role)

        await interaction.channel.send(
            f"✅ {self.user.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️"
        )

        await send_log(
            interaction.guild,
            f"✅ **ACEPTADO** {self.user} → {role_name}"
        )


    @discord.ui.button(
        label="✔ Miembro",
        style=discord.ButtonStyle.success,
        custom_id="accept_miembro"
    )
    async def miembro(self, interaction, button):
        await interaction.response.defer()
        await self.accept_player(interaction, MIEMBRO_ROLE_ID, "Miembro")


    @discord.ui.button(
        label="🛡 Tank",
        style=discord.ButtonStyle.primary,
        custom_id="accept_tank"
    )
    async def tank(self, interaction, button):
        await interaction.response.defer()
        await self.accept_player(interaction, TANK_ROLE_ID, "Tank")


    @discord.ui.button(
        label="✨ Healer",
        style=discord.ButtonStyle.primary,
        custom_id="accept_healer"
    )
    async def healer(self, interaction, button):
        await interaction.response.defer()
        await self.accept_player(interaction, HEALER_ROLE_ID, "Healer")


    @discord.ui.button(
        label="🧙 Support",
        style=discord.ButtonStyle.primary,
        custom_id="accept_support"
    )
    async def supp(self, interaction, button):
        await interaction.response.defer()
        await self.accept_player(interaction, SUPP_ROLE_ID, "Support")


    @discord.ui.button(
        label="⚔ DPS",
        style=discord.ButtonStyle.primary,
        custom_id="accept_dps"
    )
    async def dps(self, interaction, button):
        await interaction.response.defer()
        await self.accept_player(interaction, DPS_ROLE_ID, "DPS")


    @discord.ui.button(
        label="❌ Rechazar",
        style=discord.ButtonStyle.secondary,
        custom_id="reject_postulacion"
    )
    async def reject(self, interaction, button):

        await interaction.response.defer()

        try:
            await self.user.send(
                "❌ Tu postulación en **Dies-Irae** fue rechazada.\n"
                "Podés volver a aplicar más adelante."
            )
        except:
            pass

        await send_log(
            interaction.guild,
            f"❌ **RECHAZADO** {self.user}"
        )

        active_applications.pop(self.user.id, None)

        await interaction.channel.delete()


    @discord.ui.button(
        label="🔒 Cerrar Postulación",
        style=discord.ButtonStyle.danger,
        custom_id="close_postulacion"
    )
    async def close(self, interaction, button):

        await interaction.response.defer()

        transcript = await create_transcript(interaction.channel)

        await send_log(
            interaction.guild,
            f"🔒 **POSTULACIÓN CERRADA** {self.user}\n```\n{transcript[:1800]}\n```"
        )

        active_applications.pop(self.user.id, None)

        await interaction.channel.delete()


# ---------- PANEL VIEW ----------

class PanelView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)


    @discord.ui.button(
        label="⚡ Abrir Postulación",
        style=discord.ButtonStyle.success,
        custom_id="open_postulacion"
    )
    async def open_application(self, interaction: discord.Interaction, button):

        if interaction.guild is None:
            return

        user_id = interaction.user.id
        now = time.time()

        if user_id in active_applications:
            await interaction.response.send_message(
                "❌ Ya tenés una postulación activa.",
                ephemeral=True
            )
            return

        if user_id in cooldowns:
            if now - cooldowns[user_id] < COOLDOWN_SECONDS:
                await interaction.response.send_message(
                    "⏳ Esperá un momento antes de volver a intentar.",
                    ephemeral=True
                )
                return

        cooldowns[user_id] = now

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        channel_name = f"postulacion-{interaction.user.name}-{interaction.user.discriminator}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            recruiter_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }

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

        await send_log(
            guild,
            f"📥 **NUEVA POSTULACIÓN** {interaction.user} → {channel.mention}"
        )

        await interaction.followup.send(
            f"✅ Postulación creada: {channel.mention}",
            ephemeral=True
        )


# ---------- COMANDO PANEL ----------

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):

    embed = discord.Embed(
        title="⚔️ Dies-Irae Reclutamiento",
        description="Presioná el botón para abrir tu **postulación oficial**.",
        color=discord.Color.orange()
    )

    embed.set_footer(text="Albion Online Recruitment System")

    await ctx.send(embed=embed, view=PanelView())


bot.run(TOKEN)
