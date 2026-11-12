"""Gestión de roles de Observación y de CTA: /rol dar, /rol quitar, /rol historial y !votacion.

Quién puede qué (se valida siempre acá, no solo con permisos de Discord):
  • Observación — dar: Staff, Council, Admin, Coaching y Opinólogo · quitar: NADIE con /rol quitar; solo se saca con
    !votacion en el post del foro de VODs de la persona (mayoría de Coaching + Council, o aprobación directa de un Council).
  • Roles de CTA — dar: Staff, Council, Admin y Coaching · quitar: Staff, Council y Admin.
  • /rol masivo — dar o quitar cualquier rol a todos los miembros (o a los que tengan un rol filtro): solo Council y
    Administrador, con confirmación previa.
Cada cambio queda registrado con quién lo hizo."""
import asyncio
import os
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import coaching_db as cdb
import roles_db as rdb
from guildshop import COUNCIL_ROLE_ID, can_manage

OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
COACHING_ROLE_ID = int(os.getenv("COACHING_ROLE_ID", "1566691393726980167"))
OPINOLOGO_ROLE_ID = int(os.getenv("OPINOLOGO_ROLE_ID", "1559747434651852880"))
COACHING_FORUM_ID = int(os.getenv("COACHING_FORUM_ID", "1566695879656607924"))   # foro de VODs (un post por persona)
# Roles de CTA que se pueden manejar (clave del desplegable -> (nombre, id))
CTA_ROLES = {
    "tank": ("Tank", 1260755129754189854),
    "healer": ("Healer", 1260755151296266331),
    "supp": ("Supp", 1260755342472646656),
    "dps": ("DPS", 1260755289062248458),
    "battle_mount": ("Battle Mount", 1469369363739181087),
    "flex": ("Flex", 1558522576743964762),
    "bomb": ("Bomb", int(os.getenv("BOMB_ROLE_ID", "1565982191945519194"))),
}
ROLE_CHOICES = {"observacion": ("Observación", OBSERVACION_ROLE_ID), **CTA_ROLES}
NO_MENTIONS = discord.AllowedMentions.none()
MASIVO_PAUSA = float(os.getenv("ROL_MASIVO_PAUSA", "0.4"))   # segundos entre cambios (para no chocar con el límite de Discord)
MASIVO_PROGRESO = 25                                          # cada cuántos miembros se actualiza el aviso de progreso
# permisos peligrosos: un rol que los tenga no se puede dar en masa
PERMISOS_PELIGROSOS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
                       "manage_webhooks", "mention_everyone", "moderate_members")


def _ids(m) -> set:
    return {getattr(r, "id", None) for r in getattr(m, "roles", [])}


def is_privileged(m) -> bool:
    """Staff, Council o Administrador."""
    return can_manage(m) or STAFF_ROLE_ID in _ids(m)


def can_change(member, key: str, action: str) -> bool:
    """¿Puede `member` dar/quitar el rol `key`? (`action` = 'dar' | 'quitar')"""
    ids = _ids(member)
    if key == "observacion" and action == "quitar":
        return False                      # Observación solo se saca con !votacion
    if is_privileged(member):
        return key in ROLE_CHOICES
    if key == "observacion":
        return bool({COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} & ids)
    if key in CTA_ROLES:
        return action == "dar" and COACHING_ROLE_ID in ids
    return False


def can_use_any(member) -> bool:
    return is_privileged(member) or bool({COACHING_ROLE_ID, OPINOLOGO_ROLE_ID} & _ids(member))


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return 0


async def _say(interaction, text):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


CHOICES = [app_commands.Choice(name=n, value=k) for k, (n, _) in ROLE_CHOICES.items()]


# ---------- !votacion: sacar Observación por votación ----------
def voters(guild, target_id: int) -> set:
    """Quiénes votan: todos los de Coaching y Council (sin bots ni la persona votada)."""
    out = set()
    for rid in (COACHING_ROLE_ID, COUNCIL_ROLE_ID):
        role = guild.get_role(rid) if guild else None
        for m in (getattr(role, "members", None) or []):
            if not m.bot and m.id != target_id:
                out.add(m.id)
    return out


def needed(n: int) -> int:
    """Votos «sí» para mayoría absoluta del total de votantes."""
    return n // 2 + 1


def can_start_vote(member, owner_id: int) -> bool:
    return member.id == owner_id or is_privileged(member) or COACHING_ROLE_ID in _ids(member)


class ObsVoteView(discord.ui.View):
    """Botones persistentes del mensaje de votación (se resuelve la votación por el id del mensaje)."""
    def __init__(self, cog: "RolesMgmtCog", disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if disabled:
            for c in self.children:
                c.disabled = True

    @discord.ui.button(label="Sacar Observación", emoji="✅", style=discord.ButtonStyle.success, custom_id="obsvote:si")
    async def si(self, interaction: discord.Interaction, button):
        await self.cog.cast(interaction, True)

    @discord.ui.button(label="Mantener", emoji="❌", style=discord.ButtonStyle.danger, custom_id="obsvote:no")
    async def no(self, interaction: discord.Interaction, button):
        await self.cog.cast(interaction, False)

    @discord.ui.button(label="Aprobar (Council)", emoji="👑", style=discord.ButtonStyle.primary, custom_id="obsvote:council")
    async def council(self, interaction: discord.Interaction, button):
        await self.cog.council_approve(interaction)


class MasivoConfirmView(discord.ui.View):
    """Confirmación de /rol masivo (solo quien lo pidió puede tocar los botones)."""
    def __init__(self, cog: "RolesMgmtCog", actor_id: int, action: str, role, filtro, motivo: Optional[str]):
        super().__init__(timeout=120.0)
        self.cog, self.actor_id, self.action, self.role, self.filtro, self.motivo = cog, actor_id, action, role, filtro, motivo
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Este mensaje no es tuyo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button):
        if self.used:
            return await _say(interaction, "ℹ️ Esto ya se está procesando.")
        self.used = True
        self.stop()
        await self.cog._masivo_run(interaction, self)

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="✖️ Cancelado: no se cambió ningún rol.", view=None)


def masivo_targets(guild, role, filtro, action: str) -> list:
    """Miembros (sin bots) a los que les falta (dar) o tienen (quitar) el rol, opcionalmente solo los que tengan `filtro`."""
    out = []
    for m in getattr(guild, "members", []):
        if getattr(m, "bot", False):
            continue
        ids = _ids(m)
        if filtro is not None and filtro.id not in ids:
            continue
        if (role.id in ids) == (action == "quitar"):
            out.append(m)
    return out


def masivo_role_problem(guild, actor, role, action: str) -> Optional[str]:
    """Motivo por el que no se puede manejar el rol en masa (None si se puede)."""
    if role.id == getattr(guild, "id", None) or role == getattr(guild, "default_role", None):
        return "❌ No se puede usar con @everyone."
    if getattr(role, "managed", False):
        return f"❌ {role.mention} lo maneja una integración o un bot: Discord no deja asignarlo a mano."
    perms = getattr(role, "permissions", None)
    if action == "dar" and perms is not None and any(getattr(perms, p, False) for p in PERMISOS_PELIGROSOS):
        return f"🔒 {role.mention} tiene permisos de moderación o administración: no se puede dar a todos."
    top = getattr(getattr(guild, "me", None), "top_role", None)
    if top is not None and getattr(role, "position", 0) >= getattr(top, "position", 0):
        return f"❌ {role.mention} está por encima del rol del bot: subí el rol del bot para poder manejarlo."
    actor_top = getattr(actor, "top_role", None)
    if (getattr(guild, "owner_id", None) != actor.id and actor_top is not None
            and getattr(role, "position", 0) >= getattr(actor_top, "position", 0)):
        return f"🔒 {role.mention} está a la altura o por encima de tu rol más alto."
    return None


class RolesMgmtCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        rdb.init_schema(db_path)
        bot.add_view(ObsVoteView(self))
        self._masivo_busy = False

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    # ----- !votacion -----
    def vote_embed(self, vote: dict, guild, votes: dict, result: Optional[str] = None) -> discord.Embed:
        elect = voters(guild, vote["user_id"])
        si = sum(1 for u, v in votes.items() if v and u in elect)
        no = sum(1 for u, v in votes.items() if not v and u in elect)
        desc = (f"¿Se le saca el rol **Observación** a <@{vote['user_id']}>?\n\n"
                f"Votan **Coaching** y **Council** ({len(elect)} personas). Se saca con **{needed(len(elect))}** votos a favor, "
                f"o si un **Council** lo aprueba con 👑.\n\n"
                f"✅ A favor: **{si}** · ❌ En contra: **{no}** · ⏳ Faltan: **{len(elect) - si - no}**\n"
                f"Abierta por <@{vote['started_by']}>. Podés cambiar tu voto mientras esté abierta.")
        if result:
            desc += f"\n\n**{result}**"
        return discord.Embed(title="🗳️ Votación · rol Observación", description=desc,
                             color=discord.Color.green() if result and result.startswith("✅") else discord.Color.blurple())

    @commands.command(name="votacion")
    async def votacion(self, ctx):
        """!votacion en el post del foro de VODs: vota sacarle Observación a la dueña/o del post."""
        ch = ctx.channel
        if not isinstance(ch, discord.Thread) or getattr(ch, "parent_id", None) != COACHING_FORUM_ID:
            return await ctx.send("🗳️ `!votacion` solo se usa dentro del post de VODs de la persona (foro de coaching).", allowed_mentions=NO_MENTIONS)
        post = await asyncio.to_thread(cdb.post_by_thread, self.db_path, ch.id)
        if not post:
            return await ctx.send("❌ Este post no está registrado como post de VODs de nadie.", allowed_mentions=NO_MENTIONS)
        uid = int(post["user_id"])
        if not can_start_vote(ctx.author, uid):
            return await ctx.send("🔒 Pueden abrir la votación la persona del post, Coaching, Staff, Council y Admin.", allowed_mentions=NO_MENTIONS)
        member = ctx.guild.get_member(uid)
        if member is None or OBSERVACION_ROLE_ID not in _ids(member):
            return await ctx.send(f"ℹ️ <@{uid}> no tiene el rol **Observación**.", allowed_mentions=NO_MENTIONS)
        vote_id = await self.run(rdb.create_vote, ch.id, uid, ctx.author.id)
        if vote_id is None:
            return await ctx.send("ℹ️ Ya hay una votación abierta en este post.", allowed_mentions=NO_MENTIONS)
        vote = {"id": vote_id, "user_id": uid, "started_by": ctx.author.id}
        mention_roles = discord.AllowedMentions(roles=True, users=False, everyone=False)
        msg = await ch.send(f"<@&{COACHING_ROLE_ID}> <@&{COUNCIL_ROLE_ID}>", embed=self.vote_embed(vote, ctx.guild, {}),
                            view=ObsVoteView(self), allowed_mentions=mention_roles)
        await self.run(rdb.set_vote_message, vote_id, msg.id)

    async def _load(self, interaction):
        vote = await self.run(rdb.vote_by_message, interaction.message.id)
        if not vote or vote["status"] != "open":
            await _say(interaction, "ℹ️ Esta votación ya está cerrada.")
            return None
        return vote

    async def cast(self, interaction: discord.Interaction, yes: bool):
        vote = await self._load(interaction)
        if not vote:
            return
        guild = interaction.guild
        elect = voters(guild, vote["user_id"])
        if interaction.user.id not in elect:
            return await _say(interaction, "🔒 Solo votan los de **Coaching** y **Council** (y no la persona votada).")
        if not await self.run(rdb.cast_ballot, vote["id"], interaction.user.id, yes):
            return await _say(interaction, "ℹ️ Esta votación ya está cerrada.")
        votes = await self.run(rdb.ballots, vote["id"])
        si = sum(1 for u, v in votes.items() if v and u in elect)
        no = sum(1 for u, v in votes.items() if not v and u in elect)
        need = needed(len(elect))
        if si >= need:
            return await self._finish(interaction, vote, votes, approved=True, actor=None, why=f"mayoría {si}/{len(elect)}")
        if len(elect) - no < need:
            return await self._finish(interaction, vote, votes, approved=False, actor=None, why=f"en contra {no}/{len(elect)}")
        await interaction.response.edit_message(embed=self.vote_embed(vote, guild, votes), view=ObsVoteView(self))
        await _say(interaction, f"🗳️ Votaste **{'a favor' if yes else 'en contra'}**.")

    async def council_approve(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo un **Council** puede aprobarla directamente.")
        vote = await self._load(interaction)
        if not vote:
            return
        if interaction.user.id == vote["user_id"]:
            return await _say(interaction, "❌ No podés aprobar tu propia votación.")
        votes = await self.run(rdb.ballots, vote["id"])
        await self._finish(interaction, vote, votes, approved=True, actor=interaction.user, why="aprobada por Council")

    async def _finish(self, interaction, vote: dict, votes: dict, *, approved: bool, actor, why: str):
        status = "aprobada" if approved else "rechazada"
        if not await self.run(rdb.close_vote, vote["id"], status, actor.id if actor else None):
            return await _say(interaction, "ℹ️ Esta votación ya está cerrada.")
        guild = interaction.guild
        if approved:
            member = guild.get_member(vote["user_id"])
            role = guild.get_role(OBSERVACION_ROLE_ID)
            if member is not None and role is not None and OBSERVACION_ROLE_ID in _ids(member):
                try:
                    await member.remove_roles(role, reason=f"Votación Observación: {why}"[:400])
                except discord.HTTPException:
                    await self.run(rdb.reopen_vote, vote["id"])
                    return await _say(interaction, "❌ Discord no me dejó sacar el rol (revisá *Gestionar roles* y que el bot esté por encima de Observación). La votación sigue abierta.")
            actor_id = actor.id if actor else getattr(getattr(self.bot, "user", None), "id", 0)
            await self.run(rdb.log_change, vote["user_id"], OBSERVACION_ROLE_ID, "Observación", "quitar", actor_id, f"Votación: {why}")
            result = f"✅ Aprobada ({why}): se le sacó Observación a <@{vote['user_id']}>." + (f" Aprobó {actor.mention}." if actor else "")
        else:
            result = f"❌ Rechazada ({why}): <@{vote['user_id']}> sigue con Observación."
        await interaction.response.edit_message(embed=self.vote_embed(vote, guild, votes, result), view=ObsVoteView(self, disabled=True))

    async def _change(self, interaction: discord.Interaction, usuario: discord.Member, rol, motivo: Optional[str], action: str):
        key = getattr(rol, "value", rol)
        actor = interaction.user
        if key not in ROLE_CHOICES:
            return await _say(interaction, "❌ Ese rol no se puede manejar con este comando.")
        if key == "observacion" and action == "quitar":
            return await _say(interaction, "🗳️ El rol **Observación** solo se saca por votación: escribí `!votacion` en el post del foro de VODs de esa persona.")
        if not can_change(actor, key, action):
            verbo = "dar" if action == "dar" else "quitar"
            return await _say(interaction, f"🔒 No tenés permiso para {verbo} el rol **{ROLE_CHOICES[key][0]}**.")
        if usuario.bot:
            return await _say(interaction, "❌ No se puede cambiar roles a un bot.")
        if usuario.id == actor.id:
            return await _say(interaction, "❌ No podés cambiarte los roles a vos mismo.")
        name, rid = ROLE_CHOICES[key]
        guild = interaction.guild
        role = guild.get_role(rid)
        if role is None:
            return await _say(interaction, f"❌ No encuentro el rol **{name}** en el servidor.")
        has = rid in _ids(usuario)
        if action == "dar" and has:
            return await _say(interaction, f"ℹ️ {usuario.mention} ya tiene el rol **{name}**.")
        if action == "quitar" and not has:
            return await _say(interaction, f"ℹ️ {usuario.mention} no tiene el rol **{name}**.")
        me = getattr(guild, "me", None)
        top = getattr(me, "top_role", None)
        if top is not None and getattr(role, "position", 0) >= getattr(top, "position", 0):
            return await _say(interaction, f"❌ El rol **{name}** está por encima del rol del bot: subí el rol del bot para poder manejarlo.")
        try:
            if action == "dar":
                await usuario.add_roles(role, reason=f"{actor} ({actor.id}): {motivo or 'sin motivo'}"[:400])
            else:
                await usuario.remove_roles(role, reason=f"{actor} ({actor.id}): {motivo or 'sin motivo'}"[:400])
        except discord.HTTPException:
            return await _say(interaction, "❌ Discord no me dejó cambiar el rol (revisá que el bot tenga *Gestionar roles* y esté por encima de ese rol).")
        await self.run(rdb.log_change, usuario.id, rid, name, action, actor.id, motivo)
        if action == "dar":
            await _say(interaction, f"✅ {usuario.mention} recibió el rol **{name}**. Lo dio {actor.mention}.")
        else:
            await _say(interaction, f"✅ A {usuario.mention} se le quitó el rol **{name}**. Lo hizo {actor.mention}.")

    rol = app_commands.Group(name="rol", description="Dar o quitar roles (Observación, CTA o masivo); queda registrado", guild_only=True)

    @rol.command(name="dar", description="(Staff/Council/Coaching/Opinólogo) Da el rol de Observación o de CTA a una persona")
    @app_commands.describe(usuario="Persona", rol="Rol a dar", motivo="Motivo (opcional, queda en el registro)")
    @app_commands.choices(rol=CHOICES)
    async def rol_dar(self, interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str], motivo: Optional[str] = None):
        await self._change(interaction, usuario, rol, motivo, "dar")

    @rol.command(name="quitar", description="(Staff/Council/Coaching) Quita el rol de Observación o de CTA a una persona")
    @app_commands.describe(usuario="Persona", rol="Rol a quitar", motivo="Motivo (opcional, queda en el registro)")
    @app_commands.choices(rol=CHOICES)
    async def rol_quitar(self, interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str], motivo: Optional[str] = None):
        await self._change(interaction, usuario, rol, motivo, "quitar")

    @rol.command(name="masivo", description="(Council/Admin) Da o quita un rol a todos los miembros (o a los que tengan otro rol)")
    @app_commands.describe(accion="Dar o quitar", rol="Rol a dar o quitar", filtro="Solo a quienes tengan este rol (opcional)",
                           motivo="Motivo (opcional, queda en el registro)")
    @app_commands.choices(accion=[app_commands.Choice(name="Dar", value="dar"), app_commands.Choice(name="Quitar", value="quitar")])
    async def rol_masivo(self, interaction: discord.Interaction, accion: app_commands.Choice[str], rol: discord.Role,
                         filtro: Optional[discord.Role] = None, motivo: Optional[str] = None):
        action = getattr(accion, "value", accion)
        if interaction.guild is None or not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo **Council** o un **Administrador** pueden usar `/rol masivo`.")
        if self._masivo_busy:
            return await _say(interaction, "⏳ Ya hay un `/rol masivo` en curso. Esperá a que termine.")
        problem = masivo_role_problem(interaction.guild, interaction.user, rol, action)
        if problem:
            return await _say(interaction, problem)
        targets = masivo_targets(interaction.guild, rol, filtro, action)
        quienes = f"los miembros con {filtro.mention}" if filtro else "todos los miembros"
        if not targets:
            ya = "ya tienen" if action == "dar" else "no tienen"
            return await _say(interaction, f"ℹ️ No hay nadie para cambiar: {quienes} {ya} {rol.mention}.")
        verbo = "Dar" if action == "dar" else "Quitar"
        await interaction.response.send_message(
            f"⚠️ **{verbo}** {rol.mention} a {quienes}: son **{len(targets)}** miembro{'s' if len(targets) != 1 else ''} "
            f"(sin contar bots ni a quien ya {'lo tiene' if action == 'dar' else 'no lo tiene'}).\n"
            f"Tarda unos {max(1, round(len(targets) * MASIVO_PAUSA))} s. ¿Confirmás?",
            view=MasivoConfirmView(self, interaction.user.id, action, rol, filtro, motivo), ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _masivo_run(self, interaction: discord.Interaction, view: "MasivoConfirmView"):
        if self._masivo_busy:
            return await interaction.response.edit_message(content="⏳ Ya hay un `/rol masivo` en curso. Esperá a que termine.", view=None)
        guild, actor, role, action = interaction.guild, interaction.user, view.role, view.action
        problem = masivo_role_problem(guild, actor, role, action)          # se revisa otra vez: pudo cambiar mientras tanto
        if problem:
            return await interaction.response.edit_message(content=problem, view=None)
        targets = masivo_targets(guild, role, view.filtro, action)
        self._masivo_busy = True
        try:
            await interaction.response.edit_message(content=f"⏳ Procesando {len(targets)} miembros…", view=None)
            reason = f"/rol masivo por {actor} ({actor.id}): {view.motivo or 'sin motivo'}"[:400]
            log_reason = f"Masivo: {view.motivo}" if view.motivo else "Masivo"
            ok, failed = 0, 0
            for n, m in enumerate(targets, 1):
                try:
                    if action == "dar":
                        await m.add_roles(role, reason=reason)
                    else:
                        await m.remove_roles(role, reason=reason)
                    ok += 1
                    await self.run(rdb.log_change, m.id, role.id, role.name, action, actor.id, log_reason)
                except discord.HTTPException:
                    failed += 1
                if n % MASIVO_PROGRESO == 0 and n < len(targets):
                    try:
                        await interaction.edit_original_response(content=f"⏳ Procesando… {n}/{len(targets)}")
                    except Exception:
                        pass
                if MASIVO_PAUSA:
                    await asyncio.sleep(MASIVO_PAUSA)
        finally:
            self._masivo_busy = False
        hecho = "recibieron" if action == "dar" else "perdieron"
        text = f"✅ {ok} miembro{'s' if ok != 1 else ''} {hecho} {role.mention}. Lo hizo {actor.mention}."
        if failed:
            text += f"\n⚠️ {failed} no se pudieron cambiar (revisá que el bot tenga *Gestionar roles* y esté por encima del rol)."
        await _say(interaction, text)

    @rol.command(name="historial", description="(Staff/Council/Coaching/Opinólogo) Quién dio o quitó roles")
    @app_commands.describe(usuario="Persona (si no, se muestran los últimos movimientos)")
    async def rol_historial(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if not can_use_any(interaction.user):
            return await _say(interaction, "🔒 No tenés permiso para ver el historial de roles.")
        rows = await self.run(rdb.history, usuario.id if usuario else None, 15)
        if not rows:
            return await _say(interaction, "No hay movimientos registrados.")
        lines = [f"`{'➕' if r['action'] == 'dar' else '➖'}` <@{r['user_id']}> · **{discord.utils.escape_markdown(r['role_name'])}** · "
                 f"por <@{r['actor_id']}> · <t:{_unix(r['created_at'])}:f>" + (f" · {r['reason']}" if r["reason"] else "") for r in rows]
        head = f"**Historial de roles{' de ' + usuario.mention if usuario else ''}**\n"
        await _say(interaction, (head + "\n".join(lines))[:1900])


async def setup_rolesmgmt(bot, db_path: str, guild_id: int) -> RolesMgmtCog:
    cog = RolesMgmtCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
