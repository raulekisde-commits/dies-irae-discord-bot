"""Grupo C de Wujupón: cartas de coinflip con tope y duelo 1v1 especial (24 h, 5 % de penalidad, Top 10, hasta 100M)."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import casino_db  # noqa: E402
import duels  # noqa: E402
import duels_db  # noqa: E402
import economy_db as eco  # noqa: E402
import efectos_db as edb  # noqa: E402
import games  # noqa: E402
import games_db  # noqa: E402
import stats_db  # noqa: E402
import wujupon as wj  # noqa: E402
import wujupon_db as wdb  # noqa: E402
import wujupon_efectos as we  # noqa: E402

DUEL_CH = 3000000000000000030
M = 1_000_000


class GrupoCTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.duelch = discord.TextChannel(DUEL_CH, self.guild, "anuncios")
        self.guild._channels[DUEL_CH] = self.duelch
        self.bot.channels[DUEL_CH] = self.duelch
        self.wcog = run(wj.setup_wujupon(self.bot, self.db, GUILD_ID))
        self.gcog = run(games.setup_games(self.bot, self.db, GUILD_ID))
        self.dcog = run(duels.setup_duels(self.bot, self.db, GUILD_ID))
        self.cog = run(we.setup_efectos(self.bot, self.db, GUILD_ID))
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        wdb.set_setting(self.db, "duel_channel", str(DUEL_CH))
        self.top10 = [self.bob.id]
        self._orig_top = stats_db.top_by
        stats_db.top_by = lambda db, field: [stats_db.GamblerStats(u, 1) for u in self.top10]

    def tearDown(self):
        stats_db.top_by = self._orig_top
        super().tearDown()

    def card(self, key, user=None):
        return wdb.give_card(self.db, actor_id=1, user_id=(user or self.alice).id, prize_key=key, reason="t")

    # ---------------- coinflip ----------------
    def test_coinflip_supera_el_tope_normal(self):
        self.fund(self.alice, 20 * M)
        cid = self.card("coinflip_15m")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        modal = i.modals[0]
        self.assertIsInstance(modal, we.AmountModal)
        self.assertEqual(modal.limit, 15 * M)
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, 15 * M, "12m"))
        self.assertIn("publicado", i.text)
        g = games_db.live_game(self.db, GUILD_ID, "coinflip")
        self.assertEqual(g.stake, 12 * M)                      # más que el máximo normal de 5M
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        self.assertEqual(self.bal(self.alice), 8 * M)

    def test_coinflip_pasado_del_tope_no_gasta(self):
        self.fund(self.alice, 50 * M)
        cid = self.card("undo_coinflip")
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, 10 * M, "11m"))
        self.assertIn("máximo", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_coinflip_respeta_casino_cerrado(self):
        casino_db.set_open(self.db, GUILD_ID, False, 1)
        self.fund(self.alice, 5 * M)
        cid = self.card("undo_coinflip")
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, 10 * M, "1m"))
        self.assertIn("no se gastó", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_coinflip_sin_rival_devuelve_la_carta_y_jugado_la_consume(self):
        self.fund(self.alice, 10 * M)
        self.fund(self.bob, 10 * M)
        c1 = self.card("coinflip_nolimit")
        run(self.cog.play_card(self.inter(self.alice), c1, 0, "8m"))
        g = games_db.live_game(self.db, GUILD_ID, "coinflip")
        games_db.resolve_game(self.db, g.id, now=time.time() + 10_000)       # vence sin rival
        edb.settle_games(self.db)
        self.assertEqual(wdb.get_card(self.db, c1).status, "available")
        self.assertEqual(self.bal(self.alice), 10 * M)
        run(self.cog.play_card(self.inter(self.alice), c1, 0, "8m"))
        g = games_db.live_game(self.db, GUILD_ID, "coinflip")
        games_db.join_game(self.db, g.id, self.bob.id)
        games_db.resolve_game(self.db, g.id, now=time.time() + 10_000)
        edb.settle_games(self.db)
        self.assertEqual(wdb.get_card(self.db, c1).status, "consumed")

    # ---------------- duelo especial ----------------
    def test_duelo_requiere_top10_y_respeta_tope(self):
        self.fund(self.alice, 200 * M)
        self.fund(self.council, 200 * M)
        cid = self.card("duel_1v1")
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, wdb.DUEL_MAX, "10m", self.council))
        self.assertIn("Top 10", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        self.fund(self.bob, 200 * M)
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, wdb.DUEL_MAX, "150m", self.bob))
        self.assertIn("máximo", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_duelo_se_publica_con_here_y_24h(self):
        self.fund(self.alice, 50 * M)
        self.fund(self.bob, 50 * M)
        cid = self.card("duel_1v1")
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, wdb.DUEL_MAX, "20m", self.bob))
        msg = self.duelch.sent[-1]
        self.assertIn("<@&1565464173096214669>", msg.content)
        self.assertIn("1,000,000", msg.content)                               # 5 % de 20M
        d = duels_db.find_by_message(self.db, msg.id)
        self.assertGreater(d.ends_at - time.time(), 23 * 3600)
        self.assertEqual((d.stake, d.penalty), (20 * M, M))
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")

    def test_rechazar_o_no_responder_paga_la_penalidad(self):
        self.fund(self.alice, 50 * M)
        self.fund(self.bob, 50 * M)
        cid = self.card("duel_1v1")
        run(self.cog.play_card(self.inter(self.alice), cid, wdb.DUEL_MAX, "20m", self.bob))
        d = duels_db.find_by_message(self.db, self.duelch.sent[-1].id)
        res = duels_db.decline_duel(self.db, d.id, self.bob.id)
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.bal(self.alice), 50 * M + M)                     # recupera la apuesta y cobra el 5 %
        self.assertEqual(self.bal(self.bob), 50 * M - M)
        # vencido sin respuesta
        cid2 = self.card("duel_1v1")
        run(self.cog.play_card(self.inter(self.alice), cid2, wdb.DUEL_MAX, "10m", self.bob))
        d2 = duels_db.find_by_message(self.db, self.duelch.sent[-1].id)
        duels_db.expire_duel(self.db, d2.id, now=time.time() + 2 * 24 * 3600)
        self.assertEqual(self.bal(self.bob), 50 * M - M - M // 2)

    def test_duelo_normal_sigue_sin_penalidad(self):
        self.fund(self.alice, 10 * M)
        self.fund(self.bob, 10 * M)
        r = duels_db.create_duel(self.db, guild_id=GUILD_ID, channel_id=1, challenger_id=self.alice.id,
                                 challenged_id=self.bob.id, stake=M)
        duels_db.decline_duel(self.db, r.duel.id, self.bob.id)
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (10 * M, 10 * M))

    def test_duelo_contra_escudo_no_gasta(self):
        self.fund(self.alice, 50 * M)
        self.fund(self.bob, 50 * M)
        edb.use_card(self.db, card_id=self.card("shield_1h", self.bob), owner_id=self.bob.id, prize_key="shield_1h",
                     action="shield", minutes=60, target_id=self.bob.id)
        cid = self.card("duel_1v1")
        i = self.inter(self.alice)
        run(self.cog.play_card(i, cid, wdb.DUEL_MAX, "10m", self.bob))
        self.assertIn("escudo", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_duelo_sin_canal_no_gasta(self):
        wdb.set_setting(self.db, "duel_channel", None)
        cid = self.card("duel_1v1")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        self.assertIn("canal-duelos", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
