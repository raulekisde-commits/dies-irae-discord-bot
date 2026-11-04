"""Registro y vinculación Discord ↔ Albion Online: /registrar, /mi_registro, /desvincular, comandos de administración
y el filtro central `require_registered_user` para los comandos de los miembros.

═══ Qué garantiza este registro (y qué NO) ═══
La API pública de Albion (gameinfo) no tiene inicio de sesión ni OAuth, y no existe un servicio oficial para vincular Discord con
un personaje. Consultarla solo demuestra que el personaje EXISTE y a qué guild pertenece: NO demuestra quién lo controla.
Por eso este sistema NO verifica la propiedad del personaje. Lo que sí garantiza:
  * El nombre se toma EXACTO de la API (no se puede inventar uno) y el personaje debe figurar en la guild configurada.
  * Un personaje solo puede estar vinculado a UNA cuenta de Discord y una cuenta solo puede tener UN personaje.
  * Nadie registra a otra persona sin su consentimiento: debe aceptar ella misma, desde su cuenta, o no se guarda nada.
  * Si dos personas reclaman el mismo personaje, gana quien lo registró primero y un administrador resuelve con /admin_desvincular.
"""
import asyncio
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import registration_db as rdb
from deposits import is_council
from guildshop import can_manage

STAFF_ROLE_ID = 1257896709246423083


def can_admin_registro(member) -> bool:
    """Comandos admin_* del registro: Council, administradores o rol Staff."""
    return can_manage(member) or any(getattr(r, "id", None) == STAFF_ROLE_ID for r in getattr(member, "roles", []))

NO_MENTIONS = discord.AllowedMentions.none()
GOLD = discord.Color.gold()
GREY = discord.Color.light_grey()

GAMEINFO_BASES = [
    ("Américas", "https://gameinfo.albiononline.com"),
    ("Europa", "https://gameinfo-ams.albiononline.com"),
    ("Asia", "https://gameinfo-sgp.albiononline.com"),
]
HTTP_TIMEOUT = 20
HTTP_RETRIES = 2                          # reintentos ante errores temporales del servidor de Albion
HTTP_RETRY_DELAY = 1.5
RETRY_STATUS = {429, 500, 502, 503, 504}
RECRUIT_PENDING_DAYS = 14                # si no entra a la guild en este tiempo, se cancela la espera
ROSTER_REFRESH_MINUTES = 30            # cada cuánto se vuelve a bajar la lista de miembros de la guild
ROSTER_MAX_AGE_SECONDS = 24 * 3600      # una lista guardada sirve hasta 24 h aunque la API falle; recién después se vuelve a consultar en vivo
ROSTER_MISS_REFRESH_SECONDS = 300       # si alguien no figura, se vuelve a bajar la lista como máximo cada 5 min (una sola consulta)

# Comandos que siempre se pueden usar aunque no estés registrado (son los del propio registro).
EXEMPT_COMMANDS = {"registrar", "mi_registro", "desvincular", "admin_registro", "admin_desvincular", "admin_vincular", "sincronizar-apodos", "sincronizar-registros",
                   "canal registro", "registro_obligatorio", "sin-albion", "sin-discord", "guild-sin-discord", "reclutas-pendientes", "info", "info-staff", "recuperar-lista", "recuperar", "estado-api"}


MAX_EXPORT_BYTES = 2_000_000


def parse_guild_export(data: bytes) -> List[str]:
    """Nombres de personaje del archivo que exporta Albion con la lista de la guild
    ("Character Name"  "Last Seen"  "Roles", separado por tabs o comas; UTF-8 o UTF-16)."""
    import csv
    import io
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    delim = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else ";")
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    col = 0
    if rows and any("character" in (c or "").casefold() for c in rows[0]):
        col = next(i for i, c in enumerate(rows[0]) if "character" in (c or "").casefold())
        rows = rows[1:]
    seen, out = set(), []
    for r in rows:
        name = (r[col] if len(r) > col else "").strip().strip('"').strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


class RegistrationRequired(commands.CheckFailure):
    """Se usa solo para cortar un comando con prefijo (!) de alguien sin registro; no es un error real."""


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


# ============================================================
# Consulta a Albion
# ============================================================
@dataclass
class Character:
    char_id: str
    name: str
    region: str
    guild: str
    alliance: str
    kill_fame: int
    death_fame: int


@dataclass
class Lookup:
    kind: str                                  # ok | none | wrong_guild | not_in_roster | ambiguous | error
    character: Optional[Character] = None
    options: List[Character] = field(default_factory=list)
    detail: str = ""


def _character(raw: dict, region: str) -> Optional[Character]:
    try:
        return Character(str(raw["Id"]), str(raw["Name"]), region, str(raw.get("GuildName") or ""), str(raw.get("AllianceName") or ""),
                         int(raw.get("KillFame") or 0), int(raw.get("DeathFame") or 0))
    except (KeyError, TypeError, ValueError):
        return None


# ============================================================
# Vistas (los botones comprueban SIEMPRE quién los toca)
# ============================================================
class TargetView(discord.ui.View):
    """Botones para el dueño de la cuenta de Discord: aceptar o rechazar la solicitud."""

    def __init__(self, cog: "RegistrationCog", request_id: Optional[int] = None):
        super().__init__(timeout=None if request_id is None else 900.0)
        self.cog, self.request_id = cog, request_id

    @discord.ui.button(label="Es mi personaje", style=discord.ButtonStyle.success, custom_id="reg:accept", emoji="✅")
    async def accept(self, interaction: discord.Interaction, button):
        await self.cog.on_accept(interaction, self.request_id)

    @discord.ui.button(label="No, cancelar", style=discord.ButtonStyle.secondary, custom_id="reg:decline", emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button):
        await self.cog.on_decline(interaction, self.request_id)


class YesNoView(discord.ui.View):
    """Confirmación simple para /desvincular y /admin_desvincular: solo la persona indicada puede pulsar."""

    def __init__(self, owner_id: int, on_yes: Callable, label: str):
        super().__init__(timeout=60.0)
        self.owner_id, self.on_yes = owner_id, on_yes
        yes = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        yes.callback, no.callback = self._yes, self._no
        self.add_item(yes)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _yes(self, interaction: discord.Interaction):
        self.stop()
        await self.on_yes(interaction)

    async def _no(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Cancelado. No se cambió nada.", view=None)


# ============================================================
# Cog
# ============================================================
class RegistrationCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int, *, required_guild: str = "", default_log_channel: int = 0,
                 albion_guild_id: str = "", member_role_id: int = 0, public_role_id: int = 0):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.albion_guild_id = (albion_guild_id or "").strip()
        self.member_role_id = int(member_role_id or 0)
        self.public_role_id = int(public_role_id or 0)
        self.roster: Dict[str, Character] = {}      # nombre (casefold) → personaje, de la última lista bajada
        self.roster_at = 0.0                        # time.time() de la última lista buena
        self.roster_miss_at = 0.0
        self.required_guild = (required_guild or "").strip()
        self.default_log_channel = int(default_log_channel or 0)
        self.fetch_json = self._fetch_json          # los tests lo reemplazan: nunca tocan la red

    # ---------- infraestructura ----------
    async def cog_load(self):
        await self.run(rdb.init_schema)
        await self._load_saved_roster()
        self.bot.add_view(TargetView(self))
        if not self.sweeper.is_running():
            self.sweeper.start()
        if self.albion_guild_id and not self.roster_loop.is_running():
            self.roster_loop.start()
        if not self.recruit_loop.is_running():
            self.recruit_loop.start()

    async def cog_unload(self):
        self.sweeper.cancel()
        self.roster_loop.cancel()
        self.recruit_loop.cancel()

    # ---------- lista de miembros de la guild (se baja cada tanto, no en cada registro) ----------
    async def _load_saved_roster(self):
        """Recupera la última lista guardada, para no depender de la API al reiniciar el bot."""
        import json
        try:
            raw = await self.run(rdb.get_flag, rdb.ROSTER_KEY)
            data = json.loads(raw) if raw else None
            if data and data.get("guild") == self.albion_guild_id:
                members = [Character(*m) for m in data["members"]]
                if members:
                    self.roster = {c.name.casefold(): c for c in members}
                    self.roster_at = float(data["at"])
        except Exception:
            traceback.print_exc()

    async def _save_roster(self):
        import json
        try:
            payload = {"guild": self.albion_guild_id, "at": self.roster_at,
                       "members": [[c.char_id, c.name, c.region, c.guild, c.alliance, c.kill_fame, c.death_fame] for c in self.roster.values()]}
            await self.run(rdb.set_flag, rdb.ROSTER_KEY, json.dumps(payload, separators=(",", ":")))
        except Exception:
            traceback.print_exc()

    async def refresh_roster(self) -> bool:
        """Baja la lista de miembros de la guild y la guarda en memoria. Si falla, conserva la anterior."""
        import time
        if not self.albion_guild_id:
            return False
        errors = []
        bases = sorted(GAMEINFO_BASES, key=lambda rb: rb[0] != getattr(self, "_roster_region", None))   # primero el servidor que ya funcionó
        for region, base in bases:
            try:
                data = await asyncio.wait_for(self.fetch_json(f"{base}/api/gameinfo/guilds/{self.albion_guild_id}/members"), HTTP_TIMEOUT + 5)
            except Exception as exc:
                errors.append(f"{region}: {type(exc).__name__} {exc}")
                continue
            members = [c for c in (_character(m, region) for m in (data if isinstance(data, list) else []) if isinstance(m, dict)) if c is not None]
            if not members:                           # lista vacía = respuesta sospechosa: no pisamos la buena
                errors.append(f"{region}: lista vacía")
                continue
            for c in members:
                if not c.guild:
                    c.guild = self.required_guild
            self.roster = {c.name.casefold(): c for c in members}
            self.roster_at = time.time()
            self._roster_region = region
            await self._save_roster()
            return True
        print(f"⚠️ Registro: no pude actualizar la lista de la guild: {' | '.join(errors)[:300]}")
        return False

    def roster_fresh(self) -> bool:
        import time
        return bool(self.roster) and (time.time() - self.roster_at) < ROSTER_MAX_AGE_SECONDS

    @tasks.loop(minutes=ROSTER_REFRESH_MINUTES)
    async def roster_loop(self):
        try:
            await self.refresh_roster()
        except Exception:
            traceback.print_exc()

    @roster_loop.before_loop
    async def _roster_ready(self):
        await self.bot.wait_until_ready()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    async def _fetch_json(self, url: str, params: Optional[dict] = None):
        import aiohttp
        session = getattr(self.bot, "http_session", None)
        own = session is None
        if own:
            session = aiohttp.ClientSession()
        try:
            for attempt in range(HTTP_RETRIES + 1):
                async with session.get(url, params=params, headers={"User-Agent": "SmoggBot/1.0", "Accept": "application/json"},
                                       timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status in RETRY_STATUS and attempt < HTTP_RETRIES:      # 502/503/504/429: el servidor de Albion se cae seguido, se reintenta
                        pass
                    else:
                        raise RuntimeError(f"HTTP {resp.status}")
                await asyncio.sleep(HTTP_RETRY_DELAY * (attempt + 1))
        finally:
            if own:
                await session.close()

    async def _get_channel(self, channel_id):
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def log_channel(self):
        configured = await self.run(rdb.get_flag, rdb.REVIEW_CHANNEL_KEY)
        for cid in (int(configured) if configured and configured.isdigit() else 0, self.default_log_channel):
            ch = await self._get_channel(cid)
            if ch is not None:
                return ch
        return None

    async def audit_log(self, text: str):
        """Aviso en el canal de registros (sin secretos)."""
        ch = await self.log_channel()
        if ch is None:
            return
        try:
            await ch.send(text, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ Registro: no pude escribir en el canal de registros: {exc!r}")

    # ---------- consulta de personaje ----------
    async def lookup_character(self, name: str, any_guild: bool = False, live: bool = False) -> Lookup:
        name = _clean_name(name)
        if not (2 <= len(name) <= 30) or not re.fullmatch(r"[\w .'\-]+", name, re.UNICODE):
            return Lookup("none", detail="Nombre de personaje inválido.")

        if self.roster_fresh():                       # camino normal: se resuelve con la lista guardada, sin tocar la API
            hit = self.roster.get(name.casefold())
            if hit is not None:
                return Lookup("ok", character=hit)
            if not any_guild and not live:
                # No figura: puede haber entrado hace poco. Se baja la lista de nuevo (UNA consulta, como máximo cada 5 min) y se decide con ella.
                import time
                if time.time() - self.roster_miss_at >= ROSTER_MISS_REFRESH_SECONDS:
                    self.roster_miss_at = time.time()
                    refreshed = await self.refresh_roster()
                    hit = self.roster.get(name.casefold())
                    if hit is not None:
                        return Lookup("ok", character=hit)
                    if not refreshed:                  # la API no respondió: no se puede afirmar que no esté en la guild
                        return Lookup("error", detail="No pude actualizar la lista de la guild (la API de Albion está fallando).")
                return Lookup("not_in_roster", detail=self.required_guild)

        async def one(region, base):
            data = await self.fetch_json(f"{base}/api/gameinfo/search", {"q": name})
            players = data.get("players", []) if isinstance(data, dict) else []
            return [c for c in (_character(p, region) for p in players if isinstance(p, dict))
                    if c is not None and c.name.casefold() == name.casefold()]

        results = await asyncio.gather(*(asyncio.wait_for(one(r, b), HTTP_TIMEOUT + 5) for r, b in GAMEINFO_BASES), return_exceptions=True)
        exact: List[Character] = []
        errors = []
        for (region, _b), res in zip(GAMEINFO_BASES, results):
            if isinstance(res, Exception):
                errors.append(f"{region}: {type(res).__name__} {res}".strip())
            else:
                exact.extend(res)
        if not exact:
            if errors and self.roster:                # la API falla: sirve la lista guardada aunque sea vieja (mejor eso que bloquear el registro)
                hit = self.roster.get(name.casefold())
                if hit is not None:
                    print(f"⚠️ Registro: API caída ({' | '.join(errors)[:150]}); uso la lista guardada de hace {int((__import__('time').time() - self.roster_at) // 60)} min.")
                    return Lookup("ok", character=hit)
            if errors:
                return Lookup("error", detail=" | ".join(errors)[:400])
            return Lookup("none")
        if any_guild and len(exact) > 1 and self.required_guild:      # reclutamiento: si hay homónimos, gana el que ya está en la guild
            mine = [c for c in exact if c.guild.casefold() == self.required_guild.casefold()]
            if len(mine) == 1:
                exact = mine
        if self.required_guild and not any_guild:
            in_guild = [c for c in exact if c.guild.casefold() == self.required_guild.casefold()]
            if not in_guild:
                return Lookup("wrong_guild", character=exact[0], detail=self.required_guild)
            exact = in_guild
        if len(exact) > 1:
            return Lookup("ambiguous", options=exact)
        return Lookup("ok", character=exact[0])

    def character_embed(self, c: Character, *, title: str = "🧾 Personaje encontrado") -> discord.Embed:
        e = discord.Embed(title=title, color=GOLD)
        e.add_field(name="Nombre exacto", value=f"**{discord.utils.escape_markdown(c.name)}**", inline=True)
        e.add_field(name="Servidor", value=c.region, inline=True)
        e.add_field(name="Guild", value=discord.utils.escape_markdown(c.guild) or "—", inline=True)
        if c.alliance:
            e.add_field(name="Alianza", value=discord.utils.escape_markdown(c.alliance), inline=True)
        e.add_field(name="Fama PvE/PvP", value=f"Kill fame {c.kill_fame:,} · Death fame {c.death_fame:,}", inline=False)
        return e

    # ---------- /registrar ----------
    @commands.command(name="registrar")
    async def registrar_prefijo(self, ctx: commands.Context, *, _resto: str = ""):
        """`!registrar` no registra: explica que es un comando de barra (muchos lo prueban con `!`)."""
        await ctx.reply("ℹ️ El registro se hace con el comando de barra: escribí `/registrar` y elegí **usuario_de_albion** "
                        "(tu nombre exacto en el juego). Si al escribir `/` no te aparece, reiniciá Discord (Ctrl+R) o revisá que el bot "
                        "tenga permisos de comandos de aplicación en el canal.", mention_author=False, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="registrar", description="Vinculá tu cuenta de Discord con un personaje de Albion Online")
    @app_commands.describe(usuario_de_albion="Nombre exacto del personaje en Albion",
                           usuario="(Opcional) Otro miembro: él mismo tendrá que aceptar desde su cuenta")
    @app_commands.guild_only()
    async def registrar(self, interaction: discord.Interaction, usuario_de_albion: str, usuario: Optional[discord.Member] = None):
        me = interaction.user
        target = usuario or me
        if interaction.guild is None or not isinstance(me, discord.Member):
            return await _say(interaction, "❌ Solo disponible dentro del servidor.")
        if getattr(target, "bot", False):
            return await _say(interaction, "❌ No se puede registrar a un bot.")
        await interaction.response.defer(ephemeral=True)
        look = await self.lookup_character(usuario_de_albion)
        if look.kind == "error":
            print(f"⚠️ /registrar: gameinfo falló: {look.detail}")
            return await _say(interaction, "❌ No pude consultar la API de Albion ahora (puede estar caída o limitando consultas). Probá de nuevo en unos minutos.")
        if look.kind == "none":
            return await _say(interaction, f"❌ No encontré ningún personaje llamado **{discord.utils.escape_markdown(_clean_name(usuario_de_albion))}**. "
                                           "Escribilo exactamente como aparece en el juego.")
        if look.kind == "not_in_roster":
            return await _say(interaction, f"❌ No encuentro **{discord.utils.escape_markdown(_clean_name(usuario_de_albion))}** entre los miembros de la guild **{look.detail}**. "
                                           "Revisá que esté escrito exactamente como en el juego y que ya hayas entrado a la guild. "
                                           "Si entraste hace poco, probá de nuevo en unos minutos (la lista se actualiza sola).")
        if look.kind == "wrong_guild":
            return await _say(interaction, f"❌ Ese personaje existe pero no figura en la guild **{look.detail}**, así que no se puede registrar acá. "
                                           "Si acabás de entrar a la guild, esperá unos minutos a que la API se actualice.")
        if look.kind == "ambiguous":
            return await _say(interaction, "❌ Hay varios personajes con ese nombre en distintos servidores de Albion. Pedile a un administrador que use `/admin_vincular`.")
        c = look.character
        try:
            req = await self.run(rdb.create_request, target_id=target.id, initiator_id=me.id, char_id=c.char_id, char_name=c.name, region=c.region,
                                 guild_name=c.guild, info={"alliance": c.alliance, "kill_fame": c.kill_fame, "death_fame": c.death_fame},
                                 channel_id=getattr(interaction.channel, "id", None))
        except rdb.RegError as exc:
            return await _say(interaction, f"❌ {exc}")
        embed = self.character_embed(c)
        if target.id == me.id:
            embed.set_footer(text="Si es tu personaje, tocá «Es mi personaje». La solicitud vence en 15 minutos.")
            await interaction.followup.send("Confirmá que este es tu personaje:", embed=embed, view=TargetView(self, req.id), ephemeral=True)
            return
        embed.set_footer(text=f"Solicitud iniciada por {me.display_name}. Solo {target.display_name} puede aceptarla (15 minutos).")
        ch = interaction.channel
        try:
            msg = await ch.send(f"{target.mention}, {me.mention} quiere vincular tu cuenta con este personaje de Albion. "
                                f"**Solo vos podés aceptar o rechazar.**", embed=embed, view=TargetView(self),
                                allowed_mentions=discord.AllowedMentions(users=[target]))
        except Exception:
            traceback.print_exc()
            await self.run(rdb.close_request, req.id, me.id, "cancelled", "no pude publicar la solicitud")
            return await _say(interaction, "❌ No pude enviar la solicitud en este canal (revisá los permisos del bot).")
        await self.run(rdb.set_messages, req.id, prompt_msg_id=msg.id, channel_id=ch.id)
        await interaction.followup.send(f"📨 Le envié la solicitud a {target.mention}. No se guarda nada hasta que acepte.",
                                        ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _request_for(self, interaction, request_id: Optional[int]):
        if request_id is not None:
            return await self.run(rdb.get_request, request_id)
        msg = getattr(interaction, "message", None)
        return await self.run(rdb.find_request_by_message, msg.id) if msg is not None else None

    async def on_accept(self, interaction: discord.Interaction, request_id: Optional[int]):
        req = await self._request_for(interaction, request_id)
        if req is None:
            return await _say(interaction, "❌ No encuentro esa solicitud.")
        if interaction.user.id != req.target_id:                       # NO basta con ocultar el botón: se valida acá
            await self.run(rdb.audit, interaction.user.id, "impersonation_attempt", req.target_id, req.id, "intentó aceptar una solicitud ajena")
            return await _say(interaction, "🔒 Esta solicitud no es para vos.")
        try:
            reg = await self.run(rdb.accept, req.id, interaction.user.id)
        except rdb.RegError as exc:
            return await _say(interaction, f"❌ {exc}")
        nick_msg = await self.apply_nickname(interaction.user, reg.char_name)
        await interaction.response.edit_message(
            content=f"🔗 {interaction.user.mention} quedó vinculado a **{discord.utils.escape_markdown(reg.char_name)}**.\n{nick_msg}",
            embeds=[], view=None, allowed_mentions=NO_MENTIONS)
        await self.audit_log(f"🔗 Registro #{req.id}: <@{req.target_id}> ↔ **{discord.utils.escape_markdown(reg.char_name)}** "
                             f"(iniciado por <@{req.initiator_id}>, aceptado por la persona)")

    async def on_decline(self, interaction: discord.Interaction, request_id: Optional[int]):
        req = await self._request_for(interaction, request_id)
        if req is None:
            return await _say(interaction, "❌ No encuentro esa solicitud.")
        if interaction.user.id != req.target_id:
            await self.run(rdb.audit, interaction.user.id, "impersonation_attempt", req.target_id, req.id, "intentó rechazar una solicitud ajena")
            return await _say(interaction, "🔒 Esta solicitud no es para vos.")
        try:
            await self.run(rdb.close_request, req.id, interaction.user.id, "rejected", "rechazada por el dueño de la cuenta")
        except rdb.RegError as exc:
            return await _say(interaction, f"❌ {exc}")
        await interaction.response.edit_message(content="✖️ Solicitud cancelada. No se guardó nada.", embeds=[], view=None)

    async def apply_nickname(self, member: discord.Member, name: str) -> str:
        """Cambia SOLO el apodo del servidor (al personaje recién registrado). Si no se puede, el registro se conserva y se explica el motivo."""
        guild = member.guild
        me = getattr(guild, "me", None)
        if getattr(guild, "owner_id", None) == member.id:
            return "ℹ️ No pude cambiarte el apodo: Discord no permite modificar al dueño del servidor. El registro quedó guardado."
        perms = getattr(me, "guild_permissions", None)
        if me is None or not (getattr(perms, "manage_nicknames", False) or getattr(perms, "administrator", False)):
            return "ℹ️ No pude cambiarte el apodo: al bot le falta el permiso **Gestionar apodos**. El registro quedó guardado."
        top, mytop = getattr(member, "top_role", None), getattr(me, "top_role", None)
        if top is not None and mytop is not None and top >= mytop:
            return "ℹ️ No pude cambiarte el apodo: tu rol está por encima (o igual) al del bot. El registro quedó guardado."
        if (member.nick or "") == name:
            return "✅ Tu apodo ya coincide con tu personaje."
        try:
            await member.edit(nick=name[:32], reason="Registro de Albion")
            return f"✅ Tu apodo ahora es **{discord.utils.escape_markdown(name)}**."
        except discord.Forbidden:
            return "ℹ️ No pude cambiarte el apodo (Discord me lo impidió). El registro quedó guardado."
        except discord.HTTPException as exc:
            print(f"⚠️ Registro: no pude cambiar el apodo de {member.id}: {exc!r}")
            return "ℹ️ No pude cambiarte el apodo por un error de Discord. El registro quedó guardado."

    # ---------- gestión ----------
    def _regs_embed(self, regs: List[rdb.Registration], title: str) -> discord.Embed:
        e = discord.Embed(title=title, color=GOLD)
        for r in regs:
            how = {"confirmed": "aceptado por la persona", "recruitment": "registrado al aceptar su postulación", "guest_invite": "registrado al ser invitado"}.get(r.method, "⚠️ vinculado por un administrador")
            e.add_field(name=discord.utils.escape_markdown(r.char_name),
                        value=f"{r.region or '—'} · guild {discord.utils.escape_markdown(r.guild_name or '—')}\n{how}\nRegistrado {r.registered_at[:10]}",
                        inline=False)
        return e

    @app_commands.command(name="mi_registro", description="Mostrá el personaje de Albion vinculado a tu cuenta")
    @app_commands.guild_only()
    async def mi_registro(self, interaction: discord.Interaction):
        regs = await self.run(rdb.get_registrations, interaction.user.id)
        if not regs:
            return await _say(interaction, "ℹ️ Todavía no estás registrado. Usá `/registrar usuario_de_albion:TuPersonaje`.")
        await interaction.response.send_message(embed=self._regs_embed(regs, "🔗 Tu registro"), ephemeral=True)

    @app_commands.command(name="desvincular", description="(Council) Quitá el personaje de Albion vinculado a tu cuenta")
    @app_commands.guild_only()
    async def desvincular(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden desvincular. Pedile al staff que lo haga con `/admin_desvincular`.")
        reg = await self.run(rdb.get_registration, interaction.user.id)
        if reg is None:
            return await _say(interaction, "ℹ️ No tenés ningún registro para quitar.")

        async def yes(i: discord.Interaction):
            removed = await self.run(rdb.unlink, interaction.user.id, interaction.user.id, reg.char_name, "desvinculado por el propio usuario")
            await i.response.edit_message(content="✅ Listo, el personaje quedó desvinculado." if removed else "ℹ️ Ya no estaba vinculado.", view=None)
            if removed:
                await self.audit_log(f"🔓 <@{interaction.user.id}> se desvinculó de **{discord.utils.escape_markdown(reg.char_name)}**.")

        await interaction.response.send_message(
            f"¿Desvincular **{discord.utils.escape_markdown(reg.char_name)}** de tu cuenta? Vas a perder el acceso a los comandos de miembros hasta volver a registrarte.",
            view=YesNoView(interaction.user.id, yes, "Sí, desvincular"), ephemeral=True)

    @app_commands.command(name="admin_registro", description="(Staff) Personajes vinculados a un miembro")
    @app_commands.describe(usuario="Miembro")
    @app_commands.guild_only()
    async def admin_registro(self, interaction: discord.Interaction, usuario: discord.Member):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        regs = await self.run(rdb.get_registrations, usuario.id)
        pending = await self.run(rdb.pending_requests_for, usuario.id)
        rows = await self.run(rdb.recent_audit, usuario.id, 6)
        e = self._regs_embed(regs, f"Registro de {usuario.display_name}") if regs else \
            discord.Embed(title=f"Registro de {usuario.display_name}", description="Sin registro.", color=GREY)
        for req in pending:
            e.add_field(name="Solicitud en curso", value=f"#{req.id} · {req.char_name} · vence {req.expires_at}", inline=False)
        lines = [f"`{r['at'][:16]}` {r['action']}" + (f" — {r['detail']}" if r["detail"] else "") for r in rows]
        if lines:
            e.add_field(name="Últimos movimientos", value="\n".join(lines)[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="admin_desvincular", description="(Staff) Quita el personaje vinculado a un miembro")
    @app_commands.describe(usuario="Miembro", motivo="Motivo (queda en la auditoría)")
    @app_commands.guild_only()
    async def admin_desvincular(self, interaction: discord.Interaction, usuario: discord.Member, motivo: Optional[str] = None):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        regs = await self.run(rdb.get_registrations, usuario.id)
        if not regs:
            n = await self.run(rdb.cancel_active_for, usuario.id, interaction.user.id, "cancelada por un administrador")
            return await _say(interaction, "ℹ️ Esa persona no tiene registro." + (f" Se canceló {n} solicitud(es) en curso." if n else ""))
        name = None
        label = "**" + discord.utils.escape_markdown(regs[0].char_name) + "**"

        async def yes(i: discord.Interaction):
            removed = await self.run(rdb.unlink, usuario.id, interaction.user.id, name, f"por administrador. Motivo: {(motivo or 'sin motivo')[:200]}")
            await i.response.edit_message(content=f"✅ {usuario.mention}: se desvinculó su personaje." if removed else "ℹ️ Ya no tenía registro.",
                                          view=None, allowed_mentions=NO_MENTIONS)
            if removed:
                await self.audit_log(f"🔓 <@{interaction.user.id}> desvinculó a <@{usuario.id}> de " + ", ".join(f"**{discord.utils.escape_markdown(r.char_name)}**" for r in removed)
                                     + f" — {(motivo or 'sin motivo')[:200]}")

        await interaction.response.send_message(f"¿Desvincular a {usuario.mention} de {label}?", view=YesNoView(interaction.user.id, yes, "Sí, desvincular"),
                                                ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="admin_vincular", description="(Staff, excepcional) Vincula a mano un personaje sin que la persona lo acepte")
    @app_commands.describe(usuario="Miembro", personaje="Nombre exacto del personaje en Albion")
    @app_commands.guild_only()
    async def admin_vincular(self, interaction: discord.Interaction, usuario: discord.Member, personaje: str):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        if usuario.bot:
            return await _say(interaction, "❌ No se puede vincular a un bot.")
        await interaction.response.defer(ephemeral=True)
        look = await self.lookup_character(personaje, live=True)
        char = look.character if look.kind in ("ok", "wrong_guild") else None
        note = ""
        if look.kind == "ambiguous":
            return await _say(interaction, "❌ Hay varios personajes con ese nombre en distintos servidores de Albion.")
        if look.kind == "error":
            note = "\n⚠️ La API de Albion no respondió: se guardó solo el nombre, sin ID."
        elif look.kind == "none":
            note = "\n⚠️ No encontré ese personaje en la API: se guardó solo el nombre, sin ID."
        elif look.kind == "wrong_guild":
            note = f"\n⚠️ El personaje no figura en la guild **{look.detail}**."
        try:
            reg = await self.run(rdb.admin_link, usuario.id, char.name if char else _clean_name(personaje), interaction.user.id,
                                 char_id=char.char_id if char else None, region=char.region if char else None, guild_name=char.guild if char else None)
        except rdb.RegError as exc:
            return await _say(interaction, f"❌ {exc}")
        nick_msg = await self.apply_nickname(usuario, reg.char_name)
        role_msg = ""
        member_role = interaction.guild.get_role(self.member_role_id) if self.member_role_id else None
        if member_role is None:
            role_msg = "\n⚠️ No encuentro el rol Miembro: no se le pudo dar."
        elif member_role not in usuario.roles:
            try:
                await usuario.add_roles(member_role, reason=f"/admin_vincular por {interaction.user} ({interaction.user.id})")
                role_msg = f"\n✅ No tenía el rol {member_role.mention}: se lo di."
            except discord.HTTPException as exc:
                role_msg = f"\n⚠️ No pude darle el rol {member_role.mention} (revisá que mi rol esté por encima): {exc}"
        await interaction.followup.send(
            f"⚠️ {usuario.mention} quedó vinculado a **{discord.utils.escape_markdown(reg.char_name)}** como **vínculo administrativo** "
            f"(la persona no lo aceptó).{note}\n{nick_msg}{role_msg}", ephemeral=True, allowed_mentions=NO_MENTIONS)
        await self.audit_log(f"⚠️ <@{interaction.user.id}> vinculó **a mano** a <@{usuario.id}> con **{discord.utils.escape_markdown(reg.char_name)}**")

    # ---------- reclutamiento: registro al aceptar y roles recién cuando entra a la guild ----------
    async def accept_recruit(self, member: discord.Member, nick: str, role_id: int, role_name: str, recruiter, extra_role_ids=()) -> tuple:
        """Registra al postulante con su nick de Albion. Devuelve (ok, texto, roles_dados_ya).
        Si el personaje ya figura en la guild, se dan los roles en el momento; si no, quedan PENDIENTES hasta que aparezca en la lista."""
        look = await self.lookup_character(nick, any_guild=True)
        # Si la API no responde, no encuentra el personaje o no puede saber cuál de varios homónimos es, no se frena la aceptación: se vincula
        # a mano con el nick del ticket, igual que /admin_vincular (queda marcado como vínculo administrativo y auditado).
        # /sincronizar-registros completa el ID después.
        manual = {"error": "la API de Albion no respondió", "none": "no encontré ese personaje en la API de Albion",
                  "ambiguous": "hay varios personajes con ese nombre en distintos servidores de Albion"}.get(look.kind)
        c = None if manual else look.character
        name = c.name if c else _clean_name(nick)
        # Si ya estaba registrado (p. ej. entró como invitado con /invitar o usó /registrar), se usa ese registro en vez de fallar.
        existing = await self.run(rdb.get_registration, member.id)
        if existing is not None:
            if c is None or c.name.casefold() != existing.char_name.casefold():
                c = None
            manual = None
            name = existing.char_name
        else:
            try:
                await self.run(rdb.admin_link, member.id, name, recruiter.id, char_id=c.char_id if c else None, region=c.region if c else None,
                               guild_name=(c.guild or None) if c else None, method="recruitment" if c else "admin_override")
            except rdb.RegError as exc:
                return False, f"{exc}", False
        in_guild = ((bool(self.required_guild) and bool(c.guild) and c.guild.casefold() == self.required_guild.casefold()) if c else False) or name.casefold() in self.roster
        safe = discord.utils.escape_markdown(name)
        if existing is not None:
            await self.audit_log(f"🧾 <@{recruiter.id}> aceptó a <@{member.id}> (ya estaba registrado como **{safe}**).")
        elif manual:
            await self.audit_log(f"⚠️ <@{recruiter.id}> aceptó a <@{member.id}> y lo vinculó **a mano** como **{safe}** ({manual}).")
        else:
            await self.audit_log(f"🧾 <@{recruiter.id}> aceptó y registró a <@{member.id}> como **{safe}**.")
        warn = f"\n⚠️ {manual[0].upper() + manual[1:]}: quedó vinculado **a mano** (como `/admin_vincular`) con el nick del ticket. Revisá que esté bien escrito." if manual else ""
        if in_guild:
            if await self.grant_recruit_roles(member, role_id, role_name, name, recruiter.id, extra_role_ids):
                return True, f"Registrado como **{safe}** y ya está en la guild: roles entregados.{warn}", True
            return False, (f"quedó registrado como **{safe}** y está en la guild, pero **no pude darle los roles** "
                           "(revisá que el rol del bot esté por encima de Miembro y de los roles de CTA)."), False
        pend_id = existing.char_id if existing is not None else (c.char_id if c else None)
        await self.run(rdb.add_pending_recruit, member.id, name, pend_id, role_id, role_name, recruiter.id, tuple(extra_role_ids))
        try:
            await member.send(f"✅ Fuiste aceptado en **{self.required_guild or 'la guild'}** como **{role_name}**. Entrá a la guild en el juego con tu personaje "
                              f"**{name}**: apenas el bot te vea adentro, te da los roles automáticamente (puede tardar hasta unos 10 minutos).")
        except Exception:
            pass
        return True, (f"Registrado como **{safe}**. ⏳ **Todavía no está en la guild**: los roles se le dan solos cuando "
                      f"aparezca adentro (se revisa cada 10 min).{warn}"), False

    async def grant_recruit_roles(self, member: discord.Member, role_id: int, role_name: str, char_name: str, recruiter_id=None, extra_role_ids=()) -> bool:
        guild = member.guild
        ids = [self.member_role_id, role_id] + [int(x) for x in extra_role_ids]
        roles = []
        for rid in dict.fromkeys(ids):
            r = guild.get_role(rid) if rid else None
            if r is not None:
                roles.append(r)
        if not roles:
            print("⚠️ Reclutamiento: no encuentro los roles a asignar")
            return False
        try:
            await member.add_roles(*roles, reason=f"Reclutamiento: ingresó a la guild como {role_name}")
            public = guild.get_role(self.public_role_id) if self.public_role_id else None
            if public is not None and public in member.roles:
                await member.remove_roles(public, reason="Reclutamiento: se quita rol Public")
        except discord.Forbidden:
            await self.audit_log(f"⚠️ No pude darle los roles a <@{member.id}>: revisá que mi rol esté por encima de los roles a asignar.")
            return False
        except Exception:
            traceback.print_exc()
            return False
        nick_msg = await self.apply_nickname(member, char_name)
        await self.audit_log(f"✅ <@{member.id}> ya está en la guild como **{discord.utils.escape_markdown(char_name)}**: roles entregados ({role_name}). {nick_msg}")
        return True

    async def check_pending_recruits(self):
        pending = await self.run(rdb.list_pending_recruits)
        if not pending:
            return
        if self.albion_guild_id:
            await self.refresh_roster()
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return
        from datetime import datetime, timedelta, timezone
        for p in pending:
            member = guild.get_member(p.discord_id)
            if member is None:
                await self.run(rdb.remove_pending_recruit, p.discord_id, None, "la persona ya no está en el servidor")
                continue
            if p.char_name.casefold() in self.roster:
                if await self.grant_recruit_roles(member, p.role_id, p.role_name, p.char_name, p.recruiter_id, p.extra_role_ids):
                    await self.run(rdb.remove_pending_recruit, p.discord_id, None, "ingresó a la guild: roles entregados")
                    try:
                        await member.send(f"🎉 Ya te vimos dentro de la guild: te di los roles de **{p.role_name}**. ¡Bienvenido!")
                    except Exception:
                        pass
                continue
            try:
                age = datetime.now(timezone.utc) - datetime.strptime(p.created_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if age > timedelta(days=RECRUIT_PENDING_DAYS):
                await self.run(rdb.remove_pending_recruit, p.discord_id, None, f"venció a los {RECRUIT_PENDING_DAYS} días sin ingresar")
                await self.audit_log(f"⌛ <@{p.discord_id}> ({discord.utils.escape_markdown(p.char_name)}) no entró a la guild en {RECRUIT_PENDING_DAYS} días: "
                                     "se canceló la espera (sigue registrado, sin roles).")

    @tasks.loop(minutes=10)
    async def recruit_loop(self):
        try:
            await self.check_pending_recruits()
        except Exception:
            traceback.print_exc()

    @recruit_loop.before_loop
    async def _recruit_ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="reclutas-pendientes", description="(Staff) Aceptados que todavía no entraron a la guild (sin roles)")
    @app_commands.guild_only()
    async def reclutas_pendientes(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        pending = await self.run(rdb.list_pending_recruits)
        if not pending:
            return await _say(interaction, "✅ No hay aceptados esperando ingresar a la guild.")
        lines = [f"• <@{p.discord_id}> → **{discord.utils.escape_markdown(p.char_name)}** ({p.role_name}) · aceptado {p.created_at[:10]}" for p in pending]
        await interaction.response.send_message("⏳ **Aceptados sin entrar a la guild todavía:**\n" + "\n".join(lines)[:1800], ephemeral=True,
                                                allowed_mentions=NO_MENTIONS)

    @staticmethod
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

    async def _registration_gaps(self, interaction: discord.Interaction):
        """(cuentas vinculadas, plantel de la guild, sin Discord, miembros de Discord, sin personaje) o None si no se pudo calcular."""
        if not self.albion_guild_id:
            await _say(interaction, "❌ No tengo configurada la guild de Albion (ALBION_GUILD_ID), así que no puedo comparar la lista.")
            return None
        if not self.roster_fresh():
            await self.refresh_roster()
        if not self.roster:
            await _say(interaction, "❌ No pude bajar la lista de miembros de la guild desde la API de Albion. Probá de nuevo en unos minutos.")
            return None
        links = await self.run(rdb.all_registrations)
        linked_names = {r.char_name.casefold() for r in links}
        linked_ids = {r.char_id for r in links if r.char_id}
        linked_discord = {r.discord_id for r in links}
        guild = interaction.guild
        role = guild.get_role(self.member_role_id) if self.member_role_id else None
        discord_pool = [m for m in (role.members if role is not None else guild.members) if not getattr(m, "bot", False)]
        guild_unlinked = sorted((c.name for c in self.roster.values() if c.name.casefold() not in linked_names and c.char_id not in linked_ids),
                                key=str.casefold)
        discord_unlinked = sorted((m for m in discord_pool if m.id not in linked_discord), key=lambda m: m.display_name.casefold())
        return links, guild_unlinked, discord_pool, discord_unlinked, role

    @app_commands.command(name="sin-albion", description="(Staff) Miembros de Discord que todavía no registraron su personaje de Albion")
    @app_commands.describe(mencionar="true: etiqueta a todas las personas sin registrar en un mensaje público (por defecto false)")
    @app_commands.guild_only()
    async def sin_albion(self, interaction: discord.Interaction, mencionar: Optional[bool] = False):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=not mencionar)
        res = await self._registration_gaps(interaction)
        if res is None:
            return
        links, _guild_unlinked, discord_pool, discord_unlinked, role = res
        who = f"con el rol **{role.name}**" if role is not None else "del servidor"
        head = f"💬 **{len(discord_unlinked)}** de {len(discord_pool)} miembros {who} no tienen personaje de Albion registrado ({len(links)} cuentas vinculadas)."
        if not discord_unlinked:
            return await interaction.followup.send("✅ Todos los miembros están registrados.", ephemeral=not mencionar, allowed_mentions=NO_MENTIONS)
        if mencionar:
            note = "Registrate con `/registrar usuario_de_albion:TuNick` para poder usar los comandos del bot."
            chunks = self._chunks([f"<@{m.id}>" for m in discord_unlinked], limit=1500)
            for n, chunk in enumerate(chunks):
                text = (head + "\n" + note + "\n" if n == 0 else "") + chunk
                await interaction.followup.send(text, ephemeral=False,
                                                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=m.id) for m in discord_unlinked], roles=False, everyone=False))
            return
        lines = [f"• <@{m.id}> ({discord.utils.escape_markdown(m.display_name)})" for m in discord_unlinked]
        for n, chunk in enumerate(self._chunks(lines)):
            await interaction.followup.send((head + "\n" if n == 0 else "") + chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------- /limpiar-miembros (Admin): saca el rol Miembro a quien no tiene cuenta enlazada ----------
    def _premium_ids(self) -> set:
        """Invitados premium activos (tabla de invitados.py); vacío si la tabla no existe."""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                return {int(r[0]) for r in conn.execute("SELECT user_id FROM guests WHERE active=1 AND kind='premium'")}
            finally:
                conn.close()
        except sqlite3.Error:
            return set()

    async def _unlinked_members(self, guild) -> list:
        """Con rol Miembro y sin personaje enlazado. Nunca toca bots, administradores, Staff, Council, invitados premium ni aceptados en espera."""
        role = guild.get_role(self.member_role_id) if self.member_role_id else None
        if role is None:
            return []
        linked = {r.discord_id for r in await self.run(rdb.all_registrations)}
        pending = {p.discord_id for p in await self.run(rdb.list_pending_recruits)}
        premium = await asyncio.to_thread(self._premium_ids)
        out = []
        for m in role.members:
            perms = getattr(m, "guild_permissions", None)
            if getattr(m, "bot", False) or (perms is not None and perms.administrator) or can_admin_registro(m):
                continue
            if m.id in linked or m.id in pending or m.id in premium:
                continue
            out.append(m)
        return sorted(out, key=lambda m: m.display_name.casefold())

    @app_commands.command(name="limpiar-miembros", description="(Admin) Saca el rol Miembro a quienes no tienen cuenta de Albion enlazada")
    @app_commands.describe(dar_public="Darles el rol Public al sacarles Miembro (por defecto sí)")
    @app_commands.guild_only()
    async def limpiar_miembros(self, interaction: discord.Interaction, dar_public: Optional[bool] = True):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms is not None and perms.administrator):
            return await _say(interaction, "🔒 Solo los administradores pueden usar este comando.")
        guild = interaction.guild
        role = guild.get_role(self.member_role_id) if self.member_role_id else None
        if role is None:
            return await _say(interaction, "❌ No encuentro el rol Miembro en el servidor.")
        await interaction.response.defer(ephemeral=True)
        targets = await self._unlinked_members(guild)
        if not targets:
            return await interaction.followup.send("✅ Todos los que tienen el rol Miembro tienen cuenta enlazada.", ephemeral=True)
        names = ", ".join(f"<@{m.id}>" for m in targets)
        if len(names) > 1500:
            names = names[:1500].rsplit(",", 1)[0] + f", … (+{len(targets)} en total)"
        public_txt = " y se les da **Public**" if dar_public else ""
        actor = interaction.user

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content=f"⏳ Sacando el rol Miembro a {len(targets)} personas…", view=None)
            public = guild.get_role(self.public_role_id) if (dar_public and self.public_role_id) else None
            linked = {r.discord_id for r in await self.run(rdb.all_registrations)}   # se vuelve a revisar por si alguien se registró recién
            done, skipped, failed = [], 0, []
            for m in targets:
                if m.id in linked or role not in getattr(m, "roles", []):
                    skipped += 1
                    continue
                try:
                    await m.remove_roles(role, reason=f"/limpiar-miembros por {actor} ({actor.id}): sin cuenta de Albion enlazada")
                    if public is not None and public not in m.roles:
                        await m.add_roles(public, reason="/limpiar-miembros: vuelve a Public")
                    done.append(m)
                except discord.HTTPException:
                    failed.append(m)
                await asyncio.sleep(0.3)
            await self.run(rdb.audit, actor.id, "bulk_remove_member", None, None, f"sacados={len(done)} fallidos={len(failed)} omitidos={skipped}")
            msg = f"✅ Se les sacó el rol **{role.name}** a **{len(done)}** personas{public_txt if done else ''}."
            if skipped:
                msg += f"\nℹ️ {skipped} se omitieron (se registraron o ya no tenían el rol)."
            if failed:
                msg += f"\n⚠️ No pude cambiarle los roles a {len(failed)} (revisá que mi rol esté por encima): " + ", ".join(f"<@{m.id}>" for m in failed)[:800]
            await i.edit_original_response(content=msg)
            await self.audit_log(f"🧹 <@{actor.id}> usó `/limpiar-miembros`: se sacó el rol Miembro a **{len(done)}** personas sin cuenta enlazada"
                                 + (f" ({len(failed)} fallaron)" if failed else "") + ".")

        await interaction.followup.send(
            f"🧹 **{len(targets)}** personas tienen el rol **{role.name}** y no tienen cuenta de Albion enlazada. Si confirmás, se les saca el rol{public_txt}.\n"
            f"No se tocan bots, administradores, Staff, Council, invitados premium ni aceptados esperando entrar a la guild. "
            f"El balance y las estadísticas **no** se pierden.\n\n{names}",
            view=YesNoView(actor.id, yes, f"Sacar Miembro a {len(targets)}"), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="sincronizar-apodos", description="(Staff) Revisa que cada apodo sea el personaje de Albion y corrige los que no")
    @app_commands.describe(solo_ver="true: solo muestra qué cambiaría, sin tocar ningún apodo (por defecto false)")
    @app_commands.guild_only()
    async def sincronizar_apodos(self, interaction: discord.Interaction, solo_ver: Optional[bool] = False):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        first: Dict[int, rdb.Registration] = {}
        for r in await self.run(rdb.all_registrations):
            first.setdefault(r.discord_id, r)             # si tiene varios personajes, manda el primero que registró
        ok, changed, failed, absent = 0, [], [], 0
        for did, reg in first.items():
            member = guild.get_member(did)
            if member is None or member.bot:
                absent += 1
                continue
            want = reg.char_name[:32]
            if (member.nick or "") == want:
                ok += 1
                continue
            old = member.nick or member.name
            if solo_ver:
                changed.append(f"• <@{did}>: {discord.utils.escape_markdown(old)} → **{discord.utils.escape_markdown(want)}**")
                continue
            msg = await self.apply_nickname(member, reg.char_name)
            if msg.startswith("✅"):
                changed.append(f"• <@{did}>: {discord.utils.escape_markdown(old)} → **{discord.utils.escape_markdown(want)}**")
            else:
                failed.append(f"• <@{did}> ({discord.utils.escape_markdown(want)}): {msg.replace('ℹ️ No pude cambiarte el apodo', 'no pude').split(' El registro')[0]}")
            await asyncio.sleep(0.6)                      # no pasarnos del límite de Discord al editar apodos
        verb = "Cambiaría" if solo_ver else "Corregidos"
        head = (f"🏷️ **Apodos vs. personaje de Albion** · {verb}: **{len(changed)}** · ya estaban bien: **{ok}**"
                + (f" · no pude: **{len(failed)}**" if failed else "") + (f" · fuera del servidor: {absent}" if absent else ""))
        lines = [head]
        if changed:
            lines += ["", f"**{verb}:**"] + changed
        if failed:
            lines += ["", "**No se pudieron cambiar:**"] + failed
        for chunk in self._chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="recuperar-lista", description="(Staff) Los últimos 5 miembros registrados que se fueron del servidor")
    @app_commands.guild_only()
    async def recuperar_lista(self, interaction: discord.Interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        saved = await self.run(rdb.list_left_members)
        if not saved:
            return await _say(interaction, "ℹ️ No hay datos guardados: todavía no se fue ningún miembro registrado.")
        blocks = []
        for m in saved:
            chars = ", ".join(f"**{discord.utils.escape_markdown(r['char_name'])}**" for r in m["regs"])
            back = "✅ ya volvió (usá `/recuperar`)" if interaction.guild.get_member(m["discord_id"]) else "⏳ todavía no volvió"
            blocks.append(f"• <@{m['discord_id']}> (`{m['discord_id']}`) · {chars}\n"
                          f"   se fue {m['left_at'][:16].replace('T', ' ')} UTC · {len(m['roles'])} rol(es)"
                          + (f" · apodo {discord.utils.escape_markdown(m['nick'])}" if m["nick"] else "") + f" · {back}")
        await _say(interaction, f"📤 **Últimos {len(saved)} registrados que se fueron** (se guardan 5; el siguiente pisa al más viejo):\n"
                   + "\n".join(blocks) + "\nCuando vuelvan al servidor: `/recuperar @persona`.")

    @app_commands.command(name="recuperar", description="(Staff) Devuelve registro, roles y apodo a alguien que se fue y volvió al servidor")
    @app_commands.describe(persona="La persona que volvió (tiene que estar entre los 5 de /recuperar-lista)")
    @app_commands.guild_only()
    async def recuperar(self, interaction: discord.Interaction, persona: discord.Member):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        uid = persona.id
        saved = await self.run(rdb.list_left_members)
        m = next((x for x in saved if x["discord_id"] == uid), None)
        if m is None:
            return await _say(interaction, "❌ Esa persona no está entre los 5 guardados. Mirá la lista con `/recuperar-lista`.")
        member = interaction.guild.get_member(uid)
        if member is None:
            return await _say(interaction, "❌ Esa persona todavía no volvió al servidor: se puede recuperar recién cuando vuelva a entrar.")
        await interaction.response.defer(ephemeral=True)
        results = []
        for r in m["regs"]:
            try:
                await self.run(rdb.admin_link, uid, r["char_name"], interaction.user.id, char_id=r.get("char_id"), region=r.get("region"),
                               guild_name=r.get("guild_name"), method=r.get("method") or "admin_override")
                results.append(f"✅ Registro **{discord.utils.escape_markdown(r['char_name'])}** devuelto.")
            except rdb.RegError as exc:
                results.append(f"⚠️ **{discord.utils.escape_markdown(r['char_name'])}**: {exc}")
        guild = interaction.guild
        roles = [guild.get_role(i) for i, _n in m["roles"]]
        roles = [r for r in roles if r is not None and not getattr(r, "managed", False)]
        missing = [n for i, n in m["roles"] if guild.get_role(i) is None]
        try:
            if roles:
                await member.add_roles(*roles, reason=f"Recuperado por {interaction.user}")
            results.append(f"✅ {len(roles)} rol(es) devuelto(s)." + (f" No existen más: {', '.join(missing)}." if missing else ""))
        except discord.Forbidden:
            results.append("⚠️ No pude devolver los roles: mi rol tiene que estar por encima de ellos.")
        except Exception as exc:
            results.append(f"⚠️ No pude devolver los roles ({exc.__class__.__name__}).")
        if m["regs"]:
            results.append(await self.apply_nickname(member, m["regs"][0]["char_name"]))
        await self.run(rdb.delete_left_member, uid)
        await self.audit_log(f"♻️ <@{interaction.user.id}> recuperó a <@{uid}> con `/recuperar`.")
        await interaction.followup.send("\n".join(results), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="estado-api", description="(Admin) Revisa si la API de Albion está respondiendo (la que usa el registro)")
    @app_commands.guild_only()
    async def estado_api(self, interaction: discord.Interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        is_admin = bool(perms and perms.administrator) or getattr(interaction.guild, "owner_id", None) == interaction.user.id
        if not is_admin:
            return await _say(interaction, "🔒 Solo los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        import time

        async def probe(url, params=None):
            t0 = time.monotonic()
            try:
                data = await asyncio.wait_for(self.fetch_json(url, params), HTTP_TIMEOUT + 5)
                return True, time.monotonic() - t0, data, ""
            except asyncio.TimeoutError:
                return False, time.monotonic() - t0, None, f"sin respuesta en {HTTP_TIMEOUT + 5} s"
            except Exception as exc:
                return False, time.monotonic() - t0, None, f"{type(exc).__name__} {exc}".strip()[:120]

        lines, any_ok, guild_list_ok = [], False, False
        for region, base in GAMEINFO_BASES:
            ok, secs, data, err = await probe(f"{base}/api/gameinfo/search", {"q": "a"})
            any_ok = any_ok or ok
            lines.append(f"{'🟢' if ok else '🔴'} **{region}** · búsqueda de jugadores: " + (f"OK ({secs:.1f} s)" if ok else f"FALLA ({err})"))
            if self.albion_guild_id:
                ok2, secs2, data2, err2 = await probe(f"{base}/api/gameinfo/guilds/{self.albion_guild_id}/members")
                n = len(data2) if isinstance(data2, list) else 0
                guild_list_ok = guild_list_ok or (ok2 and n > 0)
                lines.append(f"   {'🟢' if ok2 and n else ('🟡' if ok2 else '🔴')} lista de la guild: "
                             + (f"{n} miembros ({secs2:.1f} s)" if ok2 else f"FALLA ({err2})"))
        age = time.time() - self.roster_at if self.roster_at else None
        roster = (f"{len(self.roster)} jugadores, actualizada hace {int(age // 60)} min" + (" ✅" if self.roster_fresh() else " ⚠️ vieja (más de 24 h; se sigue usando si la API falla)")
                  if self.roster else "vacía ⚠️")
        pending = await self.run(rdb.list_pending_recruits)
        incomplete = await self.run(rdb.incomplete_registrations)
        guild_ok = bool(self.albion_guild_id) and guild_list_ok
        if any_ok and self.albion_guild_id and not guild_ok:
            verdict = ("⚠️ La búsqueda responde, pero **la lista de la guild no se puede bajar** (suele ser el servidor de Américas con errores 502 de Albion). "
                       + ("El registro sigue funcionando con la lista guardada" + (f" ({len(self.roster)} jugadores)" if self.roster else "")
                          + ": cualquiera que figure ahí se registra igual. Quien entró hace poco a la guild no figura hasta que vuelva la API: usá `/admin_vincular`."
                          if self.roster else "Como no hay lista guardada, usá `/admin_vincular` y después `/sincronizar-registros` cuando vuelva."))
        elif any_ok:
            verdict = ("✅ La API responde: si el registro falla, el problema es otro (nick mal escrito, personaje fuera de la guild, etc.).")
        else:
            verdict = ("❌ La API de Albion no está respondiendo en ninguna región: por eso no se registra a nadie. Es un problema de ellos; "
                        "el bot reintenta solo. Mientras tanto usá `/admin_vincular` y después `/sincronizar-registros` cuando vuelva.")
        await interaction.followup.send(
            "🛰️ **Estado de la API de Albion**\n" + "\n".join(lines)
            + f"\n\n📋 Lista de la guild guardada en el bot: {roster}\n"
              f"⏳ Reclutas esperando entrar a la guild: {len(pending)} · Registros sin ID: {len(incomplete)}\n\n{verdict}",
            ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="sincronizar-registros", description="(Staff) Vuelve a buscar en Albion los registros sin ID (vínculos a mano o con la API caída) y los completa")
    @app_commands.guild_only()
    async def sincronizar_registros(self, interaction: discord.Interaction):
        if not can_admin_registro(interaction.user):
            return await _say(interaction, "🔒 Solo el Staff, el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        pending = await self.run(rdb.incomplete_registrations)
        if not pending:
            return await interaction.followup.send("✅ Todos los registros ya tienen su ID de Albion.", ephemeral=True)
        done, failed, errors_in_a_row = [], [], 0
        for n, reg in enumerate(pending):
            who = f"<@{reg.discord_id}> (**{discord.utils.escape_markdown(reg.char_name)}**)"
            if errors_in_a_row >= 3:                        # la API sigue caída: no seguir golpeándola
                failed.append(f"• {who}: no se consultó (la API de Albion sigue sin responder)")
                continue
            look = await self.lookup_character(reg.char_name, live=True)
            if look.kind == "error":
                errors_in_a_row += 1
                failed.append(f"• {who}: la API de Albion no respondió")
            elif look.kind in ("ok", "wrong_guild") and look.character is not None:
                errors_in_a_row = 0
                c = look.character
                try:
                    await self.run(rdb.fill_registration, reg.id, c.char_id, c.region, c.guild or None, interaction.user.id)
                    member = interaction.guild.get_member(reg.discord_id)
                    if member is not None:                      # de paso deja el apodo igual al personaje
                        try:
                            await self.apply_nickname(member, reg.char_name)
                        except Exception:
                            traceback.print_exc()
                    done.append(f"• {who}: {c.region}" + (f" · {discord.utils.escape_markdown(c.guild)}" if c.guild else "")
                                + (" ⚠️ no está en la guild" if look.kind == "wrong_guild" else ""))
                except rdb.RegError as exc:
                    failed.append(f"• {who}: {exc}")
            elif look.kind == "ambiguous":
                errors_in_a_row = 0
                failed.append(f"• {who}: hay varios personajes con ese nombre en distintos servidores ({', '.join(sorted({o.region for o in look.options}))})")
            else:
                errors_in_a_row = 0
                failed.append(f"• {who}: no existe un personaje con ese nombre (¿mal escrito o renombrado?)")
            await asyncio.sleep(0.4)
        head = f"🔎 **Registros sin ID:** {len(pending)} · completados: **{len(done)}** · sin verificar: **{len(failed)}**"
        lines = [head]
        if done:
            lines += ["", "**✅ Completados:**"] + done
        if failed:
            lines += ["", "**❌ No pude verificar:**"] + failed
        for chunk in self._chunks(lines):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if done:
            await self.audit_log(f"🔎 <@{interaction.user.id}> completó el ID de **{len(done)}** registro(s) con `/sincronizar-registros`.")

    @app_commands.command(name="guild-sin-discord", description="(Staff) Con el archivo de la guild de Albion: quiénes están en la guild y no en Discord (mensaje público)")
    @app_commands.describe(archivo="Archivo exportado de la lista de la guild (Character Name / Last Seen / Roles)")
    @app_commands.guild_only()
    async def guild_sin_discord(self, interaction: discord.Interaction, archivo: discord.Attachment):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        if (getattr(archivo, "size", 0) or 0) > MAX_EXPORT_BYTES:
            return await _say(interaction, "❌ El archivo es demasiado grande (máx. 2 MB).")
        await interaction.response.defer(ephemeral=False)
        try:
            names = parse_guild_export(await archivo.read())
        except Exception:
            traceback.print_exc()
            return await interaction.followup.send("❌ No pude leer el archivo. Tiene que ser el export de la guild (Character Name / Last Seen / Roles).",
                                                   ephemeral=True)
        if not names:
            return await interaction.followup.send("❌ No encontré nombres de personaje en el archivo.", ephemeral=True)
        guild = interaction.guild
        role = guild.get_role(self.member_role_id) if self.member_role_id else None
        regs = await self.run(rdb.all_registrations)
        by_name = {r.char_name.casefold(): r.discord_id for r in regs}
        alters = set()
        try:
            import alters_db
            conn = alters_db.connect(self.db_path)
            try:
                alters = {r[0].casefold() for r in conn.execute("SELECT alter_name FROM alters")}
            finally:
                conn.close()
        except Exception:
            pass
        sin_discord, sin_rol, n_alters = [], [], 0
        for n in names:
            uid = by_name.get(n.casefold())
            if uid is None:
                if n.casefold() in alters:
                    n_alters += 1
                else:
                    sin_discord.append(n)
                continue
            m = guild.get_member(uid)
            if m is None or (role is not None and role not in m.roles):
                sin_rol.append((n, uid, m is None))
        esc = discord.utils.escape_markdown
        lines = [f"🎮 **Guild vs Discord** — {len(names)} personajes en el archivo.", "",
                 f"**❌ En la guild pero sin Discord registrado ({len(sin_discord)})**"]
        lines += [f"• {esc(n)}" for n in sorted(sin_discord, key=str.casefold)] or ["✅ Ninguno."]
        if sin_rol:
            lines += ["", f"**⚠️ Registrados pero sin el rol {role.name if role else 'Miembro'} ({len(sin_rol)})**"]
            lines += [f"• {esc(n)} → <@{uid}>" + (" (ya no está en el servidor)" if gone else "") for n, uid, gone in sorted(sin_rol, key=lambda x: x[0].casefold())]
        if n_alters:
            lines += ["", f"ℹ️ {n_alters} son alters enlazados con `/alter` y no se listan."]
        for chunk in self._chunks(lines):
            await interaction.followup.send(chunk, ephemeral=False, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="sin-discord", description="(Staff) Jugadores de la guild de Albion que todavía no tienen Discord vinculado")
    @app_commands.guild_only()
    async def sin_discord(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        await interaction.response.defer(ephemeral=True)
        res = await self._registration_gaps(interaction)
        if res is None:
            return
        _links, guild_unlinked, *_ = res
        head = f"🎮 **{len(guild_unlinked)}** de {len(self.roster)} jugadores de la guild {self.required_guild or ''} no tienen Discord vinculado.".replace("  ", " ")
        if not guild_unlinked:
            return await interaction.followup.send("✅ Todos los jugadores de la guild tienen Discord vinculado.", ephemeral=True)
        lines = [f"• {discord.utils.escape_markdown(n)}" for n in guild_unlinked]
        for n, chunk in enumerate(self._chunks(lines)):
            await interaction.followup.send((head + "\n" if n == 0 else "") + chunk, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def registro_canal(self, interaction: discord.Interaction, canal: discord.TextChannel):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        await self.run(rdb.set_flag, rdb.REVIEW_CHANNEL_KEY, str(canal.id))
        await self.run(rdb.audit, interaction.user.id, "log_channel_set", None, None, f"canal={canal.id}")
        await interaction.response.send_message(f"✅ Los avisos de registro irán a {canal.mention}.", ephemeral=True)

    @app_commands.command(name="registro_obligatorio", description="(Admin) Exigir registro para usar los comandos de miembros")
    @app_commands.describe(accion="activar o desactivar")
    @app_commands.choices(accion=[app_commands.Choice(name="activar", value="activar"), app_commands.Choice(name="desactivar", value="desactivar")])
    @app_commands.guild_only()
    async def registro_obligatorio(self, interaction: discord.Interaction, accion: app_commands.Choice[str]):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council o los administradores pueden usar este comando.")
        on = accion.value == "activar"
        await self.run(rdb.set_flag, rdb.REQUIRED_KEY, "1" if on else "0")
        await self.run(rdb.audit, interaction.user.id, "required_on" if on else "required_off", None, None)
        gate = getattr(self.bot, "registration_gate", None)
        if gate is not None:
            gate.invalidate()
        await interaction.response.send_message(
            "🔐 Ahora hace falta estar **registrado** (`/registrar`) para usar los comandos de miembros. Los administradores quedan exentos."
            if on else "🔓 Ya no se exige registro para usar los comandos.", ephemeral=False)

    # ---------- limpieza ----------
    @tasks.loop(minutes=5)
    async def sweeper(self):
        try:
            for req in await self.run(rdb.expire_old):
                if req is None or not req.prompt_msg_id or not req.channel_id:
                    continue
                try:
                    ch = await self._get_channel(req.channel_id)
                    m = await ch.fetch_message(req.prompt_msg_id) if ch is not None else None
                    if m is not None:
                        await m.edit(content="⌛ La solicitud de registro venció. Podés empezar de nuevo con `/registrar`.", embeds=[], view=None)
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Si alguien se va del servidor: se cancelan sus solicitudes y se liberan sus personajes."""
        if getattr(member, "bot", False):
            return
        try:
            regs = await self.run(rdb.get_registrations, member.id)          # antes de liberarlos, para el registro de salidas
        except Exception:
            regs = []
        try:                                                                  # se guardan los datos de los últimos 5 registrados que se van
            roles = [(r.id, r.name) for r in getattr(member, "roles", []) if r.id != member.guild.id and not getattr(r, "managed", False)]
            await self.run(rdb.save_left_member, member.id, name=str(member), nick=getattr(member, "nick", None), roles=roles, regs=regs)
        except Exception:
            traceback.print_exc()
        try:
            log_cog = self.bot.get_cog("MemberLogCog")
            if log_cog is not None:
                await log_cog.post_leave(member, regs)
        except Exception:
            traceback.print_exc()
        try:
            await self.run(rdb.cancel_active_for, member.id, None, "el miembro salió del servidor")
            await self.run(rdb.remove_pending_recruit, member.id, None, "el miembro salió del servidor")
            removed = await self.run(rdb.unlink, member.id, None, None, "el miembro salió del servidor")
            if removed:
                await self.audit_log(f"🔓 <@{member.id}> salió del servidor: se liberó " + ", ".join(f"**{discord.utils.escape_markdown(r.char_name)}**" for r in removed) + ".")
        except Exception:
            traceback.print_exc()


# ============================================================
# Filtro central: require_registered_user
# ============================================================
class RegistrationGate:
    """Una sola comprobación para TODOS los comandos de miembros (slash y con prefijo).
    Activa por defecto; un administrador puede apagarla con /registro_obligatorio desactivar."""

    MESSAGE = ("🔒 Para usar los comandos del bot tenés que estar **registrado**. "
               "Usá `/registrar usuario_de_albion:TuPersonaje` y seguí los pasos.")

    def __init__(self, bot: commands.Bot, db_path: str, env_off: bool = False):
        self.bot, self.db_path, self.env_off = bot, db_path, env_off
        self._flag_cache = (0.0, False)

    def invalidate(self):
        self._flag_cache = (0.0, False)

    async def enabled(self) -> bool:
        import time
        at, val = self._flag_cache
        if time.monotonic() - at < 10:
            return val
        val = (await asyncio.to_thread(rdb.registration_required, self.db_path)) and not self.env_off
        self._flag_cache = (time.monotonic(), val)
        return val

    async def allowed(self, user, command_name: Optional[str]) -> bool:
        """True si puede seguir. Reglas: apagado → todos; bots, administradores y los comandos del propio registro → exentos."""
        if getattr(user, "bot", False) or not await self.enabled():
            return True
        if command_name and (command_name in EXEMPT_COMMANDS or command_name.split(" ")[0] in EXEMPT_COMMANDS):
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.administrator:                 # los administradores nunca quedan bloqueados
            return True
        return await asyncio.to_thread(rdb.is_registered, self.db_path, user.id)

    async def check_interaction(self, interaction: discord.Interaction) -> bool:
        cmd = getattr(interaction, "command", None)
        name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None)
        if await self.allowed(interaction.user, name):
            return True
        await _say(interaction, self.MESSAGE)
        return False

    async def check_context(self, ctx: commands.Context) -> bool:
        name = getattr(getattr(ctx, "command", None), "qualified_name", None)
        if name is None or await self.allowed(ctx.author, name):
            return True
        try:
            await ctx.reply(self.MESSAGE, mention_author=False, delete_after=15, allowed_mentions=NO_MENTIONS)
        except Exception:
            pass
        raise RegistrationRequired()


async def require_registered_user(db_path: str, user) -> bool:
    """Para usar a mano en comandos nuevos: True si el usuario tiene un registro válido."""
    return await asyncio.to_thread(rdb.is_registered, db_path, user.id)


def install_gate(bot: commands.Bot, db_path: str) -> RegistrationGate:
    gate = RegistrationGate(bot, db_path, env_off=os.getenv("REGISTRO_OBLIGATORIO", "").strip().lower() in ("0", "false", "no"))
    bot.registration_gate = gate
    bot.tree.interaction_check = gate.check_interaction            # corre antes de CADA comando slash
    bot.add_check(gate.check_context)                              # y de cada comando con prefijo
    previous = bot.tree.on_error

    async def on_error(interaction, error):
        if isinstance(error, app_commands.CheckFailure):          # ya le respondimos al usuario en check_interaction
            return
        await previous(interaction, error)

    bot.tree.on_error = on_error
    return gate


async def setup_registration(bot: commands.Bot, db_path: str, guild_id: int, *, required_guild: str = "",
                             default_log_channel: int = 0, albion_guild_id: str = "", member_role_id: int = 0,
                             public_role_id: int = 0) -> RegistrationCog:
    cog = RegistrationCog(bot, db_path, guild_id, required_guild=required_guild,
                          default_log_channel=int(os.getenv("REGISTRO_CANAL_ID", "0") or default_log_channel or 0),
                          albion_guild_id=albion_guild_id, member_role_id=member_role_id, public_role_id=public_role_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    install_gate(bot, db_path)
    return cog
