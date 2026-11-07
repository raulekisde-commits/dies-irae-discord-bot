"""Pruebas del azar verificable (provably fair) del casino: algoritmo, sellado antes de jugar, revelación y verificación."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import duels_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import games_db as gdb  # noqa: E402
import provably_fair as pf  # noqa: E402
import roulette_db as rdb  # noqa: E402
from dbutil import connect, write_tx  # noqa: E402

SEED = "9f2c4e1ab07d5c3e88f61a2b4d9e0c7f13a5b6c8d2e4f60718293a4b5c6d7e8f"


class AlgorithmTests(unittest.TestCase):
    def test_vector_fijo_compatible_con_stake(self):
        # Valores calculados con la implementación de referencia de Stake en Node (crypto.createHmac):
        # HMAC_SHA256(serverSeed, `${clientSeed}:${nonce}:${round}`) y float = Σ byte_i / 256^(i+1) con 4 bytes.
        rng = pf.FairRng(SEED, "abc123-def456", 376)
        floats = [rng.random() for _ in range(20)]                       # 80 bytes: cruza 3 rondas de HMAC
        self.assertAlmostEqual(floats[0], 0.8829975817352533, places=15)
        self.assertAlmostEqual(floats[8], 0.48233098117634654, places=15)  # primer número de la ronda 1
        self.assertAlmostEqual(floats[19], 0.3803497003391385, places=15)
        self.assertEqual(pf.compute("roulette", SEED, "abc123-def456", 376), {"number": 32})
        self.assertEqual(pf.compute("coinflip", SEED, "abc123-def456", 376), {"side": "cruz"})
        self.assertEqual(pf.compute("dice", SEED, "abc123-def456", 376, [11, 22, 33]),
                         {"winner": 11, "rounds": [[[11, 6], [22, 2], [33, 5]]]})

    def test_determinista_y_sensible_a_cada_semilla(self):
        a = pf.compute("roulette", SEED, "x", 1)
        self.assertEqual(a, pf.compute("roulette", SEED, "x", 1))
        outs = {pf.compute("roulette", SEED, f"x{i}", 1)["number"] for i in range(200)}
        self.assertGreater(len(outs), 30)                                # cambiar la semilla del jugador cambia el resultado

    def test_distribucion_pareja(self):
        n = 40_000
        caras = sum(pf.coin(pf.FairRng(SEED, "c", i)) == "cara" for i in range(n))
        self.assertLess(abs(caras - n / 2), 4 * (n ** 0.5) / 2)          # dentro de 4 desviaciones estándar
        rng = pf.FairRng(SEED, "d", 1)
        caras_dado = [0] * 6
        for _ in range(60_000):
            caras_dado[rng.randint(1, 6) - 1] += 1
        for c in caras_dado:
            self.assertLess(abs(c - 10_000), 4 * (60_000 * (1 / 6) * (5 / 6)) ** 0.5)

    def test_sample_sin_repetidos(self):
        rng = pf.FairRng(SEED, "s", 1)
        pick = rng.sample(range(50), 10)
        self.assertEqual(len(set(pick)), 10)

    def test_cli_independiente(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(pf._main(["ruleta", SEED, "abc123-def456", "376"]), 0)
        self.assertIn(pf.seed_hash(SEED), buf.getvalue())
        self.assertIn('"number": 32', buf.getvalue())


class FlowBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        gdb.init_schema(self.db)
        ddb.init_schema(self.db)
        rdb.init_schema(self.db)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + ext)
            except OSError:
                pass

    def fund(self, uid, amount=1_000_000):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"f{uid}{os.urandom(3).hex()}")


class FlowTests(FlowBase):
    def test_coinflip_sellado_antes_y_verificable_despues(self):
        self.fund(1), self.fund(2)
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=1, creator_id=1, stake=1000, side="cruz").game
        self.assertRegex(g.pf_hash or "", r"^[0-9a-f]{64}$")             # el panel ya tiene el hash
        rec = pf.get_record(self.db, "coinflip", g.id)
        self.assertIsNone(rec["server_seed"])                             # la semilla sigue secreta
        self.assertEqual(pf.verify(self.db, "coinflip", g.id).status, "pending")
        gdb.set_message(self.db, g.id, 999)
        res = gdb.join_game(self.db, g.id, 2).resolved
        self.assertEqual(res.proof["server_hash"], g.pf_hash)
        self.assertEqual(pf.seed_hash(res.proof["server_seed"]), g.pf_hash)
        expected = pf.compute("coinflip", res.proof["server_seed"], res.proof["client_seed"], res.proof["nonce"])["side"]
        self.assertEqual(res.coin_side, expected)
        self.assertEqual(res.winner_id, 1 if expected == "cruz" else 2)  # el creador eligió cruz
        v = pf.verify(self.db, "coinflip", g.id)
        self.assertEqual((v.status, v.hash_ok), ("ok", True))

    def test_manipular_la_base_se_detecta(self):
        self.fund(1), self.fund(2)
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=1, creator_id=1, stake=1000).game
        gdb.set_message(self.db, g.id, 999)
        res = gdb.join_game(self.db, g.id, 2).resolved
        other = "cara" if res.coin_side == "cruz" else "cruz"
        conn = connect(self.db)
        with write_tx(conn):
            conn.execute("UPDATE games SET outcome = ? WHERE id = ?", (json.dumps({"side": other}), g.id))
        conn.close()
        self.assertEqual(pf.verify(self.db, "coinflip", g.id).status, "mismatch")

    def test_semilla_del_jugador(self):
        self.assertRegex(pf.get_client_seed(self.db, 5), r"^[0-9a-f]{16}$")
        self.assertEqual(pf.set_client_seed(self.db, 5, "mi_semilla_7"), "mi_semilla_7")
        self.assertEqual(pf.get_client_seed(self.db, 5), "mi_semilla_7")
        for bad in ("", "con espacio", "x" * 33, "a:b"):
            with self.assertRaises(ValueError):
                pf.set_client_seed(self.db, 5, bad)
        self.fund(5), self.fund(6)
        pf.set_client_seed(self.db, 6, "otra")
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=1, creator_id=5, stake=10).game
        gdb.set_message(self.db, g.id, 1)
        res = gdb.join_game(self.db, g.id, 6).resolved
        self.assertEqual(res.proof["client_seed"], "mi_semilla_7-otra")  # creador primero, después quien acepta
        self.assertEqual(res.proof["nonce"], g.id)

    def test_dados_con_varios_jugadores(self):
        for u in (1, 2, 3, 4):
            self.fund(u)
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=1, creator_id=1, stake=100).game
        gdb.set_message(self.db, g.id, 1)
        for u in (2, 3, 4):
            gdb.join_game(self.db, g.id, u)
        self.assertIsNone(pf.get_record(self.db, "dice", g.id)["server_seed"])
        res = gdb.resolve_game(self.db, g.id, now=g.ends_at + 1)
        self.assertEqual(res.status, "done")
        self.assertEqual(pf.verify(self.db, "dice", g.id).status, "ok")

    def test_coinflip_sin_rival_revela_igual(self):
        self.fund(1)
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=1, creator_id=1, stake=10).game
        res = gdb.resolve_game(self.db, g.id, now=g.ends_at + 1)
        self.assertEqual(res.status, "refunded")
        self.assertEqual(pf.verify(self.db, "coinflip", g.id).status, "ok")

    def test_duelo(self):
        self.fund(1), self.fund(2)
        d = ddb.create_duel(self.db, guild_id=1, channel_id=1, challenger_id=1, challenged_id=2, stake=500).duel
        self.assertRegex(d.pf_hash or "", r"^[0-9a-f]{64}$")
        r = ddb.accept_duel(self.db, d.id, 2)
        self.assertEqual(r.proof["server_hash"], d.pf_hash)
        self.assertEqual(r.winner_id, 1 if r.side == "cara" else 2)
        self.assertEqual(pf.verify(self.db, "duel", d.id).status, "ok")

    def test_ruleta(self):
        self.fund(1), self.fund(2)
        panel, _ = rdb.create_panel(self.db, 1, 1, 1)
        rnd = rdb.current_round(self.db, panel.id)
        h = pf.get_hash(self.db, "roulette", rnd.id)
        self.assertRegex(h or "", r"^[0-9a-f]{64}$")
        self.assertEqual(rdb.place_bet(self.db, panel.id, 2, "red", 0, 20_000, "k2").status, "ok")
        self.assertEqual(rdb.place_bet(self.db, panel.id, 1, "number", 7, 10_000, "k1").status, "ok")
        st = rdb.resolve_round(self.db, rnd.id, force=True)
        self.assertEqual(st.proof["server_hash"], h)
        self.assertEqual(st.proof["client_seed"].count("-"), 1)          # semillas de quienes apostaron (en orden de apuesta)
        self.assertEqual(pf.verify(self.db, "roulette", rnd.id).status, "ok")
        nxt = rdb.current_round(self.db, panel.id)
        self.assertNotEqual(pf.get_hash(self.db, "roulette", nxt.id), h)  # cada ronda tiene su propia semilla

    def test_partida_anterior_al_sistema_igual_se_resuelve(self):
        self.fund(1), self.fund(2)
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=1, creator_id=1, stake=10).game
        conn = connect(self.db)
        with write_tx(conn):
            conn.execute("DELETE FROM pf_seeds")                          # como una partida creada antes del despliegue
        conn.close()
        gdb.set_message(self.db, g.id, 1)
        res = gdb.join_game(self.db, g.id, 2).resolved
        self.assertEqual(res.status, "done")
        self.assertEqual(pf.verify(self.db, "coinflip", g.id).status, "ok")


if __name__ == "__main__":
    unittest.main()


from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import casino as cs  # noqa: E402
from discord import app_commands  # noqa: E402


class CommandTests(DiscordBase):
    def setUp(self):
        super().setUp()
        gdb.init_schema(self.db)
        ddb.init_schema(self.db)
        self.cog = run(cs.setup_casino(self.bot, self.db, GUILD_ID))

    def test_verificar_y_semilla(self):
        for u in (self.alice, self.council):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[u.id], amount=10_000, request_key=f"f{u.id}")
        d = ddb.create_duel(self.db, guild_id=GUILD_ID, channel_id=1, challenger_id=self.alice.id, challenged_id=self.council.id, stake=100).duel
        choice = app_commands.Choice(name="1v1", value="duel")
        i = self.inter(self.alice)
        run(self.cog.casino_verificar.callback(self.cog, i, choice, d.id))
        self.assertIn("todavía no termina", i.text)
        self.assertIn(d.pf_hash, i.text)
        ddb.accept_duel(self.db, d.id, self.council.id)
        i = self.inter(self.alice)
        run(self.cog.casino_verificar.callback(self.cog, i, choice, d.id))
        self.assertIn("✅ resultado verificado", i.text)
        i = self.inter(self.alice)
        run(self.cog.casino_semilla.callback(self.cog, i, "nueva_1"))
        self.assertIn("nueva_1", i.text)
        i = self.inter(self.alice)
        run(self.cog.casino_semilla.callback(self.cog, i, "no vale"))
        self.assertIn("❌", i.text)
        i = self.inter(self.alice)
        run(self.cog.casino_verificar.callback(self.cog, i, choice, 99999))
        self.assertIn("no tiene registro", i.text)
