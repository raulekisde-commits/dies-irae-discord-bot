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

    def create(self, user=None, prize="Premio", tiempo="1:00", winners=1, rol=None, bal=None, anotar_rol=False, jugadores=None):
        i = self.inter(user or self.staff)
        run(self.cog.sorteo.callback(self.cog, i, prize, tiempo, winners, rol, bal, anotar_rol, None, jugadores))
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


class RolTests(SorteoBase):
    def setUp(self):
        super().setUp()
        self.r_vip = discord.Role(777000000000000001, "VIP", self.guild)
        self.guild.roles.append(self.r_vip)
        self.alice.roles.append(self.r_vip)

    def test_embed_y_guardado_con_rol(self):
        self.assertIn("Sorteo creado", self.create(rol=self.r_vip).text)
        m = self.msg()
        self.assertEqual(sdb.get(self.db, m.id).role_id, self.r_vip.id)
        self.assertTrue(any("rol" in str(f[0]).lower() for f in m.embeds[0].fields))
        self.assertIn("Solo con el rol", self.create(rol=self.r_vip).text)

    def test_sin_el_rol_no_se_anota(self):
        self.create(rol=self.r_vip)
        self.assertIn("solo para quienes tienen el rol", self.join(self.bob).text)
        self.assertEqual(sdb.count_entrants(self.db, self.msg().id), 0)
        self.assertIn("quedaste anotado", self.join(self.alice).text)

    def test_everyone_no_vale(self):
        everyone = discord.Role(self.guild.id, "@everyone", self.guild)
        self.assertIn("no @everyone", self.create(rol=everyone).text)

    def test_al_sortear_pierde_el_rol_y_queda_afuera(self):
        self.create(rol=self.r_vip, winners=1)
        m = self.msg()
        self.join(self.alice)
        self.alice.roles.remove(self.r_vip)                   # se anotó con el rol y después lo perdió
        self.past(m.id)
        run(self.cog.process_due())
        self.assertIn("cancelado", [x.content for x in self.panelch.sent if x.content][-1])
        self.assertEqual(run(self.cog.valid_entrants(self.guild, [self.bob.id], self.r_vip.id)), [])

    def test_sortea_solo_entre_los_del_rol(self):
        self.bob.roles.append(self.r_vip)
        self.create(rol=self.r_vip, winners=2)
        m = self.msg()
        self.join(self.alice); self.join(self.bob)
        self.past(m.id)
        run(self.cog.process_due())
        txt = [x.content for x in self.panelch.sent if x.content][-1]
        self.assertIn(f"<@{self.alice.id}>", txt); self.assertIn(f"<@{self.bob.id}>", txt)
        self.assertIn("con el rol", txt)

    def test_tiempo_enorme_rechazado(self):
        self.assertIn("Tiempo inválido", self.create(tiempo="999999999:00").text)


class AnotarRolTests(SorteoBase):
    def setUp(self):
        super().setUp()
        self.r_vip = discord.Role(777000000000000001, "VIP", self.guild)
        self.guild.roles.append(self.r_vip)
        self.alice.roles.append(self.r_vip)
        self.bob.roles.append(self.r_vip)

    def test_sin_rol_no_se_puede(self):
        self.assertIn("elegir también el **rol**", self.create(anotar_rol=True).text)
        self.assertEqual(len([m for m in self.panelch.sent if m.embeds]), 0)

    def test_anota_a_todos_los_del_rol_al_crear(self):
        i = self.create(rol=self.r_vip, anotar_rol=True)
        self.assertIn("Anotados con el rol: 2", i.text)
        m = self.msg()
        self.assertEqual(sorted(sdb.get(self.db, m.id).entrants), sorted([self.alice.id, self.bob.id]))
        self.assertTrue(sdb.get(self.db, m.id).auto_role)

    def test_al_sortear_entran_los_que_recibieron_el_rol_despues(self):
        self.create(rol=self.r_vip, anotar_rol=True, winners=3)
        m = self.msg()
        self.staff.roles.append(self.r_vip)                    # lo recibe mientras corre el sorteo
        self.past(m.id)
        run(self.cog.process_due())
        txt = [x.content for x in self.panelch.sent if x.content][-1]
        for u in (self.alice, self.bob, self.staff):
            self.assertIn(f"<@{u.id}>", txt)

    def test_sin_la_opcion_no_anota_a_nadie(self):
        self.create(rol=self.r_vip)
        self.assertEqual(sdb.count_entrants(self.db, self.msg().id), 0)


class JugadoresTests(SorteoBase):
    def test_anota_solo_a_los_mencionados(self):
        i = self.create(jugadores=f"<@{self.alice.id}> <@!{self.bob.id}> <@{self.alice.id}>")
        self.assertIn("Solo jugadores anotados: 2", i.text)
        g = sdb.get(self.db, self.msg().id)
        self.assertTrue(g.closed)
        self.assertEqual(sorted(g.entrants), sorted([self.alice.id, self.bob.id]))
        self.assertTrue(any("anotados" in str(f[0]).lower() for f in self.msg().embeds[0].fields))

    def test_otro_no_puede_anotarse(self):
        self.create(jugadores=f"<@{self.alice.id}>")
        self.assertIn("solo para los jugadores anotados", self.join(self.bob).text)
        self.assertIn("Ya estabas anotado", self.join(self.alice).text)
        self.assertEqual(sorted(sdb.get(self.db, self.msg().id).entrants), [self.alice.id])

    def test_sortea_solo_entre_los_mencionados(self):
        self.create(jugadores=f"<@{self.alice.id}> <@{self.bob.id}>", winners=2)
        m = self.msg()
        self.past(m.id)
        run(self.cog.process_due())
        txt = [x.content for x in self.panelch.sent if x.content][-1]
        for u in (self.alice, self.bob):
            self.assertIn(f"<@{u.id}>", txt)
        self.assertNotIn(f"<@{self.council.id}>", txt)

    def test_sin_jugadores_validos(self):
        self.assertIn("No encontré jugadores válidos", self.create(jugadores="nadie <@123>").text)
        self.assertEqual(len([m for m in self.panelch.sent if m.embeds]), 0)

    def test_no_se_combina_con_anotar_rol(self):
        r = discord.Role(777000000000000001, "VIP", self.guild)
        self.assertIn("no los dos juntos", self.create(rol=r, anotar_rol=True, jugadores=f"<@{self.alice.id}>").text)

    def test_sin_la_opcion_sigue_abierto(self):
        self.create()
        self.assertFalse(sdb.get(self.db, self.msg().id).closed)
        self.assertIn("quedaste anotado", self.join(self.bob).text)


class BalTests(SorteoBase):
    def setUp(self):
        super().setUp()
        import economy_db as eco
        self.eco = eco
        eco.init_schema(self.db)

    def balance(self, u):
        return self.eco.get_balance(self.db, u.id) if hasattr(self.eco, "get_balance") else self.eco.balance(self.db, u.id)

    def test_bal_invalido(self):
        self.assertIn("Balance inválido", self.create(bal="abc").text)
        self.assertIn("Balance inválido", self.create(bal="-5").text)
        self.assertEqual(len([m for m in self.panelch.sent if m.embeds]), 0)

    def test_paga_a_cada_ganador_una_sola_vez(self):
        self.assertIn("Balance por ganador", self.create(winners=2, bal="5m").text)
        m = self.msg()
        self.assertEqual(sdb.get(self.db, m.id).bal, 5_000_000)
        self.assertTrue(any("balance" in str(f[0]).lower() for f in m.embeds[0].fields))
        self.join(self.alice); self.join(self.bob)
        self.past(m.id)
        run(self.cog.process_due())
        self.assertEqual((self.balance(self.alice), self.balance(self.bob)), (5_000_000, 5_000_000))
        self.assertIn("Se acreditó", [x.content for x in self.panelch.sent if x.content][-1])
        run(self.cog.process_due())
        self.assertEqual(self.balance(self.alice), 5_000_000)                 # no se vuelve a pagar

    def test_reintento_del_anuncio_no_paga_dos_veces(self):
        self.create(winners=1, bal="1.000")
        m = self.msg()
        self.join(self.alice)
        self.past(m.id)
        orig = self.panelch.send

        async def boom(*a, **k):
            raise discord.HTTPException()
        self.panelch.send = boom
        run(self.cog.process_due())
        self.panelch.send = orig
        run(self.cog.process_due())
        self.assertEqual(self.balance(self.alice), 1000)

    def test_sin_participantes_no_paga(self):
        self.create(bal="1.000")
        self.past(self.msg().id)
        run(self.cog.process_due())
        self.assertEqual(self.balance(self.alice), 0)


class TiradasTests(SorteoBase):
    def create_t(self, winners, tiradas):
        i = self.inter(self.staff)
        run(self.cog.sorteo.callback(self.cog, i, "Tiradas", "1:00", winners, None, None, False, tiradas))
        return i

    def test_reparte_en_partes_iguales_una_sola_vez(self):
        import wujupon_db as wdb
        self.assertIn("Tiradas de Wujupón por ganador: 1", self.create_t(2, 2).text)
        m = self.msg()
        self.assertEqual(sdb.get(self.db, m.id).spins, 1)
        self.join(self.alice); self.join(self.bob)
        self.past(m.id)
        run(self.cog.process_due())
        self.assertEqual((wdb.get_spins(self.db, self.alice.id), wdb.get_spins(self.db, self.bob.id)), (1, 1))
        self.assertIn("tirada(s) de Wujupón", [x.content for x in self.panelch.sent if x.content][-1])
        run(self.cog.process_due())
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 1)              # no se vuelve a dar

    def test_tienen_que_repartirse_iguales(self):
        self.assertIn("partes iguales", self.create_t(2, 5).text)
        self.assertEqual(len([m for m in self.panelch.sent if m.embeds]), 0)


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
