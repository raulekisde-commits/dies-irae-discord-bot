"""/chest auditar: combina los combatlogs subidos de una CTA, los compara con el log del chest y, con la confirmación
del Staff o el Council, taguea en el canal de auditoría a quien no depositó. La lógica está en chestaudit_db.py."""
import asyncio
import io
import traceback
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import botsettings as bs
import chestaudit_db as ca
from dbutil import connect
from killboards import is_staff_or_council

try:
    import chestcard                                   # imagen de los ítems (Pillow); si falla, el tag va solo con texto
except Exception:
    traceback.print_exc()
    chestcard = None

CHEST_AUDIT_CHANNEL_ID = 1462185027247931660
bs.register("chest_audit", "Auditoría del chest", CHEST_AUDIT_CHANNEL_ID,
            "Acá se taguea a quien no depositó en el chest lo que looteó en una CTA (/chest auditar)")
LOG_EXT = (".txt", ".log", ".csv")
NO_MENTIONS = discord.AllowedMentions.none()
TAG_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
MAX_ITEMS_PER_LINE = 6
CHESTLOG_PREFIX = "📦 Chestlog"                        # así empieza el mensaje del bot en cta_chestlog_upload (bot.py)


def _cta_row(db_path: str, cta_id: Optional[int], channel_id: int) -> Optional[dict]:
    """La CTA indicada o, si no se indicó, la del hilo donde se usa el comando (combatlog privado/público, stats o la CTA)."""
    conn = connect(db_path)
    try:
        if cta_id:
            r = conn.execute("SELECT * FROM cta_runs WHERE id=?", (cta_id,)).fetchone()
        else:
            r = conn.execute("SELECT * FROM cta_runs WHERE combatlog_priv_thread_id=? OR combatlog_thread_id=? OR stats_thread_id=?"
                             " OR thread_id=? ORDER BY id DESC LIMIT 1", (channel_id,) * 4).fetchone()
        return dict(r) if r else None
    except Exception:
        traceback.print_exc()
        return None
    finally:
        conn.close()


def cta_title(run: dict) -> str:
    return run.get("title") or f"CTA {run.get('composition_name') or ''} #{run['id']}".replace("  ", " ")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _items_text(missing: List[dict]) -> str:
    parts = [ca.item_label(m) for m in missing[:MAX_ITEMS_PER_LINE]]
    extra = len(missing) - MAX_ITEMS_PER_LINE
    return ", ".join(parts) + (f" y {extra} ítem(s) más" if extra > 0 else "")


def tag_lines(result: dict, ids: Dict[str, int]) -> List[str]:
    """Una línea por jugador con faltantes (sin los filtrados), de mayor a menor."""
    out = []
    for k, p in sorted(result["players"].items(), key=lambda kv: (-kv[1].missing_units, kv[0])):
        if not p.missing or p.filtered:
            continue
        who = f"<@{ids[k]}> (**{discord.utils.escape_markdown(p.name)}**)" if k in ids else \
            f"**{discord.utils.escape_markdown(p.name)}** (sin Discord vinculado)"
        out.append(f"• {who} no depositó **{p.missing_units}**: {_items_text(p.missing)}")
    return out


def chunk_lines(header: str, lines: List[str], limit: int = 1900) -> List[str]:
    msgs, cur = [], header
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            msgs.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + ln
    if cur:
        msgs.append(cur)
    return msgs


GENERAL_NOTICE = ("**Si te tagean en este canal, no te lo tomes como un ataque personal. Puede tratarse de un error, pero es tu "
                  "responsabilidad justificar la situación y presentar las pruebas correspondientes. No voy a revisar cada muerte ni "
                  "cada trade uno por uno. Si considerás que el señalamiento es incorrecto, demostralo con evidencia.**")


def to_tag(result: dict, ids: Dict[str, int]) -> List[tuple]:
    """[(PlayerResult, discord_id | None)] de quienes se taguean (con faltantes que cuentan), de mayor a menor."""
    return [(p, ids.get(k)) for k, p in sorted(result["players"].items(), key=lambda kv: (-kv[1].missing_units, kv[0]))
            if p.missing]


def tag_text(p, discord_id: Optional[int], title: str) -> str:
    who = f"<@{discord_id}> (**{discord.utils.escape_markdown(p.name)}**)" if discord_id else \
        f"**{discord.utils.escape_markdown(p.name)}** (sin Discord vinculado)"
    return (f"{who} no depositó **{p.missing_units}** ítem(s) de **{discord.utils.escape_markdown(title)}**.\n"
            f"-# {_items_text(p.missing)}")[:1900]


async def _card(p, title: str) -> Optional[discord.File]:
    if chestcard is None:
        return None
    try:
        png = await asyncio.to_thread(chestcard.render_with_icons, p.name, p.missing, title)
        return discord.File(io.BytesIO(png), filename=f"chest-{ca.norm(p.name).replace(' ', '_')[:40]}.png")
    except Exception:
        traceback.print_exc()
        return None


class TagView(discord.ui.View):
    """Confirmación (Staff/Council) antes de taguear en el canal de auditoría."""
    def __init__(self, cog: "ChestAuditCog", audit_id: int, title: str, entries: List[tuple]):
        super().__init__(timeout=900)
        self.cog, self.audit_id, self.title, self.entries = cog, audit_id, title, entries

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_staff_or_council(interaction.user):
            await interaction.response.send_message("🔒 Solo el Staff o el Council.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Taguear", emoji="📣", style=discord.ButtonStyle.danger)
    async def tag(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await asyncio.to_thread(ca.mark_tagged, self.cog.db_path, self.audit_id, interaction.user.id):
            return await interaction.response.edit_message(content="Esta auditoría ya se tagueó.", view=None)
        await interaction.response.defer()
        ch = interaction.guild.get_channel(bs.get("chest_audit")) if interaction.guild else None
        if ch is None:
            try:
                ch = await self.cog.bot.fetch_channel(bs.get("chest_audit"))
            except Exception:
                ch = None
        if ch is None:
            return await interaction.edit_original_response(content="❌ No encuentro el canal de auditoría.", view=None)
        await interaction.edit_original_response(content=f"⏳ Tagueando a **{len(self.entries)}** en {ch.mention}…", view=None)
        try:
            general = await ch.send(GENERAL_NOTICE, allowed_mentions=NO_MENTIONS)
            await asyncio.to_thread(ca.set_general, self.cog.db_path, self.audit_id, ch.id, general.id)
        except Exception as exc:
            traceback.print_exc()
            return await interaction.edit_original_response(content=f"❌ No pude publicar en el canal: {exc!r}")
        ok = 0
        for p, uid in self.entries:
            try:
                await self.cog.post_tag(ch, interaction.guild.id, self.audit_id, self.title, p, uid)
                ok += 1
            except Exception:
                traceback.print_exc()
        await interaction.edit_original_response(content=f"✅ Tagueados **{ok}/{len(self.entries)}** en {ch.mention}.")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Listo, no se tagueó a nadie (la auditoría quedó guardada para las stats).",
                                                view=None)


class SolvedView(discord.ui.View):
    """Botón persistente de cada tag: Staff/Council/admin lo marcan solucionado (borra mensaje e hilo)."""
    def __init__(self, cog: "ChestAuditCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Solucionado", emoji="✅", style=discord.ButtonStyle.success, custom_id="chestaudit:solved")
    async def solved(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.solve(interaction)


class WarnView(discord.ui.View):
    """Después de Solucionado: le pregunta a quien lo apretó si quiere ponerle un warning a la persona."""
    def __init__(self, cog: "ChestAuditCog", actor_id: int, target_id: int, title: str):
        super().__init__(timeout=600)
        self.cog, self.actor_id, self.target_id, self.title, self.done = cog, actor_id, target_id, title, False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Esto no es tuyo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sí, poner warning", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.send_message("Ya fue procesado.", ephemeral=True)
        self.done = True
        import warnings_sys as ws
        reason = f"No depositar loot en el chest ({self.title})"[:ws.REASON_MAX]
        try:
            wid, total = await asyncio.to_thread(ws.add_warning, self.cog.db_path, interaction.guild.id, self.target_id, reason,
                                                 interaction.user.id)
        except Exception as exc:
            traceback.print_exc()
            return await interaction.response.edit_message(content=f"❌ No pude cargar el warning: {exc!r}", view=None)
        await interaction.response.edit_message(
            content=f"⚠️ Warning **#{wid}** cargado a <@{self.target_id}> (lleva **{total}**).\n**Motivo:** {reason}", view=None)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        await interaction.response.edit_message(content="Listo, sin warning.", view=None)


class ChestAuditCog(commands.Cog):
    chest = app_commands.Group(name="chest", description="Auditoría del chest después de una CTA", guild_only=True)

    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(ca.init_schema, self.db_path)
        self.bot.add_view(SolvedView(self))

    async def post_tag(self, ch, guild_id: int, audit_id: int, title: str, p, discord_id: Optional[int]):
        """Mensaje de una persona: mención + imagen de lo que falta + botón Solucionado, y su hilo."""
        file = await _card(p, title)
        kw = {"file": file} if file else {}
        msg = await ch.send(tag_text(p, discord_id, title), view=SolvedView(self), allowed_mentions=TAG_MENTIONS, **kw)
        tag_id = await asyncio.to_thread(ca.add_tag, self.db_path, audit_id, guild_id, ch.id, msg.id, p.name, discord_id, title,
                                         p.missing_units)
        try:
            th = await msg.create_thread(name=f"Chest · {p.name}"[:100], auto_archive_duration=10080)
            await asyncio.to_thread(ca.set_tag_thread, self.db_path, tag_id, th.id)
        except Exception:
            traceback.print_exc()
        return msg

    async def _delete_channel_msg(self, channel_id: int, message_id: int) -> None:
        try:
            ch = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await ch.get_partial_message(message_id).delete()
        except Exception:
            traceback.print_exc()

    async def solve(self, interaction: discord.Interaction):
        """Botón Solucionado: borra el hilo y el mensaje, pregunta por el warning y, si era el último, borra el mensaje general."""
        if not is_staff_or_council(interaction.user):
            return await interaction.response.send_message("🔒 Solo el Staff, el Council o un administrador.", ephemeral=True)
        tag = await asyncio.to_thread(ca.tag_by_message, self.db_path, interaction.message.id) if interaction.message else None
        if tag is None:
            return await interaction.response.send_message("❌ No encuentro este tag.", ephemeral=True)
        res = await asyncio.to_thread(ca.resolve_tag, self.db_path, int(tag["id"]), interaction.user.id)
        if res is None:
            return await interaction.response.send_message("Este tag ya estaba solucionado.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if tag.get("thread_id"):
            try:
                th = interaction.guild.get_thread(int(tag["thread_id"])) or await self.bot.fetch_channel(int(tag["thread_id"]))
                await th.delete()
            except Exception:
                traceback.print_exc()
        try:
            await interaction.message.delete()
        except Exception:
            traceback.print_exc()
        if res["general"]:
            await self._delete_channel_msg(*res["general"])
        done = "✅ Solucionado." + (" Era el último: borré también el mensaje general." if res["general"] else
                                    f" Quedan **{res['pending']}** por solucionar.")
        if tag.get("discord_id"):
            await interaction.followup.send(f"{done}\n¿Querés ponerle un warning a <@{tag['discord_id']}> por no depositar loot?",
                                            view=WarnView(self, interaction.user.id, int(tag["discord_id"]), tag.get("title") or ""),
                                            ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.followup.send(done + " (No tiene Discord vinculado: no se puede poner warning.)", ephemeral=True)

    async def _thread(self, guild, thread_id) -> Optional[discord.abc.Messageable]:
        if not thread_id:
            return None
        th = guild.get_thread(int(thread_id)) or guild.get_channel(int(thread_id))
        if th is None:
            try:
                th = await self.bot.fetch_channel(int(thread_id))
            except Exception:
                return None
        return th

    async def thread_files(self, guild, run: dict) -> Tuple[List[List[ca.LootEvent]], List[ca.ChestEntry]]:
        """Archivos de los hilos de combatlog de la CTA (privado y, si hay, el público viejo):
        cada archivo del loot logger por separado, y el chestlog del último mensaje que traiga uno («Subir chestlog»)."""
        files: List[List[ca.LootEvent]] = []
        chest: List[ca.ChestEntry] = []
        seen = set()
        for tid in (run.get("combatlog_priv_thread_id"), run.get("combatlog_thread_id")):
            th = await self._thread(guild, tid)
            if th is None:
                continue
            async for msg in th.history(limit=None, oldest_first=True):
                msg_chest: List[ca.ChestEntry] = []
                for a in msg.attachments:
                    if a.id in seen or not (a.filename or "").lower().endswith(LOG_EXT):
                        continue
                    seen.add(a.id)
                    try:
                        text = _decode(await a.read())
                    except Exception as exc:
                        print(f"Chest CTA #{run['id']}: no pude leer {a.filename}: {exc!r}")
                        continue
                    if ca.is_loot_log(text):
                        events = ca.parse_loot_log(text)
                        if events:
                            files.append(events)
                    elif self._is_chestlog_msg(msg):
                        msg_chest += ca.parse_chest_log(text)
                if msg_chest:                             # vale el chestlog más nuevo
                    chest = msg_chest
        return files, chest

    def _is_chestlog_msg(self, msg) -> bool:
        """Solo cuentan los chestlog que reenvió el bot con «Subir chestlog» (Staff/Council), no cualquier archivo del hilo."""
        me = getattr(self.bot, "user", None)
        return me is not None and getattr(msg.author, "id", None) == me.id and (msg.content or "").startswith(CHESTLOG_PREFIX)

    async def loot_files(self, guild, run: dict) -> List[List[ca.LootEvent]]:
        return (await self.thread_files(guild, run))[0]

    async def audit_and_preview(self, interaction: discord.Interaction, run: dict, files: List[List[ca.LootEvent]],
                                chest: List[ca.ChestEntry]):
        """Corre la auditoría, la guarda y manda el preview (solo a quien la pidió) con el botón Taguear.
        La interacción ya tiene que estar diferida."""
        result = await asyncio.to_thread(ca.audit, files, chest)
        audit_id = await asyncio.to_thread(ca.save_audit, self.db_path, interaction.guild.id, int(run["id"]),
                                           interaction.user.id, result, len(files))
        title = cta_title(run)
        ids = await asyncio.to_thread(ca.discord_ids_by_name, self.db_path, list(result["players"]))
        lines = tag_lines(result, ids)
        report = ca.report_text(result, title)
        filtered = sum(1 for p in result["players"].values() if p.filtered)
        summary = (f"📦 **Auditoría de chest — {discord.utils.escape_markdown(title)}**\n"
                   f"Combatlogs: **{len(files)}** · eventos de loot: **{len(result['events'])}** · "
                   f"auditados: **{len(result['players'])}**\n"
                   f"Depósitos válidos: <t:{int(result['chest_from'].timestamp())}:t> → <t:{int(result['chest_to'].timestamp())}:t>\n"
                   f"Sin depositar: **{len(lines)}**" + (f" · solo tolerado (sin tag): **{filtered}**" if filtered else "") + "\n\n")
        preview = "\n".join(lines) or "✅ Todos depositaron."
        if len(summary) + len(preview) > 1900:
            preview = preview[:1900 - len(summary) - 40].rsplit("\n", 1)[0] + "\n… (el resto en el archivo)"
        file = discord.File(io.BytesIO(report.encode("utf-8")), filename=f"auditoria-chest-{audit_id}.txt")
        entries = to_tag(result, ids)
        view = TagView(self, audit_id, title, entries) if entries else discord.utils.MISSING
        await interaction.followup.send(summary + preview, file=file, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def audit_from_panel(self, interaction: discord.Interaction, cta_id: int):
        """Botón «Auditar» del panel de combatlog: usa los combatlogs y el chestlog ya subidos a la CTA."""
        if not is_staff_or_council(interaction.user):
            return await interaction.response.send_message("🔒 Solo el Staff o el Council.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        run = await asyncio.to_thread(_cta_row, self.db_path, cta_id, 0)
        if run is None:
            return await interaction.followup.send("❌ No encontré la CTA de este panel.", ephemeral=True)
        files, chest = await self.thread_files(interaction.guild, run)
        falta = [t for t, ok in (("combatlogs (botón **Subir combatlog**)", files), ("chestlog (botón **Subir chestlog**)", chest)) if not ok]
        if falta:
            return await interaction.followup.send("❌ Todavía no hay " + " ni ".join(falta) + " en esta CTA.", ephemeral=True)
        await self.audit_and_preview(interaction, run, files, chest)

    @chest.command(name="auditar", description="(Staff/Council) Compara los combatlogs de la CTA con el log del chest")
    @app_commands.describe(archivo="Log del chest (si no lo pasás, se usa el subido con «Subir chestlog»)",
                           archivo2="Otra parte del log del chest (opcional)",
                           cta="Número de CTA (si no lo usás en un hilo de la CTA)")
    async def auditar(self, interaction: discord.Interaction, archivo: Optional[discord.Attachment] = None,
                      archivo2: Optional[discord.Attachment] = None, cta: Optional[int] = None):
        if not is_staff_or_council(interaction.user):
            return await interaction.response.send_message("🔒 Solo el Staff o el Council.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        run = await asyncio.to_thread(_cta_row, self.db_path, cta, interaction.channel_id)
        if run is None:
            return await interaction.followup.send("❌ No encontré la CTA. Usalo en su hilo de combatlog o indicá `cta`.", ephemeral=True)
        chest: List[ca.ChestEntry] = []
        for att in (archivo, archivo2):
            if att is None:
                continue
            try:
                chest += ca.parse_chest_log(_decode(await att.read()))
            except Exception as exc:
                return await interaction.followup.send(f"❌ No pude leer {att.filename}: {exc!r}", ephemeral=True)
        if (archivo or archivo2) and not chest:
            return await interaction.followup.send("❌ El log del chest no tiene registros que entienda.", ephemeral=True)
        files, thread_chest = await self.thread_files(interaction.guild, run)
        chest = chest or thread_chest
        if not chest:
            return await interaction.followup.send("❌ No hay chestlog: adjuntalo o subilo con **Subir chestlog**.", ephemeral=True)
        if not files:
            return await interaction.followup.send("❌ Esta CTA no tiene combatlogs (del loot logger) subidos.", ephemeral=True)
        await self.audit_and_preview(interaction, run, files, chest)


async def setup_chestaudit(bot, db_path: str, guild_id: int) -> ChestAuditCog:
    cog = ChestAuditCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
