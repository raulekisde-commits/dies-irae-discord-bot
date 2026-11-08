"""Wujupón — capa de Discord: panel con botones, tiradas, inventario, canjes y encargos del Council.

La lógica y la persistencia viven en wujupon_db.py. Todo lo personal responde en privado (ephemeral) para que el panel
no se llene de mensajes. Las tiradas se anuncian en un canal público configurable.
"""
import asyncio
import io
import time
import traceback
from typing import Callable, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy_db as eco
import wujupon_db as wdb
from guildshop import COUNCIL_ROLE_ID, can_manage

NO_MENTIONS = discord.AllowedMentions.none()
GOLD = 0xF1C40F
SPIN_FRAMES = ("🎰 Girando el Wujupón…", "🎰 ✨ ✨ ✨")
PHOTO_SECONDS = 60             # la foto de un premio se borra pasado este tiempo
SPIN_CLEANUP_SECONDS = 60      # los mensajes privados de cada tirada se borran solos pasado este tiempo


def _hm(seconds: int) -> str:
    h, m = divmod((int(seconds) + 59) // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def _next_utc_midnight() -> int:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return int((now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp())


def money(n: int) -> str:
    return f"{eco.format_amount(n)} {eco.CURRENCY_NAME}"


def prize_mode(p: wdb.Prize) -> str:
    if p.forced:
        return "☠️ se aplica sola" if p.effect else "☠️ se aplica sola (por ahora la aplica el Council)"
    if p.automatic:
        return "🤖 automático"
    if p.origin == "Bot":
        return "🛠️ lo entrega el Council (automático próximamente)"
    return "🛠️ lo entrega el Council"


def card_embed(p: wdb.Prize, card_id: int, weight: int, total: int, *, title: str = "🎴 ¡Nueva carta!",
               show_desc: bool = True) -> discord.Embed:
    """show_desc=False: la descripción no se muestra (solo se ve desde 🎒 Mi inventario)."""
    emb = discord.Embed(title=title, color=p.color)
    emb.description = f"# {p.name}\n**{p.rarity_label}** · probabilidad **{wdb.chance_text(weight, total)}**"
    if p.description and show_desc:
        emb.description += f"\n\n{p.description}"
    elif p.description:
        emb.description += "\n\n📖 Mirá qué hace en 🎒 **Mi inventario**."
    emb.add_field(name="Canje", value=prize_mode(p), inline=True)
    if card_id > 0:
        emb.add_field(name="Carta", value=f"`#{card_id}`", inline=True)
    if p.forced:
        emb.set_footer(text="Carta mala: no va a tu inventario; se aplica sola." if p.effect
                       else "Carta mala: no va a tu inventario; se le avisó al Council para aplicarla.")
    else:
        emb.set_footer(text="Guardala en tu inventario y canjeala cuando quieras desde 🎒 Mi inventario.")
    return emb


def _chunk_lines(lines: List[str], budget: int) -> List[str]:
    blocks, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) + 1 > budget:
            blocks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def prizes_embeds(prizes: List[wdb.Prize], price: int) -> List[discord.Embed]:
    total = sum(p.weight for p in prizes if p.enabled) or 1
    out = []
    for rarity in wdb.RARITY_ORDER:
        group = [p for p in prizes if p.rarity == rarity and p.enabled]
        if not group:
            continue
        name, emoji, color = wdb.RARITIES[rarity]
        share = sum(p.weight for p in group)
        lines = [f"• **{p.name}** — {wdb.chance_text(p.weight, total)} · {'☠️' if p.forced else '🤖' if p.automatic else '🛠️'}"
                 for p in group]   # la descripción solo se ve en el inventario
        title = f"{emoji} {name.upper()} — {wdb.chance_text(share, total)}"
        for n, block in enumerate(_chunk_lines(lines, 3900)):   # una rareza larga se parte en varios embeds
            out.append(discord.Embed(title=title if n == 0 else f"{title} (cont.)", color=color, description=block))
    if out:
        out[-1].set_footer(text=f"Cada tirada cuesta {money(price)} · 🤖 canje automático · 🛠️ lo entrega el Council · "
                                "☠️ carta mala, se aplica sola · ⚠️ afecta a otra persona (requiere aprobación)")
    return out


def pack_embeds(embeds: List[discord.Embed], budget: int = 5800, per_msg: int = 10) -> List[List[discord.Embed]]:
    """Agrupa embeds en mensajes que respeten los límites de Discord (6000 caracteres y 10 embeds por mensaje)."""
    groups: List[List[discord.Embed]] = []
    cur: List[discord.Embed] = []
    size = 0
    for e in embeds:
        n = len(e)
        if cur and (size + n > budget or len(cur) >= per_msg):
            groups.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        groups.append(cur)
    return groups


def request_embed(req: wdb.Request, p: Optional[wdb.Prize]) -> discord.Embed:
    color = {"pending": 0xF39C12, "taken": 0x3498DB, "approved": 0x9B59B6, "delivered": 0x2ECC71,
             "rejected": 0xE74C3C, "returned": 0x95A5A6}.get(req.status, 0x95A5A6)
    emb = discord.Embed(title=f"🎴 Encargo Wujupón #{req.id}", color=color)
    emb.add_field(name="Jugador", value=f"<@{req.user_id}>", inline=True)
    emb.add_field(name="Premio", value=f"{p.rarity_label} · **{p.name}**" if p else req.prize_key, inline=True)
    emb.add_field(name="Estado", value=wdb.STATUS_LABELS.get(req.status, req.status), inline=True)
    if p is not None:
        benefit = prize_mode(p)
        if p.description:
            benefit = f"{p.description[:800]}\n{benefit}"
        emb.add_field(name="Beneficio", value=benefit, inline=False)
    emb.add_field(name="Datos del jugador", value=(req.details or "—")[:1000], inline=False)
    who = []
    if req.taker_id:
        who.append(f"Tomado por <@{req.taker_id}>")
    if req.approved_by:
        who.append(f"Aprobado por <@{req.approved_by}>")
    if req.delivered_by:
        who.append(f"Entregado por <@{req.delivered_by}>")
    if req.rejected_by:
        who.append(f"Rechazado por <@{req.rejected_by}> · carta {'devuelta' if req.refunded else 'consumida'}")
    if req.status == "returned":
        who.append("Carta devuelta al inventario")
    if who:
        emb.add_field(name="Seguimiento", value="\n".join(who), inline=False)
    if req.note and req.status in ("rejected", "returned"):
        emb.add_field(name="Nota", value=req.note[:1000], inline=False)
    emb.set_footer(text=f"Carta #{req.card_id} · canjeada {req.created_at[:16].replace('T', ' ')} UTC")
    return emb


async def _say(interaction: discord.Interaction, content: str = None, **kw):
    """Respuesta privada. Si va como followup, devuelve el mensaje (para poder borrarlo después)."""
    kw.setdefault("allowed_mentions", NO_MENTIONS)
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True, wait=True, **kw)
        await interaction.response.send_message(content, ephemeral=True, **kw)
    except Exception:
        traceback.print_exc()
    return None


# ============================================================
# Vistas
# ============================================================
class PanelView(discord.ui.View):
    """Panel permanente (sobrevive reinicios: custom_id fijos)."""

    def __init__(self, cog: "WujuponCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Tirar Wujupón", style=discord.ButtonStyle.success, custom_id="wjp:spin", emoji="🎰")
    async def spin(self, interaction, button):
        await self.cog.do_spin(interaction)

    @discord.ui.button(label="Comprar tiradas", style=discord.ButtonStyle.primary, custom_id="wjp:buy", emoji="🛒")
    async def buy(self, interaction, button):
        await self.cog.open_buy(interaction)

    @discord.ui.button(label="Mi inventario", style=discord.ButtonStyle.secondary, custom_id="wjp:inv", emoji="🎒")
    async def inv(self, interaction, button):
        await self.cog.show_inventory(interaction)

    @discord.ui.button(label="Ver premios", style=discord.ButtonStyle.secondary, custom_id="wjp:prizes", emoji="📜")
    async def prizes(self, interaction, button):
        await self.cog.show_prizes(interaction)

    @discord.ui.button(label="Mis solicitudes", style=discord.ButtonStyle.secondary, custom_id="wjp:reqs", emoji="📨")
    async def reqs(self, interaction, button):
        await self.cog.show_requests(interaction)


class PrizesButtonView(discord.ui.View):
    """Botón «Ver premios» en el aviso de pausa (la lista se puede ver aunque Wujupón esté pausado)."""

    def __init__(self, cog: "WujuponCog"):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Ver premios", style=discord.ButtonStyle.secondary, emoji="📜")
    async def prizes(self, interaction, button):
        await self.cog.show_prizes(interaction)


class BuyModal(discord.ui.Modal, title="Comprar tiradas de Wujupón"):
    cantidad = discord.ui.TextInput(label="¿Cuántas tiradas?", placeholder="1", default="1", max_length=3)

    def __init__(self, cog: "WujuponCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.do_buy(interaction, str(self.cantidad.value))


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _say(interaction, "❌ Esto no es tuyo.")
            return False
        return True


class SpinAgainView(OwnerView):
    def __init__(self, cog: "WujuponCog", owner_id: int, spins: int):
        super().__init__(owner_id)
        self.cog = cog
        if spins <= 0:
            self.spin_again.label = "Comprar tiradas"
            self.spin_again.emoji = "🛒"

    @discord.ui.button(label="Tirar otra", style=discord.ButtonStyle.success, emoji="🎰")
    async def spin_again(self, interaction, button):
        if button.label == "Comprar tiradas":
            return await self.cog.open_buy(interaction)
        await self.cog.do_spin(interaction)

    @discord.ui.button(label="Mi inventario", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def inv(self, interaction, button):
        await self.cog.show_inventory(interaction)


class InventoryView(OwnerView):
    def __init__(self, cog: "WujuponCog", owner_id: int, rows: List[wdb.InventoryRow]):
        super().__init__(owner_id)
        self.cog = cog
        options = [discord.SelectOption(label=r.prize.name[:100], value=str(r.first_card_id),
                                        description=f"{wdb.RARITIES[r.prize.rarity][0]} · x{r.available}"[:100],
                                        emoji=wdb.RARITIES[r.prize.rarity][1])
                   for r in rows if r.first_card_id][:25]
        if options:
            sel = discord.ui.Select(placeholder="Elegí una carta para ver o canjear", options=options)
            sel.callback = lambda inter, s=sel: self.cog.show_card(inter, int(s.values[0]))
            self.add_item(sel)


class RedeemView(OwnerView):
    def __init__(self, cog: "WujuponCog", owner_id: int, card_id: int):
        super().__init__(owner_id)
        self.cog, self.card_id = cog, card_id

    @discord.ui.button(label="Canjear", style=discord.ButtonStyle.success, emoji="✨")
    async def redeem(self, interaction, button):
        await self.cog.start_redeem(interaction, self.card_id)


class RedeemModal(discord.ui.Modal, title="Canjear carta"):
    datos = discord.ui.TextInput(label="Datos para cumplir el premio", style=discord.TextStyle.paragraph, max_length=800,
                                 placeholder="A quién afecta (nick / @), qué CTA, build, ítem, horario, etc.")

    def __init__(self, cog: "WujuponCog", card_id: int, required: bool):
        super().__init__()
        self.cog, self.card_id = cog, card_id
        self.datos.required = required

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.do_redeem(interaction, self.card_id, str(self.datos.value or ""))


class RequestView(discord.ui.View):
    """Botones del encargo en el canal del Council (persistente: se busca el encargo por el mensaje)."""

    def __init__(self, cog: "WujuponCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Tomar", style=discord.ButtonStyle.secondary, custom_id="wjp:req:take", emoji="✋")
    async def take(self, interaction, button):
        await self.cog.request_action(interaction, "take")

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.primary, custom_id="wjp:req:approve", emoji="👍")
    async def approve(self, interaction, button):
        await self.cog.request_action(interaction, "approve")

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, custom_id="wjp:req:reject", emoji="✖️")
    async def reject(self, interaction, button):
        await self.cog.request_action(interaction, "reject")

    @discord.ui.button(label="Completado", style=discord.ButtonStyle.success, custom_id="wjp:req:deliver", emoji="✅")
    async def deliver(self, interaction, button):
        await self.cog.request_action(interaction, "deliver")


class WipeConfirmView(OwnerView):
    def __init__(self, cog: "WujuponCog", owner_id: int, include_spins: bool):
        super().__init__(owner_id, timeout=60)
        self.cog, self.include_spins = cog, include_spins

    @discord.ui.button(label="Sí, borrar todas las cartas", style=discord.ButtonStyle.danger, emoji="🧹")
    async def ok(self, interaction, button):
        self.stop()
        await self.cog.do_wipe(interaction, self.include_spins)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        self.stop()
        await _say(interaction, "Cancelado. No se tocó nada.")


def is_admin(member) -> bool:
    """Dueño del servidor o permiso Administrator (no delegable)."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    if guild.owner_id == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


class RejectView(OwnerView):
    def __init__(self, cog: "WujuponCog", owner_id: int, req_id: int, message):
        super().__init__(owner_id, timeout=120)
        self.cog, self.req_id, self.message = cog, req_id, message

    @discord.ui.button(label="Rechazar y devolver la carta", style=discord.ButtonStyle.primary, emoji="↩️")
    async def refund(self, interaction, button):
        await self.cog.do_reject(interaction, self.req_id, True, self.message)

    @discord.ui.button(label="Rechazar y consumir la carta", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def consume(self, interaction, button):
        await self.cog.do_reject(interaction, self.req_id, False, self.message)


# ============================================================
# Cog
# ============================================================
class WujuponCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id
        self.anim_delay = 0.8
        self.cleanup_delay = SPIN_CLEANUP_SECONDS
        self.photo_delay = PHOTO_SECONDS
        self._cleanups: set = set()
        self._locks: dict = {}

    async def run(self, fn: Callable, *args, **kwargs):
        return await asyncio.to_thread(fn, self.db_path, *args, **kwargs)

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def cog_load(self):
        await self.run(wdb.init_schema)
        self.bot.add_view(PanelView(self))
        self.bot.add_view(RequestView(self))
        self.housekeeping.start()

    async def cog_unload(self):
        self.housekeeping.cancel()
        for t in list(self._cleanups):
            t.cancel()

    def _cleanup_later(self, interaction: discord.Interaction, *extra_messages, delay: Optional[float] = None):
        """Borra a los `cleanup_delay` segundos la respuesta privada de esta interacción (y los followups dados).
        Es solo visual: la carta ya está guardada en el inventario. Si el bot se reinicia antes, el mensaje queda
        (Discord igual lo saca al cerrar o recargar el cliente)."""
        wait = self.cleanup_delay if delay is None else delay
        if not wait or wait <= 0:
            return

        async def go():
            try:
                await asyncio.sleep(wait)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass                                    # ya borrado, token vencido o el usuario lo descartó
                for m in extra_messages:
                    if m is None:
                        continue
                    try:
                        await m.delete()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass
        task = asyncio.create_task(go())
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)

    # ---------------- panel ----------------
    async def panel_embed(self) -> discord.Embed:
        price = await self.run(wdb.get_price)
        limit = await self.run(wdb.get_daily_limit)
        totals = await self.run(wdb.rarity_totals)
        all_w = sum(totals.values()) or 1
        paused = await self.run(wdb.get_setting, "paused")
        emb = discord.Embed(title="🎰 WUJUPÓN" + (" — ⏸️ PAUSADO" if paused else ""), color=0x95A5A6 if paused else GOLD)
        emb.description = (
            f"Comprá tiradas con **Wuju Coins** y conseguí cartas con premios.\n"
            f"Cada tirada cuesta **{money(price)}**."
            + (f" Límite: **{limit}** tirada(s) compradas por día (UTC)." if limit else "") + "\n\n"
            "🎰 **Tirar Wujupón** — usa una tirada y te da una carta al azar.\n"
            "🛒 **Comprar tiradas** — compralas con tu balance.\n"
            "🎒 **Mi inventario** — mirá tus cartas y canjealas.\n"
            "📜 **Ver premios** — rarezas, probabilidades y cómo se entregan.\n"
            "📨 **Mis solicitudes** — estado de tus canjes con el Council.\n\n"
            "Las cartas son de un solo uso. Todas las respuestas son privadas.")
        emb.add_field(name="Rarezas", value="\n".join(f"{e} {n} — {wdb.chance_text(totals.get(k, 0), all_w)}"
                                                    for k, (n, e, _) in wdb.RARITIES.items()), inline=True)
        if paused:
            reason = await self.run(wdb.get_setting, "paused_reason")
            emb.add_field(name="⏸️ Pausado", value=((reason or "Por ahora no se puede comprar, tirar ni canjear.")[:900]
                                                              + "\n📜 Los premios se pueden ver igual."), inline=False)
        emb.set_footer(text="Las tiradas se anuncian en el canal de Wujupón.")
        return emb

    async def publish_panel(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            return await _say(interaction, "❌ Solo el Council o administradores pueden publicar el panel.")
        channel = interaction.channel
        try:
            msg = await channel.send(embed=await self.panel_embed(), view=PanelView(self))
        except discord.Forbidden:
            return await _say(interaction, "❌ No tengo permiso para escribir en este canal.")
        await self.run(wdb.set_setting, "panel_channel", str(channel.id))
        await self.run(wdb.set_setting, "panel_message", str(getattr(msg, "id", "")))
        await _say(interaction, "✅ Panel de Wujupón publicado. Configurá los canales con `/wujupon canal-encargos` y `/wujupon canal-anuncios`.")

    async def refresh_panel(self):
        """Actualiza el embed del panel publicado (precio, límite, rarezas). Mejor esfuerzo."""
        try:
            cid = await self.run(wdb.get_setting, "panel_channel")
            mid = await self.run(wdb.get_setting, "panel_message")
            if not cid or not mid:
                return
            ch = self.bot.get_channel(int(cid))
            if ch is None:
                return
            msg = await ch.fetch_message(int(mid))
            await msg.edit(embed=await self.panel_embed(), view=PanelView(self))
        except Exception:
            traceback.print_exc()

    # ---------------- comprar ----------------
    async def _paused(self, interaction: discord.Interaction) -> bool:
        """True (y avisa) si un admin pausó Wujupón o si a esta persona le congelaron Wujupón con una carta."""
        import efectos_db
        frozen = await self.run(efectos_db.active_until, interaction.user.id, "freeze_wujupon")
        if frozen is not None:
            await _say(interaction, f"❄️ Tenés **Wujupón congelado** por una carta hasta <t:{int(frozen)}:t>: no podés comprar, tirar ni "
                                    "usar cartas. No perdés nada.")
            return True
        if not await self.run(wdb.get_setting, "paused"):
            return False
        reason = await self.run(wdb.get_setting, "paused_reason")
        await _say(interaction, "⏸️ **Wujupón está pausado** por la administración." + (f"\nMotivo: {reason}" if reason else "")
                   + "\nTus tiradas y cartas no se pierden: podés ver tu inventario, tus solicitudes y la lista de premios.",
                   view=PrizesButtonView(self))
        return True

    async def open_buy(self, interaction: discord.Interaction):
        if await self._paused(interaction):
            return
        await interaction.response.send_modal(BuyModal(self))

    async def do_buy(self, interaction: discord.Interaction, text: str):
        if await self._paused(interaction):
            return
        try:
            count = int(text.strip().replace(".", "").replace(",", ""))
        except ValueError:
            return await _say(interaction, "❌ Escribí un número de tiradas (ej: 3).")
        if not 1 <= count <= wdb.MAX_BUY:
            return await _say(interaction, f"❌ Podés comprar entre 1 y {wdb.MAX_BUY} tiradas por vez.")
        async with self._lock(interaction.user.id):
            res = await self.run(wdb.buy_spins, interaction.user.id, count, f"buy:{interaction.id}")
        if res.status == "funds":
            return await _say(interaction, f"❌ No te alcanza: {count} tirada(s) cuestan **{money(res.cost)}** y tenés {money(res.balance)}.")
        if res.status == "frozen":
            return await _say(interaction, f"❌ {eco.FROZEN_TEXT}")
        if res.status == "invalid":
            return await _say(interaction, "❌ Cantidad inválida.")
        if res.status == "limit":
            limit = await self.run(wdb.get_daily_limit)
            return await _say(interaction, f"⏳ El límite es de **{limit}** tirada(s) compradas por día (UTC). "
                                           + (f"Hoy podés comprar **{res.remaining_today}** más." if res.remaining_today
                                              else "Ya llegaste al límite de hoy: se renueva a las 00:00 UTC."))
        await _say(interaction, f"✅ Compraste **{count}** tirada(s) por **{money(res.cost)}**.\n"
                                f"🎟️ Tiradas disponibles: **{res.spins}** · Balance: {money(res.balance)}",
                   view=SpinAgainView(self, interaction.user.id, res.spins))

    # ---------------- tirar ----------------
    async def do_spin(self, interaction: discord.Interaction):
        if await self._paused(interaction):
            return
        user = interaction.user
        async with self._lock(user.id):
            role_ids = [r.id for r in getattr(user, "roles", [])]
            res = await self.run(wdb.pull, user.id, f"pull:{interaction.id}", None, role_ids)
            free_left = await self.run(wdb.free_spins_left, user.id, role_ids)
        if res.status == "no_spins":
            price = await self.run(wdb.get_price)
            msg = await _say(interaction, f"🎟️ No tenés tiradas. Cada una cuesta **{money(price)}**.",
                             view=SpinAgainView(self, user.id, 0))
            return self._cleanup_later(interaction, msg)
        if res.status == "empty":
            msg = await _say(interaction, "❌ No hay premios habilitados en este momento.")
            return self._cleanup_later(interaction, msg)
        # la carta ya quedó guardada: la animación es solo visual (si el bot se reinicia acá, no se pierde nada)
        # las cartas malas no van al inventario: su descripción se muestra acá
        final = card_embed(res.prize, res.card_id, res.weight, res.total_weight, show_desc=res.prize.forced)
        if res.credited:                                 # Wuju Coins / embo: ya se acreditaron
            final.description = final.description.split("\n\n📖")[0] + f"\n\n✨ {res.credited}"
            final.set_footer(text="Se acreditó al instante.")
        final.add_field(name="🎟️ Tiradas restantes", value=str(res.spins_left) + (f" (+{free_left} gratis hoy)" if free_left else ""),
                        inline=True)
        if self.cleanup_delay:
            first = (("Carta mala: se aplicó sola" if res.prize.effect else "Carta mala: se le avisó al Council para aplicarla")
                     if res.prize.forced
                     else "Se acreditó al instante" if res.credited
                     else "Ya quedó guardada en 🎒 Mi inventario")
            final.set_footer(text=f"{first} · este mensaje se borra en {self.cleanup_delay} s.")
        view = SpinAgainView(self, user.id, res.spins_left + free_left)
        animated = False
        if self.anim_delay > 0:
            try:
                await interaction.response.send_message(
                    embed=discord.Embed(title=SPIN_FRAMES[0], color=GOLD), ephemeral=True)
                await asyncio.sleep(self.anim_delay)
                await interaction.edit_original_response(
                    embed=discord.Embed(title=SPIN_FRAMES[1], description=f"## {res.prize.rarity_label}", color=res.prize.color))
                await asyncio.sleep(self.anim_delay)
                await interaction.edit_original_response(embed=final, view=view)
                animated = True
            except Exception:
                traceback.print_exc()
        extra = None
        if not animated:
            extra = await _say(interaction, embed=final, view=view)
        self._cleanup_later(interaction, extra)
        if res.request_id:
            await self.publish_request(res.request_id)   # carta mala: el loop de 60 s lo reintenta si falla
        elif res.prize.forced and res.prize.effect:
            efx = self.bot.get_cog("EfectosCog")         # carta mala automática (si no está cargado, se aplica al reiniciar)
            if efx is not None:
                msg = await efx.apply_forced(res.card_id, user.id, res.prize)
                if msg:
                    extra = await _say(interaction, msg)
                    self._cleanup_later(interaction, extra)
        try:
            await self.announce_pending()        # si falla, el loop de 60 s lo reintenta
        except Exception:
            traceback.print_exc()

    # ---------------- inventario y premios ----------------
    async def show_inventory(self, interaction: discord.Interaction, target: Optional[discord.abc.User] = None):
        user = target or interaction.user
        rows = await self.run(wdb.inventory, user.id)
        spins = await self.run(wdb.get_spins, user.id)
        free = await self.run(wdb.free_spins_left, user.id, [r.id for r in getattr(user, "roles", [])])
        emb = discord.Embed(title=f"🎒 Inventario de {getattr(user, 'display_name', user)}", color=GOLD)
        if not rows:
            emb.description = "No hay cartas todavía. ¡Tirá el Wujupón!"
        else:
            lines = []
            for r in rows:
                extra = f" · {r.reserved} en canje" if r.reserved else ""
                lines.append(f"{wdb.RARITIES[r.prize.rarity][1]} **{r.prize.name}** ×{r.available}{extra}")
            emb.description = "\n".join(lines)[:4000]
        emb.add_field(name="🎟️ Tiradas disponibles", value=str(spins), inline=True)
        if free:
            emb.add_field(name="🎁 Gratis de hoy", value=f"{free} (vencen <t:{_next_utc_midnight()}:R>)", inline=True)
        view = InventoryView(self, interaction.user.id, rows) if target is None and any(r.first_card_id for r in rows) else None
        await _say(interaction, embed=emb, **({"view": view} if view else {}))

    async def show_card(self, interaction: discord.Interaction, card_id: int):
        card = await self.run(wdb.get_card, card_id)
        if card is None or card.user_id != interaction.user.id or card.status != "available":
            return await _say(interaction, "❌ Esa carta ya no está disponible.")
        p = await self.run(wdb.get_prize, card.prize_key)
        prizes = await self.run(wdb.list_prizes, True)
        total = sum(x.weight for x in prizes) or wdb.TOTAL_WEIGHT
        emb = card_embed(p, card.id, p.weight, total, title="🎴 Tu carta")
        await _say(interaction, embed=emb, view=RedeemView(self, interaction.user.id, card.id))

    async def show_prizes(self, interaction: discord.Interaction):
        prizes = await self.run(wdb.list_prizes)
        price = await self.run(wdb.get_price)
        for group in pack_embeds(prizes_embeds(prizes, price)):
            await _say(interaction, embeds=group)

    async def show_requests(self, interaction: discord.Interaction, target: Optional[discord.abc.User] = None):
        user = target or interaction.user
        reqs = await self.run(wdb.user_requests, user.id)
        emb = discord.Embed(title="📨 Solicitudes de canje", color=GOLD)
        if not reqs:
            emb.description = "No hay solicitudes con el Council."
        else:
            lines = []
            for r in reqs:
                p = await self.run(wdb.get_prize, r.prize_key)
                lines.append(f"`#{r.id}` **{p.name if p else r.prize_key}** — {wdb.STATUS_LABELS.get(r.status, r.status)}"
                             + (f" · {r.note[:80]}" if r.note and r.status in ("rejected", "returned") else ""))
            emb.description = "\n".join(lines)[:4000]
        await _say(interaction, embed=emb)

    # ---------------- canjear ----------------
    async def start_redeem(self, interaction: discord.Interaction, card_id: int):
        if await self._paused(interaction):
            return
        card = await self.run(wdb.get_card, card_id)
        if card is None or card.user_id != interaction.user.id or card.status != "available":
            return await _say(interaction, "❌ Esa carta ya no está disponible.")
        p = await self.run(wdb.get_prize, card.prize_key)
        if (p.handler or "").startswith("cta:"):         # cartas de CTA
            efx = self.bot.get_cog("EfectosCog")
            if efx is None:
                return await _say(interaction, "❌ El módulo de efectos no está cargado. Tu carta no se gastó.")
            return await efx.start_cta(interaction, card, p)
        if p.effect:                                    # cartas con efecto automático (mutes, escudos, congelamientos…)
            efx = self.bot.get_cog("EfectosCog")
            if efx is None:
                return await _say(interaction, "❌ El módulo de efectos no está cargado. Tu carta no se gastó.")
            return await efx.start_use(interaction, card, p)
        if p.automatic:
            return await self.do_redeem(interaction, card_id, "")
        await interaction.response.send_modal(RedeemModal(self, card_id, required=p.third_party))

    async def do_redeem(self, interaction: discord.Interaction, card_id: int, details: str):
        if await self._paused(interaction):
            return
        user = interaction.user
        async with self._lock(user.id):
            res = await self.run(wdb.redeem, card_id=card_id, user_id=user.id, details=details,
                                 request_key=f"redeem:{interaction.id}", guild_id=self.guild_id)
        if res.status == "auto" and res.message == "photo":
            return await self.deliver_photo(interaction, res.prize, card_id)
        if res.status == "auto":
            return await _say(interaction, f"✨ **{res.prize.name}** canjeada.\n{res.message}")
        if res.status == "pending":
            published = await self.publish_request(res.request.id)
            extra = "" if published else "\n⚠️ No pude avisar en el canal del Council; se reintenta solo en unos minutos."
            return await _say(interaction, f"📨 Canje enviado al Council (encargo `#{res.request.id}`). La carta queda **reservada** "
                                           f"hasta que lo resuelvan. Seguilo en 📨 **Mis solicitudes**.{extra}")
        if res.status == "duplicate":
            return await _say(interaction, "ℹ️ Ese canje ya se procesó.")
        await _say(interaction, f"❌ {res.message}")

    # ---------------- premio con foto ----------------
    @staticmethod
    def _is_nsfw(channel) -> bool:
        fn = getattr(channel, "is_nsfw", None)
        try:
            return bool(fn()) if callable(fn) else bool(getattr(channel, "nsfw", False))
        except Exception:
            return False

    async def deliver_photo(self, interaction: discord.Interaction, prize: wdb.Prize, card_id: int):
        """La carta ya se consumió. Manda la foto como spoiler en un canal +18 y la borra a los PHOTO_SECONDS.
        Si no se puede mandar, la carta vuelve al inventario."""
        asset = await self.run(wdb.get_asset, wdb.PHOTO_ASSET)
        error = None
        try:
            if asset is None:
                error = "Todavía no se cargó la foto de este premio."
            elif self._is_nsfw(interaction.channel):
                f = discord.File(io.BytesIO(asset[1]), filename=f"SPOILER_{asset[0]}")
                await _say(interaction, f"🔞 **{prize.name}** · se borra en {self.photo_delay} s.", file=f)
                self._cleanup_later(interaction, delay=self.photo_delay)
                return
            else:
                ch = await self._channel("nsfw_channel")
                if ch is None or not self._is_nsfw(ch):
                    error = "No hay un canal +18 configurado para este premio. Avisale al Council."
                else:
                    f = discord.File(io.BytesIO(asset[1]), filename=f"SPOILER_{asset[0]}")
                    msg = await ch.send(f"🔞 {interaction.user.mention} canjeó **{prize.name}** · se borra en {self.photo_delay} s.",
                                        file=f, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                    self._delete_later(msg, self.photo_delay)
                    return await _say(interaction, f"🔞 Tu premio está en {ch.mention} (se borra en {self.photo_delay} s).")
        except Exception:
            traceback.print_exc()
            error = "No pude mandar la foto."
        await self.run(wdb.return_card, card_id=card_id, actor_id=self.bot.user.id if self.bot.user else 0,
                       reason=f"Entrega fallida: {error}")
        await _say(interaction, f"❌ {error} Tu carta volvió al inventario.")

    def _delete_later(self, message, delay: float):
        async def go():
            try:
                await asyncio.sleep(delay)
                await message.delete()
            except Exception:
                pass
        task = asyncio.create_task(go())
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)

    # ---------------- encargos del Council ----------------
    async def _channel(self, setting: str):
        cid = await self.run(wdb.get_setting, setting)
        if not cid:
            return None
        ch = self.bot.get_channel(int(cid))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(cid))
            except Exception:
                return None
        return ch

    async def publish_request(self, req_id: int) -> bool:
        req = await self.run(wdb.get_request, req_id)
        if req is None or req.message_id:
            return bool(req)
        channel = await self._channel("council_channel")
        if channel is None:
            return False
        p = await self.run(wdb.get_prize, req.prize_key)
        try:
            msg = await channel.send(f"<@&{COUNCIL_ROLE_ID}> nuevo canje de Wujupón", embed=request_embed(req, p), view=RequestView(self),
                                     allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        except Exception:
            traceback.print_exc()
            return False
        await self.run(wdb.set_request_message, req_id, channel.id, msg.id)
        return True

    async def request_action(self, interaction: discord.Interaction, action: str):
        if not can_manage(interaction.user):
            return await _say(interaction, "❌ Solo el Council o administradores gestionan los encargos de Wujupón.")
        msg = interaction.message
        req = await self.run(wdb.find_request_by_message, getattr(msg, "id", 0))
        if req is None:
            return await _say(interaction, "❌ No encontré el encargo de este mensaje.")
        if action == "reject":
            return await _say(interaction, f"¿Rechazar el encargo `#{req.id}`? Elegí qué pasa con la carta:",
                              view=RejectView(self, interaction.user.id, req.id, msg))
        fn = {"take": wdb.take_request, "approve": wdb.approve_request, "deliver": wdb.deliver_request}[action]
        ch = await self.run(fn, req.id, interaction.user.id)
        if not ch.ok:
            return await _say(interaction, f"❌ {ch.message}")
        await self._refresh_request(msg, ch.request)
        label = {"take": "tomado", "approve": "aprobado", "deliver": "marcado como entregado"}[action]
        await _say(interaction, f"✅ Encargo `#{req.id}` {label}.")
        if action in ("approve", "deliver"):
            await self._notify(ch.request)

    async def do_reject(self, interaction: discord.Interaction, req_id: int, refund: bool, message):
        ch = await self.run(wdb.reject_request, req_id, interaction.user.id, refund=refund,
                            note=f"Rechazado por {interaction.user.display_name}")
        if not ch.ok:
            return await _say(interaction, f"❌ {ch.message}")
        await self._refresh_request(message, ch.request)
        await _say(interaction, f"✅ Encargo `#{req_id}` rechazado · carta {'devuelta' if refund else 'consumida'}.")
        await self._notify(ch.request)

    async def _refresh_request(self, message, req: wdb.Request):
        if message is None or req is None:
            return
        p = await self.run(wdb.get_prize, req.prize_key)
        closed = req.status not in wdb.OPEN_STATUSES
        try:
            await message.edit(embed=request_embed(req, p), view=None if closed else RequestView(self))
        except Exception:
            traceback.print_exc()

    async def _refresh_request_by_id(self, req_id: Optional[int]):
        if not req_id:
            return
        req = await self.run(wdb.get_request, req_id)
        if req is None or not req.message_id or not req.channel_id:
            return
        ch = self.bot.get_channel(int(req.channel_id))
        try:
            msg = await ch.fetch_message(int(req.message_id)) if ch else None
        except Exception:
            msg = None
        await self._refresh_request(msg, req)

    async def _notify(self, req: wdb.Request):
        """Aviso por MD al jugador (mejor esfuerzo)."""
        try:
            user = self.bot.get_user(req.user_id) or await self.bot.fetch_user(req.user_id)
            p = await self.run(wdb.get_prize, req.prize_key)
            await user.send(f"🎴 Tu canje de Wujupón `#{req.id}` (**{p.name if p else req.prize_key}**): "
                            f"{wdb.STATUS_LABELS.get(req.status, req.status)}"
                            + (" — la carta volvió a tu inventario." if req.refunded else ""))
        except Exception:
            pass

    # ---------------- anuncios y reintentos ----------------
    async def announce_pending(self):
        channel = await self._channel("feed_channel")
        if channel is None:
            return
        pending = await self.run(wdb.unannounced_pulls)
        done = []
        for pull_id, user_id, key, weight, total in pending:
            p = await self.run(wdb.get_prize, key)
            if p is None:
                done.append(pull_id)
                continue
            emb = discord.Embed(color=p.color, description=f"🎰 <@{user_id}> sacó **{p.name}**\n"
                                                           f"{p.rarity_label} · probabilidad **{wdb.chance_text(weight, total)}**")
            try:
                await channel.send(embed=emb, allowed_mentions=NO_MENTIONS)
                done.append(pull_id)
            except Exception:
                traceback.print_exc()
                break
        await self.run(wdb.mark_announced, done)

    @tasks.loop(seconds=60)
    async def housekeeping(self):
        try:
            await self.announce_pending()
            for req_id in await self.run(wdb.unpublished_requests):
                await self.publish_request(req_id)
        except Exception:
            traceback.print_exc()

    # ============================================================
    # /wujupon (Council)
    # ============================================================
    wj = app_commands.Group(name="wujupon", description="(Council) Administración de Wujupón", guild_only=True)

    async def _council(self, interaction: discord.Interaction) -> bool:
        if not can_manage(interaction.user):
            await _say(interaction, "❌ Solo el Council o administradores.")
            return False
        return True

    @wj.command(name="canal-anuncios", description="(Council) Fija o quita el canal donde se anuncia qué carta sacó cada uno")
    @app_commands.describe(canal="Canal de anuncios (por defecto, este canal)", quitar="Sí = dejar de anunciar las tiradas")
    async def feed_channel(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None,
                           quitar: Optional[bool] = False):
        if not await self._council(interaction):
            return
        if quitar:
            await self.run(wdb.set_setting, "feed_channel", None)
            await self.run(wdb.mark_all_announced)
            return await _say(interaction, "🔕 Las tiradas ya no se anuncian. Volvé a usar `/wujupon canal-anuncios` para activarlo.")
        target = canal or interaction.channel
        perms = target.permissions_for(interaction.guild.me) if interaction.guild and interaction.guild.me else None
        if perms is not None and not (perms.send_messages and perms.embed_links):
            return await _say(interaction, f"❌ No tengo permiso para enviar mensajes con embeds en {target.mention}.")
        if not await self.run(wdb.get_setting, "feed_channel"):
            await self.run(wdb.mark_all_announced)      # no publicar de golpe lo que salió mientras estaba desactivado
        await self.run(wdb.set_setting, "feed_channel", str(target.id))
        await _say(interaction, f"📣 Las tiradas se anuncian en {target.mention} (jugador, carta, rareza y probabilidad).")

    @wj.command(name="canal-encargos", description="(Council) Fija el canal privado del Council donde llegan los canjes")
    @app_commands.describe(canal="Canal de encargos (por defecto, este canal)")
    async def council_channel(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        if not await self._council(interaction):
            return
        target = canal or interaction.channel
        await self.run(wdb.set_setting, "council_channel", str(target.id))
        await _say(interaction, f"📨 Los canjes de Wujupón llegan a {target.mention}.")

    @wj.command(name="regalar-tiradas", description="(Council) Regala tiradas a una persona o a todo un rol (o quita, con número negativo)")
    @app_commands.describe(cantidad="Tiradas por persona (negativo para quitar)", usuario="Persona (o usá rol)",
                           rol="Rol: se le dan a cada miembro (o usá usuario)", motivo="Motivo")
    async def gift(self, interaction: discord.Interaction, cantidad: int, usuario: Optional[discord.Member] = None,
                   rol: Optional[discord.Role] = None, motivo: Optional[str] = None):
        if not await self._council(interaction):
            return
        if (usuario is None) == (rol is None):
            return await _say(interaction, "❌ Indicá una persona **o** un rol (uno solo de los dos).")
        if rol is not None:
            members = [m for m in getattr(rol, "members", []) if not getattr(m, "bot", False)]
            if not members:
                return await _say(interaction, f"❌ {rol.mention} no tiene miembros.")
            try:
                n = await self.run(wdb.gift_spins_many, actor_id=interaction.user.id, user_ids=[m.id for m in members], count=cantidad,
                                   request_key=f"giftrole:{interaction.id}", reason=(motivo or f"Regalo del Council a {rol.name}")[:300])
            except wdb.WujuponError as exc:
                return await _say(interaction, f"❌ {exc}")
            return await self._gift_announce(interaction, cantidad, f"{rol.mention} (**{n}** persona(s), a cada uno)", motivo)
        try:
            after = await self.run(wdb.gift_spins, actor_id=interaction.user.id, user_id=usuario.id, count=cantidad,
                                   request_key=f"gift:{interaction.id}", reason=(motivo or "Regalo del Council")[:300])
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        await self._gift_announce(interaction, cantidad, f"{usuario.mention} (ahora tiene **{after}**)", motivo)

    async def _gift_announce(self, interaction: discord.Interaction, cantidad: int, destino: str, motivo: Optional[str]):
        """Mensaje público del regalo/quita + copia al canal de log (/canal wujupon-regalos)."""
        verbo = "regaló" if cantidad > 0 else "quitó"
        texto = (f"🎁 {interaction.user.mention} {verbo} **{abs(cantidad)}** tirada(s) de Wujupón a {destino}.\n"
                 f"📝 Motivo: {motivo or 'sin motivo'}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(texto, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.send_message(texto, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
        log = await self._channel("gift_log_channel")
        if log is not None:
            try:
                where = f" · en {interaction.channel.mention}" if getattr(interaction.channel, "mention", None) else ""
                await log.send(f"{texto}\n🕒 <t:{int(time.time())}:f>{where}", allowed_mentions=NO_MENTIONS)
            except Exception:
                traceback.print_exc()

    async def canal_regalos(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None,
                            desactivar: bool = False):
        """Lo llama /canal wujupon-regalos (guildshop.py): canal de log de /wujupon regalar-tiradas."""
        if not await self._council(interaction):
            return
        if desactivar:
            await self.run(wdb.set_setting, "gift_log_channel", None)
            return await _say(interaction, "🔕 Ya no se registran los regalos de tiradas.")
        target = canal or interaction.channel
        perms = target.permissions_for(interaction.guild.me) if interaction.guild and interaction.guild.me else None
        if perms is not None and not perms.send_messages:
            return await _say(interaction, f"❌ No tengo permiso para escribir en {target.mention}.")
        await self.run(wdb.set_setting, "gift_log_channel", str(target.id))
        await _say(interaction, f"📒 Los regalos de tiradas se registran en {target.mention}.")

    @wj.command(name="tiradas-diarias", description="(Council) Tiradas gratis por día para un rol (0 = sacar). Sin datos, muestra la lista")
    @app_commands.describe(rol="Rol", cantidad="Tiradas gratis por día (0 = sacar el rol)")
    async def daily_spins(self, interaction: discord.Interaction, rol: Optional[discord.Role] = None, cantidad: Optional[int] = None):
        if not await self._council(interaction):
            return
        if rol is not None and cantidad is not None:
            try:
                await self.run(wdb.set_daily_role, rol.id, cantidad, interaction.user.id)
            except wdb.WujuponError as exc:
                return await _say(interaction, f"❌ {exc}")
            msg = (f"✅ {rol.mention} recibe **{cantidad}** tirada(s) gratis por día." if cantidad
                   else f"✅ {rol.mention} ya no recibe tiradas gratis.")
            return await _say(interaction, msg + "\nSe renuevan a las 00:00 UTC (21:00 de Argentina) y las que no se usan vencen. "
                                                 "Si alguien tiene varios roles, recibe las del que más da.")
        if (rol is None) != (cantidad is None):
            return await _say(interaction, "❌ Indicá el rol **y** la cantidad (o ninguno de los dos para ver la lista).")
        rows = await self.run(wdb.list_daily_roles)
        await _say(interaction, embed=discord.Embed(title="🎁 Tiradas diarias gratis", color=GOLD,
                                                    description="\n".join(f"<@&{r}> — **{n}** por día" for r, n in rows)
                                                    or "Ningún rol tiene tiradas diarias."))

    async def prize_autocomplete(self, interaction: discord.Interaction, current: str):
        prizes = await self.run(wdb.list_prizes)
        cur = (current or "").lower()
        return [app_commands.Choice(name=f"{wdb.RARITIES[p.rarity][1]} {p.name}"[:100], value=p.key)
                for p in prizes if cur in p.name.lower() or cur in p.key][:25]

    @wj.command(name="carta-agregar", description="(Council) Crea una carta nueva (la entrega el Council)")
    @app_commands.describe(nombre="Nombre de la carta", rareza="Rareza", probabilidad="Probabilidad en % (ej: 0,5)",
                           descripcion="Qué da la carta y sus condiciones", afecta_a_otro="Sí si el premio afecta a otra persona")
    @app_commands.choices(rareza=[app_commands.Choice(name=f"{e} {n}", value=k) for k, (n, e, _) in wdb.RARITIES.items()])
    async def add_card(self, interaction: discord.Interaction, nombre: str, rareza: app_commands.Choice[str], probabilidad: str,
                       descripcion: Optional[str] = None, afecta_a_otro: Optional[bool] = False):
        if not await self._council(interaction):
            return
        try:
            weight = wdb.parse_chance(probabilidad)
            p = await self.run(wdb.add_prize, actor_id=interaction.user.id, name=nombre, description=descripcion or "",
                               rarity=rareza.value, weight=weight, third_party=bool(afecta_a_otro))
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        total = await self.run(wdb.enabled_total)
        warn = ""
        if total != wdb.TOTAL_WEIGHT:
            warn = (f"\n⚠️ Las probabilidades habilitadas ahora suman **{wdb.chance_text(total)}**: cada carta sale en proporción "
                    f"a su peso, así que la chance real de esta es **{wdb.chance_text(p.weight, total)}** y las demás bajan un poco. "
                    "Deshabilitá otra con `/wujupon premio` si querés volver al 100 %.")
        await _say(interaction, f"✅ Carta creada: {p.rarity_label} **{p.name}** ({wdb.chance_text(p.weight)}).{warn}",
                   embed=card_embed(p, 0, p.weight, total or wdb.TOTAL_WEIGHT, title="🆕 Vista previa"))

    @wj.command(name="carta-editar", description="(Council) Cambia el nombre y/o la descripción de una carta")
    @app_commands.describe(carta="Carta", nombre="Nombre nuevo (opcional)", descripcion="Descripción nueva (opcional; «quitar» la borra)")
    @app_commands.autocomplete(carta=prize_autocomplete)
    async def edit_card(self, interaction: discord.Interaction, carta: str, nombre: Optional[str] = None,
                        descripcion: Optional[str] = None):
        if not await self._council(interaction):
            return
        try:
            before, after = await self.run(wdb.edit_prize, carta, actor_id=interaction.user.id, name=nombre, description=descripcion)
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        total = await self.run(wdb.enabled_total) or wdb.TOTAL_WEIGHT
        lines = ["✅ Carta actualizada (también las que ya tiene la gente en el inventario)."]
        if nombre is not None:
            lines.append(f"Nombre: ~~{before.name}~~ → **{after.name}**")
        if descripcion is not None:
            lines.append("Descripción: " + (f"«{after.description[:300]}»" if after.description else "quitada"))
        await _say(interaction, "\n".join(lines), embed=card_embed(after, 0, after.weight, total, title="✏️ Vista previa"))

    @wj.command(name="dar-carta", description="(Council) Da una carta de un premio a una persona")
    @app_commands.describe(usuario="Persona", premio="Premio", motivo="Motivo")
    @app_commands.autocomplete(premio=prize_autocomplete)
    async def give(self, interaction: discord.Interaction, usuario: discord.Member, premio: str, motivo: Optional[str] = None):
        if not await self._council(interaction):
            return
        try:
            card_id = await self.run(wdb.give_card, actor_id=interaction.user.id, user_id=usuario.id, prize_key=premio,
                                     reason=(motivo or "Dada por el Council")[:300])
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        p = await self.run(wdb.get_prize, premio)
        await _say(interaction, f"✅ Carta `#{card_id}` **{p.name}** agregada al inventario de {usuario.mention}.")

    @wj.command(name="dar-ticket", description="(Council) Da tickets de descuento del Guild Shop (15 % o 20 % hasta 20M, 30 % hasta 35M)")
    @app_commands.describe(usuario="Persona", tipo="Qué ticket", cantidad="Cuántos (1 a 20)", motivo="Motivo")
    @app_commands.choices(tipo=[app_commands.Choice(name="15 % hasta 20M", value="shop_discount_15"),
                                app_commands.Choice(name="20 % hasta 20M", value="shop_discount"),
                                app_commands.Choice(name="30 % hasta 35M", value="shop_discount_30")])
    async def give_ticket(self, interaction: discord.Interaction, usuario: discord.Member, tipo: app_commands.Choice[str],
                          cantidad: Optional[int] = 1, motivo: Optional[str] = None):
        if not await self._council(interaction):
            return
        cantidad = cantidad or 1
        if not 1 <= cantidad <= 20:
            return await _say(interaction, "❌ La cantidad tiene que estar entre 1 y 20.")
        if usuario.bot:
            return await _say(interaction, "❌ No se le pueden dar tickets a un bot.")
        reason = (motivo or "Ticket de descuento dado por el Council")[:300]
        ids = []
        try:
            for _ in range(cantidad):
                ids.append(await self.run(wdb.give_card, actor_id=interaction.user.id, user_id=usuario.id, prize_key=tipo.value,
                                          reason=reason))
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        await _say(interaction, f"🎟️ {usuario.mention} recibió **{cantidad}** ticket(s) de descuento de **{tipo.name}** "
                                f"({', '.join(f'#{i}' for i in ids)}). Los ve en 🎒 Mi inventario y se usan al abrir un ticket en el Guild Shop.")

    @wj.command(name="devolver-carta", description="(Council) Devuelve al inventario una carta usada (mal usada, bug, etc.)")
    @app_commands.describe(carta="Número de carta (#)", motivo="Motivo")
    async def give_back(self, interaction: discord.Interaction, carta: int, motivo: Optional[str] = None):
        if not await self._council(interaction):
            return
        try:
            card, req_id = await self.run(wdb.return_card, card_id=carta, actor_id=interaction.user.id,
                                          reason=(motivo or f"Devuelta por {interaction.user.display_name}")[:300])
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        p = await self.run(wdb.get_prize, card.prize_key)
        await _say(interaction, f"↩️ Carta `#{card.id}` **{p.name}** devuelta al inventario de <@{card.user_id}>."
                                + (f" Se cerró el encargo `#{req_id}`." if req_id else "")
                                + ("\n⚠️ Si el premio ya se había entregado (Wuju, embo, etc.), no se revierte: ajustalo a mano si hace falta."
                                   if not req_id else ""))
        await self._refresh_request_by_id(req_id)

    @wj.command(name="inventario", description="(Council) Mira el inventario y las solicitudes de una persona")
    async def inv_admin(self, interaction: discord.Interaction, usuario: discord.Member):
        if not await self._council(interaction):
            return
        await self.show_inventory(interaction, usuario)
        await self.show_requests(interaction, usuario)

    @wj.command(name="encargos", description="(Council) Lista los canjes abiertos")
    async def open_list(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        reqs = await self.run(wdb.open_requests)
        lines = []
        for r in reqs:
            p = await self.run(wdb.get_prize, r.prize_key)
            link = (f" · [mensaje](https://discord.com/channels/{self.guild_id}/{r.channel_id}/{r.message_id})" if r.message_id else "")
            lines.append(f"`#{r.id}` <@{r.user_id}> **{p.name if p else r.prize_key}** — {wdb.STATUS_LABELS.get(r.status)}{link}")
        await _say(interaction, embed=discord.Embed(title="📋 Encargos de Wujupón abiertos", color=GOLD,
                                                    description="\n".join(lines)[:4000] or "No hay encargos abiertos."))

    @wj.command(name="historial", description="(Council) Últimas tiradas (de todos o de una persona)")
    async def history(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if not await self._council(interaction):
            return
        rows = await self.run(wdb.recent_pulls, usuario.id if usuario else None, 25)
        prizes = {p.key: p for p in await self.run(wdb.list_prizes)}
        lines = [f"`{r['created_at'][:16].replace('T', ' ')}` <@{r['user_id']}> {wdb.RARITIES[r['rarity']][1]} "
                 f"{prizes[r['prize_key']].name if r['prize_key'] in prizes else r['prize_key']} · carta `#{r['card_id']}`"
                 for r in rows]
        await _say(interaction, embed=discord.Embed(title="🧾 Historial de tiradas", color=GOLD,
                                                    description="\n".join(lines)[:4000] or "Sin tiradas."))

    @wj.command(name="registro", description="(Council) Últimas operaciones administrativas de Wujupón")
    async def admin_log(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        rows = await self.run(wdb.recent_log, 25)
        lines = [f"`{r['created_at'][:16].replace('T', ' ')}` <@{r['actor_id']}> **{r['action']}**"
                 + (f" → <@{r['user_id']}>" if r["user_id"] else "") + (f" · {r['ref']}" if r["ref"] else "")
                 + (f" · {r['detail'][:80]}" if r["detail"] else "") for r in rows]
        await _say(interaction, embed=discord.Embed(title="🗂️ Registro de Wujupón", color=GOLD,
                                                    description="\n".join(lines)[:4000] or "Sin operaciones."))

    @wj.command(name="carta-probabilidad", description="(Council) Cambia la probabilidad y/o la rareza (tier) de una carta")
    @app_commands.describe(carta="Carta", probabilidad="Probabilidad nueva en % (ej: 0,5)", rareza="Rareza nueva (mover de tier)")
    @app_commands.choices(rareza=[app_commands.Choice(name=f"{e} {n}", value=k) for k, (n, e, _) in wdb.RARITIES.items()])
    @app_commands.autocomplete(carta=prize_autocomplete)
    async def odds_card(self, interaction: discord.Interaction, carta: str, probabilidad: Optional[str] = None,
                        rareza: Optional[app_commands.Choice[str]] = None):
        if not await self._council(interaction):
            return
        try:
            weight = wdb.parse_chance(probabilidad) if probabilidad else None
            before, after = await self.run(wdb.set_prize_odds, carta, actor_id=interaction.user.id, weight=weight,
                                           rarity=rareza.value if rareza else None)
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        totals = await self.run(wdb.rarity_totals)
        total = sum(totals.values())
        lines = [f"✅ **{after.name}** actualizada."]
        if weight is not None:
            lines.append(f"Probabilidad: {wdb.chance_text(before.weight)} → **{wdb.chance_text(after.weight)}**")
        if rareza is not None:
            lines.append(f"Rareza: {before.rarity_label} → **{after.rarity_label}**")
        lines.append("Rarezas ahora: " + " · ".join(f"{e} {wdb.chance_text(totals.get(k, 0))}" for k, (_, e, _c) in wdb.RARITIES.items()))
        if total != wdb.TOTAL_WEIGHT:
            lines.append(f"⚠️ Las cartas habilitadas suman **{wdb.chance_text(total)}** (no 100 %): cada carta sale en proporción a su "
                         f"peso, así que la chance real de esta es **{wdb.chance_text(after.weight, total)}**. Ajustá otras para volver al 100 %.")
        await _say(interaction, "\n".join(lines))
        await self.refresh_panel()

    @wj.command(name="precio", description="(Council) Cambia el precio de cada tirada")
    @app_commands.describe(cantidad="Wuju Coins por tirada (ej: 500.000)")
    async def set_price(self, interaction: discord.Interaction, cantidad: str):
        if not await self._council(interaction):
            return
        try:
            price = eco.parse_amount(cantidad)
        except eco.AmountError as exc:
            return await _say(interaction, f"❌ {exc}")
        if price > 10 ** 12:
            return await _say(interaction, "❌ Precio demasiado alto.")
        old = await self.run(wdb.get_price)
        await self.run(wdb.set_setting, "price", str(price))
        await self.run(wdb.log_action, interaction.user.id, "set_price", detail=f"{old} → {price}")
        await _say(interaction, f"✅ Precio por tirada: {money(old)} → **{money(price)}**. Las tiradas ya compradas no cambian.")
        await self.refresh_panel()

    @wj.command(name="limite-diario", description="(Council) Máximo de tiradas que cada persona puede comprar por día (0 = sin límite)")
    @app_commands.describe(cantidad="Tiradas por persona por día UTC (0 = sin límite)")
    async def set_daily_limit(self, interaction: discord.Interaction, cantidad: int):
        if not await self._council(interaction):
            return
        if not 0 <= cantidad <= 10_000:
            return await _say(interaction, "❌ Poné un número entre 0 y 10.000.")
        await self.run(wdb.set_setting, "daily_limit", str(cantidad) if cantidad else None)
        await self.run(wdb.log_action, interaction.user.id, "set_daily_limit", detail=str(cantidad))
        await _say(interaction, f"✅ Límite diario de compra: **{cantidad}** tirada(s) por persona (se renueva a las 00:00 UTC). "
                                "Las tiradas regaladas por el Council no cuentan." if cantidad
                   else "✅ Sin límite diario de compra.")
        await self.refresh_panel()

    @wj.command(name="publicar-premios", description="(Admin) Publica en el canal un mensaje público con la lista de premios")
    @app_commands.describe(canal="Canal donde publicarla (por defecto, este canal)")
    async def publish_prizes(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador puede publicar la lista de premios.")
        target = canal or interaction.channel
        prizes = await self.run(wdb.list_prizes)
        price = await self.run(wdb.get_price)
        groups = pack_embeds(prizes_embeds(prizes, price))
        if not groups:
            return await _say(interaction, "❌ No hay premios habilitados.")
        try:
            for n, group in enumerate(groups):
                await target.send("📜 **Premios de Wujupón**" if n == 0 else None, embeds=group, allowed_mentions=NO_MENTIONS)
        except Exception:
            traceback.print_exc()
            return await _say(interaction, f"❌ No pude publicar en {target.mention} (¿tengo permiso para enviar mensajes con embeds?).")
        await self.run(wdb.log_action, interaction.user.id, "publish_prizes", ref=str(target.id))
        await _say(interaction, f"✅ Lista de premios publicada en {target.mention}. Si después cambiás cartas o probabilidades, "
                                "volvé a publicarla: este mensaje no se actualiza solo.")

    @wj.command(name="foto-premio", description="(Admin) Carga o cambia la imagen del premio «Foto de la cola de Gendarme»")
    @app_commands.describe(imagen="Imagen (png, jpg, gif o webp; hasta 8 MB)", quitar="Sí = borrar la imagen cargada")
    async def set_photo(self, interaction: discord.Interaction, imagen: Optional[discord.Attachment] = None,
                        quitar: Optional[bool] = False):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador puede cargar la imagen del premio.")
        if quitar:
            done = await self.run(wdb.delete_asset, wdb.PHOTO_ASSET, interaction.user.id)
            return await _say(interaction, "🗑️ Imagen borrada. El premio no se puede canjear hasta que cargues otra." if done
                              else "ℹ️ No había ninguna imagen cargada.")
        if imagen is None:
            has = await self.run(wdb.get_asset, wdb.PHOTO_ASSET)
            return await _say(interaction, "✅ Hay una imagen cargada. Para cambiarla, adjuntá otra en `imagen`." if has
                              else "❌ Adjuntá la imagen en `imagen`.")
        name = (getattr(imagen, "filename", "") or "foto.png").lower()
        ctype = (getattr(imagen, "content_type", "") or "").lower()
        if not (ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
            return await _say(interaction, "❌ El archivo tiene que ser una imagen (png, jpg, gif o webp).")
        if (getattr(imagen, "size", 0) or 0) > wdb.MAX_ASSET_BYTES:
            return await _say(interaction, "❌ La imagen tiene que pesar menos de 8 MB.")
        try:
            data = await imagen.read()
            ext = name.rsplit(".", 1)[-1] if "." in name else "png"
            await self.run(wdb.set_asset, wdb.PHOTO_ASSET, filename=f"premio.{ext}", content=data, actor_id=interaction.user.id)
        except wdb.WujuponError as exc:
            return await _say(interaction, f"❌ {exc}")
        except Exception:
            traceback.print_exc()
            return await _say(interaction, "❌ No pude leer la imagen. Probá de nuevo.")
        nsfw = await self.run(wdb.get_setting, "nsfw_channel")
        await _say(interaction, "✅ Imagen cargada. Se guarda solo en la base del bot (no en GitHub)."
                   + ("" if nsfw else "\n⚠️ Falta el canal +18: configuralo con `/wujupon canal-nsfw`."))

    @wj.command(name="canal-nsfw", description="(Admin) Canal +18 donde se entregan los premios con foto")
    @app_commands.describe(canal="Canal marcado como +18 (por defecto, este canal)")
    async def set_nsfw_channel(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador.")
        target = canal or interaction.channel
        if not self._is_nsfw(target):
            return await _say(interaction, f"❌ {target.mention} no está marcado como canal +18. Activalo en la configuración del canal "
                                           "(«Canal con restricción de edad») y probá de nuevo.")
        await self.run(wdb.set_setting, "nsfw_channel", str(target.id))
        await _say(interaction, f"🔞 Los premios con foto se entregan en {target.mention}.")

    @wj.command(name="pausa", description="(Admin) Pausa o reanuda Wujupón (comprar, tirar y canjear)")
    @app_commands.describe(pausado="Sí = pausar · No = reanudar", motivo="Motivo que ven los jugadores (opcional)")
    async def pause(self, interaction: discord.Interaction, pausado: bool, motivo: Optional[str] = None):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador puede pausar Wujupón.")
        was = bool(await self.run(wdb.get_setting, "paused"))
        if pausado:
            await self.run(wdb.set_setting, "paused", "1")
            await self.run(wdb.set_setting, "paused_reason", (motivo or "").strip()[:300] or None)
        else:
            await self.run(wdb.set_setting, "paused", None)
            await self.run(wdb.set_setting, "paused_reason", None)
        await self.run(wdb.log_action, interaction.user.id, "pause" if pausado else "resume", detail=(motivo or "")[:300])
        await self.refresh_panel()
        if pausado:
            msg = ("⏸️ Wujupón pausado." if not was else "⏸️ Wujupón ya estaba pausado (motivo actualizado).")
            msg += " Nadie puede comprar, tirar ni canjear. Se puede ver el inventario y el Council sigue resolviendo encargos."
        else:
            msg = "▶️ Wujupón reanudado." if was else "ℹ️ Wujupón no estaba pausado."
        await _say(interaction, msg)

    @wj.command(name="limpiar-cartas", description="(Admin) Borra TODAS las cartas de todos los jugadores")
    @app_commands.describe(tambien_tiradas="Sí = también deja en 0 las tiradas sin usar de todos")
    async def wipe(self, interaction: discord.Interaction, tambien_tiradas: Optional[bool] = False):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador puede limpiar todas las cartas.")
        st = await self.run(wdb.get_stats)
        await _say(interaction, "⚠️ **Esto borra todas las cartas sin usar de todos los jugadores**"
                                + (" y **deja en 0 sus tiradas**" if tambien_tiradas else "")
                                + f". Los encargos abiertos ({st.open_requests}) se anulan. No se puede deshacer.\n"
                                  "El historial de tiradas y canjes se conserva. ¿Confirmás?",
                   view=WipeConfirmView(self, interaction.user.id, bool(tambien_tiradas)))

    async def do_wipe(self, interaction: discord.Interaction, include_spins: bool):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador.")
        res = await self.run(wdb.wipe_all_cards, actor_id=interaction.user.id, include_spins=include_spins)
        await _say(interaction, f"🧹 Listo: se borraron **{res.cards}** carta(s) de **{res.users}** persona(s) y se anularon "
                                f"**{len(res.requests)}** encargo(s)." + (f" Tiradas puestas en 0: **{res.spins}**." if include_spins else ""))
        for req in res.requests:
            await self._refresh_request_by_id(req.id)

    @wj.command(name="cooldown-cobro", description="(Council) Suma, saca o muestra el cooldown de «Cobrar balance» de alguien")
    @app_commands.describe(usuario="Persona", accion="Sumar horas, sacar todo el cooldown o ver cuánto le falta",
                           horas="Horas a sumar (solo para «Sumar»)")
    @app_commands.choices(accion=[app_commands.Choice(name="Sumar horas", value="sumar"),
                                  app_commands.Choice(name="Sacar cooldown", value="quitar"),
                                  app_commands.Choice(name="Ver", value="ver")])
    async def sell_cooldown_cmd(self, interaction: discord.Interaction, usuario: discord.Member,
                                accion: app_commands.Choice[str], horas: Optional[int] = None):
        if not await self._council(interaction):
            return
        import deposits_db as ddb
        gid = interaction.guild.id
        try:
            if accion.value == "sumar":
                if not horas:
                    return await _say(interaction, "❌ Indicá cuántas horas sumar (ej: `horas: 8`).")
                left = await asyncio.to_thread(ddb.add_sell_cooldown, self.db_path, gid, usuario.id, horas)
                await self.run(wdb.log_action, interaction.user.id, "sell_cooldown_add", usuario.id, detail=f"+{horas} h")
                return await _say(interaction, f"⏳ Se le sumaron **{horas} h** a {usuario.mention}. "
                                               f"Puede volver a cobrar en **{_hm(left)}**.")
            if accion.value == "quitar":
                had = await asyncio.to_thread(ddb.clear_sell_cooldown, self.db_path, gid, usuario.id)
                if had:
                    await self.run(wdb.log_action, interaction.user.id, "sell_cooldown_clear", usuario.id)
                return await _say(interaction, f"✅ {usuario.mention} ya puede cobrar balance." if had
                                  else f"ℹ️ {usuario.mention} no tenía cooldown.")
            left = await asyncio.to_thread(ddb.sell_cooldown_remaining, self.db_path, gid, usuario.id)
            await _say(interaction, f"⏳ A {usuario.mention} le faltan **{_hm(left)}** para cobrar." if left
                       else f"✅ {usuario.mention} no tiene cooldown para cobrar.")
        except ddb.DepositError as exc:
            await _say(interaction, f"❌ {exc}")

    @wj.command(name="premio", description="(Council) Habilita o deshabilita un premio")
    @app_commands.describe(premio="Premio", activo="Habilitado (sí/no)")
    @app_commands.autocomplete(premio=prize_autocomplete)
    async def toggle_prize(self, interaction: discord.Interaction, premio: str, activo: bool):
        if not await self._council(interaction):
            return
        p = await self.run(wdb.get_prize, premio)
        if p is None:
            return await _say(interaction, "❌ Ese premio no existe.")
        changed = await self.run(wdb.set_prize_enabled, premio, activo, interaction.user.id)
        state = "habilitado" if activo else "deshabilitado"
        await _say(interaction, (f"✅ **{p.name}** {state}." if changed else f"ℹ️ **{p.name}** ya estaba {state}.")
                   + ("\n⚠️ Mientras esté deshabilitado no sale en las tiradas: las probabilidades del resto se reparten sobre lo habilitado."
                      if changed and not activo else ""))

    estad = app_commands.Group(name="estadisticas", description="(Admin) Estadísticas de lo que entregó Wujupón", parent=wj)

    @estad.command(name="monedas", description="(Admin) Wuju Coins y embo que dio Wujupón (menos lo que sacaron las cartas malas)")
    @app_commands.describe(dias="Solo los últimos N días (vacío = desde siempre)")
    async def stats_money(self, interaction: discord.Interaction, dias: Optional[int] = None):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador.")
        wuju, embo = await self.run(wdb.money_stats, dias)
        f = eco.format_amount
        emb = discord.Embed(title="💰 Wujupón — Wuju Coins y embo", color=GOLD)
        emb.add_field(name="🪙 Wuju Coins", value=(f"Dados: **{f(wuju.given)}** ({wuju.cards_given} cartas)\n"
                                                   f"Quitados (☠️): **−{f(wuju.removed)}** ({wuju.cards_removed} cartas)\n"
                                                   f"**Neto: {f(wuju.net)}**"), inline=True)
        emb.add_field(name="🍾 Embo", value=(f"Dados: **{f(embo.given)}** ({embo.cards_given} cartas)\n"
                                            f"Quitados (☠️): **−{f(embo.removed)}** ({embo.cards_removed} cartas)\n"
                                            f"**Neto: {f(embo.net)}** (≈ {f(embo.net * wdb.EMBO_SILVER_VALUE)} silver)"), inline=True)
        emb.add_field(name="Total en silver", value=f"**{f(wuju.net + embo.net * wdb.EMBO_SILVER_VALUE)}** (Wuju 1:1 · embo × "
                                                    f"{f(wdb.EMBO_SILVER_VALUE)})", inline=False)
        emb.set_footer(text=f"Últimos {dias} días" if dias else "Desde siempre")
        await _say(interaction, embed=emb)

    @estad.command(name="premios", description="(Admin) Premios entregados con su costo máximo")
    @app_commands.describe(dias="Solo los últimos N días (vacío = desde siempre)")
    async def stats_prizes(self, interaction: discord.Interaction, dias: Optional[int] = None):
        if not is_admin(interaction.user):
            return await _say(interaction, "❌ Solo el dueño del servidor o un administrador.")
        rows = await self.run(wdb.delivered_prizes, dias)
        f = eco.format_amount
        totals: dict = {}
        priced, free = [], []
        for r in rows:
            if r.prize.cost and r.prize.cost_currency:
                totals[r.prize.cost_currency] = totals.get(r.prize.cost_currency, 0) + r.total
                cur = "VAL" if r.prize.cost_currency == "val" else "silver"
                priced.append(f"{wdb.RARITIES[r.prize.rarity][1]} **{r.prize.name}** ×{r.count} · {f(r.prize.cost)} c/u → "
                              f"**{f(r.total)}** {cur}")
            else:
                free.append(f"{r.prize.name} ×{r.count}")
        emb = discord.Embed(title="🎁 Wujupón — premios entregados (costo máximo)", color=GOLD)
        emb.description = "\n".join(priced)[:3500] or "Todavía no se entregó ningún premio con costo."
        emb.add_field(name="Total", value=f"**{f(totals.get('silver', 0))}** silver" +
                      (f" · **{f(totals['val'])}** VAL" if totals.get("val") else ""), inline=False)
        if free:
            emb.add_field(name="Sin costo declarado", value=", ".join(free)[:1000], inline=False)
        emb.set_footer(text=("Últimos %d días · " % dias if dias else "Desde siempre · ")
                       + "Wuju Coins y embo van en /wujupon estadisticas monedas")
        await _say(interaction, embed=emb)

    @wj.command(name="stats", description="(Council) Ingresos, costos entregados y pendientes de Wujupón")
    async def stats(self, interaction: discord.Interaction):
        if not await self._council(interaction):
            return
        st = await self.run(wdb.get_stats)
        prizes = await self.run(wdb.list_prizes, True)
        price = await self.run(wdb.get_price)
        exp = wdb.expected_cost(prizes)
        exp_silver = exp.get("wuju", 0) + exp.get("silver", 0) + exp.get("embo", 0) * wdb.EMBO_SILVER_VALUE
        emb = discord.Embed(title="📊 Wujupón — economía", color=GOLD)
        emb.add_field(name="Tiradas", value=f"Hechas: **{st.pulls}**\nCompradas: {st.spins_bought}\nRegaladas: {st.spins_gifted}\n"
                                            f"Sin usar: {st.spins_unused}", inline=True)
        emb.add_field(name="Por rareza", value="\n".join(f"{e} {n}: {st.by_rarity.get(k, 0)}" for k, (n, e, _) in wdb.RARITIES.items()),
                      inline=True)
        emb.add_field(name="Ingresos", value=f"**{money(st.wuju_spent)}**", inline=False)

        def fmt(d):
            parts = [f"Wuju: {eco.format_amount(d.get('wuju', 0))}", f"Silver: {eco.format_amount(d.get('silver', 0))}",
                     f"Embo: {eco.format_amount(d.get('embo', 0))}", f"VAL: {eco.format_amount(d.get('val', 0))}"]
            return "\n".join(parts) + f"\n≈ **{eco.format_amount(wdb.silver_equivalent(d))}** silver (sin VAL)"
        emb.add_field(name="Entregado", value=fmt(st.paid), inline=True)
        emb.add_field(name="Pendiente (cartas sin canjear)", value=fmt(st.pending), inline=True)
        real = (wdb.silver_equivalent(st.paid) / st.pulls) if st.pulls else 0
        emb.add_field(name="Costo por tirada", value=f"Esperado: **{eco.format_amount(int(exp_silver))}** silver + "
                                                     f"{eco.format_amount(int(exp.get('val', 0)))} VAL\n"
                                                     f"Real entregado: {eco.format_amount(int(real))} silver\n"
                                                     f"Precio: {eco.format_amount(price)}", inline=False)
        emb.set_footer(text=f"Encargos abiertos: {st.open_requests} · Wuju = silver 1:1 · 1 embo = {eco.format_amount(wdb.EMBO_SILVER_VALUE)} · "
                            "premios sin costo declarado cuentan 0")
        await _say(interaction, embed=emb)


async def setup_wujupon(bot: commands.Bot, db_path: str, guild_id: int) -> WujuponCog:
    cog = WujuponCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
