import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402
import casino_db as cdb  # noqa: E402
import deposits_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import games_db as gdb  # noqa: E402
import raffles as rf  # noqa: E402
import raffles_db as rdb  # noqa: E402


class FreezeTests(DiscordBase):
    def setUp(self):
        super().setUp()
        ddb.init_schema(self.db)
        gdb.init_schema(self.db)
        rdb.init_schema(self.db)
        self.fund(self.alice, 10000)
        self.fund(self.bob, 10000)

    def open_ticket(self, user, kind="sell", channel=777):
        return ddb.create_ticket(self.db, 1, channel, user.id, kind, 1000 if kind == "buy" else 0)

    def test_only_an_open_sell_ticket_freezes(self):
        self.assertFalse(eco.is_frozen(self.db, self.alice.id))
        tid = self.open_ticket(self.alice)
        self.assertTrue(eco.is_frozen(self.db, self.alice.id))
        self.assertFalse(eco.is_frozen(self.db, self.bob.id))
        self.open_ticket(self.bob, kind="buy", channel=778)                    # comprar coins no congela
        self.assertFalse(eco.is_frozen(self.db, self.bob.id))
        ddb.close_ticket(self.db, tid, 1)
        self.assertFalse(eco.is_frozen(self.db, self.alice.id))                # al cerrar se descongela solo

    def test_cannot_play_or_move_balance_while_frozen(self):
        self.open_ticket(self.alice)
        self.assertEqual(cdb.block_reason(self.db, 1, self.alice.id), eco.FROZEN_TEXT)       # dados/coinflip/1v1: crear y anotarse
        self.assertIn("congelado", cdb.block_reason(self.db, 1, self.bob.id, self.alice.id))  # retar a alguien congelado
        self.assertIsNone(cdb.block_reason(self.db, 1, self.bob.id))
        with self.assertRaises(eco.BalanceFrozen):                                           # red de seguridad en la base
            gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=2, creator_id=self.alice.id, stake=100)
        with self.assertRaises(eco.BalanceFrozen):
            eco.transfer(self.db, from_id=self.alice.id, to_id=self.bob.id, amount=10, request_key="t1", reason="x")
        self.assertEqual(self.bal(self.alice), 10000)                                        # y no se movió nada
        self.assertEqual(self.bal(self.bob), 10000)

    def test_give_money_command_and_raffle_button_explain_it(self):
        self.open_ticket(self.alice)
        i = self.inter(self.alice)
        run(self.ecog.give_money.callback(self.ecog, i, self.bob, "100", None))
        self.assertIn("congelado", i.text)
        self.assertEqual(self.bal(self.alice), 10000)
        cog = run(rf.setup_raffles(self.bot, self.db, 1))
        j = self.inter(self.council)
        run(cog.rifas.callback(cog, j, "Premio", "100", "01:00"))
        msg = next(m for m in reversed(self.panelch.sent) if m.embeds)
        k = self.inter(self.alice, self.panelch, msg)
        run(cog.on_buy(k, 1))
        self.assertIn("congelado", k.text)
        self.assertEqual(self.bal(self.alice), 10000)

    def test_staff_withdrawal_and_incoming_money_still_work(self):
        self.open_ticket(self.alice)
        op = eco.apply_operation(self.db, kind="remove", actor_id=self.council.id, user_ids=[self.alice.id], amount=4000,
                                 request_key="cm:1:sell", reason="cobro", require_funds=True)
        self.assertTrue(op.created)
        self.assertEqual(self.bal(self.alice), 6000)
        self.fund(self.alice, 500)                                                           # premios / depósitos sí se acreditan
        self.assertEqual(self.bal(self.alice), 6500)

    def test_money_already_in_a_game_is_still_paid_out(self):
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=2, creator_id=self.alice.id, stake=100).game
        gdb.join_game(self.db, g.id, self.bob.id)
        self.open_ticket(self.alice)
        res = gdb.resolve_game(self.db, g.id, now=g.ends_at + 1)
        self.assertEqual(res.status, "done")
        self.assertEqual(self.bal(self.alice) + self.bal(self.bob), 20000)


if __name__ == "__main__":
    unittest.main()
