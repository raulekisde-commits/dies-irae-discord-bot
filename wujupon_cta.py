"""Cartas de Wujupón para las CTA (grupo B).

- «Anotación libre en CTA» (flex_cta): te podés anotar en cualquier posición aunque no tengas el rol (como Flex).
- «Slot libre en Party 1» (party1_slot): cualquier posición libre de la Party 1, sin el rol y sin la espera de Legacy.
  Las dos se usan solas: al anotarte en una posición que te bloquea, el bot te ofrece usar la carta EN ESA CTA.
  La carta queda reservada para esa CTA; si te bajás antes de que cierre (o se cancela) vuelve al inventario,
  y si la CTA termina con vos anotado, se consume.
- «Staff a tu elección en la CTA» (party_switch): al canjearla elegís una CTA abierta, alguien del Staff y una
  posición libre; el bot lo anota ahí (fijado, aunque no tenga el rol). Si ya estaba anotado, se lo mueve.
"""
import sqlite3
from typing import Callable, List, Optional, Tuple

import discord

from dbutil import connect, utcnow_iso, write_tx

PASS_FREE = "flex_cta"
PASS_P1 = "party1_slot"
STAFF_CARD = "party_switch"
PASS_KEYS = (PASS_FREE, PASS_P1)
NO_MENTIONS = discord.AllowedMentions.none()

SCHEMA = """
CREATE TABLE IF NOT EXISTS wjp_cta_pass (
    card_id    INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    run_id     INTEGER NOT NULL,
    prize_key  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wjp_cta_pass ON wjp_cta_pass(user_id, run_id);
"""


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def covers(prize_key: str, party_no: int, need_legacy: bool, need_access: bool) -> bool:
    """¿Esta carta resuelve el bloqueo? Anotación libre: solo el rol. Party 1: rol y Legacy, solo en la Party 1."""
    if prize_key == PASS_FREE:
        return need_access and not need_legacy
    if prize_key == PASS_P1:
        return int(party_no) == 1 and (need_legacy or need_access)
    return False


def check(db_path: str, user_id: int, run_id: int, party_no: int, need_legacy: bool, need_access: bool) -> Tuple[str, Optional[int], Optional[str]]:
    """('bound', card, key) si ya tiene una carta reservada para esta CTA que lo cubre ·
    ('offer', card, key) si tiene una disponible que lo cubriría · ('none', None, None)."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        try:
            for r in conn.execute("SELECT card_id, prize_key FROM wjp_cta_pass WHERE user_id = ? AND run_id = ?", (user_id, run_id)):
                if covers(r["prize_key"], party_no, need_legacy, need_access):
                    return "bound", int(r["card_id"]), r["prize_key"]
            # se ofrece primero la más específica (Party 1) si sirve, así la Anotación libre queda para otra CTA
            for key in (PASS_P1, PASS_FREE):
                if not covers(key, party_no, need_legacy, need_access):
                    continue
                r = conn.execute("SELECT id FROM wjp_cards WHERE user_id = ? AND prize_key = ? AND status = 'available' ORDER BY id LIMIT 1",
                                 (user_id, key)).fetchone()
                if r:
                    return "offer", int(r["id"]), key
        except sqlite3.OperationalError:
            pass
        return "none", None, None
    finally:
        conn.close()


def bind(db_path: str, card_id: int, user_id: int, run_id: int) -> bool:
    """Reserva la carta para esta CTA. False si ya no está disponible."""
    init_schema(db_path)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT prize_key FROM wjp_cards WHERE id = ? AND user_id = ? AND status = 'available'", (card_id, user_id)).fetchone()
            if r is None or r["prize_key"] not in PASS_KEYS:
                return False
            conn.execute("UPDATE wjp_cards SET status = 'reserved', updated_at = ? WHERE id = ?", (utcnow_iso(), card_id))
            conn.execute("INSERT INTO wjp_cta_pass(card_id, user_id, run_id, prize_key, created_at) VALUES (?,?,?,?,?)",
                         (card_id, user_id, run_id, r["prize_key"], utcnow_iso()))
            conn.execute("INSERT INTO wjp_log(actor_id, action, user_id, ref, detail, created_at) VALUES (?,?,?,?,?,?)",
                         (user_id, "cta_pass_bind", user_id, f"card:{card_id}", f"CTA #{run_id}", utcnow_iso()))
            return True
    finally:
        conn.close()


def settle(db_path: str) -> List[Tuple[int, int, str]]:
    """Resuelve las cartas reservadas: CTA terminada con la persona anotada → se consume; se bajó o la CTA se
    canceló → vuelve al inventario. Devuelve (carta, persona, 'consumed'|'released')."""
    init_schema(db_path)
    conn = connect(db_path)
    out = []
    try:
        with write_tx(conn):
            try:
                rows = conn.execute(
                    "SELECT p.card_id, p.user_id, p.run_id, p.prize_key, r.status, r.final_state, "
                    "EXISTS(SELECT 1 FROM cta_signups s WHERE s.cta_id = p.run_id AND s.user_id = p.user_id) AS signed "
                    "FROM wjp_cta_pass p LEFT JOIN cta_runs r ON r.id = p.run_id").fetchall()
            except sqlite3.OperationalError:
                return []
            now = utcnow_iso()
            for r in rows:
                status, final, signed = r["status"], r["final_state"], bool(r["signed"])
                if status == "open" and signed:
                    continue                                        # sigue en curso y anotado: queda reservada
                cancelled = status is None or status in ("cancelled", "error") or final == "cancelled"
                result = "consumed" if (signed and not cancelled) else "released"
                conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ? AND status = 'reserved'",
                             ("consumed" if result == "consumed" else "available", now, r["card_id"]))
                if result == "consumed":
                    conn.execute("INSERT OR IGNORE INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, "
                                 "created_at, updated_at) VALUES (?,?,?,?,?,'auto',?,?,?)",
                                 (f"cta:{r['card_id']}", r["card_id"], r["user_id"], r["prize_key"], f"CTA #{r['run_id']}",
                                  "Usada en la CTA", now, now))
                conn.execute("DELETE FROM wjp_cta_pass WHERE card_id = ?", (r["card_id"],))
                out.append((int(r["card_id"]), int(r["user_id"]), result))
        return out
    finally:
        conn.close()


def consume_staff_card(db_path: str, card_id: int, owner_id: int, note: str) -> bool:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            n = conn.execute("UPDATE wjp_cards SET status = 'consumed', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'available'",
                             (utcnow_iso(), card_id, owner_id)).rowcount
            if n:
                now = utcnow_iso()
                conn.execute("INSERT OR IGNORE INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, "
                             "created_at, updated_at) VALUES (?,?,?,?,?,'auto',?,?,?)",
                             (f"cta:{card_id}", card_id, owner_id, STAFF_CARD, "", note[:500], now, now))
                conn.execute("INSERT INTO wjp_log(actor_id, action, user_id, ref, detail, created_at) VALUES (?,?,?,?,?,?)",
                             (owner_id, "cta_staff_card", owner_id, f"card:{card_id}", note[:1000], now))
            return n == 1
    finally:
        conn.close()


# ============================================================
# Discord: oferta de usar la carta al anotarse
# ============================================================
class UsePassView(discord.ui.View):
    """Botón (solo para quien se anotó) para reservar la carta en esta CTA y reintentar la anotación."""

    def __init__(self, db_path: str, user_id: int, card_id: int, run_id: int, retry: Callable):
        super().__init__(timeout=120)
        self.db_path, self.user_id, self.card_id, self.run_id, self.retry = db_path, user_id, card_id, run_id, retry

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message("❌ Esta carta no es tuya.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="Usar la carta en esta CTA", style=discord.ButtonStyle.success, emoji="🎴")
    async def use(self, interaction, button):
        import asyncio
        ok = await asyncio.to_thread(bind, self.db_path, self.card_id, self.user_id, self.run_id)
        if not ok:
            return await interaction.response.edit_message(content="❌ Esa carta ya no está disponible.", view=None)
        await interaction.response.edit_message(content="🎴 Carta reservada para esta CTA. Si te bajás antes de que cierre, vuelve a tu "
                                                        "inventario.", view=None)
        await self.retry()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(content="Ok, no se usó la carta.", view=None)


async def offer_or_error(db_path: str, message, member, run_id: int, party_no: int, need_legacy: bool, need_access: bool,
                         error: str, retry: Callable) -> bool:
    """Devuelve True si la anotación puede seguir (ya tiene una carta reservada que lo cubre).
    Si no, responde el error y, si tiene una carta que lo resolvería, ofrece usarla."""
    import asyncio
    state, card_id, key = await asyncio.to_thread(check, db_path, member.id, run_id, party_no, need_legacy, need_access)
    if state == "bound":
        return True
    if state == "offer":
        name = "🎴 **Slot libre en Party 1**" if key == PASS_P1 else "🎴 **Anotación libre en CTA**"
        try:
            await message.reply(f"{error}\nTenés {name}: ¿la usás en esta CTA para anotarte igual?",
                                view=UsePassView(db_path, member.id, card_id, run_id, retry), mention_author=False,
                                allowed_mentions=NO_MENTIONS, delete_after=120)
        except Exception:
            pass
        return False
    try:
        await message.reply(error, mention_author=False, delete_after=12, allowed_mentions=NO_MENTIONS)
    except Exception:
        pass
    return False
