import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import partycheck_db as pc  # noqa: E402
from dbutil import connect  # noqa: E402


def W(text, y):
    return {"description": text, "boundingPoly": {"vertices": [{"x": 0, "y": y}, {"x": 9, "y": y}, {"x": 9, "y": y + 10}, {"x": 0, "y": y + 10}]}}


VISION = {"textAnnotations": [W("todo", 0)] + [
    W("Region", 10), W("Access", 10), W("Priority", 10), W("Party", 80), W("Priority", 80), W("Party's", 100),
    W("Alliance/Guild:", 100), W("Party", 120), W("Member", 120), W("Priority", 120), W("1", 120), W("First", 120),
    W("DomiAkd", 150), W("Rexsort", 150), W("-", 150), W("lordthemarth", 175), W("Geladinhaa", 175),
    W("Arg2prmrd", 375), W("Wujumecanicos", 325), W("TRIGORE", 200),
]}


class ExtractTests(unittest.TestCase):
    def test_solo_nicks_debajo_del_encabezado(self):
        self.assertEqual(pc.extract_names(VISION),
                         ["DomiAkd", "Rexsort", "lordthemarth", "Geladinhaa", "Arg2prmrd", "Wujumecanicos", "TRIGORE"])

    def test_sin_encabezado_filtra_ui(self):
        v = {"textAnnotations": [W("x", 0), W("Party", 5), W("Foo123", 10), W("ab", 10)]}
        self.assertEqual(pc.extract_names(v), ["Foo123"])

    def test_tolerancia(self):
        self.assertTrue(pc.same("Ismaelgrt", "lsmaelgrt"))
        self.assertTrue(pc.same("Creddyj", "Creddy"))
        self.assertFalse(pc.same("Rexsort", "Rexxsortt"))


class CompareTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)
        c = connect(self.db)
        c.executescript("""
            CREATE TABLE cta_runs(id INTEGER PRIMARY KEY, thread_id INTEGER);
            CREATE TABLE cta_signups(id INTEGER PRIMARY KEY, cta_id INTEGER, slot_key TEXT, user_id INTEGER);
            CREATE TABLE albion_links(discord_id INTEGER, char_name TEXT);
            CREATE TABLE alters(user_id INTEGER, alter_name TEXT);
            INSERT INTO cta_runs VALUES (7, 555);
            INSERT INTO cta_signups(cta_id, slot_key, user_id) VALUES (7,'1:1',1),(7,'1:2',2),(7,'1:3',3),(7,'1:4',4),(7,'2:1',5);
            INSERT INTO albion_links VALUES (1,'DomiAkd'),(2,'Ismaelgrt'),(3,'MainDe3'),(5,'Rexsort');
            INSERT INTO alters VALUES (3,'TRIGORE');
        """)
        c.close()

    def tearDown(self):
        os.remove(self.db)

    def test_cta_por_hilo(self):
        self.assertEqual(pc.cta_for_channel(self.db, 555)["id"], 7)
        self.assertIsNone(pc.cta_for_channel(self.db, 1))

    def test_compara(self):
        rows = pc.signups(self.db, 7)
        res = pc.compare(["DomiAkd", "TRIGORE", "Rexsort", "Intruso"], rows)
        self.assertEqual(res["party_no"], 1)
        self.assertEqual(sorted(r["user_id"] for r, _ in res["ok"]), [1, 3])          # 3 entra con su alter
        self.assertEqual([r["user_id"] for r in res["missing"]], [2])
        self.assertEqual([r["user_id"] for r in res["unlinked"]], [4])
        self.assertEqual(res["extra"], [("Rexsort", 2), ("Intruso", None)])

    def test_party_forzada(self):
        res = pc.compare(["Rexsort"], pc.signups(self.db, 7), 2)
        self.assertEqual((res["party_no"], len(res["ok"]), res["extra"]), (2, 1, []))


if __name__ == "__main__":
    unittest.main()
