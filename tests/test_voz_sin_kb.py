import unittest
import test_give_deposits  # noqa: F401
import killboards as k


class VoiceKb(unittest.TestCase):
    def test_clasifica(self):
        regs = [(1, "Ana"), (2, "Beto"), (3, "Cami")]
        missing, unknown = k.voice_without_kb({1, 2, 3, 4, 5}, {3}, regs, {"ana"})
        self.assertEqual(missing, [(2, ["Beto"])])
        self.assertEqual(unknown, [4, 5])

    def test_texto(self):
        self.assertIn("Falta cargar", k.voice_kb_text([], [], False))
        t = k.voice_kb_text([(2, ["Beto"])], [4], True)
        self.assertIn("<@2> (Beto)", t)
        self.assertIn("Sin personaje registrado", t)
        self.assertIn("Nadie", k.voice_kb_text([], [], True))
        self.assertLessEqual(len(k.voice_kb_text([(i, ["x"]) for i in range(500)], [], True)), 2000)
