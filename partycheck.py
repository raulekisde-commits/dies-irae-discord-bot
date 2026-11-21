"""/party check: en el hilo de una CTA, lee con OCR.space los nicks de una captura de la party
(p. ej. la ventana "Region Access Priority") y los compara con los anotados. La lógica está en partycheck_db.py.
La API key se carga con /party apikey (Administradores); si no hay, se usa la variable de entorno OCR_SPACE_API_KEY."""
import asyncio
import io
import json
import os
import traceback
from typing import List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import partycheck_db as pc
from killboards import is_loader_in_cta

OCR_URL = "https://api.ocr.space/parse/image"
OCR_MAX_BYTES = 1_000_000                               # límite del plan gratis de OCR.space
MAX_IMAGE_BYTES = 10 * 1024 * 1024
NO_MENTIONS = discord.AllowedMentions.none()


def shrink(data: bytes, filename: str) -> Tuple[bytes, str]:
    """Si la imagen pasa el límite de OCR.space, la recomprime a JPEG y la achica hasta que entre."""
    if len(data) <= OCR_MAX_BYTES:
        return data, filename
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    while True:
        for q in (90, 80, 70):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            if buf.tell() <= OCR_MAX_BYTES:
                return buf.getvalue(), "party.jpg"
        if img.width < 400:
            raise RuntimeError("la imagen es demasiado grande")
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.LANCZOS)


async def ocr_words(image: bytes, filename: str, api_key: str) -> List[Tuple[str, float]]:
    form = aiohttp.FormData()
    form.add_field("file", image, filename=filename)
    for k, v in (("language", "eng"), ("OCREngine", "2"), ("isOverlayRequired", "true"), ("scale", "true")):
        form.add_field(k, v)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(OCR_URL, data=form, headers={"apikey": api_key}) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(text[:200])
    if data.get("IsErroredOnProcessing"):
        msg = data.get("ErrorMessage") or data.get("ErrorDetails") or "error de OCR"
        raise RuntimeError(" ".join(msg) if isinstance(msg, list) else str(msg))
    return pc.ocrspace_words(data)


def _field(lines: List[str]) -> str:
    """Valor de campo de embed (máx. 1024 caracteres)."""
    out, size = [], 0
    for i, ln in enumerate(lines):
        if size + len(ln) + 1 > 1000:
            out.append(f"… y {len(lines) - i} más")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out) or "—"


def build_embed(run: dict, res: dict, read: List[str]) -> discord.Embed:
    e = discord.Embed(title=f"Party check · CTA #{run['id']} · Party {res['party_no']}",
                      color=discord.Color.green() if not res["missing"] and not res["extra"] else discord.Color.orange())
    e.add_field(name=f"❌ Faltan entrar ({len(res['missing'])})",
                value=_field([f"<@{r['user_id']}> · {r['char_name']}" for r in res["missing"]]), inline=False)
    e.add_field(name=f"⚠️ En la party sin estar anotados ({len(res['extra'])})",
                value=_field([f"{n}" + (f" · anotado en Party {p}" if p else "") for n, p in res["extra"]]), inline=False)
    if res["unlinked"]:
        e.add_field(name=f"❔ Anotados sin cuenta de Albion vinculada ({len(res['unlinked'])})",
                    value=_field([f"<@{r['user_id']}>" for r in res["unlinked"]]), inline=False)
    e.set_footer(text=f"✅ {len(res['ok'])} en su lugar · {len(read)} nicks leídos de la imagen (OCR, puede fallar alguno)")
    return e


def _is_admin(member) -> bool:
    return isinstance(member, discord.Member) and (member.guild.owner_id == member.id or member.guild_permissions.administrator)


class ApiKeyModal(discord.ui.Modal, title="API key de OCR.space"):
    key = discord.ui.TextInput(label="API key", placeholder="La que te llegó por mail de OCR.space",
                               min_length=5, max_length=100)

    def __init__(self, cog: "PartyCheckCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_admin(interaction.user):
            return await interaction.response.send_message("🔒 Solo Administradores.", ephemeral=True)
        value = str(self.key.value).strip()
        await asyncio.to_thread(pc.set_api_key, self.cog.db_path, value, interaction.user.id)
        await interaction.response.send_message(f"✅ API key guardada (termina en `{value[-4:]}`).", ephemeral=True)


class PartyCheckCog(commands.Cog):
    party = app_commands.Group(name="party", description="Herramientas de party para las CTA", guild_only=True)

    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        await asyncio.to_thread(pc.init_schema, self.db_path)

    @party.command(name="apikey", description="(Admin) Carga la API key de OCR.space para /party check")
    async def apikey(self, interaction: discord.Interaction):
        if not _is_admin(interaction.user):
            return await interaction.response.send_message("🔒 Solo Administradores.", ephemeral=True)
        await interaction.response.send_modal(ApiKeyModal(self))

    @party.command(name="check", description="Compara una captura de la party con los anotados de la CTA")
    @app_commands.describe(imagen="Captura de la party (ej. ventana Region Access Priority)",
                           party="Número de party de la CTA (si no, se elige la que más coincide)",
                           cta="Número de CTA (si no lo usás en el hilo de la CTA)")
    async def check(self, interaction: discord.Interaction, imagen: discord.Attachment,
                    party: Optional[app_commands.Range[int, 1, 20]] = None, cta: Optional[int] = None):
        key = await asyncio.to_thread(pc.get_api_key, self.db_path) or os.getenv("OCR_SPACE_API_KEY")
        if not key:
            return await interaction.response.send_message("❌ Falta la API key de OCR.space: un Administrador la carga con "
                                                           "`/party apikey`.", ephemeral=True)
        if not (imagen.content_type or "").startswith("image/") or imagen.size > MAX_IMAGE_BYTES:
            return await interaction.response.send_message("❌ Adjuntá una imagen (máx. 10 MB).", ephemeral=True)
        run = await asyncio.to_thread(pc.cta_for_channel, self.db_path, interaction.channel_id, cta)
        if run is None:
            return await interaction.response.send_message("❌ No encontré la CTA. Usalo en el hilo de la CTA o indicá `cta`.",
                                                           ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            data, fname = await asyncio.to_thread(shrink, await imagen.read(), imagen.filename or "party.png")
            names = pc.extract_names(await ocr_words(data, fname, key))
        except Exception as exc:
            traceback.print_exc()
            return await interaction.followup.send(f"❌ No pude leer la imagen: {exc}")
        if not names:
            return await interaction.followup.send("❌ No encontré nicks en la imagen.")
        rows = await asyncio.to_thread(pc.signups, self.db_path, int(run["id"]))
        if not rows:
            return await interaction.followup.send(f"❌ La CTA #{run['id']} no tiene anotados.")
        res = pc.compare(names, rows, party)
        await interaction.followup.send(embed=build_embed(run, res, names), allowed_mentions=NO_MENTIONS)

    @party.command(name="mover", description="(Staff/Council/Caller) Mueve al voice a los anotados de una party que estén conectados")
    @app_commands.describe(party="Número de party de la CTA", canal="Canal de voz destino",
                           cta="Número de CTA (si no lo usás en el hilo de la CTA)")
    async def mover(self, interaction: discord.Interaction, party: app_commands.Range[int, 1, 20],
                    canal: discord.VoiceChannel, cta: Optional[int] = None):
        if not is_loader_in_cta(interaction.user):
            return await interaction.response.send_message("🔒 Solo Staff, Council o Caller.", ephemeral=True)
        run = await asyncio.to_thread(pc.cta_for_channel, self.db_path, interaction.channel_id, cta)
        if run is None:
            return await interaction.response.send_message("❌ No encontré la CTA. Usalo en el hilo de la CTA o indicá `cta`.",
                                                           ephemeral=True)
        rows = await asyncio.to_thread(pc.signups, self.db_path, int(run["id"]))
        ids = [r["user_id"] for r in rows if r["party_no"] == party]
        if not ids:
            parties = ", ".join(str(p) for p in sorted({r["party_no"] for r in rows})) or "ninguna"
            return await interaction.response.send_message(f"❌ La Party {party} no tiene anotados en la CTA #{run['id']} "
                                                           f"(con anotados: {parties}).", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        moved, already, offline, failed = [], [], [], []
        for uid in ids:
            m = interaction.guild.get_member(uid)
            if m is None or m.voice is None or m.voice.channel is None:
                offline.append(uid)
            elif m.voice.channel.id == canal.id:
                already.append(uid)
            else:
                try:
                    await m.move_to(canal, reason=f"/party mover CTA #{run['id']} Party {party} por {interaction.user}")
                    moved.append(uid)
                except Exception as exc:
                    print(f"/party mover: no pude mover a {uid}: {exc!r}")
                    failed.append(uid)
        e = discord.Embed(title=f"Party {party} → {canal.name} · CTA #{run['id']}",
                          color=discord.Color.green() if not failed else discord.Color.orange())
        e.add_field(name=f"✅ Movidos ({len(moved)})", value=_field([f"<@{u}>" for u in moved]), inline=False)
        if already:
            e.add_field(name=f"🔊 Ya estaban ({len(already)})", value=_field([f"<@{u}>" for u in already]), inline=False)
        e.add_field(name=f"💤 No conectados a voz ({len(offline)})", value=_field([f"<@{u}>" for u in offline]), inline=False)
        if failed:
            e.add_field(name=f"❌ No pude mover ({len(failed)})", value=_field([f"<@{u}>" for u in failed]) +
                        "\n(¿el bot tiene permiso «Mover miembros» en ese canal?)", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def setup_partycheck(bot, db_path: str, guild_id: int) -> PartyCheckCog:
    cog = PartyCheckCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
