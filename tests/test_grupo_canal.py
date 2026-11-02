import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import types  # noqa: E402
import guildshop as gs  # noqa: E402
import registration as reg  # noqa: E402


class CanalGroupTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(gs.setup_guildshop(self.bot, self.db, GUILD_ID)) if hasattr(gs, "setup_guildshop") else None
        self.calls = []

    def test_subcomandos_de_canal(self):
        self.assertTrue({"dados", "entradas", "salidas", "registro", "economia"} <= set(gs.GuildShopCog.canal.commands))

    def test_delegan_en_el_modulo_de_cada_canal(self):
        def rec(name):
            async def f(interaction, *args): self.calls.append((name,) + args)
            return f
        self.bot.cogs["MemberLogCog"] = types.SimpleNamespace(canal_entradas=rec("entradas"), canal_salidas=rec("salidas"))
        self.bot.cogs["RegistrationCog"] = types.SimpleNamespace(registro_canal=rec("registro"))
        self.bot.cogs["EconomyCog"] = types.SimpleNamespace(registro_bal=rec("economia"))
        ch = object()
        for cmd, args in ((self.cog.canal_entradas_cmd, (ch, False)), (self.cog.canal_salidas_cmd, (None, True)),
                          (self.cog.canal_registro_cmd, (ch,)), (self.cog.canal_economia_cmd, (ch,))):
            run(cmd.callback(self.cog, self.inter(self.alice), *args))
        self.assertEqual([c[0] for c in self.calls], ["entradas", "salidas", "registro", "economia"])
        self.assertEqual(self.calls[1][1:], (None, True))

    def test_modulo_no_cargado_avisa(self):
        self.bot.cogs.pop("EconomyCog", None)
        i = self.inter(self.alice)
        run(self.cog.canal_economia_cmd.callback(self.cog, i, object()))
        self.assertIn("no está cargado", i.text)

    def test_canal_registro_sigue_exento_del_registro_obligatorio(self):
        self.assertIn("canal registro", reg.EXEMPT_COMMANDS)


if __name__ == "__main__":
    unittest.main()
