"""Grupo /bomb: cada bomb tiene nombre, rol y caller.
- Council (o admin/propietario): /bomb crear, /bomb editar, /bomb eliminar.
- Staff, Reclutador y Council: /bomb agregar, /bomb sacar (dan / quitan el rol y queda quién lo hizo) y /bomb cargar
  (anota en la lista a quienes ya tienen el rol de la bomb pero no fueron agregados con el comando).
- /bomb listar: lo anterior y además el caller de cualquier bomb."""
import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import bombs_db as bdb
from guildshop import can_manage

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
RECRUITER_ROLE_ID = int(os.getenv("RECRUITER_ROLE_ID", "1257896905099444354"))
NO_MENTIONS = discord.AllowedMentions.none()


def _roles(m) -> set:
    return {getattr(r, "id", None) for r in getattr(m, "roles", [])}


def can_define(member) -> bool:
    """Crear / editar / eliminar bombs: Council, propietario o administrador."""
    return can_manage(member)


def can_staff(member) -> bool:
    """Agregar / sacar gente: Council, Staff o Reclutador (y administradores)."""
    return can_manage(member) or bool({STAFF_ROLE_ID, RECRUITER_ROLE_ID} & _roles(member))


def _unix(iso: str) -> int:
    try:
        d = datetime.fromisoformat(iso)
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        return 0


async def _say(interaction, text):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


def _chunks(lines: List[str], limit: int = 1800) -> List[str]:
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        out.append(cur)
    return out


def role_problem(guild, role) -> Optional[str]:
    """Motivo por el que ese rol no sirve para una bomb (None si sirve)."""
    if role is None:
        return "❌ No encuentro ese rol."
    if role.id == getattr(guild, "id", None) or getattr(role, "managed", False):
        return "❌ Ese rol no se puede usar (es @everyone o lo maneja una integración)."
    perms = getattr(role, "permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return "❌ No se puede usar un rol con permisos de administrador."
    me = getattr(guild, "me", None)
    top = getattr(me, "top_role", None)
    if top is not None and hasattr(role, "position") and role.position >= top.position:
        return "❌ Ese rol está por encima del mío: no lo puedo dar ni sacar."
    return None


class BombsCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        bdb.init_schema(db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    bomb_g = app_commands.Group(name="bomb", description="Bombs: grupos con nombre, rol y caller", guild_only=True)

    async def bomb_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.run(bdb.list_bombs, interaction.guild.id)
        cur = (current or "").casefold()
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if cur in r["name"].casefold()][:25]

    async def _bomb(self, interaction, name: str) -> Optional[dict]:
        b = await self.run(bdb.find_bomb, interaction.guild.id, name)
        if b is None:
            await _say(interaction, f"❌ No existe la bomb **{discord.utils.escape_markdown(bdb.clean_name(name))}**. Mirá `/bomb listar`.")
        return b

    # ---------- Council: definir bombs ----------
    @bomb_g.command(name="crear", description="(Council) Crea una bomb con su nombre, rol y caller")
    @app_commands.describe(nombre="Nombre de la bomb", rol="Rol que reciben los integrantes", caller="Caller de la bomb")
    async def crear(self, interaction: discord.Interaction, nombre: str, rol: discord.Role, caller: discord.Member):
        if not can_define(interaction.user):
            return await _say(interaction, "🔒 Solo el **Council** puede crear bombs.")
        err = role_problem(interaction.guild, rol)
        if err:
            return await _say(interaction, err)
        if caller.bot:
            return await _say(interaction, "❌ El caller no puede ser un bot.")
        try:
            b = await self.run(bdb.create_bomb, interaction.guild.id, nombre, rol.id, caller.id, interaction.user.id)
        except bdb.BombError as exc:
            return await _say(interaction, f"❌ {exc}")
        await _say(interaction, f"✅ Bomb **{discord.utils.escape_markdown(b['name'])}** creada · rol {rol.mention} · caller {caller.mention}.")

    @bomb_g.command(name="editar", description="(Council) Cambia el nombre, el rol o el caller de una bomb")
    @app_commands.describe(bomb="La bomb", nombre="Nombre nuevo", rol="Rol nuevo", caller="Caller nuevo")
    async def editar(self, interaction: discord.Interaction, bomb: str, nombre: Optional[str] = None,
                     rol: Optional[discord.Role] = None, caller: Optional[discord.Member] = None):
        if not can_define(interaction.user):
            return await _say(interaction, "🔒 Solo el **Council** puede editar bombs.")
        if nombre is None and rol is None and caller is None:
            return await _say(interaction, "Indicá al menos un cambio: nombre, rol o caller.")
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        if rol is not None:
            err = role_problem(interaction.guild, rol)
            if err:
                return await _say(interaction, err)
        if caller is not None and caller.bot:
            return await _say(interaction, "❌ El caller no puede ser un bot.")
        try:
            nb = await self.run(bdb.update_bomb, interaction.guild.id, b["id"], name=nombre,
                                role_id=rol.id if rol else None, caller_id=caller.id if caller else None)
        except bdb.BombError as exc:
            return await _say(interaction, f"❌ {exc}")
        warn = ""
        if rol is not None and rol.id != b["role_id"]:
            warn = ("\n⚠️ Los integrantes que ya estaban **conservan el rol viejo** y no reciben el nuevo solos: "
                    "sacalos y volvelos a agregar si hace falta.")
        await _say(interaction, f"✅ Bomb actualizada: **{discord.utils.escape_markdown(nb['name'])}** · rol <@&{nb['role_id']}> · caller <@{nb['caller_id']}>.{warn}")

    @bomb_g.command(name="eliminar", description="(Council) Elimina una bomb y su lista de integrantes")
    @app_commands.describe(bomb="La bomb", sacar_roles="Sacarles también el rol a los integrantes (por defecto no)")
    async def eliminar(self, interaction: discord.Interaction, bomb: str, sacar_roles: Optional[bool] = False):
        if not can_define(interaction.user):
            return await _say(interaction, "🔒 Solo el **Council** puede eliminar bombs.")
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        rows = await self.run(bdb.members, b["id"])
        await self.run(bdb.delete_bomb, interaction.guild.id, b["id"])
        removed = 0
        role = interaction.guild.get_role(b["role_id"])
        if sacar_roles and role is not None:
            for r in rows:
                m = interaction.guild.get_member(r["user_id"])
                if m is not None and role in m.roles:
                    try:
                        await m.remove_roles(role, reason=f"/bomb eliminar {b['name']} por {interaction.user}")
                        removed += 1
                    except discord.HTTPException:
                        pass
        extra = f" Se les sacó el rol a {removed}." if sacar_roles else " Los integrantes conservan el rol."
        await _say(interaction, f"🗑️ Se eliminó la bomb **{discord.utils.escape_markdown(b['name'])}** ({len(rows)} integrantes).{extra}")

    # ---------- Staff / Reclutador: gente ----------
    @bomb_g.command(name="agregar", description="(Staff/Reclutador) Agrega a alguien a una bomb y le da su rol")
    @app_commands.describe(bomb="La bomb", usuario="Persona a agregar")
    async def agregar(self, interaction: discord.Interaction, bomb: str, usuario: discord.Member):
        if not can_staff(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, los reclutadores y el Council pueden agregar gente a una bomb.")
        if usuario.bot:
            return await _say(interaction, "❌ No se puede agregar un bot.")
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        role = interaction.guild.get_role(b["role_id"])
        if role is None:
            return await _say(interaction, "❌ El rol de esa bomb ya no existe. Pedile al Council que la edite con `/bomb editar`.")
        if role not in usuario.roles:
            try:
                await usuario.add_roles(role, reason=f"/bomb agregar {b['name']} por {interaction.user} ({interaction.user.id})")
            except discord.HTTPException as exc:
                return await _say(interaction, f"❌ No pude darle el rol (revisá que mi rol esté por encima): {exc}")
        new = await self.run(bdb.add_member, b["id"], usuario.id, interaction.user.id)
        name = discord.utils.escape_markdown(b["name"])
        if not new:
            return await _say(interaction, f"ℹ️ {usuario.mention} ya estaba en **{name}** (se verificó que tenga el rol).")
        await _say(interaction, f"✅ {usuario.mention} agregado a **{name}** · rol {role.mention}.")

    @bomb_g.command(name="sacar", description="(Staff/Reclutador) Saca a alguien de una bomb y le quita su rol")
    @app_commands.describe(bomb="La bomb", usuario="Persona a sacar")
    async def sacar(self, interaction: discord.Interaction, bomb: str, usuario: discord.Member):
        if not can_staff(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, los reclutadores y el Council pueden sacar gente de una bomb.")
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        role = interaction.guild.get_role(b["role_id"])
        had_role = role is not None and role in usuario.roles
        row = await self.run(bdb.remove_member, b["id"], usuario.id)
        if row is None and not had_role:
            return await _say(interaction, f"ℹ️ {usuario.mention} no está en **{discord.utils.escape_markdown(b['name'])}**.")
        if had_role:
            try:
                await usuario.remove_roles(role, reason=f"/bomb sacar {b['name']} por {interaction.user} ({interaction.user.id})")
            except discord.HTTPException as exc:
                return await _say(interaction, f"⚠️ Lo saqué de la lista pero no pude quitarle el rol (revisá que mi rol esté por encima): {exc}")
        await _say(interaction, f"✅ {usuario.mention} salió de **{discord.utils.escape_markdown(b['name'])}**.")

    @bomb_g.command(name="cargar", description="(Staff/Reclutador) Anota en la bomb a todos los que ya tienen su rol y no fueron cargados")
    @app_commands.describe(bomb="La bomb")
    async def cargar(self, interaction: discord.Interaction, bomb: str):
        if not can_staff(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, los reclutadores y el Council pueden cargar una bomb.")
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        role = interaction.guild.get_role(b["role_id"])
        if role is None:
            return await _say(interaction, "❌ El rol de esa bomb ya no existe. Pedile al Council que la edite con `/bomb editar`.")
        listed = {r["user_id"] for r in await self.run(bdb.members, b["id"])}
        nuevos = [m for m in role.members if not m.bot and m.id not in listed]
        name = discord.utils.escape_markdown(b["name"])
        if not nuevos:
            return await _say(interaction, f"✅ Todos los que tienen el rol {role.mention} ya están cargados en **{name}**.")
        added = []
        for m in nuevos:
            if await self.run(bdb.add_member, b["id"], m.id, interaction.user.id):
                added.append(m)
        lines = [f"<@{m.id}>" for m in added]
        text = f"✅ Se cargaron **{len(added)}** personas en **{name}** (ya tenían el rol {role.mention}). Quedan como agregadas por vos.\n"
        chunks = _chunks([text + ", ".join(lines)], limit=1900) if len(text) + len(", ".join(lines)) <= 1900 else _chunks([text] + lines)
        for c in chunks:
            await _say(interaction, c)

    # ---------- listar ----------
    @bomb_g.command(name="listar", description="Lista las bombs, o los integrantes de una")
    @app_commands.describe(bomb="(Opcional) Una bomb para ver sus integrantes")
    async def listar(self, interaction: discord.Interaction, bomb: Optional[str] = None):
        rows = await self.run(bdb.list_bombs, interaction.guild.id)
        if not (can_staff(interaction.user) or any(r["caller_id"] == interaction.user.id for r in rows)):
            return await _say(interaction, "🔒 Solo el Staff, los reclutadores, el Council y los callers pueden ver las bombs.")
        if not bomb:
            if not rows:
                return await _say(interaction, "No hay bombs creadas. El Council las crea con `/bomb crear`.")
            lines = [f"• **{discord.utils.escape_markdown(r['name'])}** · rol <@&{r['role_id']}> · caller <@{r['caller_id']}> · {r['n']} integrantes"
                     for r in rows]
            chunks = _chunks(lines)
            await _say(interaction, f"💣 **Bombs ({len(rows)})**\n" + chunks[0])
            for c in chunks[1:]:
                await _say(interaction, c)
            return
        b = await self._bomb(interaction, bomb)
        if b is None:
            return
        role = interaction.guild.get_role(b["role_id"])
        mem = await self.run(bdb.members, b["id"])
        listed = {r["user_id"] for r in mem}
        lines = []
        for r in mem:
            m = interaction.guild.get_member(r["user_id"])
            flag = "" if (m is not None and role is not None and role in m.roles) else " ⚠️ sin el rol" if m is not None else " ⚠️ ya no está en el servidor"
            lines.append(f"• <@{r['user_id']}> · agregado por <@{r['added_by']}> el <t:{_unix(r['added_at'])}:d>{flag}")
        if role is not None:
            for m in role.members:
                if m.id not in listed and not m.bot:
                    lines.append(f"• <@{m.id}> · tiene el rol pero no fue cargado (usá `/bomb cargar`)")
        head = (f"💣 **{discord.utils.escape_markdown(b['name'])}** · rol <@&{b['role_id']}> · caller <@{b['caller_id']}>\n"
                f"**Integrantes ({len(mem)})**\n")
        if not lines:
            return await _say(interaction, head + "Todavía no hay nadie.")
        chunks = _chunks(lines)
        await _say(interaction, head + chunks[0])
        for c in chunks[1:]:
            await _say(interaction, c)

    for _cmd in (editar, eliminar, agregar, sacar, cargar, listar):
        _cmd.autocomplete("bomb")(bomb_autocomplete)
    del _cmd


async def setup_bombs(bot, db_path: str, guild_id: int) -> BombsCog:
    cog = BombsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
