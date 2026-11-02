import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import test_give_deposits  # noqa: E402,F401  (instala el stub de discord)
import coaching  # noqa: E402


class CoachingGroupTests(unittest.TestCase):
    def test_coaching_bajo_un_solo_comando(self):
        g = coaching.CoachingCog.coaching_g
        self.assertEqual(g.name, "coaching")
        self.assertEqual(sorted(g.commands), ["mis-vods", "reporte", "verificar-posts"])


if __name__ == "__main__":
    unittest.main()
