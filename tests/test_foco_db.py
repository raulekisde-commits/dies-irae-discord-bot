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

    def test_acumula_y_no_duplica_ticket(self):
        self.assertEqual(fdb.user_total(self.db, 1), (0, 0))
        self.assertTrue(fdb.record(self.db, 1, 30000, 100, 9))
        self.assertTrue(fdb.record(self.db, 1, 20000, 101, 9))
        self.assertFalse(fdb.record(self.db, 1, 20000, 101, 9))        # mismo ticket
        self.assertTrue(fdb.record(self.db, 2, 5000, 102, 9))
        self.assertEqual(fdb.user_total(self.db, 1), (50000, 2))


if __name__ == "__main__":
    unittest.main()
