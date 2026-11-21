import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import foco_db as fdb  # noqa: E402


class FocoDbTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)

    def tearDown(self):
        os.remove(self.db)

    def test_parse(self):
        for txt, val in (("30000", 30000), ("30.000", 30000), ("30,000", 30000), ("30k", 30000), ("1.5k", 1500),
                         ("1,2m", 1200000), (" 45 K ", 45000), ("0", 0)):
            self.assertEqual(fdb.parse_amount(txt), val, txt)
        for txt in ("", "mucho", "30000 foco", None):
            self.assertIsNone(fdb.parse_amount(txt), txt)

    def test_formato_corto(self):
        for n, txt in ((0, "0"), (950, "950"), (1000, "1k"), (150000, "150k"), (1500, "1.5k"), (1_500_000, "1.5m"),
                       (2_000_000_000, "2b"), (1_250_000_000, "1.2b")):
            self.assertEqual(fdb.fmt_short(n), txt, n)

    def test_acumula_y_no_duplica_ticket(self):
        self.assertEqual(fdb.user_total(self.db, 1), (0, 0))
        self.assertTrue(fdb.record(self.db, 1, 30000, 100, 9))
        self.assertTrue(fdb.record(self.db, 1, 20000, 101, 9))
        self.assertFalse(fdb.record(self.db, 1, 20000, 101, 9))        # mismo ticket
        self.assertTrue(fdb.record(self.db, 2, 5000, 102, 9))
        self.assertEqual(fdb.user_total(self.db, 1), (50000, 2))
        self.assertEqual(fdb.top(self.db), [(1, 50000, 2), (2, 5000, 1)])


if __name__ == "__main__":
    unittest.main()
