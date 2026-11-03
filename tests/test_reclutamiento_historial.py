"""El postulante tiene que poder ver el historial de mensajes de su ticket de reclutamiento (permiso explícito en el canal).
bot.py no se puede importar en las pruebas: se revisa el código fuente."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HistorialReclutamiento(unittest.TestCase):
    def test_postulante_ve_historial(self):
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
            src = f.read()
        bloque = src[src.index("async def create_recruit_ticket"):]
        m = re.search(r"user: discord\.PermissionOverwrite\(([^)]*)\)", bloque)
        self.assertIsNotNone(m)
        self.assertIn("read_message_history=True", m.group(1))
        self.assertIn("view_channel=True", m.group(1))


if __name__ == "__main__":
    unittest.main()
