import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import run  # noqa: E402
from test_killboards import KBBase  # noqa: E402
import killboard_db as kb  # noqa: E402
import registration_db as rdb  # noqa: E402
import ctautil as cu  # noqa: E402
from dbutil import connect  # noqa: E402

SAMPLE = ('"Character Name"\t"Last Seen"\t"Roles"\r\n'
          '"Daitza"\t"10/31/2026 05:05:56"\t"Officer;Council"\r\n'
          '"Wujumecanicas"\t"Online"\t"Council"\r\n'
          '"Ana"\t"Online"\t""\r\n'
          '"Gus"\t"Online"\t""\r\n'
          '"Zeta"\t"Online"\t"Siphoned"\r\n'
          '"Beto"\t"Online"\t""\r\n').encode("utf-8")


class Conectados(KBBase):
    def test_parser(self):
        names, total = kb.parse_online_file(SAMPLE)
        self.assertEqual(total, 6)
        self.assertEqual(names, ["Wujumecanicas", "Ana", "Gus", "Zeta", "Beto"])
        self.assertEqual(kb.parse_online_file(SAMPLE.decode().encode("utf-16"))[0], names)
        self.assertEqual(kb.parse_online_file(b"basura sin tabs"), ([], 0))

    def test_skips(self):
        # killboard del hilo (Ana y Gus aparecen); Beto está anotado en la composición; Zeta y Wujumecanicas skipearon
        self.load(self.stf, "1473088480", channel=self.thread)
        rdb.admin_link(self.db, self.mem.id, "Beto", 1)
        c = connect(self.db)
        c.execute("INSERT INTO cta_positions(cta_id, slot_key, user_id) VALUES (5, 'a', ?)", (self.mem.id,)) if False else None
        c.close()
        orig = cu.positions
        cu.positions = lambda db, cid: [{"user_id": self.mem.id}] if cid == 5 else []
        try:
            text = run(self.cog.process_online(5, SAMPLE, self.stf.id))
        finally:
            cu.positions = orig
        self.assertIn("5** online", text)
        self.assertIn("Skippers", text); self.assertIn("Zeta", text); self.assertIn("Wujumecanicas", text)
        self.assertNotIn("Beto", text); self.assertNotIn("• Ana", text); self.assertNotIn("• Gus", text)
        self.assertEqual(kb.skips_of_cta(self.db, 5), ["Wujumecanicas", "Zeta"])
        self.assertEqual(kb.skip_count(self.db, ["zeta"]), 1)
        self.assertEqual(kb.top(self.db, "skips", 10)[0][1], 1.0)
        emb = run(self.cog.top_embed("skips"))
        self.assertIn("Zeta", emb.description)

    def test_no_killboard_no_skips(self):
        text = run(self.cog.process_online(5, SAMPLE, self.stf.id))
        self.assertIn("Falta cargar el killboard", text)
        self.assertEqual(kb.skips_of_cta(self.db, 5), [])

    def test_reupload_replaces_and_stats_counter(self):
        self.load(self.stf, "1473088480", channel=self.thread)
        run(self.cog.process_online(5, SAMPLE, self.stf.id))
        run(self.cog.process_online(5, b'"Character Name"\t"Last Seen"\t"Roles"\n"Zeta"\t"Online"\t""\n', self.stf.id))
        self.assertEqual(kb.skips_of_cta(self.db, 5), ["Zeta"])
        rdb.admin_link(self.db, self.mem.id, "Zeta", 1)
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))
        emb = i.response.sent[0][1]["embed"]
        self.assertTrue(any("skipeadas" in str(f) for f in emb.fields))

    def test_guarda_la_ultima_conexion_de_toda_la_guild(self):
        import guildcheck_db as gdb
        run(self.cog.process_online(5, SAMPLE, self.stf.id))
        snap, at = gdb.snapshot(self.db)
        self.assertEqual(len(snap), 6)
        self.assertEqual(snap["daitza"][1].isoformat(), "2026-10-31T05:05:56+00:00")
        self.assertEqual(snap["zeta"][1], at)                                    # Online = hora de subida
        run(self.cog.process_online(5, b'"Character Name"\t"Last Seen"\t"Roles"\n"A"\t"10/31/2026 01:00:00"\t""\n', self.stf.id))
        self.assertEqual(list(gdb.snapshot(self.db)[0]), ["a"])                 # sin nadie Online también actualiza

    def test_stats_muestra_asistencias_a_cta(self):
        from datetime import datetime, timezone
        rdb.admin_link(self.db, self.mem.id, "Zeta", 1)
        c = connect(self.db)
        c.execute("ALTER TABLE cta_runs ADD COLUMN guild_id INTEGER")
        c.execute("ALTER TABLE cta_runs ADD COLUMN closes_at TEXT")
        c.execute("UPDATE cta_runs SET guild_id=?, closes_at=? WHERE id=5", (self.guild.id, datetime.now(timezone.utc).isoformat()))
        c.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at) VALUES ('b1', 5, 1, 'x')")
        c.execute("INSERT INTO kb_players(battle_id, name_norm, name) VALUES ('b1', 'zeta', 'Zeta')")
        c.close()
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))
        emb = i.response.sent[0][1]["embed"]
        f = [x for x in emb.fields if "Asistencias a CTA" in str(x)]
        self.assertTrue(f)
        self.assertIn("Total: **1**", str(f[0]))
        self.assertIn("Esta semana: **1/5**", str(f[0]))

    def test_bad_file(self):
        self.assertIn("No pude leer", run(self.cog.process_online(5, b"hola", self.stf.id)))
        self.assertIn("ninguno figura", run(self.cog.process_online(5, b'"Character Name"\t"Last Seen"\t"Roles"\n"A"\t"10/31/2026 01:00:00"\t""\n', self.stf.id)))


if __name__ == "__main__":
    unittest.main()
