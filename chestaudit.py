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


class TagView(discord.ui.View):
    """Confirmación (Staff/Council) antes de taguear en el canal de auditoría."""
    def __init__(self, cog: "ChestAuditCog", audit_id: int, title: str, lines: List[str], report: str):
        super().__init__(timeout=900)
        self.cog, self.audit_id, self.title, self.lines, self.report = cog, audit_id, title, lines, report

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
        header = f"📦 **Chest sin depositar — {discord.utils.escape_markdown(self.title)}**"
        try:
            msgs = chunk_lines(header, self.lines)
            for i, text in enumerate(msgs):
                file = discord.File(io.BytesIO(self.report.encode("utf-8")), filename=f"auditoria-chest-{self.audit_id}.txt") \
                    if i == len(msgs) - 1 else None
                await ch.send(text, allowed_mentions=TAG_MENTIONS, **({"file": file} if file else {}))
        except Exception as exc:
            traceback.print_exc()
            return await interaction.edit_original_response(content=f"❌ No pude publicar en el canal: {exc!r}", view=None)
        await interaction.edit_original_response(content=f"✅ Tagueados **{len(self.lines)}** en {ch.mention}.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Listo, no se tagueó a nadie (la auditoría quedó guardada para las stats).",
                                                view=None)


class ChestAuditCog(commands.Cog):
    chest = app_commands.Group(name="chest", description="Auditoría del chest después de una CTA", guild_only=True)

    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(ca.init_schema, self.db_path)

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
        view = TagView(self, audit_id, title, lines, report) if lines else discord.utils.MISSING
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
