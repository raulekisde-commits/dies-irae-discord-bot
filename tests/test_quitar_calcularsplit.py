"""E7: /calcularsplit se eliminó junto con sus funciones de cálculo; /tabsell y fmt_num siguen."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class QuitarCalcularSplit(unittest.TestCase):
    def test_comando_y_funciones_ya_no_estan(self):
        src = _read("bot.py")
        nombres = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for gone in ("calcularsplit_slash", "calcularsplit_error", "parse_number_sum", "calc_split_final", "build_split_calc_panel"):
            self.assertNotIn(gone, nombres)
        self.assertNotIn("calcularsplit", src)
        self.assertNotIn("SPLIT_CALC_", src)

    def test_nada_mas_lo_referencia(self):
        for name in os.listdir(ROOT):
            if name.endswith(".py"):
                self.assertNotIn("calcularsplit", _read(name), name)

    def test_tabsell_y_fmt_num_siguen(self):
        src = _read("bot.py")
        nombres = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("tabsell", nombres)
        self.assertIn("fmt_num", nombres)
        self.assertIn('name="tabsell"', src)


if __name__ == "__main__":
    unittest.main()
