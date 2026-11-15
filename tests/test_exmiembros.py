import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import exmiembros as ex  # noqa: E402
import exmiembros_db as edb  # noqa: E402
from dbutil import connect  # noqa: E402


class ExMiembrosTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(ex.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(7300, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)
        self.cog = run(ex.setup_exmiembros(self.bot, self.db, GUILD_ID))
        self.cog._log = lambda text: _noop()

    def test_reconstruye_desde_killboards_y_registros(self):
        conn = connect(self.db)
        conn.executescript("""
            CREATE TABLE kb_battles(battle_id TEXT PRIMARY KEY, loaded_at TEXT);
            CREATE TABLE kb_players(battle_id TEXT, name_norm TEXT, name TEXT);
            CREATE TABLE albion_links(discord_id INTEGER, char_id TEXT, char_name TEXT, guild_name TEXT, method TEXT, updated_at TEXT);
            CREATE TABLE recruit_pending(discord_id INTEGER, char_name TEXT);
            INSERT INTO kb_battles VALUES ('b1', '2026-01-01T00:00:00+00:00');
            INSERT INTO kb_players VALUES ('b1','viejo','Viejo'), ('b1','actual','Actual');
            INSERT INTO albion_links VALUES (21,'id9','ExReg','Smogg','confirmed','2026-02-01T00:00:00+00:00'),
                                            (22,'id8','Invitado','OtraGuild','confirmed','2026-02-01T00:00:00+00:00'),
                                            (23,'id7','Pendiente','OtraGuild','recruitment','2026-02-01T00:00:00+00:00');
            INSERT INTO recruit_pending VALUES (23,'Pendiente');
        """)
        conn.close()
        run(self.cog.process_roster([("idA", "Actual")]))
        names = {r["name"] for r in edb.all_members(self.db)}
        self.assertEqual(names, {"Viejo", "ExReg"})
        self.assertEqual(edb.find_matches(self.db, 21, [])[0]["name"], "ExReg")

    def test_detecta_salidas_y_regresos(self):
        run(self.cog.process_roster([("1", "Ana"), ("2", "Beto"), ("3", "Caro")]))
        self.assertEqual(edb.all_members(self.db), [])                       # primera vez: solo guarda la lista
        self.assertEqual(edb.sync_roster(self.db, [("1", "Ana"), ("2", "Beto2"), ("3", "Caro")]), ([], []))   # renombre ≠ salida
        self.assertEqual(edb.sync_roster(self.db, [("1", "Ana"), ("2", "Beto2")]), (["Caro"], []))
        r = edb.find_matches(self.db, None, ["caro"])[0]
        self.assertTrue(r["left_at"])
        self.assertEqual(edb.sync_roster(self.db, [("1", "Ana"), ("2", "Beto2"), ("3", "Caro")]), ([], ["Caro"]))

    def test_corte_de_seguridad(self):
        big = [(str(i), f"P{i}") for i in range(100)]
        edb.sync_roster(self.db, big)
        self.assertIsNone(edb.sync_roster(self.db, big[:50]))
        self.assertEqual(edb.all_members(self.db), [])

    def test_ticket_respuestas_y_permisos(self):
        edb.sync_roster(self.db, [("1", "Ana"), ("2", "Beto")])
        edb.sync_roster(self.db, [("1", "Ana")])
        m = run(self.cog.ticket_matches(self.alice.id, "beto"))
        self.assertEqual([r["name"] for r in m], ["Beto"])
        edb.link_ticket(self.db, 555, self.alice.id, [r["id"] for r in m])
        self.assertEqual(edb.ticket_user(self.db, 555), self.alice.id)
        edb.save_answers(self.db, 555, "me fui a otra guild", "extraño a la gente")
        self.assertEqual(edb.find_matches(self.db, None, ["Beto"])[0]["reason_back"], "extraño a la gente")
        i = self.inter(self.alice)
        run(self.cog.ex_listar.callback(self.cog, i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.stf)
        run(self.cog.ex_listar.callback(self.cog, i))
        self.assertIn("Beto", i.text)


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
