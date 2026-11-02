import os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ctautil as cu  # noqa: E402

U = timezone.utc


class TitleTests(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(cu.base_title(datetime(2026, 10, 31, 22, 15, tzinfo=U)), "CTA 31/10 22 UTC")
        self.assertEqual(cu.base_title(datetime(2026, 10, 31, 21, 50, tzinfo=U)), "CTA 31/10 22 UTC")
        self.assertEqual(cu.base_title(datetime(2026, 10, 31, 22, 30, tzinfo=U)), "CTA 31/10 23 UTC")     # a las :30 sube
        self.assertEqual(cu.base_title(datetime(2026, 10, 31, 23, 45, tzinfo=U)), "CTA 01/11 00 UTC")     # cambia la fecha
        self.assertEqual(cu.base_title(datetime(2026, 10, 31, 19, 15, tzinfo=timezone(timedelta(hours=-3)))), "CTA 31/10 22 UTC")

    def test_collision(self):
        b = "CTA 31/10 22 UTC"
        self.assertEqual(cu.unique_title(b, []), b)
        self.assertEqual(cu.unique_title(b, [b]), b + " #2")
        self.assertEqual(cu.unique_title(b, [b, b + " #2"]), b + " #3")
        self.assertEqual(cu.unique_title(b, [b.lower()]), b + " #2")


class PublishTimeTests(unittest.TestCase):
    now = datetime(2026, 10, 31, 20, 0, tzinfo=U)

    def test_parse(self):
        self.assertEqual(cu.parse_hhmm_utc("21:30", self.now), datetime(2026, 10, 31, 21, 30, tzinfo=U))
        self.assertEqual(cu.parse_hhmm_utc("19:00", self.now), datetime(2026, 11, 1, 19, 0, tzinfo=U))
        for bad in ("", "25:00", "9", "ab:cd", "12:60"):
            with self.assertRaises(ValueError):
                cu.parse_hhmm_utc(bad, self.now)


class RoleTests(unittest.TestCase):
    TANK, HEAL, FLEX = 1260755129754189854, 1260755151296266331, 1558522576743964762

    def test_access(self):
        self.assertIsNone(cu.access_missing_role([self.TANK], [str(self.TANK)], self.FLEX))
        self.assertEqual(cu.access_missing_role([self.HEAL], [str(self.TANK)], self.FLEX), self.TANK)
        self.assertIsNone(cu.access_missing_role([self.FLEX], [str(self.TANK)], self.FLEX))       # Flex ignora el permiso
        self.assertIsNone(cu.access_missing_role([], [], self.FLEX))                              # sin rol cargado: libre

    def test_migrate(self):
        new, changed = cu.migrate_role_ids('["%d","%d"]' % (self.TANK, self.HEAL))
        self.assertTrue(changed)
        self.assertEqual(new, '["%d"]' % self.TANK)
        self.assertFalse(cu.migrate_role_ids('["%d"]' % self.TANK)[1])
        self.assertFalse(cu.migrate_role_ids("[]")[1])
        self.assertEqual(len(cu.PLAYED_ROLES), 11)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        cu.init_schedule(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, at):
        return cu.schedule(self.db, guild_id=1, composition_name="Brawl", minutes=40, title=None, publish_at=at, created_by=5)

    def test_due_once(self):
        now = datetime.now(U)
        self.add(now + timedelta(minutes=10))
        a = self.add(now - timedelta(seconds=10))
        go, dropped = cu.take_due(self.db, now)
        self.assertEqual([d["id"] for d in go], [a])
        self.assertEqual(dropped, [])
        self.assertEqual(cu.take_due(self.db, now), ([], []))                  # no se entrega dos veces
        self.assertEqual(len(cu.pending_list(self.db, 1)), 1)

    def test_late_is_dropped(self):
        now = datetime.now(U)
        self.add(now - timedelta(minutes=30))                                  # el bot estaba caído
        go, dropped = cu.take_due(self.db, now)
        self.assertEqual(go, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(cu.take_due(self.db, now), ([], []))

    def test_cancel(self):
        sid = self.add(datetime.now(U) + timedelta(hours=1))
        self.assertTrue(cu.cancel_scheduled(self.db, sid, 1))
        self.assertFalse(cu.cancel_scheduled(self.db, sid, 1))
        self.assertEqual(cu.pending_list(self.db, 1), [])


if __name__ == "__main__":
    unittest.main()


import json  # noqa: E402
from dbutil import connect  # noqa: E402


class PositionsTests(unittest.TestCase):
    TANK, HEAL = 1260755129754189854, 1260755151296266331

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        c = connect(self.db)
        c.executescript("""CREATE TABLE cta_runs(id INTEGER PRIMARY KEY, snapshot_json TEXT, status TEXT, final_state TEXT, final_by INTEGER, final_at TEXT, thread_id INTEGER);
                           CREATE TABLE cta_signups(id INTEGER PRIMARY KEY, cta_id INTEGER, slot_key TEXT, user_id INTEGER);""")
        slots = [{"party_no": 1, "position_no": 1, "role_name": "Golem", "role_ids": [str(self.TANK)]},
                 {"party_no": 1, "position_no": 2, "role_name": "Monarch", "role_ids": [str(self.HEAL)]},
                 {"party_no": 2, "position_no": 1, "role_name": "Libre", "role_ids": []}]
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status) VALUES (1, ?, 'closed')", (json.dumps({"slots": slots}),))
        c.execute("INSERT INTO cta_signups(cta_id, slot_key, user_id) VALUES (1,'1:2',10)")
        c.execute("INSERT INTO cta_signups(cta_id, slot_key, user_id) VALUES (1,'1:1',11)")
        c.close()
        cu.init_positions(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bomb(self):
        BOMB = cu.BOMB_ROLE_ID
        self.assertEqual(cu.bomb_add(self.db, 1, 50), "new")
        self.assertEqual(cu.bomb_add(self.db, 1, 50), "exists")
        self.assertEqual(cu.bomb_add(self.db, 1, 10), "regular")                 # ya tiene posición en la compo
        self.assertEqual(cu.bomb_add(self.db, 99, 50), "missing")
        self.assertEqual(cu.bomb_list(self.db, 1), [50])
        pos = {p["user_id"]: p for p in cu.positions(self.db, 1)}
        self.assertEqual((pos[50]["role_id"], pos[50]["role_name"]), (BOMB, "Bomb"))   # cuenta como rol jugado: Bomb
        cu.snapshot_positions(self.db, 1)                                        # el cierre de la inscripción no pisa al Bomb
        pos = {p["user_id"]: p for p in cu.positions(self.db, 1)}
        self.assertEqual(pos[50]["role_id"], BOMB)
        self.assertIn(10, pos)
        conn = connect(self.db)
        conn.execute("UPDATE cta_runs SET final_state='finished' WHERE id=1")
        conn.close()
        self.assertEqual(cu.bomb_add(self.db, 1, 51), "final")

    def test_bomb_varios_paneles(self):
        a = cu.bomb_panel_add(self.db, 1, 5, 100, 200)
        b = cu.bomb_panel_add(self.db, 1, 5, 101, 201)
        self.assertEqual((a["n"], b["n"]), (1, 2))
        self.assertEqual(cu.bomb_add(self.db, 1, 50, a["id"]), "new")
        self.assertEqual(cu.bomb_add(self.db, 1, 51, b["id"]), "new")
        self.assertEqual(cu.bomb_add(self.db, 1, 51, a["id"]), "exists")        # un Bomb cuenta una sola vez en la CTA
        self.assertEqual(cu.bomb_list(self.db, 1, a["id"]), [50])
        self.assertEqual(cu.bomb_list(self.db, 1, b["id"]), [51])
        self.assertEqual(sorted(cu.bomb_list(self.db, 1)), [50, 51])             # el split y el panel principal los toman a todos
        self.assertEqual(cu.bomb_panel_by(self.db, message_id=101)["id"], b["id"])
        self.assertEqual(cu.bomb_panel_by(self.db, thread_id=200)["id"], a["id"])
        self.assertIsNone(cu.bomb_panel_by(self.db, thread_id=999))
        self.assertEqual(len(cu.bomb_panels(self.db, 1)), 2)

    def test_bomb_migrar_panel_viejo(self):
        conn = connect(self.db)
        for col in ("bomb_channel_id", "bomb_message_id", "bomb_thread_id"):
            conn.execute(f"ALTER TABLE cta_runs ADD COLUMN {col} INTEGER")
        conn.execute("UPDATE cta_runs SET bomb_channel_id=5, bomb_message_id=100, bomb_thread_id=200 WHERE id=1")
        conn.close()
        cu.bomb_add(self.db, 1, 50)
        self.assertEqual(cu.bomb_migrate_old(self.db), 1)
        self.assertEqual(cu.bomb_migrate_old(self.db), 0)                        # no duplica
        p = cu.bomb_panel_by(self.db, thread_id=200)
        self.assertEqual(cu.bomb_list(self.db, 1, p["id"]), [50])

    def test_bomb_text(self):
        for t in ("+", " + ", "Bomb", "anotame", "Me anoto!", "presente."):
            self.assertTrue(cu.is_bomb_join_text(t), t)
        for t in ("", "hola", "21", "+1 mas"):
            self.assertFalse(cu.is_bomb_join_text(t), t)

    def test_snapshot_once(self):
        self.assertEqual(cu.snapshot_positions(self.db, 1), 2)
        self.assertEqual(cu.snapshot_positions(self.db, 1), 0)                    # no se duplica
        pos = {p["user_id"]: p for p in cu.positions(self.db, 1)}
        self.assertEqual(pos[10]["role_id"], self.HEAL)
        self.assertEqual(pos[11]["role_name"], "Golem")

    def test_late_accept_uses_global_number(self):
        cu.snapshot_positions(self.db, 1)
        self.assertTrue(cu.add_late_position(self.db, 1, 12, 3))                  # número 3 = party 2 lugar 1
        p = {x["user_id"]: x for x in cu.positions(self.db, 1)}[12]
        self.assertEqual((p["slot_key"], p["late"], p["role_id"]), ("2:1", 1, None))
        self.assertTrue(cu.add_late_position(self.db, 1, 13, None))               # sin posición pedida
        self.assertEqual(len(cu.positions(self.db, 1)), 4)

    def test_final_blocks_late(self):
        cu.snapshot_positions(self.db, 1)
        self.assertEqual(cu.begin_final(self.db, 1, "finished", 5), "ok")
        self.assertEqual(cu.begin_final(self.db, 1, "finished", 5), "already")    # una sola vez
        self.assertEqual(cu.begin_final(self.db, 1, "cancelled", 5), "already")   # ya no se puede cancelar
        self.assertFalse(cu.add_late_position(self.db, 1, 12, 1))
        self.assertEqual(cu.final_state(self.db, 1), "finished")

    def test_cancel_and_open_cannot_finish(self):
        c = connect(self.db)
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status) VALUES (2, '{\"slots\": []}', 'open')")
        c.close()
        self.assertEqual(cu.begin_final(self.db, 2, "finished", 5), "not_closed")
        self.assertEqual(cu.begin_final(self.db, 2, "cancelled", 5), "ok")        # cancelar vale con la inscripción abierta
        self.assertEqual(cu.begin_final(self.db, 99, "cancelled", 5), "missing")

    def test_tolerant_without_tables(self):
        other = os.path.join(self.tmp.name, "empty.db")
        self.assertIsNone(cu.final_state(other, 1))
        self.assertIsNone(cu.thread_id_of(other, 1))
        self.assertFalse(cu.add_late_position(other, 1, 1, 1))
