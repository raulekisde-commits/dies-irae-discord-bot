import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import audit_db as au  # noqa: E402
import duels_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import games_db as gdb  # noqa: E402
from dbutil import connect  # noqa: E402


def res(i, players, winner, kind="coinflip", stake=1000):
    return au.Result(kind, i, f"2026-10-{1 + i // 50:02d}T00:{i % 50:02d}:00Z", stake, list(players), winner)


class LuckTests(unittest.TestCase):
    def test_flags_someone_who_wins_everything(self):
        rs = [res(i, (1, 100 + i), 1) for i in range(40)]
        luck, streaks = au.luck_report(rs)
        self.assertEqual([r.user_id for r in luck], [1])
        self.assertGreater(luck[0].z, 5)
        self.assertEqual((streaks[0].user_id, streaks[0].length), (1, 40))

    def test_fair_alternation_is_not_flagged(self):
        rs = [res(i, (1, 2), 1 if i % 2 == 0 else 2) for i in range(60)]
        luck, streaks = au.luck_report(rs)
        self.assertEqual((luck, streaks), ([], []))

    def test_too_few_games_are_ignored(self):
        luck, _ = au.luck_report([res(i, (1, 100 + i), 1) for i in range(10)])
        self.assertEqual(luck, [])

    def test_multiplayer_dice_expectation_uses_one_over_n(self):
        rs = [res(i, (1, 2, 3, 4), 1, kind="dice") for i in range(25)]        # gana 25 de 25 con 1/4 de chance cada una
        luck, _ = au.luck_report(rs)
        self.assertEqual(luck[0].expected, 25 / 4)


class PairTests(unittest.TestCase):
    def test_pair_that_only_plays_each_other_with_one_way_flow(self):
        rs = [res(i, (1, 2), 1 if i < 11 else 2, kind="duel", stake=500) for i in range(12)]
        pairs = au.pair_report(rs)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual((p.a, p.b, p.games, p.wins_a, p.wins_b), (1, 2, 12, 11, 1))
        self.assertEqual(p.flow_to_a, 500 * 10)                                 # 11 ganadas − 1 perdida
        self.assertGreater(p.z, 2.5)

    def test_casual_opponents_are_not_pairs(self):
        rs = [res(i, (1, 100 + (i % 20)), 1 if i % 2 else 100 + (i % 20)) for i in range(40)]
        self.assertEqual(au.pair_report(rs), [])


class TransferTests(unittest.TestCase):
    def test_one_way_sender_and_collector(self):
        tr = [(1, 2, 400000)] * 4 + [(5, 9, 10)] + [(6, 9, 10), (7, 9, 10), (8, 9, 10)]
        rows, collectors = au.transfer_report(tr)
        self.assertEqual([(r.sender, r.receiver, r.total, r.count) for r in rows], [(1, 2, 1_600_000, 4)])
        self.assertEqual([(c.user_id, c.senders) for c in collectors], [(9, 4)])

    def test_round_trips_are_not_one_way(self):
        tr = [(1, 2, 100)] * 3 + [(2, 1, 100)] * 3
        self.assertEqual(au.transfer_report(tr), ([], []))


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        eco.init_schema(self.db) if hasattr(eco, "init_schema") else None
        gdb.init_schema(self.db)
        ddb.init_schema(self.db)

    def test_collects_duels_and_games_and_transfers_and_builds_report(self):
        conn = connect(self.db)
        for i in range(12):
            conn.execute("INSERT INTO duels(guild_id, channel_id, challenger_id, challenged_id, stake, status, created_at, ends_at, resolved_at, winner_id) "
                         "VALUES (1,1,10,11,100,'done','x',0,?,10)", (f"2026-10-0{1 + i % 9}T00:00:00Z",))
        conn.execute("INSERT INTO duels(guild_id, channel_id, challenger_id, challenged_id, stake, status, created_at, ends_at, resolved_at, winner_id) "
                     "VALUES (1,1,10,11,100,'declined','x',0,NULL,NULL)")
        conn.commit()
        conn.close()
        for u in (10, 11):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[u], amount=1000, request_key=f"a{u}")
        for n in range(3):
            eco.transfer(self.db, from_id=10, to_id=11, amount=100, request_key=f"t{n}")
        rep = au.build_report(self.db, days=None)
        self.assertEqual(rep.games, 12)
        self.assertEqual(len(rep.pairs), 1)
        self.assertEqual([(t.sender, t.receiver, t.total) for t in rep.transfers], [(10, 11, 300)])
        text = au.format_sections(rep, lambda n: f"{n:,}")
        self.assertIn("<@10>", text["pairs"][0])
        self.assertIn("nunca le devolvió", text["transfers"][0])

    def test_empty_database_gives_empty_report(self):
        rep = au.build_report(self.db, days=30)
        self.assertEqual((rep.games, rep.luck, rep.pairs, rep.transfers), (0, [], [], []))


if __name__ == "__main__":
    unittest.main()
