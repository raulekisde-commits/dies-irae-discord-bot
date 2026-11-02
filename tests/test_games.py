"""Pruebas de /dados y /coinflip (lógica con SQLite real + capa de Discord con el stub, sin red).

NO prueban que la API real de Discord acepte los mensajes ni el comportamiento real de los botones.
"""
import asyncio
import os
import random
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import economy as economy  # noqa: E402
import economy_db as eco  # noqa: E402
import games as gm  # noqa: E402
import casino_db as cdb  # noqa: E402
import games_db as gdb  # noqa: E402
import stats_db  # noqa: E402

GUILD_ID = 2000000000000000001
MEMBER_ROLE = 1000000000000000001
STAFF_ROLE = 1000000000000000002
GAMER_ROLE = 1000000000000000005
GAME_CH = 3000000000000000001
OTHER_CH = 3000000000000000002
DICE_CH = 3000000000000000003

_LOOP = asyncio.new_event_loop()


def run(coro):
    result = _LOOP.run_until_complete(coro)
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return result


class ScriptRng:
    """RNG determinista: devuelve los valores indicados en orden (para dados: tiradas; para moneda: 0/1)."""

    def __init__(self, *values):
        self.values = list(values)

    def randint(self, a, b):
        v = self.values.pop(0)
        assert a <= v <= b, (a, b, v)
        return v

    def choice(self, seq):
        return seq[0]


# ============================================================
# Lógica
# ============================================================
class GamesDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        gdb.init_schema(self.db)
        self.t0 = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, uid, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"f{uid}{os.urandom(3).hex()}")

    def bal(self, uid):
        return eco.get_balance(self.db, uid)

    def total_money(self, ids):
        return sum(self.bal(i) for i in ids)

    def create(self, kind="dice", creator=1, stake=1000, channel=10):
        self.fund(creator, 100000) if self.bal(creator) < stake else None
        return gdb.create_game(self.db, kind=kind, guild_id=1, channel_id=channel, creator_id=creator, stake=stake, now=self.t0)

    # ---- winrate ----
    def play_dice(self, a, b, a_wins, t=0):
        self.fund(a, 100000)
        self.fund(b, 100000)
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10 + t, creator_id=a, stake=100, now=self.t0 + t * 1000).game
        gdb.join_game(self.db, g.id, b, now=self.t0 + t * 1000 + 1)
        gdb.resolve_game(self.db, g.id, now=self.t0 + t * 1000 + 301, rng=ScriptRng(*((6, 1) if a_wins else (1, 6))))

    def test_winrate_counts_only_finished_games_with_a_winner(self):
        for n in range(4):
            self.play_dice(1, 2, a_wins=(n < 3), t=n)             # 1 gana 3, 2 gana 1
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=99, creator_id=1, stake=100, now=self.t0 + 9000).game
        gdb.resolve_game(self.db, g.id, now=self.t0 + 9301)        # sin rival: se devuelve, no cuenta
        w1, w2 = stats_db.get_winrate(self.db, "dice", 1), stats_db.get_winrate(self.db, "dice", 2)
        self.assertEqual((w1.played, w1.wins, w1.percent), (4, 3, "75%"))
        self.assertEqual((w2.played, w2.wins, w2.percent), (4, 1, "25%"))
        self.assertEqual(stats_db.get_winrate(self.db, "coinflip", 1).percent, "—")
        self.assertEqual(stats_db.get_winrate(self.db, "dice", 77).played, 0)

    def test_server_wagered_sums_everything_net_of_refunds(self):
        self.play_dice(1, 2, a_wins=True, t=0)                      # 2 entradas de 100
        self.play_dice(1, 2, a_wins=False, t=1)                     # otras 2
        self.fund(3, 100000)
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=99, creator_id=3, stake=500, now=self.t0 + 9000).game
        gdb.resolve_game(self.db, g.id, now=self.t0 + 9301)         # sin rival: se devuelve → no cuenta
        data = stats_db.server_wagered(self.db)
        self.assertEqual(data.by_kind["dice_stake"][0], 400)
        self.assertEqual(data.total, 400)
        self.assertGreaterEqual(data.players, 2)

    def test_top_winrate_combined_adds_both_games(self):
        for n in range(5):
            self.play_dice(1, 2, a_wins=True, t=n)
        rows = stats_db.top_winrate_combined(self.db)
        self.assertEqual([(w.user_id, w.played, w.wins) for w in rows], [(1, 5, 5), (2, 5, 0)])

    def test_top_winrate_needs_minimum_games_and_orders_by_rate(self):
        for n in range(5):
            self.play_dice(1, 2, a_wins=True, t=n)                 # 1: 5/5 · 2: 0/5
        self.play_dice(3, 4, a_wins=True, t=10)                    # 3: 1/1 (pocas partidas: no entra)
        top = stats_db.top_winrate(self.db, "dice")
        self.assertEqual([(w.user_id, w.percent) for w in top], [(1, "100%"), (2, "0%")])
        self.assertEqual([w.user_id for w in stats_db.top_winrate(self.db, "dice", min_games=1)][:2], [1, 3])
        self.assertEqual(stats_db.top_winrate(self.db, "coinflip"), [])

    # ---- crear ----
    def test_create_debits_creator_and_lasts_five_minutes(self):
        self.fund(1, 5000)
        r = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10, creator_id=1, stake=1000, now=self.t0)
        self.assertTrue(r.ok)
        self.assertEqual(self.bal(1), 4000)
        self.assertEqual((r.game.players, r.game.pool, r.game.ends_at), ([1], 1000, self.t0 + 300))
        row = eco.get_history(self.db, 1)[0]
        self.assertEqual((row.mov_type, row.delta), ("dice_stake", -1000))

    def test_stake_limits(self):
        for bad in (0, -5, gdb.MAX_STAKE + 1):
            with self.assertRaises(gdb.GameError):
                gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10, creator_id=1, stake=bad)
        self.fund(1, gdb.MAX_STAKE * 2)
        self.assertTrue(gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10, creator_id=1, stake=gdb.MAX_STAKE, now=self.t0).ok)
        self.assertEqual(gdb.MAX_STAKE, 5_000_000)

    def test_create_without_funds_changes_nothing(self):
        self.fund(1, 500)
        r = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=10, creator_id=1, stake=1000, now=self.t0)
        self.assertEqual((r.ok, r.code, r.balance), (False, "funds", 500))
        self.assertEqual(self.bal(1), 500)
        self.assertIsNone(gdb.live_game(self.db, 1, "coinflip"))

    def test_only_one_live_game_per_kind_but_kinds_are_independent(self):
        self.create("dice", 1)
        again = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=11, creator_id=2, stake=10, now=self.t0)
        self.assertEqual((again.ok, again.code), (False, "live"))
        self.assertEqual(again.game.channel_id, 10)
        self.assertTrue(self.create("coinflip", 3).ok)
        self.assertEqual(gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=11, creator_id=4, stake=10, now=self.t0).code, "live")
        self.assertEqual(self.bal(2), 0)                                    # al rechazado no se le cobró
        self.fund(5, 100)
        self.assertTrue(gdb.create_game(self.db, kind="dice", guild_id=2, channel_id=12, creator_id=5, stake=10, now=self.t0).ok)  # otro servidor

    def test_new_game_allowed_after_previous_finished(self):
        g = self.create("dice", 1).game
        self.fund(2, 1000)
        gdb.join_game(self.db, g.id, 2, now=self.t0 + 1)
        gdb.resolve_game(self.db, g.id, now=self.t0 + 301, rng=ScriptRng(3, 5))
        self.assertTrue(self.create("dice", 3).ok)

    def test_abort_unpublished_refunds_and_frees_slot(self):
        g = self.create("dice", 1, 1000).game
        before = self.bal(1)
        self.assertTrue(gdb.abort_unpublished(self.db, g.id))
        self.assertEqual(self.bal(1), before + 1000)
        self.assertFalse(gdb.abort_unpublished(self.db, g.id))
        self.assertTrue(self.create("dice", 2).ok)

    # ---- dados ----
    def test_join_debits_blocks_duplicates_and_closes_at_deadline(self):
        g = self.create("dice", 1, 1000).game
        self.fund(2, 1500)
        r = gdb.join_game(self.db, g.id, 2, now=self.t0 + 10)
        self.assertEqual((r.status, r.balance, self.bal(2)), ("ok", 500, 500))
        self.assertEqual(r.game.pool, 2000)
        self.assertEqual(gdb.join_game(self.db, g.id, 2, now=self.t0 + 11).status, "already")
        self.assertEqual(gdb.join_game(self.db, g.id, 1, now=self.t0 + 11).status, "already")
        self.assertEqual(self.bal(2), 500)
        self.fund(3, 100)
        self.assertEqual(gdb.join_game(self.db, g.id, 3, now=self.t0 + 11).status, "funds")
        self.assertEqual(self.bal(3), 100)
        self.fund(4, 5000)
        self.assertEqual(gdb.join_game(self.db, g.id, 4, now=self.t0 + 300).status, "closed")      # tarde
        self.assertEqual(self.bal(4), 5000)
        self.assertEqual(gdb.join_game(self.db, 999, 4, now=self.t0).status, "notfound")

    def test_dice_highest_roll_wins_whole_pool(self):
        g = self.create("dice", 1, 1000).game
        for uid in (2, 3):
            self.fund(uid, 1000)
            gdb.join_game(self.db, g.id, uid, now=self.t0 + 5)
        start = self.total_money([1, 2, 3])
        self.assertIsNone(gdb.resolve_game(self.db, g.id, now=self.t0 + 299, rng=ScriptRng()))     # todavía no vence
        res = gdb.resolve_game(self.db, g.id, now=self.t0 + 300, rng=ScriptRng(2, 6, 4))
        self.assertEqual((res.status, res.winner_id, res.pool), ("done", 2, 3000))
        self.assertEqual(res.rounds, [[(1, 2), (2, 6), (3, 4)]])
        self.assertEqual(self.bal(2), 3000)
        self.assertEqual(self.total_money([1, 2, 3]), start + 3000)   # el pozo vuelve completo al ganador (start excluye lo apostado)
        row = eco.get_history(self.db, 2)[0]
        self.assertEqual((row.mov_type, row.delta), ("dice_win", 3000))
        self.assertIsNone(gdb.resolve_game(self.db, g.id, now=self.t0 + 400, rng=ScriptRng()))     # no se paga dos veces
        self.assertEqual(self.bal(2), 3000)

    def test_dice_ties_reroll_only_among_tied_until_one_winner(self):
        g = self.create("dice", 1, 100).game
        for uid in (2, 3, 4):
            self.fund(uid, 100)
            gdb.join_game(self.db, g.id, uid, now=self.t0 + 5)
        # ronda 1: 1→3, 2→6, 3→6, 4→2 | desempate entre 2 y 3: 4,4 | otro desempate: 1,5
        res = gdb.resolve_game(self.db, g.id, now=self.t0 + 300, rng=ScriptRng(3, 6, 6, 2, 4, 4, 1, 5))
        self.assertEqual(res.winner_id, 3)
        self.assertEqual([[u for u, _ in rnd] for rnd in res.rounds], [[1, 2, 3, 4], [2, 3], [2, 3]])
        self.assertEqual(self.bal(3), 400)
        self.assertEqual(sum(self.bal(u) for u in (1, 2, 4)), self.bal(1) + self.bal(2) + self.bal(4))
        self.assertEqual(gdb.get_game(self.db, g.id).outcome["rounds"][1], [[2, 4], [3, 4]])

    def test_dice_with_nobody_else_refunds_the_creator(self):
        g = self.create("dice", 1, 1000).game
        before = self.bal(1)
        res = gdb.resolve_game(self.db, g.id, now=self.t0 + 300, rng=ScriptRng())
        self.assertEqual(res.status, "refunded")
        self.assertEqual(self.bal(1), before + 1000)
        self.assertEqual(eco.get_history(self.db, 1)[0].mov_type, "dice_refund")

    def test_real_randomness_always_one_winner_and_money_is_conserved(self):
        for n in range(60):
            ids = [100 + n * 10 + k for k in range(5)]
            for u in ids:
                self.fund(u, 500)
            g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10, creator_id=ids[0], stake=500, now=self.t0).game
            for u in ids[1:]:
                self.assertEqual(gdb.join_game(self.db, g.id, u, now=self.t0 + 1).status, "ok")
            res = gdb.resolve_game(self.db, g.id, now=self.t0 + 300)
            self.assertIn(res.winner_id, ids)
            self.assertEqual(self.total_money(ids), 2500)
            self.assertEqual(self.bal(res.winner_id), 2500)
            last = res.rounds[-1]
            self.assertEqual(sum(1 for _, r in last if r == max(r for _, r in last)), 1)

    # ---- coinflip ----
    def test_coinflip_accept_resolves_at_once_and_winner_takes_all(self):
        for side, winner in ((0, 1), (1, 2)):
            self.tearDown(); self.setUp()
            g = self.create("coinflip", 1, 2000).game
            self.fund(2, 2000)
            r = gdb.join_game(self.db, g.id, 2, now=self.t0 + 5, rng=ScriptRng(side))
            self.assertEqual(r.status, "ok")
            self.assertEqual((r.resolved.winner_id, r.resolved.pool, r.resolved.coin_side), (winner, 4000, "cara" if side == 0 else "cruz"))
            self.assertEqual(self.bal(winner), 4000 + (98000 if winner == 1 else 0))
            loser = 2 if winner == 1 else 1
            self.assertEqual(self.bal(loser), 98000 if loser == 1 else 0)
            self.assertEqual(gdb.get_game(self.db, g.id).status, "done")

    def test_coinflip_creator_can_choose_side(self):
        for side_pick, coin, winner in (("cruz", 1, 1), ("cruz", 0, 2), ("cara", 0, 1), ("cara", 1, 2), (None, 0, 1), (None, 1, 2)):
            self.tearDown(); self.setUp()
            self.fund(1, 100000)
            g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=10, creator_id=1, stake=1000, now=self.t0, side=side_pick).game
            self.assertEqual(g.creator_side, side_pick or "cara")
            self.assertEqual(g.other_side, "cruz" if (side_pick or "cara") == "cara" else "cara")
            self.fund(2, 1000)
            r = gdb.join_game(self.db, g.id, 2, now=self.t0 + 5, rng=ScriptRng(coin))
            self.assertEqual(r.resolved.winner_id, winner, (side_pick, coin))
            self.assertEqual(gdb.get_game(self.db, g.id).creator_side, side_pick or "cara")

    def test_coinflip_side_validation_and_dice_rejects_side(self):
        with self.assertRaises(gdb.GameError):
            gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=10, creator_id=1, stake=1000, now=self.t0, side="canto")
        with self.assertRaises(gdb.GameError):
            gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=10, creator_id=1, stake=1000, now=self.t0, side="cara")

    def test_old_database_gets_the_column(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.executescript("DROP TABLE game_players; DROP TABLE games; CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                        "guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, creator_id INTEGER NOT NULL, stake INTEGER NOT NULL, "
                        "status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, ends_at REAL NOT NULL, resolved_at TEXT, winner_id INTEGER, "
                        "payout INTEGER NOT NULL DEFAULT 0, outcome TEXT);")
        c.commit(); c.close()
        gdb.init_schema(self.db)
        self.assertTrue(self.create("coinflip", 1, 1000).ok)

    def test_coinflip_only_one_challenger_and_not_the_creator(self):
        g = self.create("coinflip", 1, 1000).game
        self.assertEqual(gdb.join_game(self.db, g.id, 1, now=self.t0 + 1).status, "own")
        self.fund(2, 1000)
        self.fund(3, 1000)
        self.assertEqual(gdb.join_game(self.db, g.id, 2, now=self.t0 + 2, rng=ScriptRng(0)).status, "ok")
        self.assertEqual(gdb.join_game(self.db, g.id, 3, now=self.t0 + 3).status, "closed")
        self.assertEqual(self.bal(3), 1000)

    def test_coinflip_without_challenger_refunds_after_five_minutes(self):
        g = self.create("coinflip", 1, 1000).game
        before = self.bal(1)
        self.assertIsNone(gdb.resolve_game(self.db, g.id, now=self.t0 + 100))
        res = gdb.resolve_game(self.db, g.id, now=self.t0 + 300)
        self.assertEqual(res.status, "refunded")
        self.assertEqual(self.bal(1), before + 1000)
        self.assertEqual(eco.get_history(self.db, 1)[0].mov_type, "coinflip_refund")
        self.fund(2, 1000)
        self.assertEqual(gdb.join_game(self.db, g.id, 2, now=self.t0 + 301).status, "closed")

    def test_coinflip_challenger_without_funds_leaves_game_open(self):
        g = self.create("coinflip", 1, 1000).game
        self.fund(2, 10)
        self.assertEqual(gdb.join_game(self.db, g.id, 2, now=self.t0 + 1).status, "funds")
        self.assertEqual(gdb.get_game(self.db, g.id).status, "open")
        self.assertEqual(self.bal(2), 10)

    def test_due_games_and_lookup_by_message(self):
        g = self.create("dice", 1).game
        gdb.set_message(self.db, g.id, 777)
        self.assertEqual(gdb.find_by_message(self.db, 777).id, g.id)
        self.assertEqual(gdb.due_games(self.db, self.t0 + 299), [])
        self.assertEqual(gdb.due_games(self.db, self.t0 + 300), [g.id])


# ============================================================
# Discord
# ============================================================
class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent, self.edits = [], []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))

    async def edit_message(self, **kw):
        self.done = True
        self.edits.append(kw)

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append((content, kw))


class FakeInteraction:
    _n = 0

    def __init__(self, guild, user, channel, message=None):
        FakeInteraction._n += 1
        self.id = 6100000000000000000 + FakeInteraction._n
        self.guild, self.user, self.channel, self.message = guild, user, channel, message
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    @property
    def text(self):
        return "\n".join(str(c) for c, _ in self.response.sent if c)

    @property
    def ephemeral_all(self):
        return all(kw.get("ephemeral") for _, kw in self.response.sent)


class FakeCtx:
    _mid = 8100000000000000000

    def __init__(self, guild, author, channel):
        FakeCtx._mid += 1
        self.guild, self.author, self.channel = guild, author, channel
        self.message = discord.Message(channel=channel, content="", author=author)
        self.message.id = FakeCtx._mid
        self.out = []

    async def reply(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))

    async def send(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))

    @property
    def text(self):
        return "\n".join(c for c, _ in self.out if c)


class GamesDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.bot.user = self.guild.me
        self.ch = discord.TextChannel(GAME_CH, self.guild, "juegos")
        self.other = discord.TextChannel(OTHER_CH, self.guild, "general")
        self.dice_ch = discord.TextChannel(DICE_CH, self.guild, "mesa-de-dados")
        self.guild._channels = {GAME_CH: self.ch, OTHER_CH: self.other, DICE_CH: self.dice_ch}
        self.bot.channels = dict(self.guild._channels)
        self.bot.guilds_map = {GUILD_ID: self.guild}
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.r_gamer = discord.Role(GAMER_ROLE, "Jugadores", self.guild)
        self.guild.roles = [self.r_member, self.r_gamer]

        def mk(uid, name, roles=(), admin=False):
            m = discord.Member(uid + 100000000000000000, name, roles=list(roles), guild=self.guild, administrator=admin)
            self.guild.members.append(m)
            return m

        self.admin = mk(10, "Admin", admin=True)
        self.gamer = mk(11, "Gamer", [self.r_gamer, self.r_member])
        self.p1 = mk(20, "Uno", [self.r_member])
        self.p2 = mk(21, "Dos", [self.r_member])
        self.p3 = mk(22, "Tres", [self.r_member])
        self.plain = mk(30, "Plain", [])
        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.cog = run(gm.setup_games(self.bot, self.db, GUILD_ID))
        self.ecog = self.bot.get_cog("EconomyCog")
        gdb.set_panel_channel_id(self.db, "dice", DICE_CH)          # /canal dados
        for cmd in ("dados", "coinflip"):                         # habilitados como lo haría el admin
            eco.set_command_channel(self.db, cmd, GAME_CH)
            eco.grant_command_role(self.db, cmd, GAMER_ROLE)

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user, channel=None, message=None):
        return FakeInteraction(self.guild, user, channel or self.ch, message)

    def fund(self, user, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[user.id], amount=amount, request_key=f"f{user.id}{os.urandom(3).hex()}")

    def bal(self, user):
        return eco.get_balance(self.db, user.id)

    def cmd(self, name, user, amount, channel=None):
        ctx = FakeCtx(self.guild, user, channel or self.ch)
        run(getattr(self.cog, name).callback(self.cog, ctx, cantidad=amount))
        return ctx

    def panel(self, kind):
        g = gdb.live_game(self.db, GUILD_ID, kind)
        chan = self.dice_ch if kind == "dice" else self.ch
        return g, next(m for m in chan.sent if m.id == g.message_id)

    def press(self, user, kind):
        """Un toque al botón del panel (anotarse / aceptar). Devuelve lo que ve la persona."""
        g, msg = self.panel(kind)
        i = self.inter(user, msg.channel, msg)
        run(self.cog.on_join(i))
        return types.SimpleNamespace(text=i.text, first=i)

    # ---- registro ----
    def test_commands_registered_as_prefix_and_slash(self):
        pref = {getattr(gm.GamesCog, n).name for n in dir(gm.GamesCog) if isinstance(getattr(gm.GamesCog, n, None), discord_stub.Command)}
        slash = {getattr(gm.GamesCog, n).name for n in dir(gm.GamesCog) if isinstance(getattr(gm.GamesCog, n, None), discord_stub.AppCommand)}
        self.assertEqual(pref, {"dados", "coinflip", "winrate", "topwinrate", "totalapostado"})
        self.assertEqual(slash, {"dados", "coinflip", "stats-winrate", "totalapostado"})
        kinds = {type(v).__name__ for v in self.bot.persistent_views}
        self.assertTrue({"DiceJoinView", "CoinAcceptView"} <= kinds)

    def test_games_are_manageable_with_perms_commands(self):
        self.assertIn("dados", eco.CHANNEL_COMMANDS)
        self.assertIn("coinflip", eco.CHANNEL_COMMANDS)
        self.assertIn("dados", eco.GRANTABLE_COMMANDS)
        self.assertIn("coinflip", eco.GRANTABLE_COMMANDS)

    # ---- permisos de creación ----
    def test_creation_needs_channel_and_role(self):
        self.fund(self.plain, 5000)
        self.fund(self.gamer, 5000)
        self.fund(self.admin, 5000)
        self.assertIn("permiso", self.cmd("dados", self.plain, "100").text)
        self.assertIn(f"<#{GAME_CH}>", self.cmd("dados", self.gamer, "100", self.other).text)    # canal equivocado
        self.assertIn(f"<#{GAME_CH}>", self.cmd("dados", self.admin, "100", self.other).text)    # ni el admin lo esquiva
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        self.assertEqual(self.bal(self.gamer), 5000)
        self.cmd("dados", self.gamer, "100")
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))

    def test_dice_panel_goes_to_the_configured_channel_not_where_the_command_was_sent(self):
        self.fund(self.gamer, 5000)
        out = self.cmd("dados", self.gamer, "1.000")
        self.assertEqual(self.ch.sent, [])                                    # en el canal del comando no hay panel
        self.assertEqual(len(self.dice_ch.sent), 1)
        g = gdb.live_game(self.db, GUILD_ID, "dice")
        self.assertEqual(g.channel_id, DICE_CH)
        self.assertIn(self.dice_ch.mention, out.text)                         # y se avisa dónde quedó
        self.assertIsNotNone(self.dice_ch.sent[0].view)

    def test_dice_needs_a_configured_panel_channel(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM eco_settings WHERE key = ?", (gdb.panel_channel_key("dice"),))
        c.commit()
        c.close()
        self.fund(self.gamer, 5000)
        out = self.cmd("dados", self.gamer, "1.000")
        self.assertIn("/canal dados", out.text)
        self.assertEqual(self.bal(self.gamer), 5000)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "dice"))

    def test_dice_channel_gone_is_reported_without_charging(self):
        gdb.set_panel_channel_id(self.db, "dice", 3999999999999999999)       # canal que ya no existe
        self.fund(self.gamer, 5000)
        out = self.cmd("dados", self.gamer, "1.000")
        self.assertIn("No encuentro el canal", out.text)
        self.assertEqual(self.bal(self.gamer), 5000)

    def test_coinflip_panel_stays_in_the_command_channel(self):
        self.fund(self.gamer, 5000)
        self.cmd("coinflip", self.gamer, "1.000")
        self.assertEqual(len(self.ch.sent), 1)
        self.assertEqual(self.dice_ch.sent, [])

    def test_coinflip_command_accepts_side_in_any_order(self):
        self.fund(self.gamer, 5000)
        self.cmd("coinflip", self.gamer, "1k cruz")
        g = gdb.live_game(self.db, self.guild.id, "coinflip")
        self.assertEqual((g.stake, g.creator_side), (1000, "cruz"))
        self.assertIn("cruz", self.ch.sent[0].embeds[0].description)

    def test_disabled_until_channel_is_configured(self):
        eco.set_setting(self.db, "dummy", "1")
        conn_cmd = "coinflip"
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM eco_command_channels WHERE command = ?", (conn_cmd,))
        c.commit()
        c.close()
        self.fund(self.admin, 1000)
        self.assertIn("/perms-canal", self.cmd("coinflip", self.admin, "100").text)

    # ---- dados ----
    def test_dice_panel_lists_creator_pool_and_countdown(self):
        self.fund(self.gamer, 10000)
        self.cmd("dados", self.gamer, "5.000")
        g, msg = self.panel("dice")
        emb = msg.embeds[0]
        fields = {n: v for n, v, _ in emb.fields}
        self.assertIn(f"<@{self.gamer.id}>", fields["👥 Anotados (1)"])
        self.assertIn("5,000", fields["💰 Pozo"])
        self.assertIn(f"<t:{int(g.ends_at)}:R>", fields["⏳ Sale el resultado"])
        self.assertEqual([c.label for c in msg.view.children], ["Anotarse"])
        self.assertIn("No te podés dar de baja", emb.description)
        self.assertEqual(self.bal(self.gamer), 5000)
        self.assertAlmostEqual(g.ends_at - __import__("time").time(), 300, delta=5)
        self.assertEqual(self.dice_ch.sent[0].channel, self.dice_ch)

    def test_amount_validation_messages(self):
        self.fund(self.gamer, 100)
        self.assertIn("máximo", self.cmd("dados", self.gamer, "5,000,001").text.lower())
        self.assertIn("❌", self.cmd("dados", self.gamer, "abc").text)
        self.assertIn("Uso", self.cmd("dados", self.gamer, "").text)
        self.assertIn("saldo", self.cmd("dados", self.gamer, "1000").text)
        self.assertEqual(self.bal(self.gamer), 100)
        self.assertEqual(self.ch.sent, [])

    def test_live_game_error_is_private_for_slash_and_self_deleting_for_prefix(self):
        self.fund(self.gamer, 10000)
        self.fund(self.admin, 10000)
        self.cmd("coinflip", self.gamer, "100")
        # con "!" Discord no tiene mensajes privados: la respuesta se borra sola y también el comando del usuario
        calls = []

        class Ctx(FakeCtx):
            async def reply(self, content=None, *, embed=None, **kw):
                calls.append(kw)
                await super().reply(content, embed=embed)

        ctx = Ctx(self.guild, self.admin, self.ch)
        run(self.cog.coinflip.callback(self.cog, ctx, cantidad="100"))
        self.assertIn("Ya hay un coinflip en curso", ctx.text)
        self.assertEqual(calls[0].get("delete_after"), gm.ERROR_VISIBLE_SECONDS)
        self.assertTrue(ctx.message.deleted)
        # con "/" es efímero
        i = FakeInteraction(self.guild, self.admin, self.ch)
        run(self.cog.coinflip_slash.callback(self.cog, i, "100"))
        self.assertIn("Ya hay un coinflip en curso", i.text)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))

    def test_topwinrate_and_totalapostado_commands_are_public(self):
        for n in range(5):
            self.play_dice_db(self.gamer.id, self.admin.id, win=True, t=n)
        ctx = FakeCtx(self.guild, self.admin, self.ch)
        run(self.cog.topwinrate.callback(self.cog, ctx))
        fields = {n: v for n, v, _ in ctx.out[0][1].fields}
        self.assertIn("100%", fields["🎲 Dados"])
        self.assertIn(str(self.gamer.id), fields["🎲 Dados"])
        self.assertIn("Todavía nadie", fields["🪙 Coinflip"])
        ctx = FakeCtx(self.guild, self.admin, self.ch)
        run(self.cog.totalapostado.callback(self.cog, ctx))
        emb = ctx.out[0][1]
        self.assertIn("entradas", emb.description)
        self.assertIn("Dados", [n for n, _v, _i in emb.fields][1])

    def test_winrate_command_shows_dice_and_coinflip_panel(self):
        for n in range(4):
            self.play_dice_db(self.gamer.id, self.admin.id, win=(n < 3), t=n)
        ctx = FakeCtx(self.guild, self.admin, self.ch)
        ctx.message.mentions = [self.gamer]
        run(self.cog.winrate.callback(self.cog, ctx, objetivo=self.gamer.mention))
        emb = ctx.out[0][1]
        fields = {n: v for n, v, _ in emb.fields}
        self.assertIn("75%", fields["🎲 Winrate Dados"])
        self.assertIn("3 ganadas de 4", fields["🎲 Winrate Dados"])
        self.assertIn("—", fields["🪙 Winrate Coinflip"])
        ctx2 = FakeCtx(self.guild, self.admin, self.ch)                      # sin argumento: el propio
        run(self.cog.winrate.callback(self.cog, ctx2, objetivo=""))
        self.assertIn("25%", {n: v for n, v, _ in ctx2.out[0][1].fields}["🎲 Winrate Dados"])
        for k in ("🎰 Total apostado", "🏆 Total ganado", "📉 Total perdido", "⚖️ Resultado neto"):
            self.assertIn(k, fields)
        self.assertNotIn("lista negra", ctx.out[0][1].description)
        import casino_db as cdb_
        cdb_.toggle_ban(self.db, self.guild.id, self.gamer.id, self.admin.id)
        ctx3 = FakeCtx(self.guild, self.admin, self.ch)
        ctx3.message.mentions = [self.gamer]
        run(self.cog.winrate.callback(self.cog, ctx3, objetivo=self.gamer.mention))
        self.assertIn("lista negra", ctx3.out[0][1].description)
        self.assertEqual(gm.GamesCog.winrate_slash.name, "stats-winrate")

    def play_dice_db(self, a, b, win, t=0):
        for u in (a, b):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[u], amount=100000, request_key=f"w{u}{os.urandom(3).hex()}")
        g = gdb.create_game(self.db, kind="dice", guild_id=GUILD_ID, channel_id=10 + t, creator_id=a, stake=100, now=1000.0 * (t + 1)).game
        gdb.join_game(self.db, g.id, b, now=1000.0 * (t + 1) + 1)
        gdb.resolve_game(self.db, g.id, now=1000.0 * (t + 1) + 301, rng=ScriptRng(*((6, 1) if win else (1, 6))))

    def test_only_one_live_dice_and_one_live_coinflip(self):
        self.fund(self.gamer, 10000)
        self.fund(self.admin, 10000)
        self.cmd("dados", self.gamer, "100")
        out = self.cmd("dados", self.admin, "100")
        self.assertIn("Ya hay unos dados en curso", out.text)
        self.assertEqual(self.bal(self.admin), 10000)
        self.cmd("coinflip", self.gamer, "100")                           # el otro tipo sí
        self.assertIn("Ya hay un coinflip en curso", self.cmd("coinflip", self.admin, "100").text)
        self.assertEqual((len(self.dice_ch.sent), len(self.ch.sent)), (1, 1))

    def test_closed_casino_blocks_creating_but_not_joining_what_already_started(self):
        self.fund(self.gamer, 10000)
        self.fund(self.p1, 10000)
        self.cmd("dados", self.gamer, "1000")
        cdb.set_open(self.db, GUILD_ID, False, 1)
        out = self.cmd("coinflip", self.gamer, "100")                         # crear algo nuevo: bloqueado
        self.assertIn("cerrado", out.text)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))
        self.assertEqual(self.bal(self.gamer), 9000)
        self.assertIn("Te anotaste", self.press(self.p1, "dice").text)        # pero la partida que ya estaba en marcha se puede completar
        self.assertEqual(self.bal(self.p1), 9000)
        cdb.set_open(self.db, GUILD_ID, True, 1)
        self.cmd("coinflip", self.gamer, "100")
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))

    def test_banned_people_cannot_create_or_join(self):
        self.fund(self.gamer, 10000)
        self.fund(self.p1, 10000)
        self.assertTrue(cdb.toggle_ban(self.db, GUILD_ID, self.gamer.id, 1))
        out = self.cmd("dados", self.gamer, "100")
        self.assertIn("bloqueado", out.text)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        self.assertEqual(self.bal(self.gamer), 10000)
        self.assertFalse(cdb.toggle_ban(self.db, GUILD_ID, self.gamer.id, 1))      # repetir lo saca de la lista
        self.cmd("dados", self.gamer, "100")
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        cdb.toggle_ban(self.db, GUILD_ID, self.p1.id, 1)
        self.assertIn("bloqueado", self.press(self.p1, "dice").text)
        self.assertEqual(self.bal(self.p1), 10000)
        cdb.toggle_ban(self.db, GUILD_ID, self.p1.id, 1)
        self.assertIn("Te anotaste", self.press(self.p1, "dice").text)

    def test_join_button_updates_panel_and_blocks_dupes_and_poor(self):
        self.fund(self.gamer, 10000)
        self.cmd("dados", self.gamer, "1000")
        self.fund(self.p1, 1000)
        i = self.press(self.p1, "dice")
        self.assertIn("Te anotaste", i.text)
        self.assertTrue(i.first.ephemeral_all)
        self.assertEqual(self.bal(self.p1), 0)
        g, msg = self.panel("dice")
        fields = {n: v for n, v, _ in msg.embeds[0].fields}
        self.assertIn(f"<@{self.p1.id}>", fields["👥 Anotados (2)"])
        self.assertIn("2,000", fields["💰 Pozo"])
        self.assertIn("Ya estás anotado", self.press(self.p1, "dice").text)
        self.assertIn("saldo", self.press(self.p2, "dice").text)
        self.assertEqual(self.bal(self.p2), 0)
        self.assertEqual(len(gdb.live_game(self.db, GUILD_ID, "dice").players), 2)

    def test_dice_finish_announces_closes_panel_and_pays_winner(self):
        self.fund(self.gamer, 1000)
        self.fund(self.p1, 1000)
        self.fund(self.p2, 1000)
        self.cmd("dados", self.gamer, "1000")
        self.press(self.p1, "dice")
        self.press(self.p2, "dice")
        g, panel = self.panel("dice")
        self.assertEqual(run(self.cog.process_due(g.ends_at - 1)), 0)
        n = run(self.cog.process_due(g.ends_at + 1, rng=ScriptRng(3, 3, 3, 1, 6, 2)))
        self.assertEqual(n, 1)
        self.assertEqual(self.bal(self.p1), 3000)                         # desempate de 3 → ganó p1 con 6
        self.assertEqual((self.bal(self.gamer), self.bal(self.p2)), (0, 0))
        self.assertIsNone(panel.view)
        self.assertIn("finalizado", panel.embeds[0].title)
        result = self.dice_ch.sent[-1].embeds[0]
        self.assertIn("resultado", result.title)
        self.assertIn(f"<@{self.p1.id}>", result.description)
        self.assertIn("Desempate 1", result.description)
        self.assertIn("3,000", result.description)
        self.assertEqual(run(self.cog.process_due(g.ends_at + 10)), 0)    # no se anuncia dos veces
        # y ahora sí se puede crear otro
        self.fund(self.gamer, 500)
        self.cmd("dados", self.gamer, "500")
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))

    def test_dice_with_no_other_player_refunds(self):
        self.fund(self.gamer, 1000)
        self.cmd("dados", self.gamer, "1000")
        g, _ = self.panel("dice")
        run(self.cog.process_due(g.ends_at + 1))
        self.assertEqual(self.bal(self.gamer), 1000)
        self.assertIn("se devolvió", self.dice_ch.sent[-1].embeds[0].description)

    def test_join_after_deadline_is_rejected_even_if_not_yet_announced(self):
        self.fund(self.gamer, 1000)
        self.cmd("dados", self.gamer, "1000")
        g, msg = self.panel("dice")
        self.fund(self.p1, 1000)
        import games_db
        orig = games_db.time.time
        games_db.time.time = lambda: g.ends_at + 1
        try:
            i = self.inter(self.p1, self.dice_ch, msg)
            run(self.cog.on_join(i))
        finally:
            games_db.time.time = orig
        self.assertIn("ya terminó", i.text)
        self.assertEqual(self.bal(self.p1), 1000)

    # ---- coinflip ----
    def test_coinflip_full_flow(self):
        self.fund(self.gamer, 2000)
        self.fund(self.p1, 2000)
        self.fund(self.p2, 2000)
        self.cmd("coinflip", self.gamer, "2000")
        g, msg = self.panel("coinflip")
        self.assertEqual([c.label for c in msg.view.children], ["Aceptar el desafío"])
        self.assertIn("No podés aceptar tu propio coinflip", self.press(self.gamer, "coinflip").text)
        self.assertEqual(self.bal(self.gamer), 0)
        # monkeypatch del azar: gana quien acepta ("cruz")
        import games_db
        orig = games_db.random.SystemRandom
        games_db.random.SystemRandom = lambda: ScriptRng(1)
        try:
            i = self.press(self.p1, "coinflip")
        finally:
            games_db.random.SystemRandom = orig
        self.assertIn("Aceptaste", i.text)
        self.assertEqual((self.bal(self.p1), self.bal(self.gamer)), (4000, 0))
        self.assertIsNone(msg.view)
        res = self.ch.sent[-1].embeds[0]
        self.assertIn("cruz", res.description)
        self.assertIn(f"<@{self.p1.id}>", res.description)
        self.assertEqual(self.bal(self.p2), 2000)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))
        self.cmd("coinflip", self.p1, "100", self.ch)                      # sin rol: no puede crear, pero el cupo ya está libre

    def test_coinflip_second_challenger_cannot_take_it_and_expiry_refunds(self):
        self.fund(self.gamer, 1000)
        self.cmd("coinflip", self.gamer, "1000")
        g, msg = self.panel("coinflip")
        run(self.cog.process_due(g.ends_at + 1))
        self.assertEqual(self.bal(self.gamer), 1000)
        self.assertIn("Nadie aceptó", self.ch.sent[-1].embeds[0].description)
        self.fund(self.p1, 1000)
        i = self.inter(self.p1, self.ch, msg)
        run(self.cog.on_join(i))
        self.assertIn("ya terminó", i.text)
        self.assertEqual(self.bal(self.p1), 1000)

    # ---- un solo toque (sin doble confirmación) ----
    def test_dice_join_is_one_tap_and_charges_immediately(self):
        self.fund(self.gamer, 5000)
        self.cmd("dados", self.gamer, "1000")
        self.fund(self.p1, 3000)
        i = self.press(self.p1, "dice")
        self.assertIn("Te anotaste", i.text)
        self.assertNotIn("Confirm", i.text)
        self.assertEqual(self.bal(self.p1), 2000)
        self.assertEqual(len(gdb.live_game(self.db, GUILD_ID, "dice").players), 2)
        self.assertTrue(all("view" not in kw or kw["view"] is None for _, kw in i.first.response.sent))   # sin botones extra

    def test_buttons_require_member_role_and_registration(self):
        self.fund(self.gamer, 5000)
        self.cmd("dados", self.gamer, "1000")
        self.fund(self.plain, 3000)
        i = self.press(self.plain, "dice")                                    # sin rol Miembro
        self.assertIn("Solo los miembros", i.text)
        self.assertEqual(self.bal(self.plain), 3000)
        self.assertEqual(len(gdb.live_game(self.db, GUILD_ID, "dice").players), 1)
        gate_ok = {"v": False}

        class Gate:
            MESSAGE = "🔒 Tenés que estar registrado"

            async def allowed(self, user, name):
                return gate_ok["v"]

        self.bot.registration_gate = Gate()
        self.fund(self.p1, 3000)
        i = self.press(self.p1, "dice")                                       # Miembro pero sin registro
        self.assertIn("registrado", i.text)
        self.assertEqual(self.bal(self.p1), 3000)
        gate_ok["v"] = True
        self.assertIn("Te anotaste", self.press(self.p1, "dice").text)          # Miembro + registrado
        self.assertEqual(self.bal(self.p1), 2000)

    def test_second_tap_does_not_charge_twice(self):
        self.fund(self.gamer, 5000)
        self.cmd("dados", self.gamer, "1000")
        self.fund(self.p1, 3000)
        self.press(self.p1, "dice")
        self.assertIn("Ya estás anotado", self.press(self.p1, "dice").text)
        self.assertEqual(self.bal(self.p1), 2000)

    def test_no_charge_when_creator_taps_or_without_funds(self):
        self.fund(self.gamer, 5000)
        self.cmd("dados", self.gamer, "1000")
        self.assertIn("Ya estás anotado", self.press(self.gamer, "dice").text)
        self.assertIn("saldo", self.press(self.p3, "dice").text)
        self.assertEqual(self.bal(self.gamer), 4000)

    def test_coinflip_accept_is_one_tap_and_resolves(self):
        self.fund(self.gamer, 2000)
        self.fund(self.p1, 2000)
        self.cmd("coinflip", self.gamer, "2000")
        i = self.press(self.p1, "coinflip")
        self.assertIn("Aceptaste", i.text)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))
        self.assertEqual(self.bal(self.gamer) + self.bal(self.p1), 4000)

    def test_limit_blocks_bigger_dice_and_coinflip_bets(self):
        self.fund(self.gamer, 50_000)
        eco.set_bet_limit(self.db, "dados", 2_000)
        eco.set_bet_limit(self.db, "coinflip", 500)
        i = self.inter(self.gamer)
        run(self.cog.dados_slash.callback(self.cog, i, "2.001"))
        self.assertIn("máxima en dados es 2,000", i.text)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        self.assertEqual(self.bal(self.gamer), 50_000)
        i = self.inter(self.gamer)
        run(self.cog.coinflip_slash.callback(self.cog, i, "501"))
        self.assertIn("máxima en coinflip es 500", i.text)
        i = self.inter(self.gamer)
        run(self.cog.dados_slash.callback(self.cog, i, "2.000"))                 # justo en el límite sí
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        eco.set_bet_limit(self.db, "coinflip", None)                              # sin límite propio vuelve el tope general
        i = self.inter(self.gamer)
        run(self.cog.coinflip_slash.callback(self.cog, i, "501"))
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))

    # ---- "/" de nuevo disponible ----
    def test_slash_versions_exist_again_and_create_panels(self):
        slash = {getattr(gm.GamesCog, n).name for n in dir(gm.GamesCog) if isinstance(getattr(gm.GamesCog, n, None), discord_stub.AppCommand)}
        self.assertEqual(slash, {"dados", "coinflip", "stats-winrate", "totalapostado"})
        self.fund(self.gamer, 5000)
        i = self.inter(self.gamer)
        run(self.cog.dados_slash.callback(self.cog, i, "1.000"))
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))
        self.assertEqual(len(self.dice_ch.sent), 1)                           # el panel va al canal de dados
        self.assertIn(self.dice_ch.mention, i.text)
        self.assertTrue(i.ephemeral_all)
        i = self.inter(self.gamer)
        run(self.cog.dados_slash.callback(self.cog, i, "1.000"))
        self.assertIn("Ya hay unos dados", i.text)
        self.assertTrue(i.ephemeral_all)
        i = self.inter(self.plain)
        run(self.cog.coinflip_slash.callback(self.cog, i, "100"))
        self.assertIn("permiso", i.text)
        i = self.inter(self.gamer)
        run(self.cog.coinflip_slash.callback(self.cog, i, "500"))
        self.assertTrue(i.ephemeral_all)                                       # siempre se responde la interacción
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))
        self.assertEqual(len(self.ch.sent), 1)

    def test_slash_respects_channel_and_role_rules_like_prefix(self):
        self.fund(self.gamer, 5000)
        i = self.inter(self.gamer, self.other)
        run(self.cog.coinflip_slash.callback(self.cog, i, "500"))
        self.assertIn(f"<#{GAME_CH}>", i.text)
        self.assertIsNone(gdb.live_game(self.db, GUILD_ID, "coinflip"))

    def test_panel_publish_failure_refunds_and_frees_slot(self):
        self.fund(self.gamer, 1000)
        self.dice_ch.fail_send = True
        out = self.cmd("dados", self.gamer, "1000")
        self.assertIn("No pude publicar", out.text)
        self.assertEqual(self.bal(self.gamer), 1000)
        self.dice_ch.fail_send = False
        self.cmd("dados", self.gamer, "1000")
        self.assertIsNotNone(gdb.live_game(self.db, GUILD_ID, "dice"))


# ============================================================
# Versiones "/" de los comandos con "!" de la economía
# ============================================================
class EconomySlashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.ch = discord.TextChannel(GAME_CH, self.guild, "bal")
        self.guild._channels = {GAME_CH: self.ch}
        self.bot.channels = dict(self.guild._channels)
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.guild.roles = [self.r_member]

        def mk(uid, name, roles=(), admin=False):
            m = discord.Member(uid + 100000000000000000, name, roles=list(roles), guild=self.guild, administrator=admin)
            self.guild.members.append(m)
            return m

        self.admin = mk(10, "Admin", admin=True)
        self.alice = mk(20, "Alice", [self.r_member])
        self.bob = mk(21, "Bob", [self.r_member])
        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.ecog = self.bot.get_cog("EconomyCog")

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user):
        return FakeInteraction(self.guild, user, self.ch)

    def test_slash_names_exist(self):
        slash = {getattr(economy.EconomyCog, n).name for n in dir(economy.EconomyCog) if isinstance(getattr(economy.EconomyCog, n, None), discord_stub.AppCommand)}
        self.assertTrue({"bal", "top", "addmoney", "addmoney-role", "remove-money"} <= slash)

    def test_addmoney_slash_same_rules_as_prefix(self):
        i = self.inter(self.alice)
        run(self.ecog.addmoney_slash.callback(self.ecog, i, self.bob, "100", None))
        self.assertIn("permiso", i.text)
        self.assertTrue(i.ephemeral_all)                                   # los errores son privados
        self.assertEqual(eco.get_balance(self.db, self.bob.id), 0)
        i = self.inter(self.admin)
        run(self.ecog.addmoney_slash.callback(self.ecog, i, self.bob, "1.500", "premio evento"))
        self.assertEqual(eco.get_balance(self.db, self.bob.id), 1500)
        self.assertFalse(i.ephemeral_all)                                  # el resultado es público, como con "!"
        self.assertEqual(eco.get_history(self.db, self.bob.id)[0].reason, "premio evento")

    def test_remove_money_and_role_slash(self):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[self.alice.id, self.bob.id], amount=1000, request_key="seed")
        i = self.inter(self.admin)
        run(self.ecog.remove_money_slash.callback(self.ecog, i, "300", self.alice, None, None))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 700)
        i = self.inter(self.admin)
        run(self.ecog.remove_money_slash.callback(self.ecog, i, "100", None, self.r_member, None))
        self.assertEqual((eco.get_balance(self.db, self.alice.id), eco.get_balance(self.db, self.bob.id)), (600, 900))
        i = self.inter(self.admin)
        run(self.ecog.remove_money_slash.callback(self.ecog, i, "100", None, None, None))        # ni usuario ni rol
        self.assertIn("una persona", i.text)
        i = self.inter(self.admin)
        run(self.ecog.remove_money_slash.callback(self.ecog, i, "100", self.alice, self.r_member, None))   # los dos
        self.assertIn("uno solo", i.text)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 600)
        i = self.inter(self.admin)
        run(self.ecog.addmoney_role_slash.callback(self.ecog, i, self.r_member, "50", None))
        self.assertEqual(eco.get_balance(self.db, self.bob.id), 950)

    def test_bal_and_top_slash_need_their_channel_like_before(self):
        i = self.inter(self.alice)
        run(self.ecog.bal_slash.callback(self.ecog, i, None))
        self.assertIn("/perms-canal", i.text)
        eco.set_command_channel(self.db, "bal", GAME_CH)
        eco.set_command_channel(self.db, "top", GAME_CH)
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[self.bob.id], amount=777, request_key="s2")
        i = self.inter(self.alice)
        run(self.ecog.bal_slash.callback(self.ecog, i, self.bob))
        emb = [kw["embed"] for _, kw in i.response.sent if kw.get("embed")][0]
        self.assertEqual(emb.title, "Wuju Coins")
        self.assertIn("777", emb.description)
        i = self.inter(self.alice)
        run(self.ecog.top_slash.callback(self.ecog, i))
        self.assertTrue([kw["embed"] for _, kw in i.response.sent if kw.get("embed")])


if __name__ == "__main__":
    unittest.main()
