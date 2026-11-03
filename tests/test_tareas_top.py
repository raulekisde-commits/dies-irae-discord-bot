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


class CancelarTareas(TopTareas):
    def setUp(self):
        super().setUp()
        self.log, self.deleted = [], []
        outer = self

        class Msg:
            def __init__(s, mid): s.id = mid
            async def delete(s): outer.deleted.append(s.id)

        class Ch:
            id = 1
            async def send(s, *a, **kw): outer.log.append(a[0] if a else kw.get("content"))
            async def fetch_message(s, mid): return Msg(mid)
        self.cog._channel = lambda cid: _async(Ch())

    def mk_src(self, text, source, mid):
        t = self.mk(text)
        from dbutil import connect
        c = connect(self.db); c.execute("UPDATE tasks SET source=?, message_id=? WHERE id=?", (source, mid, t.id)); c.close()
        return t

    def test_cancel_marks_cancelled_and_does_not_count(self):
        t = self.mk("x")
        edb.take(self.db, t.id, self.stf.id)
        res = edb.cancel(self.db, t.id, self.stf2.id)
        self.assertEqual((res.status, res.task.status), ("ok", "cancelled"))
        self.assertEqual(edb.cancel(self.db, t.id, self.stf2.id).status, "closed")           # ya cancelada
        self.assertEqual(edb.finish(self.db, t.id, self.stf.id, True).status, "notopen")      # y ya no se puede terminar
        self.assertEqual(edb.top_users(self.db)[1], [])
        self.assertEqual(edb.due_reminders(self.db, now=10 ** 12), [])                       # sin avisos horarios
        d = self.mk("terminada"); edb.finish(self.db, d.id, self.stf.id, True)
        self.assertEqual(edb.cancel(self.db, d.id, self.stf.id).status, "closed")           # una terminada no se cancela
        self.assertEqual(edb.cancel(self.db, 9999, 1).status, "notfound")

    def test_prefix_does_not_mix_ctas(self):
        a = self.mk_src("a", "cta:1:killboard", 11)
        b = self.mk_src("b", "cta:12:killboard", 12)
        self.assertEqual(edb.open_by_source_prefix(self.db, "cta:1:"), [a.id])
        self.assertEqual(edb.open_by_source_prefix(self.db, "cta:12:"), [b.id])

    def test_cancel_by_prefix_deletes_panels_and_logs(self):
        a = self.mk_src("killboard", "cta:7:killboard", 71)
        b = self.mk_src("conectados", "cta:7:conectados", 72)
        c = self.mk_src("otra cta", "cta:8:killboard", 81)
        n = run(self.cog.cancel_by_prefix("cta:7:", self.stf.id, "cancelada porque se canceló la CTA #7"))
        self.assertEqual(n, 2)
        self.assertEqual(sorted(self.deleted), [71, 72])
        self.assertEqual(len(self.log), 2)
        self.assertEqual(edb.get_task(self.db, c.id).status, "open")
        self.assertEqual(run(self.cog.cancel_by_prefix("cta:7:", self.stf.id, "x")), 0)       # idempotente
        self.assertFalse(run(self.cog.complete_by_source("cta:7:killboard", 1)))               # ya no se puede terminar sola

    def test_cancel_button_permissions_and_confirmation(self):
        t = self.mk_src("hacer algo", None, 91)
        msg = type("M", (), {"id": 91})()
        i = self.inter(self.alice, message=msg)
        run(self.cog.on_cancel(i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.stf, message=msg)
        run(self.cog.on_cancel(i))
        view = i.response.sent[0][1]["view"]
        self.assertEqual(edb.get_task(self.db, t.id).status, "open")                           # todavía no se canceló
        run(view._ok(self.inter(self.stf)))
        self.assertEqual(edb.get_task(self.db, t.id).status, "cancelled")
        self.assertEqual(self.deleted, [91])

    def test_view_has_cancel_button(self):
        self.assertIn("task:cancel", [c.custom_id for c in enc.TaskView(self.cog).children])


def _async(v):
    async def f(): return v
    return f()


if __name__ == "__main__":
    unittest.main()
