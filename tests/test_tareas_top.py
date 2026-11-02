import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import encargos as enc  # noqa: E402
import encargos_db as edb  # noqa: E402


class TopTareas(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(enc.STAFF_ROLE_ID, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(9001, "Stf", roles=[self.r_staff], guild=self.guild)
        self.stf2 = discord.Member(9002, "Stf2", roles=[self.r_staff], guild=self.guild)
        self.guild.members += [self.stf, self.stf2]
        self.cog = run(enc.setup_encargos(self.bot, self.db, GUILD_ID))

    def mk(self, text):
        t = edb.create_task(self.db, guild_id=GUILD_ID, channel_id=1, text=text, created_by=1)
        from dbutil import connect
        c = connect(self.db); c.execute("UPDATE tasks SET status='open' WHERE id=?", (t.id,)); c.close()
        return t

    def test_top(self):
        for i in range(3):
            t = self.mk(f"t{i}")
            edb.take(self.db, t.id, self.stf.id)
            edb.finish(self.db, t.id, self.stf.id, False)
        t = self.mk("x")
        edb.take(self.db, t.id, self.stf2.id)
        edb.release(self.db, t.id, self.stf2.id)
        edb.take(self.db, t.id, self.stf2.id)
        t2 = self.mk("y")
        edb.finish(self.db, t2.id, self.stf2.id, True)
        taken, done = edb.top_users(self.db)
        self.assertEqual(taken[0], (self.stf.id, 3))
        self.assertEqual(dict(taken)[self.stf2.id], 2)               # cuenta cada vez que la tomó
        self.assertEqual(done[0], (self.stf.id, 3))
        self.assertEqual(dict(done)[self.stf2.id], 1)

    def test_command_permissions(self):
        i = self.inter(self.alice)
        run(self.cog.send_top(i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.stf)
        run(self.cog.send_top(i))
        self.assertTrue(i.response.sent[0][1].get("embed") is not None)

    def test_done_logs_and_deletes_panel(self):
        sent = []
        class Ch:
            id = enc.TASK_LOG_CHANNEL_ID
            async def send(self, **kw): sent.append(kw)
        class Msg:
            id = 55; deleted = False
            async def delete(self): Msg.deleted = True
        self.cog._channel = lambda cid: _async(Ch())
        t = self.mk("hacer algo")
        edb.take(self.db, t.id, self.stf.id)
        i = self.inter(self.stf); i.message = Msg()
        res = edb.finish(self.db, t.id, self.stf.id, False)
        run(self.cog._after(i, res, "ok"))
        self.assertTrue(Msg.deleted)
        self.assertEqual(len(sent), 1)
        self.assertIn("terminada", sent[0]["content"])

    def test_done_keeps_panel_if_log_fails(self):
        self.cog._channel = lambda cid: _async(None)
        t = self.mk("otra")
        res = edb.finish(self.db, t.id, self.stf.id, True)
        self.assertFalse(run(self.cog.log_done(res.task)))


def _async(v):
    async def f(): return v
    return f()


if __name__ == "__main__":
    unittest.main()
