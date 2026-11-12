"""Tickets de descuento de Wujupón en el Guild Shop: 20 % hasta 20M y 30 % hasta 35M (el resto al 10 %).

Lógica con SQLite real y capa de Discord con el stub (sin red).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_guildshop as tg  # noqa: E402
from test_guildshop import run  # noqa: E402
import discord  # noqa: E402
import guildshop as gs  # noqa: E402
import guildshop_db as gdb  # noqa: E402
import wujupon as wj  # noqa: E402
import wujupon_db as wdb  # noqa: E402

T15, T20, T30 = "shop_discount_15", "shop_discount", "shop_discount_30"


class OfferMathTests(unittest.TestCase):
    def test_20_hasta_20m(self):
        self.assertEqual(gdb.offer_with_ticket(10_000_000, T20), (2000, 8_000_000))
        self.assertEqual(gdb.offer_with_ticket(20_000_000, T20), (2000, 16_000_000))
        bp, net = gdb.offer_with_ticket(40_000_000, T20)                # 20M al 20 % + 20M al 10 %
        self.assertEqual(net, 16_000_000 + 18_000_000)
        self.assertEqual(bp, 1500)

    def test_30_hasta_35m(self):
        self.assertEqual(gdb.offer_with_ticket(35_000_000, T30), (3000, 24_500_000))
        bp, net = gdb.offer_with_ticket(45_000_000, T30)                # 35M al 30 % + 10M al 10 %
        self.assertEqual(net, 24_500_000 + 9_000_000)

    def test_15_hasta_20m(self):
        self.assertEqual(gdb.offer_with_ticket(20_000_000, T15), (1500, 17_000_000))
        bp, net = gdb.offer_with_ticket(30_000_000, T15)                # 20M al 15 % + 10M al 10 %
        self.assertEqual(net, 17_000_000 + 9_000_000)

    def test_etiquetas(self):
        self.assertEqual(gdb.ticket_label(T15), "15% hasta 20,000,000")
        self.assertEqual(gdb.ticket_label(T20), "20% hasta 20,000,000")
        self.assertEqual(gdb.ticket_label(T30), "30% hasta 35,000,000")


class _Harness(tg.ShopDiscordTests):
    """Reusa el armado de las pruebas del Guild Shop sin volver a correr sus pruebas."""


for _name in [n for n in dir(tg.ShopDiscordTests) if n.startswith("test_")]:
    setattr(_Harness, _name, None)


class TicketDescuentoTests(_Harness):
    def setUp(self):
        super().setUp()
        wdb.init_schema(self.db)

    def give(self, key=T20, user=None):
        return wdb.give_card(self.db, actor_id=1, user_id=(user or self.buyer).id, prize_key=key, reason="t")

    def question(self, ch):
        return next(m for m in ch.sent if "tickets de descuento" in (m.content or ""))

    def choose(self, ch, user, key=T20):
        i = self.inter(user, ch, self.question(ch))
        run(self.cog.on_discount_choice(i, key))
        return i

    def offer_values(self, ch):
        return {n: v for n, v, _ in ch.sent[-1].embeds[0].fields}

    def test_sin_ticket_no_pregunta(self):
        ch, _ = self.open_ticket()
        self.assertFalse(any("tickets de descuento" in (m.content or "") for m in ch.sent))

    def test_solo_muestra_los_botones_de_los_tickets_que_tiene(self):
        self.give(T30)
        ch, _ = self.open_ticket()
        q = self.question(ch)
        ids = [c.custom_id for c in q.view.children]
        self.assertEqual(ids, ["gshop:ticket_use30", "gshop:ticket_skip"])
        self.assertIn("30% hasta 35,000,000", q.content)

    def test_boton_del_ticket_15(self):
        self.give(T15)
        ch, _ = self.open_ticket()
        q = self.question(ch)
        self.assertEqual([c.custom_id for c in q.view.children], ["gshop:ticket_use15", "gshop:ticket_skip"])

    def test_ticket_20_hasta_20m(self):
        cid = self.give(T20)
        self.fund(self.buyer, 100_000_000)
        ch, _ = self.open_ticket()
        i = self.choose(ch, self.seller)
        self.assertIn("Solo el comprador", i.text)
        self.choose(ch, self.buyer, T20)
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        self.start_sale(ch, self.seller, "wuju", "30.000.000", "10")      # el % que ponga el Seller se ignora
        v = self.offer_values(ch)
        self.assertIn("25,000,000", v["A cobrar"])                         # 20M al 20 % (16M) + 10M al 10 % (9M)
        self.assertIn("20% hasta 20,000,000", v["🎟️ Ticket de descuento"])
        self.confirm(ch, self.seller2)
        self.assertEqual(self.bal(self.buyer), 100_000_000 - 25_000_000)
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")
        self.assertEqual(wdb.get_stats(self.db).paid.get("silver"), 2_000_000)

    def test_ticket_30_con_los_dos_elige_el_30(self):
        c20, c30 = self.give(T20), self.give(T30)
        self.fund(self.buyer, 100_000_000)
        ch, _ = self.open_ticket()
        self.assertEqual(len(self.question(ch).view.children), 3)
        self.choose(ch, self.buyer, T30)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        self.assertIn("7,000,000", self.offer_values(ch)["A cobrar"])
        self.confirm(ch, self.seller2)
        self.assertEqual(wdb.get_card(self.db, c30).status, "consumed")
        self.assertEqual(wdb.get_card(self.db, c20).status, "available")

    def test_no_usar_deja_la_carta(self):
        cid = self.give()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer, None)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        self.confirm(ch, self.seller2)
        self.assertEqual(self.bal(self.buyer), 50_000_000 - 9_000_000)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_pagando_con_silver_no_pregunta_por_el_ticket(self):
        self.give()
        ch, _ = self.open_ticket(payment="silver")
        self.assertFalse(any("tickets de descuento" in (m.content or "") for m in ch.sent))

    def test_ticket_no_se_aplica_si_se_vende_por_silver(self):
        cid = self.give()
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer)
        self.start_sale(ch, self.seller, "silver", "10.000.000", "")
        v = self.offer_values(ch)
        self.assertIn("10,000,000", v["A cobrar"])                         # silver: sin descuento ni ticket
        self.assertNotIn("🎟️ Ticket de descuento", v)
        self.confirm(ch, self.seller2)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")    # la carta vuelve al inventario

    def test_cerrar_sin_venta_devuelve_la_carta(self):
        cid = self.give()
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer)
        t = gdb.get_ticket_by_channel(self.db, ch.id)
        self.assertTrue(gdb.close_ticket(self.db, t.id, self.seller.id))
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_no_se_puede_agregar_con_oferta_pendiente(self):
        self.give()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        question = self.question(ch)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        i = self.inter(self.buyer, ch, question)
        run(self.cog.on_discount_choice(i, T20))
        self.assertIn("venta esperando confirmación", i.text)

    def test_si_la_carta_ya_no_esta_no_se_cobra(self):
        cid = self.give()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        wdb.return_card(self.db, card_id=cid, actor_id=1, reason="bug")
        i = self.confirm(ch, self.seller2)
        self.assertIn("ya no está disponible", i.text)
        self.assertEqual(self.bal(self.buyer), 50_000_000)

    def test_canjear_desde_inventario_no_la_gasta(self):
        for key in (T20, T30):
            cid = self.give(key)
            r = wdb.redeem(self.db, card_id=cid, user_id=self.buyer.id, details="", request_key=f"r{key}", guild_id=0)
            self.assertEqual(r.status, "no_effect")
            self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_dar_ticket_solo_council(self):
        cog = run(wj.setup_wujupon(self.bot, self.db, tg.GUILD_ID))
        t30 = discord.app_commands.Choice(name="30 % hasta 35M", value=T30)
        i = self.inter(self.plain)
        run(cog.give_ticket.callback(cog, i, self.buyer, t30, 2, None))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(cog.give_ticket.callback(cog, i, self.buyer, t30, 2, "premio de evento"))
        self.assertIn("recibió **2**", i.text)
        self.assertEqual(gdb.discount_cards_available(self.db, self.buyer.id), {T30: 2})
        i = self.inter(self.council)
        run(cog.give_ticket.callback(cog, i, self.buyer, t30, 50, None))
        self.assertIn("entre 1 y 20", i.text)


if __name__ == "__main__":
    unittest.main()
