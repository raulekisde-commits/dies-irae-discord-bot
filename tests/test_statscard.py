"""Tarjeta PNG de /stats: dibujo, caché con TTL, un solo dibujo por tarjeta, límite de dibujos simultáneos y cooldown de /stats."""
import asyncio
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import run  # noqa: E402
from test_killboards import KBBase  # noqa: E402
import killboards as kc  # noqa: E402
import registration_db as rdb  # noqa: E402
import statscard as sc  # noqa: E402

DATA = {"title": "Ana", "display": "Ana", "has_kb": True, "kills": 12, "deaths": 4, "attendance": 9, "kda": 3.5,
        "first5": 2, "first5_pct": 22.2, "avg_ip": 1350.0, "max_ip": 1400.0, "min_ip": 1300.0, "skips": 1, "looted": 1000,
        "roles": [("Healer", 60.0, 6), ("Tank", 40.0, 4)], "main": {"name": "Healer", "n": 6, "total": 10},
        "cta_total": 10, "cta_week": 2, "cta_week_n": 3}


class StatsCardTests(unittest.TestCase):
    def setUp(self):
        sc._cache.clear(); sc._inflight.clear(); sc._sem = None
        self.real_render, self.calls, self.active, self.peak = sc.render, [], 0, 0
        self.lock = threading.Lock()

        def slow(data, avatar=None):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
                self.calls.append(data["title"])
            return b"png-" + data["title"].encode()
        sc.render = slow
        self.addCleanup(lambda: setattr(sc, "render", self.real_render))

    def test_dibujo_real_es_png(self):
        for rank in ("council", "municipal", "core", "member", None):
            self.assertTrue(self.real_render(dict(DATA, rank=rank)).startswith(b"\x89PNG"))
        png = self.real_render(DATA)
        self.assertTrue(png.startswith(b"\x89PNG"))
        png = self.real_render(dict(DATA, has_kb=False, roles=[], main=None, cta_total=None))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_usa_la_fuente_del_repo(self):
        self.assertTrue(sc.pf(20, "Bold").path.endswith(os.path.join("assets", "fonts", "Poppins-Bold.ttf")))

    def test_cache_y_un_solo_dibujo_por_tarjeta(self):
        async def go():
            return await asyncio.gather(*[sc.render_async(DATA) for _ in range(5)])
        out = run(go())
        self.assertEqual(set(out), {b"png-Ana"})
        self.assertEqual(self.calls, ["Ana"])                     # 5 pedidos a la vez → 1 dibujo
        run(sc.render_async(DATA))
        self.assertEqual(len(self.calls), 1)                      # caché vigente
        self.assertEqual(sc.cached(DATA), b"png-Ana")
        run(sc.render_async(dict(DATA, kills=13)))                # datos distintos → otra tarjeta
        self.assertEqual(len(self.calls), 2)

    def test_ttl_vencido_vuelve_a_dibujar(self):
        old = sc.CACHE_TTL
        sc.CACHE_TTL = 0
        try:
            run(sc.render_async(DATA))
            run(sc.render_async(DATA))
        finally:
            sc.CACHE_TTL = old
        self.assertEqual(len(self.calls), 2)

    def test_limite_de_dibujos_simultaneos(self):
        async def go():
            return await asyncio.gather(*[sc.render_async(dict(DATA, title=f"P{i}")) for i in range(8)])
        run(go())
        self.assertEqual(len(self.calls), 8)
        self.assertLessEqual(self.peak, sc.MAX_PARALELO)

    def test_no_bloquea_el_event_loop(self):
        async def go():
            ticks = 0

            async def ticker():
                nonlocal ticks
                for _ in range(5):
                    await asyncio.sleep(0.005)
                    ticks += 1
            await asyncio.gather(sc.render_async(dict(DATA, title="X")), ticker())
            return ticks
        self.assertEqual(run(go()), 5)                            # el loop siguió atendiendo mientras se dibujaba


class StatsCommandTests(KBBase):
    def setUp(self):
        super().setUp()
        kc.statscard = sc                                         # KBBase la apaga: acá se prueba la tarjeta
        sc._cache.clear(); sc._inflight.clear(); sc._sem = None
        rdb.admin_link(self.db, self.mem.id, "Ana", 1)

    def test_stats_manda_la_tarjeta_y_respeta_cooldown(self):
        kc.STATS_COOLDOWN = 60
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))
        kw = i.response.sent[-1][1]
        self.assertEqual(kw["file"].filename, "stats.png")
        self.assertTrue(kw["file"].fp.getvalue().startswith(b"\x89PNG"))
        self.assertNotIn("embed", kw)                             # adjunto suelto: se ve más grande
        self.assertEqual(i.response.sent[-1][0], self.mem.mention)
        i2 = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i2, None))
        self.assertIn("Esperá", i2.text)

    def test_rango_por_prioridad(self):
        import discord
        R = lambda rid: discord.Role(rid, "r", self.guild)
        mk = lambda *ids: discord.Member(1, "x", roles=[R(i) for i in ids], guild=self.guild)
        self.assertEqual(kc.guild_rank(mk(kc.MIEMBRO_ROLE_ID, kc.CORE_MEMBER_ROLE_ID, kc.STAFF_ROLE_ID, kc.COUNCIL_ROLE_ID)), "council")
        self.assertEqual(kc.guild_rank(mk(kc.MIEMBRO_ROLE_ID, kc.CORE_MEMBER_ROLE_ID, kc.STAFF_ROLE_ID)), "municipal")
        self.assertEqual(kc.guild_rank(mk(kc.MIEMBRO_ROLE_ID, kc.CORE_MEMBER_ROLE_ID)), "core")
        self.assertEqual(kc.guild_rank(mk(kc.MIEMBRO_ROLE_ID)), "member")
        self.assertIsNone(kc.guild_rank(mk()))

    def test_core_member_se_marca_en_la_tarjeta(self):
        import discord
        self.mem.roles.append(discord.Role(kc.CORE_MEMBER_ROLE_ID, "Core Member", self.guild))
        seen, old = {}, sc.render_async

        async def spy(data, *a, **k):
            seen.update(data)
            return await old(data, *a, **k)
        sc.render_async = spy
        try:
            run(self.cog.stats.callback(self.cog, self.inter(self.mem), None))
        finally:
            sc.render_async = old
        self.assertEqual(seen["rank"], "core")

    def test_si_falla_el_dibujo_manda_el_embed(self):
        old = sc.render_async

        async def boom(*a, **k):
            raise RuntimeError("x")
        sc.render_async = boom
        try:
            i = self.inter(self.mem)
            run(self.cog.stats.callback(self.cog, i, None))
        finally:
            sc.render_async = old
        kw = i.response.sent[-1][1]
        self.assertNotIn("file", kw)
        self.assertTrue(any("skipeadas" in str(f) or "Roles" in str(f) or "Rol main" in str(f) for f in kw["embed"].fields))


if __name__ == "__main__":
    unittest.main()
