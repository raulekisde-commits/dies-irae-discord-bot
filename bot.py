# ================== BOT ==================
import discord
from discord.ext import commands
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

# ================== TOKEN ==================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    env_path = "/root/discordbot/.env"
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN")

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

active_applications = {}   # user_id -> channel_id
ticket_images = {}         # user_id -> image_url

# ================== INTENTS ==================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================== HELPERS ==================
async def respond_ephemeral(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        pass

# ================== RECRUIT VIEW ==================
class RecruitView(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=None)
        self.user = user

    async def accept_player(self, interaction: discord.Interaction, role_id: int, role_name: str):
        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
        public_role = interaction.guild.get_role(PUBLIC_ROLE_ID)

        if not role or not member_role:
            await interaction.channel.send("❌ Error de configuración de roles.")
            return

        await self.user.add_roles(member_role, role, reason=f"Aceptado como {role_name}")
        if public_role and public_role in self.user.roles:
            await self.user.remove_roles(public_role)

        await interaction.channel.send(
            f"✅ {self.user.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️"
        )

        # -------- LOG --------
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(
                title="✅ POSTULACIÓN ACEPTADA",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="👤 Postulante", value=self.user.mention, inline=True)
            embed.add_field(name="🧑‍💼 Reclutador", value=interaction.user.mention, inline=True)
            embed.add_field(name="🎭 Rol asignado", value=f"**{role_name}**", inline=False)
            embed.add_field(name="📍 Ticket", value=interaction.channel.name, inline=False)

            img_url = ticket_images.get(self.user.id)
            if img_url:
                embed.set_image(url=img_url)

            await log_channel.send(embed=embed)

        active_applications.pop(self.user.id, None)
        await interaction.channel.delete()

    @discord.ui.button(label="✔ Miembro", style=discord.ButtonStyle.success, custom_id="accept_miembro")
    async def miembro(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, MIEMBRO_ROLE_ID, "Miembro")

    @discord.ui.button(label="🛡 Tank", style=discord.ButtonStyle.primary, custom_id="accept_tank")
    async def tank(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, TANK_ROLE_ID, "Tank")

    @discord.ui.button(label="✨ Healer", style=discord.ButtonStyle.primary, custom_id="accept_healer")
    async def healer(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, HEALER_ROLE_ID, "Healer")

    @discord.ui.button(label="🧙 Support", style=discord.ButtonStyle.primary, custom_id="accept_support")
    async def supp(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, SUPP_ROLE_ID, "Support")

    @discord.ui.button(label="⚔ DPS", style=discord.ButtonStyle.primary, custom_id="accept_dps")
    async def dps(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.accept_player(interaction, DPS_ROLE_ID, "DPS")

# ================== PANEL VIEW ==================
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="⚡ Abrir Postulación",
        style=discord.ButtonStyle.success,
        custom_id="open_postulacion_panel"
    )
    async def open_application(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id in active_applications:
            return await respond_ephemeral(interaction, "❌ Ya tenés una postulación abierta.")

        guild = interaction.guild
        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True),
            recruiter_role: discord.PermissionOverwrite(view_channel=True),
        }

        channel = await guild.create_text_channel(
            f"postulacion-{interaction.user.name}-{interaction.user.id}",
            category=category,
            overwrites=overwrites
        )

        active_applications[interaction.user.id] = channel.id

        embed = discord.Embed(
            title="⚔️ Reclutamiento Dies-Irae",
            description="📸 Screenshot perfil Albion\n⚔ Rol ZvZ\n🕒 Horarios",
            color=discord.Color.gold()
        )

        await channel.send(
            content=f"{interaction.user.mention} {recruiter_role.mention}",
            embed=embed,
            view=RecruitView(interaction.user)
        )

        await respond_ephemeral(interaction, f"✅ Postulación creada: {channel.mention}")

# ================== EVENTS ==================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if (
        isinstance(message.channel, discord.TextChannel)
        and message.channel.category_id == CATEGORY_ID
        and message.author.id in active_applications
    ):
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image"):
                ticket_images[message.author.id] = att.url

    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    bot.add_view(PanelView())

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # ignorar comandos inexistentes
    raise error

# ================== PANEL COMMAND (ANTI DUPLICADOS) ==================
@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    # 🔒 Evitar duplicados
    async for msg in ctx.channel.history(limit=30):
        if msg.author == bot.user and msg.components:
            await ctx.reply("⚠️ Ya hay un panel activo en este canal.", delete_after=5)
            return

    embed = discord.Embed(
        title="⚔️ Dies-Irae Reclutamiento",
        description="Abrí tu postulación oficial",
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed, view=PanelView())

# ================== RUN ==================
bot.run(TOKEN)
