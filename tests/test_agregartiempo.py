import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run  # noqa: E402,F401
import ctautil as cu  # noqa: E402


class T(unittest.TestCase):
    def test_formatos(self):
        p = cu.parse_extra_minutes
        self.assertEqual(p(None), 10)
        self.assertEqual(p(""), 10)
        self.assertEqual(p("15"), 15)
        self.assertEqual(p("15m"), 15)
        self.assertEqual(p("1h"), 60)
        self.assertEqual(p("1h30"), 90)
        self.assertEqual(p("00:20"), 20)
        self.assertEqual(p(" 2h "), 120)

    def test_invalidos(self):
        for bad in ("abc", "0", "00:00", "3h", "121", "00:75", "-5"):
            with self.assertRaises(ValueError, msg=bad):
                cu.parse_extra_minutes(bad)


if __name__ == "__main__":
    unittest.main()
