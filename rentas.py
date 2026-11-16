"""Servidor de rentas (Albion): paneles, tickets, canales de clientes y cobro semanal (capa de Discord).

Vive en un servidor aparte (RENT_GUILD_ID). Paneles de bienvenida en español, portugués e inglés con 3 botones:
- Quiero rentar (HO / territorio) y Permisos de crafteo → formulario → ticket privado + aviso al Council.
- Información / tarifas → mensaje efímero con el texto de ese idioma (se edita con /renta info).
El Council aprueba o rechaza cada ticket. Al aprobar, el ticket pasa a ser el canal del cliente, dentro de la categoría del mapa.
Cobro: domingos 00:00 UTC (cobro) y 23:00 UTC (recordatorio a impagos). Datos en rentas_db.
"""
import asyncio
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import rentas_db as rdb

NO_MENTIONS = discord.AllowedMentions.none()
PING = discord.AllowedMentions(roles=True, users=True, everyone=False)
GOLD = 0xF1C40F
GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB
LOG_KEYS = {"tickets": "log_tickets", "rentas": "log_rentas", "pagos": "log_pagos"}
SELECT_LIMIT = 25                     # Discord: máx. 25 opciones por menú

# ============================================================
# Textos (lo que ve el cliente va en su idioma; lo del Council, en español)
# ============================================================
T: Dict[str, Dict[str, str]] = {
    "es": {
        "panel_title": "🏰 Rentas en Albion",
        "panel_desc": ("Elegí una opción:\n\n🏠 **Quiero rentar**: alquilá un HO o un territorio en un mapa.\n"
                       "🛠️ **Permisos de crafteo**: pedí permisos de crafteo en un HO.\nℹ️ **Información / tarifas**: precios y condiciones.\n\n"
                       "Al completar el formulario se abre un ticket privado con el Council."),
        "btn_rent": "Quiero rentar", "btn_perm": "Permisos de crafteo", "btn_info": "Información / tarifas",
        "m_rent": "Quiero rentar", "m_perm": "Permisos de crafteo",
        "f_map": "Mapa", "f_map_ph": "Escribí el nombre del mapa", "f_tipo": "¿Qué querés rentar?",
        "f_extras": "Extras (solo HO, opcional)", "f_for": "¿Para quién es?", "f_name": "Nombre de la guild, alianza o personaje",
        "opt_nivel3": "Subir el HO a nivel 3", "opt_ponemos": "Que lo pongamos nosotros",
        "no_maps": "❌ Todavía no hay mapas disponibles.", "bad_map": "❌ Ese mapa no está en la lista.",
        "already": "ℹ️ Ya tenés un ticket abierto: {ch}", "opened": "✅ Ticket abierto: {ch}",
        "t_rent": "🏠 Solicitud de renta", "t_perm": "🛠️ Solicitud de permisos de crafteo",
        "t_desc": "{user}, gracias. El Council te va a responder en este canal.",
        "l_tipo": "Tipo", "l_map": "Mapa", "l_holder": "Titular", "l_extras": "Extras",
        "info_empty": "ℹ️ Todavía no hay información cargada.", "error": "❌ Algo falló. Probá de nuevo en un momento.",
        "rejected_dm": "❌ Tu solicitud de **{what}** en **{mapa}** fue rechazada.\n**Motivo:** {motivo}",
        "approved": "✅ ¡Solicitud aprobada! Este es tu canal: acá vas a recibir los avisos de pago todos los domingos.",
        "added_existing": "✅ ¡Solicitud aprobada! Se sumó a tu canal {ch}.",
        "l_price": "Precio semanal", "l_mod": "Modalidad", "l_start": "Inicio", "l_fixed": "Cargos fijos", "l_debt": "Deuda pendiente",
        "l_state": "Estado", "st_ok": "✅ Al día", "st_debt": "⚠️ Con deuda",
        "mod_adelantado": "Por adelantado", "mod_vencido": "Semana vencida", "paid": "pagado", "unpaid": "pendiente",
        "n_week": "💰 Cobro semanal — {date}", "n_initial": "💰 Pago inicial", "n_prev": "Deuda anterior: **{amount}**",
        "n_total": "**Total a pagar: {amount}**", "n_rem": "⏰ Recordatorio: pago pendiente",
        "n_rem_desc": "Tenés **{amount}** sin pagar.", "n_paid": "✅ Pago registrado: **{amount}**. ¡Gracias!",
        "c_prorrateo": "Días hasta el domingo", "c_semana": "Semana", "c_nivel3": "Subir a nivel 3", "c_ponemos": "Lo ponemos nosotros",
        "c_baja": "Días usados (baja)", "week_of": "semana del {date}",
        "tipo_ho": "HO", "tipo_terri": "Territorio", "tipo_permiso": "Permisos de crafteo",
        "h_personaje": "Personaje", "h_guild": "Guild", "h_alianza": "Alianza",
        "f_gtag": "Tu guild y tag de alianza (opcional)", "f_gtag_ph": "Ej: [Timba] Smogg — o solo Smogg si no tenés alianza",
        "l_role": "Rol", "choose_tipo": "¿Qué querés rentar?",
    },
    "pt": {
        "panel_title": "🏰 Aluguéis no Albion",
        "panel_desc": ("Escolha uma opção:\n\n🏠 **Quero alugar**: alugue uma HO ou um território em um mapa.\n"
                       "🛠️ **Permissões de craft**: peça permissões de craft em uma HO.\nℹ️ **Informações / tarifas**: preços e condições.\n\n"
                       "Ao preencher o formulário, abre-se um ticket privado com o Council."),
        "btn_rent": "Quero alugar", "btn_perm": "Permissões de craft", "btn_info": "Informações / tarifas",
        "m_rent": "Quero alugar", "m_perm": "Permissões de craft",
        "f_map": "Mapa", "f_map_ph": "Escreva o nome do mapa", "f_tipo": "O que você quer alugar?",
        "f_extras": "Extras (só HO, opcional)", "f_for": "Para quem é?", "f_name": "Nome da guild, aliança ou personagem",
        "opt_nivel3": "Subir a HO para nível 3", "opt_ponemos": "Que nós a coloquemos",
        "no_maps": "❌ Ainda não há mapas disponíveis.", "bad_map": "❌ Esse mapa não está na lista.",
        "already": "ℹ️ Você já tem um ticket aberto: {ch}", "opened": "✅ Ticket aberto: {ch}",
        "t_rent": "🏠 Pedido de aluguel", "t_perm": "🛠️ Pedido de permissões de craft",
        "t_desc": "{user}, obrigado. O Council vai responder neste canal.",
        "l_tipo": "Tipo", "l_map": "Mapa", "l_holder": "Titular", "l_extras": "Extras",
        "info_empty": "ℹ️ Ainda não há informações cadastradas.", "error": "❌ Algo deu errado. Tente novamente em instantes.",
        "rejected_dm": "❌ Seu pedido de **{what}** em **{mapa}** foi recusado.\n**Motivo:** {motivo}",
        "approved": "✅ Pedido aprovado! Este é o seu canal: aqui você vai receber os avisos de pagamento todo domingo.",
        "added_existing": "✅ Pedido aprovado! Foi adicionado ao seu canal {ch}.",
        "l_price": "Preço semanal", "l_mod": "Modalidade", "l_start": "Início", "l_fixed": "Cobranças fixas", "l_debt": "Dívida pendente",
        "l_state": "Situação", "st_ok": "✅ Em dia", "st_debt": "⚠️ Com dívida",
        "mod_adelantado": "Adiantado", "mod_vencido": "Semana vencida", "paid": "pago", "unpaid": "pendente",
        "n_week": "💰 Cobrança semanal — {date}", "n_initial": "💰 Pagamento inicial", "n_prev": "Dívida anterior: **{amount}**",
        "n_total": "**Total a pagar: {amount}**", "n_rem": "⏰ Lembrete: pagamento pendente",
        "n_rem_desc": "Você tem **{amount}** em aberto.", "n_paid": "✅ Pagamento registrado: **{amount}**. Obrigado!",
        "c_prorrateo": "Dias até domingo", "c_semana": "Semana", "c_nivel3": "Subir para nível 3", "c_ponemos": "Nós colocamos",
        "c_baja": "Dias usados (cancelamento)", "week_of": "semana de {date}",
        "tipo_ho": "HO", "tipo_terri": "Território", "tipo_permiso": "Permissões de craft",
        "h_personaje": "Personagem", "h_guild": "Guild", "h_alianza": "Aliança",
        "f_gtag": "Sua guild e tag da aliança (opcional)", "f_gtag_ph": "Ex: [Timba] Smogg — ou só Smogg se não tiver aliança",
        "l_role": "Cargo", "choose_tipo": "O que você quer alugar?",
    },
    "en": {
        "panel_title": "🏰 Albion rentals",
        "panel_desc": ("Pick an option:\n\n🏠 **I want to rent**: rent a HO or a territory on a map.\n"
                       "🛠️ **Crafting permissions**: request crafting permissions in a HO.\nℹ️ **Info / prices**: prices and terms.\n\n"
                       "Once you fill in the form, a private ticket with the Council is opened."),
        "btn_rent": "I want to rent", "btn_perm": "Crafting permissions", "btn_info": "Info / prices",
        "m_rent": "I want to rent", "m_perm": "Crafting permissions",
        "f_map": "Map", "f_map_ph": "Type the map name", "f_tipo": "What do you want to rent?",
        "f_extras": "Extras (HO only, optional)", "f_for": "Who is it for?", "f_name": "Guild, alliance or character name",
        "opt_nivel3": "Upgrade the HO to level 3", "opt_ponemos": "We place it for you",
        "no_maps": "❌ There are no maps available yet.", "bad_map": "❌ That map is not on the list.",
        "already": "ℹ️ You already have an open ticket: {ch}", "opened": "✅ Ticket opened: {ch}",
        "t_rent": "🏠 Rental request", "t_perm": "🛠️ Crafting permissions request",
        "t_desc": "{user}, thanks. The Council will answer you in this channel.",
        "l_tipo": "Type", "l_map": "Map", "l_holder": "Holder", "l_extras": "Extras",
        "info_empty": "ℹ️ No information has been added yet.", "error": "❌ Something went wrong. Please try again in a moment.",
        "rejected_dm": "❌ Your **{what}** request on **{mapa}** was rejected.\n**Reason:** {motivo}",
        "approved": "✅ Request approved! This is your channel: you'll get the payment notices here every Sunday.",
        "added_existing": "✅ Request approved! It was added to your channel {ch}.",
        "l_price": "Weekly price", "l_mod": "Billing", "l_start": "Start", "l_fixed": "One-time fees", "l_debt": "Outstanding debt",
        "l_state": "Status", "st_ok": "✅ Up to date", "st_debt": "⚠️ Owes money",
        "mod_adelantado": "In advance", "mod_vencido": "Week in arrears", "paid": "paid", "unpaid": "pending",
        "n_week": "💰 Weekly charge — {date}", "n_initial": "💰 Initial payment", "n_prev": "Previous debt: **{amount}**",
        "n_total": "**Total due: {amount}**", "n_rem": "⏰ Reminder: payment pending",
        "n_rem_desc": "You have **{amount}** unpaid.", "n_paid": "✅ Payment recorded: **{amount}**. Thanks!",
        "c_prorrateo": "Days until Sunday", "c_semana": "Week", "c_nivel3": "Level 3 upgrade", "c_ponemos": "We place it",
        "c_baja": "Days used (cancellation)", "week_of": "week of {date}",
        "tipo_ho": "HO", "tipo_terri": "Territory", "tipo_permiso": "Crafting permissions",
        "h_personaje": "Character", "h_guild": "Guild", "h_alianza": "Alliance",
        "f_gtag": "Your guild and alliance tag (optional)", "f_gtag_ph": "E.g. [Timba] Smogg — or just Smogg with no alliance",
        "l_role": "Role", "choose_tipo": "What do you want to rent?",
    },
}
LANG_NAMES = {"es": "Español", "pt": "Português", "en": "English"}
TIPO_ES = {"ho": "HO", "terri": "Territorio", "permiso": "Permisos de crafteo"}
CONCEPTO_ES = {"ho": "HO (semanal)", "terri": "Territorio (semanal)", "permiso": "Permisos (semanal)", "nivel3": "Subir a nivel 3 (fijo)",
               "ponemos": "Lo ponemos nosotros (fijo)"}


def t(lang: str, key: str, **kw) -> str:
    txt = T.get(lang, T["es"]).get(key) or T["es"][key]
    return txt.format(**kw) if kw else txt


def slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (name or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:90] or "cliente"


def ddmm(d) -> str:
    return d.strftime("%d/%m/%Y")


async def _reply(interaction: discord.Interaction, content: Optional[str] = None, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def _optional_select(**kw):
    """Menú que se puede dejar vacío dentro de un formulario."""
    try:
        return discord.ui.Select(required=False, **kw)
    except TypeError:                     # versiones de discord.py sin `required` en Select
        return discord.ui.Select(**kw)


def _label(text: str, component, description: Optional[str] = None):
    return discord.ui.Label(text=text[:45], component=component, description=description)


# ============================================================
# Vistas persistentes
# ============================================================
class PanelView(discord.ui.View):
    def __init__(self, cog: "RentasCog", lang: str):
        super().__init__(timeout=None)
        self.cog, self.lang = cog, lang
        for key, kind, emoji, style in (("btn_rent", "rent", "🏠", discord.ButtonStyle.success),
                                        ("btn_perm", "perm", "🛠️", discord.ButtonStyle.primary),
                                        ("btn_info", "info", "ℹ️", discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=t(lang, key), style=style, emoji=emoji, custom_id=f"rent:{lang}:{kind}")
            b.callback = self._cb(kind)
            self.add_item(b)

    def _cb(self, kind: str):
        async def cb(interaction: discord.Interaction):
            if kind == "info":
                await self.cog.on_info(interaction, self.lang)
            else:
                await self.cog.on_request(interaction, self.lang, kind)
        return cb


class TipoChoiceView(discord.ui.View):
    """Mensaje efímero antes del formulario de renta: HO o territorio (así el formulario entra en 5 campos)."""

    def __init__(self, cog: "RentasCog", lang: str, owner_id: int):
        super().__init__(timeout=120.0)
        self.cog, self.lang, self.owner_id = cog, lang, owner_id
        for tipo, emoji in (("ho", "🏠"), ("terri", "🚩")):
            b = discord.ui.Button(label=t(lang, f"tipo_{tipo}"), emoji=emoji, style=discord.ButtonStyle.primary)
            b.callback = self._cb(tipo)
            self.add_item(b)

    def _cb(self, tipo: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                return
            await self.cog.open_request_modal(interaction, self.lang, "rent", tipo)
        return cb


class TicketView(discord.ui.View):
    """Botones del ticket. Solo el Council (se revisa en cada pulsación)."""

    def __init__(self, cog: "RentasCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.success, emoji="✅", custom_id="rent:t:approve")
    async def approve(self, interaction: discord.Interaction, button):
        await self.cog.on_approve(interaction)

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="rent:t:reject")
    async def reject(self, interaction: discord.Interaction, button):
        await self.cog.on_reject(interaction)


class PaidView(discord.ui.View):
    """Botón de los avisos de cobro. Marca pagado todo lo pendiente del canal. Solo el Council."""

    def __init__(self, cog: "RentasCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Marcar pagado", style=discord.ButtonStyle.success, emoji="💰", custom_id="rent:paid")
    async def paid(self, interaction: discord.Interaction, button):
        await self.cog.on_paid(interaction)


# ============================================================
# Modales
# ============================================================
class RequestModal(discord.ui.Modal):
    """Formulario del cliente. kind = rent (HO / territorio) | perm (permisos de crafteo)."""

    def __init__(self, cog: "RentasCog", lang: str, kind: str, mapas: List[rdb.Mapa], tipo: str = "permiso"):
        title = t(lang, "m_rent" if kind == "rent" else "m_perm") + (f" — {t(lang, 'tipo_' + tipo)}" if kind == "rent" else "")
        super().__init__(title=title[:45])
        self.cog, self.lang, self.kind, self.tipo = cog, lang, kind, (tipo if kind == "rent" else "permiso")
        if len(mapas) <= SELECT_LIMIT:
            self.map_select = discord.ui.Select(options=[discord.SelectOption(label=m.nombre[:100], value=str(m.id)) for m in mapas])
            self.map_text = None
            self.add_item(_label(t(lang, "f_map"), self.map_select))
        else:
            self.map_select = None
            self.map_text = discord.ui.TextInput(label=t(lang, "f_map"), placeholder=t(lang, "f_map_ph"), max_length=90)
            self.add_item(self.map_text)
        self.extras = None
        if kind == "rent" and self.tipo == "ho":
            self.extras = _optional_select(options=[discord.SelectOption(label=t(lang, "opt_nivel3"), value="nivel3"),
                                                    discord.SelectOption(label=t(lang, "opt_ponemos"), value="ponemos")],
                                           min_values=0, max_values=2)
            self.add_item(_label(t(lang, "f_extras"), self.extras))
        holders = rdb.TITULARES_POR_TIPO[self.tipo]
        self.holder = discord.ui.Select(options=[discord.SelectOption(label=t(lang, f"h_{h}"), value=h) for h in holders])
        self.add_item(_label(t(lang, "f_for"), self.holder))
        self.name = discord.ui.TextInput(label=t(lang, "f_name")[:45], max_length=60)
        self.add_item(self.name)
        self.gtag = discord.ui.TextInput(label=t(lang, "f_gtag")[:45], placeholder=t(lang, "f_gtag_ph")[:100], max_length=70, required=False)
        self.add_item(self.gtag)

    async def on_submit(self, interaction: discord.Interaction):
        map_value = self.map_select.values[0] if self.map_select is not None and self.map_select.values else None
        await self.cog.on_request_submitted(
            interaction, self.lang, tipo=self.tipo, guild_label=str(self.gtag.value or ""),
            map_id=int(map_value) if map_value else None, map_text=str(self.map_text.value) if self.map_text is not None else None,
            extras=list(self.extras.values) if self.extras is not None else [],
            holder=self.holder.values[0] if self.holder.values else "", name=str(self.name.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, t(self.lang, "error"))


class ApproveModal(discord.ui.Modal):
    def __init__(self, cog: "RentasCog", ticket: rdb.Ticket, precio: Optional[int], nivel3: Optional[int], ponemos: Optional[int]):
        super().__init__(title="Aprobar renta")
        self.cog, self.ticket = cog, ticket
        self.modalidad = discord.ui.Select(options=[discord.SelectOption(label="Por adelantado", value="adelantado"),
                                                    discord.SelectOption(label="Semana vencida", value="vencido")])
        self.add_item(_label("Modalidad de pago", self.modalidad))
        self.precio = discord.ui.TextInput(label="Precio semanal (silver, sugerido)", max_length=20, placeholder="Ej: 1.5M",
                                           default=rdb.fmt_silver(precio) if precio else None)
        self.add_item(self.precio)
        self.nivel3 = self.ponemos = None
        if ticket.tipo == "ho" and ticket.nivel3:
            self.nivel3 = discord.ui.TextInput(label="Cargo fijo: subir a nivel 3", max_length=20, placeholder="Ej: 5M",
                                               default=rdb.fmt_silver(nivel3) if nivel3 else None)
            self.add_item(self.nivel3)
        if ticket.tipo == "ho" and ticket.ponemos:
            self.ponemos = discord.ui.TextInput(label="Cargo fijo: poner el HO (sugerido)", max_length=20, placeholder="Ej: 2M",
                                                default=rdb.fmt_silver(ponemos) if ponemos else None)
            self.add_item(self.ponemos)
        default_role = rdb.guild_role_name(ticket.guild_nombre, ticket.alianza_tag) if ticket.guild_nombre else None
        self.rol = discord.ui.TextInput(label="Rol de guild (vacío = sin rol)", max_length=70, required=False,
                                        placeholder="Ej: [Timba] Smogg", default=default_role)
        self.add_item(self.rol)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_approve_submitted(
            interaction, self.ticket.id, self.modalidad.values[0] if self.modalidad.values else "",
            str(self.precio.value), str(self.nivel3.value) if self.nivel3 is not None else None,
            str(self.ponemos.value) if self.ponemos is not None else None, str(self.rol.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No se pudo aprobar. No se cobró nada; probá de nuevo.")


class RejectModal(discord.ui.Modal):
    def __init__(self, cog: "RentasCog", ticket_id: int):
        super().__init__(title="Rechazar solicitud")
        self.cog, self.ticket_id = cog, ticket_id
        self.motivo = discord.ui.TextInput(label="Motivo (se le envía por privado)", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.motivo)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.on_reject_submitted(interaction, self.ticket_id, str(self.motivo.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No se pudo rechazar. Probá de nuevo.")


class InfoModal(discord.ui.Modal):
    def __init__(self, cog: "RentasCog", lang: str, current: Optional[str]):
        super().__init__(title=f"Información / tarifas — {LANG_NAMES[lang]}"[:45])
        self.cog, self.lang = cog, lang
        self.texto = discord.ui.TextInput(label="Texto", style=discord.TextStyle.paragraph, max_length=4000, default=current or None)
        self.add_item(self.texto)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.run(rdb.set_info, self.lang, str(self.texto.value))
        await self.cog.log("rentas", f"ℹ️ {interaction.user.mention} actualizó la información / tarifas ({LANG_NAMES[self.lang]}).")
        await _reply(interaction, f"✅ Información en {LANG_NAMES[self.lang]} actualizada.")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _reply(interaction, "❌ No se pudo guardar el texto.")


# ============================================================
# Cog
# ============================================================
IDIOMA_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in LANG_NAMES.items()]
CONCEPTO_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in CONCEPTO_ES.items()]
TITULAR_CHOICES = [app_commands.Choice(name=n, value=v) for n, v in (("Personaje", "personaje"), ("Guild", "guild"), ("Alianza", "alianza"))]


class RentasCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int, council_role_id: int):
        self.bot, self.db_path, self.guild_id, self.council_role_id = bot, db_path, guild_id, council_role_id
        self._lock = asyncio.Lock()

    async def cog_load(self):
        await self.run(rdb.init_schema)
        for lang in rdb.IDIOMAS:
            self.bot.add_view(PanelView(self, lang))
        self.bot.add_view(TicketView(self))
        self.bot.add_view(PaidView(self))
        if not self.scheduler.is_running():
            self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    # ---------- permisos ----------
    def is_council(self, member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        return any(getattr(r, "id", None) == self.council_role_id for r in getattr(member, "roles", []))

    @staticmethod
    def is_admin(member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        return bool(perms is not None and perms.administrator)

    async def _council(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.guild.id != self.guild_id or not self.is_council(interaction.user):
            await _reply(interaction, "❌ Solo el Council.")
            return False
        return True

    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(self.guild_id)

    @property
    def council_role(self):
        g = self.guild
        return g.get_role(self.council_role_id) if g is not None else None

    # ---------- logs ----------
    async def log(self, kind: str, text: str, embed: Optional[discord.Embed] = None):
        try:
            cid = await self.run(rdb.get_config, LOG_KEYS[kind])
            g = self.guild
            ch = g.get_channel(int(cid)) if (cid and g is not None) else None
            if ch is not None:
                await ch.send(text, embed=embed, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()

    # ---------- categorías ----------
    def _private_overwrites(self, guild: discord.Guild) -> dict:
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        if self.council_role is not None:
            ow[self.council_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if guild.me is not None:
            ow[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True)
        return ow

    async def ensure_category(self, guild: discord.Guild, mapa_id: Optional[int], base: str):
        """Categoría del mapa (o «Archivo» si mapa_id es None) con lugar libre; si se llenó, crea «Base (2)», «Base (3)»..."""
        cats = await self.run(rdb.categorias, mapa_id)
        last = 0
        for idx, cid in cats:
            last = max(last, idx)
            cat = guild.get_channel(cid)
            if cat is None:
                await self.run(rdb.forget_categoria, cid)
                continue
            if len(getattr(cat, "channels", [])) < rdb.CATEGORY_LIMIT:
                return cat
        idx = last + 1
        cat = await guild.create_category(rdb.category_name(base, idx), overwrites=self._private_overwrites(guild))
        await self.run(rdb.add_categoria, mapa_id, idx, cat.id)
        return cat

    async def archive_channel(self, channel, remove_user_ids: List[int]):
        """Mueve el canal a «Archivo» y le saca el acceso a los clientes (Council lo sigue viendo)."""
        guild = channel.guild
        cat = await self.ensure_category(guild, None, "Archivo")
        remove = set(remove_user_ids)
        ow = {tg: o for tg, o in channel.overwrites.items() if getattr(tg, "id", None) not in remove}
        await channel.edit(category=cat, overwrites=ow)

    # ---------- panel de bienvenida ----------
    def panel_embed(self, lang: str) -> discord.Embed:
        return discord.Embed(title=t(lang, "panel_title"), description=t(lang, "panel_desc"), color=GOLD)

    async def on_info(self, interaction: discord.Interaction, lang: str):
        try:
            text = await self.run(rdb.get_info, lang)
            await _reply(interaction, embed=discord.Embed(title=t(lang, "btn_info"), description=text or t(lang, "info_empty"), color=BLUE))
        except Exception:
            traceback.print_exc()
            await _reply(interaction, t(lang, "error"))

    async def on_request(self, interaction: discord.Interaction, lang: str, kind: str):
        try:
            existing = await self.run(rdb.open_ticket_of, interaction.user.id)
            if existing is not None:
                return await _reply(interaction, t(lang, "already", ch=f"<#{existing.channel_id}>"))
            if not await self.run(rdb.list_mapas):
                return await _reply(interaction, t(lang, "no_maps"))
            if kind == "rent":
                return await _reply(interaction, t(lang, "choose_tipo"), view=TipoChoiceView(self, lang, interaction.user.id))
            await self.open_request_modal(interaction, lang, kind, "permiso")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, t(lang, "error"))

    async def open_request_modal(self, interaction: discord.Interaction, lang: str, kind: str, tipo: str):
        try:
            mapas = await self.run(rdb.list_mapas)
            if not mapas:
                return await _reply(interaction, t(lang, "no_maps"))
            await interaction.response.send_modal(RequestModal(self, lang, kind, mapas, tipo))
        except Exception:
            traceback.print_exc()
            await _reply(interaction, t(lang, "error"))

    # ---------- abrir ticket ----------
    def ticket_embed(self, tk: rdb.Ticket, mapa: str, user_mention: str) -> discord.Embed:
        lang = tk.idioma
        emb = discord.Embed(title=t(lang, "t_perm" if tk.tipo == "permiso" else "t_rent"), color=GOLD,
                            description=t(lang, "t_desc", user=user_mention))
        emb.add_field(name=t(lang, "l_tipo"), value=t(lang, f"tipo_{tk.tipo}"), inline=True)
        emb.add_field(name=t(lang, "l_map"), value=mapa, inline=True)
        emb.add_field(name=t(lang, "l_holder"), value=f"{t(lang, 'h_' + tk.titular_tipo)}: **{tk.titular_nombre}**", inline=False)
        if tk.guild_nombre:
            emb.add_field(name=t(lang, "l_role"), value=rdb.guild_role_name(tk.guild_nombre, tk.alianza_tag), inline=False)
        if tk.tipo == "ho":
            extras = [t(lang, "opt_nivel3")] * tk.nivel3 + [t(lang, "opt_ponemos")] * tk.ponemos
            emb.add_field(name=t(lang, "l_extras"), value="\n".join(extras) or "—", inline=False)
        emb.set_footer(text=f"Ticket #{tk.id} · {LANG_NAMES[lang]}")
        return emb

    async def on_request_submitted(self, interaction: discord.Interaction, lang: str, *, tipo: str, map_id: Optional[int],
                                   map_text: Optional[str], extras: List[str], holder: str, name: str, guild_label: str = ""):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            existing = await self.run(rdb.open_ticket_of, interaction.user.id)
            if existing is not None:
                return await _reply(interaction, t(lang, "already", ch=f"<#{existing.channel_id}>"))
            mapa = await self.run(rdb.get_mapa, map_id) if map_id else await self.run(rdb.find_mapa, map_text or "")
            if mapa is None or not any(m.id == mapa.id for m in await self.run(rdb.list_mapas)):
                return await _reply(interaction, t(lang, "bad_map"))
            try:
                nombre = rdb.validate_request(tipo, holder, name)
                tag, gname = rdb.parse_guild_label(guild_label)
            except rdb.RentaError as exc:
                return await _reply(interaction, f"❌ {exc}")
            if gname is None and holder == "guild":
                gname = nombre                                   # la guild que renta es la suya
            if gname is None:
                tag = None
            guild, user = interaction.guild, interaction.user
            ow = self._private_overwrites(guild)
            ow[user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
            try:
                channel = await guild.create_text_channel(name=f"ticket-{slug(nombre)}"[:90], category=getattr(interaction.channel, "category", None),
                                                          overwrites=ow, topic=f"Ticket de renta · usuario {user.id}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                print("rentas: no pude crear el ticket:", exc)
                return await _reply(interaction, t(lang, "error"))
            tid = await self.run(rdb.create_ticket, channel.id, user.id, lang, tipo, mapa.id, "nivel3" in extras, "ponemos" in extras,
                                 holder, nombre, gname, tag)
        tk = await self.run(rdb.ticket_by_channel, channel.id)
        role = self.council_role
        msg = await channel.send(content=f"{role.mention if role else ''} {user.mention}".strip(), embed=self.ticket_embed(tk, mapa.nombre, user.mention),
                                 view=TicketView(self), allowed_mentions=PING)
        await self.run(rdb.set_ticket_message, tid, msg.id)
        await _reply(interaction, t(lang, "opened", ch=channel.mention))
        await self.log("tickets", f"🎫 Ticket #{tid} abierto por {user.mention} en {channel.mention}: {TIPO_ES[tipo]} · {mapa.nombre} · "
                                  f"{holder} **{nombre}**")

    # ---------- aprobar ----------
    async def _ticket_here(self, interaction: discord.Interaction) -> Optional[rdb.Ticket]:
        tk = await self.run(rdb.ticket_by_channel, interaction.channel.id)
        if tk is None or tk.status != "open":
            await _reply(interaction, "ℹ️ Este ticket ya fue procesado.")
            return None
        return tk

    async def on_approve(self, interaction: discord.Interaction):
        try:
            if not await self._council(interaction):
                return
            tk = await self._ticket_here(interaction)
            if tk is None:
                return
            precio = await self.run(rdb.get_precio, tk.tipo, tk.mapa_id, tk.titular_tipo)
            n3 = await self.run(rdb.get_precio, "nivel3", tk.mapa_id, tk.titular_tipo) if tk.nivel3 else None
            pon = await self.run(rdb.get_precio, "ponemos", tk.mapa_id, tk.titular_tipo) if tk.ponemos else None
            await interaction.response.send_modal(ApproveModal(self, tk, precio, n3, pon))
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude abrir el formulario.")

    async def on_approve_submitted(self, interaction: discord.Interaction, ticket_id: int, modalidad: str, precio_txt: str,
                                   nivel3_txt: Optional[str], ponemos_txt: Optional[str], rol_txt: str = ""):
        try:
            tag, gname = rdb.parse_guild_label(rol_txt)
            if tag and not gname:
                raise rdb.RentaError("Falta el nombre de la guild en el rol (ej: [Timba] Smogg).")
            precio = rdb.parse_silver(precio_txt)
            n3 = rdb.parse_silver(nivel3_txt) if nivel3_txt is not None else 0
            pon = rdb.parse_silver(ponemos_txt) if ponemos_txt is not None else 0
            if precio <= 0 or modalidad not in rdb.MODALIDADES:
                raise rdb.RentaError("Precio o modalidad inválidos.")
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            tk = await self.run(rdb.ticket_by_channel, interaction.channel.id)
            if tk is None or tk.id != ticket_id or not await self.run(rdb.close_ticket, ticket_id, "approved", interaction.user.id):
                return await _reply(interaction, "ℹ️ Este ticket ya fue procesado.")
            try:
                canal, moved = await self._client_channel_for(interaction.guild, tk, interaction.channel)
            except Exception:
                traceback.print_exc()
                await self.run(rdb.reopen_ticket, ticket_id)
                return await _reply(interaction, "❌ No pude preparar el canal del cliente (¿permisos del bot?). El ticket sigue abierto.")
            hoy = rdb.today_utc()
            rid = await self.run(rdb.create_renta, canal_id=canal.id, ticket=tk, precio=precio, modalidad=modalidad, inicio=hoy,
                                 actor_id=interaction.user.id, nivel3_monto=n3, ponemos_monto=pon)
        await self._close_ticket_message(interaction.channel, tk, f"✅ Aprobado por {interaction.user.display_name}")
        client_ch = interaction.guild.get_channel(canal.channel_id)
        lang = canal.idioma
        if moved:
            await client_ch.send(t(lang, "approved"), allowed_mentions=NO_MENTIONS)
        else:
            await interaction.channel.send(t(tk.idioma, "added_existing", ch=f"<#{canal.channel_id}>"), allowed_mentions=NO_MENTIONS)
            try:
                await self.archive_channel(interaction.channel, [tk.user_id])
            except Exception:
                traceback.print_exc()
        await self.refresh_panel(canal.id)
        pend = [c for c in await self.run(rdb.cargos_renta, rid) if not c.pagado]
        if pend and client_ch is not None:
            await self.send_notice(canal, t(lang, "n_initial"), pend)
        role_note = ""
        if gname:
            role_note = await self.give_guild_role(interaction.guild, tk.user_id, gname, tag)
        mapa = await self.run(rdb.get_mapa, tk.mapa_id)
        await _reply(interaction, f"✅ Renta #{rid} aprobada en <#{canal.channel_id}>.{role_note}")
        await self.log("tickets", f"✅ Ticket #{tk.id} aprobado por {interaction.user.mention} → <#{canal.channel_id}>")
        await self.log("rentas", f"➕ Renta #{rid}: {TIPO_ES[tk.tipo]} · {mapa.nombre if mapa else tk.mapa_id} · {tk.titular_tipo} "
                                 f"**{tk.titular_nombre}** · {rdb.fmt_silver(precio)}/semana · {modalidad} · alta por {interaction.user.mention}")

    async def _client_channel_for(self, guild: discord.Guild, tk: rdb.Ticket, ticket_channel):
        """(canal, moved). Si el titular ya tiene canal en ese mapa, se reutiliza (moved=False); si no, el ticket se convierte en el canal."""
        mapa = await self.run(rdb.get_mapa, tk.mapa_id)
        existing = await self.run(rdb.find_canal, tk.mapa_id, tk.titular_nombre)
        if existing is not None:
            ch = guild.get_channel(existing.channel_id)
            if ch is not None:
                member = guild.get_member(tk.user_id)
                if member is not None:
                    await ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
                await self.run(rdb.add_miembro, existing.id, tk.user_id)
                return existing, False
            await self.run(rdb.set_canal_archivado, existing.id, True)      # el canal se borró a mano: se crea uno nuevo
        cat = await self.ensure_category(guild, tk.mapa_id, mapa.nombre)
        await ticket_channel.edit(name=slug(tk.titular_nombre), category=cat, topic=f"{TIPO_ES[tk.tipo]} · {mapa.nombre} · {tk.titular_nombre}")
        canal = await self.run(rdb.create_canal, ticket_channel.id, tk.mapa_id, tk.titular_tipo, tk.titular_nombre, tk.idioma)
        await self.run(rdb.add_miembro, canal.id, tk.user_id)
        return canal, True

    async def _close_ticket_message(self, channel, tk: rdb.Ticket, footer: str):
        if not tk.message_id:
            return
        try:
            msg = await channel.fetch_message(tk.message_id)
            emb = msg.embeds[0] if msg.embeds else None
            if emb is not None:
                emb.set_footer(text=footer)
            await msg.edit(embed=emb, view=None)
        except Exception:
            traceback.print_exc()

    # ---------- rechazar ----------
    async def on_reject(self, interaction: discord.Interaction):
        try:
            if not await self._council(interaction):
                return
            tk = await self._ticket_here(interaction)
            if tk is not None:
                await interaction.response.send_modal(RejectModal(self, tk.id))
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude abrir el formulario.")

    async def on_reject_submitted(self, interaction: discord.Interaction, ticket_id: int, motivo: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tk = await self.run(rdb.ticket_by_channel, interaction.channel.id)
        if tk is None or tk.id != ticket_id or not await self.run(rdb.close_ticket, ticket_id, "rejected", interaction.user.id, motivo):
            return await _reply(interaction, "ℹ️ Este ticket ya fue procesado.")
        mapa = await self.run(rdb.get_mapa, tk.mapa_id)
        dm_ok = False
        try:
            user = interaction.guild.get_member(tk.user_id) or await self.bot.fetch_user(tk.user_id)
            await user.send(t(tk.idioma, "rejected_dm", what=t(tk.idioma, f"tipo_{tk.tipo}"), mapa=mapa.nombre if mapa else "?", motivo=motivo))
            dm_ok = True
        except Exception as exc:
            print("rentas: no pude enviar el privado de rechazo:", exc)
        await self._close_ticket_message(interaction.channel, tk, f"✖️ Rechazado por {interaction.user.display_name}")
        try:
            await self.archive_channel(interaction.channel, [tk.user_id])
        except Exception:
            traceback.print_exc()
        await _reply(interaction, "✅ Rechazado. " + ("Se le envió el motivo por privado." if dm_ok else "⚠️ No se le pudo enviar el privado (DMs cerrados)."))
        await self.log("tickets", f"✖️ Ticket #{tk.id} rechazado por {interaction.user.mention} · <@{tk.user_id}> · privado: "
                                  f"{'enviado' if dm_ok else 'NO llegó'}\n**Motivo:** {motivo}")

    # ---------- canal del cliente ----------
    async def client_embed(self, canal: rdb.Canal) -> discord.Embed:
        lang = canal.idioma
        mapas = await self.run(rdb.mapa_names)
        rentas = await self.run(rdb.rentas_canal, canal.id)
        deuda = await self.run(rdb.deuda_canal, canal.id)
        emb = discord.Embed(title=f"📋 {canal.titular_nombre}", color=RED if deuda else GREEN,
                            description=f"{t(lang, 'h_' + canal.titular_tipo)} · {mapas.get(canal.mapa_id, '?')}")
        for r in rentas[:20]:
            cargos = await self.run(rdb.cargos_renta, r.id)
            fixed = [f"{t(lang, 'c_' + c.concepto)}: {rdb.fmt_silver(c.monto)} ({t(lang, 'paid' if c.pagado else 'unpaid')})"
                     for c in cargos if c.concepto in ("nivel3", "ponemos")]
            lines = [f"**{t(lang, 'l_price')}:** {rdb.fmt_silver(r.precio)}",
                     f"**{t(lang, 'l_mod')}:** {t(lang, 'mod_' + r.modalidad)}",
                     f"**{t(lang, 'l_start')}:** {ddmm(r.fecha_inicio)}"]
            if fixed:
                lines.append(f"**{t(lang, 'l_fixed')}:** " + " · ".join(fixed))
            emb.add_field(name=f"{t(lang, 'tipo_' + r.tipo)} · {mapas.get(r.mapa_id, '?')}", value="\n".join(lines), inline=False)
        emb.add_field(name=t(lang, "l_state"), value=t(lang, "st_debt" if deuda else "st_ok"), inline=True)
        emb.add_field(name=t(lang, "l_debt"), value=rdb.fmt_silver(deuda), inline=True)
        return emb

    async def refresh_panel(self, canal_id: int):
        """Crea o actualiza el panel fijado del canal del cliente."""
        try:
            canal = await self.run(rdb.get_canal, canal_id)
            g = self.guild
            ch = g.get_channel(canal.channel_id) if (canal and g is not None) else None
            if ch is None:
                return
            emb = await self.client_embed(canal)
            if canal.panel_message_id:
                try:
                    msg = await ch.fetch_message(canal.panel_message_id)
                    await msg.edit(embed=emb)
                    return
                except discord.NotFound:
                    pass
            msg = await ch.send(embed=emb, allowed_mentions=NO_MENTIONS)
            await self.run(rdb.set_canal_panel, canal.id, msg.id)
            try:
                await msg.pin()
            except Exception:
                pass
        except Exception:
            traceback.print_exc()

    async def send_notice(self, canal: rdb.Canal, title: str, cargos: List[rdb.Cargo], reminder: bool = False):
        """Aviso de cobro en el canal del cliente, con el botón «Marcar pagado»."""
        g = self.guild
        ch = g.get_channel(canal.channel_id) if g is not None else None
        if ch is None:
            return
        lang = canal.idioma
        mapas = await self.run(rdb.mapa_names)
        deuda = await self.run(rdb.deuda_canal, canal.id)
        if deuda <= 0:
            return
        if reminder:
            desc = t(lang, "n_rem_desc", amount=rdb.fmt_silver(deuda))
        else:
            rentas = {r.id: r for r in await self.run(rdb.rentas_canal, canal.id, False)}
            lines = []
            for c in cargos:
                r = rentas.get(c.renta_id)
                head = f"{t(lang, 'tipo_' + r.tipo)} · {mapas.get(r.mapa_id, '?')}" if r else "?"
                extra = f" ({t(lang, 'week_of', date=ddmm(rdb._d(c.periodo)))})" if c.concepto == "semana" else ""
                lines.append(f"• {head} — {t(lang, 'c_' + c.concepto)}{extra}: **{rdb.fmt_silver(c.monto)}**")
            prev = deuda - sum(c.monto for c in cargos if not c.pagado)
            if prev > 0:
                lines.append(t(lang, "n_prev", amount=rdb.fmt_silver(prev)))
            lines.append("")
            lines.append(t(lang, "n_total", amount=rdb.fmt_silver(deuda)))
            desc = "\n".join(lines)
        emb = discord.Embed(title=title, description=desc, color=RED if reminder else GOLD)
        pings = " ".join(f"<@{u}>" for u in await self.run(rdb.miembros, canal.id))
        try:
            await ch.send(content=pings or None, embed=emb, view=PaidView(self), allowed_mentions=PING)
        except Exception:
            traceback.print_exc()

    async def on_paid(self, interaction: discord.Interaction):
        try:
            if not await self._council(interaction):
                return
            canal = await self.run(rdb.canal_by_channel, interaction.channel.id)
            if canal is None:
                return await _reply(interaction, "❌ Este no es un canal de cliente.")
            total = await self.run(rdb.marcar_pagado_canal, canal.id, interaction.user.id)
            if not total:
                return await _reply(interaction, "ℹ️ No hay nada pendiente en este canal.")
            try:
                emb = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
                if emb is not None:
                    emb.set_footer(text=f"✅ Pagado · marcado por {interaction.user.display_name}")
                await interaction.response.edit_message(embed=emb, view=None)
            except Exception:
                traceback.print_exc()
            await interaction.channel.send(t(canal.idioma, "n_paid", amount=rdb.fmt_silver(total)), allowed_mentions=NO_MENTIONS)
            await self.refresh_panel(canal.id)
            await self.log("pagos", f"💰 {interaction.user.mention} marcó pagado **{rdb.fmt_silver(total)}** en <#{canal.channel_id}> "
                                    f"({canal.titular_nombre})")
        except Exception:
            traceback.print_exc()
            await _reply(interaction, "❌ No pude registrar el pago.")

    # ---------- cobro semanal ----------
    @tasks.loop(minutes=1)
    async def scheduler(self):
        try:
            await self.tick(datetime.now(timezone.utc))
        except Exception:
            traceback.print_exc()

    @scheduler.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()

    async def tick(self, now: datetime) -> List[str]:
        """Corre lo que toque: cobro del último domingo (si no se hizo) y recordatorio de los domingos a las 23:00 UTC."""
        done = []
        sunday = rdb.week_start(now.date())
        if await self.run(rdb.mark_job, f"cobro:{sunday.isoformat()}"):
            canal_ids = await self.run(rdb.run_weekly, sunday)
            sent = 0
            for cid in canal_ids:
                canal = await self.run(rdb.get_canal, cid)
                if canal is None or canal.archivado:
                    continue
                cargos = await self.run(rdb.cargos_lote_canal, cid, sunday)
                await self.send_notice(canal, t(canal.idioma, "n_week", date=ddmm(sunday)), cargos)
                await self.refresh_panel(cid)
                sent += 1
            done.append("cobro")
            await self.log("pagos", f"🗓️ Cobro semanal del {ddmm(sunday)} generado · {sent} canal(es) avisado(s).")
        if now.weekday() == 6 and now.hour >= 23 and await self.run(rdb.mark_job, f"aviso:{sunday.isoformat()}"):
            n = 0
            for canal in await self.run(rdb.active_canales):
                if await self.run(rdb.deuda_canal, canal.id) > 0:
                    await self.send_notice(canal, t(canal.idioma, "n_rem"), [], reminder=True)
                    n += 1
            done.append("aviso")
            await self.log("pagos", f"⏰ Recordatorio del {ddmm(sunday)} enviado a {n} canal(es) con deuda.")
        return done

    # ============================================================
    # /renta
    # ============================================================
    renta = app_commands.Group(name="renta", description="Rentas de Albion (Council)", guild_only=True)
    mapa_g = app_commands.Group(name="mapa", description="Lista de mapas", parent=renta)

    async def _map_autocomplete(self, interaction: discord.Interaction, current: str):
        mapas = await self.run(rdb.list_mapas)
        cur = (current or "").lower()
        return [app_commands.Choice(name=m.nombre[:100], value=m.nombre[:100]) for m in mapas if cur in m.nombre.lower()][:25]

    @renta.command(name="instalar", description="(Council) Crea los canales de logs y la categoría Archivo")
    async def instalar(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        try:
            created = []
            cat = None
            for kind, key in LOG_KEYS.items():
                cid = await self.run(rdb.get_config, key)
                if cid and guild.get_channel(int(cid)) is not None:
                    continue
                if cat is None:
                    cat = await guild.create_category("Logs rentas", overwrites=self._private_overwrites(guild))
                ch = await guild.create_text_channel(f"logs-{kind}", category=cat, overwrites=self._private_overwrites(guild))
                await self.run(rdb.set_config, key, ch.id)
                created.append(ch.mention)
            await self.ensure_category(guild, None, "Archivo")
            await _reply(interaction, "✅ Listo. " + (f"Canales creados: {', '.join(created)}." if created else "Los canales de logs ya existían."))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await _reply(interaction, f"❌ No pude crear los canales: {exc}")

    @renta.command(name="panel", description="(Council) Publica el panel de bienvenida en un idioma")
    @app_commands.choices(idioma=IDIOMA_CHOICES)
    async def panel(self, interaction: discord.Interaction, idioma: app_commands.Choice[str], canal: Optional[discord.TextChannel] = None):
        if not await self._council(interaction):
            return
        ch = canal or interaction.channel
        try:
            await ch.send(embed=self.panel_embed(idioma.value), view=PanelView(self, idioma.value), allowed_mentions=NO_MENTIONS)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await _reply(interaction, f"❌ No pude publicar en {ch.mention}: {exc}")
        await _reply(interaction, f"✅ Panel en {idioma.name} publicado en {ch.mention}.")

    @renta.command(name="info", description="(Council) Edita el texto de Información / tarifas de un idioma")
    @app_commands.choices(idioma=IDIOMA_CHOICES)
    async def info(self, interaction: discord.Interaction, idioma: app_commands.Choice[str]):
        if not await self._council(interaction):
            return
        current = await self.run(rdb.get_info, idioma.value)
        await interaction.response.send_modal(InfoModal(self, idioma.value, current))

    @mapa_g.command(name="agregar", description="(Council) Agrega un mapa con su precio semanal de HO y el precio fijo de poner el HO")
    @app_commands.describe(semanal="Precio semanal sugerido del HO (ej: 1.5M)", poner_ho="Precio fijo sugerido de poner el HO (ej: 2M)")
    async def mapa_agregar(self, interaction: discord.Interaction, nombre: str, semanal: Optional[str] = None, poner_ho: Optional[str] = None):
        if not await self._council(interaction):
            return
        try:
            sem = rdb.parse_silver(semanal) if semanal else None
            pon = rdb.parse_silver(poner_ho) if poner_ho else None
            m = await self.run(rdb.add_mapa, nombre)
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        for concepto, value in (("ho", sem), ("ponemos", pon)):
            if value:
                for holder in ("guild", "alianza"):
                    await self.run(rdb.set_precio, concepto, m.id, holder, value)
        precios = " · ".join(x for x in ((f"semanal {rdb.fmt_silver(sem)}" if sem else ""), (f"poner el HO {rdb.fmt_silver(pon)}" if pon else "")) if x)
        n = len(await self.run(rdb.list_mapas))
        warn = f"\n⚠️ Hay {n} mapas: el formulario pasa a pedir el mapa escrito (Discord admite 25 opciones)." if n > SELECT_LIMIT else ""
        await _reply(interaction, f"✅ Mapa **{m.nombre}** agregado" + (f" ({precios})" if precios else "") + f".{warn}")
        await self.log("rentas", f"🗺️ {interaction.user.mention} agregó el mapa **{m.nombre}**" + (f" ({precios})" if precios else "") + ".")

    @mapa_g.command(name="quitar", description="(Council) Quita un mapa (solo si no tiene rentas activas)")
    async def mapa_quitar(self, interaction: discord.Interaction, nombre: str):
        if not await self._council(interaction):
            return
        try:
            m = await self.run(rdb.remove_mapa, nombre)
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await _reply(interaction, f"✅ Mapa **{m.nombre}** quitado de la lista.")
        await self.log("rentas", f"🗺️ {interaction.user.mention} quitó el mapa **{m.nombre}**.")

    @mapa_g.command(name="editar", description="(Council) Cambia el nombre de un mapa (y de sus categorías)")
    async def mapa_editar(self, interaction: discord.Interaction, actual: str, nuevo: str):
        if not await self._council(interaction):
            return
        try:
            m = await self.run(rdb.rename_mapa, actual, nuevo)
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        for idx, cid in await self.run(rdb.categorias, m.id):
            cat = interaction.guild.get_channel(cid)
            if cat is not None:
                try:
                    await cat.edit(name=rdb.category_name(m.nombre, idx))
                except Exception:
                    traceback.print_exc()
        for canal in await self.run(rdb.active_canales):
            if canal.mapa_id == m.id:
                await self.refresh_panel(canal.id)
        await _reply(interaction, f"✅ Mapa renombrado a **{m.nombre}**.")
        await self.log("rentas", f"🗺️ {interaction.user.mention} renombró el mapa **{actual}** → **{m.nombre}**.")

    @mapa_g.command(name="lista", description="(Council) Muestra la lista de mapas")
    async def mapa_lista(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        mapas = await self.run(rdb.list_mapas)
        await _reply(interaction, embed=discord.Embed(title="🗺️ Mapas", color=BLUE,
                                                      description="\n".join(f"• {m.nombre}" for m in mapas)[:4000] or "No hay mapas."))

    mapa_quitar.autocomplete("nombre")(_map_autocomplete)
    mapa_editar.autocomplete("actual")(_map_autocomplete)

    @renta.command(name="precio", description="(Council) Fija un precio (semanal o cargo fijo) por mapa y titular")
    @app_commands.choices(concepto=CONCEPTO_CHOICES, titular=TITULAR_CHOICES)
    async def precio(self, interaction: discord.Interaction, concepto: app_commands.Choice[str], mapa: str,
                     titular: app_commands.Choice[str], monto: str):
        if not await self._council(interaction):
            return
        try:
            m = await self.run(rdb.find_mapa, mapa)
            if m is None:
                raise rdb.RentaError(f"No existe el mapa «{mapa}».")
            if concepto.value in rdb.TIPOS and titular.value not in rdb.TITULARES_POR_TIPO[concepto.value]:
                raise rdb.RentaError(f"{TIPO_ES[concepto.value]} no se renta a un {titular.value}.")
            if concepto.value in ("nivel3", "ponemos") and titular.value == "personaje":
                raise rdb.RentaError("Los cargos de HO no aplican a personajes.")
            value = rdb.parse_silver(monto)
            await self.run(rdb.set_precio, concepto.value, m.id, titular.value, value)
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await _reply(interaction, f"✅ {concepto.name} · {m.nombre} · {titular.name}: **{rdb.fmt_silver(value)}**")
        await self.log("rentas", f"💲 {interaction.user.mention} fijó {concepto.name} · {m.nombre} · {titular.name} = **{rdb.fmt_silver(value)}**")

    precio.autocomplete("mapa")(_map_autocomplete)

    @renta.command(name="precios", description="(Council) Lista de precios cargados")
    async def precios(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        rows = await self.run(rdb.list_precios)
        lines, last = [], None
        for mapa, concepto, titular, monto in rows:
            if mapa != last:
                lines.append(f"\n**{mapa}**")
                last = mapa
            lines.append(f"• {CONCEPTO_ES[concepto]} · {titular}: {rdb.fmt_silver(monto)}")
        await _reply(interaction, embed=discord.Embed(title="💲 Precios", color=BLUE, description="\n".join(lines).strip()[:4000] or "No hay precios cargados."))

    @renta.command(name="agregar-miembro", description="(Admin) Da acceso a otra persona al canal de este cliente")
    async def agregar_miembro(self, interaction: discord.Interaction, usuario: discord.Member):
        if not self.is_admin(interaction.user):
            return await _reply(interaction, "❌ Solo un administrador.")
        canal = await self.run(rdb.canal_by_channel, interaction.channel.id)
        if canal is None or canal.archivado:
            return await _reply(interaction, "❌ Usalo dentro del canal de un cliente.")
        try:
            await interaction.channel.set_permissions(usuario, view_channel=True, send_messages=True, read_message_history=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await _reply(interaction, f"❌ No pude darle acceso: {exc}")
        await self.run(rdb.add_miembro, canal.id, usuario.id)
        note = ""
        first = (await self.run(rdb.miembros, canal.id))[0]
        g = await self.run(rdb.user_guild, first) if first != usuario.id else None
        if g is not None and await self.run(rdb.user_guild, usuario.id) is None:
            note = await self.give_guild_role(interaction.guild, usuario.id, g.guild_nombre, g.tag)
        await _reply(interaction, f"✅ {usuario.mention} ahora ve este canal.{note}")
        await self.log("rentas", f"👥 {interaction.user.mention} agregó a {usuario.mention} al canal <#{canal.channel_id}>.")

    async def _renta_autocomplete(self, interaction: discord.Interaction, current: str):
        canal = await self.run(rdb.canal_by_channel, interaction.channel.id)
        if canal is None:
            return []
        mapas = await self.run(rdb.mapa_names)
        return [app_commands.Choice(name=f"#{r.id} · {TIPO_ES[r.tipo]} · {mapas.get(r.mapa_id, '?')} · {rdb.fmt_silver(r.precio)}"[:100], value=r.id)
                for r in await self.run(rdb.rentas_canal, canal.id)][:25]

    @renta.command(name="baja", description="(Council) Da de baja una renta de este canal")
    async def baja(self, interaction: discord.Interaction, renta: int):
        if not await self._council(interaction):
            return
        canal = await self.run(rdb.canal_by_channel, interaction.channel.id)
        r = await self.run(rdb.get_renta, renta)
        if canal is None or r is None or r.canal_id != canal.id:
            return await _reply(interaction, "❌ Esa renta no es de este canal.")
        r = await self.run(rdb.dar_baja, renta, rdb.today_utc())
        if r is None:
            return await _reply(interaction, "ℹ️ Esa renta ya estaba dada de baja.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        quedan = await self.run(rdb.rentas_canal, canal.id)
        if not quedan:
            try:
                await self.archive_channel(interaction.channel, await self.run(rdb.miembros, canal.id))
                await self.run(rdb.set_canal_archivado, canal.id, True)
            except Exception:
                traceback.print_exc()
        await self.refresh_panel(canal.id)
        deuda = sum(c.monto for c in await self.run(rdb.cargos_renta, r.id) if not c.pagado)
        await _reply(interaction, f"✅ Renta #{r.id} dada de baja." + (" Canal archivado." if not quedan else "") +
                     (f" Deuda que queda registrada al titular: **{rdb.fmt_silver(deuda)}**." if deuda else ""))
        await self.log("rentas", f"➖ Renta #{r.id} ({TIPO_ES[r.tipo]} · {r.titular_nombre}) dada de baja por {interaction.user.mention}"
                                 + (f" · deuda registrada: {rdb.fmt_silver(deuda)}" if deuda else ""))

    baja.autocomplete("renta")(_renta_autocomplete)

    # ---------- roles de guild ----------
    async def ensure_guild_role(self, guild: discord.Guild, gname: str, tag: Optional[str]):
        """Rol «[TAG] Guild» sin permisos, separado en la lista de miembros y con un color propio. Se reutiliza si ya existe."""
        g = await self.run(rdb.get_guild_role, gname)
        role = guild.get_role(g.role_id) if g is not None else None
        if role is not None:
            if tag and tag != g.tag:
                g = await self.run(rdb.set_guild_tag, g.guild_nombre, tag)
            if role.name != g.name:
                await role.edit(name=g.name)
            return role
        color = g.color if g is not None else await self.run(rdb.next_color)
        name = rdb.guild_role_name(g.guild_nombre if g else gname, tag or (g.tag if g else None))
        role = await guild.create_role(name=name, permissions=discord.Permissions.none(), colour=discord.Colour(color), hoist=True,
                                       mentionable=False, reason="Rol de guild (rentas)")
        await self.run(rdb.save_guild_role, g.guild_nombre if g else gname, tag or (g.tag if g else None), role.id, color)
        await self.log("rentas", f"🎨 Rol creado: {role.mention}")
        return role

    async def give_guild_role(self, guild: discord.Guild, user_id: int, gname: str, tag: Optional[str]) -> str:
        """Le da a la persona el rol de su guild (y le saca el de la guild anterior). Devuelve una nota para la respuesta."""
        try:
            role = await self.ensure_guild_role(guild, gname, tag)
            g = await self.run(rdb.get_guild_role, gname)
            prev_id = await self.run(rdb.set_user_guild, user_id, g.id)
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if prev_id and prev_id != g.id:
                prev = await self.run(rdb.get_guild_role_by_id, prev_id)
                old = guild.get_role(prev.role_id) if prev else None
                if old is not None and old in member.roles:
                    await member.remove_roles(old, reason="Cambio de guild (rentas)")
            if role not in member.roles:
                await member.add_roles(role, reason="Rol de guild (rentas)")
            return f" Rol {role.mention} asignado."
        except Exception as exc:
            traceback.print_exc()
            return f" ⚠️ No pude asignar el rol de guild: {exc}"

    async def _guild_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        return [app_commands.Choice(name=g.name[:100], value=g.guild_nombre[:100]) for g in await self.run(rdb.list_guild_roles)
                if cur in g.name.lower()][:25]

    @renta.command(name="alianza", description="(Council) Pone, cambia o saca el tag de alianza de una guild (renombra su rol)")
    @app_commands.describe(guild="Guild", tag="Tag de la alianza (hasta 5). Vacío = la guild queda sin alianza")
    async def alianza(self, interaction: discord.Interaction, guild: str, tag: Optional[str] = None):
        if not await self._council(interaction):
            return
        tag = (tag or "").strip().strip("[]").strip() or None
        try:
            g = await self.run(rdb.set_guild_tag, guild, tag)
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        role = interaction.guild.get_role(g.role_id)
        try:
            if role is not None:
                await role.edit(name=g.name)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await _reply(interaction, f"❌ No pude renombrar el rol: {exc}")
        await _reply(interaction, f"✅ Rol actualizado: **{g.name}**.")
        await self.log("rentas", f"🎨 {interaction.user.mention} cambió la alianza de **{g.guild_nombre}** → **{g.name}**.")

    @renta.command(name="cambiar-guild", description="(Council) Cambia la guild (y el rol) de un cliente")
    @app_commands.describe(guild="Guild nueva con tag de alianza, ej: [Timba] Smogg — o solo Smogg")
    async def cambiar_guild(self, interaction: discord.Interaction, usuario: discord.Member, guild: str):
        if not await self._council(interaction):
            return
        try:
            tag, gname = rdb.parse_guild_label(guild)
            if not gname:
                raise rdb.RentaError("Falta el nombre de la guild (ej: [Timba] Smogg).")
        except rdb.RentaError as exc:
            return await _reply(interaction, f"❌ {exc}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = await self.give_guild_role(interaction.guild, usuario.id, gname, tag)
        await _reply(interaction, f"✅ {usuario.mention} → **{rdb.guild_role_name(gname, tag)}**.{note}")
        await self.log("rentas", f"🎨 {interaction.user.mention} cambió la guild de {usuario.mention} → **{rdb.guild_role_name(gname, tag)}**.")

    alianza.autocomplete("guild")(_guild_autocomplete)

    @renta.command(name="resumen", description="(Council) Total cobrado, cobro de esta semana y del domingo pasado")
    async def resumen(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        s = await self.run(rdb.resumen, rdb.today_utc())
        emb = discord.Embed(title="📊 Resumen de rentas", color=BLUE)
        emb.add_field(name="Total cobrado (sin la semana en curso)", value=rdb.fmt_silver(s.cobrado_historico), inline=False)
        emb.add_field(name=f"Próximo domingo ({ddmm(s.proximo_domingo)}) — a cobrar", value=rdb.fmt_silver(s.proximo_estimado), inline=False)
        emb.add_field(name=f"Domingo pasado ({ddmm(s.ultimo_domingo)}) — pagado",
                      value=f"{rdb.fmt_silver(s.ultimo_pagado)} de {rdb.fmt_silver(s.ultimo_generado)} cobrado", inline=False)
        emb.add_field(name="Deuda pendiente (rentas activas)", value=rdb.fmt_silver(s.deuda_activa), inline=False)
        emb.set_footer(text="No incluye deudas de rentas dadas de baja (/renta deudas-baja).")
        await _reply(interaction, embed=emb)

    @renta.command(name="deudas-baja", description="(Council) Deudas que quedaron de rentas dadas de baja")
    async def deudas_baja(self, interaction: discord.Interaction, titular: Optional[str] = None):
        if not await self._council(interaction):
            return
        rows = await self.run(rdb.deudas_baja, titular)
        lines = [f"• {tt} **{name}** · {mapa}: {rdb.fmt_silver(total)}" for tt, name, mapa, total in rows][:60]
        await _reply(interaction, embed=discord.Embed(title="📕 Deudas de rentas dadas de baja", color=RED,
                                                      description="\n".join(lines)[:4000] or "No hay deudas registradas."))


async def setup_rentas(bot: commands.Bot, db_path: str, guild_id: int, council_role_id: int) -> RentasCog:
    cog = RentasCog(bot, db_path, guild_id, council_role_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
