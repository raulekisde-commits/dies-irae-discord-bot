"""Pruebas de la capa de Discord de apuestas, motivos e historial usando el stub (sin red).

Verifican permisos, flujo completo (crear → apostar → cerrar → resolver/cancelar), panel, hilo,
historial por DM / efímero y motivos de los comandos de economía. NO prueban la API real de Discord.
"""
import asyncio
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import bets as betsm  # noqa: E402
import bets_db as bdb  # noqa: E402
import economy as economy  # noqa: E402
import economy_db as eco  # noqa: E402

GUILD_ID = 2000000000000000001
MEMBER_ROLE = 1000000000000000001
STAFF_ROLE = 1000000000000000002
BETTOR_ROLE = 1000000000000000003
TARGET_ROLE = 1000000000000000004
PANEL_CH = 3000000000000000001
LOG_CH = 3000000000000000002

_LOOP = asyncio.new_event_loop()


def run(coro):
    result = _LOOP.run_until_complete(coro)
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return result


class DMMember(discord.Member):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.dms, self.block_dms = [], False

    async def send(self, content=None, *, embed=None, **kw):
        if self.block_dms:
            raise discord.Forbidden("DMs cerrados")
        self.dms.append(embed)


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
    def __init__(self, guild, user, channel, message=None):
        self.guild, self.user, self.channel, self.message = guild, user, channel, message
        self.channel_id = channel.id
        FakeInteraction._n = getattr(FakeInteraction, "_n", 0) + 1
        self.id = 6000000000000000000 + FakeInteraction._n
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    async def edit_original_response(self, **kw):
        self.response.edits.append(kw)

    @property
    def text(self):
        out = [str(c) for c, _ in self.response.sent if c]
        out += [str(e.get("content")) for e in self.response.edits if e.get("content")]
        return "\n".join(out)

    @property
    def embeds(self):
        return [kw["embed"] for _, kw in self.response.sent if kw.get("embed")]

    @property
    def views(self):
        return [kw["view"] for _, kw in self.response.sent if kw.get("view")]

    @property
    def ephemeral_all(self):
        return all(kw.get("ephemeral") for _, kw in self.response.sent)


class FakeCtx:
    _mid = 8000000000000000000

    def __init__(self, guild, author, channel, content=""):
        FakeCtx._mid += 1
        self.guild, self.author, self.channel = guild, author, channel
        self.message = discord.Message(channel=channel, content=content, author=author)
        self.message.id = FakeCtx._mid
        self.out = []
        self.views = []

    async def reply(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))
        if kw.get("view") is not None:
            self.views.append(kw["view"])

    async def send(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))

    @property
    def text(self):
        return "\n".join(c for c, _ in self.out if c)

    @property
    def embeds(self):
        return [e for _, e in self.out if e]


class BetsDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.channel = discord.TextChannel(PANEL_CH, self.guild, "apuestas")
        self.logch = discord.TextChannel(LOG_CH, self.guild, "log")
        self.bot.channels = {PANEL_CH: self.channel, LOG_CH: self.logch}
        self.bot.guilds_map = {GUILD_ID: self.guild}
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.r_staff = discord.Role(STAFF_ROLE, "Staff", self.guild)
        self.r_bettor = discord.Role(BETTOR_ROLE, "Apostadores", self.guild)
        self.r_target = discord.Role(TARGET_ROLE, "Target", self.guild)
        self.guild.roles = [self.r_member, self.r_staff, self.r_bettor, self.r_target]

        def mk(uid, name, roles=(), admin=False, cls=DMMember):
            m = cls(uid + 100000000000000000, name, roles=list(roles), guild=self.guild, administrator=admin)
            self.guild.members.append(m)
            return m

        self.admin = mk(10, "Admin", admin=True)
        self.creator = mk(11, "Creador", [self.r_bettor])
        self.alice = mk(20, "Alice", [self.r_member, self.r_target])
        self.bob = mk(21, "Bob", [self.r_member, self.r_target])
        self.carol = mk(22, "Carol", [self.r_member])
        self.staff = mk(30, "Staffer", [self.r_staff])
        self.plain = mk(31, "Plain", [])

        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.cog = run(betsm.setup_bets(self.bot, self.db, GUILD_ID))
        self.ecog = self.bot.get_cog("EconomyCog")
        eco.grant_command_role(self.db, "apuesta", BETTOR_ROLE)

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user, channel=None, message=None):
        return FakeInteraction(self.guild, user, channel or self.channel, message)

    def fund(self, user, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[user.id], amount=amount, request_key=f"f:{user.id}:{os.urandom(3).hex()}")

    def bal(self, user):
        return eco.get_balance(self.db, user.id)

    def create(self, user=None, enunciado="¿Quién gana?", opciones="Gana Smogg, Pierde Smogg, Empate"):
        i = self.inter(user or self.creator)
        run(self.cog.apuesta.callback(self.cog, i, enunciado, opciones))
        bets = bdb.list_bets(self.db, ("open", "closed", "resolved", "cancelled"))
        if bets and bets[0].message_id:
            msg = next((m for m in self.channel.sent if m.id == bets[0].message_id), None)
            if msg is not None and msg.thread is not None:
                self.bot.channels[msg.thread.id] = msg.thread       # el bot real encuentra los hilos por ID
        return (bets[0] if bets else None), i

    def thread_of(self, bet):
        return next(m for m in self.channel.sent if m.id == bet.message_id).thread

    def say(self, name, user, channel, args=None):
        ctx = FakeCtx(self.guild, user, channel)
        cb = getattr(self.ecog if name in ("addmoney", "addmoney-role", "remove-money", "bal") else self.cog, name.replace("-", "_")).callback
        owner = self.ecog if name in ("addmoney", "addmoney-role", "remove-money", "bal") else self.cog
        run(cb(owner, ctx) if args is None else cb(owner, ctx, **args))
        return ctx

    def bet_cmd(self, bet, user, amount_text, thread=None):
        thread = thread or self.thread_of(bet)
        return self.say("apostar", user, thread, {"args": amount_text})

    # ---------- carga ----------
    def test_cogs_load_and_commands_exist(self):
        self.assertIn("BetsCog", self.bot.cogs)
        for name in ("apuesta", "cerrarapuesta", "resolverapuesta", "cancelarapuesta"):
            self.assertTrue(hasattr(self.cog, name))
        self.assertTrue(hasattr(self.cog, "apostar"))
        self.assertTrue(hasattr(self.ecog, "verhistorial"))
        self.assertTrue(any(isinstance(v, betsm.BetPanelView) for v in self.bot.persistent_views))
        self.assertEqual([c.custom_id for c in betsm.BetPanelView(self.cog).children], ["bet:detail", "bet:cancel"])

    # ---------- /apuesta ----------
    def test_create_requires_permission(self):
        bet, i = self.create(self.plain)
        self.assertIsNone(bet)
        self.assertIn("No tenés permiso", i.text)
        bet, _ = self.create(self.admin)
        self.assertIsNotNone(bet)
        bet2, _ = self.create(self.creator)           # rol otorgado con /eco perms-comando
        self.assertNotEqual(bet2.id, bet.id)

    def test_create_builds_panel_and_thread(self):
        bet, i = self.create()
        msg = next(m for m in self.channel.sent if m.id == bet.message_id)
        emb = msg.embed
        self.assertIn("¿Quién gana?", emb.description)
        names = {n for n, _, _ in emb.fields}
        self.assertTrue({"Estado", "Creada por", "Pozo total", "Participantes", "Opciones"} <= names)
        self.assertIsNotNone(msg.thread)
        self.assertEqual(bdb.get_bet(self.db, bet.id).thread_id, msg.thread.id)
        self.assertIn("creada", i.text)

    def test_create_validations(self):
        bet, i = self.create(opciones="solo una")
        self.assertIsNone(bet)
        self.assertIn("al menos 2", i.text)
        bet, i = self.create(opciones="a, A")
        self.assertIsNone(bet)

    def test_create_fails_cleanly_if_panel_cannot_be_posted(self):
        self.channel.fail_send = True
        bet, i = self.create()
        self.assertEqual(bet.status, "cancelled")
        self.assertIn("No pude publicar", i.text)

    # ---------- !apostar ----------
    def test_wager_in_thread_and_rules(self):
        self.fund(self.alice, 10000)
        bet, _ = self.create()
        ctx = self.bet_cmd(bet, self.alice, "gana  smogg 4.000")
        self.assertIn("apostó", ctx.text)
        self.assertEqual(self.bal(self.alice), 6000)
        ctx = self.bet_cmd(bet, self.alice, "Empate 7000")
        self.assertIn("saldo suficiente", ctx.text)
        self.assertEqual(self.bal(self.alice), 6000)
        ctx = self.bet_cmd(bet, self.alice, "Otra 10")
        self.assertIn("no existe", ctx.text)
        for bad in ("Empate 0", "Empate -5", "Empate 1.5", "Empate", "Empate abc"):
            ctx = self.bet_cmd(bet, self.alice, bad)
            self.assertIn("❌", ctx.text, bad)
        self.assertEqual(self.bal(self.alice), 6000)

    # ---------- !apostartodo / versiones "/" ----------
    def all_in(self, user, text, thread):
        return self.say("apostartodo", user, thread, {"opcion": text})

    def test_apostartodo_asks_confirmation_and_then_bets_everything(self):
        self.fund(self.alice, 7500)
        bet, _ = self.create()
        thread = self.thread_of(bet)
        ctx = self.all_in(self.alice, "gana smogg", thread)
        self.assertIn("TODO tu saldo", ctx.text)
        self.assertIn("7,500", ctx.text)
        self.assertEqual(self.bal(self.alice), 7500)                       # todavía no se descontó
        self.assertEqual(bdb.get_bet(self.db, bet.id).pool, 0)
        view = ctx.views[0]
        i = self.inter(self.alice, thread)
        run(view._ok(i))
        self.assertEqual(self.bal(self.alice), 0)
        got = bdb.get_bet(self.db, bet.id)
        self.assertEqual((got.pool, got.option_total(0)), (7500, 7500))
        self.assertIn("todo su saldo", i.response.edits[0]["content"])
        run(view._ok(self.inter(self.alice, thread)))                      # doble clic: no repite
        self.assertEqual(bdb.get_bet(self.db, bet.id).pool, 7500)

    def test_apostartodo_cancel_and_other_users_cannot_confirm(self):
        self.fund(self.alice, 3000)
        bet, _ = self.create()
        thread = self.thread_of(bet)
        view = self.all_in(self.alice, "Empate", thread).views[0]
        self.assertFalse(run(view.interaction_check(self.inter(self.bob, thread))))
        self.assertTrue(run(view.interaction_check(self.inter(self.alice, thread))))
        run(view._no(self.inter(self.alice, thread)))
        self.assertEqual(self.bal(self.alice), 3000)
        self.assertEqual(bdb.get_bet(self.db, bet.id).pool, 0)

    def test_apostartodo_aborts_if_balance_changed_before_confirming(self):
        self.fund(self.alice, 3000)
        bet, _ = self.create()
        thread = self.thread_of(bet)
        view = self.all_in(self.alice, "Empate", thread).views[0]
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[self.alice.id], amount=500, request_key="extra")
        i = self.inter(self.alice, thread)
        run(view._ok(i))
        self.assertIn("saldo cambió", i.response.edits[0]["content"])
        self.assertEqual(self.bal(self.alice), 3500)
        self.assertEqual(bdb.get_bet(self.db, bet.id).pool, 0)

    def test_apostartodo_rejections(self):
        bet, _ = self.create()
        thread = self.thread_of(bet)
        self.assertIn("no tenés saldo", self.all_in(self.alice, "Empate", thread).text.lower())   # saldo 0
        self.fund(self.alice, 1000)
        self.assertIn("no existe", self.all_in(self.alice, "Otra", thread).text)
        self.assertIn("Uso", self.all_in(self.alice, "", thread).text)
        self.assertIn("dentro del hilo", self.all_in(self.alice, "Empate", self.channel).text)
        run(self.cog.cerrarapuesta.callback(self.cog, self.inter(self.creator, thread), bet.id))
        self.assertIn("ya no acepta", self.all_in(self.alice, "Empate", thread).text)
        self.assertEqual(self.bal(self.alice), 1000)

    def test_slash_apostar_and_apostartodo(self):
        self.fund(self.alice, 5000)
        self.fund(self.bob, 2000)
        bet, _ = self.create()
        thread = self.thread_of(bet)
        i = self.inter(self.alice, thread)
        run(self.cog.apostar_slash.callback(self.cog, i, "Gana Smogg", "1.000"))
        self.assertIn("apostó", i.text)
        self.assertEqual(self.bal(self.alice), 4000)
        i = self.inter(self.alice, self.channel)                           # fuera del hilo: error privado
        run(self.cog.apostar_slash.callback(self.cog, i, "Gana Smogg", "1.000"))
        self.assertIn("dentro del hilo", i.text)
        self.assertTrue(i.ephemeral_all)
        i = self.inter(self.bob, thread)
        run(self.cog.apostartodo_slash.callback(self.cog, i, "Empate"))
        views = i.views
        self.assertEqual(len(views), 1)
        self.assertEqual(self.bal(self.bob), 2000)
        run(views[0]._ok(self.inter(self.bob, thread)))
        self.assertEqual(self.bal(self.bob), 0)

    def test_wager_outside_thread_rejected(self):
        self.fund(self.alice, 1000)
        bet, _ = self.create()
        ctx = self.say("apostar", self.alice, self.channel, {"args": "Empate 10"})
        self.assertIn("dentro del hilo", ctx.text)
        other = discord.Thread(777, self.guild, "otro")
        ctx = self.say("apostar", self.alice, other, {"args": "Empate 10"})
        self.assertIn("dentro del hilo", ctx.text)
        self.assertEqual(self.bal(self.alice), 1000)

    def test_panel_updates_after_wagers_and_lists_participants(self):
        self.fund(self.alice, 5000)
        self.fund(self.bob, 3000)
        bet, _ = self.create()
        self.bet_cmd(bet, self.alice, "Gana Smogg 2000")
        self.bet_cmd(bet, self.alice, "Empate 500")
        self.bet_cmd(bet, self.bob, "Gana Smogg 3000")
        run(self.cog.ticker.fn(self.cog))
        msg = next(m for m in self.channel.sent if m.id == bet.message_id)
        fields = {n: v for n, v, _ in msg.embed.fields}
        self.assertIn("5,500", fields["Pozo total"])
        self.assertEqual(fields["Participantes"], "2")
        self.assertIn("5,000", fields["Opciones"])
        participants = fields["Apuestas realizadas"]
        self.assertIn(f"<@{self.alice.id}>", participants)
        self.assertIn("Gana Smogg: 2,000", participants)
        self.assertIn("Empate: 500", participants)
        self.assertEqual(bdb.needing_sync(self.db), [])

    # ---------- cerrar / resolver ----------
    def test_close_only_creator_and_blocks_wagers(self):
        self.fund(self.alice, 1000)
        bet, _ = self.create()
        thread = self.thread_of(bet)
        i = self.inter(self.alice, thread)
        run(self.cog.cerrarapuesta.callback(self.cog, i, bet.id))
        self.assertIn("Solo quien creó", i.text)
        i = self.inter(self.admin, thread)
        run(self.cog.cerrarapuesta.callback(self.cog, i, bet.id))
        self.assertIn("Solo quien creó", i.text)                 # ni el admin (la regla es del creador)
        i = self.inter(self.creator, thread)
        run(self.cog.cerrarapuesta.callback(self.cog, i, None))  # inferida desde el hilo
        self.assertIn("cerrada", i.text)
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "closed")
        ctx = self.bet_cmd(bet, self.alice, "Empate 10")
        self.assertIn("cerrada", ctx.text)
        self.assertEqual(self.bal(self.alice), 1000)

    def full_example(self):
        for u, a in ((self.alice, 5000), (self.bob, 15000), (self.carol, 80000)):
            self.fund(u, a)
        bet, _ = self.create()
        self.bet_cmd(bet, self.alice, "Gana Smogg 5000")
        self.bet_cmd(bet, self.bob, "Gana Smogg 15000")
        self.bet_cmd(bet, self.carol, "Pierde Smogg 80000")
        return bet

    def test_resolve_flow_pays_updates_panel_and_announces(self):
        bet = self.full_example()
        thread = self.thread_of(bet)
        i = self.inter(self.creator, thread)
        run(self.cog.resolverapuesta.callback(self.cog, i, "gana smogg", None))
        self.assertIn("cerrá la apuesta", i.text.lower())          # todavía abierta
        run(self.cog.cerrarapuesta.callback(self.cog, self.inter(self.creator, thread), None))
        i = self.inter(self.alice, thread)
        run(self.cog.resolverapuesta.callback(self.cog, i, "Gana Smogg", None))
        self.assertIn("Solo quien creó", i.text)
        i = self.inter(self.creator, thread)
        run(self.cog.resolverapuesta.callback(self.cog, i, "Gana Smogg", None))
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "closed")      # falta confirmar
        run(i.views[0]._ok(self.inter(self.creator, thread)))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob), self.bal(self.carol)), (25000, 75000, 0))
        msg = next(m for m in self.channel.sent if m.id == bet.message_id)
        fields = {n: v for n, v, _ in msg.embed.fields}
        self.assertIn("Resuelta", fields["Estado"])
        self.assertIn("Gana Smogg", fields["Resultado"])
        self.assertIn("100,000", fields["Resultado"])
        self.assertTrue(any("Ganó" in (m.content or "") for m in thread.sent))
        # segunda resolución: rechazada y sin pagos nuevos
        i = self.inter(self.creator, thread)
        run(self.cog.resolverapuesta.callback(self.cog, i, "Gana Smogg", None))
        self.assertIn("ya fue resuelta", i.text)
        self.assertEqual(self.bal(self.alice), 25000)

    def test_resolve_without_winners_cancels_and_refunds(self):
        bet = self.full_example()
        thread = self.thread_of(bet)
        run(self.cog.cerrarapuesta.callback(self.cog, self.inter(self.creator, thread), None))
        i = self.inter(self.creator, thread)
        run(self.cog.resolverapuesta.callback(self.cog, i, "Empate", None))
        self.assertIn("Nadie apostó", i.text)
        run(i.views[0]._ok(self.inter(self.creator, thread)))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob), self.bal(self.carol)), (5000, 15000, 80000))
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "cancelled")

    def test_cancel_permissions_confirmation_and_idempotence(self):
        bet = self.full_example()
        thread = self.thread_of(bet)
        i = self.inter(self.alice, thread)
        run(self.cog.cancelarapuesta.callback(self.cog, i, None))
        self.assertIn("Solo quien creó", i.text)
        i = self.inter(self.admin, thread)
        run(self.cog.cancelarapuesta.callback(self.cog, i, None))
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "open")        # pide confirmación
        view = i.views[0]
        run(view._ok(self.inter(self.admin, thread)))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob), self.bal(self.carol)), (5000, 15000, 80000))
        run(view._ok(self.inter(self.admin, thread)))                        # doble clic
        i = self.inter(self.creator, thread)
        run(self.cog.cancelarapuesta.callback(self.cog, i, None))
        self.assertIn("ya fue resuelta o cancelada", i.text)
        self.assertEqual(self.bal(self.alice), 5000)

    def test_cancel_button_on_panel_only_for_creator_or_admin_and_refunds(self):
        bet = self.full_example()
        panel = next(m for m in self.channel.sent if m.id == bet.message_id)
        i = self.inter(self.alice, self.channel, message=panel)
        run(self.cog.cancel_from_panel(i))
        self.assertIn("Solo quien creó", i.text)
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "open")
        i = self.inter(self.creator, self.channel, message=panel)
        run(self.cog.cancel_from_panel(i))
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "open")        # primero pide confirmación
        run(i.views[0]._ok(self.inter(self.creator, self.channel)))
        self.assertEqual(bdb.get_bet(self.db, bet.id).status, "cancelled")
        self.assertEqual((self.bal(self.alice), self.bal(self.bob), self.bal(self.carol)), (5000, 15000, 80000))

    def test_panel_survives_deleted_message_and_restart(self):
        bet = self.full_example()
        old = next(m for m in self.channel.sent if m.id == bet.message_id)
        old.deleted = True
        run(self.cog.sync_panel(bet.id))
        fresh = bdb.get_bet(self.db, bet.id)
        self.assertNotEqual(fresh.message_id, old.id)
        self.assertEqual(fresh.pool, 100000)
        # "reinicio"
        bot2 = discord.ext.commands.Bot()
        bot2.channels = {PANEL_CH: self.channel}
        before = len(self.channel.sent)
        cog2 = run(betsm.setup_bets(bot2, self.db, GUILD_ID))
        run(cog2.ticker.fn(cog2))
        self.assertEqual(len(self.channel.sent), before)                     # no duplica el panel
        self.assertEqual(bdb.get_bet(self.db, bet.id).pool, 100000)

    def test_panel_with_many_participants_respects_embed_limits(self):
        bet, _ = self.create(opciones=", ".join(f"Opción larga número {i}" for i in range(10)))
        users = []
        for n in range(150):
            uid = 5000 + n
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=10 ** 6, request_key=f"m{n}")
            for k in range(3):
                bdb.place_wager(self.db, bet.id, uid, f"Opción larga número {(n + k) % 10}", 1000 + n)
            users.append(uid)
        full = bdb.get_bet(self.db, bet.id)
        emb = betsm.build_panel_embed(full, self.cog.money)
        self.assertLessEqual(len(emb), 6000)
        for _, value, _ in emb.fields:
            self.assertLessEqual(len(value), 1024)
        self.assertIn("Ver detalle", "".join(v for _, v, _ in emb.fields))
        # detalle completo y paginado
        msg = next(m for m in self.channel.sent if m.id == bet.message_id)
        i = self.inter(self.alice, message=msg)
        run(self.cog.show_detail(i))
        view = i.views[0]
        self.assertGreater(len(view.pages), 1)
        text = "".join(view.pages)
        for uid in users:
            self.assertIn(f"<@{uid}>", text)
        for page in view.pages:
            self.assertLessEqual(len(page), 4096)

    def test_data_survives_restart_open_bet(self):
        self.fund(self.alice, 1000)
        bet, _ = self.create()
        self.bet_cmd(bet, self.alice, "Empate 300")
        bot2 = discord.ext.commands.Bot()
        bot2.channels = {PANEL_CH: self.channel}
        run(betsm.setup_bets(bot2, self.db, GUILD_ID))
        again = bdb.get_bet(self.db, bet.id)
        self.assertEqual((again.status, again.pool, self.bal(self.alice)), ("open", 300, 700))

    # ---------- historial ----------
    def test_verhistorial_own_goes_to_dm_and_confirms_ephemeral(self):
        self.fund(self.alice, 500)
        i = self.inter(self.alice)
        run(self.ecog.verhistorial.callback(self.ecog, i, None))
        self.assertEqual(len(self.alice.dms), 1)
        self.assertIn("500", self.alice.dms[0].description)
        self.assertIn("Agregado", self.alice.dms[0].description)
        self.assertIn("Te envié", i.text)
        self.assertTrue(i.ephemeral_all)

    def test_verhistorial_blocked_dms_is_reported_without_crash(self):
        self.fund(self.alice, 500)
        self.alice.block_dms = True
        i = self.inter(self.alice)
        run(self.ecog.verhistorial.callback(self.ecog, i, None))
        self.assertIn("No pude enviarte", i.text)
        self.assertTrue(i.ephemeral_all)

    def test_verhistorial_empty(self):
        i = self.inter(self.alice)
        run(self.ecog.verhistorial.callback(self.ecog, i, None))
        self.assertIn("Todavía no tenés", i.text)
        self.assertEqual(self.alice.dms, [])

    def test_verhistorial_staff_other_user_is_ephemeral_and_no_dm(self):
        self.fund(self.alice, 500)
        i = self.inter(self.staff)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice))
        self.assertTrue(i.ephemeral_all)
        self.assertEqual(len(i.embeds), 1)
        self.assertEqual(self.alice.dms, [])
        self.assertEqual(self.staff.dms, [])
        i = self.inter(self.admin)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.carol))
        self.assertIn("no tiene movimientos", i.text)

    def test_verhistorial_other_user_denied_for_non_staff(self):
        self.fund(self.alice, 500)
        i = self.inter(self.bob)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice))
        self.assertIn("Solo el staff", i.text)
        self.assertEqual(i.embeds, [])
        # un rol otorgado con /eco perms-comando también cuenta como staff
        eco.grant_command_role(self.db, "verhistorial", MEMBER_ROLE)
        i = self.inter(self.bob)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice))
        self.assertEqual(len(i.embeds), 1)

    def test_history_shows_20_latest_most_recent_first(self):
        for n in range(25):
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[self.alice.id], amount=10, request_key=f"x{n}", reason=f"motivo {n}")
        i = self.inter(self.staff)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice))
        lines = i.embeds[0].description.split("\n")
        self.assertEqual(len(lines), 20)
        self.assertIn("motivo 24", lines[0])
        self.assertIn("motivo 5", lines[-1])

    # ---------- motivos en comandos de economía ----------
    def econ_env(self):
        eco.set_command_channel(self.db, "bal", PANEL_CH)
        eco.grant_command_role(self.db, "addmoney", STAFF_ROLE)
        eco.grant_command_role(self.db, "addmoney-role", STAFF_ROLE)
        eco.grant_command_role(self.db, "remove-money", STAFF_ROLE)

    def test_economy_commands_with_and_without_reason(self):
        self.econ_env()
        self.say("addmoney", self.staff, self.channel, {"args": f"{self.alice.mention} 5000 recompensa evento"})
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].reason, "recompensa evento")
        self.say("addmoney", self.staff, self.channel, {"args": f"{self.alice.mention} 100"})
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].reason, "Sin motivo especificado")
        self.say("remove-money", self.staff, self.channel, {"args": f"{self.alice.mention} 500"})
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].reason, "cobro el bal")
        self.say("remove-money", self.staff, self.channel, {"args": f"{self.alice.mention} 50 multa por faltar"})
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].reason, "multa por faltar")
        self.assertEqual(self.bal(self.alice), 5000 + 100 - 500 - 50)
        # formato anterior sigue funcionando, con puntos en la cantidad
        self.say("addmoney", self.staff, self.channel, {"args": f"{self.alice.mention} 1.500"})
        self.assertEqual(self.bal(self.alice), 4550 + 1500)

    def test_role_operation_reason_and_common_id(self):
        self.econ_env()
        ctx = self.say("addmoney-role", self.staff, self.channel, {"args": f"{self.r_target.mention} 1000 compensacion"})
        ha, hb = eco.get_history(self.db, self.alice.id)[0], eco.get_history(self.db, self.bob.id)[0]
        self.assertEqual((ha.reason, hb.reason), ("compensacion", "compensacion"))
        self.assertEqual(ha.related_id, hb.related_id)
        self.assertEqual(ha.mov_type, "addmoney-role")
        self.assertEqual(eco.get_history(self.db, self.carol.id), [])      # no tocó a quien no corresponde
        self.assertIn("compensacion", "".join(f.value if hasattr(f, "value") else f[1] for e in ctx.embeds for f in e.fields))
        self.say("remove-money", self.staff, self.channel, {"args": f"{self.r_target.mention} 200"})
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].reason, "cobro el bal")
        self.assertEqual(self.bal(self.alice), 800)

    def test_bad_amount_with_reason_changes_nothing(self):
        self.econ_env()
        ctx = self.say("addmoney", self.staff, self.channel, {"args": f"{self.alice.mention} 1.5 motivo"})
        self.assertIn("❌", ctx.text)
        self.assertEqual(self.bal(self.alice), 0)

    def test_bet_movements_visible_in_history(self):
        bet = self.full_example()
        run(self.cog.cerrarapuesta.callback(self.cog, self.inter(self.creator, self.thread_of(bet)), None))
        i = self.inter(self.creator, self.thread_of(bet))
        run(self.cog.resolverapuesta.callback(self.cog, i, "Gana Smogg", None))
        run(i.views[0]._ok(self.inter(self.creator, self.thread_of(bet))))
        types = [r.mov_type for r in eco.get_history(self.db, self.alice.id)]
        self.assertEqual(types[:2], ["bet_payout", "bet_stake"])

    # ---------- balance histórico ----------
    def test_lifetime_balance_only_adds_and_shows_in_bal(self):
        self.econ_env()
        eco.grant_command_role(self.db, "bal", MEMBER_ROLE)
        self.say("addmoney", self.staff, self.channel, {"args": f"{self.alice.mention} 5000"})
        self.say("addmoney-role", self.staff, self.channel, {"args": f"{self.r_target.mention} 1000"})   # alice y bob
        self.say("remove-money", self.staff, self.channel, {"args": f"{self.alice.mention} 4000"})
        self.assertEqual(self.bal(self.alice), 2000)
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 6000)       # el remove no resta
        # apostar no lo toca; los premios tampoco cuentan como depósito
        bet, _ = self.create()
        self.bet_cmd(bet, self.alice, "Gana Smogg 1000")
        run(self.cog.cerrarapuesta.callback(self.cog, self.inter(self.creator, self.thread_of(bet)), None))
        i = self.inter(self.creator, self.thread_of(bet))
        run(self.cog.resolverapuesta.callback(self.cog, i, "Gana Smogg", None))
        run(i.views[0]._ok(self.inter(self.creator, self.thread_of(bet))))
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 6000)
        ctx = self.say("bal", self.alice, self.channel, {"objetivo": ""})
        values = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("6,000", values["📈 Balance histórico"])
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.carol.id), 0)

    def test_lifetime_counts_import_increases_only(self):
        self.fund(self.alice, 100)
        i = self.run_import(self.admin, "Alice;1000\nBob;50")
        run(i.views[0]._ok(self.inter(self.admin)))
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 100 + 900)
        i = self.run_import(self.admin, "Alice;10")                                     # bajar el saldo no resta
        run(i.views[0]._ok(self.inter(self.admin)))
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 1000)
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.bob.id), 50)

    # ---------- salida del servidor / set-balance / reset histórico / stats ----------
    def test_historico_reset_requires_admin_confirmation_and_keeps_balances(self):
        self.fund(self.alice, 5000)
        self.fund(self.bob, 700)
        i = self.inter(self.staff)
        run(self.ecog.historico_reset.callback(self.ecog, i))
        self.assertIn("solo para el propietario", i.text)
        i = self.inter(self.admin)
        run(self.ecog.historico_reset.callback(self.ecog, i))
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 5000)       # falta confirmar
        view = i.views[0]
        j = self.inter(self.admin)
        run(view._ok(j))
        self.assertEqual((eco.get_lifetime_deposits(self.db, self.alice.id), eco.get_lifetime_deposits(self.db, self.bob.id)), (0, 0))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (5000, 700))        # balances intactos
        self.assertEqual(len(eco.get_history(self.db, self.alice.id)), 1)                # historial intacto
        run(view._ok(self.inter(self.admin)))                                            # doble clic
        self.fund(self.alice, 100)                                                       # lo nuevo sí cuenta
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 100)
        self.assertEqual(eco.get_stats(self.db).lifetime_total, 100)
        self.assertFalse(run(view.interaction_check(self.inter(self.staff))))

    def test_eco_stats_staff_only(self):
        self.fund(self.alice, 5000)
        self.fund(self.bob, 1000)
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.carol.id], amount=300, request_key="n1")
        for user in (self.alice, self.plain):
            i = self.inter(user)
            run(self.ecog.eco_stats.callback(self.ecog, i))
            self.assertIn("solo para el staff", i.text)
            self.assertEqual(i.embeds, [])
        for user in (self.staff, self.admin):
            i = self.inter(user)
            run(self.ecog.eco_stats.callback(self.ecog, i))
            self.assertTrue(i.ephemeral_all)
            fields = {n: v for n, v, _ in i.embeds[0].fields}
            self.assertIn("5,700", fields["Total cargado (suma de todos los balances)"])
            self.assertIn("6,000", fields["Balances positivos"])
            self.assertIn("-300", fields["Balances negativos"])

    def test_bal_title_is_just_wuju_coins(self):
        eco.set_command_channel(self.db, "bal", PANEL_CH)
        eco.grant_command_role(self.db, "bal", MEMBER_ROLE)
        ctx = self.say("bal", self.alice, self.channel, {"objetivo": ""})
        self.assertEqual(ctx.embeds[0].title, "Wuju Coins")

    # ---------- importación única de saldos ----------
    def test_parse_import_text_formats(self):
        text = "Alice;5000\nBob,1.500\nCarol 2,500\n# comentario\n\nJuan Perez 300\nmal;abc\nsolo\nNegativo;-5\nCero;0\n"
        entries, errors = eco.parse_import_text(text)
        self.assertEqual([(n, a) for _, n, a in entries],
                         [("Alice", 5000), ("Bob", 1500), ("Carol", 2500), ("Juan Perez", 300), ("Negativo", -5), ("Cero", 0)])
        self.assertEqual(len(errors), 2)

    class FakeAttachment(discord.Attachment):
        def __init__(self, text, size=None):
            self.data = text.encode("utf-8")
            self.size = size if size is not None else len(self.data)

        async def read(self):
            return self.data

    def run_import(self, user, text):
        i = self.inter(user)
        run(self.ecog.importar_bal.callback(self.ecog, i, self.FakeAttachment(text)))
        return i

    def test_import_requires_admin_and_changes_nothing_without_confirm(self):
        i = self.run_import(self.staff, "Alice;100")
        self.assertIn("propietario", i.text)
        i = self.run_import(self.admin, "Alice;100\nBob;200\nNadie;50")
        self.assertIn("Vista previa", i.text)
        self.assertIn("NO se cargarán", i.text)
        self.assertIn("Nadie", i.text)
        self.assertEqual(self.bal(self.alice), 0)

    def test_import_applies_once_with_reason_and_ignores_double_click(self):
        self.fund(self.alice, 300)
        self.fund(self.bob, 9000)
        i = self.run_import(self.admin, "alice ;1.000\nBOB;2000\nNadie;5")
        view = i.views[0]
        j = self.inter(self.admin)
        run(view._ok(j))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (1000, 2000))      # SETEA, no suma
        row = eco.get_history(self.db, self.alice.id)[0]
        self.assertEqual((row.reason, row.mov_type, row.delta, row.balance_after), ("Importación inicial de saldos", "balance_set", 700, 1000))
        self.assertEqual(eco.get_history(self.db, self.bob.id)[0].delta, -7000)
        run(view._ok(self.inter(self.admin)))                               # doble clic
        self.assertEqual(self.bal(self.alice), 1000)
        # repetir el comando con el mismo archivo no cambia nada (setea el mismo valor)
        i2 = self.run_import(self.admin, "alice;1000\nbob;2000")
        j2 = self.inter(self.admin)
        run(i2.views[0]._ok(j2))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (1000, 2000))
        self.assertIn("ya estaban en ese valor", j2.text)

    def test_import_uses_discord_names_not_server_nicknames(self):
        self.carol.nick = "ApodoDelServer"
        i = self.run_import(self.admin, "ApodoDelServer;100\nCarol;100")
        self.assertIn("1 no encontrado(s)", i.text)
        self.assertIn("ApodoDelServer", i.text)
        run(i.views[0]._ok(self.inter(self.admin)))
        self.assertEqual(self.bal(self.carol), 100)             # por su nombre de usuario "Carol"
        self.assertEqual(eco.get_history(self.db, self.carol.id)[0].mov_type, "balance_set")

    def test_import_can_set_zero_and_negative(self):
        self.fund(self.alice, 500)
        i = self.run_import(self.admin, "Alice;0\nBob;-250")
        run(i.views[0]._ok(self.inter(self.admin)))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (0, -250))
        self.assertEqual(self.logch.sent, [])                               # sin spam en el registro público

    def test_import_ambiguous_and_duplicate_names_are_skipped(self):
        twin = discord.Member(900000000000000001, "Alice", roles=[], guild=self.guild)
        self.guild.members.append(twin)
        i = self.run_import(self.admin, "Alice;100\nBob;5\nbob;7")
        self.assertIn("más de una coincidencia", i.text)
        self.assertIn("formato inválido o repetidas", i.text)
        run(i.views[0]._ok(self.inter(self.admin)))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (0, 5))

    def test_import_returns_file_with_all_not_found_people(self):
        names = [f"Fantasma{n}" for n in range(40)]
        text = "Alice;100\n" + "\n".join(f"{n};{1000 + k}" for k, n in enumerate(names)) + "\nroto\n"
        i = self.run_import(self.admin, text)
        files = [kw["file"] for _, kw in i.response.sent if kw.get("file")]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "no_encontrados.txt")
        content = files[0].read_text()
        for k, n in enumerate(names):
            self.assertIn(f"{n};{1000 + k}", content)                  # TODAS, en formato reutilizable
        self.assertIn("NO encontradas: 40", content)
        self.assertIn("40 no encontrado(s)", i.text)
        self.assertLessEqual(len(i.text), 2000)
        # el archivo devuelto se puede volver a importar tal cual (los # se ignoran)
        entries, errors = eco.parse_import_text(content)
        self.assertEqual(len(entries), 40)
        self.assertEqual(errors, [])

    def test_import_without_problems_has_no_file(self):
        i = self.run_import(self.admin, "Alice;100")
        self.assertEqual([kw for _, kw in i.response.sent if kw.get("file")], [])

    def test_import_all_not_found_still_returns_file(self):
        i = self.run_import(self.admin, "Nadie1;5\nNadie2;6")
        self.assertIn("No pude vincular", i.text)
        self.assertEqual(len([kw for _, kw in i.response.sent if kw.get("file")]), 1)

    def test_import_other_admin_cannot_press_confirm(self):
        i = self.run_import(self.admin, "Alice;100")
        view = i.views[0]
        self.assertFalse(run(view.interaction_check(self.inter(self.staff))))
        self.assertEqual(self.bal(self.alice), 0)


if __name__ == "__main__":
    unittest.main()
