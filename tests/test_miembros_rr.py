import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import miembros as mb  # noqa: E402
import reactionroles as rr  # noqa: E402
import registration_db as rdb  # noqa: E402
from guildshop import COUNCIL_ROLE_ID  # noqa: E402


class FakeChan:
    def __init__(self, cid):
        self.id, self.sent = cid, []
        self.mention = f"<#{cid}>"
    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class Miembros(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_council = discord.Role(COUNCIL_ROLE_ID, "Council", self.guild)
        self.guild.roles.append(self.r_council)
        self.cou = discord.Member(7001, "Cou", roles=[self.r_council], guild=self.guild)
        self.guild.members.append(self.cou)
        rdb.init_schema(self.db)
        self.cog = run(mb.setup_miembros(self.bot, self.db, GUILD_ID))
        self.chan = FakeChan(555)
        self.guild._channels[555] = self.chan

    def test_config_permissions_and_toggle(self):
        i = self.inter(self.alice)
        run(self.cog.canal_entradas.callback(self.cog, i, self.chan, False))
        self.assertIn("Solo el Council", i.text)
        self.assertIsNone(mb.get_channel_id(self.db, GUILD_ID, "join"))
        i = self.inter(self.cou)
        run(self.cog.canal_entradas.callback(self.cog, i, self.chan, False))
        self.assertEqual(mb.get_channel_id(self.db, GUILD_ID, "join"), 555)
        self.assertIsNone(mb.get_channel_id(self.db, GUILD_ID, "leave"))
        run(self.cog.canal_salidas.callback(self.cog, self.inter(self.cou), self.chan, False))
        run(self.cog.canal_entradas.callback(self.cog, self.inter(self.cou), None, True))
        self.assertIsNone(mb.get_channel_id(self.db, GUILD_ID, "join"))
        self.assertEqual(mb.get_channel_id(self.db, GUILD_ID, "leave"), 555)

    def test_join_only_mentions(self):
        run(self.cog.on_member_join(self.bob))                          # sin canal: no hace nada
        self.assertEqual(self.chan.sent, [])
        mb.set_channel_id(self.db, GUILD_ID, "join", 555)
        run(self.cog.on_member_join(self.bob))
        content, kw = self.chan.sent[0]
        self.assertEqual(content, f"<@{self.bob.id}> entró al servidor")
        self.assertNotIn("embed", kw)
        bot_m = discord.Member(9, "Bot", guild=self.guild, bot=True)
        run(self.cog.on_member_join(bot_m))
        self.assertEqual(len(self.chan.sent), 1)

    def test_leave_shows_roles_and_albion(self):
        mb.set_channel_id(self.db, GUILD_ID, "leave", 555)
        r1, r2 = discord.Role(31, "A", self.guild), discord.Role(32, "B", self.guild)
        m = discord.Member(8800, "Fulano", roles=[r1, r2], guild=self.guild)
        rdb.admin_link(self.db, m.id, "FulanoAlbion", 1)
        regs = rdb.get_registrations(self.db, m.id)
        self.assertTrue(run(self.cog.post_leave(m, regs)))
        emb = self.chan.sent[0][1]["embed"]
        text = " ".join(str(f) for f in emb.fields)
        self.assertIn("<@&31>", text); self.assertIn("<@&32>", text); self.assertIn("FulanoAlbion", text)
        m2 = discord.Member(8801, "SinReg", roles=[], guild=self.guild)
        run(self.cog.post_leave(m2, []))
        text2 = " ".join(str(f) for f in self.chan.sent[1][1]["embed"].fields)
        self.assertIn("No estaba registrado", text2); self.assertIn("Sin roles", text2)


class ReactionRoles(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(rr.setup_reactionroles(self.bot, self.db, GUILD_ID))
        self.role = discord.Role(901, "Rol", self.guild)
        self.guild.roles.append(self.role)
        self.guild.me = discord.Member(1, "bot", roles=[], guild=self.guild)
        self.cog.bot.user = self.guild.me
        self._orig = rr.role_problem
        rr.role_problem = lambda g, r: None

    def tearDown(self):
        rr.role_problem = self._orig

    def pay(self, user, emoji="✅", mid=700):
        return discord.RawReactionActionEvent(GUILD_ID, mid, user.id, discord.PartialEmoji.from_str(emoji))

    def test_add_remove_and_noop_if_already_has(self):
        pid = rr.create_panel(self.db, GUILD_ID, "Juegos", 5, 700, 1)
        self.assertEqual(rr.add_role(self.db, pid, "✅", self.role.id), "ok")
        self.assertEqual(rr.add_role(self.db, pid, "✅", 902), "emoji")
        self.assertEqual(rr.add_role(self.db, pid, "🎮", self.role.id), "role")
        self.guild.members += [m for m in (self.alice,) if m not in self.guild.members]
        calls = []
        orig_add = self.alice.add_roles
        async def spy(*a, **k):
            calls.append(a); return await orig_add(*a, **k)
        self.alice.add_roles = spy
        run(self.cog._handle(self.pay(self.alice), True))
        self.assertIn(self.role, self.alice.roles); self.assertEqual(len(calls), 1)
        run(self.cog._handle(self.pay(self.alice), True))                          # ya lo tiene: no hace nada
        self.assertEqual(len(calls), 1)
        run(self.cog._handle(self.pay(self.alice, "🎮"), True))                    # emoji que no es del panel
        run(self.cog._handle(self.pay(self.alice, mid=999), True))                 # otro mensaje
        self.assertEqual(len(calls), 1)
        run(self.cog._handle(self.pay(self.alice), False))
        self.assertNotIn(self.role, self.alice.roles)
        run(self.cog._handle(self.pay(self.alice), False))                          # ya no lo tiene: no falla
        self.assertNotIn(self.role, self.alice.roles)

    def test_bot_reaction_ignored_and_panel_deleted_with_message(self):
        pid = rr.create_panel(self.db, GUILD_ID, "X", 5, 701, 1)
        rr.add_role(self.db, pid, "✅", self.role.id)
        run(self.cog._handle(discord.RawReactionActionEvent(GUILD_ID, 701, self.guild.me.id, discord.PartialEmoji.from_str("✅")), True))
        self.assertNotIn(self.role, self.guild.me.roles)
        run(self.cog.on_raw_message_delete(discord.RawMessageDeleteEvent(701)))
        self.assertIsNone(rr.get_by_message(self.db, 701))

    def test_limits_and_remove(self):
        pid = rr.create_panel(self.db, GUILD_ID, "L", 5, 702, 1)
        for i in range(rr.MAX_ROLES_PER_PANEL):
            self.assertEqual(rr.add_role(self.db, pid, f"e{i}", 1000 + i), "ok")
        self.assertEqual(rr.add_role(self.db, pid, "zz", 5000), "full")
        self.assertEqual(rr.remove_role(self.db, pid, 1000), "e0")
        self.assertIsNone(rr.remove_role(self.db, pid, 1000))
        self.assertTrue(rr.name_taken(self.db, GUILD_ID, "l"))

    def test_role_problem_blocks_dangerous(self):
        class P:  # permisos falsos
            def __init__(self, **k): self.__dict__.update(k)
        class R:
            def __init__(self, perms, managed=False, default=False, top=False):
                self.permissions, self.managed, self._d, self._top = P(**perms), managed, default, top
            def is_default(self): return self._d
            def __ge__(self, other): return self._top
        class G:
            me = type("M", (), {"top_role": object()})()
        rp = self._orig
        self.assertIn("@everyone", rp(G, R({}, default=True)))
        self.assertIn("integración", rp(G, R({}, managed=True)))
        self.assertIn("moderación", rp(G, R({"administrator": True})))
        self.assertIn("por encima", rp(G, R({}, top=True)))
        self.assertIsNone(rp(G, R({})))

    def test_commands_only_council(self):
        i = self.inter(self.alice)
        run(self.cog.crear.callback(self.cog, i, "Algo"))
        self.assertIn("Solo el Council", i.text)


if __name__ == "__main__":
    unittest.main()
