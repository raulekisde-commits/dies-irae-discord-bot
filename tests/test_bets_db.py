import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bets_db as b  # noqa: E402
import economy_db as eco  # noqa: E402

A, B, C, D = 101, 102, 103, 104
CREATOR = 900


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        b.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, uid, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"fund:{uid}:{amount}:{os.urandom(4).hex()}")

    def bal(self, uid):
        return eco.get_balance(self.db, uid)

    def new_bet(self, options=("Gana Smogg", "Pierde Smogg", "Empate")):
        bid = b.create_bet(self.db, guild_id=1, channel_id=2, creator_id=CREATOR, statement="¿Quién gana?", options=list(options))
        b.set_location(self.db, bid, message_id=50 + bid, thread_id=70 + bid)
        return bid

    def total_money(self):
        return sum(eco.get_all_balances(self.db).values())


class TestParsing(unittest.TestCase):
    def test_options(self):
        self.assertEqual(b.parse_options(" Gana Smogg ,Pierde  Smogg, Empate "), ["Gana Smogg", "Pierde Smogg", "Empate"])
        for bad in ("solo una", "a, A", "a,", "a, b, " + ", ".join(str(i) for i in range(12))):
            with self.assertRaises(b.BetError):
                b.parse_options(bad)

    def test_split_option_and_amount(self):
        self.assertEqual(b.split_option_and_amount("Gana Smogg 5000"), ("Gana Smogg", "5000"))
        with self.assertRaises(b.BetError):
            b.split_option_and_amount("5000")


class TestWagers(Base):
    def test_valid_wager_deducts_and_records_history(self):
        self.fund(A, 10000)
        bid = self.new_bet()
        r = b.place_wager(self.db, bid, A, "  gana   SMOGG ", 4000, thread_id=70 + bid)
        self.assertTrue(r.ok)
        self.assertEqual(self.bal(A), 6000)
        bet = b.get_bet(self.db, bid)
        self.assertEqual(bet.pool, 4000)
        hist = eco.get_history(self.db, A, 20)
        self.assertEqual(hist[0].mov_type, "bet_stake")
        self.assertEqual(hist[0].delta, -4000)
        self.assertEqual(hist[0].balance_after, 6000)
        self.assertEqual(hist[0].related_id, f"bet:{bid}")
        self.assertIn("Gana Smogg", hist[0].reason)

    def test_insufficient_funds_changes_nothing(self):
        self.fund(A, 100)
        bid = self.new_bet()
        r = b.place_wager(self.db, bid, A, "Empate", 101)
        self.assertEqual((r.ok, r.code, r.balance), (False, "funds", 100))
        self.assertEqual(self.bal(A), 100)
        self.assertEqual(b.get_bet(self.db, bid).pool, 0)
        self.assertEqual(len(eco.get_history(self.db, A)), 1)       # solo la carga inicial

    def test_negative_balance_cannot_bet(self):
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[A], amount=50, request_key="neg")
        bid = self.new_bet()
        self.assertEqual(b.place_wager(self.db, bid, A, "Empate", 1).code, "funds")
        self.assertEqual(self.bal(A), -50)

    def test_unknown_option_and_bad_amounts(self):
        self.fund(A, 1000)
        bid = self.new_bet()
        self.assertEqual(b.place_wager(self.db, bid, A, "Otra", 10).code, "option")
        for bad in (0, -5, True, 1.5):
            self.assertEqual(b.place_wager(self.db, bid, A, "Empate", bad).code, "invalid")
        self.assertEqual(self.bal(A), 1000)

    def test_wrong_thread_rejected(self):
        self.fund(A, 1000)
        bid = self.new_bet()
        r = b.place_wager(self.db, bid, A, "Empate", 10, thread_id=12345)
        self.assertEqual(r.code, "thread")
        self.assertEqual(self.bal(A), 1000)

    def test_multiple_wagers_accumulate_and_multiple_options(self):
        self.fund(A, 10000)
        bid = self.new_bet()
        for opt, amt in (("Empate", 1000), ("Empate", 500), ("Gana Smogg", 2000)):
            self.assertTrue(b.place_wager(self.db, bid, A, opt, amt).ok)
        bet = b.get_bet(self.db, bid)
        self.assertEqual(bet.user_totals()[A], {2: 1500, 0: 2000})
        self.assertEqual(bet.pool, 3500)
        self.assertEqual(self.bal(A), 6500)
        self.assertEqual(len(bet.participants), 1)

    def test_closed_resolved_cancelled_reject_new_wagers(self):
        self.fund(A, 1000)
        bid = self.new_bet()
        b.place_wager(self.db, bid, A, "Empate", 100)
        b.close_bet(self.db, bid, CREATOR)
        self.assertEqual(b.place_wager(self.db, bid, A, "Empate", 1).code, "state")
        b.resolve_bet(self.db, bid, CREATOR, 2)
        self.assertEqual(b.place_wager(self.db, bid, A, "Empate", 1).code, "state")
        bid2 = self.new_bet()
        b.cancel_bet(self.db, bid2, CREATOR)
        self.assertEqual(b.place_wager(self.db, bid2, A, "Empate", 1).code, "state")

    def test_failure_after_deduction_rolls_back_money(self):
        """Si falla algo DESPUÉS de descontar, el dinero vuelve (no se descuenta sin registrar la apuesta)."""
        self.fund(A, 1000)
        bid = self.new_bet()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON bet_wagers BEGIN SELECT RAISE(ABORT, 'falla simulada'); END")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception):
            b.place_wager(self.db, bid, A, "Empate", 300)
        self.assertEqual(self.bal(A), 1000)
        self.assertEqual(b.get_bet(self.db, bid).pool, 0)
        self.assertEqual(len(eco.get_history(self.db, A)), 1)

    def test_concurrent_wagers_cannot_overspend(self):
        self.fund(A, 1000)
        bid = self.new_bet()
        results = []

        def go():
            results.append(b.place_wager(self.db, bid, A, "Empate", 600).ok)

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.bal(A), 400)
        self.assertEqual(b.get_bet(self.db, bid).pool, 600)


class TestResolution(Base):
    def setup_example(self):
        # Ejemplo del enunciado: pozo 100.000, ganadora 20.000 (A: 5.000, B: 15.000)
        for u, amt in ((A, 5000), (B, 15000), (C, 50000), (D, 30000)):
            self.fund(u, amt)
        bid = self.new_bet()
        b.place_wager(self.db, bid, A, "Gana Smogg", 5000)
        b.place_wager(self.db, bid, B, "Gana Smogg", 15000)
        b.place_wager(self.db, bid, C, "Pierde Smogg", 50000)
        b.place_wager(self.db, bid, D, "Empate", 30000)
        return bid

    def test_payout_example(self):
        bid = self.setup_example()
        before_total = self.total_money()
        self.assertEqual(before_total, 0)                     # todo apostado
        self.assertEqual(b.close_bet(self.db, bid, CREATOR).ok, True)
        r = b.resolve_bet(self.db, bid, CREATOR, 0)
        self.assertTrue(r.ok)
        self.assertEqual(r.payouts, {A: 25000, B: 75000})
        self.assertEqual(r.remainder, 0)
        self.assertEqual((self.bal(A), self.bal(B), self.bal(C), self.bal(D)), (25000, 75000, 0, 0))
        self.assertEqual(self.total_money(), 100000)          # no se crea ni se pierde dinero
        bet = b.get_bet(self.db, bid)
        self.assertEqual((bet.status, bet.winner_idx, bet.distributed), ("resolved", 0, 100000))
        self.assertEqual(eco.get_history(self.db, A)[0].mov_type, "bet_payout")

    def test_rounding_down_and_remainder_recorded(self):
        for u in (A, B, C, D):
            self.fund(u, 1000)
        bid = self.new_bet()
        b.place_wager(self.db, bid, A, "Gana Smogg", 1)
        b.place_wager(self.db, bid, B, "Gana Smogg", 1)
        b.place_wager(self.db, bid, C, "Gana Smogg", 1)
        b.place_wager(self.db, bid, D, "Empate", 1000)       # pozo 1003, ganadora 3
        b.close_bet(self.db, bid, CREATOR)
        r = b.resolve_bet(self.db, bid, CREATOR, 0)
        self.assertEqual(r.payouts, {A: 334, B: 334, C: 334})   # floor(1003/3)
        self.assertEqual(r.remainder, 1)
        bet = b.get_bet(self.db, bid)
        self.assertEqual(bet.remainder, 1)
        self.assertLessEqual(bet.distributed, bet.pool)

    def test_distribution_never_exceeds_pool_property(self):
        import random
        rnd = random.Random(7)
        for _ in range(200):
            stakes = {i: rnd.randint(1, 10 ** 9) for i in range(rnd.randint(1, 8))}
            pool = sum(stakes.values()) + rnd.randint(0, 10 ** 12)
            payouts, rem = b.compute_payouts(pool, stakes)
            self.assertLessEqual(sum(payouts.values()), pool)
            self.assertEqual(sum(payouts.values()) + rem, pool)

    def test_user_with_many_winning_wagers(self):
        self.fund(A, 1000)
        self.fund(B, 1000)
        bid = self.new_bet()
        b.place_wager(self.db, bid, A, "Gana Smogg", 100)
        b.place_wager(self.db, bid, A, "Gana Smogg", 100)
        b.place_wager(self.db, bid, B, "Empate", 200)
        b.close_bet(self.db, bid, CREATOR)
        r = b.resolve_bet(self.db, bid, CREATOR, 0)
        self.assertEqual(r.payouts, {A: 400})

    def test_no_winner_refunds_everyone_once(self):
        for u in (A, B):
            self.fund(u, 1000)
        bid = self.new_bet()
        b.place_wager(self.db, bid, A, "Gana Smogg", 300)
        b.place_wager(self.db, bid, A, "Pierde Smogg", 200)
        b.place_wager(self.db, bid, B, "Pierde Smogg", 400)
        b.close_bet(self.db, bid, CREATOR)
        r = b.resolve_bet(self.db, bid, CREATOR, 2)          # Empate: nadie apostó
        self.assertEqual((r.ok, r.code), (False, "no_winner"))
        self.assertEqual((self.bal(A), self.bal(B)), (1000, 1000))
        bet = b.get_bet(self.db, bid)
        self.assertEqual((bet.status, bet.outcome), ("cancelled", "no_winner"))
        self.assertEqual(b.resolve_bet(self.db, bid, CREATOR, 2).code, "state")
        self.assertEqual(b.cancel_bet(self.db, bid, CREATOR).code, "state")
        self.assertEqual((self.bal(A), self.bal(B)), (1000, 1000))

    def test_cannot_resolve_twice_or_before_closing(self):
        bid = self.setup_example()
        self.assertEqual(b.resolve_bet(self.db, bid, CREATOR, 0).code, "state")          # abierta
        b.close_bet(self.db, bid, CREATOR)
        self.assertTrue(b.resolve_bet(self.db, bid, CREATOR, 0).ok)
        snapshot = (self.bal(A), self.bal(B))
        self.assertEqual(b.resolve_bet(self.db, bid, CREATOR, 0).code, "state")
        self.assertEqual(b.resolve_bet(self.db, bid, CREATOR, 1).code, "state")
        self.assertEqual((self.bal(A), self.bal(B)), snapshot)

    def test_only_creator_can_close_and_resolve(self):
        bid = self.setup_example()
        self.assertEqual(b.close_bet(self.db, bid, A).code, "permission")
        b.close_bet(self.db, bid, CREATOR)
        self.assertEqual(b.resolve_bet(self.db, bid, A, 0).code, "permission")
        self.assertEqual(b.get_bet(self.db, bid).status, "closed")

    def test_cancel_refunds_and_is_idempotent(self):
        bid = self.setup_example()
        self.assertEqual(b.cancel_bet(self.db, bid, A).code, "permission")
        self.assertTrue(b.cancel_bet(self.db, bid, 777, is_admin=True).ok)
        self.assertEqual((self.bal(A), self.bal(B), self.bal(C), self.bal(D)), (5000, 15000, 50000, 30000))
        self.assertEqual(b.cancel_bet(self.db, bid, CREATOR).code, "state")
        self.assertEqual((self.bal(A), self.bal(B)), (5000, 15000))
        self.assertEqual(eco.get_history(self.db, A)[0].mov_type, "bet_refund")

    def test_cancel_after_close_before_resolve(self):
        bid = self.setup_example()
        b.close_bet(self.db, bid, CREATOR)
        self.assertTrue(b.cancel_bet(self.db, bid, CREATOR).ok)
        self.assertEqual(self.total_money(), 100000)

    def test_cannot_cancel_resolved(self):
        bid = self.setup_example()
        b.close_bet(self.db, bid, CREATOR)
        b.resolve_bet(self.db, bid, CREATOR, 0)
        self.assertEqual(b.cancel_bet(self.db, bid, CREATOR, is_admin=True).code, "state")

    def test_concurrent_resolve_pays_once(self):
        bid = self.setup_example()
        b.close_bet(self.db, bid, CREATOR)
        oks = []

        def go():
            oks.append(b.resolve_bet(self.db, bid, CREATOR, 0).ok)

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(oks), 1)
        self.assertEqual(self.total_money(), 100000)
        self.assertEqual((self.bal(A), self.bal(B)), (25000, 75000))

    def test_concurrent_cancel_refunds_once(self):
        bid = self.setup_example()
        oks = []

        def go():
            oks.append(b.cancel_bet(self.db, bid, CREATOR).ok)

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(oks), 1)
        self.assertEqual((self.bal(A), self.bal(C)), (5000, 50000))

    def test_failure_during_payout_rolls_back_everything(self):
        bid = self.setup_example()
        b.close_bet(self.db, bid, CREATOR)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON bet_payouts WHEN NEW.user_id = 102 BEGIN SELECT RAISE(ABORT, 'falla simulada'); END")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception):
            b.resolve_bet(self.db, bid, CREATOR, 0)
        self.assertEqual((self.bal(A), self.bal(B)), (0, 0))              # ningún pago parcial
        self.assertEqual(b.get_bet(self.db, bid).status, "closed")
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER boom")
        conn.commit()
        conn.close()
        self.assertTrue(b.resolve_bet(self.db, bid, CREATOR, 0).ok)       # se puede reintentar
        self.assertEqual((self.bal(A), self.bal(B)), (25000, 75000))

    def test_data_persists_across_reinit(self):
        bid = self.setup_example()
        b.close_bet(self.db, bid, CREATOR)
        b.init_schema(self.db)
        bet = b.get_bet(self.db, bid)
        self.assertEqual((bet.status, bet.pool, len(bet.participants)), ("closed", 100000, 4))
        self.assertEqual(b.find_by_thread(self.db, 70 + bid).id, bid)


class TestHistoryAndReasons(Base):
    def test_reasons_defaults_and_role_common_id(self):
        r = eco.apply_operation(self.db, kind="add", actor_id=5, user_ids=[A, B], amount=100, request_key="k1",
                                role_id=999, reason=eco.clean_reason("compensacion  extra", eco.REASON_ADD_DEFAULT))
        ha, hb = eco.get_history(self.db, A)[0], eco.get_history(self.db, B)[0]
        self.assertEqual((ha.reason, ha.mov_type, ha.actor_id), ("compensacion extra", "addmoney-role", 5))
        self.assertEqual(ha.related_id, hb.related_id)
        self.assertEqual(ha.related_id, r.op_id)
        eco.apply_operation(self.db, kind="add", actor_id=5, user_ids=[A], amount=1, request_key="k2")
        self.assertEqual(eco.get_history(self.db, A)[0].reason, "Sin motivo especificado")
        eco.apply_operation(self.db, kind="remove", actor_id=5, user_ids=[A], amount=1, request_key="k3")
        self.assertEqual(eco.get_history(self.db, A)[0].reason, "cobro el bal")

    def test_history_limit_order_and_balances(self):
        for i in range(25):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[A], amount=10, request_key=f"h{i}")
        rows = eco.get_history(self.db, A, 20)
        self.assertEqual(len(rows), 20)
        self.assertEqual([r.balance_after for r in rows], [250 - 10 * i for i in range(20)])
        self.assertEqual(eco.get_history(self.db, B), [])

    def test_migration_keeps_old_data_and_fills_history(self):
        """Una base con el esquema ANTERIOR (sin columnas nuevas) se migra sin perder nada."""
        old = os.path.join(self.tmp.name, "old.sqlite3")
        conn = sqlite3.connect(old)
        conn.executescript("""
        CREATE TABLE eco_accounts (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE eco_operations (op_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('add','remove')),
            scope TEXT NOT NULL CHECK (scope IN ('user','role')), actor_id INTEGER NOT NULL, role_id INTEGER, per_user_amount INTEGER NOT NULL,
            target_count INTEGER NOT NULL, total_amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'completed', created_at TEXT NOT NULL,
            log_status TEXT NOT NULL DEFAULT 'pending', log_pages_sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE eco_movements (id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL REFERENCES eco_operations(op_id), user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL, balance_before INTEGER NOT NULL, balance_after INTEGER NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO eco_accounts VALUES (101, 700, 't', 't');
        INSERT INTO eco_operations VALUES ('op1','k','add','user',5,NULL,700,1,700,'completed','2026-01-01T00:00:00+00:00','sent',1);
        INSERT INTO eco_movements(op_id,user_id,delta,balance_before,balance_after,created_at) VALUES ('op1',101,700,0,700,'2026-01-01T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()
        b.init_schema(old)
        b.init_schema(old)                       # idempotente
        self.assertEqual(eco.get_balance(old, 101), 700)
        rows = eco.get_history(old, 101)
        self.assertEqual((rows[0].mov_type, rows[0].delta, rows[0].actor_id, rows[0].reason), ("addmoney", 700, 5, "—"))
        eco.apply_operation(old, kind="remove", actor_id=5, user_ids=[101], amount=100, request_key="n")
        self.assertEqual(eco.get_balance(old, 101), 600)


if __name__ == "__main__":
    unittest.main()
