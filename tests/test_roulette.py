import os
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402
import discord  # noqa: E402
import economy_db as eco  # noqa: E402
import roulette as rl  # noqa: E402
import roulette_db as rdb  # noqa: E402
from dbutil import connect  # noqa: E402

P = rdb.DEFAULT_PAYOUTS


class RulesTests(unittest.TestCase):
    def test_colors_and_zero(self):
        self.assertEqual(sum(1 for n in range(37) if rdb.color_of(n) == "red"), 18)
        self.assertEqual(sum(1 for n in range(37) if rdb.color_of(n) == "black"), 18)
        self.assertEqual(rdb.color_of(0), "green")
        for kind, sel in (("red", 0), ("black", 0), ("even", 0), ("odd", 0), ("low", 0), ("high", 0), ("dozen", 1), ("dozen", 2), ("dozen", 3),
                          ("column", 1), ("column", 2), ("column", 3)):
            self.assertFalse(rdb.wins(kind, sel, 0), kind)            # el 0 pierde todas las externas, docenas y columnas
        self.assertTrue(rdb.wins("number", 0, 0))

    def test_each_bet_type_wins_expected_numbers(self):
        self.assertEqual(sum(rdb.wins("red", 0, n) for n in range(37)), 18)
        self.assertEqual(sum(rdb.wins("even", 0, n) for n in range(37)), 18)
        self.assertEqual(sum(rdb.wins("low", 0, n) for n in range(37)), 18)
        self.assertEqual([n for n in range(37) if rdb.wins("dozen", 2, n)], list(range(13, 25)))
        self.assertEqual([n for n in range(37) if rdb.wins("column", 1, n)], [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34])
        self.assertEqual([n for n in range(37) if rdb.wins("column", 3, n)], [3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36])
        self.assertTrue(rdb.wins("high", 0, 36) and not rdb.wins("high", 0, 18))

    def test_payouts_and_house_edge_exact(self):
        cases = [("red", 0), ("black", 0), ("even", 0), ("odd", 0), ("low", 0), ("high", 0), ("dozen", 1), ("dozen", 3), ("column", 2), ("number", 17), ("number", 0)]
        for kind, sel in cases:
            total = sum(rdb.gross_payout(kind, sel, 37, n, P) for n in range(37))     # apostando 37 en cada resultado posible
            self.assertEqual(total, 37 * 36, kind)                                    # RTP exacto 36/37 → ventaja de la casa 1/37 = 2,70%
        self.assertEqual(rdb.gross_payout("red", 0, 100, 1, P), 200)
        self.assertEqual(rdb.gross_payout("dozen", 2, 100, 14, P), 300)
        self.assertEqual(rdb.gross_payout("number", 17, 100, 17, P), 3600)
        self.assertEqual(rdb.gross_payout("red", 0, 100, 2, P), 0)

    def test_parse_selection(self):
        self.assertEqual(rdb.parse_selection("rojo"), ("red", 0))
        self.assertEqual(rdb.parse_selection("NEGRO"), ("black", 0))
        self.assertEqual(rdb.parse_selection("falta"), ("low", 0))
        self.assertEqual(rdb.parse_selection("docena 2"), ("dozen", 2))
        self.assertEqual(rdb.parse_selection("columna 3"), ("column", 3))
        self.assertEqual(rdb.parse_selection("numero 17"), ("number", 17))
        self.assertEqual(rdb.parse_selection("0"), ("number", 0))
        for bad in ("", "docena 4", "numero 37", "37", "verde", "docena", "rojo 5"):
            with self.assertRaises(rdb.RouletteError, msg=bad):
                rdb.parse_selection(bad)

    def test_worst_case(self):
        n, paid, loss = rdb.worst_case([("number", 17, 100), ("red", 0, 1000)], P)
        self.assertEqual(n, 17)                                   # 17 es negro: gana el pleno (3600), el rojo pierde
        self.assertEqual((paid, loss), (3600, 3600 - 1100))
        n, paid, loss = rdb.worst_case([("red", 0, 1000), ("black", 0, 1000)], P)
        self.assertEqual(loss, 0)                                 # cubiertas: pagan 2000 de 2000 apostados (y el 0 las pierde a ambas)


class RouletteDbBase(DiscordBase):
    def setUp(self):
        super().setUp()
        rdb.init_schema(self.db)
        rdb.update_config(self.db, 1, min_bet=100, max_bet=100_000, reserve=1_000, initial_capital=1_000_000)
        rdb.adjust_house_balance(self.db, 1, 1_000_000, "preparar test")

    def panel(self):
        p, created = rdb.create_panel(self.db, 1, 2, 3)
        self.assertTrue(created)
        return p

    def bet(self, user, kind, sel, amount, key=None, now=1000.0):
        return rdb.place_bet(self.db, self.panel_id, user.id, kind, sel, amount, key or os.urandom(4).hex(), now=now)

    def start(self):
        p = self.panel()
        self.panel_id = p.id
        return p

    def house(self):
        return rdb.get_config(self.db).house_balance


class PanelTests(RouletteDbBase):
    def test_only_one_active_roulette(self):
        p, created = rdb.create_panel(self.db, 1, 2, 3)
        self.assertTrue(created)
        p2, created2 = rdb.create_panel(self.db, 1, 99, 4)             # otro canal, otro admin
        self.assertFalse(created2)
        self.assertEqual(p2.id, p.id)
        conn = connect(self.db)
        with self.assertRaises(Exception):                             # y la base lo impide aunque se intente a mano
            conn.execute("INSERT INTO roulette_panels(guild_id, channel_id, created_by, created_at) VALUES (1,2,3,'x')")
        conn.close()

    def test_simultaneous_creation_yields_one(self):
        results = []
        barrier = threading.Barrier(8)

        def go(i):
            barrier.wait()
            results.append(rdb.create_panel(self.db, 1, i, i))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for _, c in results if c), 1)
        self.assertEqual(len({p.id for p, _ in results}), 1)
        conn = connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM roulette_panels WHERE status='open'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM roulette_rounds WHERE status='betting'").fetchone()[0], 1)
        conn.close()

    def test_close_frees_global_state_and_restart_recovers(self):
        p = self.panel()
        rdb.init_schema(self.db)                                       # «reinicio»: la ruleta abierta se recupera de la base
        self.assertEqual(rdb.get_active_panel(self.db).id, p.id)
        self.assertFalse(rdb.create_panel(self.db, 1, 5, 5)[1])
        self.assertIsNotNone(rdb.close_panel(self.db, p.id, 3))
        self.assertIsNone(rdb.close_panel(self.db, p.id, 3))           # cerrar dos veces no hace nada
        self.assertIsNone(rdb.get_active_panel(self.db))
        p2, created = rdb.create_panel(self.db, 1, 5, 5)
        self.assertTrue(created)
        self.assertNotEqual(p2.id, p.id)

    def test_abort_frees_state(self):
        p = self.panel()
        rdb.abort_panel(self.db, p.id)
        self.assertTrue(rdb.create_panel(self.db, 1, 2, 3)[1])


class BetAndResolveTests(RouletteDbBase):
    def setUp(self):
        super().setUp()
        self.start()
        self.fund(self.alice, 100_000)
        self.fund(self.bob, 100_000)

    def total(self):
        return self.bal(self.alice) + self.bal(self.bob) + self.house()

    def test_bet_debits_and_is_stored_individually(self):
        r1 = self.bet(self.alice, "red", 0, 1000)
        r2 = self.bet(self.alice, "number", 17, 500)
        r3 = self.bet(self.alice, "black", 0, 700)                     # varias apuestas, incluso opuestas
        self.assertEqual([r.status for r in (r1, r2, r3)], ["ok"] * 3)
        self.assertEqual(self.bal(self.alice), 100_000 - 2200)
        self.assertEqual(len(rdb.user_bets(self.db, r1.round.id, self.alice.id)), 3)
        self.assertEqual(rdb.round_summary(self.db, r1.round.id), (1, 3, 2200))
        self.assertEqual(self.house(), 1_000_000)                      # la casa no cambia hasta resolver
        self.assertEqual(r1.round.ends_at, 1000.0 + 60)                # la primera apuesta arranca el reloj
        self.assertEqual(r3.round.ends_at, r1.round.ends_at)

    def test_limits(self):
        self.assertEqual(self.bet(self.alice, "red", 0, 99).status, "min")
        self.assertEqual(self.bet(self.alice, "red", 0, 100_001).status, "max")
        self.fund(self.bob, 10)
        broke = self.outsider
        self.assertEqual(self.bet(broke, "red", 0, 500).status, "funds")
        self.assertEqual(self.bal(self.alice), 100_000)
        with self.assertRaises(rdb.RouletteError):
            self.bet(self.alice, "number", 40, 500)

    def test_idempotent_request_key(self):
        a = self.bet(self.alice, "red", 0, 1000, key="same")
        b = self.bet(self.alice, "red", 0, 1000, key="same")
        self.assertEqual((a.status, b.status), ("ok", "duplicate"))
        self.assertEqual(self.bal(self.alice), 99_000)

    def test_resolve_accounting_winner_and_loser(self):
        self.bet(self.alice, "red", 0, 1000)                           # gana con 1 (rojo)
        self.bet(self.bob, "black", 0, 3000)                           # pierde
        self.bet(self.alice, "number", 1, 100)                         # gana pleno: 3600
        rid = rdb.current_round(self.db, self.panel_id).id
        self.assertIsNone(rdb.resolve_round(self.db, rid, now=1010.0))          # todavía no vence
        st = rdb.resolve_round(self.db, rid, now=1061.0, number=1)
        self.assertEqual(st.status, "done")
        self.assertEqual(st.round.total_staked, 4100)
        self.assertEqual(st.round.total_paid, 2000 + 3600)
        self.assertEqual(st.round.house_delta, 4100 - 5600)
        self.assertEqual(self.bal(self.alice), 100_000 - 1100 + 5600)
        self.assertEqual(self.bal(self.bob), 100_000 - 3000)
        self.assertEqual(self.house(), 1_000_000 - 1500)
        # conservación: lo que sale de los jugadores y de la casa es igual a lo que entra
        self.assertEqual(self.bal(self.alice) + self.bal(self.bob) + self.house(), 100_000 * 2 + 1_000_000)
        self.assertEqual((len(st.winners), len(st.losers)), (2, 1))
        self.assertIsNotNone(st.next_round_id)                         # sigue abierta → nueva ronda
        self.assertEqual(rdb.current_round(self.db, self.panel_id).id, st.next_round_id)
        # el balance de la casa queda registrado
        self.assertEqual(rdb.stats(self.db)["rounds"], 1)

    def test_zero_loses_outside_bets(self):
        for kind, sel in (("red", 0), ("black", 0), ("even", 0), ("low", 0), ("dozen", 1), ("column", 2)):
            self.bet(self.alice, kind, sel, 1000)
        self.bet(self.bob, "number", 0, 100)
        rid = rdb.current_round(self.db, self.panel_id).id
        st = rdb.resolve_round(self.db, rid, force=True, number=0)
        self.assertEqual(st.round.total_paid, 3600)
        self.assertEqual(st.round.house_delta, 6100 - 3600)
        self.assertEqual(self.bal(self.alice), 100_000 - 6000)

    def test_double_resolution_is_impossible(self):
        self.bet(self.alice, "red", 0, 1000)
        rid = rdb.current_round(self.db, self.panel_id).id
        first = rdb.resolve_round(self.db, rid, force=True, number=1)
        again = rdb.resolve_round(self.db, rid, force=True, number=1)
        self.assertEqual(first.status, "done")
        self.assertIsNone(again)
        self.assertEqual(self.bal(self.alice), 101_000)
        results = []
        self.bet(self.alice, "red", 0, 1000)
        rid2 = rdb.current_round(self.db, self.panel_id).id
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            results.append(rdb.resolve_round(self.db, rid2, force=True, number=3))

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for r in results if r is not None), 1)
        self.assertEqual(self.bal(self.alice), 101_000 - 1000 + 2000)
        self.assertEqual(self.house(), 1_000_000 - 1000 - 1000)

    def test_no_bets_after_time_or_after_close(self):
        self.bet(self.alice, "red", 0, 1000)
        self.assertEqual(self.bet(self.bob, "red", 0, 1000, now=1061.0).status, "closed")      # vencida la ronda
        rdb.close_panel(self.db, self.panel_id, 3)
        self.assertEqual(self.bet(self.bob, "red", 0, 1000).status, "closed")
        self.assertEqual(self.bal(self.bob), 100_000)

    def test_close_refunds_everything_and_keeps_history(self):
        self.bet(self.alice, "red", 0, 1000)
        self.bet(self.alice, "number", 5, 500)
        self.bet(self.bob, "odd", 0, 2000)
        st = rdb.close_panel(self.db, self.panel_id, 3)
        self.assertEqual(st.status, "cancelled")
        self.assertEqual((self.bal(self.alice), self.bal(self.bob), self.house()), (100_000, 100_000, 1_000_000))
        self.assertEqual(sum(st.stake_by_user.values()), 3500)
        conn = connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM roulette_bets").fetchone()[0], 3)   # historial intacto
        self.assertEqual(conn.execute("SELECT status FROM roulette_rounds WHERE id=?", (st.round.id,)).fetchone()[0], "cancelled")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM eco_movements WHERE kind='roulette_refund'").fetchone()[0], 2)
        conn.close()
        self.assertIsNone(rdb.resolve_round(self.db, st.round.id, force=True, number=1))        # no se puede resolver lo cancelado

    def test_due_rounds_and_restart_recovery(self):
        self.bet(self.alice, "red", 0, 1000, now=1000.0)
        self.assertEqual(rdb.due_rounds(self.db, now=1059.0), [])
        rdb.init_schema(self.db)                                                                  # reinicio: nada se pierde
        due = rdb.due_rounds(self.db, now=5000.0)                                                 # el bot estuvo caído: se resuelve al volver
        self.assertEqual(len(due), 1)
        self.assertEqual(rdb.resolve_round(self.db, due[0], now=5000.0, number=2).status, "done")
        self.assertEqual(rdb.round_bets(self.db, due[0])[0].amount, 1000)

    def test_provably_fair_rng_is_used(self):
        self.bet(self.alice, "red", 0, 1000)
        rid = rdb.current_round(self.db, self.panel_id).id
        sealed = rdb.pf.get_record(self.db, "roulette", rid)
        self.assertFalse(sealed["revealed"])
        self.assertIsNone(sealed["server_seed"])                          # la semilla no se expone antes de girar
        st = rdb.resolve_round(self.db, rid, force=True)
        self.assertEqual(st.proof["server_hash"], sealed["server_hash"])
        self.assertEqual(rdb.pf.seed_hash(st.proof["server_seed"]), sealed["server_hash"])
        self.assertEqual(st.number, rdb.pf.compute("roulette", st.proof["server_seed"], st.proof["client_seed"], st.proof["nonce"])["number"])
        self.assertEqual(rdb.pf.verify(self.db, "roulette", rid).status, "ok")

    def test_rng_covers_all_numbers(self):
        rng = rdb.pf.FairRng(rdb.pf.new_server_seed(), "test", 1)
        seen = {rdb.pf.roulette_number(rng) for _ in range(4000)}
        self.assertEqual(seen, set(range(37)))


class RiskTests(RouletteDbBase):
    def setUp(self):
        super().setUp()
        self.start()
        self.fund(self.alice, 10_000_000)

    def test_exposure_rejects_what_house_cannot_cover(self):
        rdb.update_config(self.db, 1, max_bet=500_000, reserve=100_000)
        rdb.adjust_house_balance(self.db, 1, 1_000_000, "tope")
        # pleno de 25.000 → paga 900.000 (pérdida neta 875.000): disponible = 1.000.000 − reserva 100.000 = 900.000 → entra
        self.assertEqual(self.bet(self.alice, "number", 7, 25_000).status, "ok")
        # otro pleno distinto de 25.000: peor caso es un solo pleno (900k − 50k) → entra; el mismo número otra vez: 1.8M − 50k → rechazado
        self.assertEqual(self.bet(self.alice, "number", 8, 25_000).status, "ok")
        self.assertEqual(self.bet(self.alice, "number", 7, 25_000).status, "exposure")
        self.assertEqual(self.bal(self.alice), 10_000_000 - 50_000)    # lo rechazado no se cobró

    def test_reserve_is_never_used_to_authorize(self):
        rdb.update_config(self.db, 1, reserve=1_000_000)               # reserva = todo el balance
        self.assertEqual(self.bet(self.alice, "red", 0, 1000).status, "exposure")
        rdb.update_config(self.db, 1, reserve=999_000)
        self.assertEqual(self.bet(self.alice, "red", 0, 1000).status, "ok")     # pierde máx. 1000 neto; 1.000.000−1000 ≥ 999.000

    def test_balanced_bets_do_not_add_exposure(self):
        rdb.update_config(self.db, 1, reserve=999_000)               # la casa solo puede perder 1.000 netos
        self.assertEqual(self.bet(self.alice, "red", 0, 500).status, "ok")
        self.assertEqual(self.bet(self.alice, "red", 0, 400).status, "ok")          # pérdida máx. 900
        self.assertEqual(self.bet(self.alice, "red", 0, 200).status, "exposure")    # 1.100 > 1.000
        self.assertEqual(self.bet(self.alice, "black", 0, 1_100).status, "ok")      # cubrir el otro color reduce el riesgo
        self.assertEqual(self.bet(self.alice, "red", 0, 200).status, "ok")

    def test_concurrent_bets_never_exceed_house_capacity(self):
        rdb.update_config(self.db, 1, max_bet=100_000, reserve=0)
        rdb.adjust_house_balance(self.db, 1, 1_000_000, "tope")
        self.fund(self.bob, 10_000_000)
        out = []
        barrier = threading.Barrier(16)

        def go(i):
            u = self.alice if i % 2 else self.bob
            barrier.wait()
            out.append(rdb.place_bet(self.db, self.panel_id, u.id, "number", 5, 100_000, f"c{i}", now=1000.0))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = [r for r in out if r.status == "ok"]
        # cada pleno de 100k paga 3.6M: la casa (1M) cubre apuestas hasta que pérdida neta = 3.6M·k − 100k·k ≤ 1M → solo 0 entra? (3.5M > 1M)
        rid = rdb.current_round(self.db, self.panel_id).id
        bets = [(b.kind, b.selection, b.amount) for b in rdb.round_bets(self.db, rid)]
        if bets:
            self.assertLessEqual(max(0, rdb.worst_case(bets, P)[2]), 1_000_000)
        self.assertEqual(len(ok), len(bets))
        self.assertEqual(len(ok), 0)

    def test_concurrent_covered_bets_respect_limit(self):
        rdb.update_config(self.db, 1, max_bet=100_000, reserve=0)
        rdb.adjust_house_balance(self.db, 1, 300_000, "tope")
        self.fund(self.bob, 10_000_000)
        out = []
        barrier = threading.Barrier(12)

        def go(i):
            u = self.alice if i % 2 else self.bob
            barrier.wait()
            out.append(rdb.place_bet(self.db, self.panel_id, u.id, "red", 0, 100_000, f"d{i}", now=1000.0))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n_ok = sum(1 for r in out if r.status == "ok")
        self.assertEqual(n_ok, 3)                                      # 3 apuestas de 100k al rojo: pérdida máx. neta 300k = balance de la casa
        self.assertEqual(self.bal(self.alice) + self.bal(self.bob), 20_000_000 - 300_000)
        self.assertEqual(rdb.round_summary(self.db, rdb.current_round(self.db, self.panel_id).id)[2], 300_000)

    def test_house_never_negative_over_many_rounds(self):
        rdb.update_config(self.db, 1, max_bet=100_000, reserve=0)
        rdb.adjust_house_balance(self.db, 1, 400_000, "chica")
        self.fund(self.alice, 5_000_000)
        for i in range(40):
            self.bet(self.alice, "red", 0, 100_000, key=f"r{i}", now=1000.0)
            self.bet(self.alice, "number", i % 37, 5_000, key=f"n{i}", now=1000.0)
            rid = rdb.current_round(self.db, self.panel_id).id
            rdb.resolve_round(self.db, rid, force=True)
            self.assertGreaterEqual(self.house(), 0)
        # conservación: jugadores + casa = lo que había al empezar (nada se crea ni se pierde)
        self.assertEqual(self.bal(self.alice) + self.house(), 15_400_000)


class ConfigTests(RouletteDbBase):
    def test_initial_capital_does_not_overwrite_balance(self):
        rdb.update_config(self.db, 9, initial_capital=77_000_000)
        cfg = rdb.get_config(self.db)
        self.assertEqual((cfg.initial_capital, cfg.house_balance), (77_000_000, 1_000_000))

    def test_each_parameter_individually_and_persisted(self):
        rdb.update_config(self.db, 9, min_bet=500)
        rdb.update_config(self.db, 9, max_bet=250_000)
        rdb.update_config(self.db, 9, reserve=5_000)
        rdb.update_config(self.db, 9, round_seconds=90)
        rdb.init_schema(self.db)
        c = rdb.get_config(self.db)
        self.assertEqual((c.min_bet, c.max_bet, c.reserve, c.round_seconds), (500, 250_000, 5_000, 90))

    def test_validation(self):
        for kw in ({"min_bet": 0}, {"max_bet": 50}, {"reserve": -1}, {"round_seconds": 5}, {"initial_capital": 0}):
            with self.assertRaises(rdb.RouletteError, msg=str(kw)):
                rdb.update_config(self.db, 9, **kw)

    def test_payouts_need_explicit_enable_and_apply_next_round(self):
        with self.assertRaises(rdb.RouletteError):
            rdb.update_config(self.db, 9, payouts={"color": 3})
        self.assertEqual(rdb.get_config(self.db).payouts["color"], 2)
        rdb.update_config(self.db, 9, payouts_enabled=True)
        p = self.panel()
        self.fund(self.alice, 10_000)
        r = rdb.place_bet(self.db, p.id, self.alice.id, "red", 0, 1000, "k", now=1000.0)
        rdb.update_config(self.db, 9, payouts={"color": 3})
        st = rdb.resolve_round(self.db, r.round.id, force=True, number=1)
        self.assertEqual(st.round.total_paid, 2000)                    # la ronda en curso mantiene los pagos con los que abrió
        r2 = rdb.place_bet(self.db, p.id, self.alice.id, "red", 0, 1000, "k2", now=2000.0)
        self.assertEqual(r2.round.payouts["color"], 3)

    def test_admin_adjust_is_audited(self):
        before, after = rdb.adjust_house_balance(self.db, 9, 2_000_000, "inyección del staff")
        self.assertEqual((before, after), (1_000_000, 2_000_000))
        with self.assertRaises(rdb.RouletteError):
            rdb.adjust_house_balance(self.db, 9, 5, "")
        with self.assertRaises(rdb.RouletteError):
            rdb.adjust_house_balance(self.db, 9, -1, "negativo")
        s = rdb.stats(self.db)
        self.assertTrue(any(a["kind"] == "admin_adjust" and a["actor_id"] == 9 and "inyección" in a["note"] for a in s["adjustments"]))

    def test_cannot_lower_balance_below_open_exposure(self):
        self.start()
        self.fund(self.alice, 100_000)
        self.bet(self.alice, "number", 3, 10_000)                      # expone 350.000 netos
        with self.assertRaises(rdb.RouletteError):
            rdb.adjust_house_balance(self.db, 9, 100_000, "demasiado bajo")


class StatsTests(RouletteDbBase):
    def test_stats(self):
        self.start()
        self.fund(self.alice, 100_000)
        self.fund(self.bob, 100_000)
        self.bet(self.alice, "red", 0, 1000)
        self.bet(self.bob, "black", 0, 3000)
        rdb.resolve_round(self.db, rdb.current_round(self.db, self.panel_id).id, force=True, number=1)      # alice gana 2000; casa +4000−2000
        self.bet(self.alice, "number", 9, 1000)
        rdb.resolve_round(self.db, rdb.current_round(self.db, self.panel_id).id, force=True, number=9)      # casa 1000 − 36000
        s = rdb.stats(self.db)
        self.assertEqual((s["rounds"], s["players"], s["staked"], s["paid"]), (2, 2, 5000, 38000))
        self.assertEqual((s["gains"], s["losses"]), (2000, 35000))
        self.assertEqual((s["biggest_payout"], s["biggest_house_loss"]), (36000, 35000))
        self.assertEqual(s["config"].house_balance, 1_000_000 + 2000 - 35000)


class FakeCtx:
    _n = 0

    def __init__(self, user, channel):
        FakeCtx._n += 1
        self.author, self.channel, self.guild = user, channel, channel.guild
        self.message = type("M", (), {"id": 7000000000000 + FakeCtx._n})()
        self.replies = []

    async def reply(self, text=None, **kw):
        self.replies.append(text)


class RouletteCogTests(RouletteDbBase):
    def setUp(self):
        super().setUp()
        self.cog = run(rl.setup_roulette(self.bot, self.db, 1))
        self.cog.ticker.cancel()

    def thread_of(self, panel):
        msg = next(m for m in self.panelch.sent if m.id == panel.message_id)
        self.bot.channels[msg.thread.id] = msg.thread
        self.assertEqual(msg.thread.id, panel.thread_id)
        return msg.thread

    def open(self, user=None, channel=None):
        i = self.inter(user or self.admin, channel)
        run(self.cog.ruleta(i))
        return i

    def test_only_admin_opens_and_only_one(self):
        i = self.open(self.alice)
        self.assertIn("Solo", i.text)
        self.assertIsNone(rdb.get_active_panel(self.db))
        i1 = self.open(self.admin)
        panel = rdb.get_active_panel(self.db)
        self.assertIsNotNone(panel)
        self.assertIsNotNone(panel.thread_id)
        i2 = self.open(self.council)
        self.assertIn("Ya hay una ruleta abierta", i2.text)
        self.assertIn("discord.com/channels", i2.text)
        self.assertEqual(rdb.get_active_panel(self.db).id, panel.id)

    def test_perms_comando_grants_roulette_admin(self):
        for c in ("ruleta", "ruleta-editar", "ruleta-stats"):
            self.assertIn(c, eco.GRANTABLE_COMMANDS)
        role = discord.Role(777001, "Crupier", self.guild)
        self.guild.roles.append(role)
        self.alice.roles.append(role)
        self.assertIn("Solo", self.open(self.alice).text)
        eco.grant_command_role(self.db, "ruleta", role.id)
        self.assertTrue(run(self.cog._allowed(self.alice, "ruleta")))
        self.assertFalse(run(self.cog._allowed(self.alice, "ruleta-editar")))      # el permiso es por comando
        self.assertTrue(self.open(self.alice).text.startswith("✅"))

    def test_bets_only_in_thread_and_with_gates(self):
        self.open()
        panel = rdb.get_active_panel(self.db)
        thread = self.thread_of(panel)
        self.fund(self.alice, 50_000)
        elsewhere = FakeCtx(self.alice, self.panelch)
        self.assertFalse(run(self.cog.handle_bet_command(elsewhere, "rojo 1000")))              # fuera del hilo no es de la ruleta
        ctx = FakeCtx(self.alice, thread)
        self.assertTrue(run(self.cog.handle_bet_command(ctx, "numero 17 2k")))
        self.assertIn("apostó", ctx.replies[-1])
        self.assertEqual(self.bal(self.alice), 48_000)
        ctx = FakeCtx(self.alice, thread)
        run(self.cog.handle_bet_command(ctx, "docena 2 abc"))
        self.assertTrue(ctx.replies[-1].startswith("❌"))
        ctx = FakeCtx(self.outsider, thread)                                                       # sin rol Miembro
        run(self.cog.handle_bet_command(ctx, "rojo 1000"))
        self.assertIn("miembros", ctx.replies[-1])

    def test_close_button_admin_only_refunds_and_updates(self):
        self.open()
        panel = rdb.get_active_panel(self.db)
        thread = self.thread_of(panel)
        self.fund(self.alice, 10_000)
        run(self.cog.handle_bet_command(FakeCtx(self.alice, thread), "rojo 1000"))
        msg = next(m for m in self.panelch.sent if m.id == panel.message_id)
        i = self.inter(self.alice, message=msg)
        run(self.cog.on_close(i))
        self.assertEqual(rdb.get_active_panel(self.db).id, panel.id)                               # un usuario normal no cierra
        i = self.inter(self.admin, message=msg)
        run(self.cog.on_close(i))
        self.assertIsNone(rdb.get_active_panel(self.db))
        self.assertEqual(self.bal(self.alice), 10_000)
        ctx = FakeCtx(self.alice, thread)
        run(self.cog.handle_bet_command(ctx, "rojo 1000"))
        self.assertIn("cerrada", ctx.replies[-1])
        self.assertTrue(self.open().text.startswith("✅"))                                         # se puede abrir otra

    def test_ticker_resolves_due_round(self):
        self.open()
        panel = rdb.get_active_panel(self.db)
        thread = self.thread_of(panel)
        self.fund(self.alice, 10_000)
        run(self.cog.handle_bet_command(FakeCtx(self.alice, thread), "rojo 1000"))
        import time
        self.assertEqual(run(self.cog.process_due(time.time() + 3600)), 1)
        self.assertEqual(rdb.last_finished_round(self.db).status, "done")
        self.assertIsNotNone(rdb.current_round(self.db, panel.id))                                 # nueva ronda


if __name__ == "__main__":
    unittest.main()
