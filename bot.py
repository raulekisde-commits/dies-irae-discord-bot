import discord
from discord.ext import commands
import os

TOKEN = os.getenv("TOKEN")

GUILD_ID = 1257878770841288724
CATEGORY_ID = 1257902293609742346

RECRUITER_ROLE_ID = 1257896905099444354
MIEMBRO_ROLE_ID = 1257896455860129822
TANK_ROLE_ID = 1260755129754189854
HEALER_ROLE_ID = 1260755151296266331
SUPP_ROLE_ID = 1260755342472646656
DPS_ROLE_ID = 1260755289062248458

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- READY ----------

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")

    try:
        bot.add_view(TicketPanel())
        print("✅ Panel persistente cargado")
    except:
        pass


# ---------- POSTULACION CONTROL VIEW ----------

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


    async def give_role(self, interaction, role_id, role_name):

        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)

        target = self.user

        await target.add_roles(member_role, role)

        await interaction.channel.send(
            f"✅ {target.mention} aceptado como **{role_name}** en **Dies-Irae** ⚔️"
        )


    @discord.ui.button(label="✔ Miembro", style=discord.ButtonStyle.success)
    async def miembro(self, interaction, button):
        await interaction.response.defer()
        await self.give_role(interaction, MIEMBRO_ROLE_ID, "Miembro")


    @discord.ui.button(label="🛡 Tank", style=discord.ButtonStyle.primary)
    async def tank(self, interaction, button):
        await interaction.response.defer()
        await self.give_role(interaction, TANK_ROLE_ID, "Tank")


    @discord.ui.button(label="✨ Healer", style=discord.ButtonStyle.primary)
    async def healer(self, interaction, button):
        await interaction.response.defer()
        await self.give_role(interaction, HEALER_ROLE_ID, "Healer")


    @discord.ui.button(label="🧙 Support", style=discord.ButtonStyle.primary)
    async def supp(self, interaction, button):
        await interaction.response.defer()
        await self.give_role(interaction, SUPP_ROLE_ID, "Support")


    @discord.ui.button(label="⚔ DPS", style=discord.ButtonStyle.primary)
    async def dps(self, interaction, button):
        await interaction.response.defer()
        await self.give_role(interaction, DPS_ROLE_ID, "DPS")


    @discord.ui.button(label="🔒 Cerrar Postulación", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        await interaction.response.defer()
        await interaction.channel.send("🔒 Postulación cerrada por reclutador.")
        await interaction.channel.delete()


# ---------- PANEL BOTON ----------

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


    @discord.ui.button(label="⚡ Abrir Postulación", style=discord.ButtonStyle.success)
    async def open_ticket(self, interaction: discord.Interaction, button):

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Solo podés usar esto dentro del servidor.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        if category is None:
            await interaction.followup.send(
                "❌ Error: Categoría de postulaciones no encontrada.",
                ephemeral=True
            )
            return

        channel_name = f"postulacion-{interaction.user.name}".lower()

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

        embed = discord.Embed(
            title="⚔️ Reclutamiento Dies-Irae",
            description=(
                "**Enviá lo siguiente:**\n\n"
                "📸 Screenshot perfil Albion\n"
                "⚔ Rol ZvZ\n"
                "🕒 Horarios disponibles\n\n"
                "Un reclutador revisará tu postulación."
            ),
            color=discord.Color.gold()
        )

        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=RecruitView(interaction.user)
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
        description="Presioná el botón para abrir tu **postulación** a la guild.",
        color=discord.Color.orange()
    )

    embed.set_footer(text="Albion Online Recruitment System")

    await ctx.send(embed=embed, view=TicketPanel())


bot.run(TOKEN)
