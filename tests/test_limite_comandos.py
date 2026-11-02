"""Guarda contra el límite de Discord: 100 comandos de barra RAÍZ por servidor (los grupos cuentan como 1, sus subcomandos no).
Cuenta por el código fuente (bot.py no se puede importar en las pruebas); es una estimación por arriba: si falla, hay que agrupar comandos."""
import ast
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMITE = 100
AVISO = 90          # avisa antes de llegar al tope


def comandos_raiz():
    raiz = []
    for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        nombre = os.path.basename(path)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    s = ast.unparse(d)
                    if s.split("(")[0] in ("app_commands.command", "bot.tree.command", "tree.command", "self.tree.command",
                                           "app_commands.context_menu", "bot.tree.context_menu"):
                        m = re.search(r"name='([^']+)'", s)
                        raiz.append((nombre, m.group(1) if m else n.name))
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and ast.unparse(n.value.func).endswith("Group"):
                if not any(k.arg == "parent" for k in n.value.keywords):
                    m = [k.value.value for k in n.value.keywords if k.arg == "name"]
                    if m:
                        raiz.append((nombre, "grupo " + m[0]))
    return raiz


class LimiteComandosTests(unittest.TestCase):
    def test_no_se_pasa_del_limite_de_discord(self):
        raiz = comandos_raiz()
        self.assertLessEqual(len(raiz), LIMITE, f"{len(raiz)} comandos raíz (máx. {LIMITE}). Agrupá comandos en un grupo /algo sub.")

    def test_aviso_antes_de_llegar_al_tope(self):
        raiz = comandos_raiz()
        self.assertLess(len(raiz), AVISO, f"{len(raiz)} comandos raíz: cerca del límite de {LIMITE}, conviene agrupar antes de agregar más.")


if __name__ == "__main__":
    unittest.main()
