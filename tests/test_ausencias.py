import os, sys, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import warnings_sys as w  # noqa: E402
import ausencias as a  # noqa: E402


class Ausencias(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(w.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(9101, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)
        self.cog = run(a.setup_ausencias(self.bot, self.db, GUILD_ID))

    def test_miembro_avisa_y_staff_ve(self):
        i = self.inter(self.bob)
        run(self.cog.avisar.callback(self.cog, i, "viaje", 5))
        self.assertIn("5", i.text); self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        i = self.inter(self.stf)
        run(self.cog.lista.callback(self.cog, i))
        kw = i.response.sent[0][1]
        self.assertTrue(kw.get("ephemeral")); self.assertIn("viaje", kw["embed"].description)
        i = self.inter(self.stf)
        run(self.cog.ver.callback(self.cog, i, self.bob))
        self.assertIn("activa", i.response.sent[0][1]["embed"].description)

    def test_solo_staff_lista_y_ver(self):
        for cmd, args in ((self.cog.lista, ()), (self.cog.ver, (self.bob,))):
            i = self.inter(self.alice)
            run(cmd.callback(self.cog, i, *args))
            self.assertIn("Solo el Staff", i.text)

    def test_reemplazo_y_vencidas(self):
        past = datetime.now(timezone.utc) - timedelta(days=10)
        a.add_ausencia(self.db, GUILD_ID, self.alice.id, "vieja", 2, now=past)
        self.assertEqual(a.list_activas(self.db, GUILD_ID), [])
        a.add_ausencia(self.db, GUILD_ID, self.bob.id, "uno", 3)
        r = a.add_ausencia(self.db, GUILD_ID, self.bob.id, "dos", 4)
        self.assertTrue(r["reemplazo"])
        act = a.list_activas(self.db, GUILD_ID)
        self.assertEqual([x["texto"] for x in act], ["dos"])
        self.assertEqual(len(a.list_usuario(self.db, GUILD_ID, self.bob.id)), 2)


if __name__ == "__main__":
    unittest.main()
