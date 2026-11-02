"""E1: /rolalbion se eliminó; los ayudantes de Albion que usa «Agregar killboard» de los splits siguen en su lugar."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bot_tree():
    with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


class QuitarRolAlbion(unittest.TestCase):
    def test_comando_ya_no_esta_registrado(self):
        src, tree = _bot_tree()
        nombres = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertNotIn("rolalbion", nombres)
        self.assertNotIn("construir_indice_discord", nombres)
        self.assertNotIn('name="rolalbion"', src)

    def test_help_no_lo_menciona(self):
        src, _ = _bot_tree()
        self.assertNotIn("/rolalbion", src)

    def test_splits_no_lo_menciona(self):
        with open(os.path.join(ROOT, "splits.py"), encoding="utf-8") as f:
            self.assertNotIn("rolalbion", f.read())

    def test_ayudantes_compartidos_siguen(self):
        src, tree = _bot_tree()
        nombres = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for esperado in ("normalizar_nombre", "extraer_ids", "obtener_jugadores_batalla_albion",
                         "obtener_jugadores_albion", "albion_participantes_para_split"):
            self.assertIn(esperado, nombres)
        self.assertIn("set_albion_lookup(albion_participantes_para_split)", src)          # sigue enganchado a los splits


if __name__ == "__main__":
    unittest.main()
