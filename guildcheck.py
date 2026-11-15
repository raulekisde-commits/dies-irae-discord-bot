"""/miembros: control de miembros guild de Albion ↔ Discord en un solo bloque, más un panel con botones.

  /miembros panel            Publica el panel con un botón por cada reporte (los resultados le llegan solo a quien toca).
  /miembros sin-vincular     Con rol Miembro y sin personaje registrado (no lista invitados premium).
  /miembros fuera-discord    En la guild y sin Discord vinculado (con o sin el archivo exportado; no lista alters).
  /miembros inactivos        Más de N días sin conectarse a Albion (según el último archivo de la guild subido).
  /miembros kick-lista       Candidatos a expulsar por motivo: inactivos, poca asistencia a CTA, faltan VODs; marca quién tiene /ausencia.
  /miembros fuera-guild      Registrados cuyo personaje ya no está en la guild, con listas para expulsar.
  /miembros canal-avisos     Canal donde el bot avisa solo cuando alguien sale de la guild, con botones Expulsar / Ignorar.

Los cuatro reportes que ya existían (sin-albion, sin-discord, guild-sin-discord, fuera-de-guild) siguen en registration.py
y se llaman desde acá; ya no son comandos sueltos."""
import asyncio
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import coaching_db as cdb
import guildcheck_db as gdb
from guildshop import can_manage
from registration import YesNoView, can_admin_registro

MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
MAX_EXPORT_BYTES = 2_000_000
LEAVE_CHECK_MINUTES = 30
LEAVE_CONFIRM_PER_ROUND = 10       # consultas a la API por ronda para confirmar el gremio actual
ROSTER_DROP_GUARD = 0.7            # si la lista de la guild baja de golpe más de un 30 %, esa ronda no se avisa nada
NO_MENTIONS = discord.AllowedMentions.none()


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def _chunks(lines: List[str], limit: int = 1800) -> List[str]:
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        out.append(cur)
    return out


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _is_admin(m) -> bool:
    perms = getattr(m, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ============================================================
# Vistas persistentes (sobreviven a reinicios: custom_id fijo + el mensaje se busca en la base)
# ============================================================
class PanelView(discord.ui.View):
    def __init__(self, cog: "GuildCheckCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Sin vincular", emoji="🔗", style=discord.ButtonStyle.secondary, custom_id="gchk:panel:sinvinc", row=0)
    async def b_sin_vincular(self, interaction: discord.Interaction, button):
        await self.cog.do_sin_vincular(interaction, False)

    @discord.ui.button(label="En la guild sin Discord", emoji="🎮", style=discord.ButtonStyle.secondary, custom_id="gchk:panel:fueradiscord", row=0)
    async def b_fuera_discord(self, interaction: discord.Interaction, button):
        await self.cog.do_fuera_discord(interaction, None)

    @discord.ui.button(label="Inactivos +7 días", emoji="💤", style=discord.ButtonStyle.secondary, custom_id="gchk:panel:inactivos", row=0)
    async def b_inactivos(self, interaction: discord.Interaction, button):
        await self.cog.do_inactivos(interaction, gdb.INACTIVE_DAYS, None)

    @discord.ui.button(label="Lista para expulsar", emoji="👢", style=discord.ButtonStyle.danger, custom_id="gchk:panel:kick", row=1)
    async def b_kick(self, interaction: discord.Interaction, button):
        await self.cog.do_kick_lista(interaction, "anterior")

    @discord.ui.button(label="Salieron de la guild", emoji="🚪", style=discord.ButtonStyle.danger, custom_id="gchk:panel:fueraguild", row=1)
    async def b_fuera_guild(self, interaction: discord.Interaction, button):
        await self.cog.do_fuera_guild(interaction)


class LeaveAlertView(discord.ui.View):
    """Botones del aviso «salió de la guild». La persona se busca por el mensaje, así sirven después de reiniciar."""

    def __init__(self, cog: "GuildCheckCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Expulsar del Discord", emoji="👢", style=discord.ButtonStyle.danger, custom_id="gchk:leave:kick")
    async def b_kick(self, interaction: discord.Interaction, button):
        await self.cog.alert_kick(interaction)

    @discord.ui.button(label="Ignorar", style=discord.ButtonStyle.secondary, custom_id="gchk:leave:ignore")
    async def b_ignore(self, interaction: discord.Interaction, button):
        await self.cog.alert_ignore(interaction)


# ============================================================
# Cog
# ============================================================
class GuildCheckCog(commands.Cog):
    miembros_g = app_commands.Group(name="miembros", description="Control de miembros: guild de Albion vs Discord", guild_only=True)

    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await self.run(gdb.init_schema)
        self.bot.add_view(PanelView(self))
        self.bot.add_view(LeaveAlertView(self))
        if not self.leave_loop.is_running():
            self.leave_loop.start()

    async def cog_unload(self):
        self.leave_loop.cancel()

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, self.db_path, *a, **kw)

    def reg(self):
        return self.bot.get_cog("RegistrationCog")

    async def _need_reg(self, interaction):
        reg = self.reg()
        if reg is None:
            await _say(interaction, "❌ El módulo de registro no está cargado.")
        return reg

    # ---------- reportes que ya existían (viven en registration.py) ----------
    async def do_sin_vincular(self, interaction, mencionar: bool = False):
        reg = await self._need_reg(interaction)
        if reg is not None:
            await reg.sin_albion(interaction, mencionar)

    async def do_fuera_discord(self, interaction, archivo: Optional[discord.Attachment]):
        reg = await self._need_reg(interaction)
        if reg is None:
            return
        if archivo is not None:
            await reg.guild_sin_discord(interaction, archivo)
        else:
            await reg.sin_discord(interaction)

    async def do_fuera_guild(self, interaction):
        reg = await self._need_reg(interaction)
        if reg is not None:
            await reg.fuera_de_guild(interaction)

    # ---------- inactivos ----------
    async def _load_export(self, interaction, archivo) -> bool:
        if (getattr(archivo, "size", 0) or 0) > MAX_EXPORT_BYTES:
            await _say(interaction, "❌ El archivo es demasiado grande (máx. 2 MB).")
            return False
        try:
            n = await self.run(gdb.save_from_export, await archivo.read())
        except Exception:
            traceback.print_exc()
            n = 0
        if not n:
            await _say(interaction, "❌ No pude leer el archivo. Tiene que ser el export de la guild (Character Name / Last Seen / Roles).")
            return False
        return True

    async def do_inactivos(self, interaction, dias: int = gdb.INACTIVE_DAYS, archivo: Optional[discord.Attachment] = None):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        if archivo is not None and not await self._load_export(interaction, archivo):
            return
        snap, at = await self.run(gdb.snapshot)
        if not snap or at is None:
            return await _say(interaction, "ℹ️ Todavía no hay lista de la guild cargada. Subí el archivo con `/miembros inactivos archivo:` "
                                           "o la lista de conectados en el hilo de stats de una CTA.")
        dias = max(1, int(dias or gdb.INACTIVE_DAYS))
        cutoff = at - timedelta(days=dias)
        owner = gdb.owner_of_names(await self.run(gdb.chars_by_user))
        rows = sorted((v for v in snap.values() if v[1] < cutoff), key=lambda v: v[1])
        esc = discord.utils.escape_markdown
        head = (f"💤 **{len(rows)}** de {len(snap)} personajes de la guild llevan más de **{dias} días** sin conectarse "
                f"(archivo del <t:{_ts(at)}:f>).")
        if datetime.now(timezone.utc) - at > timedelta(days=2):
            head += "\n⚠️ El archivo tiene más de 2 días: conviene subir uno nuevo."
        lines = [head]
        for name, seen in rows:
            uid = owner.get(gdb.norm(name))
            who = f"<@{uid}>" if uid else "sin Discord"
            lines.append(f"• **{esc(name)}** → {who} · última conexión <t:{_ts(seen)}:R>")
        if not rows:
            lines.append("✅ Nadie.")
        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- lista para expulsar ----------
    def _guest_ids(self, guild_id: int) -> set:
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                return {int(r[0]) for r in conn.execute("SELECT user_id FROM guests WHERE guild_id=? AND active=1", (guild_id,))}
            finally:
                conn.close()
        except sqlite3.Error:
            return set()

    async def do_kick_lista(self, interaction, semana: str = "anterior"):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        role = guild.get_role(MIEMBRO_ROLE_ID)
        if role is None:
            return await _say(interaction, "❌ No encuentro el rol Miembro en el servidor.")
        current = semana == "actual"
        start, end = gdb.week_bounds(0 if current else -1)
        guests = await asyncio.to_thread(self._guest_ids, guild.id)
        pool = [m for m in role.members if not getattr(m, "bot", False) and not _is_admin(m) and not can_admin_registro(m) and m.id not in guests]
        chars = await self.run(gdb.chars_by_user)
        snap, at = await self.run(gdb.snapshot)
        seen = gdb.last_seen_by_user(snap, chars)
        n_ctas, att = await self.run(gdb.cta_attendance, guild.id, start, end, chars)
        need_cta = min(gdb.CTA_WEEKLY_MIN, n_ctas)
        vods = await self.run(cdb.count_between, start, end, only_obs=True)
        obs_role = guild.get_role(OBSERVACION_ROLE_ID)
        observers = {m.id for m in (obs_role.members if obs_role else [])}
        absent = await self.run(gdb.ausencias_en, guild.id, start, end)

        inactive, low_cta, low_vods = [], [], []
        for m in pool:
            if at is not None and m.id in seen and seen[m.id][1] < at - timedelta(days=gdb.INACTIVE_DAYS):
                inactive.append((m, seen[m.id]))
            if need_cta and att.get(m.id, 0) < need_cta:
                low_cta.append((m, att.get(m.id, 0)))
            if m.id in observers and vods.get(m.id, 0) < cdb.WEEKLY_MIN:
                low_vods.append((m, vods.get(m.id, 0)))
        reasons = {}
        for lst in (inactive, low_cta, low_vods):
            for m, _x in lst:
                reasons[m.id] = reasons.get(m.id, 0) + 1

        esc = discord.utils.escape_markdown

        def tags(m) -> str:
            out = ""
            a = absent.get(m.id)
            if a:
                txt = esc(str(a.get("texto") or ""))[:60]
                out += f"\n   🟡 **Tiene /ausencia** hasta <t:{int(datetime.fromisoformat(a['hasta']).timestamp())}:D>" + (f": «{txt}»" if txt else "")
            if reasons.get(m.id, 0) > 1:
                out += f" · ⚠️ {reasons[m.id]} motivos"
            joined = getattr(m, "joined_at", None)
            if joined and joined > start:
                out += f" · 🆕 entró al Discord <t:{_ts(joined)}:R>"
            return out

        def order(lst, key):
            return sorted(lst, key=lambda mx: (mx[0].id in absent, key(mx), mx[0].display_name.casefold()))

        label = f"<t:{_ts(start)}:D> → <t:{_ts(end - timedelta(seconds=1))}:D>"
        total = len(reasons)
        with_abs = sum(1 for u in reasons if u in absent)
        lines = [f"👢 **Lista para expulsar** · semana {'en curso' if current else 'anterior'} ({label})",
                 f"**{total}** personas con al menos un motivo" + (f" · 🟡 {with_abs} con /ausencia (revisar antes de expulsar)" if with_abs else "") + ".",
                 "No incluye Staff, Council, administradores, bots ni invitados."]
        if current:
            lines.append("⚠️ La semana todavía no terminó: la asistencia y los VODs pueden subir.")

        lines += ["", f"**💤 Más de {gdb.INACTIVE_DAYS} días sin conectarse a Albion ({len(inactive)})**"]
        if at is None:
            lines.append("ℹ️ No hay lista de la guild cargada: subila con `/miembros inactivos archivo:`.")
        else:
            lines += [f"• <@{m.id}> (**{esc(name)}**) · última conexión <t:{_ts(when)}:R>{tags(m)}"
                      for m, (name, when) in order(inactive, lambda mx: mx[1][1])] or ["✅ Nadie."]

        lines += ["", f"**⚔️ Poca asistencia a CTA: menos de {need_cta} de {n_ctas} ({len(low_cta)})**"]
        if not n_ctas:
            lines.append("ℹ️ No hubo CTAs esa semana: no se evalúa.")
        else:
            lines.append("Cuenta si se anotó y además estuvo en el canal de voz o aparece en el killboard.")
            lines += [f"• <@{m.id}> · **{n}/{need_cta}** CTA{tags(m)}" for m, n in order(low_cta, lambda mx: mx[1])] or ["✅ Nadie."]

        lines += ["", f"**🎥 Faltan VODs de coaching: menos de {cdb.WEEKLY_MIN} (solo Observación) ({len(low_vods)})**"]
        lines += [f"• <@{m.id}> · **{n}/{cdb.WEEKLY_MIN}** VODs{tags(m)}" for m, n in order(low_vods, lambda mx: mx[1])] or ["✅ Nadie."]

        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- canal de avisos ----------
    async def do_canal_avisos(self, interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        if interaction.guild is None or not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        if desactivar:
            await self.run(gdb.set_setting, gdb.CHANNEL_KEY, None)
            return await _say(interaction, "🚫 Avisos de salida de la guild **desactivados**.")
        channel = canal or interaction.channel
        if channel is None or not hasattr(channel, "send"):
            return await _say(interaction, "❌ No puedo usar ese canal.")
        me = interaction.guild.me
        if me is not None and hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                return await _say(interaction, f"❌ No tengo permiso para escribir en {channel.mention}.")
        await self.run(gdb.set_setting, gdb.CHANNEL_KEY, str(channel.id))
        await _say(interaction, f"✅ Cuando un registrado salga de la guild de Albion, aviso en {channel.mention} con botones para expulsarlo o ignorar. "
                                f"Se revisa cada {LEAVE_CHECK_MINUTES} min.")

    # ---------- detección automática de quien sale de la guild ----------
    @tasks.loop(minutes=LEAVE_CHECK_MINUTES)
    async def leave_loop(self):
        try:
            await self.check_leavers()
        except Exception:
            traceback.print_exc()

    @leave_loop.before_loop
    async def _leave_ready(self):
        await self.bot.wait_until_ready()

    async def check_leavers(self) -> int:
        """Avisa (una vez por persona) a quien tiene personaje registrado que ya no figura en la guild. Devuelve cuántos avisos mandó."""
        reg = self.reg()
        cid = await self.run(gdb.get_setting, gdb.CHANNEL_KEY)
        if reg is None or not getattr(reg, "albion_guild_id", "") or not cid:
            return 0
        guild = self.bot.get_guild(self.guild_id)
        channel = guild.get_channel(int(cid)) if guild is not None else None
        if guild is None or channel is None:
            return 0
        if not reg.roster_fresh() or time.time() - reg.roster_at > 3600:
            await reg.refresh_roster()
        if not reg.roster or time.time() - reg.roster_at > 3600:   # sin una lista recién bajada no se avisa nada
            return 0
        prev = await self.run(gdb.get_setting, "last_roster_size")
        await self.run(gdb.set_setting, "last_roster_size", str(len(reg.roster)))
        if prev and prev.isdigit() and len(reg.roster) < int(prev) * ROSTER_DROP_GUARD:
            print(f"⚠️ /miembros: la lista de la guild bajó de {prev} a {len(reg.roster)}; esta ronda no se avisa a nadie.")
            return 0
        import registration_db as rdb
        links = await asyncio.to_thread(rdb.all_registrations, self.db_path)
        chars = await self.run(gdb.chars_by_user)
        ids = {c.char_id for c in reg.roster.values() if c.char_id}
        names = {gdb.norm(n) for n in reg.roster}
        by_user = {}
        for r in links:
            by_user.setdefault(r.discord_id, []).append(r)
        inside = {uid for uid, rs in by_user.items()
                  if any(r.char_id and r.char_id in ids for r in rs) or (chars.get(uid, set()) & names)}
        alerted = await self.run(gdb.all_alert_ids)
        await self.run(gdb.clear_alerts, alerted & inside)                       # volvió a la guild: si se va de nuevo, se avisa de nuevo
        guests = await asyncio.to_thread(self._guest_ids, guild.id)
        sent, checked = 0, 0
        for uid, rs in by_user.items():
            if uid in inside or uid in alerted or uid in guests:
                continue
            member = guild.get_member(uid)
            if member is None or getattr(member, "bot", False):
                continue
            if checked >= LEAVE_CONFIRM_PER_ROUND:
                break
            checked += 1
            current = await reg._current_guild(rs[0])                           # se confirma con la API antes de avisar
            if current is None or (current and current.casefold() == (reg.required_guild or "").casefold()):
                continue
            esc = discord.utils.escape_markdown
            emb = discord.Embed(title="🚪 Salió de la guild de Albion", color=discord.Color.orange())
            emb.description = (f"<@{uid}> ({esc(member.display_name)})\nPersonaje: **{esc(rs[0].char_name)}**\n"
                               f"Gremio actual: {('**' + esc(current) + '**') if current else 'sin gremio'}\n\n¿Lo expulso del Discord?")
            emb.timestamp = datetime.now(timezone.utc)
            try:
                msg = await channel.send(embed=emb, view=LeaveAlertView(self), allowed_mentions=NO_MENTIONS)
            except Exception:
                traceback.print_exc()
                return sent
            await self.run(gdb.add_alert, uid, rs[0].char_name, channel.id, msg.id)
            sent += 1
        return sent

    async def _alert_for(self, interaction):
        if not can_admin_registro(interaction.user):
            await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar estos botones.")
            return None
        msg = getattr(interaction, "message", None)
        alert = await self.run(gdb.alert_by_message, msg.id) if msg is not None else None
        if alert is None or alert["status"] != "pending":
            await _say(interaction, "ℹ️ Este aviso ya fue resuelto.")
            return None
        return alert

    async def _close_message(self, msg, text: str):
        try:
            emb = msg.embeds[0] if getattr(msg, "embeds", None) else None
            if emb is not None:
                emb.add_field(name="Resultado", value=text, inline=False)
            await msg.edit(embed=emb, view=None)
        except Exception:
            traceback.print_exc()

    async def alert_ignore(self, interaction):
        alert = await self._alert_for(interaction)
        if alert is None:
            return
        msg = interaction.message
        if not await self.run(gdb.close_alert, msg.id, "ignored", interaction.user.id):
            return await _say(interaction, "ℹ️ Este aviso ya fue resuelto.")
        await interaction.response.defer()
        await self._close_message(msg, f"🙈 Ignorado por <@{interaction.user.id}>")

    async def alert_kick(self, interaction):
        alert = await self._alert_for(interaction)
        if alert is None:
            return
        from expulsar import check_target
        guild, actor, msg, uid = interaction.guild, interaction.user, interaction.message, int(alert["discord_id"])
        target = guild.get_member(uid)
        if target is None:
            await self.run(gdb.close_alert, msg.id, "kicked", actor.id)
            await _say(interaction, "ℹ️ Esa persona ya no está en el servidor.")
            return await self._close_message(msg, "🚪 Ya no estaba en el servidor")
        problem = check_target(actor, target, guild)
        if problem:
            return await _say(interaction, problem)

        async def yes(i: discord.Interaction):
            if not await self.run(gdb.close_alert, msg.id, "kicked", actor.id):
                return await i.response.edit_message(content="ℹ️ Este aviso ya lo resolvió otra persona.", view=None)
            again = check_target(actor, target, guild)
            if again:
                await self.run(gdb.reopen_alert, msg.id)
                return await i.response.edit_message(content=again, view=None)
            try:
                await target.kick(reason=f"Salió de la guild de Albion ({alert['char_name']}) · expulsado por {actor} ({actor.id})")
            except Exception as exc:
                await self.run(gdb.reopen_alert, msg.id)
                return await i.response.edit_message(content=f"❌ No pude expulsarlo ({type(exc).__name__}).", view=None)
            await i.response.edit_message(content=f"✅ <@{uid}> expulsado del Discord.", view=None, allowed_mentions=NO_MENTIONS)
            await self._close_message(msg, f"👢 Expulsado por <@{actor.id}>")
            reg = self.reg()
            if reg is not None:
                await reg.audit_log(f"👢 <@{actor.id}> expulsó del Discord a <@{uid}> (**{discord.utils.escape_markdown(alert['char_name'])}**): salió de la guild.")

        await interaction.response.send_message(
            f"⚠️ ¿Expulsar del Discord a <@{uid}> ({discord.utils.escape_markdown(target.display_name)})? Puede volver con una invitación nueva.",
            view=YesNoView(actor.id, yes, "Sí, expulsar"), ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ============================================================
    # Comandos
    # ============================================================
    @miembros_g.command(name="panel", description="(Staff) Publica el panel con botones de todos los reportes de miembros")
    async def panel(self, interaction: discord.Interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        emb = discord.Embed(title="🛡️ Control de miembros", color=discord.Color.blurple(), description=(
            "Los resultados te llegan solo a vos.\n\n"
            "🔗 **Sin vincular**: con rol Miembro y sin personaje de Albion (no lista invitados premium).\n"
            "🎮 **En la guild sin Discord**: jugadores de la guild sin Discord vinculado (no lista alters).\n"
            f"💤 **Inactivos**: más de {gdb.INACTIVE_DAYS} días sin conectarse (según el último archivo de la guild).\n"
            f"👢 **Lista para expulsar**: semana anterior; inactivos, menos de {gdb.CTA_WEEKLY_MIN} CTA, menos de {cdb.WEEKLY_MIN} VODs. Marca quién tiene /ausencia.\n"
            "🚪 **Salieron de la guild**: registrados que ya no están en la guild, con listas para expulsar.\n\n"
            "Con opciones (archivo, días, semana en curso): `/miembros ...`"))
        await interaction.response.send_message(embed=emb, view=PanelView(self))

    @miembros_g.command(name="sin-vincular", description="(Council) Con rol Miembro y sin personaje de Albion registrado")
    @app_commands.describe(mencionar="true: los etiqueta en un mensaje público (por defecto false)")
    async def sin_vincular_cmd(self, interaction: discord.Interaction, mencionar: Optional[bool] = False):
        await self.do_sin_vincular(interaction, bool(mencionar))

    @miembros_g.command(name="fuera-discord", description="(Council) En la guild de Albion pero sin Discord vinculado (no lista alters)")
    @app_commands.describe(archivo="Opcional: export de la guild (Character Name / Last Seen / Roles). Con archivo el mensaje es público")
    async def fuera_discord_cmd(self, interaction: discord.Interaction, archivo: Optional[discord.Attachment] = None):
        await self.do_fuera_discord(interaction, archivo)

    @miembros_g.command(name="inactivos", description="(Staff) Quiénes llevan más de N días sin conectarse a Albion")
    @app_commands.describe(dias="Días sin conectarse (por defecto 7)", archivo="Opcional: export de la guild nuevo (actualiza las conexiones)")
    async def inactivos_cmd(self, interaction: discord.Interaction, dias: Optional[app_commands.Range[int, 1, 90]] = gdb.INACTIVE_DAYS,
                            archivo: Optional[discord.Attachment] = None):
        await self.do_inactivos(interaction, dias or gdb.INACTIVE_DAYS, archivo)

    @miembros_g.command(name="kick-lista", description="(Staff) Candidatos a expulsar: inactivos, poca asistencia a CTA, faltan VODs")
    @app_commands.describe(semana="Semana a evaluar (lunes a domingo, hora UTC). Por defecto la anterior")
    @app_commands.choices(semana=[app_commands.Choice(name="Anterior", value="anterior"), app_commands.Choice(name="En curso", value="actual")])
    async def kick_lista_cmd(self, interaction: discord.Interaction, semana: Optional[app_commands.Choice[str]] = None):
        await self.do_kick_lista(interaction, getattr(semana, "value", semana) or "anterior")

    @miembros_g.command(name="fuera-guild", description="(Staff) Registrados cuyo personaje ya no está en la guild, con opción de expulsarlos")
    async def fuera_guild_cmd(self, interaction: discord.Interaction):
        await self.do_fuera_guild(interaction)

    @miembros_g.command(name="canal-avisos", description="(Council) Canal donde se avisa cuando alguien sale de la guild de Albion")
    @app_commands.describe(canal="Canal (por defecto, este)", desactivar="Apagar los avisos")
    async def canal_avisos_cmd(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None, desactivar: bool = False):
        await self.do_canal_avisos(interaction, canal, desactivar)


async def setup_guildcheck(bot, db_path: str, guild_id: int) -> GuildCheckCog:
    cog = GuildCheckCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
