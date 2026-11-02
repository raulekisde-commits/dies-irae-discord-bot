import os, sys, tempfile, unittest, json
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ctavoice as cv  # noqa: E402
import ctautil as cu  # noqa: E402
from dbutil import connect  # noqa: E402

T0 = datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc)
m = lambda n: T0 + timedelta(minutes=n)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        c = connect(self.db)
        c.executescript("""CREATE TABLE cta_runs(id INTEGER PRIMARY KEY, snapshot_json TEXT, status TEXT, final_state TEXT, final_by INTEGER, final_at TEXT, thread_id INTEGER);""")
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status) VALUES (1, '{\"slots\": []}', 'closed')")
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status) VALUES (2, '{\"slots\": []}', 'open')")
        c.close()
        cu.init_positions(self.db); cv.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tracking_runs(self):
        self.assertEqual(cv.tracking_runs(self.db), [1])
        cu.begin_final(self.db, 1, "finished", 9)
        self.assertEqual(cv.tracking_runs(self.db), [])

    def test_minimum_two_minutes_and_sessions_add_up(self):
        cv.user_joined(self.db, 1, 10, m(0)); cv.user_left(self.db, 1, 10, m(1))          # 1 min
        cv.user_joined(self.db, 1, 10, m(5)); cv.user_left(self.db, 1, 10, m(6))          # +1 min = 2
        cv.user_joined(self.db, 1, 11, m(0)); cv.user_left(self.db, 1, 11, m(1))          # solo 1 min
        cv.user_joined(self.db, 1, 12, m(10))                                              # sigue conectado
        now = m(20)
        self.assertEqual(cv.present_users(self.db, 1, now), {10, 12})
        cv.stop(self.db, 1, m(30))
        self.assertAlmostEqual(cv.seconds_by_user(self.db, 1, m(99))[12], 20 * 60)

    def test_join_twice_does_not_duplicate(self):
        cv.user_joined(self.db, 1, 10, m(0)); cv.user_joined(self.db, 1, 10, m(1))
        self.assertAlmostEqual(cv.seconds_by_user(self.db, 1, m(10))[10], 600)

    def test_sync_after_restart(self):
        cv.user_joined(self.db, 1, 10, m(0)); cv.user_joined(self.db, 1, 11, m(0))
        cv.sync(self.db, 1, [11, 12], m(5))                  # 10 se fue mientras el bot estaba caído, 12 entró
        s = cv.seconds_by_user(self.db, 1, m(10))
        self.assertAlmostEqual(s[10], 300); self.assertAlmostEqual(s[11], 600); self.assertAlmostEqual(s[12], 300)

    def test_compare_with_signups(self):
        conn = connect(self.db)
        for u in (10, 11, 13):
            conn.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key) VALUES (1,?,'1:1')", (u,))
        conn.close()
        cv.user_joined(self.db, 1, 10, m(0)); cv.user_left(self.db, 1, 10, m(30))         # anotado y presente
        cv.user_joined(self.db, 1, 99, m(0)); cv.user_left(self.db, 1, 99, m(30))         # topo: en voz y no anotado
        cv.user_joined(self.db, 1, 11, m(0)); cv.user_left(self.db, 1, 11, m(1))          # anotado, pasó 1 min -> no cuenta
        not_voice, extra = cv.compare_with_signups(self.db, 1, m(40))
        self.assertEqual(not_voice, [11, 13])
        self.assertEqual(extra, [99])


if __name__ == "__main__":
    unittest.main()
