"""Pruebas de contadores (stats_db), paneles de /apuestas panel, paginación y !top / !bal (SQLite real + stub, sin red).

NO prueban la API real de Discord (botones, mensajes efímeros ni permisos de canal).
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import bets_db as bdb  # noqa: E402
import economy as economy  # noqa: E402
import economy_db as eco  # noqa: E402
import games_db as gdb  # noqa: E402
import rankings as rk  # noqa: E402
import stats_db  # noqa: E402

GUILD_ID = 2000000000000000001
MEMBER_ROLE = 1000000000000000001
STAFF_ROLE = 1000000000000000002
EXTRA_ROLE = 1000000000000000007
CH = 3000000000000000001
BAL_CH = 3000000000000000002
BASE = 100000000000000000

_LOOP = asyncio.new_event_loop()


def run(coro):
    result = _LOOP.run_until_complete(coro)
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return result


class Seq:
    def __init__(self, *v):
        self.v = list(v)

    def randint(self, a, b):
        return self.v.pop(0)


def fund(db, uid, amount):
    eco.apply_operation(db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"f{uid}{os.urandom(3).hex()}")


def make_bet(db, wagers, winner):
    """wagers: [(user, opción, cantidad)]. Resuelve con la opción `winner`."""
    bid = bdb.create_bet(db, guild_id=1, channel_id=2, creator_id=1, statement="¿Quién gana?", options=["X", "Y"])
    for uid, opt, amount in wagers:
        r = bdb.place_wager(db, bid, uid, opt, amount)
        assert r.ok, r.message
    bdb.close_bet(db, bid, 1)
    assert bdb.resolve_bet(db, bid, 1, ["X", "Y"].index(winner)).ok
    return bid


# ============================================================
# Contadores
# ============================================================
class StatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        stats_db.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def stats(self, uid):
        s = stats_db.get_user_stats(self.db, uid)
        return (s.wagered, s.won, s.lost)

    def test_new_user_has_zeroes(self):
        self.assertEqual(self.stats(77), (0, 0, 0))

    def test_resolved_bet_counts_net_win_and_loss(self):
        for u in (1, 2, 3):
            fund(self.db, u, 10000)
        make_bet(self.db, [(1, "X", 1000), (2, "Y", 3000), (3, "Y", 1000)], "Y")
        # pozo 5000: B cobra 3750 (+750), C cobra 1250 (+250), A pierde 1000
        self.assertEqual(self.stats(1), (1000, 0, 1000))
        self.assertEqual(self.stats(2), (3000, 750, 0))
        self.assertEqual(self.stats(3), (1000, 250, 0))

    def test_open_and_cancelled_bets_do_not_count(self):
        for u in (1, 2):
            fund(self.db, u, 10000)
        bid = bdb.create_bet(self.db, guild_id=1, channel_id=2, creator_id=1, statement="x", options=["X", "Y"])
        bdb.place_wager(self.db, bid, 1, "X", 500)
        self.assertEqual(self.stats(1), (0, 0, 0))                      # todavía abierta
        bdb.cancel_bet(self.db, bid, 1, is_admin=True)
        self.assertEqual(self.stats(1), (0, 0, 0))                      # cancelada: se devolvió todo

    def test_dice_and_coinflip_results_count_and_refunds_do_not(self):
        t0 = 5_000_000.0
        for u in (1, 2, 3):
            fund(self.db, u, 100000)
        g = gdb.create_game(self.db, kind="dice", guild_id=1, channel_id=9, creator_id=1, stake=1000, now=t0).game
        gdb.join_game(self.db, g.id, 2, now=t0 + 1)
        gdb.join_game(self.db, g.id, 3, now=t0 + 1)
        self.assertEqual(self.stats(1), (0, 0, 0))                      # en curso no cuenta
        gdb.resolve_game(self.db, g.id, now=t0 + 300, rng=Seq(6, 2, 3))
        self.assertEqual(self.stats(1), (1000, 2000, 0))                # ganó el pozo de 3000 poniendo 1000
        self.assertEqual(self.stats(2), (1000, 0, 1000))
        c = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=9, creator_id=2, stake=500, now=t0 + 400).game
        gdb.resolve_game(self.db, c.id, now=t0 + 700)                   # sin rival: se devuelve
        self.assertEqual(self.stats(2), (1000, 0, 1000))
        c2 = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=9, creator_id=2, stake=500, now=t0 + 800).game
        gdb.join_game(self.db, c2.id, 3, now=t0 + 801, rng=Seq(0))      # gana quien creó (cara)
        self.assertEqual(self.stats(2), (1500, 500, 1000))
        self.assertEqual(self.stats(3), (1500, 0, 1500))

    def test_counters_match_real_balances(self):
        for u in (1, 2, 3):
            fund(self.db, u, 10000)
        make_bet(self.db, [(1, "X", 1000), (2, "Y", 3000), (3, "Y", 1000)], "Y")
        make_bet(self.db, [(1, "Y", 2500), (2, "X", 700)], "Y")
        for u in (1, 2, 3):
            s = stats_db.get_user_stats(self.db, u)
            self.assertEqual(eco.get_balance(self.db, u) - 10000, s.won - s.lost, u)

    def test_top_by_orders_and_skips_zeroes(self):
        for u in (1, 2, 3, 4):
            fund(self.db, u, 10000)
        make_bet(self.db, [(1, "X", 1000), (2, "Y", 3000), (3, "Y", 1000), (4, "X", 4000)], "Y")
        self.assertEqual([s.user_id for s in stats_db.top_by(self.db, "won")], [2, 3])
        self.assertEqual([s.user_id for s in stats_db.top_by(self.db, "lost")], [4, 1])
        self.assertEqual([s.user_id for s in stats_db.top_by(self.db, "wagered")], [4, 2, 1, 3])
        with self.assertRaises(ValueError):
            stats_db.top_by(self.db, "otro")


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
        self.id = 6200000000000000000 + FakeInteraction._n
        self.guild, self.user, self.channel, self.message = guild, user, channel, message
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    @property
    def text(self):
        return "\n".join(str(c) for c, _ in self.response.sent if c)

    @property
    def embed(self):
        return next((kw["embed"] for _, kw in self.response.sent if kw.get("embed")), None)

    @property
    def view(self):
        return next((kw["view"] for _, kw in self.response.sent if kw.get("view")), None)


class FakeCtx:
    _mid = 8200000000000000000

    def __init__(self, guild, author, channel):
        FakeCtx._mid += 1
        self.guild, self.author, self.channel = guild, author, channel
        self.message = discord.Message(channel=channel, content="", author=author)
        self.message.id = FakeCtx._mid
        self.out = []

    async def reply(self, content=None, *, embed=None, view=None, **kw):
        self.out.append((content, embed, view))

    async def send(self, content=None, *, embed=None, view=None, **kw):
        self.out.append((content, embed, view))

    @property
    def embeds(self):
        return [e for _, e, _ in self.out if e]

    @property
    def views(self):
        return [v for _, _, v in self.out if v]


class RankingsDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.bot.user = self.guild.me
        self.ch = discord.TextChannel(CH, self.guild, "ranking")
        self.balch = discord.TextChannel(BAL_CH, self.guild, "bal")
        self.guild._channels = {CH: self.ch, BAL_CH: self.balch}
        self.bot.channels = dict(self.guild._channels)
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.r_extra = discord.Role(EXTRA_ROLE, "Extra", self.guild)
        self.guild.roles = [self.r_member, self.r_extra]

        def mk(uid, name, roles=(), admin=False):
            m = discord.Member(uid + BASE, name, roles=list(roles), guild=self.guild, administrator=admin)
            self.guild.members.append(m)
            return m

        self.mk = mk
        self.admin = mk(10, "Admin", admin=True)
        self.extra = mk(11, "Extra", [self.r_extra])
        self.alice = mk(20, "Alice", [self.r_member])
        self.bob = mk(21, "Bob", [self.r_member])
        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.cog = run(rk.setup_rankings(self.bot, self.db, GUILD_ID))
        self.ecog = self.bot.get_cog("EconomyCog")

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user, channel=None, message=None):
        return FakeInteraction(self.guild, user, channel or self.ch, message)

    def seed_many(self, n=25):
        """n ganadores (stake i*100 a X) y un perdedor grande (apuesta a Y)."""
        loser = BASE + 9000
        fund(self.db, loser, 10 ** 7)
        wagers = []
        for i in range(1, n + 1):
            uid = BASE + 1000 + i
            fund(self.db, uid, 10 ** 6)
            wagers.append((uid, "X", i * 100))
        wagers.append((loser, "Y", 50000))
        make_bet(self.db, wagers, "X")
        return loser

    def panel(self, user=None):
        i = self.inter(user or self.admin)
        run(self.cog.publish_panels(i))
        return i

    # ---- /apuestas panel ----
    def test_command_registered(self):
        import panels as pn
        self.assertEqual(pn.PanelsCog.panel.name, "panel")
        self.assertEqual({c.name for c in pn.PanelsCog.panel.commands.values()} if isinstance(pn.PanelsCog.panel.commands, dict) else set(), {"apuestas", "cambiomoneda", "reclutamiento", "foco", "shop", "coaching", "wujupon"})
        self.assertIn("apuestas", eco.GRANTABLE_COMMANDS)
        kinds = {type(v).__name__ for v in self.bot.persistent_views}
        self.assertIn("RankPanelView", kinds)

    def test_panel_permissions(self):
        i = self.panel(self.alice)
        self.assertIn("administradores", i.text)
        self.assertEqual(self.ch.sent, [])
        i = self.panel(self.extra)
        self.assertEqual(self.ch.sent, [])
        eco.grant_command_role(self.db, "apuestas", EXTRA_ROLE)
        self.panel(self.extra)
        self.assertEqual(len(self.ch.sent), 4)

    def test_panel_posts_four_top10_panels_with_next_button(self):
        self.seed_many(25)
        i = self.panel()
        self.assertIn("Paneles publicados", i.text)
        self.assertEqual(len(self.ch.sent), 4)
        titles = [m.embeds[0].title for m in self.ch.sent]
        self.assertEqual(titles, ["🎰 Top apostado", "🏆 Top ganado", "📉 Top perdido", "🚫 Lista negra del casino"])
        wagered = self.ch.sent[0].embeds[0].description.split("\n")
        self.assertEqual(len(wagered), 10)
        self.assertIn(f"Usuario {BASE + 9000}", wagered[0])                   # el perdedor puso 50,000: es quien más apostó
        self.assertIn("50,000", wagered[0])
        net = self.ch.sent[1].embeds[0].description.split("\n")
        self.assertEqual(len(net), 10)
        self.assertIn(f"Usuario {BASE + 1025}", net[0])                       # quien más puso a la ganadora, más ganó
        self.assertIn("+", net[0])
        with_button = [m for m in self.ch.sent if m.view is not None]
        self.assertEqual([m.view.children[0].custom_id for m in with_button], ["rank:wagered", "rank:net", "rank:loss"])
        for m in with_button:
            self.assertEqual([c.label for c in m.view.children], ["Siguiente ▶"])
        self.assertIsNone(self.ch.sent[3].view)                           # la lista negra no tiene botón

    def test_top_ganado_only_positives_and_top_perdido_only_negatives(self):
        loser = self.seed_many(25)
        self.panel()
        net_all = run(self.cog.rows_of("net"))
        self.assertEqual(len(net_all), 25)                                # los 25 ganadores, nadie en negativo
        self.assertTrue(all(v > 0 for _, v in net_all))
        self.assertNotIn(loser, [u for u, _ in net_all])
        loss_desc = self.ch.sent[2].embeds[0].description.split("\n")
        self.assertEqual(len(loss_desc), 1)                               # el único que perdió
        self.assertIn(f"Usuario {loser}", loss_desc[0])
        self.assertIn("-50,000", loss_desc[0])                            # se muestra en negativo
        i = self.inter(self.alice, self.ch, self.ch.sent[1])
        run(self.cog.on_next(i, "net"))
        j = self.inter(self.alice)
        run(i.view._go(j, +1))
        last = j.response.edits[0]["embed"].description.split("\n")
        self.assertEqual(len(last), 5)                                    # 25 personas: puestos 21–25
        self.assertNotIn(f"Usuario {loser}", "\n".join(last))

    def test_top_ganado_is_won_minus_lost_across_games(self):
        for u in (1, 2):
            fund(self.db, u, 100000)
        make_bet(self.db, [(1, "X", 1000), (2, "Y", 1000)], "X")          # 1 gana 1.000, 2 pierde 1.000
        make_bet(self.db, [(1, "X", 3000), (2, "Y", 1000)], "Y")          # 1 pierde 3.000, 2 gana 3.000
        self.assertEqual(run(self.cog.rows_of("net")), [(2, 2000)])        # 2: +3000 −1000 (Top ganado: solo positivos)
        self.assertEqual(run(self.cog.rows_of("loss")), [(1, -2000)])      # 1: +1000 −3000 (Top perdido: solo negativos)
        self.assertEqual([s.user_id for s in stats_db.top_by(self.db, "net")], [2, 1])

    def test_next_button_pages_through_the_full_list_ten_by_ten(self):
        self.seed_many(25)
        self.panel()
        wagered_panel = self.ch.sent[0]
        i = self.inter(self.alice, self.ch, wagered_panel)
        run(self.cog.on_next(i, "wagered"))
        self.assertTrue(all(kw.get("ephemeral") for _, kw in i.response.sent))
        lines = i.embed.description.split("\n")
        self.assertEqual(len(lines), 10)                                  # puestos 11–20
        self.assertIn("11–20", i.embed.title)
        self.assertTrue(lines[0].startswith("`11.`"))
        view = i.view
        self.assertFalse(view.prev.disabled)
        self.assertFalse(view.next.disabled)
        self.assertFalse(run(view.interaction_check(self.inter(self.bob))))   # la lista es de quien la abrió
        j = self.inter(self.alice)
        run(view._go(j, +1))
        last = j.response.edits[0]["embed"]
        self.assertEqual(len(last.description.split("\n")), 6)            # 26 personas: puestos 21–26
        self.assertTrue(view.next.disabled)
        run(view._go(self.inter(self.alice), -2))
        self.assertEqual(view.index, 0)
        self.assertEqual(len(view.pages[0].description.split("\n")), 10)
        self.assertTrue(view.prev.disabled)

    def test_next_with_a_short_list_says_there_is_nothing_more(self):
        self.seed_many(3)
        self.panel()
        i = self.inter(self.alice, self.ch, self.ch.sent[0])
        run(self.cog.on_next(i, "net"))
        self.assertIn("No hay más", i.text)
        self.assertIsNone(i.view)

    def test_empty_ranking_panels(self):
        self.panel()
        for m in self.ch.sent:
            d = m.embeds[0].description
            self.assertTrue("Todavía no hay datos" in d or "Nadie está bloqueado" in d, d)

    def test_blacklist_panel_lists_banned_people_and_updates(self):
        import casino_db as cdb
        self.panel()
        self.assertIn("Nadie está bloqueado", self.ch.sent[3].embeds[0].description)
        cdb.toggle_ban(self.db, GUILD_ID, BASE + 77, 1)
        cdb.toggle_ban(self.db, GUILD_ID, BASE + 78, 1)
        run(self.cog.refresh_all())
        d = self.ch.sent[3].embeds[0].description
        self.assertIn(f"Usuario {BASE + 77}", d)
        self.assertIn(f"Usuario {BASE + 78}", d)
        cdb.toggle_ban(self.db, GUILD_ID, BASE + 77, 1)
        run(self.cog.refresh_all())
        self.assertNotIn(f"Usuario {BASE + 77}", self.ch.sent[3].embeds[0].description)

    def test_refresh_updates_panels_and_repost_replaces_old_ones(self):
        self.panel()
        first = list(self.ch.sent)
        self.seed_many(3)
        self.assertEqual(run(self.cog.refresh_all()), 4)
        self.assertIn(f"Usuario {BASE + 1003}", first[0].embeds[0].description)
        self.panel()
        self.assertTrue(all(m.deleted for m in first))
        self.assertEqual(len([m for m in self.ch.sent if not m.deleted]), 4)
        self.assertEqual(run(self.cog.refresh_all()), 4)

    def test_panels_show_names_not_raw_mentions(self):
        self.bot.guilds_map = {GUILD_ID: self.guild}
        for u in (self.alice, self.bob):
            fund(self.db, u.id, 100000)
        make_bet(self.db, [(self.alice.id, "X", 1000), (self.bob.id, "Y", 3000)], "Y")
        self.panel()
        for m in self.ch.sent[:3]:
            d = m.embeds[0].description
            self.assertNotIn("<@", d)                                    # nada de ids crudos
            self.assertTrue("Alice" in d or "Bob" in d, d)
        self.assertIn("Alice — **-1,000", self.ch.sent[2].embeds[0].description)
        self.assertIn("Bob — **+1,000", self.ch.sent[1].embeds[0].description)

    def test_bal_gain_and_loss_use_the_same_numbers_as_the_panels(self):
        self.configure_top()
        for u in (self.alice, self.bob):
            fund(self.db, u.id, 100000)
        make_bet(self.db, [(self.alice.id, "X", 1000), (self.bob.id, "Y", 3000)], "Y")
        ctx = FakeCtx(self.guild, self.alice, self.balch)
        run(self.ecog.bal.callback(self.ecog, ctx, objetivo=""))
        f = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("-1,000", f["📉 Total perdido apostando"])         # igual que «Top perdido»: en negativo
        self.assertNotIn("-", f["🏆 Total ganado apostando"])
        ctx = FakeCtx(self.guild, self.bob, self.balch)
        run(self.ecog.bal.callback(self.ecog, ctx, objetivo=""))
        f = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("+1,000", f["🏆 Total ganado apostando"])           # igual que «Top ganado»: con +
        self.assertNotIn("-", f["📉 Total perdido apostando"])

    def test_obsolete_panels_are_removed_by_themselves(self):
        self.panel()
        old = []
        for k in ("lost_total", "wr_dice", "wr_coinflip"):
            m = run(self.ch.send(embed=None))
            old.append(m)
            rk.rdb.set_panel(self.db, GUILD_ID, k, self.ch.id, m.id)
        run(self.cog.refresh_all())
        self.assertTrue(all(m.deleted for m in old))
        self.assertIsNone(rk.rdb.get_panel(self.db, GUILD_ID, "wr_dice"))
        self.assertEqual(len([m for m in self.ch.sent if not m.deleted and m not in old]), 4)

    # ---- solo gente que sigue en el servidor + reinicio al salir ----
    def test_tops_and_blacklist_only_show_people_still_in_the_server(self):
        import casino_db as cdb
        self.bot.guilds_map = {GUILD_ID: self.guild}
        for u in (self.alice, self.bob):
            fund(self.db, u.id, 100000)
        make_bet(self.db, [(self.alice.id, "X", 1000), (self.bob.id, "Y", 3000)], "Y")
        self.assertEqual([u for u, _ in run(self.cog.rows_of("wagered"))], [self.bob.id, self.alice.id])
        self.guild.members.remove(self.bob)                                  # Bob se fue
        self.assertEqual([u for u, _ in run(self.cog.rows_of("wagered"))], [self.alice.id])
        self.assertEqual(run(self.cog.rows_of("net")), [])
        self.assertEqual([u for u, _ in run(self.cog.rows_of("loss"))], [self.alice.id])
        cdb.toggle_ban(self.db, GUILD_ID, self.bob.id, 1)
        cdb.toggle_ban(self.db, GUILD_ID, self.alice.id, 1)
        d = run(self.cog.blacklist_embed()).description
        self.assertIn("Alice", d)
        self.assertNotIn("Bob", d)
        self.assertTrue(cdb.is_banned(self.db, GUILD_ID, self.bob.id))        # el bloqueo se conserva por si vuelve

    def test_leaving_the_server_resets_balance_and_stats(self):
        import stats_db as st
        for u in (self.alice, self.bob):
            fund(self.db, u.id, 100000)
        make_bet(self.db, [(self.alice.id, "X", 1000), (self.bob.id, "Y", 3000)], "Y")
        self.assertEqual(st.get_user_stats(self.db, self.bob.id).won, 1000)
        run(self.ecog.on_member_remove(self.bob))
        self.assertEqual(eco.get_balance(self.db, self.bob.id), 0)
        s = st.get_user_stats(self.db, self.bob.id)
        self.assertEqual((s.wagered, s.won, s.lost, s.games), (0, 0, 0, 0))
        self.assertEqual([x.user_id for x in st.top_by(self.db, "wagered")], [self.alice.id])      # ya no aparece en ningún top
        self.assertEqual(st.get_user_stats(self.db, self.alice.id).lost, 1000)                      # los demás no cambian
        # si vuelve, empieza de cero: solo cuenta lo que juegue desde ahora
        fund(self.db, self.bob.id, 50000)
        make_bet(self.db, [(self.bob.id, "X", 500), (self.alice.id, "Y", 500)], "X")
        s = st.get_user_stats(self.db, self.bob.id)
        self.assertEqual((s.wagered, s.won, s.lost), (500, 500, 0))

    def test_leaving_resets_winrate_too(self):
        import stats_db as st
        for u in (1, 2):
            fund(self.db, u, 10 ** 6)
        g = gdb.create_game(self.db, kind="coinflip", guild_id=1, channel_id=10, creator_id=1, stake=100, now=1000.0).game
        gdb.join_game(self.db, g.id, 2, now=1001.0)
        gdb.resolve_game(self.db, g.id, now=1301.0, rng=type("R", (), {"randint": lambda s, a, b: 0, "choice": lambda s, q: q[0]})())
        self.assertEqual(st.get_winrate(self.db, "coinflip", 2).played, 1)
        st.reset_user(self.db, 2)
        self.assertEqual(st.get_winrate(self.db, "coinflip", 2).played, 0)
        self.assertEqual(st.get_winrate(self.db, "coinflip", 1).played, 1)

    def test_old_panels_are_replaced_and_old_buttons_explain(self):
        old_msgs = []
        for k in ("won", "lost"):
            m = run(self.ch.send(embed=None))
            old_msgs.append(m)
            rk.rdb.set_panel(self.db, GUILD_ID, k, self.ch.id, m.id)
        self.panel()
        self.assertTrue(all(m.deleted for m in old_msgs))
        self.assertIsNone(rk.rdb.get_panel(self.db, GUILD_ID, "won"))
        i = self.inter(self.alice)
        run(self.cog.on_next(i, "won"))
        self.assertIn("versión anterior", i.text)

    def test_publish_failure_leaves_no_half_panels(self):
        self.ch.fail_send = True
        i = self.panel()
        self.assertIn("No pude publicar", i.text)
        self.assertIsNone(rk.rdb.get_panel(self.db, GUILD_ID, "net"))

    # ---- !top con páginas ----
    def top_cmd(self, user):
        ctx = FakeCtx(self.guild, user, self.balch)
        run(self.ecog.top.callback(self.ecog, ctx))
        return ctx

    def configure_top(self):
        eco.set_command_channel(self.db, "top", BAL_CH)
        eco.set_command_channel(self.db, "bal", BAL_CH)

    def test_top_shows_ten_and_next_goes_ten_by_ten(self):
        self.configure_top()
        for i in range(25):
            m = self.mk(50 + i, f"p{i}", [self.r_member])
            fund(self.db, m.id, (i + 1) * 1000)
        ctx = self.top_cmd(self.alice)
        emb = ctx.embeds[0]
        self.assertEqual(len(emb.description.split("\n")), 10)
        self.assertIn("Top", emb.title)
        self.assertIn("Página 1/3", emb.footer)
        view = ctx.views[0]
        j = self.inter(self.alice, self.balch)
        run(view._go(j, +1))
        page2 = j.response.edits[0]["embed"]
        self.assertTrue(page2.description.split("\n")[0].startswith("`11.`"))
        self.assertIn("Página 2/3", page2.footer)
        run(view._go(self.inter(self.alice), +1))
        page3 = view.pages[2]
        self.assertTrue(page3.description.split("\n")[0].startswith("`21.`"))
        self.assertTrue(view.next.disabled)
        self.assertFalse(run(view.interaction_check(self.inter(self.bob))))
        self.assertTrue(run(view.interaction_check(self.inter(self.alice))))

    def test_top_with_ten_or_fewer_has_no_buttons(self):
        self.configure_top()
        fund(self.db, self.alice.id, 500)
        ctx = self.top_cmd(self.alice)
        self.assertEqual(ctx.views, [])

    # ---- !bal con contadores ----
    def test_bal_shows_the_three_gambling_counters(self):
        self.configure_top()
        fund(self.db, self.alice.id, 10000)
        fund(self.db, self.bob.id, 10000)
        make_bet(self.db, [(self.alice.id, "X", 1000), (self.bob.id, "Y", 3000)], "Y")
        ctx = FakeCtx(self.guild, self.alice, self.balch)
        run(self.ecog.bal.callback(self.ecog, ctx, objetivo=""))
        fields = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("1,000", fields["🎲 Total apostado"])
        self.assertIn("0", fields["🏆 Total ganado apostando"])
        self.assertIn("1,000", fields["📉 Total perdido apostando"])
        ctx = FakeCtx(self.guild, self.alice, self.balch)
        run(self.ecog.bal.callback(self.ecog, ctx, objetivo=self.bob.mention))
        fields = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("3,000", fields["🎲 Total apostado"])
        self.assertIn("1,000", fields["🏆 Total ganado apostando"])
        self.assertEqual(ctx.embeds[0].title, "Wuju Coins")


if __name__ == "__main__":
    unittest.main()
