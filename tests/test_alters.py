import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import alters as al  # noqa: E402
import alters_db as adb  # noqa: E402
import invitados as iv  # noqa: E402


class AltersTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(al.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(7100, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)
        self.cog = run(al.setup_alters(self.bot, self.db, GUILD_ID))

    def add(self, who, target, name, note=None):
        i = self.inter(who)
        run(self.cog.alter_agregar.callback(self.cog, i, target, name, note))
        return i.text

    def test_solo_staff_council_admin(self):
        self.assertIn("Solo el Staff", self.add(self.alice, self.bob, "Alt1"))
        self.assertEqual(adb.alters_of(self.db, self.bob.id), [])
        for who in (self.stf, self.council, self.admin):
            self.assertIn("quedó enlazado", self.add(who, self.bob, f"Alt{who.id}"))

    def test_guarda_quien_autorizo_y_ver(self):
        self.add(self.stf, self.bob, "AltBob", "segunda cuenta")
        row = adb.find_alter(self.db, "altbob")
        self.assertEqual((row["user_id"], row["added_by"]), (self.bob.id, self.stf.id))
        i = self.inter(self.stf)
        run(self.cog.alter_ver.callback(self.cog, i, self.bob, None))
        self.assertIn(f"<@{self.stf.id}>", i.text)
        i = self.inter(self.alice)
        run(self.cog.alter_ver.callback(self.cog, i, self.bob, None))
        self.assertIn("Solo el Staff", i.text)

    def test_duplicados_y_limite_y_quitar(self):
        self.add(self.stf, self.bob, "AltBob")
        self.assertIn("ya está enlazado", self.add(self.stf, self.alice, "altbob"))
        self.assertIn("inválido", self.add(self.stf, self.alice, "x"))
        for n in range(adb.MAX_ALTERS_PER_USER - 1):
            self.add(self.stf, self.bob, f"Otro{n}")
        self.assertIn("máximo", self.add(self.stf, self.bob, "Extra"))
        i = self.inter(self.alice)
        run(self.cog.alter_quitar.callback(self.cog, i, "AltBob"))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.council)
        run(self.cog.alter_quitar.callback(self.cog, i, "altbob"))
        self.assertIn("Se quitó", i.text)
        self.assertIsNone(adb.find_alter(self.db, "AltBob"))

    def test_no_puede_ser_principal_de_otro(self):
        import registration_db as rdb
        rdb.init_schema(self.db)
        rdb.admin_link(self.db, self.alice.id, "MainAlice", self.stf.id)
        self.assertIn("principal", self.add(self.stf, self.bob, "MainAlice"))
        self.assertIn("quedó enlazado", self.add(self.stf, self.alice, "MainAlice"))

    def test_info(self):
        self.assertIn("alters", [c.value for c in iv.InvitadosCog.INFO_CHOICES])
        self.assertIn("autorizó", iv.InvitadosCog.info_embed("alters", False).description)
        self.assertIn("/alter-agregar", iv.InvitadosCog.info_embed("alters", True).description)


if __name__ == "__main__":
    unittest.main()
