import ast
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import guildfilter as gf  # noqa: E402

MIO, OTRO = 111, 222


def ns(**kw):
    return types.SimpleNamespace(**kw)


class GuildFilterTests(unittest.TestCase):
    def test_mensaje_de_otro_servidor_se_descarta(self):
        self.assertFalse(gf.event_allowed((ns(guild=ns(id=OTRO)),), {MIO}))

    def test_mensaje_propio_pasa(self):
        self.assertTrue(gf.event_allowed((ns(guild=ns(id=MIO)),), {MIO}))

    def test_payload_crudo_usa_guild_id(self):
        self.assertFalse(gf.event_allowed((ns(guild_id=OTRO),), {MIO}))
        self.assertTrue(gf.event_allowed((ns(guild_id=MIO),), {MIO}))

    def test_mensaje_directo_y_eventos_sin_servidor_pasan(self):
        self.assertTrue(gf.event_allowed((ns(guild=None),), {MIO}))
        self.assertTrue(gf.event_allowed((), {MIO}))
        self.assertTrue(gf.event_allowed((ns(id=OTRO),), {MIO}))      # p. ej. un Guild en guild_join: no se descarta

    def test_servidor_extra_permitido(self):
        self.assertTrue(gf.event_allowed((ns(guild=ns(id=OTRO)),), {MIO, OTRO}))

    def test_voz_y_miembro(self):
        self.assertFalse(gf.event_allowed((ns(guild=ns(id=OTRO), bot=False), None, None), {MIO}))

    def test_mybot_filtra_en_dispatch(self):
        src = open(os.path.join(os.path.dirname(HERE), "bot.py"), encoding="utf-8").read()
        cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "MyBot")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "dispatch")
        self.assertIn("guildfilter.event_allowed", ast.get_source_segment(src, fn))


if __name__ == "__main__":
    unittest.main()
