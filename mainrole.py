"""Rol main de los miembros: /mainrole sync (reconstruye desde los logs de reclutamiento) · /mainrole set (corrección a mano)."""
import asyncio
import csv
import io
import os
import re
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import botsettings as bs
import ctautil as cu
import mainrole_db as mdb
from guildshop import can_manage

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
RECRUITER_ROLE_ID = int(os.getenv("RECRUITER_ROLE_ID", "1257896905099444354"))
NO_MENTIONS = discord.AllowedMentions.none()
BOMB_ROLE_ID = int(os.getenv("BOMB_ROLE_ID", str(cu.BOMB_ROLE_ID)))   # quien tiene este rol no entra al /top rolsec

ACCEPTED_TITLE = "POSTULACIÓN ACEPTADA"
APPLICANT_FIELD = "👤 Postulante"
MAIN_FIELD = "⚔ Rol main"
ASSIGNED_FIELD = "🎭 Rol asignado"
CONFIRMED_FIELD = "⚔ Rol main confirmado"   # lo eligió el reclutador al aceptar
_MENTION = re.compile(r"<@!?(\d+)>")
ROLE_CHOICES = [app_commands.Choice(name=name, value=str(rid)) for rid, name in mdb.MAIN_ROLES.items()]


def can_edit(member) -> bool:
    return can_manage(member) or any(getattr(r, "id", None) in (STAFF_ROLE_ID, RECRUITER_ROLE_ID) for r in getattr(member, "roles", []))


async def _say(interaction, text, **kw):
    if interaction.response.is_done():
        return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)


def parse_accepted_log(message) -> Optional[dict]:
    """Datos de un log «POSTULACIÓN ACEPTADA» del bot: {user_id, raw, assigned}. None si el mensaje no es uno de esos."""
    for emb in (getattr(message, "embeds", None) or []):
        if ACCEPTED_TITLE not in (getattr(emb, "title", None) or "").upper():
            continue
        fields = {f.name: (f.value or "") for f in emb.fields}
        m = _MENTION.search(fields.get(APPLICANT_FIELD, ""))
        if m is None:
            return None
        raw = fields.get(MAIN_FIELD, "").strip()
        return {"user_id": int(m.group(1)), "raw": "" if raw == "—" else raw, "assigned": fields.get(ASSIGNED_FIELD, "").strip(),
                "confirmed": fields.get(CONFIRMED_FIELD, "").strip()}
    return None


class MainRoleCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self._syncing = False
        mdb.init_schema(db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    async def sync_from_logs(self, channel) -> tuple:
        """Recorre el canal de log del más nuevo al más viejo: por persona vale su aceptación MÁS RECIENTE.
        Devuelve (guardados, ya_cargados, filas_para_revisar)."""
        seen, saved, kept, review = set(), 0, 0, []
        me = getattr(self.bot.user, "id", None)
        async for msg in channel.history(limit=None, oldest_first=False):
            if me is not None and getattr(msg.author, "id", None) != me:
                continue
            data = parse_accepted_log(msg)
            if data is None or data["user_id"] in seen:
                continue
            seen.add(data["user_id"])
            rid, source = mdb.parse_main(data["confirmed"]) or mdb.parse_main(data["raw"]), "log"
            if rid is None:
                rid, source = mdb.parse_main(data["assigned"]), "log_respaldo"
            if rid is None:
                review.append((data, "sin rol main", msg.jump_url))
                continue
            if await self.run(mdb.set_main, data["user_id"], rid, source, data["raw"] or None, None, msg.jump_url, overwrite=False):
                saved += 1
                if source == "log_respaldo":
                    review.append((data, f"respaldo: {mdb.role_name(rid)} (de «Rol asignado»)", msg.jump_url))
            else:
                kept += 1
        return saved, kept, review

    def review_csv(self, guild, rows) -> discord.File:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "usuario", "en_servidor", "estado", "rol_main_escrito", "rol_asignado", "log"])
        for data, estado, url in rows:
            m = guild.get_member(data["user_id"]) if guild is not None else None
            w.writerow([data["user_id"], str(m) if m else "", "si" if m else "no", estado, data["raw"], data["assigned"], url])
        return discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename="rol_main_revisar.csv")

    # ---------- /top rolsec ----------
    async def send_top(self, interaction: discord.Interaction):
        """`/top rolsec`: los que menos juegan su rol main (miembros actuales con al menos MIN_CTAS_FOR_TOP CTA cerradas)."""
        from paging import PagedView, chunk
        guild = interaction.guild
        rows = []
        for d in await self.run(mdb.secondary_top):
            m = guild.get_member(d["user_id"]) if guild is not None else None
            if guild is not None and (m is None or any(getattr(r, "id", None) == BOMB_ROLE_ID for r in m.roles)):
                continue                                   # se fue del servidor, o tiene el rol Bomb
            rows.append(d)

        def label(key) -> str:
            if not isinstance(key, int):
                return str(key)
            role = guild.get_role(key) if guild is not None else None
            return mdb.MAIN_ROLES.get(key) or cu.PLAYED_ROLES.get(key) or (role.name if role is not None else f"Rol {key}")

        def line(i, d) -> str:
            other = f" · juega más **{label(d['top_other_id'])}** ({d['top_other_n']})" if d["top_other_id"] is not None and d["top_other_n"] > d["main"] else ""
            return (f"`{i}.` <@{d['user_id']}> — **{d['pct']:.0f}%** de {label(d['role_id'])} "
                    f"({d['main']} de {d['total']} CTA){other}")

        pages = chunk(list(enumerate(rows, start=1)), 10)
        embeds = []
        for n, page in enumerate(pages, start=1):
            emb = discord.Embed(title="⚔ Top rol secundario — los que menos juegan su main", color=discord.Color.orange(),
                                description="\n".join(line(i, d) for i, d in page)
                                or f"Nadie con rol main cargado y al menos {mdb.MIN_CTAS_FOR_TOP} CTA cerradas.")
            emb.set_footer(text=f"CTA cerradas en las que estuvo anotado · mínimo {mdb.MIN_CTAS_FOR_TOP} CTA"
                                + (f" · Página {n}/{len(pages)}" if len(pages) > 1 else ""))
            embeds.append(emb)
        if len(embeds) > 1:
            view = PagedView(embeds, owner_id=interaction.user.id)
            await interaction.response.send_message(embed=embeds[0], view=view, allowed_mentions=NO_MENTIONS)
            try:
                view.message = await interaction.original_response()
            except Exception:
                pass
        else:
            await interaction.response.send_message(embed=embeds[0], allowed_mentions=NO_MENTIONS)

    # ---------- comandos ----------
    mr_g = app_commands.Group(name="mainrole", description="Rol main de los miembros", guild_only=True)

    @mr_g.command(name="sync", description="(Council) Carga el rol main de todos desde los logs de reclutamiento aceptados")
    async def mr_sync(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede usar esto.")
        if self._syncing:
            return await _say(interaction, "⏳ Ya hay una sincronización en curso.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._syncing = True
        try:
            ch = interaction.guild.get_channel(bs.get("log")) or await interaction.guild.fetch_channel(bs.get("log"))
            saved, kept, review = await self.sync_from_logs(ch)
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude leer el canal de logs de reclutamiento (¿está configurado y tengo permiso de leer el historial?).")
        finally:
            self._syncing = False
        missing = sum(1 for _d, e, _u in review if e == "sin rol main")
        text = (f"✅ Rol main cargado a **{saved}** personas desde los logs.\n"
                f"🔒 **{kept}** ya tenían uno elegido por un reclutador o a mano (no se tocaron).\n"
                f"⚠️ **{missing}** sin rol main reconocible · **{len(review) - missing}** cargadas por respaldo (revisalas).")
        if review:
            text += "\nCorregí con `/mainrole set`."
            return await _say(interaction, text, file=self.review_csv(interaction.guild, review))
        await _say(interaction, text)

    @mr_g.command(name="set", description="(Staff/Council/Reclutador) Cambia el rol main de un miembro")
    @app_commands.describe(usuario="Miembro", rol="Rol main")
    @app_commands.choices(rol=ROLE_CHOICES)
    async def mr_set(self, interaction: discord.Interaction, usuario: discord.Member, rol: app_commands.Choice[str]):
        if not can_edit(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council y los reclutadores pueden cambiar el rol main.")
        await self.run(mdb.set_main, usuario.id, int(rol.value), "manual", None, interaction.user.id, None)
        await _say(interaction, f"✅ Rol main de {usuario.mention}: **{rol.name}**.")


async def setup_mainrole(bot, db_path: str, guild_id: int) -> MainRoleCog:
    cog = MainRoleCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
