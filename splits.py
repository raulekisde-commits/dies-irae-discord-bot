"""/transparencia y /editar — splits con panel, hilo y pago al cerrar (capa de Discord)."""
import asyncio
import re
import traceback
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import economy_db as eco
import splits_db as sdb
from names import resolve_names
from deposits import is_council
from guildshop import can_manage

SPLIT_CHANNEL_ID = 1558630638066602094
NO_MENTIONS = discord.AllowedMentions.none()
GOLD, GREY, GREEN = 0xF1C40F, 0x95A5A6, 0x2ECC71
MENTION_RE = re.compile(r"<@!?(\d+)>")
EDIT_TIMEOUT = 900.0
ALBION_GUILD_DEFAULT = "Smogg"
ALBION_LOOKUP = None          # lo registra bot.py: async (guild, texto, nombre_guild) -> dict con battles/players/found/not_found/ambiguous


def set_albion_lookup(fn) -> None:
    global ALBION_LOOKUP
    ALBION_LOOKUP = fn


async def _say(interaction: discord.Interaction, text: str, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def fmt(n: int) -> str:
    return eco.format_amount(n)


def pct(x: float) -> str:
    return f"{x:g}%"


# ============================================================
# Mensajes
# ============================================================
def _nm(names, uid: int) -> str:
    """Nombre para mostrar dentro de un embed (las menciones <@id> ahí se ven como número si el cliente no conoce a la persona)."""
    return (names or {}).get(uid) or f"<@{uid}>"


def people_embed(s: sdb.Split, names=None) -> discord.Embed:
    looters = {l.user_id for l in s.looters}
    lines = [f"{'🪙' if u in looters else '•'} {_nm(names, u)}" for u in s.people]
    emb = discord.Embed(title=f"👥 Personas del split ({len(s.people)})", color=GOLD)
    emb.description = "\n".join(lines)[:4000] if lines else "*Todavía no hay nadie en la lista.*"
    if looters:
        emb.set_footer(text="🪙 = looter")
    return emb


def data_embeds(s: sdb.Split, names=None) -> List[discord.Embed]:
    c = sdb.compute(s)
    out: List[discord.Embed] = []
    if s.is_ava and s.scouts:
        lines = "\n".join(f"• {_nm(names, u)} — {fmt(s.scout_pay)}" for u in s.scouts)
        out.append(discord.Embed(title=f"🔭 Scouts ({len(s.scouts)}) · pago fijo {fmt(s.scout_pay)} c/u",
                                 description=lines[:3900], color=GOLD))
    if s.looters and not s.is_ava:
        blocks = []
        for l in s.looters:
            blocks.append(
                f"**Looter:** {_nm(names, l.user_id)}\n"
                f"Total looteado: {fmt(l.looted)}\n"
                f"Reparación looter: {fmt(l.repair)}\n"
                f"Tax Looter ({pct(s.looter_pct(l))}): {fmt(c.looter_taxes[l.user_id])}")
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        out.append(discord.Embed(title="🪙 Looters", description=text, color=GOLD))
    summary = discord.Embed(title="📊 Datos del split", color=GOLD)
    if s.is_ava:
        mid = (f"**Pago por scout:** {fmt(s.scout_pay)} × {len(s.scouts)} = {fmt(c.scouts_total)}\n\n"
               f"Reparación total: {fmt(c.repair_total)}\n\n")
    else:
        mid = (f"**Tax looter (por defecto):** {pct(s.tax_looter)}\n\n"
               f"Looteado por los looters: {fmt(c.looted_total)} · Reparación total: {fmt(c.repair_total)}\n"
               f"Taxes de looters: {fmt(c.looter_tax_total)}\n\n")
    summary.description = (
        f"**Total:** {fmt(s.total)}\n"
        f"**Bolsitas:** {fmt(s.bolsitas)} (se suman al total a splitear)\n"
        f"**Reparación:** {fmt(s.repair)}\n"
        f"**Tax de venta:** {pct(s.tax_sale)} ({fmt(c.sale_tax)})\n"
        f"**Tax de guild:** {pct(s.tax_guild)} ({fmt(c.guild_tax)})\n"
        + mid +
        f"**Total a splitear:** {fmt(c.to_split)}\n"
        f"**Cantidad de personas:** {c.people}\n"
        f"**Total por persona:** {fmt(c.per_person)}")
    if c.leftover:
        pass
    note = "💡 Para agregar gente a la lista: !agregarlista @Ana @Beto @Caro"
    summary.set_footer(text=(f"Sobran {fmt(c.leftover)} que no se reparten (no alcanzan para una unidad más por persona).\n" if c.leftover else "") + note)
    out.append(summary)
    return out


STATUS_TEXT = {"open": "🟢 Abierto", "editing": "🟠 En edición: el pago anterior sigue vigente hasta confirmar", "closing": "🟡 Cerrando…", "closed": "🔒 Cerrado", "cancelled": "🚫 Cancelado"}


def panel_embed(s: sdb.Split, names=None) -> discord.Embed:
    c = sdb.compute(s)
    emb = discord.Embed(title=f"Split {s.title}", color=GOLD if s.is_open else GREY)
    extra = []
    if s.tab:
        extra.append(f"**Tab:** {s.tab}")
    if s.city:
        extra.append(f"**City:** {s.city}")
    if s.role_id:
        extra.append(f"**Rol:** <@&{s.role_id}>")
    lines = extra + [
        f"**Total:** {fmt(s.total)} · **Bolsitas:** {fmt(s.bolsitas)} · **Reparación:** {fmt(s.repair)}",
        (f"**Personas:** {c.people} · **Scouts:** {len(s.scouts)} (AVA)" if s.is_ava
         else f"**Personas:** {c.people} · **Looters:** {len(s.looters)}"),
        f"**Total a splitear:** {fmt(c.to_split)}",
        f"**Total por persona:** {fmt(c.per_person)}",
        f"Estado: {STATUS_TEXT.get('editing' if s.is_editing else s.status, s.status)}",
        f"Creado por {_nm(names, s.creator_id)} · ID #{s.id}",
    ]
    emb.description = "\n".join(lines)
    return emb


# ============================================================
# Vistas y modales
# ============================================================
class SplitPanelView(discord.ui.View):
    def __init__(self, cog: "SplitsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cerrar split", style=discord.ButtonStyle.danger, custom_id="split:close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button):
        await self.cog.on_close_pressed(interaction)

    @discord.ui.button(label="Cancelar split", style=discord.ButtonStyle.secondary, custom_id="split:cancel", emoji="🚫")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel_pressed(interaction)

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.primary, custom_id="split:edit", emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button):
        await self.cog.on_edit_pressed(interaction)


class SplitEditingPanelView(discord.ui.View):
    """Botones del panel mientras un split ya pagado está en edición."""

    def __init__(self, cog: "SplitsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Confirmar edición", style=discord.ButtonStyle.danger, custom_id="split:confirm_edit", emoji="♻️")
    async def confirm(self, interaction: discord.Interaction, button):
        await self.cog.on_close_pressed(interaction)

    @discord.ui.button(label="Cancelar edición", style=discord.ButtonStyle.secondary, custom_id="split:cancel_edit", emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button):
        await self.cog.on_cancel_edit_pressed(interaction)

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.primary, custom_id="split:edit", emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button):
        await self.cog.on_edit_pressed(interaction)


class SplitClosedPanelView(discord.ui.View):
    """Panel de un split ya cerrado: solo permite pedir una edición (reajuste)."""

    def __init__(self, cog: "SplitsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.primary, custom_id="split:edit", emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button):
        await self.cog.on_edit_pressed(interaction)


class ReopenView(discord.ui.View):
    def __init__(self, cog: "SplitsCog", split_id: int, owner_id: int):
        super().__init__(timeout=120.0)
        self.cog, self.split_id, self.owner_id = cog, split_id, owner_id
        ok = discord.ui.Button(label="Reabrir para editar", style=discord.ButtonStyle.primary, emoji="✏️")
        ok.callback = self._ok
        self.add_item(ok)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _ok(self, interaction: discord.Interaction):
        self.stop()
        await self.cog.reopen_split(interaction, self.split_id)


class SplitRequestView(discord.ui.View):
    """Botones de cada persona que se anotó después del cierre de la CTA: el organizador decide si entra al split."""

    def __init__(self, cog: "SplitsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Agregar al split", style=discord.ButtonStyle.success, custom_id="split:req_add", emoji="✅")
    async def add(self, interaction: discord.Interaction, button):
        await self.cog.on_request_decision(interaction, True)

    @discord.ui.button(label="Descartar", style=discord.ButtonStyle.secondary, custom_id="split:req_no", emoji="✖️")
    async def no(self, interaction: discord.Interaction, button):
        await self.cog.on_request_decision(interaction, False)


class ConfirmCancelView(discord.ui.View):
    def __init__(self, cog: "SplitsCog", split_id: int, owner_id: int):
        super().__init__(timeout=120.0)
        self.cog, self.split_id, self.owner_id, self.used = cog, split_id, owner_id, False
        ok = discord.ui.Button(label="Sí, cancelar split", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Volver", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _ok(self, interaction: discord.Interaction):
        if self.used:
            return await _say(interaction, "ℹ️ Ya fue procesado.")
        self.used = True
        self.stop()
        await self.cog.cancel_split(interaction, self.split_id)

    async def _no(self, interaction: discord.Interaction):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Listo, el split sigue abierto.", view=None)


class ConfirmCloseView(discord.ui.View):
    def __init__(self, cog: "SplitsCog", split_id: int, owner_id: int, label: str = "Sí, cerrar y pagar"):
        super().__init__(timeout=120.0)
        self.cog, self.split_id, self.owner_id, self.used = cog, split_id, owner_id, False
        ok = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_council(interaction.user):
            await _say(interaction, "❌ Este mensaje no es tuyo.")
            return False
        return True

    async def _ok(self, interaction: discord.Interaction):
        if self.used:
            return await _say(interaction, "ℹ️ Ya fue procesado.")
        self.used = True
        self.stop()
        await self.cog.close_split(interaction, self.split_id)

    async def _no(self, interaction: discord.Interaction):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelado. No se pagó nada.", view=None)


class _OwnedView(discord.ui.View):
    def __init__(self, cog: "SplitsCog", split_id: int, owner_id: int, timeout: float = EDIT_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog, self.split_id, self.owner_id = cog, split_id, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Este panel de edición no es tuyo. Tocá **Editar** en el panel del split.")
            return False
        return True

    def add_button(self, label: str, callback, *, style=discord.ButtonStyle.secondary, emoji: Optional[str] = None, row: Optional[int] = None):
        b = discord.ui.Button(label=label, style=style, emoji=emoji, row=row)
        b.callback = callback
        self.add_item(b)
        return b


class EditView(_OwnedView):
    """Panel de /editar (efímero): cada botón abre un formulario o un selector."""

    def __init__(self, cog, split_id, owner_id, ava: bool = False):
        super().__init__(cog, split_id, owner_id)
        self.add_button("Datos (título, tab, city)", self._info, emoji="📝", row=0)
        self.add_button("Montos y taxes", self._amounts, emoji="💰", row=0)
        self.add_button("Bolsitas", self._bolsitas, emoji="👜", row=0)
        if ava:
            self.add_button("Agregar scouts", self._add_scouts, style=discord.ButtonStyle.success, emoji="🔭", row=1)
            self.add_button("Quitar scouts", self._rm_scouts, style=discord.ButtonStyle.danger, emoji="➖", row=1)
        else:
            self.add_button("Agregar looters", self._add_looters, style=discord.ButtonStyle.success, emoji="🪙", row=1)
            self.add_button("Editar looter", self._edit_looter, emoji="✏️", row=1)
            self.add_button("Quitar looters", self._rm_looters, style=discord.ButtonStyle.danger, emoji="➖", row=1)
        self.add_button("Agregar personas", self._add_people, style=discord.ButtonStyle.success, emoji="👥", row=2)
        self.add_button("Quitar personas", self._rm_people, style=discord.ButtonStyle.danger, emoji="🧹", row=2)
        self.add_button("Agregar rol", self._add_role, emoji="🎭", row=2)
        self.add_button("Agregar killboard", self._killboard, emoji="⚔️", row=2)

    async def _split(self, interaction) -> Optional[sdb.Split]:
        s = await self.cog.run(sdb.get_split, self.split_id)
        if s is None or not s.is_open:
            await _say(interaction, "❌ Ese split ya no se puede editar.")
            return None
        return s

    async def _info(self, interaction):
        s = await self._split(interaction)
        if s:
            await interaction.response.send_modal(InfoModal(self.cog, s))

    async def _amounts(self, interaction):
        s = await self._split(interaction)
        if s:
            await interaction.response.send_modal(AmountsModal(self.cog, s))

    async def _killboard(self, interaction):
        if await self._split(interaction):
            await interaction.response.send_modal(KillboardModal(self.cog, self.split_id))

    async def _bolsitas(self, interaction):
        s = await self._split(interaction)
        if s:
            await interaction.response.send_modal(BolsitasModal(self.cog, s))

    async def _add_scouts(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí a los scouts (cobran el pago fijo; no entran a la lista general):",
                       view=UserPickView(self.cog, self.split_id, self.owner_id, "add_scouts"))

    async def _rm_scouts(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí los scouts a quitar:",
                       view=UserPickView(self.cog, self.split_id, self.owner_id, "rm_scouts"))

    async def _add_looters(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí a los looters (se suman también a la lista general):",
                       view=UserPickView(self.cog, self.split_id, self.owner_id, "add_looters"))

    async def _add_people(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí las personas a agregar a la lista:",
                       view=UserPickView(self.cog, self.split_id, self.owner_id, "add_people"))

    async def _rm_people(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí las personas a quitar de la lista (si eran looters, también dejan de serlo):",
                       view=UserPickView(self.cog, self.split_id, self.owner_id, "rm_people"))

    async def _add_role(self, interaction):
        if await self._split(interaction):
            await _say(interaction, "Elegí un rol: se agregan a la lista todas las personas que lo tienen.",
                       view=RolePickView(self.cog, self.split_id, self.owner_id))

    async def _edit_looter(self, interaction):
        s = await self._split(interaction)
        if s is None:
            return
        if not s.looters:
            return await _say(interaction, "ℹ️ Todavía no hay looters. Usá **Agregar looters**.")
        await _say(interaction, "¿A qué looter querés editarle los datos?", view=LooterPickView(self.cog, s, self.owner_id, "edit"))

    async def _rm_looters(self, interaction):
        s = await self._split(interaction)
        if s is None:
            return
        if not s.looters:
            return await _say(interaction, "ℹ️ No hay looters para quitar.")
        await _say(interaction, "¿Qué looters querés quitar? (siguen en la lista general)", view=LooterPickView(self.cog, s, self.owner_id, "remove"))


class UserPickView(_OwnedView):
    def __init__(self, cog, split_id, owner_id, action: str):
        super().__init__(cog, split_id, owner_id, timeout=300.0)
        self.action = action
        self.select = discord.ui.UserSelect(placeholder="Elegí personas…", min_values=1, max_values=10)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction):
        users = [u for u in self.select.values if not getattr(u, "bot", False)]
        if not users:
            return await _say(interaction, "❌ Elegí al menos una persona (los bots no cuentan).")
        await self.cog.apply_user_action(interaction, self.split_id, self.action, [u.id for u in users])


class RolePickView(_OwnedView):
    def __init__(self, cog, split_id, owner_id):
        super().__init__(cog, split_id, owner_id, timeout=300.0)
        self.select = discord.ui.RoleSelect(placeholder="Elegí un rol…", min_values=1, max_values=1)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction):
        await self.cog.apply_role(interaction, self.split_id, self.select.values[0])


class LooterPickView(_OwnedView):
    def __init__(self, cog, split: sdb.Split, owner_id, mode: str):
        super().__init__(cog, split.id, owner_id, timeout=300.0)
        self.mode = mode
        names = {}
        guild = cog.bot.get_guild(cog.guild_id)
        for l in split.looters[:25]:
            m = guild.get_member(l.user_id) if guild else None
            names[l.user_id] = (getattr(m, "display_name", None) or str(l.user_id))[:80]
        opts = [discord.SelectOption(label=names[l.user_id], value=str(l.user_id),
                                     description=f"Looteado {fmt(l.looted)} · Reparación {fmt(l.repair)}"[:100])
                for l in split.looters[:25]]
        self.select = discord.ui.Select(placeholder="Elegí un looter…", options=opts, min_values=1,
                                        max_values=1 if mode == "edit" else len(opts))
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction):
        ids = [int(v) for v in self.select.values]
        if self.mode == "edit":
            s = await self.cog.run(sdb.get_split, self.split_id)
            lt = next((l for l in (s.looters if s else []) if l.user_id == ids[0]), None)
            if s is None or not s.is_open or lt is None:
                return await _say(interaction, "❌ Ese looter ya no está en el split.")
            return await interaction.response.send_modal(LooterModal(self.cog, s, lt))
        await self.cog.apply_user_action(interaction, self.split_id, "rm_looters", ids)


class InfoModal(discord.ui.Modal):
    def __init__(self, cog, s: sdb.Split):
        super().__init__(title="Datos del split")
        self.cog, self.split_id = cog, s.id
        self.title_in = discord.ui.TextInput(label="Título", default=s.title, max_length=sdb.MAX_TITLE, required=True)
        self.tab_in = discord.ui.TextInput(label="Tab (vacío = ninguna)", default=s.tab or "", max_length=sdb.MAX_TEXT, required=False)
        self.city_in = discord.ui.TextInput(label="City (vacío = ninguna)", default=s.city or "", max_length=sdb.MAX_TEXT, required=False)
        for i in (self.title_in, self.tab_in, self.city_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_info(interaction, self.split_id, str(self.title_in.value), str(self.tab_in.value), str(self.city_in.value))

    async def on_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _say(interaction, "❌ No pude guardar los cambios.")


class AmountsModal(discord.ui.Modal):
    def __init__(self, cog, s: sdb.Split):
        super().__init__(title="Montos y taxes")
        self.cog, self.split_id = cog, s.id
        mk = lambda label, default: discord.ui.TextInput(label=label, default=default, max_length=25, required=True)  # noqa: E731
        self.total_in = mk("Total", fmt(s.total))
        self.repair_in = mk("Reparación", fmt(s.repair))
        self.sale_in = mk("Tax de venta (%)", f"{s.tax_sale:g}")
        self.guild_in = mk("Tax de guild (%)", f"{s.tax_guild:g}")
        self.ava = s.is_ava
        self.looter_in = mk("Pago fijo por scout", fmt(s.scout_pay)) if s.is_ava else mk("Tax looter por defecto (%)", f"{s.tax_looter:g}")
        for i in (self.total_in, self.repair_in, self.sale_in, self.guild_in, self.looter_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_amounts(interaction, self.split_id, str(self.total_in.value), str(self.repair_in.value),
                                     str(self.sale_in.value), str(self.guild_in.value), str(self.looter_in.value))

    async def on_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _say(interaction, "❌ No pude guardar los cambios.")


class KillboardModal(discord.ui.Modal):
    def __init__(self, cog, split_id: int):
        super().__init__(title="Agregar desde el killboard")
        self.cog, self.split_id = cog, split_id
        self.link_in = discord.ui.TextInput(label="Link o IDs de AlbionBB", style=discord.TextStyle.paragraph, max_length=1500,
                                            placeholder="https://albionbb.com/battles/123456789 (podés poner varias batallas)", required=True)
        self.guild_in = discord.ui.TextInput(label="Guild de Albion", default=ALBION_GUILD_DEFAULT, max_length=60, required=True)
        self.add_item(self.link_in)
        self.add_item(self.guild_in)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_killboard(interaction, self.split_id, str(self.link_in.value), str(self.guild_in.value))

    async def on_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _say(interaction, "❌ No pude leer el killboard.")


class BolsitasModal(discord.ui.Modal):
    def __init__(self, cog, s: sdb.Split):
        super().__init__(title="Bolsitas")
        self.cog, self.split_id = cog, s.id
        self.b_in = discord.ui.TextInput(label="Bolsitas (se suman al total)", default=fmt(s.bolsitas), max_length=25, required=True)
        self.add_item(self.b_in)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_bolsitas(interaction, self.split_id, str(self.b_in.value))

    async def on_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _say(interaction, "❌ No pude guardar los cambios.")


class LooterModal(discord.ui.Modal):
    def __init__(self, cog, s: sdb.Split, lt: sdb.Looter):
        super().__init__(title="Datos del looter")
        self.cog, self.split_id, self.user_id = cog, s.id, lt.user_id
        self.looted_in = discord.ui.TextInput(label="Total looteado", default=fmt(lt.looted), max_length=25, required=True)
        self.repair_in = discord.ui.TextInput(label="Reparación del looter", default=fmt(lt.repair), max_length=25, required=True)
        self.tax_in = discord.ui.TextInput(label=f"Tax looter % (vacío = {s.tax_looter:g}% por defecto)",
                                           default="" if lt.tax_pct is None else f"{lt.tax_pct:g}", max_length=10, required=False)
        for i in (self.looted_in, self.repair_in, self.tax_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_looter(interaction, self.split_id, self.user_id, str(self.looted_in.value),
                                    str(self.repair_in.value), str(self.tax_in.value))

    async def on_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        await _say(interaction, "❌ No pude guardar los cambios.")


# ============================================================
# Cog
# ============================================================
class SplitsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int, channel_id: int = SPLIT_CHANNEL_ID):
        self.bot, self.db_path, self.guild_id, self.channel_id = bot, db_path, guild_id, channel_id
        self._locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self.run(sdb.init_schema)
        self.bot.add_view(SplitPanelView(self))
        self.bot.add_view(SplitEditingPanelView(self))
        self.bot.add_view(SplitClosedPanelView(self))
        self.bot.add_view(SplitRequestView(self))
        self._closed_panels_done = False

    @commands.Cog.listener()
    async def on_ready(self):
        """Los paneles de splits cerrados antes de existir el botón «Editar» se actualizan una vez para que lo tengan."""
        if self._closed_panels_done:
            return
        self._closed_panels_done = True
        try:
            rows = await self.run(sdb.list_closed)
        except Exception as exc:
            return print(f"⚠️ Splits: no pude listar los paneles cerrados: {exc!r}")
        for split_id, channel_id, message_id in rows:
            try:
                ch = await self._get_channel(channel_id)
                msg = await ch.fetch_message(message_id) if ch is not None else None
                if msg is not None and not getattr(msg, "components", None):
                    await msg.edit(view=SplitClosedPanelView(self), allowed_mentions=NO_MENTIONS)
                    await asyncio.sleep(1)
            except Exception as exc:
                print(f"⚠️ Split #{split_id}: no pude agregar el botón Editar al panel: {exc!r}")

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def money(self, amount: int) -> str:
        e = self.economy
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

    async def names_of(self, s: sdb.Split) -> dict:
        ids = list(s.people) + [l.user_id for l in s.looters] + [s.creator_id]
        return await resolve_names(self.bot.get_guild(self.guild_id), ids)

    def _lock(self, split_id: int) -> asyncio.Lock:
        return self._locks.setdefault(split_id, asyncio.Lock())

    async def _get_channel(self, channel_id: Optional[int]):
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def _publish_log(self, op_ids: List[str]):
        e = self.economy
        for op in op_ids:
            try:
                if e is not None:
                    await e.publish_log(op)
            except Exception:
                traceback.print_exc()

    # ---------- sincronización de los mensajes ----------
    async def sync_split(self, split_id: int):
        """Refleja el estado actual en el panel, la lista y los datos (la base es la fuente de verdad)."""
        s = await self.run(sdb.get_split, split_id)
        if s is None:
            return
        view = ((SplitEditingPanelView(self) if s.is_editing else SplitPanelView(self)) if s.is_open
                else SplitClosedPanelView(self) if s.status == "closed" else None)
        try:
            ch = await self._get_channel(s.channel_id)
            if ch is not None and s.message_id:
                msg = await ch.fetch_message(s.message_id)
                await msg.edit(embed=panel_embed(s, await self.names_of(s)), view=view, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ Split #{split_id}: no pude actualizar el panel: {exc!r}")
        th = await self._get_channel(s.thread_id)
        if th is None:
            return
        names = await self.names_of(s)
        for col, builder in (("list_message_id", lambda: [people_embed(s, names)]), ("data_message_id", lambda: data_embeds(s, names))):
            mid = getattr(s, col)
            try:
                if mid:
                    msg = await th.fetch_message(mid)
                    await msg.edit(embeds=builder(), allowed_mentions=NO_MENTIONS)
                else:
                    raise discord.NotFound()
            except discord.NotFound:
                try:
                    msg = await th.send(embeds=builder(), allowed_mentions=NO_MENTIONS)
                    await self.run(sdb.set_location, split_id, **{col: msg.id})
                except Exception as exc:
                    print(f"⚠️ Split #{split_id}: no pude recrear {col}: {exc!r}")
            except Exception as exc:
                print(f"⚠️ Split #{split_id}: no pude actualizar {col}: {exc!r}")

    # ---------- /transparencia y /transparencia-ava ----------
    async def _parse_mentions(self, interaction, text: Optional[str], what: str):
        """Devuelve (ids, error). Las menciones tienen que ser de gente del servidor y no bots."""
        guild = interaction.guild
        ids: List[int] = []
        for m in MENTION_RE.finditer(text or ""):
            uid = int(m.group(1))
            member = guild.get_member(uid) if guild else None
            if member is None:
                return [], f"❌ No encuentro en el servidor a <@{uid}>."
            if getattr(member, "bot", False):
                return [], f"❌ <@{uid}> es un bot: no puede ser {what}."
            if uid not in ids:
                ids.append(uid)
        if (text or "").strip() and not ids:
            return [], f"❌ En `{what}s` mencioná a las personas con @ (ej: @Ana @Beto)."
        return ids, None

    async def _create_command(self, interaction, *, ava: bool, titulo, tab, city, rol, total, reparacion, bolsitas, extra, pago_scout=None):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council (o un administrador) puede crear splits.")
        try:
            total_v = sdb.parse_money(total, field_name="Total")
            repair_v = sdb.parse_money(reparacion, field_name="Reparación")
            bols_v = sdb.parse_money(bolsitas, field_name="Bolsitas")
            pay_v = sdb.parse_money(pago_scout, field_name="Pago por scout") if ava else 0
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        ids, err = await self._parse_mentions(interaction, extra, "scout" if ava else "looter")
        if err:
            return await _say(interaction, err)
        people: List[int] = []
        if rol is not None:
            people = [m.id for m in getattr(rol, "members", []) if not getattr(m, "bot", False)]
        await interaction.response.defer(ephemeral=True)
        kw = dict(scouts=ids, scout_pay=pay_v, kind="ava") if ava else dict(looters=ids)
        split, msg, error = await self._publish(
            creator_id=interaction.user.id, title=titulo, tab=tab, city=city, role_id=rol.id if rol else None,
            total=total_v, repair=repair_v, bolsitas=bols_v, people=people, **kw)
        if error:
            return await interaction.followup.send(error, ephemeral=True)
        what = "los montos, los scouts y las personas" if ava else "los datos de cada looter, los montos y las personas"
        await interaction.followup.send(
            f"✅ Split **{split.title}** creado: {getattr(msg, 'jump_url', '')}\n"
            f"Con el botón **Editar** del panel cargás {what}.", ephemeral=True)

    @app_commands.command(name="transparencia", description="Crea un split: panel + hilo con la lista de personas y el cálculo")
    @app_commands.describe(
        titulo="Título del split",
        tab="Tab (opcional)", city="City (opcional)",
        rol="Rol cuyos miembros entran al split (opcional)",
        total="Total del split, en silver (opcional)", reparacion="Reparación total (opcional)",
        bolsitas="Bolsitas: se suman al total (opcional, por defecto 0)",
        looters="Mencioná a los looters, por ejemplo @Ana @Beto (opcional)")
    @app_commands.guild_only()
    async def transparencia(self, interaction: discord.Interaction, titulo: str, tab: Optional[str] = None, city: Optional[str] = None,
                            rol: Optional[discord.Role] = None, total: Optional[str] = None, reparacion: Optional[str] = None,
                            bolsitas: Optional[str] = None, looters: Optional[str] = None):
        await self._create_command(interaction, ava=False, titulo=titulo, tab=tab, city=city, rol=rol, total=total,
                                   reparacion=reparacion, bolsitas=bolsitas, extra=looters)

    @app_commands.command(name="transparencia-ava", description="Como /transparencia, pero sin looters: los scouts cobran un monto fijo")
    @app_commands.describe(
        titulo="Título del split",
        tab="Tab (opcional)", city="City (opcional)",
        rol="Rol cuyos miembros entran al split (opcional)",
        total="Total del split, en silver (opcional)", reparacion="Reparación total (opcional)",
        bolsitas="Bolsitas: se suman al total (opcional, por defecto 0)",
        scouts="Mencioná a los scouts, por ejemplo @Ana @Beto (opcional)",
        pago_scout="Monto fijo que cobra cada scout, se descuenta del total a repartir (opcional)")
    @app_commands.guild_only()
    async def transparencia_ava(self, interaction: discord.Interaction, titulo: str, tab: Optional[str] = None, city: Optional[str] = None,
                                rol: Optional[discord.Role] = None, total: Optional[str] = None, reparacion: Optional[str] = None,
                                bolsitas: Optional[str] = None, scouts: Optional[str] = None, pago_scout: Optional[str] = None):
        await self._create_command(interaction, ava=True, titulo=titulo, tab=tab, city=city, rol=rol, total=total,
                                   reparacion=reparacion, bolsitas=bolsitas, extra=scouts, pago_scout=pago_scout)

    async def _publish(self, *, creator_id: int, title: str, tab=None, city=None, role_id=None, total: int = 0, repair: int = 0,
                       people=(), looters=(), source_cta_id: Optional[int] = None,
                       kind: str = "normal", bolsitas: int = 0, scouts=(), scout_pay: int = 0):
        """Crea el split, publica el panel en el canal de splits y arma el hilo. Devuelve (split, mensaje, error)."""
        channel = await self._get_channel(self.channel_id)
        if channel is None:
            return None, None, f"❌ No encuentro el canal de splits (<#{self.channel_id}>). Revisá que el bot lo vea."
        try:
            sid = await self.run(sdb.create_split, guild_id=self.guild_id, creator_id=creator_id, title=title, tab=tab, city=city,
                                 role_id=role_id, total=total, repair=repair, people=people, looters=looters, source_cta_id=source_cta_id,
                                 kind=kind, bolsitas=bolsitas, scouts=scouts, scout_pay=scout_pay)
        except sdb.SplitError as exc:
            return None, None, f"❌ {exc}"
        s = await self.run(sdb.get_split, sid)
        try:
            msg = await channel.send(embed=panel_embed(s, await self.names_of(s)), view=SplitPanelView(self), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            await self.run(sdb.abort_unpublished, sid)
            return None, None, "❌ No pude publicar el panel (revisá los permisos del bot en el canal de splits)."
        await self.run(sdb.set_location, sid, channel_id=channel.id, message_id=msg.id)
        try:
            try:
                thread = await msg.create_thread(name=f"Split {s.title}"[:100], auto_archive_duration=10080)
            except discord.HTTPException:
                thread = await msg.create_thread(name=f"Split {s.title}"[:100], auto_archive_duration=1440)
            names = await self.names_of(s)
            lst = await thread.send(embeds=[people_embed(s, names)], allowed_mentions=NO_MENTIONS)
            dat = await thread.send(embeds=data_embeds(s, names), allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            try:
                await msg.delete()
            except Exception:
                pass
            await self.run(sdb.abort_unpublished, sid)
            return None, None, "❌ No pude crear el hilo (revisá el permiso *Crear hilos públicos* del bot). No se creó el split."
        await self.run(sdb.set_location, sid, thread_id=thread.id, list_message_id=lst.id, data_message_id=dat.id)
        return await self.run(sdb.get_split, sid), msg, None

    # ---------- split generado desde una CTA con track ----------
    async def create_from_cta(self, *, cta_id: int, title: str, tab: Optional[str], city: Optional[str],
                              user_ids: List[int], creator_id: int):
        """Al cerrar una CTA con track: arma el split con los anotados (sin crear ningún rol).
        Devuelve (split, mensaje, error); si ya existía uno para esa CTA, lo devuelve sin crear otro."""
        async with self._lock(-cta_id):
            old = await self.run(sdb.find_by_cta, cta_id)
            if old is not None:
                return old, None, None
            return await self._publish(creator_id=creator_id, title=title, tab=tab, city=city,
                                       people=[u for u in dict.fromkeys(user_ids)][:sdb.MAX_PEOPLE], source_cta_id=cta_id)

    async def register_late_request(self, *, cta_id: int, user_id: int, slot_no: Optional[int]) -> Optional[str]:
        """Alguien se anotó en la CTA ya cerrada: queda una solicitud en el hilo del split para que el organizador decida.
        Devuelve None si esa CTA no tiene split; si no, el estado: new · listed · pending · accepted · rejected · closed."""
        s = await self.run(sdb.find_by_cta, cta_id)
        if s is None:
            return None
        state, count = await self.run(sdb.add_request, s.id, user_id, slot_no)
        if state != "new":
            return state
        th = await self._get_channel(s.thread_id)
        if th is None:
            return state
        try:
            if count == 1:
                await th.send("🕐 **Anotados después del cierre de la CTA.** Decidí con los botones si cada persona se suma al split.",
                              allowed_mentions=NO_MENTIONS)
            pos = f" (quería la posición {slot_no})" if slot_no else ""
            m = await th.send(f"🙋 <@{user_id}> se anotó después del cierre{pos}.", view=SplitRequestView(self), allowed_mentions=NO_MENTIONS)
            await self.run(sdb.set_request_message, s.id, user_id, m.id)
        except Exception as exc:
            print(f"⚠️ Split #{s.id}: no pude publicar la solicitud de {user_id}: {exc!r}")
        return state

    async def on_request_decision(self, interaction: discord.Interaction, accept: bool):
        msg = getattr(interaction, "message", None)
        req = await self.run(sdb.get_request_by_message, msg.id) if msg is not None else None
        if req is None:
            return await _say(interaction, "❌ No encuentro esta solicitud.")
        s = await self.run(sdb.get_split, req.split_id)
        if s is None or not (can_manage(interaction.user) or interaction.user.id == s.creator_id):
            return await _say(interaction, "🔒 Solo el Council, un administrador o quien creó el split puede decidir.")
        try:
            res = await self.run(sdb.decide_request, req.split_id, req.user_id, accept, interaction.user.id)
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        if res == "closed":
            return await _say(interaction, "❌ El split ya está cerrado.")
        if res in ("decided", "missing"):
            return await _say(interaction, "ℹ️ Esa solicitud ya estaba decidida.")
        verdict = "✅ Agregado al split" if res == "accepted" else "✖️ Descartado"
        try:
            await interaction.response.edit_message(content=f"{verdict} por <@{interaction.user.id}>: <@{req.user_id}>", view=None,
                                                    allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        if res == "accepted":
            await self.sync_split(req.split_id)

    # ---------- /editar ----------
    async def _target(self, interaction, split_id: Optional[int]) -> Optional[sdb.Split]:
        if split_id is not None:
            s = await self.run(sdb.get_split, split_id)
        else:
            s = await self.run(sdb.find_by_thread, interaction.channel.id if interaction.channel else 0)
        if s is None:
            await _say(interaction, "❌ No encuentro el split. Usá `/editar` dentro de su hilo o indicá el `id`.")
        return s

    @app_commands.command(name="editar", description="Edita un split: datos, montos, taxes, looters y personas")
    @app_commands.describe(id="Número del split (no hace falta si lo usás dentro de su hilo)")
    @app_commands.guild_only()
    async def editar(self, interaction: discord.Interaction, id: Optional[int] = None):
        s = await self._target(interaction, id)
        if s is not None:
            await self._open_edit(interaction, s)

    async def on_edit_pressed(self, interaction: discord.Interaction):
        """Botón «Editar» del panel del split."""
        msg = getattr(interaction, "message", None)
        s = await self.run(sdb.find_by_message, msg.id) if msg is not None else None
        if s is None:
            return await _say(interaction, "❌ No encuentro el split de este panel.")
        await self._open_edit(interaction, s)

    async def _open_edit(self, interaction: discord.Interaction, s: sdb.Split):
        if not (can_manage(interaction.user) or interaction.user.id == s.creator_id):
            return await _say(interaction, "🔒 Solo el Council, un administrador o quien creó el split puede editarlo.")
        if s.status == "closed":
            if not is_council(interaction.user):
                return await _say(interaction, "🔒 Solo el Council puede reabrir un split ya pagado para editarlo.")
            return await _say(interaction,
                              f"**Split {s.title}** (#{s.id}) ya está cerrado y pagado.\n"
                              f"Si lo reabrís podés cambiar lo que quieras; al **confirmar la edición** se resta a cada persona lo que "
                              f"cobró antes y se reparte de nuevo con los datos nuevos (quien ya retiró esa plata puede quedar en negativo). "
                              f"Hasta confirmar no se mueve nada, y podés cancelar la edición.",
                              view=ReopenView(self, s.id, interaction.user.id))
        if not s.is_open:
            return await _say(interaction, "❌ Ese split está cancelado o cerrándose: no se puede editar.")
        await _say(interaction, f"✏️ Editando **Split {s.title}** (#{s.id}). Los cambios se reflejan al instante en el hilo.",
                   view=EditView(self, s.id, interaction.user.id, ava=s.is_ava))

    # ---------- acciones de edición ----------
    async def _guard(self, interaction, split_id: int) -> Optional[sdb.Split]:
        s = await self.run(sdb.get_split, split_id)
        if s is None or not s.is_open:
            await _say(interaction, "❌ Ese split ya no se puede editar.")
            return None
        if not (can_manage(interaction.user) or interaction.user.id == s.creator_id):
            await _say(interaction, "🔒 No tenés permiso para editar este split.")
            return None
        return s

    async def _done(self, interaction, split_id: int, text: str):
        await self.sync_split(split_id)
        await _say(interaction, text)

    async def apply_info(self, interaction, split_id, title, tab, city):
        if not await self._guard(interaction, split_id):
            return
        try:
            await self.run(sdb.update_info, split_id, title=title, tab=tab, city=city, clear_tab=not tab.strip(), clear_city=not city.strip())
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, "✅ Datos actualizados.")

    async def apply_amounts(self, interaction, split_id, total, repair, sale, guild, looter):
        s = await self._guard(interaction, split_id)
        if not s:
            return
        try:
            vals = dict(total=sdb.parse_money(total, field_name="Total"), repair=sdb.parse_money(repair, field_name="Reparación"),
                        tax_sale=sdb.parse_pct(sale, field_name="Tax de venta"), tax_guild=sdb.parse_pct(guild, field_name="Tax de guild"))
            if s.is_ava:
                vals["scout_pay"] = sdb.parse_money(looter, field_name="Pago por scout")
            else:
                vals["tax_looter"] = sdb.parse_pct(looter, field_name="Tax looter")
            await self.run(sdb.update_amounts, split_id, **vals)
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, "✅ Montos actualizados.")

    async def apply_bolsitas(self, interaction, split_id, bolsitas):
        if not await self._guard(interaction, split_id):
            return
        try:
            await self.run(sdb.update_amounts, split_id, bolsitas=sdb.parse_money(bolsitas, field_name="Bolsitas"))
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, "✅ Bolsitas actualizadas.")

    async def apply_killboard(self, interaction, split_id, text, guild_name):
        """Lista a la gente de la guild que participó en las batallas (igual que /rolalbion) y la suma a la lista del split."""
        s = await self._guard(interaction, split_id)
        if not s:
            return
        if ALBION_LOOKUP is None:
            return await _say(interaction, "❌ La búsqueda en AlbionBB no está disponible en este momento.")
        guild_name = (guild_name or "").strip() or ALBION_GUILD_DEFAULT
        await interaction.response.defer(ephemeral=True)
        try:
            res = await asyncio.wait_for(ALBION_LOOKUP(interaction.guild, text, guild_name), timeout=90)
        except asyncio.TimeoutError:
            return await _say(interaction, "❌ AlbionBB tardó demasiado en responder. Probá de nuevo en un rato.")
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude obtener los participantes de AlbionBB. Revisá el link y que AlbionBB esté disponible.")
        if res.get("error"):
            return await _say(interaction, f"❌ {res['error']}")
        found = res["found"]
        added = 0
        if found:
            try:
                added = await self.run(sdb.add_people, split_id, [m.id for _n, m in found])
            except sdb.SplitError as exc:
                return await _say(interaction, f"❌ {exc}")
            await self.sync_split(split_id)
        lines = [f"⚔️ **Killboard** · guild `{guild_name}` · {res['battles']} batalla(s) · {len(res['players'])} jugador(es) de la guild",
                 f"✅ Agregados a la lista: **{added}**" + (f" ({len(found) - added} ya estaban)" if len(found) - added else "")]
        if res["not_found"]:
            lines.append(f"❓ Sin cuenta de Discord que coincida ({len(res['not_found'])}): " + ", ".join(f"`{n}`" for n in res["not_found"]))
            lines.append("   Agregalos a mano con `!agregarlista` o el botón **Agregar personas**.")
        if res["ambiguous"]:
            lines.append(f"⚠️ Ambiguos, no se agregaron ({len(res['ambiguous'])}): " +
                         "; ".join(f"`{n}` → " + ", ".join(m.display_name for m in ms) for n, ms in res["ambiguous"]))
        text_out = "\n".join(lines)
        for i in range(0, len(text_out), 1900):
            await _say(interaction, text_out[i:i + 1900])

    async def apply_looter(self, interaction, split_id, user_id, looted, repair, tax):
        if not await self._guard(interaction, split_id):
            return
        try:
            looted_v = sdb.parse_money(looted, field_name="Total looteado")
            repair_v = sdb.parse_money(repair, field_name="Reparación")
            if (tax or "").strip():
                await self.run(sdb.set_looter, split_id, user_id, looted=looted_v, repair=repair_v, tax_pct=sdb.parse_pct(tax, field_name="Tax looter"))
            else:
                await self.run(sdb.set_looter, split_id, user_id, looted=looted_v, repair=repair_v, reset_tax=True)
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, f"✅ Datos de <@{user_id}> actualizados.")

    async def apply_user_action(self, interaction, split_id, action: str, ids: List[int]):
        if not await self._guard(interaction, split_id):
            return
        try:
            if action == "add_looters":
                n = await self.run(sdb.add_looters, split_id, ids)
                text = f"✅ {n} looter(s) agregado(s). Cargales los montos con **Editar looter**." if n else "ℹ️ Ya eran looters."
            elif action == "add_scouts":
                n = await self.run(sdb.add_scouts, split_id, ids)
                text = f"✅ {n} scout(s) agregado(s)." if n else "ℹ️ Ya eran scouts."
            elif action == "rm_scouts":
                n = await self.run(sdb.remove_scouts, split_id, ids)
                text = f"✅ {n} scout(s) quitado(s)." if n else "ℹ️ No eran scouts."
            elif action == "add_people":
                n = await self.run(sdb.add_people, split_id, ids)
                text = f"✅ {n} persona(s) agregada(s) a la lista." if n else "ℹ️ Ya estaban en la lista."
            elif action == "rm_people":
                n, _lost = await self.run(sdb.remove_people, split_id, ids)
                text = f"✅ {n} persona(s) quitada(s) de la lista." if n else "ℹ️ Esas personas no estaban en la lista."
            else:
                n = await self.run(sdb.remove_looters, split_id, ids)
                text = f"✅ {n} looter(s) quitado(s) (siguen en la lista general)." if n else "ℹ️ No eran looters."
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, text)

    async def apply_role(self, interaction, split_id, role):
        if not await self._guard(interaction, split_id):
            return
        members = [m.id for m in getattr(role, "members", []) if not getattr(m, "bot", False)]
        if not members:
            return await _say(interaction, "❌ Ese rol no tiene miembros (o el bot no los puede ver).")
        try:
            n = await self.run(sdb.add_people, split_id, members, role_id=role.id)
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, f"✅ {n} persona(s) nueva(s) agregada(s) desde {getattr(role, 'mention', 'el rol')}.")

    # ---------- cerrar el split ----------
    async def on_close_pressed(self, interaction: discord.Interaction):
        if not is_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede cerrar un split.")
        msg = getattr(interaction, "message", None)
        s = await self.run(sdb.find_by_message, msg.id) if msg is not None else None
        if s is None:
            return await _say(interaction, "❌ No encuentro el split de este panel.")
        if s.status in ("closed", "cancelled"):
            return await _say(interaction, "ℹ️ Este split ya está cerrado.")
        c = sdb.compute(s)
        if not s.people:
            return await _say(interaction, "❌ El split no tiene personas en la lista. Agregalas con el botón **Editar** del panel.")
        if s.is_editing:
            old = await self.run(sdb.get_payouts, s.id, s.pay_round)
            return await _say(interaction,
                              f"¿Confirmar la edición de **Split {s.title}**?\n"
                              f"• Se **resta** lo pagado antes a {len(old)} persona(s) ({self.money(sum(old.values()))} en total). "
                              f"Si alguien ya retiró esa plata, su balance puede quedar en negativo.\n"
                              f"• Se **vuelve a repartir** con los datos nuevos: {c.people} persona(s) × {self.money(c.per_person)} "
                              f"(+ taxes de looters {self.money(c.looter_tax_total)}, scouts {self.money(c.scouts_total)}).\n"
                              f"Esto **mueve Wuju Coins**.",
                              view=ConfirmCloseView(self, s.id, interaction.user.id, label="Sí, confirmar edición"))
        await _say(interaction,
                   f"¿Cerrar **Split {s.title}** y pagar?\n"
                   f"• {c.people} persona(s) × {self.money(c.per_person)}\n"
                   f"• Tax looters: {self.money(c.looter_tax_total)} en total\n"
                   f"Esto **mueve Wuju Coins** y no se puede deshacer.",
                   view=ConfirmCloseView(self, s.id, interaction.user.id))

    async def close_split(self, interaction: discord.Interaction, split_id: int):
        if not is_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede cerrar un split.")
        await interaction.response.defer(ephemeral=True)
        async with self._lock(split_id):
            state, s = await self.run(sdb.begin_close, split_id, interaction.user.id)
            if state in ("missing", "cancelled"):
                return await interaction.followup.send("❌ No encuentro ese split.", ephemeral=True)
            if state == "closed":
                return await interaction.followup.send("ℹ️ Ese split ya estaba cerrado.", ephemeral=True)
            old_pay = await self.run(sdb.get_payouts, split_id, s.pay_round) if s.pay_round > 0 else {}
            try:
                calc, paid, ops = await asyncio.to_thread(sdb.pay_out, self.db_path, s, interaction.user.id)
            except Exception:
                traceback.print_exc()
                return await interaction.followup.send(
                    "❌ Falló el pago. No se perdió nada: volvé a tocar **Cerrar split** y se reintenta sin duplicar pagos.", ephemeral=True)
            await self.run(sdb.finish_close, split_id, paid)
        await self._publish_log(ops)
        s = await self.run(sdb.get_split, split_id)
        await self.sync_split(split_id)
        if old_pay:
            await self._post_adjustment(s, old_pay, paid, interaction.user.id)
            return await interaction.followup.send("✅ Edición confirmada: se restó lo anterior y se repartió de nuevo.", ephemeral=True)
        await self._post_result(s, calc, paid, interaction.user.id)
        await interaction.followup.send("✅ Split cerrado y pagado. La lista quedó en el hilo.", ephemeral=True)

    # ---------- reajuste: reabrir un split pagado ----------
    async def reopen_split(self, interaction: discord.Interaction, split_id: int):
        if not is_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede reabrir un split ya pagado.")
        async with self._lock(split_id):
            res = await self.run(sdb.reopen, split_id, interaction.user.id)
        if res == "missing" or res == "not_closed":
            return await interaction.response.edit_message(content="❌ Ese split no se puede reabrir (¿cancelado o ya en edición?).", view=None)
        s = await self.run(sdb.get_split, split_id)
        await self.sync_split(split_id)
        await interaction.response.edit_message(
            content=f"✏️ **Split {s.title}** reabierto. Tocá **Editar** en el panel para cambiar lo que quieras; los cambios se ven en el hilo pero "
                    f"**no mueven plata** hasta que toques **Confirmar edición** en el panel.", view=None)
        th = await self._get_channel(s.thread_id)
        if th is not None and res == "reopened":
            try:
                await th.send(f"✏️ **Split {s.title}** reabierto para edición por <@{interaction.user.id}>. "
                              f"Lo pagado antes sigue vigente hasta que se confirme la edición.", allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Split #{split_id}: no pude avisar la reapertura: {exc!r}")

    async def on_cancel_edit_pressed(self, interaction: discord.Interaction):
        msg = getattr(interaction, "message", None)
        s = await self.run(sdb.find_by_message, msg.id) if msg is not None else None
        if s is None or not s.is_editing:
            return await _say(interaction, "ℹ️ Este split no está en edición.")
        if not is_council(interaction.user):
            return await _say(interaction, "🔒 Solo el Council puede cancelar la edición.")
        async with self._lock(s.id):
            res = await self.run(sdb.cancel_edit, s.id)
        if res != "restored":
            return await _say(interaction, "ℹ️ Este split no está en edición.")
        await self.sync_split(s.id)
        await _say(interaction, "↩️ Edición cancelada: el split volvió a como estaba y no se movió plata.")
        th = await self._get_channel(s.thread_id)
        if th is not None:
            try:
                await th.send(f"↩️ Edición de **Split {s.title}** cancelada por <@{interaction.user.id}>. Quedó como estaba.", allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Split #{s.id}: no pude avisar la cancelación de la edición: {exc!r}")

    async def _post_adjustment(self, s: sdb.Split, old: Dict[int, int], new: Dict[int, int], actor_id: int):
        th = await self._get_channel(s.thread_id)
        if th is None:
            return
        head = (f"♻️ **Split {s.title} reajustado** por <@{actor_id}>\n"
                f"Se restó lo pagado antes y se repartió de nuevo con los datos editados.\n**Detalle por persona:**")
        lines = []
        for u in dict.fromkeys(list(old) + list(new)):
            o, n = old.get(u, 0), new.get(u, 0)
            diff = n - o
            lines.append(f"• <@{u}> → −{self.money(o)} +{self.money(n)} (neto {'+' if diff >= 0 else '−'}{self.money(abs(diff))})")
        chunks, cur = [], head
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1900:
                chunks.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + ln
        chunks.append(cur)
        for ch in chunks:
            try:
                await th.send(ch, allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Split #{s.id}: no pude escribir el reajuste en el hilo: {exc!r}")
                break

    # ---------- cancelar el split (sin pagar nada) ----------
    async def on_cancel_pressed(self, interaction: discord.Interaction):
        msg = getattr(interaction, "message", None)
        s = await self.run(sdb.find_by_message, msg.id) if msg is not None else None
        if s is None:
            return await _say(interaction, "❌ No encuentro el split de este panel.")
        if not (is_council(interaction.user) or can_manage(interaction.user) or interaction.user.id == s.creator_id):
            return await _say(interaction, "🔒 Solo el Council, un administrador o quien creó el split puede cancelarlo.")
        if s.status in ("closed", "cancelled"):
            return await _say(interaction, "ℹ️ Este split ya está cerrado.")
        await _say(interaction,
                   f"¿Cancelar **Split {s.title}**? Se cierra **sin pagar nada** y no se puede reabrir.",
                   view=ConfirmCancelView(self, s.id, interaction.user.id))

    async def cancel_split(self, interaction: discord.Interaction, split_id: int):
        s = await self.run(sdb.get_split, split_id)
        if s is None or not (is_council(interaction.user) or can_manage(interaction.user) or interaction.user.id == s.creator_id):
            return await _say(interaction, "🔒 No tenés permiso para cancelar este split.")
        async with self._lock(split_id):
            res = await self.run(sdb.cancel_split, split_id, interaction.user.id)
        if res == "closing":
            return await interaction.response.edit_message(content="❌ Este split ya empezó a pagarse: no se puede cancelar.", view=None)
        if res in ("closed", "cancelled_before", "missing"):
            return await interaction.response.edit_message(content="ℹ️ Este split ya estaba cerrado.", view=None)
        await interaction.response.edit_message(content="🚫 Split cancelado. No se pagó nada.", view=None)
        await self.sync_split(split_id)
        th = await self._get_channel(s.thread_id)
        if th is not None:
            try:
                await th.send(f"🚫 **Split {s.title} cancelado** por <@{interaction.user.id}>. No se acreditó nada a nadie.", allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Split #{split_id}: no pude avisar la cancelación en el hilo: {exc!r}")

    # ---------- !agregarlista ----------
    async def _add_to_list(self, actor, channel_id: Optional[int], explicit_id: Optional[int], members) -> str:
        """Lógica común de !agregarlista y /agregarlista. Devuelve el texto de respuesta."""
        if explicit_id is not None:
            s = await self.run(sdb.get_split, explicit_id)
        else:
            s = await self.run(sdb.find_by_thread, channel_id or 0)
        if s is None:
            return "❌ Usalo dentro del hilo de un split (o poné el número del split antes de las menciones: `!agregarlista 3 @Ana @Beto`)."
        if not (can_manage(actor) or actor.id == s.creator_id):
            return "🔒 Solo el Council, un administrador o quien creó el split puede agregar gente."
        if not s.is_open:
            return "❌ Ese split ya está cerrado: no se puede modificar."
        ids = [m.id for m in members if not getattr(m, "bot", False)]
        if not ids:
            return "❌ Mencioná a las personas que querés agregar. Ej: `!agregarlista @Ana @Beto @Caro`"
        try:
            n = await self.run(sdb.add_people, s.id, ids)
        except sdb.SplitError as exc:
            return f"❌ {exc}"
        await self.sync_split(s.id)
        if n == 0:
            return "ℹ️ Todas esas personas ya estaban en la lista."
        ya = len(set(ids)) - n
        return f"✅ Agregué {n} persona(s) a la lista de **Split {s.title}**." + (f" ({ya} ya estaba(n).)" if ya else "")

    @commands.command(name="agregarlista")
    async def agregarlista(self, ctx: commands.Context, *, texto: str = ""):
        if ctx.guild is None or getattr(ctx.author, "bot", False):
            return
        first = (texto or "").split()[:1]
        explicit = int(first[0]) if first and first[0].isdigit() else None
        members = [m for m in getattr(ctx.message, "mentions", [])]
        reply = await self._add_to_list(ctx.author, getattr(ctx.channel, "id", None), explicit, members)
        await ctx.reply(reply, mention_author=False, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="agregarlista", description="Agrega gente a la lista de un split (usalo dentro de su hilo)")
    @app_commands.describe(personas="Mencioná a todas las personas que quieras, por ejemplo @Ana @Beto",
                           id="Número del split (no hace falta dentro de su hilo)")
    @app_commands.guild_only()
    async def agregarlista_slash(self, interaction: discord.Interaction, personas: str, id: Optional[int] = None):
        members = []
        for m in MENTION_RE.finditer(personas or ""):
            member = interaction.guild.get_member(int(m.group(1))) if interaction.guild else None
            if member is not None and member not in members:
                members.append(member)
        text = await self._add_to_list(interaction.user, interaction.channel.id if interaction.channel else None, id, members)
        await _say(interaction, text) if text[:1] in "❌🔒ℹ" else await interaction.response.send_message(text, allowed_mentions=NO_MENTIONS)

    async def _post_result(self, s: sdb.Split, calc: sdb.Calc, paid: Dict[int, int], actor_id: int):
        th = await self._get_channel(s.thread_id)
        if th is None:
            return
        looters = {l.user_id for l in s.looters}
        head = (f"🔒 **Split {s.title} cerrado** por <@{actor_id}>\n"
                f"Total a splitear: {self.money(calc.to_split)} · Personas: {calc.people} · Por persona: {self.money(calc.per_person)}\n"
                f"**Balance cargado a cada persona:**")
        lines = []
        for u in s.people:
            amount = paid.get(u, 0)
            extra = ""
            if u in looters and calc.looter_taxes.get(u):
                extra = f" (parte {fmt(calc.per_person)} + tax looter {fmt(calc.looter_taxes[u])})"
            if s.is_ava and u in s.scouts:
                extra = f" (parte {fmt(calc.per_person)} + pago de scout {fmt(s.scout_pay)})"
            lines.append(f"• <@{u}> → +{self.money(amount)}{extra}")
        for u in ([x for x in s.scouts if x not in s.people] if s.is_ava else []):
            if paid.get(u):
                lines.append(f"• <@{u}> → +{self.money(paid[u])} (pago de scout {fmt(s.scout_pay)})")
        for u in looters - set(s.people):                      # looter sacado de la lista general: igual cobra su tax
            if paid.get(u):
                lines.append(f"• <@{u}> → +{self.money(paid[u])} (tax looter)")
        if not lines:
            lines.append("*No había nada para repartir.*")
        foot = f"\nSobraron {fmt(calc.leftover)} sin repartir." if calc.leftover else ""
        chunks, cur = [], head
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1900:
                chunks.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + ln
        chunks.append(cur + foot)
        for ch in chunks:
            try:
                await th.send(ch, allowed_mentions=NO_MENTIONS)
            except Exception as exc:
                print(f"⚠️ Split #{s.id}: no pude escribir el resultado en el hilo: {exc!r}")
                break


async def setup_splits(bot: commands.Bot, db_path: str, guild_id: int) -> SplitsCog:
    cog = SplitsCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
