"""Pruebas del Guild Shop (lógica con SQLite real + capa de Discord con el stub, sin red).

NO prueban que la API real de Discord acepte los mensajes, permisos de canal o modales.
"""
import asyncio
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import economy as economy  # noqa: E402
import economy_db as eco  # noqa: E402
import guildshop as gs  # noqa: E402
import guildshop_db as gdb  # noqa: E402
import games_db  # noqa: E402

GUILD_ID = 2000000000000000001
MEMBER_ROLE = 1000000000000000001
STAFF_ROLE = 1000000000000000002
SELLER = gs.SELLER_ROLE_ID
COUNCIL = gs.COUNCIL_ROLE_ID
SHOP_CH = 3000000000000000001
LOG_CH = 3000000000000000002

_LOOP = asyncio.new_event_loop()


def run(coro):
    result = _LOOP.run_until_complete(coro)
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return result


# ============================================================
# Lógica
# ============================================================
class ShopDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        gdb.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, uid, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"f{uid}{os.urandom(3).hex()}")

    def ticket(self, buyer=100, channel=500, items="2x Espada"):
        return gdb.create_ticket(self.db, 1, channel, buyer, items)

    def pending(self, msg, ticket, seller, cur, total, disc="10"):
        gross, bp, net = gdb.build_offer(total, disc)
        gdb.save_pending(self.db, msg, ticket, seller, cur, gross, bp, net)
        return net

    def test_discount_parsing_and_default(self):
        self.assertEqual(gdb.parse_discount(""), 1000)
        self.assertEqual(gdb.parse_discount(None), 1000)
        self.assertEqual(gdb.parse_discount("10%"), 1000)
        self.assertEqual(gdb.parse_discount("0"), 0)
        self.assertEqual(gdb.parse_discount("7,5"), 750)
        self.assertEqual(gdb.parse_discount("100"), 10000)
        for bad in ("101", "-5", "abc", "10.555", "1e2"):
            with self.assertRaises(gdb.ShopError):
                gdb.parse_discount(bad)

    def test_net_calculation(self):
        self.assertEqual(gdb.compute_net(1000, 1000), 900)
        self.assertEqual(gdb.compute_net(1000, 0), 1000)
        self.assertEqual(gdb.compute_net(999, 1000), 899)          # 899,1 → 899
        self.assertEqual(gdb.compute_net(5, 1000), 5)               # 4,5 → 5 (mitades hacia arriba)
        self.assertEqual(gdb.build_offer("1,500,000", "")[2], 1350000)
        with self.assertRaises(gdb.ShopError):
            gdb.build_offer("100", "100")                           # cobrar 0
        with self.assertRaises(gdb.ShopError):
            gdb.build_offer("1.5", "10")
        with self.assertRaises(gdb.ShopError):
            gdb.build_offer("0", "10")

    def test_wuju_sale_debits_buyer_records_sale_and_marks_ticket(self):
        self.fund(100, 5000)
        t = self.ticket()
        net = self.pending(7001, t, 200, "wuju", "1000")
        self.assertEqual(net, 900)
        r = gdb.confirm_sale(self.db, 7001, 201)
        self.assertEqual((r.status, r.balance_before, r.balance_after), ("ok", 5000, 4100))
        self.assertEqual(eco.get_balance(self.db, 100), 4100)
        self.assertEqual(eco.get_balance(self.db, 200), 0)                  # el vendedor no recibe monedas
        self.assertEqual(gdb.get_ticket(self.db, t).status, "sold")
        row = eco.get_history(self.db, 100)[0]
        self.assertEqual((row.mov_type, row.delta, row.balance_after), ("guildshop_purchase", -900, 4100))
        self.assertIn("ticket", row.reason.lower())
        top = gdb.top_sellers(self.db, 1)
        self.assertEqual([(x.seller_id, x.total, x.wuju, x.silver, x.sales) for x in top], [(200, 900, 900, 0, 1)])
        self.assertEqual(eco.get_lifetime_deposits(self.db, 100), 5000)     # el histórico no cambia

    def test_double_confirm_charges_once(self):
        self.fund(100, 5000)
        t = self.ticket()
        self.pending(7001, t, 200, "wuju", "1000")
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 200).status, "ok")
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 200).status, "gone")
        self.assertEqual(eco.get_balance(self.db, 100), 4100)
        self.assertEqual(len(gdb.top_sellers(self.db, 1)), 1)

    def test_two_pending_for_same_ticket_only_one_sale(self):
        self.fund(100, 5000)
        t = self.ticket()
        self.pending(7001, t, 200, "wuju", "1000")
        self.pending(7002, t, 201, "wuju", "2000")
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 200).status, "ok")
        self.assertEqual(gdb.confirm_sale(self.db, 7002, 201).status, "gone")   # se limpiaron las demás
        self.assertEqual(eco.get_balance(self.db, 100), 4100)

    def test_insufficient_funds_changes_nothing_and_drops_pending(self):
        self.fund(100, 500)
        t = self.ticket()
        self.pending(7001, t, 200, "wuju", "1000")
        r = gdb.confirm_sale(self.db, 7001, 200)
        self.assertEqual((r.status, r.balance_before), ("funds", 500))
        self.assertEqual(r.pending.net, 900)
        self.assertEqual(eco.get_balance(self.db, 100), 500)
        self.assertEqual(gdb.top_sellers(self.db, 1), [])
        self.assertEqual(gdb.get_ticket(self.db, t).status, "open")
        self.assertIsNone(gdb.get_pending(self.db, 7001))
        self.assertEqual(len(eco.get_history(self.db, 100)), 1)              # solo la carga inicial

    def test_buyer_without_account_cannot_buy(self):
        t = self.ticket(buyer=999)
        self.pending(7001, t, 200, "wuju", "10")
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 200).status, "funds")
        self.assertEqual(eco.get_balance(self.db, 999), 0)

    def test_silver_sale_does_not_touch_balances(self):
        self.fund(100, 5000)
        t = self.ticket()
        self.pending(7001, t, 200, "silver", "2.000.000", "")
        r = gdb.confirm_sale(self.db, 7001, 200)
        self.assertEqual(r.status, "ok")
        self.assertEqual(eco.get_balance(self.db, 100), 5000)
        top = gdb.top_sellers(self.db, 1)[0]
        self.assertEqual((top.total, top.wuju, top.silver), (1800000, 0, 1800000))

    def test_custom_discount_applies(self):
        t = self.ticket()
        net = self.pending(7001, t, 200, "silver", "1000", "25")
        self.assertEqual(net, 750)

    def test_closed_ticket_cannot_be_sold_and_close_is_idempotent(self):
        self.fund(100, 5000)
        t = self.ticket()
        self.pending(7001, t, 200, "wuju", "1000")
        self.assertTrue(gdb.close_ticket(self.db, t, 300))
        self.assertFalse(gdb.close_ticket(self.db, t, 300))
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 200).status, "gone")   # el cierre limpió las pendientes
        self.assertEqual(eco.get_balance(self.db, 100), 5000)

    def test_sold_ticket_cannot_be_closed_or_sold_again(self):
        self.fund(100, 5000)
        t = self.ticket()
        self.pending(7001, t, 200, "silver", "1000")
        gdb.confirm_sale(self.db, 7001, 200)
        self.assertFalse(gdb.close_ticket(self.db, t, 300))
        self.pending(7002, t, 200, "wuju", "1000")
        self.assertEqual(gdb.confirm_sale(self.db, 7002, 200).status, "closed")
        self.assertEqual(eco.get_balance(self.db, 100), 5000)

    def test_seller_cannot_sell_to_self(self):
        self.fund(100, 5000)
        t = self.ticket(buyer=100)
        self.pending(7001, t, 100, "wuju", "1000")
        self.assertEqual(gdb.confirm_sale(self.db, 7001, 100).status, "self")
        self.assertEqual(eco.get_balance(self.db, 100), 5000)
        self.assertEqual(gdb.top_sellers(self.db, 1), [])

    def test_cancel_pending_once(self):
        t = self.ticket()
        self.pending(7001, t, 200, "wuju", "1000")
        self.assertEqual(gdb.cancel_pending(self.db, 7001).net, 900)
        self.assertIsNone(gdb.cancel_pending(self.db, 7001))

    def test_top_sellers_order_sum_and_guild_scope(self):
        specs = [(200, "silver", "1000"), (201, "silver", "5000"), (200, "silver", "10000")]
        for i, (seller, cur, total) in enumerate(specs):
            t = self.ticket(buyer=100 + i, channel=600 + i)
            self.pending(8000 + i, t, seller, cur, total, "0")
            self.assertEqual(gdb.confirm_sale(self.db, 8000 + i, seller).status, "ok")
        other = gdb.create_ticket(self.db, 2, 650, 150, "x")                    # otro servidor
        gdb.save_pending(self.db, 8100, other, 202, "silver", 99999, 0, 99999)
        gdb.confirm_sale(self.db, 8100, 202)
        top = gdb.top_sellers(self.db, 1)
        self.assertEqual([(x.seller_id, x.total, x.sales) for x in top], [(200, 11000, 2), (201, 5000, 1)])

    def test_open_ticket_lookup_and_discard(self):
        t = self.ticket(buyer=100)
        self.assertEqual(gdb.open_ticket_of(self.db, 1, 100).id, t)
        gdb.close_ticket(self.db, t, 1)
        self.assertIsNone(gdb.open_ticket_of(self.db, 1, 100))
        t2 = self.ticket(buyer=101, channel=501)
        gdb.discard_ticket(self.db, t2)
        self.assertIsNone(gdb.get_ticket(self.db, t2))

    def test_empty_items_rejected(self):
        with self.assertRaises(gdb.ShopError):
            gdb.create_ticket(self.db, 1, 1, 1, "   ")


# ============================================================
# Discord
# ============================================================
class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent, self.edits, self.modals = [], [], []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))

    async def edit_message(self, **kw):
        self.done = True
        self.edits.append(kw)

    async def defer(self, **kw):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append((content, kw))


class FakeInteraction:
    _n = 0

    def __init__(self, guild, user, channel, message=None):
        FakeInteraction._n += 1
        self.id = 6000000000000000000 + FakeInteraction._n
        self.guild, self.user, self.channel, self.message = guild, user, channel, message
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    @property
    def text(self):
        out = [str(c) for c, _ in self.response.sent if c]
        out += [str(e.get("content")) for e in self.response.edits if e.get("content")]
        return "\n".join(out)

    @property
    def modals(self):
        return self.response.modals


class ShopDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.bot.user = self.guild.me
        self.shopch = discord.TextChannel(SHOP_CH, self.guild, "shop")
        self.logch = discord.TextChannel(LOG_CH, self.guild, "log-shop")
        self.guild._channels = {SHOP_CH: self.shopch, LOG_CH: self.logch}
        self.bot.channels = dict(self.guild._channels)
        self.bot.guilds_map = {GUILD_ID: self.guild}
        self.r_seller = discord.Role(SELLER, "Seller", self.guild)
        self.r_council = discord.Role(COUNCIL, "Council", self.guild)
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.guild.roles = [self.r_seller, self.r_council, self.r_member]

        def mk(uid, name, roles=(), admin=False):
            m = discord.Member(uid + 100000000000000000, name, roles=list(roles), guild=self.guild, administrator=admin)
            self.guild.members.append(m)
            return m

        self.admin = mk(10, "Admin", admin=True)
        self.buyer = mk(20, "Comprador", [self.r_member])
        self.buyer2 = mk(21, "Comprador2", [self.r_member])
        self.seller = mk(30, "Vendedor", [self.r_seller])
        self.seller2 = mk(31, "Vendedor2", [self.r_seller])
        self.council = mk(32, "Consejero", [self.r_council])
        self.plain = mk(40, "Plain", [self.r_member])

        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.cog = run(gs.setup_guildshop(self.bot, self.db, GUILD_ID))
        self.cog.close_delay = 0
        self.cog.sale_close_delay = 0

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user, channel=None, message=None):
        return FakeInteraction(self.guild, user, channel or self.shopch, message)

    def fund(self, user, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[user.id], amount=amount, request_key=f"f{user.id}{os.urandom(3).hex()}")

    def bal(self, user):
        return eco.get_balance(self.db, user.id)

    def publish_panels(self):
        i = self.inter(self.council)
        run(self.cog.guild_shop(i, None))
        return i

    def open_ticket(self, user=None, items="2x Espada T8\n1x Capa T7", payment="wuju"):
        user = user or self.buyer
        i = self.inter(user)
        run(self.cog.on_buy_pressed(i))
        self.assertEqual(i.modals, [], i.text)                               # primero se pregunta cómo paga
        self.assertEqual([c.label for c in i.response.sent[0][1]["view"].children], ["Wuju Coins", "Silver"])
        i1 = self.inter(user)
        run(self.cog.on_payment_chosen(i1, payment))
        self.assertEqual(len(i1.modals), 1)
        self.assertEqual(i1.modals[0].payment, payment)
        i2 = self.inter(user)
        run(self.cog.on_buy_submitted(i2, items, payment))
        ch = self.guild.created_channels[-1]
        return ch, i2

    def start_sale(self, ch, seller, currency, total, discount="10"):
        i = self.inter(seller, ch)
        run(self.cog.on_sell_pressed(i, currency))
        self.assertEqual(len(i.modals), 1, i.text)
        modal = i.modals[0]
        j = self.inter(seller, ch)
        run(self.cog.on_sale_submitted(j, modal.ticket_id, currency, total, discount))
        return j

    def confirm(self, ch, user, press="confirm"):
        msg = ch.sent[-1]
        i = self.inter(user, ch, msg)
        run(self.cog.on_confirm_pressed(i) if press == "confirm" else self.cog.on_cancel_pressed(i))
        return i

    # ---------- comandos ----------
    def test_command_names_registered(self):
        names = {getattr(gs.GuildShopCog, n).name for n in dir(gs.GuildShopCog)
                 if isinstance(getattr(gs.GuildShopCog, n, None), discord_stub.AppCommand)}
        self.assertEqual(names, {"guildshop", "dados", "entradas", "salidas", "registro", "economia"})
        self.assertEqual(gs.GuildShopCog.canal.name, "canal")
        self.assertEqual(gs.GuildShopCog.canal_logs.name, "logs")

    def test_guild_shop_requires_council_or_admin_and_publishes_two_panels(self):
        i = self.inter(self.seller)
        run(self.cog.guild_shop(i, None))
        self.assertIn("Council", i.text)
        self.assertEqual(self.shopch.sent, [])
        i = self.publish_panels()
        self.assertEqual(len(self.shopch.sent), 2)
        top, shop = self.shopch.sent
        self.assertIn("Top Vendedores", top.embeds[0].title)
        self.assertIsNone(top.view)
        self.assertIn("10% de descuento", shop.embeds[0].description)
        self.assertIsNotNone(shop.view)
        self.assertEqual([c.label for c in shop.view.children], ["Comprar"])
        i = self.inter(self.admin)                                       # el administrador también puede
        other = discord.TextChannel(3000000000000000009, self.guild, "otro")
        run(self.cog.guild_shop(i, other))
        self.assertEqual(len(other.sent), 2)
        self.assertTrue(all(m.deleted for m in self.shopch.sent))        # reemplazó a los viejos

    def test_guild_shop_failure_leaves_no_orphan_panel(self):
        self.shopch.fail_send = True
        i = self.publish_panels()
        self.assertIn("No pude publicar", i.text)
        self.assertIsNone(gdb.get_panel(self.db, GUILD_ID, "top"))

    def test_logs_command_permissions_and_saving(self):
        i = self.inter(self.seller)
        run(self.cog.logs_guildshop.callback(self.cog, i, self.logch))
        self.assertIn("Council", i.text)
        self.assertIsNone(gdb.get_log_channel_id(self.db))
        i = self.inter(self.council)
        run(self.cog.logs_guildshop.callback(self.cog, i, self.logch))
        self.assertEqual(gdb.get_log_channel_id(self.db), LOG_CH)
        self.assertEqual(len(self.logch.sent), 1)
        self.logch.fail_send = True
        i = self.inter(self.council)
        run(self.cog.logs_guildshop.callback(self.cog, i, self.logch))
        self.assertIn("No tengo permiso", i.text)

    def test_canal_dados_command_saves_channel_for_dice_panels(self):
        target = discord.TextChannel(3000000000000000077, self.guild, "mesa-de-dados")
        i = self.inter(self.seller)
        run(self.cog.canal_dados.callback(self.cog, i, target))
        self.assertIn("Council", i.text)
        self.assertIsNone(games_db.get_panel_channel_id(self.db, "dice"))
        i = self.inter(self.council)
        run(self.cog.canal_dados.callback(self.cog, i, target))
        self.assertEqual(games_db.get_panel_channel_id(self.db, "dice"), target.id)
        self.assertEqual(len(target.sent), 1)
        self.assertIn("dados", i.text)
        target.fail_send = True
        other = discord.TextChannel(3000000000000000078, self.guild, "sin-permiso")
        other.fail_send = True
        i = self.inter(self.admin)
        run(self.cog.canal_dados.callback(self.cog, i, other))
        self.assertIn("No tengo permiso", i.text)
        self.assertEqual(games_db.get_panel_channel_id(self.db, "dice"), target.id)       # no cambió

    # ---------- comprar ----------
    def test_buy_opens_private_ticket_with_items_and_pings_seller(self):
        ch, reply = self.open_ticket()
        self.assertIn(ch.mention, reply.text)
        ow = ch.overwrites
        self.assertFalse(ow[self.guild.default_role].perms["view_channel"])
        self.assertTrue(ow[self.buyer].perms["view_channel"])
        self.assertTrue(ow[self.r_seller].perms["view_channel"])
        self.assertTrue(ow[self.r_council].perms["view_channel"])
        msg = ch.sent[0]
        self.assertIn(f"<@&{SELLER}>", msg.content)
        self.assertIn("2x Espada T8", msg.embeds[0].fields[1][1])
        self.assertEqual([c.label for c in msg.view.children],
                         ["Cerrar sin transacción", "Vender por Wuju Coins", "Vender por silver"])

    def test_ticket_shows_and_stores_chosen_payment_method(self):
        for payment, text in (("wuju", "Wuju Coins"), ("silver", "Silver")):
            buyer = self.buyer if payment == "wuju" else self.buyer2
            ch, _ = self.open_ticket(buyer, payment=payment)
            fields = {n: v for n, v, _ in ch.sent[0].embeds[0].fields}
            self.assertIn(text, fields["Quiere pagar con"])
            self.assertEqual(gdb.get_ticket_by_channel(self.db, ch.id).payment, payment)

    def test_payment_choice_buttons_belong_to_the_buyer(self):
        i = self.inter(self.buyer)
        run(self.cog.on_buy_pressed(i))
        view = i.response.sent[0][1]["view"]
        other = self.inter(self.plain)
        self.assertFalse(run(view.interaction_check(other)))
        self.assertTrue(run(view.interaction_check(self.inter(self.buyer))))

    def test_old_database_gets_payment_column(self):
        import sqlite3
        db = os.path.join(self.tmp.name, "old.sqlite3")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE gshop_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL UNIQUE, "
                     "buyer_id INTEGER NOT NULL, items TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, closed_at TEXT, closed_by INTEGER)")
        conn.execute("INSERT INTO gshop_tickets(guild_id, channel_id, buyer_id, items, created_at) VALUES (1,2,3,'x','now')")
        conn.commit()
        conn.close()
        gdb.init_schema(db)
        self.assertIsNone(gdb.get_ticket(db, 1).payment)
        self.assertEqual(gdb.create_ticket(db, 1, 9, 9, "y", "silver"), 2)

    def test_only_one_open_ticket_per_buyer(self):
        ch, _ = self.open_ticket()
        i = self.inter(self.buyer)
        run(self.cog.on_buy_pressed(i))
        self.assertEqual(i.modals, [])
        self.assertIn(ch.mention.replace("<#", "<#"), i.text)
        n = len(self.guild.created_channels)
        i = self.inter(self.buyer)
        run(self.cog.on_buy_submitted(i, "otra cosa"))
        self.assertEqual(len(self.guild.created_channels), n)

    def test_buy_modal_has_items_field_and_blank_is_rejected(self):
        modal = gs.BuyModal(self.cog)
        self.assertEqual(modal.items.label, "Lista de compra")
        i = self.inter(self.buyer)
        run(self.cog.on_buy_submitted(i, "   "))
        self.assertIn("vacía", i.text)
        self.assertFalse(getattr(self.guild, "created_channels", []))

    def test_channel_creation_failure_creates_no_ticket(self):
        self.guild.fail_create_channel = True
        i = self.inter(self.buyer)
        run(self.cog.on_buy_submitted(i, "algo"))
        self.assertIn("No pude crear el ticket", i.text)
        self.assertIsNone(gdb.open_ticket_of(self.db, GUILD_ID, self.buyer.id))

    # ---------- permisos de los botones ----------
    def test_buttons_are_for_seller_or_council_only(self):
        ch, _ = self.open_ticket()
        for user in (self.buyer, self.plain, self.admin):
            i = self.inter(user, ch)
            run(self.cog.on_sell_pressed(i, "wuju"))
            self.assertIn("Seller", i.text)
            self.assertEqual(i.modals, [])
            i = self.inter(user, ch)
            run(self.cog.on_close_pressed(i))
            self.assertIn("Seller", i.text)
            self.assertFalse(ch.deleted)
        i = self.inter(self.council, ch)
        run(self.cog.on_sell_pressed(i, "silver"))
        self.assertEqual(len(i.modals), 1)

    def test_sale_modal_permissions_rechecked_on_submit(self):
        ch, _ = self.open_ticket()
        t = gdb.get_ticket_by_channel(self.db, ch.id)
        i = self.inter(self.plain, ch)
        run(self.cog.on_sale_submitted(i, t.id, "silver", "1000", "10"))
        self.assertIn("Seller", i.text)
        self.assertEqual(len(ch.sent), 1)

    def test_confirm_and_cancel_are_seller_only(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        for press in ("confirm", "cancel"):
            i = self.confirm(ch, self.buyer, press)
            self.assertIn("Seller", i.text)
            i = self.confirm(ch, self.plain, press)
            self.assertIn("Seller", i.text)
        self.assertEqual(self.bal(self.buyer), 5000)

    # ---------- cerrar sin transacción ----------
    def test_close_without_transaction_deletes_channel_and_logs(self):
        gdb.set_log_channel_id(self.db, LOG_CH)
        ch, _ = self.open_ticket()
        i = self.inter(self.seller, ch)
        run(self.cog.on_close_pressed(i))
        self.assertTrue(ch.deleted)
        t = gdb.get_ticket(self.db, 1)
        self.assertEqual((t.status,), ("closed",))
        self.assertEqual(len(self.logch.sent), 1)
        self.assertIn("sin transacción", self.logch.sent[0].embeds[0].title)

    # ---------- vender por Wuju Coins ----------
    def test_wuju_sale_full_flow(self):
        gdb.set_log_channel_id(self.db, LOG_CH)
        self.fund(self.buyer, 5000)
        self.publish_panels()
        ch, _ = self.open_ticket()
        j = self.start_sale(ch, self.seller, "wuju", "1000")                 # descuento 10% por defecto
        self.assertIn("Pedí la confirmación", j.text)
        conf = ch.sent[-1]
        self.assertIn("A cobrar", [f[0] for f in conf.embeds[0].fields])
        self.assertTrue(any("900" in str(f[1]) for f in conf.embeds[0].fields))
        self.assertEqual([c.label for c in conf.view.children], ["Confirmar", "Cancelar"])
        self.assertEqual(self.bal(self.buyer), 5000)                          # todavía no se cobró
        i = self.confirm(ch, self.seller2)                                    # confirma OTRO seller
        self.assertEqual(self.bal(self.buyer), 4100)
        self.assertEqual(gdb.get_ticket(self.db, 1).status, "sold")
        top_msg = self.shopch.sent[0]
        self.assertIn(f"<@{self.seller.id}>", top_msg.embeds[0].description)  # la venta es de quien la inició
        self.assertIn("900", top_msg.embeds[0].description)
        self.assertEqual(len(self.logch.sent), 1)
        log = {n: v for n, v, _ in self.logch.sent[0].embeds[0].fields}
        self.assertIn("900", log["Cobrado"])
        self.assertIn("5,000", log["Balance comprador"])
        self.assertIsNone(ch.sent[-1].view)                                    # sin botón de cerrar
        self.assertTrue(ch.deleted)                                            # el canal se cerró solo al confirmar
        again = self.confirm(ch, self.seller)                                 # el último mensaje ya no es la confirmación
        self.assertEqual(self.bal(self.buyer), 4100)

    def test_wuju_sale_custom_discount(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "2.000", "25")
        self.confirm(ch, self.seller)
        self.assertEqual(self.bal(self.buyer), 5000 - 1500)

    def test_wuju_sale_rejected_upfront_when_balance_not_enough(self):
        self.fund(self.buyer, 800)
        ch, _ = self.open_ticket()
        n = len(ch.sent)
        j = self.start_sale(ch, self.seller, "wuju", "1000")
        self.assertIn("no le alcanza", j.text)
        self.assertEqual(len(ch.sent), n)                                      # no hay mensaje de confirmación
        self.assertEqual(self.bal(self.buyer), 800)

    def test_balance_checked_again_at_confirm_time(self):
        self.fund(self.buyer, 1000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.buyer.id], amount=500, request_key="gasto")
        i = self.confirm(ch, self.seller)
        self.assertEqual(self.bal(self.buyer), 500)
        self.assertEqual(gdb.get_ticket(self.db, 1).status, "open")
        self.assertTrue(any("saldo suficiente" in (e.get("content") or "") for e in i.response.edits))

    def test_invalid_values_are_explained_and_nothing_created(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        n = len(ch.sent)
        for total, disc in (("abc", "10"), ("1000", "150"), ("-5", "10"), ("1000", "xx"), ("0", "10")):
            j = self.start_sale(ch, self.seller, "wuju", total, disc)
            self.assertIn("❌", j.text, (total, disc))
        self.assertEqual(len(ch.sent), n)

    def test_seller_cannot_sell_to_himself(self):
        self.fund(self.seller, 5000)
        ch, _ = self.open_ticket(self.seller)
        i = self.inter(self.seller, ch)
        run(self.cog.on_sell_pressed(i, "wuju"))
        self.assertIn("vos mismo", i.text)
        self.assertEqual(i.modals, [])

    def test_cancel_keeps_ticket_open(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        i = self.confirm(ch, self.seller2, "cancel")
        self.assertEqual(self.bal(self.buyer), 5000)
        self.assertEqual(gdb.get_ticket(self.db, 1).status, "open")
        again = self.confirm(ch, self.seller, "confirm")                      # ya cancelada
        self.assertIn("ya fue procesada", again.text)
        self.assertEqual(self.bal(self.buyer), 5000)

    def test_double_press_confirm_charges_once(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        msg = ch.sent[-1]
        run(self.cog.on_confirm_pressed(self.inter(self.seller, ch, msg)))
        again = self.inter(self.seller2, ch, msg)
        run(self.cog.on_confirm_pressed(again))
        self.assertIn("ya fue procesada", again.text)
        self.assertEqual(self.bal(self.buyer), 4100)

    # ---------- vender por silver ----------
    def test_silver_sale_flow_does_not_touch_balance_and_counts_in_top(self):
        gdb.set_log_channel_id(self.db, LOG_CH)
        self.fund(self.buyer, 50)
        self.publish_panels()
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "silver", "3.000.000")
        conf = ch.sent[-1]
        self.assertIn("silver", conf.embeds[0].title)
        self.assertTrue(any("2,700,000" in str(f[1]) for f in conf.embeds[0].fields))
        self.assertEqual(gdb.top_sellers(self.db, GUILD_ID), [])              # nada hasta confirmar
        self.confirm(ch, self.seller)
        self.assertEqual(self.bal(self.buyer), 50)
        self.assertIn("2,700,000", self.shopch.sent[0].embeds[0].description)
        self.assertIn("silver", self.logch.sent[0].embeds[0].title)

    def test_silver_cancel_records_nothing(self):
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "silver", "1000")
        self.confirm(ch, self.seller, "cancel")
        self.assertEqual(gdb.top_sellers(self.db, GUILD_ID), [])

    # ---------- registro / paneles ----------
    def test_log_failure_does_not_undo_sale(self):
        gdb.set_log_channel_id(self.db, LOG_CH)
        self.logch.fail_send = True
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        self.confirm(ch, self.seller)
        self.assertEqual(self.bal(self.buyer), 4100)
        self.assertEqual(gdb.get_ticket(self.db, 1).status, "sold")

    def test_no_log_channel_configured_is_fine(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        self.confirm(ch, self.seller)
        self.assertEqual(self.logch.sent, [])
        self.assertEqual(gdb.get_ticket(self.db, 1).status, "sold")

    def test_top_embed_ranks_with_mentions_and_totals(self):
        rows = [gdb.SellerRow(11, 3000, 1000, 2000, 3), gdb.SellerRow(12, 500, 500, 0, 1)]
        emb = self.cog.top_embed(rows)
        lines = emb.description.split("\n")
        self.assertIn("<@11>", lines[0])
        self.assertIn("3,000", lines[0])
        self.assertIn("<@12>", lines[1])
        self.assertIn("_Todavía no hay ventas", self.cog.top_embed([]).description)

    def test_sold_ticket_channel_closes_itself_after_confirm(self):
        run(self.cog.logs_guildshop.callback(self.cog, self.inter(self.council), self.logch))
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        self.assertFalse(ch.deleted)
        self.confirm(ch, self.seller)
        self.assertTrue(ch.deleted)
        self.assertEqual(len(self.logch.sent), 2)                              # el aviso de config + el registro de la venta

    def test_cancelled_sale_keeps_channel_open(self):
        self.fund(self.buyer, 5000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000")
        self.assertFalse(ch.deleted)

    def test_persistent_views_registered(self):
        kinds = {type(v).__name__ for v in self.bot.persistent_views}
        self.assertTrue({"ShopPanelView", "TicketActionView", "ConfirmSaleView", "SoldTicketView"} <= kinds)


if __name__ == "__main__":
    unittest.main()
