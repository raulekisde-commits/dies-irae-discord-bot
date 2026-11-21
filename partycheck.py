"""/party check: en el hilo de una CTA, lee con Google Cloud Vision los nicks de una captura de la party
(p. ej. la ventana "Region Access Priority") y los compara con los anotados. La lógica está en partycheck_db.py.
Necesita la variable de entorno GOOGLE_VISION_API_KEY."""
import asyncio
import base64
import os
import traceback
from typing import List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import partycheck_db as pc

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
NO_MENTIONS = discord.AllowedMentions.none()


async def vision_text(image: bytes, api_key: str) -> dict:
    body = {"requests": [{"image": {"content": base64.b64encode(image).decode()},
                          "features": [{"type": "TEXT_DETECTION"}]}]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post(VISION_URL, params={"key": api_key}, json=body) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {(data.get('error') or {}).get('message', data)}")
    res = (data.get("responses") or [{}])[0]
    if res.get("error"):
        raise RuntimeError(res["error"].get("message", res["error"]))
    return res


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


class PartyCheckCog(commands.Cog):
    party = app_commands.Group(name="party", description="Herramientas de party para las CTA", guild_only=True)

    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    @party.command(name="check", description="Compara una captura de la party con los anotados de la CTA")
    @app_commands.describe(imagen="Captura de la party (ej. ventana Region Access Priority)",
                           party="Número de party de la CTA (si no, se elige la que más coincide)",
                           cta="Número de CTA (si no lo usás en el hilo de la CTA)")
    async def check(self, interaction: discord.Interaction, imagen: discord.Attachment,
                    party: Optional[app_commands.Range[int, 1, 20]] = None, cta: Optional[int] = None):
        key = os.getenv("GOOGLE_VISION_API_KEY")
        if not key:
            return await interaction.response.send_message("❌ Falta configurar GOOGLE_VISION_API_KEY.", ephemeral=True)
        if not (imagen.content_type or "").startswith("image/") or imagen.size > MAX_IMAGE_BYTES:
            return await interaction.response.send_message("❌ Adjuntá una imagen (máx. 10 MB).", ephemeral=True)
        run = await asyncio.to_thread(pc.cta_for_channel, self.db_path, interaction.channel_id, cta)
        if run is None:
            return await interaction.response.send_message("❌ No encontré la CTA. Usalo en el hilo de la CTA o indicá `cta`.",
                                                           ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            names = pc.extract_names(await vision_text(await imagen.read(), key))
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


async def setup_partycheck(bot, db_path: str, guild_id: int) -> PartyCheckCog:
    cog = PartyCheckCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
