import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run  # noqa: E402
import types  # noqa: E402
import panels as pn  # noqa: E402


class PanelDelegationTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(pn.setup_panels(self.bot, 1))
        self.calls = []

    def call(self, name, user, *a):
        i = self.inter(user)
        run(getattr(self.cog, name).callback(self.cog, i, *a))
        return i

    def test_reclutamiento_y_foco_usan_las_funciones_de_bot(self):
        async def rec(i): self.calls.append(("rec", i.user.id))
        async def foco(i): self.calls.append(("foco", i.user.id))
        self.bot.panel_handlers = {"reclutamiento": rec, "foco": foco}
        self.call("reclutamiento", self.alice)
        self.call("foco", self.alice)
        self.assertEqual(self.calls, [("rec", self.alice.id), ("foco", self.alice.id)])

    def test_sin_modulo_cargado_avisa(self):
        self.assertIn("no está cargado", self.call("reclutamiento", self.alice).text)
        self.assertIn("no está cargado", self.call("shop", self.alice, None).text)
        self.assertIn("no está cargado", self.call("coaching", self.alice).text)

    def test_shop_y_coaching_delegan_en_su_cog(self):
        async def shop(i, canal): self.calls.append(("shop", canal))
        async def panel(i): self.calls.append(("coaching",))
        self.bot.cogs["GuildShopCog"] = types.SimpleNamespace(guild_shop=shop)
        self.bot.cogs["CoachingCog"] = types.SimpleNamespace(coaching_panel=panel)
        self.call("shop", self.alice, None)
        self.call("coaching", self.alice)
        self.assertEqual(self.calls, [("shop", None), ("coaching",)])


if __name__ == "__main__":
    unittest.main()
