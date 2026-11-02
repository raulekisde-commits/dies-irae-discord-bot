import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase  # noqa: E402,F401  (instala el stub de discord)
import buildcard as bc  # noqa: E402
import diagnostico as dg  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._old = (os.environ.get("BUILDCARD_CACHE"), bc._dir_ok, bc._render, bc._sem)
        os.environ["BUILDCARD_CACHE"] = self.dir
        bc._dir_ok, bc._sem = None, None
        bc._inflight.clear()
        self.calls, self.activos, self.max_activos = [], 0, 0
        self.lock = threading.Lock()

        def falso(data, title=""):
            with self.lock:
                self.calls.append(data["id"])
                self.activos += 1
                self.max_activos = max(self.max_activos, self.activos)
            time.sleep(0.05)
            with self.lock:
                self.activos -= 1
            return b"PNG" + data["id"].encode()
        bc._render = falso

    def tearDown(self):
        old, bc._dir_ok, bc._render, bc._sem = self._old[0], self._old[1], self._old[2], self._old[3]
        if old is None:
            os.environ.pop("BUILDCARD_CACHE", None)
        else:
            os.environ["BUILDCARD_CACHE"] = old


class ConcurrenciaTests(Base):
    def test_cuarenta_pedidos_de_la_misma_build_dibujan_una_vez(self):
        avisos = []

        async def avisar():
            avisos.append(1)

        async def main():
            return await asyncio.gather(*[bc.render_async({"id": "b1"}, "T", on_wait=avisar) for _ in range(40)])
        res = asyncio.run(main())
        self.assertEqual(self.calls, ["b1"])
        self.assertTrue(all(r == b"PNGb1" for r in res))
        self.assertEqual(len(avisos), 40)             # todos los que tuvieron que esperar recibieron el aviso

    def test_guardada_sale_directo_sin_aviso(self):
        async def main():
            await bc.render_async({"id": "b1"}, "T")
            avisos = []

            async def avisar():
                avisos.append(1)
            await bc.render_async({"id": "b1"}, "T", on_wait=avisar)
            return avisos
        self.assertEqual(asyncio.run(main()), [])
        self.assertEqual(self.calls, ["b1"])

    def test_builds_distintas_en_tandas(self):
        async def main():
            return await asyncio.gather(*[bc.render_async({"id": f"b{i}"}, "T") for i in range(10)])
        asyncio.run(main())
        self.assertEqual(len(self.calls), 10)
        self.assertLessEqual(self.max_activos, bc.MAX_PARALELO)

    def test_error_no_deja_la_build_trabada(self):
        n = {"i": 0}

        def a_veces(data, title=""):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("falló")
            return b"OK"
        bc._render = a_veces

        async def main():
            with self.assertRaises(RuntimeError):
                await bc.render_async({"id": "x"}, "T")
            return await bc.render_async({"id": "x"}, "T")
        self.assertEqual(asyncio.run(main()), b"OK")


class CacheEstadoTests(Base):
    def test_resumen_ok(self):
        open(os.path.join(self.dir, "card_a.png"), "wb").write(b"x")
        open(os.path.join(self.dir, "T8_HEAD.png"), "wb").write(b"x")
        ok, txt = bc.cache_summary()
        self.assertTrue(ok)
        self.assertIn("1 imágenes y 1 íconos", txt)

    def test_sin_permiso_avisa(self):
        orig = bc.cache_status
        bc.cache_status = lambda: {"dir": "/x", "writable": False, "images": 0, "icons": 0, "bytes": 0}
        try:
            ok, txt = bc.cache_summary()
        finally:
            bc.cache_status = orig
        self.assertFalse(ok)
        self.assertIn("no se pueden guardar", txt)

    def test_diagnostico_incluye_la_cache_solo_si_se_pasa(self):
        m = type("M", (), {"roles": [], "id": 1})()
        con = dg.diagnose(m, None, member_role_id=None, registered=None, command_count=None, build_cache=(False, "sin caché"))
        self.assertIn((False, "sin caché"), con)
        sin = dg.diagnose(m, None, member_role_id=None, registered=None, command_count=None)
        self.assertNotIn((False, "sin caché"), sin)


if __name__ == "__main__":
    unittest.main()
