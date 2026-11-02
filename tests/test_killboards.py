import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import killboards as kc  # noqa: E402
import killboard_db as kb  # noqa: E402
import registration_db as rdb  # noqa: E402
import ctautil as cu  # noqa: E402
import ctavoice as cv  # noqa: E402
from dbutil import connect  # noqa: E402
from test_killboard_db import EVENTS  # noqa: E402


class KBBase(DiscordBase):
    def setUp(self):
        super().setUp()
        g = self.guild
        self.r_staff = discord.Role(kc.STAFF_ROLE_ID, "Staff", g)
        self.r_caller = discord.Role(kc.CALLER_ROLE_ID, "Caller", g)
        g.roles += [self.r_staff, self.r_caller]
        self.r_member.id = kc.MIEMBRO_ROLE_ID
        import economy_db as _eco; _eco.init_schema(self.db)
        _eco.grant_command_role(self.db, 'stats', kc.MIEMBRO_ROLE_ID); _eco.grant_command_role(self.db, 'top', kc.MIEMBRO_ROLE_ID)
        def mk(uid, name, roles):
            m = discord.Member(uid, name, roles=list(roles), guild=g); g.members.append(m); return m
        self.stf = mk(8001, "Stf", [self.r_staff])
        self.caller = mk(8002, "Cal", [self.r_caller])
        self.mem = mk(8003, "Mem", [self.r_member])
        c = connect(self.db)
        c.executescript("""CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, snapshot_json TEXT, status TEXT, final_state TEXT, final_by INTEGER, final_at TEXT,
                           thread_id INTEGER, stats_channel_id INTEGER, stats_message_id INTEGER, stats_thread_id INTEGER);""")
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status, final_state, stats_thread_id) VALUES (5, '{\"slots\": []}', 'closed', 'finished', 555)")
        c.close()
        cu.init_positions(self.db); cv.init_schema(self.db); rdb.init_schema(self.db)
        self.cog = run(kc.setup_killboards(self.bot, self.db, GUILD_ID))
        self.calls = []

        async def fake(url, params=None):
            self.calls.append((url, params))
            if url.endswith("/battles/kills"):
                return EVENTS
            return {"players": [{"Name": "Gus", "GuildName": "Smogg"}]}
        self.cog.fetch_json = fake
        self.thread = discord.TextChannel(555, g, "stats-hilo")

    def load(self, who, text, channel=None):
        i = self.inter(who, channel=channel)
        run(self.cog.cargar.callback(self.cog, i, text))
        return i


class LoadTests(KBBase):
    def test_parse_ids(self):
        self.assertEqual(kc.parse_ids("https://albionbb.com/battles/multi?ids=1473088480,1473160689,1473166677"), ["1473088480", "1473160689", "1473166677"])
        self.assertEqual(kc.parse_ids("1473088480 1473088480"), ["1473088480"])

    def test_standalone_only_staff(self):
        self.assertIn("Solo el Staff", self.load(self.caller, "1473088480").text)
        self.assertIn("Solo el Staff", self.load(self.mem, "1473088480").text)
        self.assertIsNone(kb.get_battle(self.db, "1473088480") if hasattr(kb, "get_battle") else None)
        i = self.load(self.stf, "1473088480")
        self.assertIn("✅", i.text)
        self.assertIn("gus", kb.names_in_cta(self.db, 5) or {"gus": 1})
        self.assertEqual(kb.player_stats(self.db, ["gus"])["attendance"], 1)       # asistencia desde la ficha
        self.assertEqual(kb.player_stats(self.db, ["ana"])["deaths"], 2)

    def test_dedupe_and_delete(self):
        self.load(self.stf, "1473088480")
        i = self.load(self.stf, "1473088480 1473088481")
        self.assertIn("ya estaba cargado", i.text); self.assertIn("1473088481", i.text)
        d = self.inter(self.stf)
        run(self.cog.borrar.callback(self.cog, d, "1473088480"))
        view = d.response.sent[0][1]["view"]
        run(view.children[0].callback(self.inter(self.stf)))
        self.assertEqual(kb.player_stats(self.db, ["bob"])["attendance"], 1)       # queda el otro
        d = self.inter(self.mem)
        run(self.cog.borrar.callback(self.cog, d, "1473088481"))
        self.assertIn("Solo el Staff", d.text)

    def test_cta_thread_caller_allowed_and_comparison_staff_only(self):
        c = connect(self.db)
        for u in (101, 102):
            c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id) VALUES (5,?,'1:1',1260755129754189854)", (u,))
        c.close()
        rdb.admin_link(self.db, 101, "Ana", 1)
        rdb.admin_link(self.db, 103, "Bob", 1)
        cv.user_joined(self.db, 5, 101, kb_dt(0)); cv.user_left(self.db, 5, 101, kb_dt(30))
        cv.user_joined(self.db, 5, 104, kb_dt(0)); cv.user_left(self.db, 5, 104, kb_dt(30))          # topo
        i = self.load(self.caller, "1473088480", channel=self.thread)
        self.assertIn("✅", i.text); self.assertNotIn("Topos", i.text)
        self.assertIn("las ve el Staff", i.text)
        self.assertEqual(len(kb.battles_of_cta(self.db, 5)), 1)
        c = connect(self.db); c.execute("DELETE FROM kb_battles"); c.execute("DELETE FROM kb_players"); c.close()
        i = self.load(self.stf, "1473088480", channel=self.thread)
        self.assertIn("Topos", i.text); self.assertIn("<@104>", i.text)
        self.assertIn("<@103>", i.text)                                                          # Bob: en killboard, no estuvo en voz


class OpinologoTests(KBBase):
    def test_opinologo_can_load_in_cta_thread_but_not_loose_killboards(self):
        r_opi = discord.Role(kc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)
        self.guild.roles.append(r_opi)
        opi = discord.Member(8003, "Opi", roles=[r_opi], guild=self.guild)
        self.assertTrue(kc.is_loader_in_cta(opi))
        self.assertFalse(kc.is_staff_or_council(opi))
        self.assertIn("✅", self.load(opi, "1473088480", channel=self.thread).text)         # hilo de stats de una CTA: sí
        self.assertIn("Solo el Staff", self.load(opi, "1473088481").text)                   # killboard suelto: solo Staff/Council


def kb_dt(minutes):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class TopStatsTests(KBBase):
    def test_top_and_stats(self):
        self.load(self.stf, "1473088480")
        emb = run(self.cog.top_embed("deaths"))
        self.assertIn("Ana", emb.description)
        i = self.inter(self.mem)
        run(self.cog.send_ranking(i, "kills"))
        self.assertTrue(i.response.sent)
        i = self.inter(self.outsider)
        run(self.cog.send_ranking(i, "kills"))
        self.assertTrue("solo para miembros" in i.text or "permiso" in i.text)
        rdb.admin_link(self.db, self.mem.id, "Ana", 1)
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))
        emb = i.response.sent[0][1]["embed"]
        self.assertTrue(any("Muertes" in str(f) for f in emb.fields))


if __name__ == "__main__":
    unittest.main()
