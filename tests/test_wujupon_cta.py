"""Cartas de Wujupón para las CTA: Anotación libre, Slot libre en Party 1 y Staff a tu elección.

bot.py no se puede importar en las pruebas: se crean las tablas mínimas de CTA y se simulan sus ganchos.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import efectos_db as edb  # noqa: E402
import wujupon as wj  # noqa: E402
import wujupon_cta as wcta  # noqa: E402
import wujupon_db as wdb  # noqa: E402
import wujupon_efectos as we  # noqa: E402
from dbutil import connect  # noqa: E402

STAFF_ROLE = we.STAFF_ROLE_ID


class CtaCardsTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.wcog = run(wj.setup_wujupon(self.bot, self.db, GUILD_ID))
        self.cog = run(we.setup_efectos(self.bot, self.db, GUILD_ID))
        conn = connect(self.db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cta_runs (id INTEGER PRIMARY KEY, guild_id INTEGER, status TEXT, final_state TEXT);
            CREATE TABLE IF NOT EXISTS cta_signups (cta_id INTEGER, slot_key TEXT, user_id INTEGER);
            INSERT INTO cta_runs(id, guild_id, status) VALUES (1, %d, 'open'), (2, %d, 'open');
        """ % (GUILD_ID, GUILD_ID))
        conn.close()
        self.r_staff = discord.Role(STAFF_ROLE, "Staff", self.guild)
        self.staffer = discord.Member(900000000000000077, "Staffer", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.staffer)
        self.assigned = []

        def assign(run_id, target_id, key, actor_id):
            if key == "1:1":
                return "occupied", 555
            self.assigned.append((run_id, target_id, key, actor_id))
            return "assigned", None
        self.bot.cta_hooks = {"open_runs": lambda gid: [(1, "CTA uno")],
                              "slot": lambda run_id, n: (f"1:{n}", f"posición {n} · Tank (Party 1)") if n < 50 else None,
                              "assign": assign}

    def card(self, key, user=None):
        return wdb.give_card(self.db, actor_id=1, user_id=(user or self.alice).id, prize_key=key, reason="t")

    def sign(self, run_id, user):
        conn = connect(self.db)
        conn.execute("INSERT INTO cta_signups(cta_id, slot_key, user_id) VALUES (?,?,?)", (run_id, "2:3", user.id))
        conn.close()

    def set_run(self, run_id, status, final=None):
        conn = connect(self.db)
        conn.execute("UPDATE cta_runs SET status = ?, final_state = ? WHERE id = ?", (status, final, run_id))
        conn.close()

    # ---------------- reglas de cobertura ----------------
    def test_que_cubre_cada_carta(self):
        self.assertTrue(wcta.covers(wcta.PASS_FREE, 3, False, True))
        self.assertFalse(wcta.covers(wcta.PASS_FREE, 1, True, False))      # la anotación libre no salta Legacy
        self.assertTrue(wcta.covers(wcta.PASS_P1, 1, True, True))
        self.assertFalse(wcta.covers(wcta.PASS_P1, 2, False, True))          # Party 1 solo sirve en la Party 1

    def test_oferta_y_reserva(self):
        self.assertEqual(wcta.check(self.db, self.alice.id, 1, 2, False, True)[0], "none")
        cid = self.card("flex_cta")
        state, card_id, key = wcta.check(self.db, self.alice.id, 1, 2, False, True)
        self.assertEqual((state, card_id, key), ("offer", cid, "flex_cta"))
        self.assertTrue(wcta.bind(self.db, cid, self.alice.id, 1))
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        self.assertEqual(wcta.check(self.db, self.alice.id, 1, 4, False, True)[0], "bound")      # vale para toda la CTA
        self.assertEqual(wcta.check(self.db, self.alice.id, 2, 4, False, True)[0], "none")       # no para otra CTA
        self.assertFalse(wcta.bind(self.db, cid, self.alice.id, 2))

    def test_prefiere_party1_si_sirve(self):
        self.card("flex_cta")
        p1 = self.card("party1_slot")
        self.assertEqual(wcta.check(self.db, self.alice.id, 1, 1, False, True)[1], p1)

    # ---------------- cierre de la CTA ----------------
    def test_termina_anotado_se_consume(self):
        cid = self.card("flex_cta")
        wcta.bind(self.db, cid, self.alice.id, 1)
        self.sign(1, self.alice)
        self.assertEqual(wcta.settle(self.db), [])                         # abierta y anotado: sigue reservada
        self.set_run(1, "closed", "finished")
        self.assertEqual(wcta.settle(self.db), [(cid, self.alice.id, "consumed")])
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")

    def test_se_bajo_vuelve_al_inventario(self):
        cid = self.card("party1_slot")
        wcta.bind(self.db, cid, self.alice.id, 1)
        self.assertEqual(wcta.settle(self.db), [(cid, self.alice.id, "released")])     # no está anotado
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_cta_cancelada_vuelve_al_inventario(self):
        cid = self.card("flex_cta")
        wcta.bind(self.db, cid, self.alice.id, 1)
        self.sign(1, self.alice)
        self.set_run(1, "closed", "cancelled")
        wcta.settle(self.db)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    # ---------------- oferta en el hilo ----------------
    def test_oferta_en_el_hilo(self):
        class Msg:
            def __init__(s):
                s.replies = []

            async def reply(s, text, **kw):
                s.replies.append((text, kw))
        cid = self.card("flex_cta")
        msg = Msg()
        retried = []

        async def retry():
            retried.append(1)
        ok = run(wcta.offer_or_error(self.db, msg, self.alice, 1, 2, False, True, "Necesitás el rol Tank.", retry))
        self.assertFalse(ok)
        text, kw = msg.replies[0]
        self.assertIn("Anotación libre", text)
        view = kw["view"]
        i = self.inter(self.bob)
        self.assertFalse(run(view.interaction_check(i)))                    # solo el dueño
        i = self.inter(self.alice)
        run(view.use.callback(i))
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        self.assertEqual(retried, [1])
        self.assertTrue(run(wcta.offer_or_error(self.db, Msg(), self.alice, 1, 3, False, True, "x", retry)))

    def test_sin_carta_solo_el_error(self):
        class Msg:
            replies = []

            async def reply(s, text, **kw):
                s.replies.append((text, kw))
        m = Msg()
        self.assertFalse(run(wcta.offer_or_error(self.db, m, self.alice, 1, 2, False, True, "Necesitás el rol Tank.", None)))
        self.assertEqual(m.replies[0][0], "Necesitás el rol Tank.")
        self.assertNotIn("view", m.replies[0][1])

    # ---------------- Staff a tu elección ----------------
    def test_canjear_pases_no_los_gasta(self):
        cid = self.card("flex_cta")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        self.assertIn("se usa sola", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_staff_a_tu_eleccion(self):
        cid = self.card("party_switch")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        self.assertIsInstance(i.response.sent[0][1]["view"], we.StaffCardView)
        # no es Staff
        i = self.inter(self.alice)
        run(self.cog.finish_staff_card(i, cid, 1, self.bob, "5"))
        self.assertIn("rol **Staff**", i.text)
        # posición ocupada
        i = self.inter(self.alice)
        run(self.cog.finish_staff_card(i, cid, 1, self.staffer, "1"))
        self.assertIn("ocupada", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        # con escudo
        edb.use_card(self.db, card_id=self.card("shield_1h", self.staffer), owner_id=self.staffer.id, prize_key="shield_1h",
                     action="shield", minutes=60, target_id=self.staffer.id)
        i = self.inter(self.alice)
        run(self.cog.finish_staff_card(i, cid, 1, self.staffer, "5"))
        self.assertIn("escudo", i.text)
        edb.end_all(self.db, self.staffer.id, 1)
        # bien
        i = self.inter(self.alice)
        run(self.cog.finish_staff_card(i, cid, 1, self.staffer, "5"))
        self.assertIn("quedó en la posición 5", i.text)
        self.assertEqual(self.assigned, [(1, self.staffer.id, "1:5", self.alice.id)])
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")

    def test_staff_sin_cta_abierta(self):
        self.bot.cta_hooks["open_runs"] = lambda gid: []
        cid = self.card("party_switch")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        self.assertIn("No hay ninguna CTA abierta", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
