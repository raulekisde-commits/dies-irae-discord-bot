import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import blacklist as bl  # noqa: E402
import blacklist_db as bdb  # noqa: E402


class BlacklistTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(bl.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(7200, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)
        self.cog = run(bl.setup_blacklist(self.bot, self.db, GUILD_ID))

    def add(self, who, motivo, personaje=None, usuario=None):
        i = self.inter(who)
        run(self.cog.bl_agregar.callback(self.cog, i, motivo, personaje, usuario))
        return i.text

    def rm(self, who, personaje=None, usuario=None):
        i = self.inter(who)
        run(self.cog.bl_sacar.callback(self.cog, i, personaje, usuario))
        return i.text

    def test_permisos_agregar_y_sacar(self):
        self.assertIn("Solo el Staff", self.add(self.alice, "x", "Malo"))
        self.assertIn("agregado", self.add(self.stf, "robó loot", "Malo"))
        self.assertIn("Solo el Council", self.rm(self.stf, "malo"))
        self.assertIn("Se sacó", self.rm(self.council, "malo"))
        self.assertEqual(bdb.all_entries(self.db), [])

    def test_discord_y_duplicado(self):
        self.add(self.council, "toxico", "Pj1", self.bob)
        self.assertIn("ya está", self.add(self.stf, "otra vez", "pj1"))
        self.assertEqual(len(bdb.all_entries(self.db)), 2)

    def test_matches_ticket(self):
        self.add(self.stf, "robó loot", "Malo")
        self.add(self.stf, "toxico", usuario=self.bob)
        m = bdb.find_matches(self.db, self.bob.id, ["Bueno / malo"])
        self.assertEqual({r["kind"] for r in m}, {"albion", "discord"})
        self.assertEqual(bdb.find_matches(self.db, self.alice.id, ["Bueno"]), [])

    def test_miembro_puede_listar(self):
        self.add(self.stf, "robó loot", "Malo")
        i = self.inter(self.alice)
        run(self.cog.bl_listar.callback(self.cog, i))
        self.assertIn("Solo los Miembros", i.text)
        r_m = discord.Role(bl.MIEMBRO_ROLE_ID, "Miembro", self.guild)
        self.guild.roles.append(r_m)
        m = discord.Member(7201, "Mie", roles=[r_m], guild=self.guild)
        self.guild.members.append(m)
        i = self.inter(m)
        run(self.cog.bl_listar.callback(self.cog, i))
        self.assertIn("Malo", i.text)
        self.assertIn("Solo el Staff", self.add(m, "x", "Otro"))


if __name__ == "__main__":
    unittest.main()
