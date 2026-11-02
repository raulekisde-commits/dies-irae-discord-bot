import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, STAFF_ROLE, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import sorteos as sg  # noqa: E402
import sorteos_db as sdb  # noqa: E402

NOW = datetime.now(timezone.utc)


class SorteoBase(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(STAFF_ROLE, "Staff", self.guild)
        self.staff = discord.Member(300000000000000001, "Staffy", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.staff)
        self.cog = run(sg.setup_sorteos(self.bot, self.db, GUILD_ID, STAFF_ROLE))

    def create(self, user=None, prize="Premio", tiempo="1:00", winners=1):
        i = self.inter(user or self.staff)
        run(self.cog.sorteo.callback(self.cog, i, prize, tiempo, winners))
        return i

    def msg(self):
        return [m for m in self.panelch.sent if m.embeds][-1]

    def join(self, user, message=None):
        i = self.inter(user, message=message or self.msg())
        run(self.cog.on_join(i))
        return i

    def past(self, mid):
        from dbutil import connect
        c = connect(self.db)
        c.execute("UPDATE giveaways SET end_at=? WHERE message_id=?", ((NOW - timedelta(minutes=20)).isoformat(), mid))
        c.commit()
        c.close()


class CreationTests(SorteoBase):
    def test_permissions(self):
        self.assertIn("Solo el **Staff**", self.create(self.alice).text)
        self.assertEqual(len([m for m in self.panelch.sent if m.embeds]), 0)
        self.assertIn("Sorteo creado", self.create(self.staff).text)
        self.assertIn("Sorteo creado", self.create(self.council).text)

    def test_bad_time(self):
        self.assertIn("Tiempo inválido", self.create(tiempo="abc").text)
        self.assertIn("mayor a", self.create(tiempo="0:00").text)


class JoinTests(SorteoBase):
    def test_member_required(self):
        self.create()
        self.assertIn("miembros", self.join(self.outsider).text)
        self.assertNotIn("quedaste anotado", self.join(self.outsider).text)
        self.assertIn("quedaste anotado", self.join(self.alice).text)
        self.assertIn("Ya estabas anotado", self.join(self.alice).text)
        self.assertEqual(sdb.count_entrants(self.db, self.msg().id), 1)

    def test_unregistered_blocked(self):
        class Gate:
            MESSAGE = "NO REGISTRADO"
            async def allowed(self, user, cmd):
                return False
        self.bot.registration_gate = Gate()
        self.create()
        self.assertIn("NO REGISTRADO", self.join(self.alice).text)
        self.assertEqual(sdb.count_entrants(self.db, self.msg().id), 0)

    def test_after_end_closed(self):
        self.create()
        m = self.msg()
        self.past(m.id)
        self.assertIn("ya terminó", self.join(self.alice, m).text)


class DrawTests(SorteoBase):
    def test_overdue_draw_and_announce_once(self):
        self.create(winners=1)
        m = self.msg()
        self.join(self.alice)
        self.join(self.bob)
        self.past(m.id)
        self.assertEqual(run(self.cog.process_due()), 1)
        texts = [x.content for x in self.panelch.sent if x.content]
        self.assertTrue(any("finalizado" in t for t in texts))
        self.assertIsNone(sdb.get(self.db, m.id))                 # terminado y limpiado
        self.assertEqual(run(self.cog.process_due()), 0)          # no se anuncia dos veces
        self.assertEqual(len([x for x in self.panelch.sent if x.content]), len(texts))

    def test_announce_failure_never_redraws(self):
        self.create(winners=1)
        m = self.msg()
        self.join(self.alice)
        self.join(self.bob)
        self.past(m.id)
        orig = self.panelch.send
        calls = {"n": 0}

        async def failing(*a, **k):
            if not k.get("embed"):
                calls["n"] += 1
                raise discord.HTTPException()
            return await orig(*a, **k)
        self.panelch.send = failing
        run(self.cog.process_due())
        g = sdb.get(self.db, m.id)
        self.assertEqual(g.state, "announcing")
        winners = list(g.winners)
        self.assertEqual(len(winners), 1)
        for _ in range(3):
            run(self.cog.process_due())
        self.assertEqual(sdb.get(self.db, m.id).winners, winners)  # mismo ganador
        self.panelch.send = orig
        self.assertEqual(run(self.cog.process_due()), 1)
        self.assertIsNone(sdb.get(self.db, m.id))
        self.assertIn(f"<@{winners[0]}>", [x.content for x in self.panelch.sent if x.content][-1])

    def test_insufficient_cancels(self):
        self.create(winners=3)
        m = self.msg()
        self.join(self.alice)
        self.past(m.id)
        run(self.cog.process_due())
        last = [x.content for x in self.panelch.sent if x.content][-1]
        self.assertIn("No se pudo sortear", last)
        self.assertNotIn("1. <@", last)

    def test_no_entrants(self):
        self.create()
        m = self.msg()
        self.past(m.id)
        run(self.cog.process_due())
        self.assertIn("cancelado", [x.content for x in self.panelch.sent if x.content][-1])

    def test_left_server_excluded_but_error_keeps(self):
        self.create(winners=1)
        m = self.msg()
        self.join(self.alice)
        self.join(self.bob)
        self.guild.members.remove(self.bob)
        self.past(m.id)
        orig = self.guild.fetch_member

        async def boom(uid):
            raise discord.HTTPException()
        self.guild.fetch_member = boom
        valid = run(self.cog.valid_entrants(self.guild, [self.alice.id, self.bob.id]))
        self.assertEqual(sorted(valid), sorted([self.alice.id, self.bob.id]))   # error pasajero: nadie se descarta
        self.guild.fetch_member = orig
        valid = run(self.cog.valid_entrants(self.guild, [self.alice.id, self.bob.id]))
        self.assertEqual(valid, [self.alice.id])                                   # NotFound: sí se descarta

    def test_restart_resets_drawing_claim(self):
        self.create()
        m = self.msg()
        self.past(m.id)
        self.assertTrue(sdb.claim_draw(self.db, m.id))
        self.assertFalse(sdb.claim_draw(self.db, m.id))            # solo uno puede reclamar
        sdb.init_schema(self.db)                                   # «reinicio» sin resultado guardado
        self.assertEqual(sdb.get(self.db, m.id).state, "open")
        self.assertEqual(run(self.cog.process_due()), 1)


if __name__ == "__main__":
    unittest.main()
