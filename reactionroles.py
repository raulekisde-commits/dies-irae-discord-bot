"""/reaction-roles: un mensaje del bot con roles y reacciones. Reaccionar da el rol; sacar la reacción lo quita."""
import asyncio
import traceback
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx, utcnow_iso
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
MAX_ROLES_PER_PANEL = 20                                  # límite de Discord: 20 reacciones distintas por mensaje
NAME_MAX = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS rr_panels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rr_panels_msg ON rr_panels(message_id);
CREATE TABLE IF NOT EXISTS rr_roles (
    panel_id INTEGER NOT NULL,
    emoji    TEXT NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (panel_id, emoji),
    FOREIGN KEY (panel_id) REFERENCES rr_panels(id) ON DELETE CASCADE
);
"""

# Roles que NO se pueden ofrecer por reacción (escalada de privilegios)
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_nicknames", "kick_members",
             "ban_members", "moderate_members", "manage_messages", "mention_everyone", "manage_threads", "manage_expressions")


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _panel(conn, row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["roles"] = [(r["emoji"], int(r["role_id"])) for r in conn.execute("SELECT emoji, role_id FROM rr_roles WHERE panel_id=? ORDER BY rowid", (d["id"],))]
    return d


def create_panel(db_path: str, guild_id: int, name: str, channel_id: int, message_id: int, created_by: int) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("INSERT INTO rr_panels(guild_id, name, channel_id, message_id, created_by, created_at) VALUES (?,?,?,?,?,?)",
                               (guild_id, name, channel_id, message_id, created_by, utcnow_iso()))
            return int(cur.lastrowid)
    finally:
        conn.close()


def name_taken(db_path: str, guild_id: int, name: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM rr_panels WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, name)).fetchone() is not None
    finally:
        conn.close()


def get_by_name(db_path: str, guild_id: int, name: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _panel(conn, conn.execute("SELECT * FROM rr_panels WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, name)).fetchone())
    finally:
        conn.close()


def get_by_message(db_path: str, message_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        return _panel(conn, conn.execute("SELECT * FROM rr_panels WHERE message_id=?", (message_id,)).fetchone())
    finally:
        conn.close()


def list_panels(db_path: str, guild_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [_panel(conn, r) for r in conn.execute("SELECT * FROM rr_panels WHERE guild_id=? ORDER BY id", (guild_id,))]
    finally:
        conn.close()


def add_role(db_path: str, panel_id: int, emoji: str, role_id: int) -> str:
    """'ok' | 'emoji' (ya usado) | 'role' (ya está) | 'full'."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            rows = conn.execute("SELECT emoji, role_id FROM rr_roles WHERE panel_id=?", (panel_id,)).fetchall()
            if any(r["emoji"] == emoji for r in rows):
                return "emoji"
            if any(int(r["role_id"]) == role_id for r in rows):
                return "role"
            if len(rows) >= MAX_ROLES_PER_PANEL:
                return "full"
            conn.execute("INSERT INTO rr_roles(panel_id, emoji, role_id) VALUES (?,?,?)", (panel_id, emoji, role_id))
            return "ok"
    finally:
        conn.close()


def remove_role(db_path: str, panel_id: int, role_id: int) -> Optional[str]:
    """Devuelve el emoji que tenía ese rol, o None si no estaba."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT emoji FROM rr_roles WHERE panel_id=? AND role_id=?", (panel_id, role_id)).fetchone()
            if r is None:
                return None
            conn.execute("DELETE FROM rr_roles WHERE panel_id=? AND role_id=?", (panel_id, role_id))
            return r["emoji"]
    finally:
        conn.close()


def delete_panel(db_path: str, panel_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM rr_roles WHERE panel_id=?", (panel_id,))
            conn.execute("DELETE FROM rr_panels WHERE id=?", (panel_id,))
    finally:
        conn.close()


def role_problem(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Por qué NO se puede ofrecer ese rol por reacción (None = se puede)."""
    if role.is_default():
        return "No se puede usar @everyone."
    if role.managed:
        return "Ese rol lo administra una integración o bot."
    bad = [p for p in DANGEROUS if getattr(role.permissions, p, False)]
    if bad:
        return "Ese rol tiene permisos de moderación o administración, y no se puede dar por reacción."
    me = guild.me
    if me is not None and role >= me.top_role:
        return "Ese rol está por encima de mi rol más alto: no podría darlo. Subí mi rol en la lista de roles."
    return None


def panel_embed(panel: dict) -> discord.Embed:
    lines = [f"{e} — <@&{rid}>" for e, rid in panel["roles"]]
    emb = discord.Embed(title=f"🎭 {panel['name']}", color=discord.Color.blurple(),
                        description=("\n".join(lines) if lines else "_Todavía no hay roles._") + "\n\nReaccioná para recibir el rol; sacá la reacción para quitártelo.")
    return emb


class ReactionRolesCog(commands.Cog):
    group = app_commands.Group(name="reaction-roles", description="Roles por reacción", guild_only=True)

    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not can_manage(interaction.user):
            await interaction.response.send_message("🔒 Solo el Council o los administradores pueden usar este comando.", ephemeral=True)
            return False
        return True

    async def _message(self, panel: dict) -> Optional[discord.Message]:
        try:
            ch = self.bot.get_channel(panel["channel_id"]) or await self.bot.fetch_channel(panel["channel_id"])
            return await ch.fetch_message(panel["message_id"])
        except Exception:
            return None

    async def name_autocomplete(self, interaction: discord.Interaction, current: str):
        panels = await self.run(list_panels, interaction.guild_id)
        return [app_commands.Choice(name=p["name"][:100], value=p["name"]) for p in panels if current.casefold() in p["name"].casefold()][:25]

    # ---------- /reaction-roles crear ----------
    @group.command(name="crear", description="Crea un mensaje de roles por reacción en este canal")
    @app_commands.describe(nombre="Nombre del panel (título del mensaje)")
    async def crear(self, interaction: discord.Interaction, nombre: app_commands.Range[str, 1, NAME_MAX]):
        if not await self._gate(interaction):
            return
        nombre = " ".join(nombre.split())
        if await self.run(name_taken, interaction.guild.id, nombre):
            return await interaction.response.send_message("❌ Ya hay un panel con ese nombre.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        draft = {"name": nombre, "roles": []}
        try:
            msg = await interaction.channel.send(embed=panel_embed(draft), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            return await interaction.followup.send("❌ No pude publicar el mensaje en este canal (¿me faltan permisos?).", ephemeral=True)
        await self.run(create_panel, interaction.guild.id, nombre, interaction.channel.id, msg.id, interaction.user.id)
        await interaction.followup.send(f"✅ Panel **{discord.utils.escape_markdown(nombre)}** creado. Agregá roles con `/reaction-roles agregar nombre:{nombre} rol:@rol emoji:😀`.", ephemeral=True)

    # ---------- /reaction-roles agregar ----------
    @group.command(name="agregar", description="Agrega un rol (y su reacción) a un panel")
    @app_commands.describe(nombre="Panel", rol="Rol que se da", emoji="Emoji de la reacción")
    @app_commands.autocomplete(nombre=name_autocomplete)
    async def agregar(self, interaction: discord.Interaction, nombre: str, rol: discord.Role, emoji: str):
        if not await self._gate(interaction):
            return
        panel = await self.run(get_by_name, interaction.guild.id, nombre)
        if panel is None:
            return await interaction.response.send_message("❌ No encuentro ese panel.", ephemeral=True)
        problem = role_problem(interaction.guild, rol)
        if problem:
            return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        pe = discord.PartialEmoji.from_str(emoji.strip())
        key = str(pe)
        if not key:
            return await interaction.response.send_message("❌ Emoji inválido.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        msg = await self._message(panel)
        if msg is None:
            return await interaction.followup.send("❌ No encuentro el mensaje del panel (¿lo borraron?). Eliminá el panel y creá otro.", ephemeral=True)
        try:
            await msg.add_reaction(pe)                   # si el emoji no existe o no lo puedo usar, falla acá y no se guarda nada
        except Exception:
            return await interaction.followup.send("❌ No pude usar ese emoji (¿es de otro servidor o no existe?).", ephemeral=True)
        res = await self.run(add_role, panel["id"], key, rol.id)
        if res != "ok":
            try:
                if res != "emoji":
                    await msg.clear_reaction(pe)
            except Exception:
                pass
            texts = {"emoji": "Ese emoji ya está usado en este panel.", "role": "Ese rol ya está en este panel.",
                     "full": f"Un panel admite hasta {MAX_ROLES_PER_PANEL} roles."}
            return await interaction.followup.send(f"❌ {texts[res]}", ephemeral=True)
        panel = await self.run(get_by_name, interaction.guild.id, nombre)
        try:
            await msg.edit(embed=panel_embed(panel), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        await interaction.followup.send(f"✅ {key} → {rol.mention} agregado a **{discord.utils.escape_markdown(panel['name'])}**.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- /reaction-roles quitar ----------
    @group.command(name="quitar", description="Quita un rol de un panel")
    @app_commands.describe(nombre="Panel", rol="Rol a quitar del panel (no se lo saca a nadie)")
    @app_commands.autocomplete(nombre=name_autocomplete)
    async def quitar(self, interaction: discord.Interaction, nombre: str, rol: discord.Role):
        if not await self._gate(interaction):
            return
        panel = await self.run(get_by_name, interaction.guild.id, nombre)
        if panel is None:
            return await interaction.response.send_message("❌ No encuentro ese panel.", ephemeral=True)
        emoji = await self.run(remove_role, panel["id"], rol.id)
        if emoji is None:
            return await interaction.response.send_message("ℹ️ Ese rol no estaba en el panel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        msg = await self._message(panel)
        if msg is not None:
            try:
                await msg.clear_reaction(discord.PartialEmoji.from_str(emoji))
            except Exception:
                pass
            panel = await self.run(get_by_name, interaction.guild.id, nombre)
            try:
                await msg.edit(embed=panel_embed(panel), allowed_mentions=NO_MENTIONS)
            except Exception:
                traceback.print_exc()
        await interaction.followup.send(f"✅ {rol.mention} salió del panel (a quienes ya lo tienen no se les quita).", ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- /reaction-roles eliminar / listar ----------
    @group.command(name="eliminar", description="Elimina un panel y su mensaje (no se le saca el rol a nadie)")
    @app_commands.describe(nombre="Panel")
    @app_commands.autocomplete(nombre=name_autocomplete)
    async def eliminar(self, interaction: discord.Interaction, nombre: str):
        if not await self._gate(interaction):
            return
        panel = await self.run(get_by_name, interaction.guild.id, nombre)
        if panel is None:
            return await interaction.response.send_message("❌ No encuentro ese panel.", ephemeral=True)
        await self.run(delete_panel, panel["id"])
        msg = await self._message(panel)
        if msg is not None:
            try:
                await msg.delete()
            except Exception:
                pass
        await interaction.response.send_message("🗑️ Panel eliminado.", ephemeral=True)

    @group.command(name="listar", description="Lista los paneles de roles por reacción")
    async def listar(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        panels = await self.run(list_panels, interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No hay paneles.", ephemeral=True)
        text = "\n".join(f"• **{discord.utils.escape_markdown(p['name'])}** · <#{p['channel_id']}> · {len(p['roles'])} rol(es) · "
                         f"[mensaje](https://discord.com/channels/{interaction.guild.id}/{p['channel_id']}/{p['message_id']})" for p in panels)
        await interaction.response.send_message(text[:1990], ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- reacciones ----------
    async def _handle(self, payload: discord.RawReactionActionEvent, adding: bool):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
            return
        panel = await self.run(get_by_message, payload.message_id)
        if panel is None:
            return
        role_id = next((rid for e, rid in panel["roles"] if e == str(payload.emoji)), None)
        if role_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        if member.bot:
            return
        role = guild.get_role(role_id)
        if role is None or role_problem(guild, role):
            return
        has = role in member.roles
        try:
            if adding and not has:
                await member.add_roles(role, reason=f"Reaction role «{panel['name']}»")
            elif not adding and has:
                await member.remove_roles(role, reason=f"Reaction role «{panel['name']}»")
            # si ya tenía el rol y reacciona (o no lo tiene y saca la reacción) no se hace nada
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        panel = await self.run(get_by_message, payload.message_id)
        if panel is not None:
            await self.run(delete_panel, panel["id"])


async def setup_reactionroles(bot, db_path: str, guild_id: int) -> ReactionRolesCog:
    cog = ReactionRolesCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
