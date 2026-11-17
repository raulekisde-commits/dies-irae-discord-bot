"""Tarjeta PNG de /winrate: emojis originales (Wuju / Embo) cargados una vez, dibujo y envío desde GamesCog."""
import os
import sys
import unittest
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import run  # noqa: E402
import casinocard as cc  # noqa: E402
import statscard as sc  # noqa: E402
from PIL import Image  # noqa: E402

DATA = {"name": "Ana", "banned": True, "dice": {"played": 4, "wins": 3}, "coin": {"played": 0, "wins": 0},
        "wagered": 30_000_000, "games": 12, "net": -500_000, "streak": 2, "best": 5,
        "level": "Apostador", "level_min": 25_000_000, "next": {"name": "Viciado", "min": 100_000_000, "missing": 70_000_000}}


class CasinoCardTests(unittest.TestCase):
    def test_emojis_originales_con_transparencia_y_cacheados(self):
        for name in ("wuju", "embo"):
            src = Image.open(os.path.join(cc.EMOJI_DIR, f"{name}.png"))
            self.assertEqual(src.mode, "RGBA")
            e = cc.emoji(name, 48)
            self.assertEqual(e.size, (48, 48))                        # proporción 1:1 del original (96×96)
            self.assertEqual(e.getpixel((0, 0))[3], 0)                # esquina transparente
            self.assertIs(cc.emoji(name, 48), e)                      # se escala una sola vez

    def test_dibuja_png_con_y_sin_proximo_nivel(self):
        self.assertTrue(cc.render(DATA).startswith(b"\x89PNG"))
        self.assertTrue(cc.render(dict(DATA, next=None, level="Rey de la ludopatía", banned=False)).startswith(b"\x89PNG"))
        self.assertTrue(cc.render(dict(DATA, level=None, level_min=0)).startswith(b"\x89PNG"))

    def test_usa_cache_compartida_sin_pisar_stats(self):
        sc._cache.clear(); sc._inflight.clear(); sc._sem = None
        png = run(sc.render_async(DATA, renderer=cc.render, ns=cc.NS))
        self.assertEqual(sc.cached(DATA, ns=cc.NS), png)
        self.assertIsNone(sc.cached(DATA))                            # misma data, otra tarjeta: clave distinta

    def test_gamescog_arma_la_tarjeta(self):
        import games as gm
        cog = gm.GamesCog.__new__(gm.GamesCog)
        png = run(cog._casino_card(NS(display_avatar=None), DATA))
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
