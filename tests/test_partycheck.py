import io, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import partycheck_db as pc  # noqa: E402
from dbutil import connect  # noqa: E402

WORDS = [
    ("Region", 10), ("Access", 10), ("Priority", 10), ("Party", 80), ("Priority", 80), ("Party's", 100),
    ("Alliance/Guild:", 100), ("Party", 120), ("Member", 120), ("Priority", 120), ("1", 120), ("First", 120),
    ("DomiAkd", 150), ("Rexsort", 150), ("-", 150), ("lordthemarth", 175), ("Geladinhaa", 175),
    ("Arg2prmrd", 375), ("Wujumecanicos", 325), ("TRIGORE", 200),
]


class ExtractTests(unittest.TestCase):
    def test_solo_nicks_debajo_del_encabezado(self):
        self.assertEqual(pc.extract_names(WORDS),
                         ["DomiAkd", "Rexsort", "lordthemarth", "Geladinhaa", "Arg2prmrd", "Wujumecanicos", "TRIGORE"])

    def test_sin_encabezado_filtra_ui(self):
        self.assertEqual(pc.extract_names([("x", 0), ("Party", 5), ("Foo123", 10), ("ab", 10)]), ["Foo123"])

    def test_respuesta_ocrspace(self):
        resp = {"ParsedResults": [{"TextOverlay": {"Lines": [
            {"Words": [{"WordText": "Party", "Top": 100, "Height": 10}, {"WordText": "Member", "Top": 100, "Height": 10}]},
            {"Words": [{"WordText": "DomiAkd", "Top": 130, "Height": 10}, {"WordText": "Rexsort", "Top": 131, "Height": 10}]},
        ]}}]}
        self.assertEqual(pc.extract_names(pc.ocrspace_words(resp)), ["DomiAkd", "Rexsort"])

    def test_tolerancia(self):
        self.assertTrue(pc.same("Ismaelgrt", "lsmaelgrt"))
        self.assertTrue(pc.same("Creddyj", "Creddy"))
        self.assertFalse(pc.same("Rexsort", "Rexxsortt"))


class ShrinkTests(unittest.TestCase):
    def test_achica_imagen_grande(self):
        try:
            import partycheck
            from PIL import Image
        except Exception as exc:                          # sin discord/aiohttp instalados en el entorno de pruebas
            self.skipTest(str(exc))
        buf = io.BytesIO()
        Image.frombytes("RGB", (1500, 1500), os.urandom(1500 * 1500 * 3)).save(buf, "PNG")
        data, name = partycheck.shrink(buf.getvalue(), "x.png")
        self.assertLessEqual(len(data), partycheck.OCR_MAX_BYTES)
        self.assertEqual(name, "party.jpg")


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

    def test_api_key(self):
        pc.init_schema(self.db)
        self.assertIsNone(pc.get_api_key(self.db))
        pc.set_api_key(self.db, "K1", 9)
        pc.set_api_key(self.db, "K2", 9)
        self.assertEqual(pc.get_api_key(self.db), "K2")

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
