import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase  # noqa: E402,F401  (instala el stub de discord)
import nodesync as ns  # noqa: E402

NOW = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
PANEL = """8.4 Nodes
 8.4 Ore - Redtree Enclave - en 28 minutos 14:57 UTC
 8.4 Hide - Sandmount Ascent - en 4 horas 18:03 UTC
 8.4 Wood - Unhallowed Cloister - en 11 horas 01:21 UTC
 8.4 Fiber - Citadel of Ash - en 11 horas 01:58 UTC
7.4 Nodes
 7.4 Ore - Everwinter Expanse - en 3 horas 17:03 UTC
Vortex - Cores
 Gold Vortex - Drytop Pillars - en 11 minutos 14:39 UTC
 Blue Vortex - Sandmount Strand - en 36 minutos 15:04 UTC
 Purple Vortex - Stonemouth Southbluff - en una hora 15:35 UTC
14 active objective(s)•hoy a las 11:28"""


class ParseTests(unittest.TestCase):
    def test_panel_completo(self):
        t, skipped = ns.parse_timers(PANEL, NOW)
        self.assertEqual(len(t), 8)
        self.assertEqual(skipped, 0)
        a = t[0]
        self.assertEqual((a["material"], a["tier"], a["mapa"], a["clock"]), ("mineral", "8.4", "Redtree Enclave", (14, 57)))
        self.assertEqual(a["spawn_at"], datetime(2026, 11, 2, 14, 57, tzinfo=timezone.utc))

    def test_materiales_y_colores(self):
        t, _ = ns.parse_timers(PANEL, NOW)
        mats = {(x["material"], x["tier"]) for x in t}
        self.assertIn(("cuero", "8.4"), mats)
        self.assertIn(("madera", "8.4"), mats)
        self.assertIn(("fibra", "8.4"), mats)
        self.assertIn(("vortex", "legendary"), mats)
        self.assertIn(("vortex", "rare"), mats)
        self.assertIn(("vortex", "epic"), mats)
        self.assertIn(("mineral", "7.4"), mats)

    def test_cruza_medianoche(self):
        t, _ = ns.parse_timers("8.4 Wood - X - en 11 horas 01:21 UTC", NOW)
        self.assertEqual(t[0]["spawn_at"], datetime(2026, 11, 3, 1, 21, tzinfo=timezone.utc))

    def test_ya_salio_no_se_carga_para_manana(self):
        t, _ = ns.parse_timers("8.4 Ore - X - hace 5 minutos 14:25 UTC", NOW)
        self.assertEqual(t, [])

    def test_con_timestamp_de_discord_y_formato(self):
        txt = "<:ore:123456789> **8.4 Ore** - __Redtree Enclave__ - <t:1793717820:R> 14:57 UTC"
        t, _ = ns.parse_timers(txt, NOW)
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["mapa"], "Redtree Enclave")
        self.assertEqual(int(t[0]["spawn_at"].timestamp()), 1793717820)

    def test_tier_o_material_raro_se_cuenta_como_descartado(self):
        t, skipped = ns.parse_timers("3.4 Ore - X - en 1 hora 15:00 UTC\n8.4 Stone - X - en 1 hora 15:00 UTC\nBlack Vortex - X - en 1 hora 15:00 UTC", NOW)
        self.assertEqual(t, [])
        self.assertEqual(skipped, 3)

    def test_texto_de_mensaje_junta_embeds(self):
        f = types.SimpleNamespace(name="8.4 Nodes", value=" 8.4 Ore - Redtree Enclave - en 28 minutos 14:57 UTC")
        e = types.SimpleNamespace(title="Timers", description="", fields=[f])
        m = types.SimpleNamespace(content="", embeds=[e])
        t, _ = ns.parse_timers(ns.message_text(m), NOW)
        self.assertEqual(len(t), 1)


class ColoresYPasadosTests(unittest.TestCase):
    def test_colores_a_tier_como_en_la_captura(self):
        base = int(NOW.timestamp()) + 600
        txt = "\n".join([f"🟨 Gold Vortex - A - <t:{base}:R> 14:40 UTC", f"🟦 Blue Vortex - B - <t:{base}:R> 14:40 UTC",
                         f"🟪 Purple Vortex - C - <t:{base}:R> 14:40 UTC", f"🟩 Green Vortex - D - <t:{base}:R> 14:40 UTC",
                         f"🟨 Gold Core - E - <t:{base}:R> 14:40 UTC"])
        t, skipped = ns.parse_timers(txt, NOW)
        self.assertEqual([(x["material"], x["tier"], x["mapa"]) for x in t],
                         [("vortex", "legendary", "A"), ("vortex", "rare", "B"), ("vortex", "epic", "C"), ("vortex", "common", "D"),
                          ("core", "legendary", "E")])
        self.assertEqual(skipped, 0)

    def test_tambien_acepta_el_nombre_del_tier(self):
        t, _ = ns.parse_timers("Epic Vortex - X - en 1 hora 15:00 UTC\nLegendary Core - Y - en 1 hora 15:10 UTC\nRare Vortex - Z - en 1 hora 15:20 UTC", NOW)
        self.assertEqual([x["tier"] for x in t], ["epic", "legendary", "rare"])

    def test_los_que_ya_salieron_no_se_cargan(self):
        pasado = int(NOW.timestamp()) - 2 * 3600
        reciente = int(NOW.timestamp()) - 5 * 60
        txt = f"⛏️ 8.4 Ore - Vieja - <t:{pasado}:R> 12:30 UTC\n⛏️ 8.4 Ore - Reciente - <t:{reciente}:R> 14:25 UTC\n8.4 Ore - Texto - hace 2 horas 12:30 UTC"
        t, _ = ns.parse_timers(txt, NOW)
        self.assertEqual([x["mapa"] for x in t], ["Reciente"])


class SeenTests(unittest.TestCase):
    def test_no_repite_ni_revive_borrados(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        ns.init_schema(db)
        t, _ = ns.parse_timers(PANEL, NOW)
        self.assertEqual(len(ns.filter_new(db, t, NOW)), 8)
        self.assertEqual(ns.filter_new(db, t, NOW), [])                       # la segunda lectura no trae nada nuevo
        otro, _ = ns.parse_timers(PANEL + "\n 8.4 Ore - Nueva Zona - en 2 horas 16:30 UTC", NOW)
        self.assertEqual([x["mapa"] for x in ns.filter_new(db, otro, NOW)], ["Nueva Zona"])


class SyncTests(unittest.TestCase):
    def test_carga_a_nombre_del_dueno_y_refresca(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        ns.init_schema(db)
        added, refreshed = [], []

        class Hist:
            def __init__(self, msgs):
                self.msgs = msgs

            def __aiter__(self):
                self.it = iter(self.msgs)
                return self

            async def __anext__(self):
                try:
                    return next(self.it)
                except StopIteration:
                    raise StopAsyncIteration

        panel = types.SimpleNamespace(author=types.SimpleNamespace(id=77), content=PANEL, embeds=[])
        ruido = types.SimpleNamespace(author=types.SimpleNamespace(id=1), content="8.4 Ore - Falso - en 1 hora 20:00 UTC", embeds=[])
        chan = types.SimpleNamespace(history=lambda limit: Hist([ruido, panel]))
        bot = types.SimpleNamespace(get_channel=lambda cid: chan, get_guild=lambda gid: object())

        async def refresh(g, cats):
            refreshed.append(cats)

        cog = ns.NodeSyncCog.__new__(ns.NodeSyncCog)
        cog.bot, cog.db_path, cog.channel_id, cog.source_bot_id, cog.owner_id, cog.guild_id = bot, db, 5, 77, 999, 10
        cog.find_entries = lambda *a: []
        cog.add_entry = lambda guild, mat, tier, mapa, spawn, who: added.append((mat, tier, mapa, who))
        cog.category_of = lambda m: "vortex" if m == "vortex" else "nodos"
        cog.refresh = refresh
        cog._warned_empty = False
        cog.clock = lambda: NOW
        self.assertEqual(asyncio.run(cog.sync_once()), 8)
        self.assertTrue(all(a[3] == 999 for a in added))
        self.assertNotIn("Falso", [a[2] for a in added])                      # solo se lee lo que publicó el bot indicado
        self.assertEqual(refreshed, [{"nodos", "vortex"}])
        self.assertEqual(asyncio.run(cog.sync_once()), 0)


if __name__ == "__main__":
    unittest.main()
