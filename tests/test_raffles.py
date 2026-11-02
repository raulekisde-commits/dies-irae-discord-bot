import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402
import discord  # noqa: E402
import economy_db as eco  # noqa: E402
import raffles as rf  # noqa: E402
import raffles_db as rdb  # noqa: E402


class FixedRng:
    def __init__(self, n):
        self.n = n

    def randrange(self, total):
        return self.n % total


class AmountShortTests(unittest.TestCase):
    def test_abbreviations_only_on_input(self):
        for text, expected in (("1k", 1000), ("1m", 1_000_000), ("10m", 10_000_000), ("1M", 1_000_000), ("2b", 2_000_000_000),
                               ("1.5m", 1_500_000), ("1,5m", 1_500_000), ("2.25k", 2250), ("500k", 500_000)):
            self.assertEqual(eco.parse_amount_short(text), expected, text)
        for text, expected in (("150000", 150000), ("1.500.000", 1_500_000), ("1,500,000", 1_500_000)):   # lo de siempre sigue igual
            self.assertEqual(eco.parse_amount_short(text), expected)
        self.assertEqual(eco.format_amount(10_000_000), "10,000,000")                                    # se muestra como siempre

    def test_bad_abbreviations_are_rejected(self):
        for text in ("0m", "m", "1mm", "1.555m", "-1m", "1 m", "1e3", "999999999999999b"):
            with self.assertRaises(eco.AmountError, msg=text):
                eco.parse_amount_short(text)
        with self.assertRaises(eco.AmountError):
            eco.parse_amount("1m")                                                                       # parse_amount (economía) no cambia


class RaffleDbTests(DiscordBase):
    def setUp(self):
        super().setUp()
        rdb.init_schema(self.db)

    def mk(self, price=100, secs=3600, now=1000.0):
        return rdb.create_raffle(self.db, guild_id=1, channel_id=2, creator_id=3, prize="Espada T8", price=price, duration_seconds=secs, now=now)

    def test_parse_duration(self):
        self.assertEqual(rdb.parse_duration("01:30"), 5400)
        self.assertEqual(rdb.parse_duration("0:45"), 2700)
        for bad in ("90", "1:5", "1:60", "00:00", "169:00", "a:b", "1:30:00", ""):
            with self.assertRaises(rdb.RaffleError, msg=bad):
                rdb.parse_duration(bad)

    def test_buy_charges_and_counts(self):
        self.fund(self.alice, 1000)
        r = self.mk()
        res = rdb.buy_tickets(self.db, r.id, self.alice.id, 3, "k1", now=1001)
        self.assertEqual((res.status, res.mine, res.spent, res.balance), ("ok", 3, 300, 700))
        self.assertEqual(self.bal(self.alice), 700)
        self.assertEqual(rdb.get_raffle(self.db, r.id).tickets, {self.alice.id: 3})

    def test_same_purchase_key_never_charges_twice(self):
        self.fund(self.alice, 1000)
        r = self.mk()
        rdb.buy_tickets(self.db, r.id, self.alice.id, 1, "same", now=1001)
        self.assertEqual(rdb.buy_tickets(self.db, r.id, self.alice.id, 1, "same", now=1002).status, "duplicate")
        self.assertEqual(self.bal(self.alice), 900)
        self.assertEqual(rdb.get_raffle(self.db, r.id).tickets[self.alice.id], 1)

    def test_not_enough_balance_buys_nothing(self):
        self.fund(self.alice, 250)
        r = self.mk()
        res = rdb.buy_tickets(self.db, r.id, self.alice.id, 3, "k", now=1001)
        self.assertEqual((res.status, res.balance), ("funds", 250))
        self.assertEqual(self.bal(self.alice), 250)
        self.assertEqual(rdb.get_raffle(self.db, r.id).tickets, {})
        self.assertEqual(rdb.buy_tickets(self.db, r.id, self.alice.id, 2, "k", now=1002).status, "ok")   # la clave fallida no quedó "gastada"

    def test_per_person_limit_and_closed(self):
        self.fund(self.alice, 10 ** 6)
        r = self.mk(price=1)
        self.assertEqual(rdb.buy_tickets(self.db, r.id, self.alice.id, rdb.MAX_TICKETS_PER_USER, "a", now=1001).status, "ok")
        res = rdb.buy_tickets(self.db, r.id, self.alice.id, 1, "b", now=1002)
        self.assertEqual(res.status, "limit")
        self.assertEqual(self.bal(self.alice), 10 ** 6 - rdb.MAX_TICKETS_PER_USER)
        self.assertEqual(rdb.buy_tickets(self.db, r.id, self.alice.id, 1, "c", now=99999).status, "closed")

    def test_draw_is_weighted_once_and_only_when_due(self):
        for u, n in ((self.alice, 3), (self.bob, 1)):
            self.fund(u, 1000)
        r = self.mk()
        rdb.buy_tickets(self.db, r.id, self.alice.id, 3, "a", now=1001)
        rdb.buy_tickets(self.db, r.id, self.bob.id, 1, "b", now=1002)
        self.assertIsNone(rdb.draw(self.db, r.id, now=2000))                                   # todavía no vence
        done = rdb.draw(self.db, r.id, now=5000, rng=FixedRng(3))                              # ticket #4 de 4 → el último (Bob)
        self.assertEqual(done.winner_id, self.bob.id)
        self.assertEqual(rdb.get_raffle(self.db, r.id).winner_id, self.bob.id)
        self.assertEqual(done.status, "done")
        self.assertIsNone(rdb.draw(self.db, r.id, now=5001))                                   # nunca se sortea dos veces
        self.assertEqual(rdb.buy_tickets(self.db, r.id, self.alice.id, 1, "z", now=5002).status, "closed")

    def test_draw_without_tickets_and_cancel_refunds_everyone(self):
        r0 = self.mk()
        self.assertIsNone(rdb.draw(self.db, r0.id, now=5000).winner_id)
        for u in (self.alice, self.bob):
            self.fund(u, 1000)
        r = self.mk()
        rdb.buy_tickets(self.db, r.id, self.alice.id, 2, "a", now=1001)
        rdb.buy_tickets(self.db, r.id, self.bob.id, 5, "b", now=1002)
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (800, 500))
        self.assertEqual(rdb.cancel(self.db, r.id).status, "cancelled")
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (1000, 1000))
        self.assertIsNone(rdb.cancel(self.db, r.id))                                           # no se devuelve dos veces
        self.assertEqual(self.bal(self.alice), 1000)


class RaffleDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(rf.setup_raffles(self.bot, self.db, 1))

    def create(self, user=None, premio="Espada T8", precio="1k", tiempo="01:00"):
        i = self.inter(user or self.council)
        run(self.cog.rifas.callback(self.cog, i, premio, precio, tiempo))
        return i

    def panel(self):
        return next(m for m in reversed(self.panelch.sent) if m.embeds and "Rifa" in (m.embeds[0].title or ""))

    def press(self, user, qty=1):
        msg = self.panel()
        i = self.inter(user, self.panelch, msg)
        run(self.cog.on_buy(i, qty))
        return i

    def test_only_staff_can_create_and_bad_input_is_explained(self):
        self.assertIn("Solo el Council", self.create(self.alice).text)
        self.assertIn("hh:mm", self.create(tiempo="90").text)
        self.assertIn("Cantidad inválida", self.create(precio="abc").text)

    def test_create_publishes_panel_with_price_and_buttons(self):
        self.create(precio="1m")
        emb = self.panel().embeds[0]
        self.assertIn("Espada T8", emb.title)
        fields = {n: v for n, v, _ in emb.fields}
        self.assertIn("1,000,000", fields["💰 Precio por ticket"])
        self.assertIn("Todavía nadie", fields["📋 Quién compró"])
        self.assertEqual(len(self.panel().view.children), 2)

    def test_button_buy_charges_updates_panel_and_lists_buyer(self):
        self.create()
        self.fund(self.alice, 5000)
        i = self.press(self.alice, 3)
        self.assertIn("Compraste 3 tickets", i.text)
        self.assertEqual(self.bal(self.alice), 2000)
        fields = {n: v for n, v, _ in self.panel().embeds[0].fields}
        self.assertIn(str(self.alice.id), fields["📋 Quién compró"])
        self.assertIn("3", fields["📋 Quién compró"])
        self.assertIn("3", fields["🎫 Tickets vendidos"])

    def test_balance_is_checked_before_buying(self):
        self.create()
        self.fund(self.alice, 500)
        i = self.press(self.alice, 1)
        self.assertIn("No te alcanza", i.text)
        self.assertEqual(self.bal(self.alice), 500)

    def test_non_members_cannot_buy(self):
        self.create()
        self.fund(self.outsider, 5000)
        self.assertIn("Solo los miembros", self.press(self.outsider).text)
        self.assertEqual(self.bal(self.outsider), 5000)

    def test_due_raffle_is_drawn_announced_and_closed(self):
        self.create()
        self.fund(self.alice, 5000)
        self.press(self.alice, 2)
        import time
        self.assertEqual(run(self.cog.process_due(now=time.time() + 4000)), 1)
        self.assertEqual(run(self.cog.process_due(now=time.time() + 4001)), 0)                 # una sola vez
        self.assertIsNone(self.panel().view)                                                    # sin botones
        announce = self.panelch.sent[-1]
        self.assertIn(str(self.alice.id), announce.content)

    def test_cancel_command_refunds(self):
        self.create()
        self.fund(self.alice, 5000)
        self.press(self.alice, 2)
        i = self.inter(self.alice)
        run(self.cog.rifa_cancelar.callback(self.cog, i, 1))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.rifa_cancelar.callback(self.cog, i, 1))
        self.assertIn("cancelada", i.text)
        self.assertEqual(self.bal(self.alice), 5000)


if __name__ == "__main__":
    unittest.main()
