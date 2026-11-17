"""Tarjeta PNG de !bal / /bal: emojis originales (Wuju / Embo), rango como en /stats, caché propia y armado desde EconomyCog."""
import io
import os
import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import run  # noqa: E402
import casinocard as cc  # noqa: E402
import statscard as sc  # noqa: E402
import walletcard as wc  # noqa: E402
from PIL import Image  # noqa: E402

DATA = {"name": "Ana", "currency": "Wuju Coins", "rank": "council", "balance": 12_345_678, "lifetime": 98_765_432,
        "embo": -1500, "wagered": 30_000_000, "won": 0, "lost": -500_000}


class WalletCardTests(unittest.TestCase):
    def test_dibuja_png_con_y_sin_rango_y_con_montos_grandes(self):
        for d in (DATA, dict(DATA, rank=None, balance=0, embo=0), dict(DATA, rank="member", wagered=9_999_999_999, won=1_250_000_000, lost=0)):
            png = wc.render(d)
            self.assertTrue(png.startswith(b"\x89PNG"))
            self.assertEqual(Image.open(io.BytesIO(png)).width, sc.W)

    def test_cada_moneda_usa_su_emoji(self):
        used = []
        real = wc._put_emoji
        with mock.patch.object(wc, "_put_emoji", side_effect=lambda img, name, *a: used.append(name) or real(img, name, *a)):
            wc.render(DATA)
        self.assertEqual(used.count("embo"), 1)                       # solo Energías Embotelladas
        self.assertEqual(used.count("wuju"), 5)                       # actual, histórico, apostado, ganado, perdido

    def test_emojis_cacheados_por_tamano(self):
        e = cc.emoji("embo", 40)
        self.assertIs(cc.emoji("embo", 40), e)
        self.assertEqual(e.getpixel((0, 0))[3], 0)                    # transparencia intacta

    def test_cache_propia_sin_pisar_otras_tarjetas(self):
        sc._cache.clear(); sc._inflight.clear(); sc._sem = None
        png = run(sc.render_async(DATA, renderer=wc.render, ns=wc.NS))
        self.assertEqual(sc.cached(DATA, ns=wc.NS), png)
        self.assertIsNone(sc.cached(DATA, ns=cc.NS))

    def test_economycog_arma_la_tarjeta_con_los_mismos_numeros(self):
        import economy as ec
        cog = ec.EconomyCog.__new__(ec.EconomyCog)
        seen = {}

        async def fake_render(data, *a, **k):
            seen.update(data)
            return b"\x89PNGfake"
        with mock.patch.object(sc, "render_async", fake_render):
            png = run(cog._bal_card(NS(id=5, display_name="Ana", display_avatar=None, roles=[]),
                                    (100, 200, -3, NS(wagered=50, net=-20))))
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual((seen["balance"], seen["lifetime"], seen["embo"], seen["wagered"], seen["won"], seen["lost"]),
                         (100, 200, -3, 50, 0, -20))
        self.assertIsNone(seen["rank"])


if __name__ == "__main__":
    unittest.main()
