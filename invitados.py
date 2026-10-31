"""Sistema de invitados: el rol Legacy invita a alguien (/invitar), los miembros ven quién invitó (/quien-invito),
el staff los lista (/invitados), y /info describe sistemas."""
import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx, utcnow_iso
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
LEGACY_ROLE_ID = int(os.getenv("LEGACY_ROLE_ID", "1465833385804038427"))
GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", "1565993695461777468"))
PUBLIC_ROLE_ID = int(os.getenv("PUBLIC_ROLE_ID", "1266805315547041902"))
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
REASON_MAX = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS guests (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    invited_by INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    ended_at   TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""

INFO_TEXTS = {
    "invitados": ("👥 Sistema de invitados",
                  "Permite que alguien de confianza entre a la comunidad sin ser miembro de la guild.\n\n"
                  "• Solo quienes tienen el rol **Legacy** pueden invitar con `/invitar usuario motivo`.\n"
                  "• Al invitado se le saca el rol **Public** y recibe el rol **Invitado**.\n"
                  "• Queda registrado quién lo invitó y por qué: los miembros pueden consultarlo con `/quien-invito usuario`.\n"
                  "• El staff ve la lista completa con `/invitados`.\n"
                  "• Quien invita responde por su invitado."),
}


def _roles(m) -> set:
    return {r.id for r in getattr(m, "roles", [])}


def add_guest(db_path: str, guild_id: int, user_id: int, invited_by: int, reason: str) -> bool:
    """False si ya es invitado activo."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT active FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
            if r is not None and r["active"]:
                return False
            conn.execute("INSERT OR REPLACE INTO guests(guild_id,user_id,invited_by,reason,created_at,active,ended_at) VALUES (?,?,?,?,?,1,NULL)",
                         (guild_id, user_id, invited_by, reason, utcnow_iso()))
            return True
    finally:
        conn.close()


def get_guest(db_path: str, guild_id: int, user_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_guests(db_path: str, guild_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM guests WHERE guild_id=? AND active=1 ORDER BY created_at", (guild_id,))]
    finally:
        conn.close()


def end_guest(db_path: str, guild_id: int, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE guests SET active=0, ended_at=? WHERE guild_id=? AND user_id=? AND active=1", (utcnow_iso(), guild_id, user_id))
    finally:
        conn.close()


def remove_guest_row(db_path: str, guild_id: int, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    finally:
        conn.close()


async def _say(interaction, text, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def _unix(iso: str) -> int:
    d = datetime.fromisoformat(iso)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


class InvitadosCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        conn = connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    # ---------- /invitar ----------
    @app_commands.command(name="invitar", description="(Legacy) Invitar a alguien: pasa de Public a Invitado")
    @app_commands.describe(usuario="La persona de Discord que invitás", quien_es="Quién es / por qué lo invitás")
    @app_commands.guild_only()
    async def invitar(self, interaction: discord.Interaction, usuario: discord.Member, quien_es: str):
        me = interaction.user
        perms = getattr(me, "guild_permissions", None)
        if LEGACY_ROLE_ID not in _roles(me) and not (perms is not None and perms.administrator):
            return await _say(interaction, "🔒 Solo quienes tienen el rol **Legacy** pueden invitar.")
        reason = " ".join((quien_es or "").split())
        if not reason:
            return await _say(interaction, "❌ Tenés que indicar quién es el invitado.")
        if len(reason) > REASON_MAX:
            return await _say(interaction, f"❌ Máximo {REASON_MAX} caracteres.")
        if usuario.bot or usuario.id == me.id:
            return await _say(interaction, "❌ No podés invitar a un bot ni a vos mismo.")
        have = _roles(usuario)
        if MIEMBRO_ROLE_ID in have:
            return await _say(interaction, "❌ Esa persona ya es miembro de la guild.")
        guest_role = interaction.guild.get_role(GUEST_ROLE_ID)
        if guest_role is None:
            return await _say(interaction, "❌ No encuentro el rol Invitado en el servidor.")
        if GUEST_ROLE_ID in have:
            g = await self.run(get_guest, interaction.guild.id, usuario.id)
            by = f" (invitado por <@{g['invited_by']}>)" if g and g["active"] else ""
            return await _say(interaction, f"❌ {usuario.mention} ya es invitado{by}.")
        await interaction.response.defer(ephemeral=True)
        try:
            await usuario.add_roles(guest_role, reason=f"Invitado por {me} ({me.id}): {reason}")
        except discord.HTTPException as exc:
            return await _say(interaction, f"❌ No pude darle el rol Invitado (revisá que mi rol esté por encima y tenga Gestionar roles): {exc}")
        warn = ""
        public = interaction.guild.get_role(PUBLIC_ROLE_ID)
        if public is not None and PUBLIC_ROLE_ID in have:
            try:
                await usuario.remove_roles(public, reason=f"Invitado por {me} ({me.id})")
            except discord.HTTPException:
                warn = "\n⚠️ Quedó con el rol Invitado pero no pude sacarle Public; avisale al staff."
        try:
            ok = await self.run(add_guest, interaction.guild.id, usuario.id, me.id, reason)
        except Exception:
            traceback.print_exc()
            ok = False
            warn += "\n⚠️ No pude guardar el registro; avisale al staff."
        await _say(interaction, f"✅ {usuario.mention} ahora es invitado.{warn}")
        try:
            await interaction.channel.send(f"🎟️ {me.mention} invitó a {usuario.mention}: *{reason}*", allowed_mentions=NO_MENTIONS)
        except Exception:
            pass

    # ---------- /quien-invito ----------
    @app_commands.command(name="quien-invito", description="Ver quién invitó a alguien y por qué")
    @app_commands.describe(usuario="El invitado")
    @app_commands.guild_only()
    async def quien_invito(self, interaction: discord.Interaction, usuario: discord.Member):
        me = interaction.user
        if MIEMBRO_ROLE_ID not in _roles(me) and not can_manage(me) and STAFF_ROLE_ID not in _roles(me):
            return await _say(interaction, "🔒 Solo los miembros de la guild pueden usar este comando.")
        g = await self.run(get_guest, interaction.guild.id, usuario.id)
        if g is None:
            return await _say(interaction, f"{usuario.mention} no figura como invitado.")
        estado = "invitado actual" if g["active"] else "ya no es invitado"
        await _say(interaction, f"🎟️ {usuario.mention} ({estado})\nInvitado por: <@{g['invited_by']}>\nMotivo: {g['reason']}\nFecha: <t:{_unix(g['created_at'])}:f>")

    # ---------- /invitados ----------
    @app_commands.command(name="invitados", description="(Staff) Listar los invitados actuales")
    @app_commands.guild_only()
    async def invitados(self, interaction: discord.Interaction):
        me = interaction.user
        if not (can_manage(me) or STAFF_ROLE_ID in _roles(me)):
            return await _say(interaction, "🔒 Solo el Staff puede ver la lista de invitados.")
        rows = await self.run(list_guests, interaction.guild.id)
        if not rows:
            return await _say(interaction, "No hay invitados actualmente.")
        lines = [f"• <@{r['user_id']}> · invitado por <@{r['invited_by']}> el <t:{_unix(r['created_at'])}:d> · {r['reason']}" for r in rows]
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        await _say(interaction, f"**Invitados ({len(rows)})**\n" + chunks[0])
        for c in chunks[1:]:
            await _say(interaction, c)

    # ---------- /info ----------
    @app_commands.command(name="info", description="Explica cómo funciona un sistema del servidor")
    @app_commands.describe(tema="Qué sistema querés conocer")
    @app_commands.choices(tema=[app_commands.Choice(name="invitados", value="invitados")])
    async def info(self, interaction: discord.Interaction, tema: app_commands.Choice[str]):
        value = getattr(tema, "value", tema)
        title, text = INFO_TEXTS[value]
        await interaction.response.send_message(embed=discord.Embed(title=title, description=text, color=discord.Color.blurple()), ephemeral=True)

    # ---------- si pierde el rol Invitado (p. ej. lo hacen miembro) el registro queda como histórico ----------
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if GUEST_ROLE_ID in _roles(before) and GUEST_ROLE_ID not in _roles(after):
            try:
                await self.run(end_guest, after.guild.id, after.id)
            except Exception:
                traceback.print_exc()


async def setup_invitados(bot, db_path: str, guild_id: int) -> InvitadosCog:
    cog = InvitadosCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
