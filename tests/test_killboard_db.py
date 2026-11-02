import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import killboard_db as kb  # noqa: E402
import ctautil as cu  # noqa: E402
from dbutil import connect  # noqa: E402


def P(name, guild="Smogg", ip=1300):
    return {"Name": name, "GuildName": guild, "AverageItemPower": ip}


def ev(killer, victim, t, parts=(), group=()):
    return {"Killer": killer, "Victim": victim, "TimeStamp": t, "Participants": list(parts), "GroupMembers": list(group)}


EVENTS = [
    ev(P("Enemigo", "Otros"), P("Ana", ip=1400), "2026-10-31T22:05:00Z"),
    ev(P("Bob", ip=1200), P("Rival", "Otros"), "2026-10-31T22:02:00Z", group=[P("Cami", ip=1100)]),
    ev(P("Enemigo", "Otros"), P("Bob", ip=1250), "2026-10-31T22:01:00Z"),
    ev(P("Enemigo", "Otros"), P("Ana", ip=1450), "2026-10-31T22:09:00Z"),          # Ana muere dos veces
    ev(P("Enemigo", "Otros"), P("Dan"), "2026-10-31T22:03:00Z"),
    ev(P("Enemigo", "Otros"), P("Eli"), "2026-10-31T22:04:00Z"),
    ev(P("Enemigo", "Otros"), P("Fer"), "2026-10-31T22:06:00Z"),                  # 6° muerto distinto
    ev(P("Enemigo", "Otros"), P("Rival2", "Otros"), None),
]


class ParseTests(unittest.TestCase):
    def test_parse(self):
        r = kb.parse_events(EVENTS, "Smogg")
        pl = r["players"]
        self.assertNotIn("enemigo", pl); self.assertNotIn("rival", pl)                 # solo la guild
        self.assertEqual((pl["bob"]["kills"], pl["bob"]["deaths"]), (1, 1))
        self.assertEqual(pl["ana"]["deaths"], 2)
        self.assertIn("cami", pl)                                                        # asistió (GroupMembers)
        self.assertEqual(pl["ana"]["ip_max"], 1450); self.assertEqual(pl["ana"]["ip_min"], 1400)
        first = {k for k, v in pl.items() if v["first5"]}
        self.assertEqual(first, {"bob", "dan", "eli", "ana", "fer"})                      # los 5 primeros distintos
        self.assertEqual(r["smogg_deaths"], 6)

    def test_five_distinct_players_in_order(self):
        r = kb.parse_events(EVENTS, "smogg")
        ranks = {k: v["death_rank"] for k, v in r["players"].items() if v["death_rank"]}
        self.assertEqual(ranks, {"bob": 1, "dan": 2, "eli": 3, "ana": 4, "fer": 5})
        self.assertFalse(r["players"]["cami"]["first5"])

    def test_missing_timestamp_counts(self):
        r = kb.parse_events([ev(P("X", "O"), P("Ana"), None)], "Smogg")
        self.assertEqual(r["ts_missing"], 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        kb.init_schema(self.db); cu.init_positions(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupe_delete_and_tops(self):
        p = kb.parse_events(EVENTS, "Smogg")
        self.assertEqual(kb.save_battle(self.db, "100", 7, 1, p), "new")
        self.assertEqual(kb.save_battle(self.db, "100", None, 1, p), "dup")          # no se carga dos veces
        p2 = kb.parse_events([ev(P("Enemigo", "O"), P("Ana", ip=1500), "2026-10-31T23:00:00Z")], "Smogg")
        kb.save_battle(self.db, "200", None, 1, p2)                                   # suelto
        s = kb.player_stats(self.db, ["ana"])
        self.assertEqual((s["attendance"], s["deaths"], s["first5"]), (2, 3, 2))
        self.assertAlmostEqual(s["first5_pct"], 100.0)
        self.assertEqual(s["max_ip"], 1500); self.assertEqual(s["min_ip"], 1400)
        self.assertEqual(kb.top(self.db, "deaths")[0][0], "Ana")
        self.assertEqual(kb.top(self.db, "attendance")[0][:2], ("Ana", 2))
        self.assertEqual(kb.top(self.db, "min_ip")[0][0], "Cami")                    # el más bajo
        self.assertTrue(kb.delete_battle(self.db, "200", 9))
        self.assertFalse(kb.delete_battle(self.db, "200", 9))
        self.assertEqual(kb.player_stats(self.db, ["ana"])["attendance"], 1)
        self.assertEqual(kb.save_battle(self.db, "200", None, 1, p2), "new")         # tras borrar se puede volver a cargar

    def test_roles_cuentan_todas_las_cta_cerradas_con_o_sin_killboard(self):
        c = connect(self.db)
        c.execute("CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, status TEXT)")
        for cta, status in ((7, "closed"), (8, "closed"), (9, "closed"), (10, "open"), (11, "cancelled")):
            c.execute("INSERT INTO cta_runs(id, status) VALUES (?,?)", (cta, status))
        for cta, role, name in ((7, 111, "Tank"), (8, 111, "Tank"), (9, 222, "Healer"), (10, 111, "Tank"), (11, 111, "Tank")):
            c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (?,?,?,?,?)", (cta, 55, "1:1", role, name))
        c.close()
        kb.save_battle(self.db, "100", 7, 1, kb.parse_events(EVENTS, "Smogg"))          # solo la CTA 7 tiene killboard: igual cuentan las 3 cerradas
        self.assertEqual(kb.role_counts(self.db, 55, ["ana"]), {111: 2, 222: 1})        # la abierta (10) y la cancelada (11) no cuentan
        self.assertEqual(kb.role_counts(self.db, 55), {111: 2, 222: 1})                 # no depende de los nombres del killboard
        self.assertEqual(kb.role_counts(self.db, 99), {})

    def test_roles_sin_rol_cargado_se_cuentan_por_nombre(self):
        c = connect(self.db)
        c.execute("CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, status TEXT)")
        for cta in (1, 2, 3):
            c.execute("INSERT INTO cta_runs(id, status) VALUES (?, 'closed')", (cta,))
        c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (1,55,'1:1',NULL,'Scout')")
        c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (2,55,'1:1',NULL,'Scout')")
        c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (3,55,'1:1',NULL,NULL)")   # sin nada: no se cuenta
        c.close()
        self.assertEqual(kb.role_counts(self.db, 55), {"Scout": 2})


if __name__ == "__main__":
    unittest.main()
