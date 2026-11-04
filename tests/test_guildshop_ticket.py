"""Ticket de descuento de Wujupón en el Guild Shop: 20 % en vez de 10 % sobre los primeros 50M.

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
import guildshop_db as gdb  # noqa: E402
import wujupon_db as wdb  # noqa: E402


class OfferMathTests(unittest.TestCase):
    def test_hasta_50m_es_20(self):
        self.assertEqual(gdb.offer_with_ticket(10_000_000), (2000, 8_000_000))
        self.assertEqual(gdb.offer_with_ticket(50_000_000), (2000, 40_000_000))

    def test_lo_que_pasa_de_50m_va_al_10(self):
        bp, net = gdb.offer_with_ticket(100_000_000)
        self.assertEqual(net, 40_000_000 + 45_000_000)
        self.assertEqual(bp, 1500)


class _Harness(tg.ShopDiscordTests):
    """Reusa el armado de las pruebas del Guild Shop sin volver a correr sus pruebas."""


for _name in [n for n in dir(tg.ShopDiscordTests) if n.startswith("test_")]:
    setattr(_Harness, _name, None)


class TicketDescuentoTests(_Harness):
    def setUp(self):
        super().setUp()
        wdb.init_schema(self.db)

    def give_ticket(self, user=None):
        return wdb.give_card(self.db, actor_id=1, user_id=(user or self.buyer).id, prize_key="shop_discount", reason="t")

    def question(self, ch):
        return next(m for m in ch.sent if "ticket(s) de descuento" in (m.content or ""))

    def choose(self, ch, user, use=True):
        i = self.inter(user, ch, self.question(ch))
        run(self.cog.on_discount_choice(i, use))
        return i

    def test_sin_ticket_no_pregunta(self):
        ch, _ = self.open_ticket()
        self.assertFalse(any("ticket(s) de descuento" in (m.content or "") for m in ch.sent))

    def test_usar_ticket_aplica_20_y_consume_la_carta(self):
        cid = self.give_ticket()
        self.fund(self.buyer, 100_000_000)
        ch, _ = self.open_ticket()
        q = self.question(ch)
        self.assertIn("20%", q.content)
        # solo el comprador elige
        i = self.choose(ch, self.seller)
        self.assertIn("Solo el comprador", i.text)
        self.choose(ch, self.buyer)
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        # el Seller pone 10 % pero el bot calcula el ticket
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "10")
        offer = ch.sent[-1].embeds[0]
        values = {n: v for n, v, _ in offer.fields}
        self.assertIn("20%", values["Descuento"])
        self.assertIn("8,000,000", values["A cobrar"])
        self.assertIn("🎟️ Ticket de descuento", values)
        self.confirm(ch, self.seller2)
        self.assertEqual(self.bal(self.buyer), 100_000_000 - 8_000_000)
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")
        self.assertEqual(wdb.get_stats(self.db).paid.get("silver"), 5_000_000)   # queda en las estadísticas de Wujupón

    def test_no_usar_deja_la_carta(self):
        cid = self.give_ticket()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer, use=False)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        self.confirm(ch, self.seller2)
        self.assertEqual(self.bal(self.buyer), 50_000_000 - 9_000_000)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_cerrar_sin_venta_devuelve_la_carta(self):
        cid = self.give_ticket()
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer)
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        t = gdb.get_ticket_by_channel(self.db, ch.id)
        self.assertTrue(gdb.close_ticket(self.db, t.id, self.seller.id))
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_no_se_puede_agregar_con_oferta_pendiente(self):
        self.give_ticket()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        question = self.question(ch)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        i = self.inter(self.buyer, ch, question)
        run(self.cog.on_discount_choice(i, True))
        self.assertIn("venta esperando confirmación", i.text)

    def test_si_la_carta_ya_no_esta_no_se_cobra(self):
        cid = self.give_ticket()
        self.fund(self.buyer, 50_000_000)
        ch, _ = self.open_ticket()
        self.choose(ch, self.buyer)
        self.start_sale(ch, self.seller, "wuju", "10.000.000", "")
        wdb.return_card(self.db, card_id=cid, actor_id=1, reason="bug")       # el Council la devolvió en el medio
        i = self.confirm(ch, self.seller2)
        self.assertIn("ya no está disponible", i.text)
        self.assertEqual(self.bal(self.buyer), 50_000_000)

    def test_canjear_desde_inventario_no_la_gasta(self):
        cid = self.give_ticket()
        r = wdb.redeem(self.db, card_id=cid, user_id=self.buyer.id, details="", request_key="r", guild_id=0)
        self.assertEqual(r.status, "no_effect")
        self.assertIn("Guild Shop", r.message)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")


if __name__ == "__main__":
    unittest.main()
