"""/miembros: control de miembros guild de Albion ↔ Discord en un solo bloque, más un panel con botones.

  /miembros panel            Mensaje privado con un botón por cada reporte (dura 10 min).
  /miembros sin-vincular     Con rol Miembro y sin personaje registrado (no lista invitados premium).
  /miembros fuera-discord    En la guild y sin Discord vinculado (con o sin el archivo exportado; no lista alters).
  /miembros inactivos        Más de N días sin conectarse a Albion (según el último archivo de la guild subido).
  /miembros kick-lista       Candidatos a expulsar por motivo: inactivos, poca asistencia a CTA, faltan VODs; marca quién tiene /ausencia.
  /miembros asistencias      Asistencias a CTA por semana o de una persona (killboard, o anotado + 20 min en voz).
  /miembros antiguedad       Registrados: tag, personaje, días desde que se registraron y VODs subidos desde entonces.
  /miembros cambiar-guild    Pasa a alguien entre Smogg y Smogged: actualiza el registro y el rol (no lo toma como salida mientras se cambia).
  /miembros limpiar-registros Libera el personaje de quienes siguen en el Discord sin rol Miembro (con confirmación).
  /miembros fuera-guild      Registrados cuyo personaje ya no está en la guild, con listas para expulsar.
  /miembros historial      Guilds por las que pasó un personaje y con quién comparte kills (API de Albion en vivo, no guarda nada).
  /miembros canal-avisos     Canal donde queda registrado cuando alguien sale de la guild (Smogg o Smogged): el bot le saca los roles solo.
  /miembros kick             Le saca todos los roles de Discord a una persona y desvincula su cuenta de Albion (con confirmación; no la expulsa).

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
import historial as hist
from guildshop import can_manage
from registration import YesNoView, can_admin_registro

MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
OBSERVACION_ROLE_ID = int(os.getenv("OBSERVACION_ROLE_ID", "1559680220800360519"))
MAX_EXPORT_BYTES = 2_000_000
LEAVE_CHECK_MINUTES = 30
NEW_LINK_DAYS = 5                  # /miembros poca-asistencia: los vinculados hace 5 días o menos van aparte
LEAVE_CONFIRM_PER_ROUND = 10       # consultas a la API por ronda para confirmar el gremio actual
ROSTER_DROP_GUARD = 0.7            # si la lista de la guild baja de golpe más de un 30 %, esa ronda no se avisa nada
NO_MENTIONS = discord.AllowedMentions.none()
# Guilds para /miembros cambiar-guild (mismo formato que ALBION_EXTRA_GUILDS en bot.py: "Nombre:ID,...").
GUILD_CHOICES = [os.getenv("ALBION_GUILD_NAME", "Smogg")] + [x.split(":", 1)[0].strip() for x in
                                                            os.getenv("ALBION_EXTRA_GUILDS", "Smogged:PrAIGLB7QDiq2aARE6brwQ").split(",") if ":" in x]


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
# Vistas
# ============================================================
PANEL_TIMEOUT = 600.0


class PanelView(discord.ui.View):
    """Panel privado (solo lo ve y lo usa quien hizo /miembros panel); deja de funcionar a los 10 min."""

    def __init__(self, cog: "GuildCheckCog", owner_id: int):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.owner_id = cog, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Este panel no es tuyo: usá `/miembros panel`.")
            return False
        return True

    @discord.ui.button(label="Sin vincular", emoji="🔗", style=discord.ButtonStyle.secondary, row=0)
    async def b_sin_vincular(self, interaction: discord.Interaction, button):
        await self.cog.do_sin_vincular(interaction, False)

    @discord.ui.button(label="En la guild sin Discord", emoji="🎮", style=discord.ButtonStyle.secondary, row=0)
    async def b_fuera_discord(self, interaction: discord.Interaction, button):
        await self.cog.do_fuera_discord(interaction, None)

    @discord.ui.button(label="Inactivos +7 días", emoji="💤", style=discord.ButtonStyle.secondary, row=0)
    async def b_inactivos(self, interaction: discord.Interaction, button):
        await self.cog.do_inactivos(interaction, gdb.INACTIVE_DAYS, None)

    @discord.ui.button(label="Lista para expulsar", emoji="👢", style=discord.ButtonStyle.danger, row=1)
    async def b_kick(self, interaction: discord.Interaction, button):
        await self.cog.do_kick_lista(interaction, "anterior")

    @discord.ui.button(label="Salieron de la guild", emoji="🚪", style=discord.ButtonStyle.danger, row=1)
    async def b_fuera_guild(self, interaction: discord.Interaction, button):
        await self.cog.do_fuera_guild(interaction)

    @discord.ui.button(label="Asistencias CTA", emoji="🛡️", style=discord.ButtonStyle.secondary, row=1)
    async def b_asistencias(self, interaction: discord.Interaction, button):
        await self.cog.do_asistencias(interaction, "actual", None)


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
            lines.append("Cuenta si aparece en el killboard o si se anotó y estuvo más de 20 min en el canal de voz.")
            lines += [f"• <@{m.id}> · **{n}/{need_cta}** CTA{tags(m)}" for m, n in order(low_cta, lambda mx: mx[1])] or ["✅ Nadie."]

        lines += ["", f"**🎥 Faltan VODs de coaching: menos de {cdb.WEEKLY_MIN} (solo Observación) ({len(low_vods)})**"]
        lines += [f"• <@{m.id}> · **{n}/{cdb.WEEKLY_MIN}** VODs{tags(m)}" for m, n in order(low_vods, lambda mx: mx[1])] or ["✅ Nadie."]

        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- asistencias a CTA ----------
    async def do_asistencias(self, interaction, semana: str = "actual", persona: Optional[discord.Member] = None):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        chars = await self.run(gdb.chars_by_user)
        rule = "Cuenta si aparece en el killboard o si se anotó y estuvo más de 20 min en el canal de voz."
        if persona is not None:
            ws, we = gdb.week_bounds(0)
            ps, pe = gdb.week_bounds(-1)
            n_w, w = await self.run(gdb.cta_attendance, guild.id, ws, we, chars, user_id=persona.id)
            n_p, p = await self.run(gdb.cta_attendance, guild.id, ps, pe, chars, user_id=persona.id)
            n_t, t = await self.run(gdb.cta_attendance, guild.id, None, None, chars, user_id=persona.id)
            return await _say(interaction, f"🛡️ **Asistencias a CTA de {persona.mention}**\n"
                                           f"• Esta semana: **{w.get(persona.id, 0)}/{gdb.CTA_WEEKLY_MIN}** ({n_w} CTA hechas)\n"
                                           f"• Semana anterior: **{p.get(persona.id, 0)}** de {n_p}\n"
                                           f"• Total: **{t.get(persona.id, 0)}**\n{rule}")
        current = semana == "actual"
        start, end = gdb.week_bounds(0 if current else -1)
        n, att = await self.run(gdb.cta_attendance, guild.id, start, end, chars)
        role = guild.get_role(MIEMBRO_ROLE_ID)
        pool = [m for m in (role.members if role else []) if not getattr(m, "bot", False)]
        need = gdb.CTA_WEEKLY_MIN                     # mínimo semanal fijo, aunque se hayan hecho menos CTA
        rows = sorted(pool, key=lambda m: (-att.get(m.id, 0), m.display_name.casefold()))
        label = f"<t:{_ts(start)}:D> → <t:{_ts(end - timedelta(seconds=1))}:D>"
        lines = [f"🛡️ **Asistencias a CTA** · semana {'en curso' if current else 'anterior'} ({label}) · **{n}** CTA", rule]
        if not n:
            lines.append("ℹ️ No hubo CTAs en esa semana.")
        else:
            under = sum(1 for m in pool if att.get(m.id, 0) < need)
            lines.append(f"Debajo de {need}: **{under}** de {len(pool)} miembros (marcados con ⚠️).")
            lines += [f"• <@{m.id}> · **{att.get(m.id, 0)}/{need}**" + (" ⚠️" if att.get(m.id, 0) < need else "") for m in rows]
        others = [u for u in att if u not in {m.id for m in pool}]
        if others:
            lines.append(f"\nℹ️ {len(others)} persona(s) sin rol Miembro también asistieron: " + ", ".join(f"<@{u}>" for u in others)[:600])
        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- poca asistencia desde que entró (público) ----------
    async def do_poca_asistencia(self, interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer()                       # público: se publica en el canal
        guild = interaction.guild
        role = guild.get_role(MIEMBRO_ROLE_ID)
        if role is None:
            return await interaction.followup.send("❌ No encuentro el rol Miembro en el servidor.")
        now = datetime.now(timezone.utc)
        guests = await asyncio.to_thread(self._guest_ids, guild.id)
        pool = [m for m in role.members if not getattr(m, "bot", False) and not _is_admin(m) and not can_admin_registro(m) and m.id not in guests]
        chars = await self.run(gdb.chars_by_user)
        regs = await self.run(gdb.registered_at_by_user)
        ctas = await self.run(gdb.kb_cta_presence, guild.id, chars)
        if not ctas:
            return await interaction.followup.send("ℹ️ Todavía no hay CTA con killboard cargado: no hay nada que comparar.")
        signups = await self.run(gdb.cta_signups, guild.id)
        esc = discord.utils.escape_markdown
        low, new, no_char = [], [], 0
        for m in pool:
            if m.id not in chars:
                no_char += 1
                continue
            since, char = regs.get(m.id, (None, None))
            since = since or ctas[0][0]                       # ingreso = cuando lo reclutaron o vinculó su cuenta de Albion
            if since > now - timedelta(days=NEW_LINK_DAYS):     # recién vinculados: van aparte, abajo
                new.append((m, since, char, sum(1 for when, present in ctas if when >= since and m.id in present)))
                continue
            res = gdb.attendance_since(ctas, m.id, since, now)
            if res and res[0] < res[1]:
                low.append((m, since, char, *res))
        low.sort(key=lambda x: (x[3] / x[4], x[3], x[0].display_name.casefold()))
        new.sort(key=lambda x: x[1])
        absent = await self.run(gdb.ausencias_en, guild.id, ctas[0][0], now)
        vods = await self.run(cdb.count_since_by_user, {x[0].id: x[1] for x in low + new})

        def stats(m, since) -> str:
            """Killboard N/total (%) · anotado a /cta N/total (%) · VODs, todo desde que entró."""
            kb_tot = sum(1 for when, _p in ctas if when >= since)
            kb_in = sum(1 for when, present in ctas if when >= since and m.id in present)
            cta_tot = sum(1 for when, _s in signups if when >= since)
            cta_in = sum(1 for when, signed in signups if when >= since and m.id in signed)
            pct = lambda a, b: f"{a * 100 // b}%" if b else "—"
            return (f"🗡️ killboard **{kb_in}/{kb_tot}** ({pct(kb_in, kb_tot)}) · 📝 anotado a /cta **{cta_in}/{cta_tot}** ({pct(cta_in, cta_tot)})"
                    f" · 🎥 {vods.get(m.id, 0)} VOD{'s' if vods.get(m.id, 0) != 1 else ''}")

        def who(m, char) -> str:
            return f"<@{m.id}>" + (f" (**{esc(char)}**)" if char else "")

        def tag(m) -> str:
            a = absent.get(m.id)
            if not a:
                return ""
            txt = esc(str(a.get("texto") or ""))[:60]
            return f"\n   🟡 **Tiene /ausencia** hasta <t:{int(datetime.fromisoformat(a['hasta']).timestamp())}:D>" + (f": «{txt}»" if txt else "")

        lines = [f"⚔️ **Poca asistencia a CTA** · se piden **{gdb.CTA_WEEKLY_MIN} por semana**, contadas desde que lo reclutaron o vinculó su cuenta de Albion",
                 f"Solo cuentan las CTA con killboard cargado (desde <t:{_ts(ctas[0][0])}:D>, **{len(ctas)}** CTA). "
                 "Si entró hace poco, se le pide la parte proporcional.",
                 "🗡️ killboard = CTA con killboard en las que aparece / CTA con killboard desde que entró · "
                 "📝 /cta = CTA a las que se anotó / CTA hechas desde que entró · 🎥 VODs subidos desde que entró.",
                 f"**{len(low)}** de {len(pool) - no_char - len(new)} miembros por debajo"
                 + (f" · 🟡 {sum(1 for x in low if x[0].id in absent)} con /ausencia (revisar)" if any(x[0].id in absent for x in low) else "") + ":", ""]
        lines += [f"• {who(m, char)} · pedidas **{need}** · vinculado <t:{_ts(since)}:R>\n   {stats(m, since)}{tag(m)}"
                  for m, since, char, a, need, _n in low] or ["✅ Nadie está por debajo."]
        if new:
            lines += ["", f"**🆕 Vinculados hace {NEW_LINK_DAYS} días o menos ({len(new)})** · no se evalúan todavía"]
            lines += [f"• {who(m, char)} · vinculado <t:{_ts(since)}:R>\n   {stats(m, since)}{tag(m)}" for m, since, char, n in new]
        if no_char:
            lines.append(f"\nℹ️ {no_char} miembro(s) sin personaje registrado no se evalúan (ver `/miembros sin-vincular`).")
        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)

    # ---------- antigüedad: días desde que se registró y VODs subidos desde entonces ----------
    async def do_antiguedad(self, interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        now = datetime.now(timezone.utc)
        regs = await self.run(gdb.registered_at_by_user)
        rows = []
        for uid, (since, char) in regs.items():
            m = guild.get_member(uid)
            if m is None or getattr(m, "bot", False):
                continue
            rows.append((m, since, char))
        if not rows:
            return await _say(interaction, "ℹ️ No hay miembros registrados con el bot en el servidor.")
        vods = await self.run(cdb.count_since_by_user, {m.id: since for m, since, _c in rows})
        rows.sort(key=lambda x: (x[1], vods.get(x[0].id, 0), x[0].display_name.casefold()))   # más antiguo primero, menos VODs primero
        esc = discord.utils.escape_markdown
        lines = [f"📋 **Miembros por antigüedad** · **{len(rows)}** registrados con el bot",
                 "Ordenado de más días en la guild a menos; a igual antigüedad, los que menos VODs subieron primero.", ""]
        for m, since, char in rows:
            days = max(0, (now - since).days)
            n = vods.get(m.id, 0)
            lines.append(f"• <@{m.id}> (`{esc(m.name)}`) · **{esc(char or '?')}** · {days} día{'s' if days != 1 else ''} "
                         f"(<t:{_ts(since)}:D>) · 🎥 {n} VOD{'s' if n != 1 else ''}")
        for chunk in _chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- limpiar registros de quienes ya no tienen rol Miembro ----------
    def _cleanup_candidates(self, guild: discord.Guild, links: list, guests: set) -> list:
        """[(miembro, [registros])] de quienes siguen en el Discord sin rol Miembro.
        No toca invitados, Staff/Council/admins ni a quienes tienen el rol Observación."""
        by_user: dict = {}
        for r in links:
            by_user.setdefault(r.discord_id, []).append(r)
        out = []
        for uid, rs in by_user.items():
            m = guild.get_member(uid)
            if m is None or getattr(m, "bot", False) or uid in guests or _is_admin(m) or can_admin_registro(m):
                continue
            ids = {r.id for r in getattr(m, "roles", [])}
            if MIEMBRO_ROLE_ID in ids or OBSERVACION_ROLE_ID in ids:
                continue
            out.append((m, rs))
        out.sort(key=lambda x: x[0].display_name.casefold())
        return out

    async def do_limpiar_registros(self, interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        import registration_db as rdb
        guild, actor = interaction.guild, interaction.user
        links = await asyncio.to_thread(rdb.all_registrations, self.db_path)
        guests = await asyncio.to_thread(self._guest_ids, guild.id)
        cands = self._cleanup_candidates(guild, links, guests)
        if not cands:
            return await _say(interaction, "✅ Todos los registrados que siguen en el Discord tienen rol Miembro (o están exceptuados). No hay nada que liberar.")
        esc = discord.utils.escape_markdown
        lines = [f"• <@{m.id}> (`{esc(m.name)}`) → " + ", ".join(f"**{esc(r.char_name)}**" for r in rs) for m, rs in cands]
        head = (f"🧹 **{len(cands)}** registrados siguen en el Discord **sin rol Miembro**. Se les va a liberar el personaje "
                "(como si se hubieran ido del servidor). No se los expulsa ni se les tocan los roles.\n"
                "No incluye invitados, Staff, Council, administradores ni rol Observación.\n")
        chunks = _chunks(lines, 1500)

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content="⏳ Liberando personajes…", view=None)
            links_now = await asyncio.to_thread(rdb.all_registrations, self.db_path)
            guests_now = await asyncio.to_thread(self._guest_ids, guild.id)
            still = self._cleanup_candidates(guild, links_now, guests_now)          # se vuelve a revisar: alguien pudo recuperar el rol
            freed, names = 0, []
            for m, _rs in still:
                try:
                    await asyncio.to_thread(rdb.cancel_active_for, self.db_path, m.id, actor.id, "registro liberado: sin rol Miembro")
                    removed = await asyncio.to_thread(rdb.unlink, self.db_path, m.id, actor.id, None, "sin rol Miembro (/miembros limpiar-registros)")
                except Exception:
                    traceback.print_exc()
                    continue
                if removed:
                    freed += 1
                    names.append(f"<@{m.id}> → " + ", ".join(f"**{esc(r.char_name)}**" for r in removed))
            await i.followup.send(f"✅ Liberé el registro de **{freed}** persona(s).", ephemeral=True)
            reg = self.reg()
            if reg is not None and names:
                for chunk in _chunks([f"🧹 <@{actor.id}> liberó los registros de {freed} persona(s) sin rol Miembro:"] + [f"• {n}" for n in names]):
                    await reg.audit_log(chunk.rstrip("\n"))

        await interaction.response.send_message(head + chunks[0] + ("\n…y la lista sigue abajo." if len(chunks) > 1 else ""),
                                                view=YesNoView(actor.id, yes, f"Liberar {len(cands)} registros"),
                                                ephemeral=True, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- pase entre guilds (Smogg ↔ Smogged) ----------
    async def do_cambiar_guild(self, interaction, persona: discord.Member, guild_name: str):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        reg = await self._need_reg(interaction)
        if reg is None:
            return
        target = next((n for n, _i in reg.guilds if n and n.casefold() == (guild_name or "").casefold()), None)
        if target is None:
            return await _say(interaction, f"❌ Guild desconocida. Opciones: {reg.guild_label}.")
        import registration_db as rdb
        try:
            regs = await asyncio.to_thread(rdb.set_guild, self.db_path, persona.id, target, interaction.user.id)
        except rdb.RegError as exc:
            return await _say(interaction, f"❌ {exc}")
        esc = discord.utils.escape_markdown
        notes = await self._apply_guild_roles(reg, interaction.guild, persona, target,
                                              f"/miembros cambiar-guild a {target} por {interaction.user}")
        await self.run(gdb.clear_alerts, [persona.id])
        chars = ", ".join(f"**{esc(r.char_name)}**" for r in regs)
        await reg.audit_log(f"🔀 <@{interaction.user.id}> pasó a <@{persona.id}> ({chars}) a la guild **{esc(target)}**.")
        await _say(interaction, f"🔀 <@{persona.id}> ({chars}) ahora figura en **{esc(target)}**.\n" + "\n".join(notes) +
                   f"\nℹ️ Mientras se cambia de guild en el juego no se lo toma como salida. Si en {rdb.TRANSFER_HOURS} h no aparece en "
                   f"**{esc(target)}**, el bot vuelve a usar la guild real de su personaje.")

    @staticmethod
    async def _apply_guild_roles(reg, guild: discord.Guild, persona: discord.Member, target: str, reason: str) -> List[str]:
        """Pone el rol de la guild hermana de destino y saca el de las otras. Devuelve las notas de lo hecho."""
        esc = discord.utils.escape_markdown
        notes = []
        for gname, _gid in reg.extra_guilds:                      # rol de la guild hermana: se pone o se saca según el destino
            role = await reg.guild_role(guild, gname)
            if role is None:
                notes.append(f"⚠️ No pude encontrar ni crear el rol **{esc(gname)}**.")
                continue
            try:
                if gname.casefold() == target.casefold() and role not in persona.roles:
                    await persona.add_roles(role, reason=reason)
                    notes.append(f"✅ Le di el rol {role.mention}.")
                elif gname.casefold() != target.casefold() and role in persona.roles:
                    await persona.remove_roles(role, reason=reason)
                    notes.append(f"✅ Le saqué el rol {role.mention}.")
            except discord.Forbidden:
                notes.append(f"⚠️ No pude cambiar el rol {role.mention} (revisá que mi rol esté por encima).")
        return notes

    # ---------- actualizar miembros: guild de cada uno según los export de Smogg y Smogged ----------
    @staticmethod
    def plan_guild_sync(regs, rosters: dict, main_guild: str) -> dict:
        """regs: registros (discord_id, char_name, guild_name). rosters: {guild: set(nombres casefold)}.
        Devuelve {"change": [(uid, destino, actual, [chars])], "same": [(uid, guild)], "missing": [(uid, [chars])],
        "conflict": [(uid, {guild: [chars]})]}."""
        by_uid: dict = {}
        for r in regs:
            by_uid.setdefault(r.discord_id, []).append(r)
        out = {"change": [], "same": [], "missing": [], "conflict": []}
        for uid, rs in by_uid.items():
            chars = [r.char_name for r in rs]
            found = {g: [c for c in chars if c.casefold() in names] for g, names in rosters.items()}
            found = {g: cs for g, cs in found.items() if cs}
            if not found:
                out["missing"].append((uid, chars))
            elif len(found) > 1:
                out["conflict"].append((uid, found))
            else:
                target = next(iter(found))
                current = next((r.guild_name for r in rs if r.guild_name), None) or main_guild
                if any((r.guild_name or main_guild).casefold() != target.casefold() for r in rs):
                    out["change"].append((uid, target, current, chars))
                else:
                    out["same"].append((uid, target))
        return out

    async def do_actualizar(self, interaction, archivos: dict):
        """archivos: {nombre de guild: discord.Attachment} con el export de cada guild."""
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        reg = await self._need_reg(interaction)
        if reg is None:
            return
        await interaction.response.defer(ephemeral=True)
        from registration import parse_guild_export
        import registration_db as rdb
        esc = discord.utils.escape_markdown
        rosters, roster_norm = {}, set()
        for gname, att in archivos.items():
            if (getattr(att, "size", 0) or 0) > MAX_EXPORT_BYTES:
                return await _say(interaction, f"❌ El archivo de **{esc(gname)}** es demasiado grande (máx. 2 MB).")
            try:
                names = parse_guild_export(await att.read())
            except Exception:
                traceback.print_exc()
                names = []
            if not names:
                return await _say(interaction, f"❌ No pude leer el archivo de **{esc(gname)}**. Tiene que ser el export de la guild "
                                               "(Character Name / Last Seen / Roles).")
            rosters[gname] = {n.casefold() for n in names}
            roster_norm |= {gdb.norm(n) for n in names}
        dup = set.intersection(*rosters.values()) if len(rosters) > 1 else set()
        if len(dup) > max(5, 0.5 * min(len(v) for v in rosters.values())):
            return await _say(interaction, "❌ Los dos archivos tienen casi los mismos personajes: ¿subiste dos veces el mismo export?")
        main = reg.guilds[0][0] or "Smogg"
        plan = self.plan_guild_sync(await self.run(rdb.all_registrations), rosters, main)
        guild, actor = interaction.guild, interaction.user
        role_fix = []                                              # ya figuran bien en el registro pero el rol no coincide
        for uid, target in plan["same"]:
            m = guild.get_member(uid)
            if m is None:
                continue
            for gname, _gid in reg.extra_guilds:
                role = await reg.guild_role(guild, gname, create=False)
                if role is not None and (role in m.roles) != (gname.casefold() == target.casefold()):
                    role_fix.append((uid, target))
                    break
        # no están en ninguna guild: se les sacan los roles (como a quien sale de la guild), salvo exentos
        strip, keep = [], []
        if plan["missing"]:
            alters = await self.run(gdb.chars_by_user)                       # incluye alters y nombres anteriores
            guests = await asyncio.to_thread(self._guest_ids, guild.id)
            moving = await reg.transfer_targets()
        for uid, chars in plan["missing"]:
            m = guild.get_member(uid)
            why = None
            if alters.get(uid, set()) & roster_norm:
                why = "tiene un alter en la guild"
            elif uid in guests:
                why = "invitado"
            elif uid in moving:
                why = "pase de guild en curso"
            elif m is None:
                why = "ya no está en el Discord"
            elif getattr(m, "bot", False) or _is_admin(m) or can_admin_registro(m):
                why = "Staff/Council/admin"
            elif not self._removable_roles(guild, m):
                why = "no tiene roles para sacar"
            if why:
                keep.append((uid, chars, why))
            else:
                strip.append((uid, chars))
        counts = " · ".join(f"{esc(g)}: **{len(n)}**" for g, n in rosters.items())
        head = [f"🔄 **Actualizar miembros** — personajes en los archivos: {counts}",
                f"• Ya estaban bien: **{len(plan['same']) - len(role_fix)}**",
                f"• Cambian de guild: **{len(plan['change'])}**" + (f" · solo corregir rol: **{len(role_fix)}**" if role_fix else ""),
                f"• Fuera de las guilds, se les sacan los roles: **{len(strip)}**"]
        lines = head[:]
        if plan["change"]:
            lines.append("\n**🔀 Cambian de guild**")
            lines += [f"• <@{uid}> ({', '.join(esc(c) for c in chars)}): {esc(cur)} → **{esc(t)}**" for uid, t, cur, chars in plan["change"]]
        if role_fix:
            lines.append("\n**🎭 Solo corregir el rol**")
            lines += [f"• <@{uid}> → {esc(t)}" for uid, t in role_fix]
        if plan["conflict"]:
            lines.append("\n**⚠️ Con personajes en las dos guilds (no se tocan)**")
            lines += [f"• <@{uid}>: " + " · ".join(f"{esc(g)}: {', '.join(esc(c) for c in cs)}" for g, cs in f.items())
                      for uid, f in plan["conflict"]]
        if strip:
            lines.append(f"\n**🚫 No están en ninguna guild: se les sacan todos los roles ({len(strip)})** — el registro no se toca")
            lines += [f"• <@{uid}> ({', '.join(esc(c) for c in chars)})" for uid, chars in strip]
        if keep:
            lines.append(f"\n**❓ No están en ninguno de los dos archivos, pero no se tocan ({len(keep)})**")
            lines += [f"• <@{uid}> ({', '.join(esc(c) for c in chars)}) · {why}" for uid, chars, why in keep]
        lines.append("\nℹ️ Los alters no se tocan.")
        todo = len(plan["change"]) + len(role_fix) + len(strip)
        chunks = _chunks(lines)
        if not todo:
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)
            return

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content="⏳ Actualizando…", view=None)
            done, notes = 0, []
            for uid, target, _cur, _chars in plan["change"]:
                try:
                    await asyncio.to_thread(rdb.sync_guild, self.db_path, uid, target, actor.id)
                    done += 1
                except rdb.RegError as exc:
                    notes.append(f"❌ <@{uid}>: {exc}")
                    continue
                m = guild.get_member(uid)
                if m is not None:
                    notes += [f"<@{uid}>: {n}" for n in await self._apply_guild_roles(
                        reg, guild, m, target, f"/miembros actualizar por {actor}") if n.startswith("⚠️")]
            for uid, target in role_fix:
                m = guild.get_member(uid)
                if m is not None:
                    notes += [f"<@{uid}>: {n}" for n in await self._apply_guild_roles(
                        reg, guild, m, target, f"/miembros actualizar por {actor}") if n.startswith("⚠️")]
            if plan["change"]:
                await self.run(gdb.clear_alerts, [uid for uid, *_ in plan["change"]])
            stripped = []
            for uid, chars in strip:
                m = guild.get_member(uid)
                if m is None or can_admin_registro(m) or _is_admin(m):
                    continue
                roles = self._removable_roles(guild, m)
                if not roles:
                    continue
                try:
                    await m.remove_roles(*roles, reason=f"/miembros actualizar por {actor}: no está en ninguna guild ({', '.join(chars)})")
                except Exception as exc:
                    notes.append(f"⚠️ <@{uid}>: no pude sacarle los roles ({type(exc).__name__}).")
                    continue
                stripped.append(f"• <@{uid}> ({', '.join(esc(c) for c in chars)}): " + " ".join(f"<@&{r.id}>" for r in roles))
                await self.run(gdb.add_alert, uid, chars[0], 0, 0)            # así el aviso automático de salida no lo repite
                await self.run(gdb.close_alert, 0, "roles_removed", actor.id)
            for chunk in _chunks([f"✅ Listo: **{done}** cambiaron de guild, **{len(role_fix)}** con el rol corregido, "
                                  f"**{len(stripped)}** sin roles por estar fuera de las guilds."] + notes):
                await i.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)
            audit = [f"🔄 <@{actor.id}> actualizó los miembros con los export ({counts}): {done} cambiaron de guild, "
                     f"{len(role_fix)} con el rol corregido."]
            audit += [f"• <@{uid}> → **{esc(t)}**" for uid, t, _c, _ch in plan["change"]]
            if stripped:
                audit += [f"🚫 Se les sacaron los roles por no estar en ninguna guild ({len(stripped)}):"] + stripped
            for chunk in _chunks(audit):
                await reg.audit_log(chunk.rstrip("\n"))

        await interaction.followup.send(chunks[0] + ("\n…y la lista sigue abajo." if len(chunks) > 1 else ""),
                                        view=YesNoView(actor.id, yes, f"Aplicar {todo} cambio(s)"),
                                        ephemeral=True, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- kick: todos los roles + desvincular la cuenta de Albion (sigue en el Discord) ----------
    @staticmethod
    def kick_problem(actor, target, guild) -> Optional[str]:
        """Motivo por el que `actor` NO puede usar /miembros kick con `target`, o None si puede."""
        if not can_admin_registro(actor):
            return "🔒 Solo el Staff, el Council o los administradores pueden usar este comando."
        if target.id == actor.id:
            return "❌ No podés usarlo con vos mismo."
        if getattr(target, "bot", False):
            return "❌ No se puede usar con un bot."
        if target.id == getattr(guild, "owner_id", None):
            return "❌ No se puede usar con el dueño del servidor."
        actor_top = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False)) or actor.id == getattr(guild, "owner_id", None)
        if can_admin_registro(target) and not actor_top:
            return "🔒 Solo un administrador puede usarlo con alguien del Staff o del Council."
        pt = getattr(getattr(target, "top_role", None), "position", None)
        pa = getattr(getattr(actor, "top_role", None), "position", None)
        if actor.id != getattr(guild, "owner_id", None) and pt is not None and pa is not None and pt >= pa:
            return "❌ Esa persona tiene un rol igual o más alto que el tuyo."
        return None

    async def do_kick(self, interaction, persona: discord.Member, motivo: Optional[str] = None):
        guild, actor = interaction.guild, interaction.user
        problem = self.kick_problem(actor, persona, guild)
        if problem:
            return await _say(interaction, problem)
        import registration_db as rdb
        esc = discord.utils.escape_markdown
        regs = await asyncio.to_thread(rdb.get_registrations, self.db_path, persona.id)
        roles = self._removable_roles(guild, persona)
        if not regs and not roles:
            return await _say(interaction, f"ℹ️ <@{persona.id}> no tiene roles para sacar ni cuenta de Albion vinculada.")
        why = (motivo or "sin motivo")[:200]

        async def yes(i: discord.Interaction):
            again = self.kick_problem(actor, persona, guild)
            if again:
                return await i.response.edit_message(content=again, view=None)
            notes = []
            removed = await asyncio.to_thread(rdb.unlink, self.db_path, persona.id, actor.id, None, f"/miembros kick. Motivo: {why}")
            notes.append("🔓 Cuenta de Albion desvinculada: " + ", ".join(f"**{esc(r.char_name)}**" for r in removed) if removed
                         else "ℹ️ No tenía cuenta de Albion vinculada.")
            now_roles = self._removable_roles(guild, persona)
            if now_roles:
                try:
                    await persona.remove_roles(*now_roles, reason=f"/miembros kick por {actor} ({actor.id}): {why}")
                    notes.append(f"🎭 Se le sacaron **{len(now_roles)}** rol(es): " + " ".join(f"<@&{r.id}>" for r in now_roles))
                except discord.Forbidden:
                    notes.append("⚠️ No pude sacarle los roles (revisá que mi rol esté por encima).")
                except Exception as exc:
                    notes.append(f"⚠️ No pude sacarle los roles ({type(exc).__name__}).")
            else:
                notes.append("ℹ️ No tenía roles que yo pueda sacar.")
            await self.run(gdb.clear_alerts, [persona.id])
            await i.response.edit_message(content=f"👢 <@{persona.id}>\n" + "\n".join(notes), view=None, allowed_mentions=NO_MENTIONS)
            reg = self.reg()
            if reg is not None:
                await reg.audit_log(f"👢 <@{actor.id}> usó /miembros kick con <@{persona.id}>: " +
                                    (", ".join(f"**{esc(r.char_name)}**" for r in removed) if removed else "sin cuenta de Albion") +
                                    f", {len(now_roles)} rol(es) sacados — {esc(why)}")

        what = []
        if roles:
            what.append(f"sacarle **{len(roles)}** rol(es)")
        if regs:
            what.append("desvincular " + ", ".join(f"**{esc(r.char_name)}**" for r in regs))
        await interaction.response.send_message(
            f"⚠️ ¿{' y '.join(what).capitalize()} a <@{persona.id}> ({esc(persona.display_name)})? Sigue en el Discord.",
            view=YesNoView(actor.id, yes, "Sí, sacar todo"), ephemeral=True, allowed_mentions=NO_MENTIONS)

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
        await _say(interaction, f"✅ Cuando un registrado salga de la guild de Albion (Smogg o Smogged), le saco los roles de Discord y lo dejo "
                                f"anotado en {channel.mention}. Se revisa cada {LEAVE_CHECK_MINUTES} min.")

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
        moving = await reg.transfer_targets()                                   # /miembros cambiar-guild en curso: no es una salida
        sent, checked = 0, 0
        for uid, rs in by_user.items():
            if uid in inside or uid in alerted or uid in guests or uid in moving:
                continue
            member = guild.get_member(uid)
            if member is None or getattr(member, "bot", False) or _is_admin(member) or can_admin_registro(member):
                continue
            if checked >= LEAVE_CONFIRM_PER_ROUND:
                break
            checked += 1
            current = await reg._current_guild(rs[0])                           # se confirma con la API antes de avisar
            if current is None or (current and reg.is_own_guild(current)):
                continue
            await self._strip_leaver(guild, channel, member, rs[0].char_name, current)
            sent += 1
        return sent

    @staticmethod
    def _removable_roles(guild, member) -> list:
        """Roles que el bot puede sacar: no @everyone, no los de integraciones, y solo los que están por debajo de su rol más alto."""
        top = getattr(getattr(getattr(guild, "me", None), "top_role", None), "position", None)
        out = []
        for r in getattr(member, "roles", []):
            if r.id == guild.id or getattr(r, "managed", False):
                continue
            if top is not None and getattr(r, "position", 0) >= top:
                continue
            out.append(r)
        return out

    async def _strip_leaver(self, guild, channel, member, char_name: str, current: str) -> None:
        """Salió de la guild de Albion: se le sacan los roles de Discord y queda registrado en el canal de avisos."""
        esc = discord.utils.escape_markdown
        roles = self._removable_roles(guild, member)
        result = "No tenía roles para sacar."
        if roles:
            try:
                await member.remove_roles(*roles, reason=f"Salió de la guild de Albion ({char_name})")
                result = f"Se le sacaron **{len(roles)}** rol(es): " + " ".join(f"<@&{r.id}>" for r in roles)
            except discord.Forbidden:
                result = "⚠️ No pude sacarle los roles (revisá que mi rol esté por encima)."
            except Exception as exc:
                result = f"⚠️ No pude sacarle los roles ({type(exc).__name__})."
        emb = discord.Embed(title="🚪 Salió de la guild de Albion", color=discord.Color.orange())
        emb.description = (f"<@{member.id}> ({esc(member.display_name)})\nPersonaje: **{esc(char_name)}**\n"
                           f"Gremio actual: {('**' + esc(current) + '**') if current else 'sin gremio'}")
        emb.add_field(name="🎭 Roles", value=result[:1000], inline=False)
        emb.timestamp = datetime.now(timezone.utc)
        msg_id = 0
        try:
            msg = await channel.send(embed=emb, allowed_mentions=NO_MENTIONS)
            msg_id = getattr(msg, "id", 0) or 0
        except Exception:
            traceback.print_exc()
        await self.run(gdb.add_alert, member.id, char_name, channel.id, msg_id)          # una sola vez por salida
        await self.run(gdb.close_alert, msg_id, "roles_removed", 0)
        reg = self.reg()
        if reg is not None:
            await reg.audit_log(f"🚪 <@{member.id}> (**{esc(char_name)}**) salió de la guild de Albion: {result}")

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
    @miembros_g.command(name="panel", description="(Staff) Panel privado con botones de todos los reportes de miembros")
    async def panel(self, interaction: discord.Interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        emb = discord.Embed(title="🛡️ Control de miembros", color=discord.Color.blurple(), description=(
            "Solo lo ves vos. Los botones funcionan durante 10 minutos.\n\n"
            "🔗 **Sin vincular**: con rol Miembro y sin personaje de Albion (no lista invitados premium).\n"
            "🎮 **En la guild sin Discord**: jugadores de la guild sin Discord vinculado (no lista alters).\n"
            f"💤 **Inactivos**: más de {gdb.INACTIVE_DAYS} días sin conectarse (según el último archivo de la guild).\n"
            f"👢 **Lista para expulsar**: semana anterior; inactivos, menos de {gdb.CTA_WEEKLY_MIN} CTA, menos de {cdb.WEEKLY_MIN} VODs. Marca quién tiene /ausencia.\n"
            "🚪 **Salieron de la guild**: registrados que ya no están en la guild, con listas para expulsar.\n"
            "🛡️ **Asistencias CTA**: semana en curso; killboard o anotado + 20 min en voz.\n\n"
            "Con opciones (archivo, días, semana en curso): `/miembros ...`"))
        await interaction.response.send_message(embed=emb, view=PanelView(self, interaction.user.id), ephemeral=True)

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

    @miembros_g.command(name="asistencias", description="(Staff) Asistencias a CTA por semana (killboard o anotado + 20 min en voz)")
    @app_commands.describe(semana="Semana (lunes a domingo, hora UTC). Por defecto la en curso",
                           persona="Ver solo a una persona: esta semana, la anterior y el total")
    @app_commands.choices(semana=[app_commands.Choice(name="En curso", value="actual"), app_commands.Choice(name="Anterior", value="anterior")])
    async def asistencias_cmd(self, interaction: discord.Interaction, semana: Optional[app_commands.Choice[str]] = None,
                              persona: Optional[discord.Member] = None):
        await self.do_asistencias(interaction, getattr(semana, "value", semana) or "actual", persona)

    @miembros_g.command(name="historial", description="(Staff/Council/Reclutador) Guilds por las que pasó un personaje y con quién comparte kills")
    @app_commands.describe(nombre="Nombre exacto del personaje en Albion", servidor="Servidor (si hay homónimos)",
                           publico="true = la respuesta la ve todo el canal (por defecto oculta)")
    @app_commands.choices(servidor=hist.REGION_CHOICES)
    async def historial_cmd(self, interaction: discord.Interaction, nombre: str,
                            servidor: Optional[app_commands.Choice[str]] = None, publico: bool = False):
        await hist.Historial(self.bot).run(interaction, nombre, getattr(servidor, "value", servidor), publico)

    @miembros_g.command(name="poca-asistencia", description="(Staff) Lista pública: menos de 5 CTA por semana desde que entraron (killboards)")
    async def poca_asistencia_cmd(self, interaction: discord.Interaction):
        await self.do_poca_asistencia(interaction)

    @miembros_g.command(name="antiguedad", description="(Staff) Miembros con nombre en Albion, días desde que se registraron y VODs subidos")
    async def antiguedad_cmd(self, interaction: discord.Interaction):
        await self.do_antiguedad(interaction)

    @miembros_g.command(name="cambiar-guild", description="(Staff) Pasa a alguien de Smogg a Smogged o al revés (registro y rol)")
    @app_commands.describe(persona="A quién", guild="Guild de destino")
    @app_commands.choices(guild=[app_commands.Choice(name=g, value=g) for g in GUILD_CHOICES])
    async def cambiar_guild_cmd(self, interaction: discord.Interaction, persona: discord.Member, guild: app_commands.Choice[str]):
        await self.do_cambiar_guild(interaction, persona, getattr(guild, "value", guild))

    @miembros_g.command(name="actualizar", description="(Staff) Con los export de Smogg y Smogged, actualiza en qué guild está cada miembro")
    @app_commands.describe(smogg="Export de la guild Smogg (Character Name / Last Seen / Roles)",
                           smogged="Export de la guild Smogged (Character Name / Last Seen / Roles)")
    async def actualizar_cmd(self, interaction: discord.Interaction, smogg: discord.Attachment, smogged: discord.Attachment):
        reg = self.reg()
        names = [n for n, _i in reg.guilds] if reg is not None else GUILD_CHOICES
        await self.do_actualizar(interaction, {(names[0] if names else "Smogg"): smogg,
                                               (names[1] if len(names) > 1 else "Smogged"): smogged})

    @miembros_g.command(name="kick", description="(Staff) Le saca todos los roles de Discord y desvincula su cuenta de Albion")
    @app_commands.describe(persona="A quién", motivo="Motivo (queda en la auditoría)")
    async def kick_cmd(self, interaction: discord.Interaction, persona: discord.Member, motivo: Optional[str] = None):
        await self.do_kick(interaction, persona, motivo)

    @miembros_g.command(name="limpiar-registros", description="(Staff) Libera el personaje de quienes siguen en el Discord sin rol Miembro")
    async def limpiar_registros_cmd(self, interaction: discord.Interaction):
        await self.do_limpiar_registros(interaction)

    @miembros_g.command(name="fuera-guild",description="(Staff) Registrados cuyo personaje ya no está en la guild, con opción de expulsarlos")
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
