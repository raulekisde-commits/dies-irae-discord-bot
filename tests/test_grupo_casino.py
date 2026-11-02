import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import types  # noqa: E402
import discord  # noqa: E402
import casino as cs  # noqa: E402


class CasinoGroupTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(cs.setup_casino(self.bot, self.db, GUILD_ID))
        self.calls = []

    def test_un_solo_comando_raiz(self):
        g = cs.CasinoCog.casino_g
        self.assertEqual(g.name, "casino")
        self.assertEqual(sorted(g.commands), sorted(["abrir", "cerrar", "limite", "ban", "dados", "coinflip", "1v1", "winrate", "total",
                                                     "ruleta", "ruleta-editar", "ruleta-stats", "apuestas"]))

    def test_apuestas_viven_en_casino_apuestas(self):
        g = cs.CasinoCog.apuestas_g
        self.assertEqual(g.name, "apuestas")
        self.assertIs(g.parent, cs.CasinoCog.casino_g)
        self.assertEqual(sorted(g.commands), ["apostar", "cancelar", "cerrar", "crear", "resolver", "todo"])

    def test_casino_apuestas_delega_en_bets(self):
        calls = []

        def rec(name):
            async def f(interaction, *args):
                calls.append((name,) + args)
            return f
        self.bot.cogs["BetsCog"] = types.SimpleNamespace(apuesta=rec("crear"), apostar_slash=rec("apostar"), apostartodo_slash=rec("todo"),
                                                         cerrarapuesta=rec("cerrar"), resolverapuesta=rec("resolver"), cancelarapuesta=rec("cancelar"))
        i = self.inter(self.alice)
        run(self.cog.apuestas_crear.callback(self.cog, i, "¿Quién gana?", "A, B"))
        run(self.cog.apuestas_apostar.callback(self.cog, i, "A", "1000"))
        run(self.cog.apuestas_todo.callback(self.cog, i, "B"))
        run(self.cog.apuestas_cerrar.callback(self.cog, i, 7))
        run(self.cog.apuestas_resolver.callback(self.cog, i, "A", None))
        run(self.cog.apuestas_cancelar.callback(self.cog, i, None))
        self.assertEqual(calls, [("crear", "¿Quién gana?", "A, B"), ("apostar", "A", "1000"), ("todo", "B"), ("cerrar", 7),
                                 ("resolver", "A", None), ("cancelar", None)])

    def test_apuestas_sin_el_modulo_avisa(self):
        self.bot.cogs.pop("BetsCog", None)
        i = self.inter(self.alice)
        run(self.cog.apuestas_cerrar.callback(self.cog, i, None))
        self.assertIn("no está cargado", i.text)

    def test_abrir_y_cerrar_solo_council(self):
        i = self.inter(self.alice)
        run(self.cog.casino_abrir.callback(self.cog, i))
        self.assertIn("Solo el rol **Council**", i.text)
        i = self.inter(self.council)
        run(self.cog.casino_cerrar.callback(self.cog, i))
        self.assertIn("cerró", i.text)
        i = self.inter(self.council)
        run(self.cog.casino_abrir.callback(self.cog, i))
        self.assertIn("abrió", i.text)

    def test_delegan_en_el_modulo_de_cada_juego(self):
        def rec(name):
            async def f(interaction, *args): self.calls.append((name,) + args)
            return f
        self.bot.cogs["GamesCog"] = types.SimpleNamespace(dados_slash=rec("dados"), coinflip_slash=rec("coinflip"),
                                                          winrate_slash=rec("winrate"), totalapostado_slash=rec("total"))
        self.bot.cogs["DuelsCog"] = types.SimpleNamespace(duel=rec("1v1"))
        self.bot.cogs["RouletteCog"] = types.SimpleNamespace(ruleta=rec("ruleta"), ruleta_stats=rec("stats"), ruleta_editar=rec("editar"))
        def go(cmd, *a): run(cmd.callback(self.cog, self.inter(self.alice), *a))
        go(self.cog.casino_dados, "100")
        go(self.cog.casino_coinflip, "100", None)
        go(self.cog.casino_1v1, self.bob, "50")
        go(self.cog.casino_winrate, None)
        go(self.cog.casino_total)
        go(self.cog.casino_ruleta)
        go(self.cog.casino_ruleta_stats)
        go(self.cog.casino_ruleta_editar, "1m")
        self.assertEqual([c[0] for c in self.calls], ["dados", "coinflip", "1v1", "winrate", "total", "ruleta", "stats", "editar"])
        self.assertEqual(self.calls[2][1:], (self.bob, "50"))
        self.assertEqual(self.calls[7][1], "1m")

    def test_modulo_no_cargado_avisa(self):
        self.bot.cogs.pop("GamesCog", None)
        i = self.inter(self.alice)
        run(self.cog.casino_dados.callback(self.cog, i, "100"))
        self.assertIn("no está cargado", i.text)


if __name__ == "__main__":
    unittest.main()
