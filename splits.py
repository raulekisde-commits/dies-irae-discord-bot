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
from deposits import is_council
from guildshop import can_manage

SPLIT_CHANNEL_ID = 1558630638066602094
NO_MENTIONS = discord.AllowedMentions.none()
GOLD, GREY, GREEN = 0xF1C40F, 0x95A5A6, 0x2ECC71
MENTION_RE = re.compile(r"<@!?(\d+)>")
EDIT_TIMEOUT = 900.0


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
def people_embed(s: sdb.Split) -> discord.Embed:
    looters = {l.user_id for l in s.looters}
    lines = [f"{'🪙' if u in looters else '•'} <@{u}>" for u in s.people]
    emb = discord.Embed(title=f"👥 Personas del split ({len(s.people)})", color=GOLD)
    emb.description = "\n".join(lines)[:4000] if lines else "*Todavía no hay nadie en la lista.*"
    if looters:
        emb.set_footer(text="🪙 = looter")
    return emb


def data_embeds(s: sdb.Split) -> List[discord.Embed]:
    c = sdb.compute(s)
    out: List[discord.Embed] = []
    if s.looters:
        blocks = []
        for l in s.looters:
            blocks.append(
                f"**Looter:** <@{l.user_id}>\n"
                f"Total looteado: {fmt(l.looted)}\n"
                f"Reparación looter: {fmt(l.repair)}\n"
                f"Tax Looter ({pct(s.looter_pct(l))}): {fmt(c.looter_taxes[l.user_id])}")
        text = "\n\n".join(blocks)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        out.append(discord.Embed(title="🪙 Looters", description=text, color=GOLD))
    summary = discord.Embed(title="📊 Datos del split", color=GOLD)
    summary.description = (
        f"**Total:** {fmt(s.total)}\n"
        f"**Reparación:** {fmt(s.repair)}\n"
        f"**Tax de venta:** {pct(s.tax_sale)} ({fmt(c.sale_tax)})\n"
        f"**Tax de guild:** {pct(s.tax_guild)} ({fmt(c.guild_tax)})\n"
        f"**Tax looter (por defecto):** {pct(s.tax_looter)}\n\n"
        f"Looteado por los looters: {fmt(c.looted_total)} · Reparación total: {fmt(c.repair_total)}\n"
        f"Taxes de looters: {fmt(c.looter_tax_total)}\n\n"
        f"**Total a splitear:** {fmt(c.to_split)}\n"
        f"**Cantidad de personas:** {c.people}\n"
        f"**Total por persona:** {fmt(c.per_person)}")
    if c.leftover:
        summary.set_footer(text=f"Sobran {fmt(c.leftover)} que no se reparten (no alcanzan para una unidad más por persona).")
    out.append(summary)
    return out


STATUS_TEXT = {"open": "🟢 Abierto", "closing": "🟡 Cerrando…", "closed": "🔒 Cerrado", "cancelled": "🚫 Cancelado"}


def panel_embed(s: sdb.Split) -> discord.Embed:
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
        f"**Total:** {fmt(s.total)} · **Reparación:** {fmt(s.repair)}",
        f"**Personas:** {c.people} · **Looters:** {len(s.looters)}",
        f"**Total a splitear:** {fmt(c.to_split)}",
        f"**Total por persona:** {fmt(c.per_person)}",
        f"Estado: {STATUS_TEXT.get(s.status, s.status)}",
        f"Creado por <@{s.creator_id}> · ID #{s.id}",
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
    def __init__(self, cog: "SplitsCog", split_id: int, owner_id: int):
        super().__init__(timeout=120.0)
        self.cog, self.split_id, self.owner_id, self.used = cog, split_id, owner_id, False
        ok = discord.ui.Button(label="Sí, cerrar y pagar", style=discord.ButtonStyle.danger)
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
            await _say(interaction, "❌ Este panel de edición no es tuyo. Usá `/editar`.")
            return False
        return True

    def add_button(self, label: str, callback, *, style=discord.ButtonStyle.secondary, emoji: Optional[str] = None, row: Optional[int] = None):
        b = discord.ui.Button(label=label, style=style, emoji=emoji, row=row)
        b.callback = callback
        self.add_item(b)
        return b


class EditView(_OwnedView):
    """Panel de /editar (efímero): cada botón abre un formulario o un selector."""

    def __init__(self, cog, split_id, owner_id):
        super().__init__(cog, split_id, owner_id)
        self.add_button("Datos (título, tab, city)", self._info, emoji="📝", row=0)
        self.add_button("Montos y taxes", self._amounts, emoji="💰", row=0)
        self.add_button("Agregar looters", self._add_looters, style=discord.ButtonStyle.success, emoji="🪙", row=1)
        self.add_button("Editar looter", self._edit_looter, emoji="✏️", row=1)
        self.add_button("Quitar looters", self._rm_looters, style=discord.ButtonStyle.danger, emoji="➖", row=1)
        self.add_button("Agregar personas", self._add_people, style=discord.ButtonStyle.success, emoji="👥", row=2)
        self.add_button("Quitar personas", self._rm_people, style=discord.ButtonStyle.danger, emoji="🧹", row=2)
        self.add_button("Agregar rol", self._add_role, emoji="🎭", row=2)

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
        self.looter_in = mk("Tax looter por defecto (%)", f"{s.tax_looter:g}")
        for i in (self.total_in, self.repair_in, self.sale_in, self.guild_in, self.looter_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_amounts(interaction, self.split_id, str(self.total_in.value), str(self.repair_in.value),
                                     str(self.sale_in.value), str(self.guild_in.value), str(self.looter_in.value))

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
        self.bot.add_view(SplitRequestView(self))

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    @property
    def economy(self):
        return self.bot.get_cog("EconomyCog")

    def money(self, amount: int) -> str:
        e = self.economy
        return e.money(amount) if e is not None else f"{eco.format_amount(amount)} {eco.CURRENCY_NAME}"

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
        view = SplitPanelView(self) if s.is_open else None
        try:
            ch = await self._get_channel(s.channel_id)
            if ch is not None and s.message_id:
                msg = await ch.fetch_message(s.message_id)
                await msg.edit(embed=panel_embed(s), view=view, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            print(f"⚠️ Split #{split_id}: no pude actualizar el panel: {exc!r}")
        th = await self._get_channel(s.thread_id)
        if th is None:
            return
        for col, builder in (("list_message_id", lambda: [people_embed(s)]), ("data_message_id", lambda: data_embeds(s))):
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

    # ---------- /transparencia ----------
    @app_commands.command(name="transparencia", description="Crea un split: panel + hilo con la lista de personas y el cálculo")
    @app_commands.describe(
        titulo="Título del split",
        tab="Tab (opcional)", city="City (opcional)",
        rol="Rol cuyos miembros entran al split (opcional)",
        total="Total del split, en silver (opcional)", reparacion="Reparación total (opcional)",
        looters="Mencioná a los looters, por ejemplo @Ana @Beto (opcional)")
    @app_commands.guild_only()
    async def transparencia(self, interaction: discord.Interaction, titulo: str, tab: Optional[str] = None, city: Optional[str] = None,
                            rol: Optional[discord.Role] = None, total: Optional[str] = None, reparacion: Optional[str] = None,
                            looters: Optional[str] = None):
        if not can_manage(interaction.user):
            return await _say(interaction, "🔒 Solo el Council (o un administrador) puede crear splits.")
        try:
            total_v = sdb.parse_money(total, field_name="Total")
            repair_v = sdb.parse_money(reparacion, field_name="Reparación")
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        guild = interaction.guild
        looter_ids: List[int] = []
        for m in MENTION_RE.finditer(looters or ""):
            uid = int(m.group(1))
            member = guild.get_member(uid) if guild else None
            if member is None:
                return await _say(interaction, f"❌ No encuentro en el servidor a <@{uid}>.")
            if getattr(member, "bot", False):
                return await _say(interaction, f"❌ <@{uid}> es un bot: no puede ser looter.")
            if uid not in looter_ids:
                looter_ids.append(uid)
        if (looters or "").strip() and not looter_ids:
            return await _say(interaction, "❌ En `looters` mencioná a las personas con @ (ej: @Ana @Beto).")
        people: List[int] = []
        if rol is not None:
            people = [m.id for m in getattr(rol, "members", []) if not getattr(m, "bot", False)]
        await interaction.response.defer(ephemeral=True)
        split, msg, error = await self._publish(
            creator_id=interaction.user.id, title=titulo, tab=tab, city=city, role_id=rol.id if rol else None,
            total=total_v, repair=repair_v, people=people, looters=looter_ids)
        if error:
            return await interaction.followup.send(error, ephemeral=True)
        await interaction.followup.send(
            f"✅ Split **{split.title}** creado: {getattr(msg, 'jump_url', '')}\n"
            f"Con `/editar` (dentro del hilo) cargás los datos de cada looter, los montos y las personas.", ephemeral=True)

    async def _publish(self, *, creator_id: int, title: str, tab=None, city=None, role_id=None, total: int = 0, repair: int = 0,
                       people=(), looters=(), source_cta_id: Optional[int] = None):
        """Crea el split, publica el panel en el canal de splits y arma el hilo. Devuelve (split, mensaje, error)."""
        channel = await self._get_channel(self.channel_id)
        if channel is None:
            return None, None, f"❌ No encuentro el canal de splits (<#{self.channel_id}>). Revisá que el bot lo vea."
        try:
            sid = await self.run(sdb.create_split, guild_id=self.guild_id, creator_id=creator_id, title=title, tab=tab, city=city,
                                 role_id=role_id, total=total, repair=repair, people=people, looters=looters, source_cta_id=source_cta_id)
        except sdb.SplitError as exc:
            return None, None, f"❌ {exc}"
        s = await self.run(sdb.get_split, sid)
        try:
            msg = await channel.send(embed=panel_embed(s), view=SplitPanelView(self), allowed_mentions=NO_MENTIONS)
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
            lst = await thread.send(embeds=[people_embed(s)], allowed_mentions=NO_MENTIONS)
            dat = await thread.send(embeds=data_embeds(s), allowed_mentions=NO_MENTIONS)
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
        if s is None:
            return
        if not (can_manage(interaction.user) or interaction.user.id == s.creator_id):
            return await _say(interaction, "🔒 Solo el Council, un administrador o quien creó el split puede editarlo.")
        if not s.is_open:
            return await _say(interaction, "❌ Ese split ya está cerrado: no se puede editar.")
        await _say(interaction, f"✏️ Editando **Split {s.title}** (#{s.id}). Los cambios se reflejan al instante en el hilo.",
                   view=EditView(self, s.id, interaction.user.id))

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
        if not await self._guard(interaction, split_id):
            return
        try:
            vals = dict(total=sdb.parse_money(total, field_name="Total"), repair=sdb.parse_money(repair, field_name="Reparación"),
                        tax_sale=sdb.parse_pct(sale, field_name="Tax de venta"), tax_guild=sdb.parse_pct(guild, field_name="Tax de guild"),
                        tax_looter=sdb.parse_pct(looter, field_name="Tax looter"))
            await self.run(sdb.update_amounts, split_id, **vals)
        except sdb.SplitError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._done(interaction, split_id, "✅ Montos actualizados.")

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
            return await _say(interaction, "❌ El split no tiene personas en la lista. Agregalas con `/editar`.")
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
            try:
                calc, paid, ops = await asyncio.to_thread(sdb.pay_out, self.db_path, s, interaction.user.id)
            except Exception:
                traceback.print_exc()
                return await interaction.followup.send(
                    "❌ Falló el pago. No se perdió nada: volvé a tocar **Cerrar split** y se reintenta sin duplicar pagos.", ephemeral=True)
            await self.run(sdb.finish_close, split_id)
        await self._publish_log(ops)
        s = await self.run(sdb.get_split, split_id)
        await self.sync_split(split_id)
        await self._post_result(s, calc, paid, interaction.user.id)
        await interaction.followup.send("✅ Split cerrado y pagado. La lista quedó en el hilo.", ephemeral=True)

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
            lines.append(f"• <@{u}> → +{self.money(amount)}{extra}")
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
