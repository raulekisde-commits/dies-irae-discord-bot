import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase  # noqa: E402,F401  (instala el stub de discord)

ROOT = os.path.dirname(HERE)


def source(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


class SinPrefijoTests(unittest.TestCase):
    def test_on_message_no_procesa_comandos_con_prefijo(self):
        src = source("bot.py")
        fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_message")
        code = "\n".join(l for l in ast.get_source_segment(src, fn).splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("process_commands", code)

    def test_solo_algunos_siguen_con_prefijo(self):
        src = source("bot.py")
        self.assertIn('PREFIX_COMMANDS_ON = {"bal", "coinflip", "1v1", "agregarlista", "agregarlistamitad", "eliminarlista"}', src)
        fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_message")
        code = ast.get_source_segment(src, fn)
        self.assertIn("ctx.command.name in PREFIX_COMMANDS_ON", code)
        self.assertIn("bot.invoke(ctx)", code)

    def test_los_dos_comandos_que_solo_existian_con_prefijo_tienen_slash(self):
        bot_src = source("bot.py")
        self.assertIn('name="addroll-list", description=', bot_src)
        self.assertIn("async def addroll_list_slash", bot_src)
        import splits
        cls = next(getattr(splits, n) for n in dir(splits) if n.endswith("Cog") and hasattr(getattr(splits, n), "agregarlista"))
        self.assertTrue(hasattr(cls, "agregarlistamitad_slash"))


if __name__ == "__main__":
    unittest.main()


class ReclutadorTiradaTests(unittest.TestCase):
    def test_aceptar_postulacion_da_tirada_al_reclutador(self):
        src = source("bot.py")
        self.assertIn("async def recruit_accept_spin(interaction", src)
        self.assertIn('request_key=f"recruit-accept:{interaction.channel.id}"', src)
        self.assertIn("spin_text = await recruit_accept_spin(interaction, member)", src)
