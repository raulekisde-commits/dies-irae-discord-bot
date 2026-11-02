import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import test_give_deposits  # noqa: E402,F401  (instala el stub de discord)
import bets  # noqa: E402


class ApuestaGroupTests(unittest.TestCase):
    def test_apuestas_viven_bajo_apuesta(self):
        g = bets.BetsCog.apuesta_g
        self.assertEqual(g.name, "apuesta")
        self.assertEqual(sorted(g.commands), ["apostar", "cancelar", "cerrar", "crear", "resolver", "todo"])


if __name__ == "__main__":
    unittest.main()
