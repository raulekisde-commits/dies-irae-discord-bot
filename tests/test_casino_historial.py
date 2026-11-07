"""/casino ver-historial: historial de una persona y cara a cara entre dos."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import casino as cs  # noqa: E402
import duels_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import games as gm  # noqa: E402
import games_db as gdb  # noqa: E402
import roulette_db as rdb  # noqa: E402
import stats_db  # noqa: E402


class FixedRng:
    def __init__(self, v):
        self.v = v

    def randint(self, a, b):
        return self.v

    def choice(self, seq):
        return seq[0]


class HistorialTests(DiscordBase):
    def setUp(self):
        super().setUp()
        gdb.init_schema(self.db)
        ddb.init_schema(self.db)
        rdb.init_schema(self.db)
        stats_db.init_schema(self.db)
        self.gcog = run(gm.setup_games(self.bot, self.db, GUILD_ID))
        self.cog = run(cs.setup_casino(self.bot, self.db, GUILD_ID))
        self.carol = 99
        for u in (self.alice.id, self.bob.id, self.carol):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[u], amount=1_000_000, request_key=f"f{u}")

    def coinflip(self, creator, other, stake, creator_wins):
        g = gdb.create_game(self.db, kind="coinflip", guild_id=GUILD_ID, channel_id=1, creator_id=creator, stake=stake).game
        gdb.set_message(self.db, g.id, 1000 + g.id)
        return gdb.join_game(self.db, g.id, other, rng=FixedRng(0 if creator_wins else 1)).resolved   # creador = cara

    def duel(self, challenger, challenged, stake, challenger_wins):
        d = ddb.create_duel(self.db, guild_id=GUILD_ID, channel_id=1, challenger_id=challenger, challenged_id=challenged, stake=stake).duel
        return ddb.accept_duel(self.db, d.id, challenged, rng=FixedRng(0 if challenger_wins else 1))       # retador = cara

    def test_historial_y_cara_a_cara(self):
        a, b, c = self.alice.id, self.bob.id, self.carol
        self.coinflip(a, b, 1000, creator_wins=True)       # alice +1000
        self.duel(b, a, 5000, challenger_wins=True)        # alice -5000
        self.coinflip(a, c, 300, creator_wins=False)       # alice -300 (vs carol: no cuenta en el cara a cara con bob)
        g = gdb.create_game(self.db, kind="dice", guild_id=GUILD_ID, channel_id=1, creator_id=a, stake=200).game
        gdb.set_message(self.db, g.id, 1)
        gdb.join_game(self.db, g.id, b)
        gdb.join_game(self.db, g.id, c)
        gdb.resolve_game(self.db, g.id, now=g.ends_at + 1, rng=FixedRng(3))   # empate total → choice: gana alice (primera)
        nothing = gdb.create_game(self.db, kind="coinflip", guild_id=GUILD_ID, channel_id=1, creator_id=a, stake=50).game
        gdb.resolve_game(self.db, nothing.id, now=nothing.ends_at + 1)        # devuelto: no aparece

        hist = stats_db.history(self.db, a)
        self.assertEqual([e.game for e in hist], ["dice", "coinflip", "duel", "coinflip"])   # más nueva primero
        self.assertEqual([e.net for e in hist], [400, -300, -5000, 1000])
        self.assertEqual(sorted(hist[0].rivals), sorted([b, c]))

        h = stats_db.head_to_head(self.db, a, b)
        self.assertEqual((h.a_wins, h.b_wins, h.a_net), (1, 1, -4000))     # solo coinflip y duelo entre ellos dos
        self.assertEqual(len(h.entries), 2)
        h2 = stats_db.head_to_head(self.db, b, a)
        self.assertEqual((h2.a_wins, h2.b_wins, h2.a_net), (1, 1, 4000))

        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, None, None, None))
        text = self.embed_text(i)
        self.assertIn("**4** partidas", text)
        self.assertIn("✅ 2 ganadas", text)
        self.assertIn("ganó el **50,0%**", text)
        self.assertIn("vs 2 jugadores", text)
        self.assertIn("Coinflip #", text)

        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, self.alice, self.bob, None))
        text = self.embed_text(i)
        self.assertIn(f"<@{b}> le va ganando", text)
        self.assertIn("2 partidas entre ellos", text)

        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, self.bob, self.bob, None))
        self.assertIn("dos personas distintas", i.text)

    def test_paginas_de_a_diez(self):
        a, b = self.alice.id, self.bob.id
        for k in range(23):
            self.coinflip(a, b, 10, creator_wins=k % 2 == 0)
        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, None, None, None))
        view = self.sent_kw(i).get("view")
        self.assertIsNotNone(view)
        self.assertEqual(len(view.pages), 3)
        self.assertIn("Página 1/3", view.pages[0].footer)
        self.assertEqual(view.pages[0].description.count("Coinflip #"), 10)

    def test_historial_del_dia_desde_las_00_utc(self):
        from discord import app_commands
        from dbutil import connect, write_tx
        a, b = self.alice.id, self.bob.id
        ayer = self.coinflip(a, b, 7777, creator_wins=True).game.id
        for k in range(12):                                 # 12 hoy: 8 ganadas, 4 perdidas
            self.coinflip(a, b, 100, creator_wins=k < 8)
        conn = connect(self.db)
        with write_tx(conn):
            conn.execute("UPDATE games SET resolved_at = '2000-01-01T23:59:59+00:00' WHERE id = ?", (ayer,))
        conn.close()
        hoy = app_commands.Choice(name="Hoy (desde 00:00 UTC)", value="hoy")

        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, None, None, hoy))
        kw = self.sent_kw(i)
        self.assertIsNone(kw.get("view"))                   # una sola página
        text = self.embed_text(i)
        self.assertIn("**12** partidas hoy", text)          # el resumen cuenta todas las de hoy
        self.assertIn("ganó el **66,7%**", text)
        self.assertIn("Neto del día: **+", text)
        self.assertEqual(text.count("Coinflip #"), 10)      # pero lista solo las últimas 10
        self.assertNotIn(f"#{ayer} ", text)
        self.assertIn("00:00 UTC", kw["embed"].footer)

        i = self.inter(self.alice)
        run(self.cog.casino_ver_historial.callback(self.cog, i, self.alice, self.bob, hoy))
        text = self.embed_text(i)
        self.assertIn("12 partidas entre ellos hoy", text)
        self.assertIn(f"<@{a}> ganó **8** (66,7%)", text)

    def test_sin_partidas(self):
        i = self.inter(self.bob)
        run(self.cog.casino_ver_historial.callback(self.cog, i, None, None, None))
        self.assertIn("Todavía no hay partidas", self.embed_text(i))

    # --- helpers sobre el stub de interacción ---
    def sent_kw(self, i):
        self.assertTrue(i.response.sent, "no se respondió")
        return i.response.sent[-1][1]

    def embed_text(self, i):
        emb = self.sent_kw(i).get("embed")
        self.assertIsNotNone(emb, "no se envió ningún embed")
        return (emb.title or "") + "\n" + (emb.description or "")


if __name__ == "__main__":
    unittest.main()
