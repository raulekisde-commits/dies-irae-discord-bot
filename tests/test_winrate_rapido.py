"""El winrate no debe tardar: la consulta de stats usa la clave primaria y el /casino winrate avisa a Discord antes de calcular."""
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import economy_db as eco  # noqa: E402
import games as gm  # noqa: E402
import games_db as gdb  # noqa: E402
import duels_db as ddb  # noqa: E402
import stats_db  # noqa: E402


class FixedRng:
    def __init__(self, v):
        self.v = v

    def randint(self, a, b):
        return self.v


class WinrateRapidoTests(DiscordBase):
    def setUp(self):
        super().setUp()
        gdb.init_schema(self.db)
        ddb.init_schema(self.db)
        stats_db.init_schema(self.db)
        self.gcog = run(gm.setup_games(self.bot, self.db, GUILD_ID))
        for u in (self.alice, self.bob):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[u.id], amount=10_000_000, request_key=f"f{u.id}")

    def test_plan_usa_clave_primaria(self):
        sql = stats_db._SQL.format(stakes=",".join("?" * len(stats_db.STAKE_TYPES)),
                                   results=",".join("?" * len(stats_db.RESULT_TYPES)), user_filter="")
        params = list(stats_db.STAKE_TYPES) + list(stats_db.RESULT_TYPES)
        conn = sqlite3.connect(self.db)
        plan = " | ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        conn.close()
        for alias in ("b", "g", "d"):                     # nada de recorrer bets/games/duels completas por cada movimiento
            self.assertIn(f"SEARCH {alias} USING INTEGER PRIMARY KEY", plan)

    def test_resultados_y_slash_con_defer(self):
        a, b = self.alice.id, self.bob.id
        for k in range(30):
            g = gdb.create_game(self.db, kind="coinflip", guild_id=GUILD_ID, channel_id=1, creator_id=a, stake=100).game
            gdb.set_message(self.db, g.id, 1000 + g.id)
            gdb.join_game(self.db, g.id, b, rng=FixedRng(0 if k < 20 else 1))      # alice gana 20, pierde 10
        st = stats_db.get_user_stats(self.db, a)
        self.assertEqual((st.wagered, st.won, st.lost, st.games), (3000, 2000, 1000, 30))
        i = self.inter(self.alice)
        run(self.gcog.winrate_slash(i, None))
        self.assertTrue(i.response.done)
        embeds = [kw["embed"] for _, kw in i.response.sent if kw.get("embed")]
        self.assertEqual(len(embeds), 1)                   # llegó por followup después del defer
        self.assertIn("20 ganadas de 30", " ".join(str(v) for _, v, _ in embeds[0].fields))


if __name__ == "__main__":
    unittest.main()
