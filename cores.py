"""/core y /vortex: un miembro reporta un orbe con foto; el Staff lo aprueba, rechaza, edita o reparte desde un panel.

- El comando pide los tres datos (repartir, rareza y foto); la respuesta al miembro es privada.
- El panel se publica en el canal «cores» (configurable con /canal) con la foto reenviada y los botones.
- Aprobar: 1/2/3/5 tiradas de Wujupón (Verde/Azul/Morado/Dorado) a quien lo reportó. Si pidió repartirlo, se reparte
  directamente entre quien lo reportó y los compañeros que puso: a cada uno le toca la parte entera y el sobrante se sortea.
  Si hay más personas que tiradas, se sortean todas.
- Todos los avisos van al canal general etiquetando a las personas.
- /stats cuenta, por rareza, cuántos orbes le dieron tiradas a cada persona."""
import asyncio
import glob
import io
import os
import re
import traceback
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import botsettings as bs
import cores_db as odb
from registration import can_admin_registro

MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
GENERAL_CHANNEL_ID = int(os.getenv("GENERAL_CHANNEL_ID", "1557135745523912875"))
bs.register("cores", "Panel de /core y /vortex", 0, "Acá llegan los reportes de /core y /vortex para que el Staff los apruebe")
bs.register("general", "Canal general", GENERAL_CHANNEL_ID, "Acá se avisan los resultados de /core y /vortex, etiquetando a las personas")

NO_MENTIONS = discord.AllowedMentions.none()
USER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
STATUS_TEXT = {"pending": "⏳ Pendiente", "processing": "⏳ Procesando", "approved": "✅ Aprobado",
               "shared": "🎲 Repartido", "rejected": "❌ Rechazado"}
STATUS_COLOR = {"approved": discord.Color.green(), "shared": discord.Color.gold(), "rejected": discord.Color.red()}
RARITY_COLOR = {"verde": discord.Color.green(), "azul": discord.Color.blue(), "morado": discord.Color.purple(),
                "dorado": discord.Color.gold()}
MAX_REASON = 300
MAX_MATES = 24                                     # con quien lo reportó, 25 (lo mismo que deja elegir el botón Repartir)
MENTION_RE = re.compile(r"<@!?(\d+)>")

SHARE_CHOICES = [app_commands.Choice(name="Sí", value="si"), app_commands.Choice(name="No", value="no")]
RARITY_CHOICES = [app_commands.Choice(name=odb.RARITY_LABEL[r], value=r) for r in odb.RARITIES]


def orb_name(report: dict) -> str:
    return f"{odb.KIND_LABEL[report['kind']]} {odb.RARITY_LABEL[report['rarity']]}"


def is_member(m) -> bool:
    return bool(m) and not getattr(m, "bot", False) and any(getattr(r, "id", None) == MIEMBRO_ROLE_ID for r in getattr(m, "roles", []))


def build_embed(report: dict, image_url: Optional[str] = None, awards: Optional[Dict[int, int]] = None,
                actor_id: Optional[int] = None) -> discord.Embed:
    st = report["status"]
    emb = discord.Embed(title=f"{odb.RARITY_EMOJI[report['rarity']]} {orb_name(report)} · #{report['id']}",
                        color=STATUS_COLOR.get(st) or RARITY_COLOR[report["rarity"]])
    emb.add_field(name="Reportado por", value=f"<@{report['user_id']}>", inline=True)
    emb.add_field(name="Rareza", value=odb.RARITY_LABEL[report["rarity"]], inline=True)
    emb.add_field(name="¿Lo quiere repartir?", value="Sí" if report["share"] else "No", inline=True)
    mates = odb.mates_of(report)
    if mates:
        emb.add_field(name=f"Repartir con ({len(mates)})", value=" ".join(f"<@{u}>" for u in mates)[:1000], inline=False)
    emb.add_field(name="Tiradas", value=f"🎰 {odb.RARITY_SPINS[report['rarity']]}", inline=True)
    emb.add_field(name="Estado", value=STATUS_TEXT.get(st, st) + (f" por <@{actor_id}>" if actor_id else ""), inline=True)
    if awards:
        emb.add_field(name="Tiradas entregadas", value="\n".join(f"<@{u}> → **{n}**" for u, n in awards.items())[:1000], inline=False)
    if report.get("reason"):
        emb.add_field(name="Motivo", value=report["reason"][:1000], inline=False)
    if image_url:
        emb.set_image(url=image_url)
    emb.set_footer(text=f"{odb.KIND_LABEL[report['kind']]} #{report['id']}")
    return emb


def share_text(report: dict, awards: Dict[int, int], participants: List[int]) -> str:
    total = odb.RARITY_SPINS[report["rarity"]]
    n = len(participants)
    lines = [f"🎲 **Reparto del {orb_name(report)}** de <@{report['user_id']}>: **{total}** tirada(s) de Wujupón entre **{n}** persona(s)."]
    kind = odb.KIND_LABEL[report["kind"]].lower()
    mates = odb.mates_of(report)
    if mates:
        lines.append(f"👥 Las personas del reparto las eligió <@{report['user_id']}> al poner el {kind}: "
                     + " ".join(f"<@{u}>" for u in mates))
    added = [u for u in participants if u != int(report["user_id"]) and u not in mates]
    if added:
        lines.append("➕ Sumados por el Staff: " + " ".join(f"<@{u}>" for u in added))
    if n > total:
        lines.append("Había más personas que tiradas, así que se sortearon.")
    elif total % n:
        lines.append(f"Se repartió en partes iguales y se sortearon **{total % n}** tirada(s) sobrante(s).")
    else:
        lines.append("Se repartió en partes iguales.")
    lines += [f"• <@{u}> → **{c}** tirada(s) 🎰" for u, c in awards.items()]
    left = [u for u in participants if u not in awards]
    if left:
        lines.append("Sin premio esta vez: " + " ".join(f"<@{u}>" for u in left))
    lines.append("¡Ya se les dieron las tiradas, gracias por aportar! 💚")
    return "\n".join(lines)


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


class RejectModal(discord.ui.Modal, title="Rechazar reporte"):
    motivo = discord.ui.TextInput(label="Motivo (opcional, se muestra en el general)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=MAX_REASON)

    def __init__(self, cog: "CoresCog", message):
        super().__init__()
        self.cog, self.message = cog, message

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.reject(interaction, self.message, " ".join(str(self.motivo.value or "").split()))


class EditView(discord.ui.View):
    """Cambiar el color del orbe (solo mientras está pendiente)."""

    def __init__(self, cog: "CoresCog", message, current: str):
        super().__init__(timeout=180)
        self.cog, self.message = cog, message
        sel = discord.ui.Select(placeholder="Elegí la rareza correcta", min_values=1, max_values=1,
                                options=[discord.SelectOption(label=odb.RARITY_LABEL[r], value=r, emoji=odb.RARITY_EMOJI[r],
                                                              default=r == current) for r in odb.RARITIES])
        sel.callback = self.on_pick
        self.sel = sel
        self.add_item(sel)

    async def on_pick(self, interaction: discord.Interaction):
        await self.cog.edit_rarity(interaction, self.message, self.sel.values[0])


class OrbPanelView(discord.ui.View):
    """Botones del panel (persistentes: el reporte se busca por el mensaje)."""

    def __init__(self, cog: "CoresCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.success, emoji="✅", custom_id="orb:approve")
    async def approve_btn(self, interaction: discord.Interaction, button):
        await self.cog.approve(interaction, interaction.message)

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, emoji="❌", custom_id="orb:reject")
    async def reject_btn(self, interaction: discord.Interaction, button):
        if await self.cog.pending_or_say(interaction, interaction.message) is None:
            return
        await interaction.response.send_modal(RejectModal(self.cog, interaction.message))

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id="orb:edit")
    async def edit_btn(self, interaction: discord.Interaction, button):
        report = await self.cog.pending_or_say(interaction, interaction.message)
        if report is None:
            return
        await _say(interaction, f"✏️ Elegí la rareza correcta del **{orb_name(report)}** #{report['id']}.",
                   view=EditView(self.cog, interaction.message, report["rarity"]))


class CoresCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.view = OrbPanelView(self)
        self.bot.add_view(self.view)

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    async def _channel(self, channel_id: int):
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        return ch

    # ---------- foto ----------
    def _photo_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "orb_photos")

    def _photo_paths(self, report_id: int) -> List[str]:
        return glob.glob(os.path.join(self._photo_dir(), f"{int(report_id)}.*"))

    def _save_photo(self, report_id: int, filename: str, data: bytes) -> None:
        """Guarda la foto en disco al recibir el reporte: así el aviso del general no depende de los adjuntos de Discord."""
        try:
            os.makedirs(self._photo_dir(), exist_ok=True)
            ext = os.path.splitext(filename or "")[1].lower()
            ext = ext if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp") else ".png"
            with open(os.path.join(self._photo_dir(), f"{int(report_id)}{ext}"), "wb") as f:
                f.write(data)
        except Exception:
            traceback.print_exc()

    def _drop_photo(self, report_id: int) -> None:
        for path in self._photo_paths(report_id):
            try:
                os.remove(path)
            except Exception:
                pass

    async def _photo(self, report_id: Optional[int], message=None):
        """La foto del reporte lista para subir: del disco; si no está, del adjunto del panel; si no, de la imagen del embed."""
        for path in self._photo_paths(report_id) if report_id else []:
            try:
                with open(path, "rb") as f:
                    return discord.File(io.BytesIO(f.read()), filename=f"foto{os.path.splitext(path)[1]}")
            except Exception:
                traceback.print_exc()
        for att in getattr(message, "attachments", None) or []:
            try:
                return await att.to_file()
            except Exception:
                traceback.print_exc()
        url = self._image_url(message) if message is not None else None
        session = getattr(self.bot, "http_session", None)
        if url and url.startswith("http") and session is not None:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return discord.File(io.BytesIO(await resp.read()), filename="foto.png")
            except Exception:
                traceback.print_exc()
        print(f"/core: no pude conseguir la foto del reporte #{report_id} para el aviso.")
        return None

    async def announce(self, text: str, message=None, report_id: Optional[int] = None):
        """Aviso en el canal general, etiquetando a las personas, con la foto del reporte si se puede."""
        ch = await self._channel(bs.get("general"))
        if ch is None:
            print("/core: no encuentro el canal general para avisar.")
            return False
        try:
            file = await self._photo(report_id, message) if (message is not None or report_id) else None
            if file is not None:
                await ch.send(text[:2000], file=file, allowed_mentions=USER_MENTIONS)
            else:
                await ch.send(text[:2000], allowed_mentions=USER_MENTIONS)
            if report_id:
                self._drop_photo(report_id)
            return True
        except Exception:
            traceback.print_exc()
            return False

    # ---------- comandos ----------
    async def _parse_mates(self, guild, author, text: Optional[str]):
        """Personas mencionadas en «compañeros». Devuelve (miembros, error)."""
        if not text or not text.strip():
            return [], None
        ids = list(dict.fromkeys(int(x) for x in MENTION_RE.findall(text)))
        if not ids:
            return [], "❌ En **compañeros** mencioná a las personas con @ (por ejemplo: `@Ana @Beto`)."
        ids = [u for u in ids if u != author.id]
        if len(ids) > MAX_MATES:
            return [], f"❌ Podés poner hasta {MAX_MATES} compañeros."
        out, bad = [], []
        for uid in ids:
            m = guild.get_member(uid)
            if m is None:
                try:
                    m = await guild.fetch_member(uid)
                except Exception:
                    m = None
            (out if is_member(m) else bad).append(m if m is not None else uid)
        if bad:
            return [], ("❌ Solo se puede repartir entre miembros. No tienen el rol de miembro: "
                        + " ".join(getattr(b, "mention", f"<@{b}>") for b in bad))
        return out, None

    async def submit(self, interaction: discord.Interaction, kind: str, repartir: str, rareza: str, foto,
                     companeros: Optional[str] = None):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _say(interaction, "❌ Solo disponible en el servidor.")
        if not is_member(interaction.user):
            return await _say(interaction, f"🔒 Solo los miembros pueden usar /{kind}.")
        if repartir not in ("si", "no") or rareza not in odb.RARITIES or foto is None:
            return await _say(interaction, "❌ Tenés que completar todos los datos: si lo querés repartir, la rareza y la foto.")
        ctype = (getattr(foto, "content_type", None) or "").lower()
        fname = (getattr(foto, "filename", "") or "").lower()
        if not (ctype.startswith("image/") or fname.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
            return await _say(interaction, "❌ La foto tiene que ser una imagen (png, jpg, gif o webp).")
        mates, err = await self._parse_mates(interaction.guild, interaction.user, companeros)
        if err:
            return await _say(interaction, err)
        if mates and repartir != "si":
            return await _say(interaction, "❌ Pusiste compañeros pero elegiste **Repartir: No**. Elegí **Sí** para repartirlo con ellos.")
        if repartir == "si" and not mates:
            return await _say(interaction, "❌ Si elegís **Repartir: Sí**, tenés que poner con quiénes en **compañeros** "
                                           "(mencioná a una o varias personas: `@persona @persona`).")
        ch = await self._channel(bs.get("cores"))
        if ch is None:
            return await _say(interaction, "❌ El canal del panel de cores no está configurado. Avisale al Staff (se configura con `/canal`).")
        await interaction.response.defer(ephemeral=True)
        rid = await self.run(odb.create, guild_id=interaction.guild.id, kind=kind, user_id=interaction.user.id, rarity=rareza,
                             share=repartir == "si", mates=[m.id for m in mates])
        report = await self.run(odb.get, rid)
        try:
            try:
                data = await foto.read()
                self._save_photo(rid, foto.filename, data)
                file = discord.File(io.BytesIO(data), filename=foto.filename)
            except Exception:
                file = await foto.to_file()
            emb = build_embed(report, image_url=f"attachment://{file.filename}")
            msg = await ch.send(embed=emb, file=file, view=self.view, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self.run(odb.delete, rid)
            self._drop_photo(rid)
            return await _say(interaction, "❌ No pude publicar el reporte. Probá de nuevo en un rato.")
        await self.run(odb.set_message, rid, msg.channel.id if getattr(msg, "channel", None) else ch.id, msg.id)
        con = f" para repartir con {len(mates)} compañero(s)" if mates else ""
        await _say(interaction, f"✅ Recibimos tu **{orb_name(report)}** (#{rid}){con}. El Staff lo va a revisar y el resultado se avisa en "
                                f"<#{bs.get('general')}>.")

    @app_commands.command(name="core", description="Reportar un core con foto para recibir tiradas de Wujupón")
    @app_commands.describe(repartir="¿Lo querés repartir?", rareza="Rareza del core", foto="Foto del core",
                           companeros="Obligatorio si repartís: mencioná a uno o varios (@persona @persona)")
    @app_commands.choices(repartir=SHARE_CHOICES, rareza=RARITY_CHOICES)
    @app_commands.rename(companeros="compañeros")
    @app_commands.guild_only()
    async def core_cmd(self, interaction: discord.Interaction, repartir: app_commands.Choice[str], rareza: app_commands.Choice[str],
                       foto: discord.Attachment, companeros: Optional[str] = None):
        await self.submit(interaction, "core", repartir.value, rareza.value, foto, companeros)

    @app_commands.command(name="vortex", description="Reportar un vortex con foto para recibir tiradas de Wujupón")
    @app_commands.describe(repartir="¿Lo querés repartir?", rareza="Rareza del vortex", foto="Foto del vortex",
                           companeros="Obligatorio si repartís: mencioná a uno o varios (@persona @persona)")
    @app_commands.choices(repartir=SHARE_CHOICES, rareza=RARITY_CHOICES)
    @app_commands.rename(companeros="compañeros")
    @app_commands.guild_only()
    async def vortex_cmd(self, interaction: discord.Interaction, repartir: app_commands.Choice[str], rareza: app_commands.Choice[str],
                         foto: discord.Attachment, companeros: Optional[str] = None):
        await self.submit(interaction, "vortex", repartir.value, rareza.value, foto, companeros)

    # ---------- panel ----------
    async def pending_or_say(self, interaction: discord.Interaction, message) -> Optional[dict]:
        if not can_admin_registro(interaction.user):
            await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar estos botones.")
            return None
        report = await self.run(odb.get_by_message, getattr(message, "id", 0))
        if report is None:
            await _say(interaction, "❌ No encontré este reporte.")
            return None
        if report["status"] != "pending":
            await _say(interaction, f"ℹ️ Este reporte ya está {STATUS_TEXT.get(report['status'], report['status']).lower()}.")
            return None
        return report

    @staticmethod
    def _image_url(message) -> Optional[str]:
        try:
            emb = (getattr(message, "embeds", None) or [None])[0]
            img = getattr(emb, "image", None)
            if isinstance(img, str):
                return img
            return getattr(img, "url", None) or (img.get("url") if isinstance(img, dict) else None)
        except Exception:
            return None

    async def _refresh(self, message, report: dict, awards=None, actor_id=None, final: bool = False):
        try:
            await message.edit(embed=build_embed(report, self._image_url(message), awards, actor_id),
                               view=None if final else self.view, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    async def _grant(self, report: dict, awards: Dict[int, int], actor_id: int) -> Optional[str]:
        """Da las tiradas (idempotente por reporte y persona). Devuelve el error o None."""
        wcog = self.bot.get_cog("WujuponCog")
        if wcog is None:
            return "❌ El módulo de Wujupón no está cargado. No se dio nada."
        import wujupon_db as wdb
        try:
            for uid, n in awards.items():
                await wcog.run(wdb.gift_spins, actor_id=actor_id, user_id=uid, count=n, request_key=f"orb:{report['id']}:{uid}",
                               reason=f"{orb_name(report)} #{report['id']}")
        except Exception as exc:
            traceback.print_exc()
            return f"❌ No pude dar las tiradas ({exc}). El reporte quedó pendiente: volvé a intentarlo."
        return None

    async def _decide(self, interaction, message, status: str, awards: Dict[int, int], reason: Optional[str] = None):
        """Toma el reporte, da las tiradas y lo cierra. Devuelve el reporte cerrado o None (ya avisó el error)."""
        report = await self.run(odb.get_by_message, getattr(message, "id", 0))
        claimed = await self.run(odb.claim, report["id"]) if report else None
        if claimed is None:
            await _say(interaction, "ℹ️ Este reporte ya lo resolvió otra persona.")
            return None
        if awards:
            err = await self._grant(claimed, awards, interaction.user.id)
            if err:
                await self.run(odb.release, claimed["id"])
                await _say(interaction, err)
                return None
        await self.run(odb.finish, claimed["id"], status, interaction.user.id, awards, reason)
        done = await self.run(odb.get, claimed["id"])
        await self._refresh(message, done, awards, interaction.user.id, final=True)
        return done

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Paneles viejos todavía tienen el botón «Repartir» (orb:share): ahora hace lo mismo que Aprobar."""
        data = getattr(interaction, "data", None) or {}
        if data.get("custom_id") == "orb:share" and not interaction.response.is_done():
            await self.approve(interaction, interaction.message)

    async def approve(self, interaction: discord.Interaction, message):
        report = await self.pending_or_say(interaction, message)
        if report is None:
            return
        if report["share"]:                            # pidió repartirlo: se reparte con los que puso, sin elegir a nadie más
            return await self.share(interaction, message, [])
        await interaction.response.defer(ephemeral=True)
        n = odb.RARITY_SPINS[report["rarity"]]
        done = await self._decide(interaction, message, "approved", {int(report["user_id"]): n})
        if done is None:
            return
        await self.announce(f"✅ <@{done['user_id']}> tu **{orb_name(done)}** fue aprobado: ya se te dieron **{n}** tirada(s) de "
                            f"Wujupón 🎰. ¡Gracias por aportar a la guild! 💚", message, done["id"])
        await _say(interaction, f"✅ Aprobado: {n} tirada(s) para <@{done['user_id']}>.")

    async def reject(self, interaction: discord.Interaction, message, reason: str):
        report = await self.pending_or_say(interaction, message)
        if report is None:
            return
        await interaction.response.defer(ephemeral=True)
        done = await self._decide(interaction, message, "rejected", {}, reason[:MAX_REASON] or None)
        if done is None:
            return
        await self.announce(f"❌ <@{done['user_id']}> tu **{orb_name(done)}** fue rechazado."
                            + (f"\n📝 Motivo: {reason[:MAX_REASON]}" if reason else "")
                            + "\nSi creés que es un error, hablá con el Staff.", message, done["id"])
        await _say(interaction, "❌ Reporte rechazado.")

    async def edit_rarity(self, interaction: discord.Interaction, message, rarity: str):
        report = await self.pending_or_say(interaction, message)
        if report is None:
            return
        if not await self.run(odb.set_rarity, report["id"], rarity):
            return await _say(interaction, "ℹ️ El reporte ya no está pendiente.")
        report = await self.run(odb.get, report["id"])
        await self._refresh(message, report)
        await _say(interaction, f"✏️ Ahora es **{orb_name(report)}** ({odb.RARITY_SPINS[rarity]} tirada(s)).")

    async def share(self, interaction: discord.Interaction, message, picked):
        report = await self.pending_or_say(interaction, message)
        if report is None:
            return
        bad = [u for u in picked if not is_member(u)]
        if bad:
            return await _say(interaction, "❌ Solo se puede repartir entre miembros. No tienen el rol de miembro: "
                                           + " ".join(getattr(u, "mention", str(u)) for u in bad))
        participants = list(dict.fromkeys([int(report["user_id"])] + odb.mates_of(report) + [int(u.id) for u in picked]))
        await interaction.response.defer(ephemeral=True)
        awards = odb.allocate(odb.RARITY_SPINS[report["rarity"]], participants)
        done = await self._decide(interaction, message, "shared", awards)
        if done is None:
            return
        await self.announce(share_text(done, awards, participants), message, done["id"])
        await _say(interaction, f"🎲 Repartido entre {len(participants)} persona(s).")


async def setup_cores(bot: commands.Bot, db_path: str, guild_id: int) -> CoresCog:
    await asyncio.to_thread(odb.init_schema, db_path)
    cog = CoresCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
