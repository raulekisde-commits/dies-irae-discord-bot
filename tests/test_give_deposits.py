"""Pruebas de /give-money (transferencias).

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban que la API real de Discord acepte
los permisos de canal, los modales ni los mensajes.
"""
import asyncio
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import economy as economy  # noqa: E402
import economy_db as eco  # noqa: E402
from guildshop import COUNCIL_ROLE_ID  # noqa: E402

GUILD_ID = 2000000000000000001
MEMBER_ROLE = 1000000000000000001
STAFF_ROLE = 1000000000000000002
PANEL_CH = 3000000000000000001
LOG_CH = 3000000000000000002

_LOOP = asyncio.new_event_loop()


def run(coro):
    result = _LOOP.run_until_complete(coro)
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return result



# ============================================================
# Lógica de transferencia
# ============================================================
class TransferDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        eco.init_schema(self.db)
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[1], amount=1000, request_key="f1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_transfer_moves_money_and_records_both_sides(self):
        r = eco.transfer(self.db, from_id=1, to_id=2, amount=400, request_key="g1", reason="por el café")
        self.assertEqual(r, ("applied", 600, 400))
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 2)), (600, 400))
        out, inn = eco.get_history(self.db, 1)[0], eco.get_history(self.db, 2)[0]
        self.assertEqual((out.mov_type, out.delta, out.reason), ("transfer_out", -400, "por el café"))
        self.assertEqual((inn.mov_type, inn.delta), ("transfer_in", 400))
        self.assertEqual(eco.get_lifetime_deposits(self.db, 2), 0)          # recibir una transferencia no es un depósito

    def test_insufficient_funds_changes_nothing(self):
        with self.assertRaises(eco.InsufficientFunds):
            eco.transfer(self.db, from_id=1, to_id=2, amount=1001, request_key="g1")
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 2)), (1000, 0))
        self.assertEqual(len(eco.get_history(self.db, 2)), 0)

    def test_same_request_is_not_applied_twice(self):
        eco.transfer(self.db, from_id=1, to_id=2, amount=100, request_key="g1")
        self.assertEqual(eco.transfer(self.db, from_id=1, to_id=2, amount=100, request_key="g1")[0], "repeated")
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 2)), (900, 100))

    def test_invalid_transfers(self):
        for kw in (dict(from_id=1, to_id=1, amount=10), dict(from_id=1, to_id=2, amount=0), dict(from_id=1, to_id=2, amount=-5)):
            with self.assertRaises(ValueError):
                eco.transfer(self.db, request_key="x", **kw)
        self.assertEqual(eco.get_balance(self.db, 1), 1000)

    def test_concurrent_transfers_never_overspend(self):
        results = []

        def go(i):
            try:
                results.append(eco.transfer(self.db, from_id=1, to_id=2, amount=300, request_key=f"c{i}")[0])
            except eco.InsufficientFunds:
                results.append("funds")

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count("applied"), 3)                         # 1000 alcanza para 3 de 300
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 2)), (100, 900))


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent, self.edits, self.modals = [], [], []

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

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append((content, kw))


class FakeInteraction:
    _n = 0

    def __init__(self, guild, user, channel, message=None):
        FakeInteraction._n += 1
        self.id = 6000000000000000000 + FakeInteraction._n
        self.guild, self.user, self.channel, self.message = guild, user, channel, message
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    @property
    def channel_id(self):
        return getattr(self.channel, "id", None)

    @property
    def text(self):
        out = [str(c) for c, _ in self.response.sent if c]
        out += [str(e.get("content")) for e in self.response.edits if e.get("content")]
        return "\n".join(out)

    @property
    def modals(self):
        return self.response.modals


# ============================================================
# Discord
# ============================================================
class DiscordBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.bot.user = self.guild.me
        self.panelch = discord.TextChannel(PANEL_CH, self.guild, "depositos")
        self.logch = discord.TextChannel(LOG_CH, self.guild, "log")
        self.guild._channels = {PANEL_CH: self.panelch, LOG_CH: self.logch}
        self.bot.channels = dict(self.guild._channels)
        self.bot.guilds_map = {GUILD_ID: self.guild}
        self.r_council = discord.Role(COUNCIL_ROLE_ID, "Council", self.guild)
        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.guild.roles = [self.r_council, self.r_member]

        def mk(uid, name, roles=(), admin=False, bot=False):
            m = discord.Member(uid + 100000000000000000, name, roles=list(roles), guild=self.guild, administrator=admin, bot=bot)
            self.guild.members.append(m)
            return m

        self.admin = mk(10, "Admin", admin=True)
        self.alice = mk(20, "Alice", [self.r_member])
        self.bob = mk(21, "Bob", [self.r_member])
        self.council = mk(32, "Consejero", [self.r_council])
        self.outsider = mk(40, "Afuera")
        self.robot = mk(50, "Robot", [self.r_member], bot=True)
        run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE, STAFF_ROLE))
        self.ecog = self.bot.cogs["EconomyCog"]
        self.ecog.set_log_channel_for_tests = None

    def tearDown(self):
        self.tmp.cleanup()

    def inter(self, user, channel=None, message=None):
        return FakeInteraction(self.guild, user, channel or self.panelch, message)

    def fund(self, user, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[user.id], amount=amount, request_key=f"f{user.id}{os.urandom(3).hex()}")

    def bal(self, user):
        return eco.get_balance(self.db, user.id)


class GiveMoneyTests(DiscordBase):
    def give(self, giver, target, amount, reason=None):
        i = self.inter(giver)
        run(self.ecog.give_money.callback(self.ecog, i, target, amount, reason))
        return i

    def test_registered_and_grantable(self):
        self.assertEqual(economy.EconomyCog.give_money.name, "give-money")
        self.assertIn("give-money", eco.GRANTABLE_COMMANDS)

    def test_member_gives_coins_publicly_and_balances_move(self):
        self.fund(self.alice, 5000)
        i = self.give(self.alice, self.bob, "1.500", "gracias")
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (3500, 1500))
        content, kw = i.response.sent[0]
        self.assertFalse(kw.get("ephemeral"))                                  # lo ve todo el canal
        self.assertIn("1,500", content)
        self.assertIn("gracias", content)
        self.assertIn(str(self.bob.id), content)

    def test_insufficient_funds_is_private_and_moves_nothing(self):
        self.fund(self.alice, 100)
        i = self.give(self.alice, self.bob, "101")
        self.assertIn("saldo suficiente", i.text)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (100, 0))

    def test_invalid_cases(self):
        self.fund(self.alice, 1000)
        self.assertIn("a vos mismo", self.give(self.alice, self.alice, "10").text)
        self.assertIn("bot", self.give(self.alice, self.robot, "10").text)
        for bad in ("0", "-5", "abc", "1.5", ""):
            self.assertIn("❌", self.give(self.alice, self.bob, bad).text, bad)
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (1000, 0))

    def test_people_without_member_role_cannot_use_it_until_granted(self):
        self.fund(self.outsider, 1000)
        self.assertIn("permiso", self.give(self.outsider, self.bob, "10").text)
        self.assertEqual(self.bal(self.bob), 0)
        eco.grant_command_role(self.db, "give-money", COUNCIL_ROLE_ID)
        self.fund(self.council, 50)
        self.give(self.council, self.bob, "10")
        self.assertEqual(self.bal(self.bob), 10)

    def test_history_shows_both_sides_and_lifetime_is_untouched(self):
        self.fund(self.alice, 2000)
        self.give(self.alice, self.bob, "700")
        self.assertEqual(eco.get_history(self.db, self.alice.id)[0].label, "Transferencia enviada")
        self.assertEqual(eco.get_history(self.db, self.bob.id)[0].label, "Transferencia recibida")
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.bob.id), 0)

    def test_audit_note_goes_to_the_log_channel_when_configured(self):
        run(self.ecog._db(eco.set_setting, "log_channel_id", str(LOG_CH)))
        self.fund(self.alice, 2000)
        self.give(self.alice, self.bob, "700")
        self.assertTrue(any("Transferencia" in (m.embeds[0].title or "") for m in self.logch.sent if m.embeds))


if __name__ == "__main__":
    unittest.main()
