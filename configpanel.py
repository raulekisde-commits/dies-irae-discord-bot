"""/config — panel de administración dentro de Discord (solo administradores y el dueño del servidor).

Sirve para cambiar sin tocar código: los canales del bot (botsettings.py) y los permisos de los comandos de economía/casino
(los mismos datos que /eco perms-comando y /eco perms-canal). Cada cambio se valida en el bot (no solo en los botones),
queda registrado y se avisa en el canal de logs."""
import asyncio
import traceback
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import botsettings as bs
import economy_db as eco

NO_MENTIONS = discord.AllowedMentions.none()
ECO_LOG_KEY = "eco_log"          # el canal de registro de economía vive en eco_settings, no en bot_settings
ECO_LOG_LABEL = "Registro de economía"


def is_admin(member) -> bool:
    """Propietario del servidor o permiso Administrador (se revisa en cada pulsación)."""
    guild = getattr(member, "guild", None)
    if guild is None or not isinstance(member, discord.Member):
        return False
    return guild.owner_id == member.id or bool(member.guild_permissions.administrator)


async def _say(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except Exception:
        traceback.print_exc()


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def db(self, fn, *args):
        return await asyncio.to_thread(fn, self.db_path, *args)

    # ---------- lógica (sin interfaz) ----------
    def keys(self) -> List[Tuple[str, str, str]]:
        """[(clave, nombre, ayuda)] de todos los canales configurables."""
        items = [(s.key, s.label, s.help) for s in bs.all_settings()]
        items.append((ECO_LOG_KEY, ECO_LOG_LABEL, "Registro de los movimientos de economía (/addmoney, /give-money, premios…)"))
        return items

    def label_of(self, key: str) -> str:
        return next((lab for k, lab, _ in self.keys() if k == key), key)

    async def current(self, key: str) -> Tuple[int, bool]:
        """(canal vigente, si es uno puesto por un administrador). 0 = sin canal."""
        if key == ECO_LOG_KEY:
            raw = await self.db(eco.get_setting, "log_channel_id")
            return (int(raw) if raw and str(raw).isdigit() else 0), True
        return bs.get(key), bs.override(key) is not None

    async def set_channel(self, guild: discord.Guild, actor: discord.Member, key: str, channel) -> str:
        """Cambia un canal. Devuelve el texto para mostrar; empieza con ❌ si no se cambió."""
        if not is_admin(actor):
            return "❌ Solo el propietario o los administradores pueden cambiar la configuración."
        if key not in {k for k, _, _ in self.keys()}:
            return "❌ Ese ajuste no existe."
        ch = guild.get_channel(getattr(channel, "id", 0))
        if ch is None or getattr(ch, "guild", guild).id != guild.id:
            return "❌ Elegí un canal de este servidor."
        me = guild.me
        if me is not None and hasattr(ch, "permissions_for"):
            perms = ch.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                return f"❌ No puedo escribir en {ch.mention}. Dale al bot los permisos *Ver canal* y *Enviar mensajes* ahí y probá de nuevo."
        old, _ = await self.current(key)
        if key == ECO_LOG_KEY:
            await self.db(eco.set_setting, "log_channel_id", str(ch.id))
        else:
            await asyncio.to_thread(bs.set_value, key, ch.id, actor.id)
        await self.audit(guild, f"⚙️ <@{actor.id}> cambió **{self.label_of(key)}**: {f'<#{old}>' if old else 'sin canal'} → {ch.mention}")
        return f"✅ **{self.label_of(key)}** ahora es {ch.mention}. Ya está en uso."

    async def reset_channel(self, guild: discord.Guild, actor: discord.Member, key: str) -> str:
        if not is_admin(actor):
            return "❌ Solo el propietario o los administradores pueden cambiar la configuración."
        if key == ECO_LOG_KEY:
            return "ℹ️ El registro de economía no tiene un valor de siempre: elegí otro canal."
        if key not in bs.REGISTRY:
            return "❌ Ese ajuste no existe."
        old, new = await asyncio.to_thread(bs.reset, key, actor.id)
        await self.audit(guild, f"⚙️ <@{actor.id}> restauró **{self.label_of(key)}**: <#{old}> → <#{new}> (el de siempre)")
        return f"✅ **{self.label_of(key)}** volvió al canal de siempre: <#{new}>."

    async def command_roles(self, command: str) -> List[int]:
        return sorted(await self.db(eco.get_command_roles, command))

    async def change_command_role(self, guild: discord.Guild, actor: discord.Member, command: str, role, grant: bool) -> str:
        if not is_admin(actor):
            return "❌ Solo el propietario o los administradores pueden cambiar la configuración."
        if command not in eco.GRANTABLE_COMMANDS:
            return "❌ Ese comando no se puede configurar."
        if role is None or role.id == guild.id:
            return "❌ Elegí un rol del servidor (no @everyone)."
        if grant:
            changed = await self.db(eco.grant_command_role, command, role.id)
            msg = f"✅ {role.mention} ahora puede usar `{command}`." if changed else f"ℹ️ {role.mention} ya podía usar `{command}`."
        else:
            changed = await self.db(eco.revoke_command_role, command, role.id)
            msg = f"✅ {role.mention} ya no puede usar `{command}`." if changed else f"ℹ️ {role.mention} no tenía permiso para `{command}`."
        if changed:
            await self.audit(guild, f"⚙️ <@{actor.id}> {'dio' if grant else 'quitó'} el permiso de `{command}` {'a' if grant else 'a'} {role.mention}")
        return msg

    async def audit(self, guild: discord.Guild, text: str) -> None:
        """Avisa el cambio en el canal de logs (si no se puede, no pasa nada: el cambio ya quedó guardado)."""
        try:
            ch = guild.get_channel(bs.get("log"))
            if ch is None:
                ch = await guild.fetch_channel(bs.get("log"))
            await ch.send(text, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"/config: no pude avisar el cambio en el canal de logs: {exc!r}")

    # ---------- pantallas ----------
    async def main_embed(self) -> discord.Embed:
        emb = discord.Embed(title="⚙️ Configuración del bot", color=discord.Color.blurple(),
                            description="Elegí un canal para cambiarlo, o entrá a **Permisos de comandos**. Los cambios valen al instante.")
        lines = []
        for key, label, _ in self.keys():
            val, custom = await self.current(key)
            where = f"<#{val}>" if val else "⚠️ sin configurar"
            lines.append(f"**{label}** — {where}" + (" ✏️" if custom and key != ECO_LOG_KEY else ""))
        emb.add_field(name="📺 Canales (✏️ = cambiado desde acá)", value="\n".join(lines)[:1024] or "—", inline=False)
        return emb

    async def channel_embed(self, key: str) -> discord.Embed:
        val, custom = await self.current(key)
        hlp = next((h for k, _, h in self.keys() if k == key), "")
        emb = discord.Embed(title=f"📺 {self.label_of(key)}", color=discord.Color.blurple(),
                            description=(hlp + "\n\n" if hlp else "") + f"Ahora: {f'<#{val}>' if val else '⚠️ sin configurar'}"
                            + (" (cambiado desde acá)" if custom and key != ECO_LOG_KEY else " (el de siempre)" if key != ECO_LOG_KEY else ""))
        emb.set_footer(text="Elegí el canal nuevo abajo. El bot necesita poder ver y escribir ahí.")
        return emb

    async def command_embed(self, command: str) -> discord.Embed:
        roles = await self.command_roles(command)
        chan = await self.db(eco.get_command_channel, command)
        emb = discord.Embed(title=f"🔐 Permisos de `{command}`", color=discord.Color.blurple())
        emb.add_field(name="Roles con permiso (además de los administradores)", value=", ".join(f"<@&{r}>" for r in roles) or "Ninguno", inline=False)
        emb.add_field(name="Canal", value=(f"<#{chan}>" if chan else "Cualquier canal (o el que corresponda por defecto)"), inline=False)
        emb.set_footer(text="El canal de cada comando se cambia con /eco perms-canal.")
        return emb

    @app_commands.command(name="config", description="(Administrador) Panel para configurar canales y permisos del bot")
    @app_commands.guild_only()
    async def config(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await _say(interaction, "🔒 Este panel es solo para el propietario del servidor y los administradores.")
        await interaction.response.send_message(embed=await self.main_embed(), view=MainView(self, interaction.user.id), ephemeral=True,
                                                allowed_mentions=NO_MENTIONS)


# ============================================================
# Interfaz
# ============================================================
class _RoleRef:
    """Un rol borrado de Discord: solo hace falta su id para quitarle el permiso."""
    def __init__(self, role_id: int):
        self.id, self.mention = role_id, f"<@&{role_id}>"


class _AdminView(discord.ui.View):
    def __init__(self, cog: ConfigCog, user_id: int):
        super().__init__(timeout=300.0)
        self.cog, self.user_id = cog, user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _say(interaction, "❌ Este panel es de otra persona. Abrí el tuyo con `/config`.")
            return False
        if not is_admin(interaction.user):                      # se vuelve a revisar: si le sacaron el permiso, deja de funcionar
            await _say(interaction, "🔒 Ya no tenés permisos de administrador.")
            return False
        return True

    async def show_main(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=await self.cog.main_embed(), view=MainView(self.cog, self.user_id))


class MainView(_AdminView):
    def __init__(self, cog: ConfigCog, user_id: int):
        super().__init__(cog, user_id)
        options = [discord.SelectOption(label=lab[:100], value=key, description=(hlp or None) and hlp[:100]) for key, lab, hlp in cog.keys()][:25]
        sel = discord.ui.Select(placeholder="Elegí qué canal cambiar…", options=options)
        sel.callback = self._pick
        self.add_item(sel)
        self._select = sel
        perms = discord.ui.Button(label="Permisos de comandos", emoji="🔐", style=discord.ButtonStyle.primary)
        close = discord.ui.Button(label="Cerrar", style=discord.ButtonStyle.secondary)
        perms.callback, close.callback = self._perms, self._close
        self.add_item(perms)
        self.add_item(close)

    async def _pick(self, interaction: discord.Interaction):
        key = self._select.values[0]
        await interaction.response.edit_message(embed=await self.cog.channel_embed(key), view=ChannelView(self.cog, self.user_id, key))

    async def _perms(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=discord.Embed(title="🔐 Permisos de comandos", color=discord.Color.blurple(),
                                                                    description="Elegí un comando para ver y cambiar qué roles lo pueden usar."),
                                                view=PermsView(self.cog, self.user_id))

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Panel cerrado.", embed=None, view=None)


class ChannelView(_AdminView):
    def __init__(self, cog: ConfigCog, user_id: int, key: str):
        super().__init__(cog, user_id)
        self.key = key
        pick = discord.ui.ChannelSelect(placeholder="Elegí el canal nuevo…", channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        pick.callback = self._chosen
        self.add_item(pick)
        self._pick = pick
        if key != ECO_LOG_KEY:
            reset = discord.ui.Button(label="Volver al de siempre", emoji="↩️", style=discord.ButtonStyle.secondary)
            reset.callback = self._reset
            self.add_item(reset)
        back = discord.ui.Button(label="Volver", style=discord.ButtonStyle.secondary)
        back.callback = self.show_main
        self.add_item(back)

    async def _chosen(self, interaction: discord.Interaction):
        text = await self.cog.set_channel(interaction.guild, interaction.user, self.key, self._pick.values[0])
        await interaction.response.edit_message(content=text, embed=await self.cog.channel_embed(self.key),
                                                view=ChannelView(self.cog, self.user_id, self.key))

    async def _reset(self, interaction: discord.Interaction):
        text = await self.cog.reset_channel(interaction.guild, interaction.user, self.key)
        await interaction.response.edit_message(content=text, embed=await self.cog.channel_embed(self.key),
                                                view=ChannelView(self.cog, self.user_id, self.key))


class PermsView(_AdminView):
    def __init__(self, cog: ConfigCog, user_id: int):
        super().__init__(cog, user_id)
        options = [discord.SelectOption(label=c, value=c) for c in eco.GRANTABLE_COMMANDS][:25]
        sel = discord.ui.Select(placeholder="Elegí un comando…", options=options)
        sel.callback = self._pick
        self.add_item(sel)
        self._select = sel
        back = discord.ui.Button(label="Volver", style=discord.ButtonStyle.secondary)
        back.callback = self.show_main
        self.add_item(back)

    async def _pick(self, interaction: discord.Interaction):
        command = self._select.values[0]
        await interaction.response.edit_message(content=None, embed=await self.cog.command_embed(command),
                                                view=await CommandView.build(self.cog, self.user_id, command, interaction.guild))


class CommandView(_AdminView):
    @classmethod
    async def build(cls, cog: ConfigCog, user_id: int, command: str, guild: discord.Guild):
        view = cls(cog, user_id, command, await cog.command_roles(command), guild)
        return view

    def __init__(self, cog: ConfigCog, user_id: int, command: str, current_roles: List[int], guild: discord.Guild):
        super().__init__(cog, user_id)
        self.command = command
        grant = discord.ui.RoleSelect(placeholder="Dar permiso a un rol…", min_values=1, max_values=1)
        grant.callback = self._grant
        self.add_item(grant)
        self._grant_sel = grant
        if current_roles:
            opts = []
            for rid in current_roles[:25]:
                role = guild.get_role(rid)
                opts.append(discord.SelectOption(label=(role.name if role else f"Rol {rid}")[:100], value=str(rid)))
            revoke = discord.ui.Select(placeholder="Quitar permiso a un rol…", options=opts)
            revoke.callback = self._revoke
            self.add_item(revoke)
            self._revoke_sel = revoke
        back = discord.ui.Button(label="Volver", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _refresh(self, interaction: discord.Interaction, text: str):
        await interaction.response.edit_message(content=text, embed=await self.cog.command_embed(self.command),
                                                view=await CommandView.build(self.cog, self.user_id, self.command, interaction.guild))

    async def _grant(self, interaction: discord.Interaction):
        text = await self.cog.change_command_role(interaction.guild, interaction.user, self.command, self._grant_sel.values[0], True)
        await self._refresh(interaction, text)

    async def _revoke(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self._revoke_sel.values[0]))
        if role is None:                                           # el rol ya no existe: se limpia igual con un objeto mínimo
            role = _RoleRef(int(self._revoke_sel.values[0]))
        text = await self.cog.change_command_role(interaction.guild, interaction.user, self.command, role, False)
        await self._refresh(interaction, text)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=None, embed=discord.Embed(title="🔐 Permisos de comandos", color=discord.Color.blurple(),
                                                                                  description="Elegí un comando para ver y cambiar qué roles lo pueden usar."),
                                                view=PermsView(self.cog, self.user_id))


async def setup_config(bot: commands.Bot, db_path: str, guild_id: int) -> ConfigCog:
    cog = ConfigCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
