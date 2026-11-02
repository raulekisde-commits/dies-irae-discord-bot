import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import warnings_sys as w  # noqa: E402


class Warnings(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(w.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(9101, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)
        self.cog = run(w.setup_warnings(self.bot, self.db, GUILD_ID))

    def test_only_staff(self):
        for cmd, args in ((self.cog.warning, (self.bob, "mal")), (self.cog.warnings, (self.bob,)), (self.cog.warning_top, ())):
            i = self.inter(self.alice)
            run(cmd.callback(self.cog, i, *args))
            self.assertIn("Solo el Staff", i.text)

    def test_add_list_top_privacy(self):
        i = self.inter(self.stf)
        run(self.cog.warning.callback(self.cog, i, self.bob, "insultó"))
        self.assertIn("#1", i.text); self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        run(self.cog.warning.callback(self.cog, self.inter(self.stf), self.bob, "otra vez"))
        run(self.cog.warning.callback(self.cog, self.inter(self.stf), self.alice, "una"))
        i = self.inter(self.stf)
        run(self.cog.warnings.callback(self.cog, i, self.bob))
        kw = i.response.sent[0][1]
        self.assertTrue(kw.get("ephemeral"))                        # la lista de warnings es privada
        self.assertIn("insultó", str(kw["embed"].description)); self.assertIn("2", str(kw["embed"].description))
        i = self.inter(self.stf)
        run(self.cog.warning_top.callback(self.cog, i))
        kw = i.response.sent[0][1]
        self.assertTrue(kw.get("ephemeral"))
        self.assertTrue(kw["embed"].description.splitlines()[0].startswith("🥇") and str(self.bob.id) in kw["embed"].description.splitlines()[0])
        self.assertEqual(w.list_warnings(self.db, GUILD_ID, self.bob.id)[0]["issued_by"], self.stf.id)

    def test_no_bots_and_empty(self):
        bot_m = discord.Member(9200, "Bot", roles=[], guild=self.guild); bot_m.bot = True
        i = self.inter(self.stf)
        run(self.cog.warning.callback(self.cog, i, bot_m, "x"))
        self.assertIn("bot", i.text)
        i = self.inter(self.stf)
        run(self.cog.warnings.callback(self.cog, i, self.alice))
        self.assertIn("no tiene warnings", i.response.sent[0][1]["embed"].description)


if __name__ == "__main__":
    unittest.main()
