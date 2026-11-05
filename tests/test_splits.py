"""Pruebas de /transparencia, /editar y el cierre de splits (SQLite real + stub de Discord, sin red).

NO prueban la API real de Discord (límites de componentes, permisos del bot, etc.).
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, PANEL_CH, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import economy_db as eco  # noqa: E402
import splits as sp  # noqa: E402
import splits_db as sdb  # noqa: E402


class SplitMathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        eco.init_schema(self.db)
        sdb.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, people=(1, 2, 3), looters=(), **kw):
        sid = sdb.create_split(self.db, guild_id=1, creator_id=9, title="Avalon", people=people, looters=looters, **kw)
        return sid

    def test_formula_with_one_looter(self):
        sid = self.make(people=(1, 2, 3), looters=(4,), total=1_000_000, repair=100_000)
        sdb.set_looter(self.db, sid, 4, looted=200_000, repair=20_000)
        s = sdb.get_split(self.db, sid)
        self.assertEqual(s.people, [1, 2, 3, 4])                       # el looter también está en la lista general
        c = sdb.compute(s)
        self.assertEqual((c.gross, c.repair_total, c.net), (1_200_000, 120_000, 1_080_000))
        self.assertEqual((c.sale_tax, c.guild_tax), (216_000, 54_000))
        self.assertEqual(c.looter_taxes, {4: 18_000})                  # (200,000 − 20,000) × 10 %
        self.assertEqual(c.to_split, 792_000)
        self.assertEqual((c.people, c.per_person, c.leftover), (4, 198_000, 0))

    def test_custom_percentages_and_rounding_down(self):
        sid = self.make(people=(1, 2, 3), total=1_000)
        sdb.update_amounts(self.db, sid, tax_sale=7.5, tax_guild=2.5)
        c = sdb.compute(sdb.get_split(self.db, sid))
        self.assertEqual((c.sale_tax, c.guild_tax, c.to_split), (75, 25, 900))
        self.assertEqual((c.per_person, c.leftover), (300, 0))
        sdb.update_amounts(self.db, sid, total=1_001)
        c = sdb.compute(sdb.get_split(self.db, sid))
        self.assertEqual(c.sale_tax, 75)                                # 75,075 → hacia abajo
        self.assertEqual(c.per_person * 3 + c.leftover, c.to_split)

    def test_never_negative_and_zero_people(self):
        sid = self.make(people=(), total=100, repair=500)
        c = sdb.compute(sdb.get_split(self.db, sid))
        self.assertEqual((c.net, c.to_split, c.per_person), (0, 0, 0))
        sid = self.make(people=(), total=1000)
        self.assertEqual(sdb.compute(sdb.get_split(self.db, sid)).per_person, 0)       # sin personas no hay división por cero

    def test_looter_with_more_repair_than_loot_pays_no_tax(self):
        sid = self.make(people=(1,), looters=(2,), total=0)
        sdb.set_looter(self.db, sid, 2, looted=100, repair=500)
        self.assertEqual(sdb.compute(sdb.get_split(self.db, sid)).looter_taxes, {2: 0})

    def test_per_looter_tax_override_and_reset(self):
        sid = self.make(people=(1,), looters=(2,))
        sdb.set_looter(self.db, sid, 2, looted=1000, tax_pct=50)
        self.assertEqual(sdb.compute(sdb.get_split(self.db, sid)).looter_taxes, {2: 500})
        sdb.set_looter(self.db, sid, 2, reset_tax=True)
        self.assertEqual(sdb.compute(sdb.get_split(self.db, sid)).looter_taxes, {2: 100})

    def test_people_and_looter_management(self):
        sid = self.make(people=(1, 2))
        self.assertEqual(sdb.add_people(self.db, sid, [2, 3, 3]), 1)
        self.assertEqual(sdb.add_looters(self.db, sid, [3, 5]), 2)
        s = sdb.get_split(self.db, sid)
        self.assertEqual(s.people, [1, 2, 3, 5])
        self.assertEqual([l.user_id for l in s.looters], [3, 5])
        self.assertEqual(sdb.remove_people(self.db, sid, [3]), (1, [3]))               # al quitarlo de la lista deja de ser looter
        self.assertEqual(sdb.remove_looters(self.db, sid, [5]), 1)                     # quitar el apartado no lo saca de la lista
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.people, s.looters), ([1, 2, 5], []))
        with self.assertRaises(sdb.SplitError):
            sdb.set_looter(self.db, sid, 99, looted=5)

    def test_limits_and_validation(self):
        with self.assertRaises(sdb.SplitError):
            sdb.create_split(self.db, guild_id=1, creator_id=1, title="  ")
        with self.assertRaises(sdb.SplitError):
            sdb.create_split(self.db, guild_id=1, creator_id=1, title="x", people=range(sdb.MAX_PEOPLE + 1))
        for bad in ("-1", "abc", "101", "nan", "inf"):
            with self.assertRaises(sdb.SplitError):
                sdb.parse_pct(bad, field_name="x")
        self.assertEqual(sdb.parse_pct("7,5%", field_name="x"), 7.5)
        self.assertEqual(sdb.parse_money("1.500.000", field_name="x"), 1_500_000)
        self.assertEqual(sdb.parse_money("", field_name="x"), 0)
        with self.assertRaises(sdb.SplitError):
            sdb.parse_money("12abc", field_name="x")

    def test_close_flow_is_idempotent_and_locks_edits(self):
        sid = self.make(people=(1, 2), looters=(3,), total=1000)
        sdb.set_looter(self.db, sid, 3, looted=1000)
        self.assertEqual(sdb.begin_close(self.db, sid, 7)[0], "started")
        self.assertEqual(sdb.begin_close(self.db, sid, 7)[0], "resume")
        with self.assertRaises(sdb.SplitError):
            sdb.update_amounts(self.db, sid, total=5)
        s = sdb.get_split(self.db, sid)
        calc, paid, ops = sdb.pay_out(self.db, s, 7)
        self.assertEqual(len(ops), 2)
        c2, paid2, ops2 = sdb.pay_out(self.db, s, 7)                                    # reintento: no vuelve a pagar
        self.assertEqual(ops2, [])
        self.assertEqual(paid, paid2)
        sdb.finish_close(self.db, sid)
        self.assertEqual(sdb.begin_close(self.db, sid, 7)[0], "closed")
        # (1000+1000) − 20 % − 5 % − tax looter (100) = 1400 → 3 personas = 466 c/u, el looter cobra +100
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 2), eco.get_balance(self.db, 3)), (466, 466, 566))

    def test_bolsitas_are_added_to_the_amount_to_split_not_to_the_total(self):
        sid = self.make(people=(1, 2), total=1_000)
        sdb.update_amounts(self.db, sid, bolsitas=500)
        c = sdb.compute(sdb.get_split(self.db, sid))
        self.assertEqual((c.gross, c.sale_tax, c.guild_tax, c.to_split), (1_000, 200, 50, 1_250))   # sin taxes sobre las bolsitas

    def test_ava_scouts_get_a_fixed_pay_taken_from_the_split(self):
        sid = self.make(people=(1, 2, 3), looters=(8,), total=10_000, kind="ava", bolsitas=0, scouts=(7, 8), scout_pay=500)
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.kind, s.scouts, s.looters), ("ava", [7, 8], []))        # en ava no hay looters
        c = sdb.compute(s)
        # 10.000 − 20 % − 5 % − 2 scouts × 500 = 6.500 → 3 personas
        self.assertEqual((c.scouts_total, c.to_split, c.per_person, c.leftover), (1_000, 6_500, 2_166, 2))
        sdb.begin_close(self.db, sid, 1)
        _calc, paid, ops = sdb.pay_out(self.db, s, 1)
        self.assertEqual(len(ops), 3)
        self.assertEqual(sdb.pay_out(self.db, s, 1)[2], [])                           # no paga dos veces
        self.assertEqual((eco.get_balance(self.db, 1), eco.get_balance(self.db, 7), eco.get_balance(self.db, 8)), (2_166, 500, 500))

    def test_ava_half_list_gets_half_of_the_share(self):
        sid = self.make(people=(1, 2), kind="ava", total=10_000)
        self.assertEqual(sdb.add_half(self.db, sid, [3, 4, 2]), (3, 1))               # el 2 pasa de la lista general a la mitad
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.people, s.half), ([1], [3, 4, 2]))
        c = sdb.compute(s)
        # 10.000 − 25 % = 7.500 → 1 persona entera (2 partes) + 3 mitades = 5 partes → 1.500
        self.assertEqual((c.to_split, c.per_person, c.per_half, c.half, c.leftover), (7_500, 3_000, 1_500, 3, 0))
        sdb.begin_close(self.db, sid, 1)
        _c, paid, _ops = sdb.pay_out(self.db, s, 1)
        self.assertEqual(sdb.pay_out(self.db, s, 1)[2], [])
        self.assertEqual([eco.get_balance(self.db, u) for u in (1, 2, 3, 4)], [3_000, 1_500, 1_500, 1_500])
        self.assertEqual(paid[3], 1_500)

    def test_half_list_only_in_ava_and_full_add_overrides(self):
        normal = self.make(people=(1,))
        with self.assertRaises(sdb.SplitError):
            sdb.add_half(self.db, normal, [5])
        sid = self.make(people=(1,), kind="ava", total=1_000)
        sdb.add_half(self.db, sid, [5])
        sdb.add_people(self.db, sid, [5])                                              # !agregarlista lo pasa a parte entera
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.people, s.half), ([1, 5], []))
        sdb.add_half(self.db, sid, [6])
        self.assertEqual(sdb.remove_half(self.db, sid, [6]), 1)
        sdb.add_half(self.db, sid, [7])
        sdb.remove_people(self.db, sid, [7])                                           # quitar personas también saca de la mitad
        self.assertEqual(sdb.get_split(self.db, sid).half, [])

    def test_half_list_survives_reopen_and_cancel_edit(self):
        sid = self.make(people=(1,), kind="ava", total=1_000)
        sdb.add_half(self.db, sid, [2])
        s = sdb.get_split(self.db, sid)
        sdb.begin_close(self.db, sid, 1)
        _c, paid, _o = sdb.pay_out(self.db, s, 1)
        sdb.finish_close(self.db, sid, paid)
        self.assertEqual(sdb.reopen(self.db, sid, 1), "reopened")
        sdb.remove_half(self.db, sid, [2])
        self.assertEqual(sdb.cancel_edit(self.db, sid), "restored")
        self.assertEqual(sdb.get_split(self.db, sid).half, [2])

    def test_ava_scout_management_and_normal_split_unchanged(self):
        sid = self.make(people=(1,), kind="ava", scout_pay=100)
        self.assertEqual(sdb.add_scouts(self.db, sid, [5, 5, 6]), 2)
        self.assertEqual(sdb.remove_scouts(self.db, sid, [5]), 1)
        self.assertEqual(sdb.get_split(self.db, sid).scouts, [6])
        normal = self.make(people=(1,), looters=(2,), total=1000, scouts=(9,))        # scouts ignorados fuera de ava
        self.assertEqual(sdb.get_split(self.db, normal).scouts, [])

    def close_and_pay(self, sid):
        sdb.begin_close(self.db, sid, 7)
        s = sdb.get_split(self.db, sid)
        _c, paid, _ops = sdb.pay_out(self.db, s, 7)
        sdb.finish_close(self.db, sid, paid)

    def test_reajuste_subtracts_old_payment_and_repays_with_new_data(self):
        sid = self.make(people=(1, 2), looters=(3,), total=1000)
        sdb.set_looter(self.db, sid, 3, looted=1000)
        self.close_and_pay(sid)                                      # 2000 − 25 % − 100 = 1400 / 3 = 466 · looter +100
        self.assertEqual([eco.get_balance(self.db, u) for u in (1, 2, 3)], [466, 466, 566])
        self.assertEqual(sdb.reopen(self.db, sid, 7), "reopened")
        self.assertEqual(sdb.reopen(self.db, sid, 7), "already")
        s = sdb.get_split(self.db, sid)
        self.assertTrue(s.is_editing)
        self.assertEqual([eco.get_balance(self.db, u) for u in (1, 2, 3)], [466, 466, 566])      # abrir no mueve plata
        sdb.update_amounts(self.db, sid, total=2000)
        sdb.remove_people(self.db, sid, [2])
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[1], amount=466, request_key="retiro")   # ya retiró todo
        self.assertEqual(eco.get_balance(self.db, 1), 0)
        sdb.begin_close(self.db, sid, 7)
        s = sdb.get_split(self.db, sid)
        _c, paid, ops = sdb.pay_out(self.db, s, 7)
        self.assertEqual(sdb.pay_out(self.db, s, 7)[2], [])                                      # reintento: no duplica
        sdb.finish_close(self.db, sid, paid)
        # nuevo: 3000 − 25 % − tax looter 100 = 2150 / 2 personas (1 y 3) = 1075 · looter +100
        self.assertEqual(paid, {1: 1075, 3: 1175})
        self.assertEqual(eco.get_balance(self.db, 1), 0 - 466 + 1075)                            # había retirado: queda en negativo y se compensa
        self.assertEqual(eco.get_balance(self.db, 2), 0)                                         # se le restó lo que cobró
        self.assertEqual(eco.get_balance(self.db, 3), 566 - 566 + 1175)
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.status, s.pay_round), ("closed", 2))
        self.assertEqual(sdb.get_payouts(self.db, sid, 2), {1: 1075, 3: 1175})
        self.assertEqual(sdb.reopen(self.db, sid, 7), "reopened")                                 # y se puede volver a reajustar

    def test_reopen_legacy_closed_split_rebuilds_payouts(self):
        sid = self.make(people=(1, 2), total=1000)
        sdb.begin_close(self.db, sid, 7)
        sdb.pay_out(self.db, sdb.get_split(self.db, sid), 7)
        sdb.finish_close(self.db, sid)                                # como se cerraban antes: sin guardar pagos
        conn = sdb.connect(self.db)
        conn.execute("UPDATE splits SET pay_round=0 WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        self.assertEqual(sdb.reopen(self.db, sid, 7), "reopened")
        self.assertEqual(sdb.get_payouts(self.db, sid, 1), {1: 375, 2: 375})

    def test_cancel_edit_restores_everything_and_cannot_cancel_split(self):
        sid = self.make(people=(1, 2), looters=(3,), total=1000)
        self.close_and_pay(sid)
        sdb.reopen(self.db, sid, 7)
        self.assertEqual(sdb.cancel_split(self.db, sid, 7), "closed")                              # pagado: no se cancela sin pagar
        sdb.update_amounts(self.db, sid, total=9, bolsitas=5)
        sdb.add_people(self.db, sid, [50])
        sdb.set_looter(self.db, sid, 3, looted=777)
        self.assertEqual(sdb.cancel_edit(self.db, sid), "restored")
        s = sdb.get_split(self.db, sid)
        self.assertEqual((s.status, s.total, s.bolsitas, s.people, [(l.user_id, l.looted) for l in s.looters]),
                         ("closed", 1000, 0, [1, 2, 3], [(3, 0)]))
        self.assertEqual(sdb.cancel_edit(self.db, sid), "not_editing")


class SplitDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = sp.SplitsCog(self.bot, self.db, GUILD_ID, PANEL_CH)
        run(self.bot.add_cog(self.cog))
        self.r_split = discord.Role(777, "Avalon", self.guild)
        self.guild.roles.append(self.r_split)
        self.alice.roles.append(self.r_split)
        self.bob.roles.append(self.r_split)
        self.robot.roles.append(self.r_split)

    def create(self, user=None, **kw):
        i = self.inter(user or self.council)
        args = dict(titulo="Avalon", tab=None, city=None, rol=None, total=None, reparacion=None, looters=None)
        args.update(kw)
        run(self.cog.transparencia.callback(self.cog, i, **args))
        return i

    def split_and_thread(self):
        s = sdb.get_split(self.db, 1)
        thread = self.bot.channels.get(s.thread_id)
        if thread is None:                                   # el stub no registra los hilos solo
            panel = [m for m in self.panelch.sent if m.id == s.message_id][0]
            thread = panel.thread
            self.bot.channels[thread.id] = thread
        return s, thread

    def test_only_council_or_admin_can_create(self):
        i = self.create(self.alice)
        self.assertIn("Council", i.text)
        self.assertIsNone(sdb.get_split(self.db, 1))
        self.create(self.council)
        self.assertIsNotNone(sdb.get_split(self.db, 1))

    def test_creates_panel_thread_list_and_data(self):
        i = self.create(rol=self.r_split, total="1.000.000", reparacion="100.000", tab="Tab 3", city="Caerleon",
                        looters=f"<@{self.admin.id}>")
        self.assertIn("creado", i.text)
        s, thread = self.split_and_thread()
        self.assertEqual((s.total, s.repair, s.tab, s.city), (1_000_000, 100_000, "Tab 3", "Caerleon"))
        self.assertEqual(s.people, [self.alice.id, self.bob.id, self.admin.id])         # el robot no entra; el looter sí
        panel = self.panelch.sent[0]
        self.assertEqual(panel.embeds[0].title, "Split Avalon")
        self.assertEqual([c.custom_id for c in panel.view.children], ["split:close", "split:cancel", "split:edit"])
        self.assertEqual(len(thread.sent), 2)
        self.assertIn("Alice", thread.sent[0].embeds[0].description)
        self.assertEqual(thread.sent[0].embeds[0].title, "👥 Personas del split (3)")
        data = thread.sent[1].embeds
        self.assertIn("Looter", data[0].description)
        self.assertIn("Total por persona", data[-1].description)

    def test_invalid_inputs_do_not_create_anything(self):
        for kw, word in ((dict(total="abc"), "Total"), (dict(looters="sin menciones"), "mencion"),
                         (dict(looters=f"<@{self.robot.id}>"), "bot"), (dict(looters="<@123456>"), "No encuentro")):
            self.assertIn(word, self.create(**kw).text)
        self.assertIsNone(sdb.get_split(self.db, 1))
        self.assertEqual(self.panelch.sent, [])

    def edit_panel(self, user=None):
        s, thread = self.split_and_thread()
        i = self.inter(user or self.council, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        view = i.response.sent[0][1]["view"]
        return view, s, thread

    def press(self, view, label_start, user=None):
        btn = [b for b in view.children if b.label.startswith(label_start)][0]
        i = self.inter(user or self.council)
        run(btn.callback(i))
        return i

    def test_editar_requires_permission_and_thread(self):
        self.create()
        s, thread = self.split_and_thread()
        i = self.inter(self.alice, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council, self.panelch)                       # fuera del hilo y sin id
        run(self.cog.editar.callback(self.cog, i, None))
        self.assertIn("No encuentro el split", i.text)
        i = self.inter(self.council, self.panelch)
        run(self.cog.editar.callback(self.cog, i, 1))                    # con id funciona desde cualquier canal
        self.assertIn("Editando", i.text)

    def test_edit_buttons_update_data_and_messages(self):
        self.create(rol=self.r_split, total="1000")
        view, s, thread = self.edit_panel()
        # --- montos
        i = self.press(view, "Montos")
        modal = i.modals[0]
        modal.total_in.value, modal.repair_in.value = "2.000.000", "200.000"
        modal.sale_in.value, modal.guild_in.value, modal.looter_in.value = "20", "5", "10"
        j = self.inter(self.council)
        run(modal.on_submit(j))
        self.assertIn("actualizados", j.text)
        s = sdb.get_split(self.db, 1)
        self.assertEqual((s.total, s.repair), (2_000_000, 200_000))
        self.assertIn("2,000,000", thread.sent[1].embeds[-1].description)          # el mensaje del bot se editó
        self.assertIn("2,000,000", self.panelch.sent[0].embeds[0].description)
        # --- datos
        i = self.press(view, "Datos")
        modal = i.modals[0]
        modal.title_in.value, modal.tab_in.value, modal.city_in.value = "Nuevo título", "Tab 1", ""
        run(modal.on_submit(self.inter(self.council)))
        s = sdb.get_split(self.db, 1)
        self.assertEqual((s.title, s.tab, s.city), ("Nuevo título", "Tab 1", None))
        self.assertEqual(self.panelch.sent[0].embeds[0].title, "Split Nuevo título")
        # --- agregar looters (selector de usuarios)
        i = self.press(view, "Agregar looters")
        pick = i.response.sent[0][1]["view"]
        pick.select.values = [self.admin, self.robot]
        j = self.inter(self.council)
        run(pick.select.callback(j))
        s = sdb.get_split(self.db, 1)
        self.assertEqual([l.user_id for l in s.looters], [self.admin.id])               # el bot se ignora
        self.assertIn(self.admin.id, s.people)
        self.assertIn("Admin", thread.sent[1].embeds[0].description)
        # --- editar looter
        i = self.press(view, "Editar looter")
        pick = i.response.sent[0][1]["view"]
        pick.select.values = [str(self.admin.id)]
        j = self.inter(self.council)
        run(pick.select.callback(j))
        lm = j.modals[0]
        lm.looted_in.value, lm.repair_in.value, lm.tax_in.value = "500.000", "50.000", ""
        run(lm.on_submit(self.inter(self.council)))
        s = sdb.get_split(self.db, 1)
        self.assertEqual((s.looters[0].looted, s.looters[0].repair, s.looters[0].tax_pct), (500_000, 50_000, None))
        self.assertIn("45,000", thread.sent[1].embeds[0].description)                   # tax looter = 450,000 × 10 %
        # --- valores inválidos no cambian nada
        lm.looted_in.value = "mucho"
        j = self.inter(self.council)
        run(lm.on_submit(j))
        self.assertIn("❌", j.text)
        self.assertEqual(sdb.get_split(self.db, 1).looters[0].looted, 500_000)
        # --- quitar looter / personas / agregar rol
        i = self.press(view, "Quitar looters")
        pick = i.response.sent[0][1]["view"]
        pick.select.values = [str(self.admin.id)]
        run(pick.select.callback(self.inter(self.council)))
        self.assertEqual(sdb.get_split(self.db, 1).looters, [])
        i = self.press(view, "Quitar personas")
        pick = i.response.sent[0][1]["view"]
        pick.select.values = [self.bob]
        run(pick.select.callback(self.inter(self.council)))
        self.assertNotIn(self.bob.id, sdb.get_split(self.db, 1).people)
        i = self.press(view, "Agregar rol")
        pick = i.response.sent[0][1]["view"]
        pick.select.values = [self.r_split]
        run(pick.select.callback(self.inter(self.council)))
        self.assertIn(self.bob.id, sdb.get_split(self.db, 1).people)

    def test_other_people_cannot_use_my_edit_panel(self):
        self.create()
        view, s, thread = self.edit_panel()
        i = self.inter(self.alice)
        self.assertFalse(run(view.interaction_check(i)))
        self.assertTrue(run(view.interaction_check(self.inter(self.council))))

    def test_close_requires_council_then_confirm_then_pays_and_lists(self):
        self.create(rol=self.r_split, total="3000", looters=f"<@{self.admin.id}>")
        sdb.set_looter(self.db, 1, self.admin.id, looted=1000)
        s, thread = self.split_and_thread()
        panel = self.panelch.sent[0]
        # alguien sin Council ni Administrator no puede
        i = self.inter(self.alice, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("Council", i.text)
        # un administrador sin rol Council sí (recibe la confirmación)
        i = self.inter(self.admin, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("¿Cerrar", i.text)
        # el Council recibe la confirmación (todavía no se pagó nada)
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        confirm = i.response.sent[0][1]["view"]
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        self.assertFalse(run(confirm.interaction_check(self.inter(self.alice))))
        ok = confirm.children[0]
        j = self.inter(self.council)
        run(ok.callback(j))
        # (3000+1000) − 20 % − 5 % − 100 = 2900 / 3 personas = 966 · el looter cobra +100
        self.assertEqual((eco.get_balance(self.db, self.alice.id), eco.get_balance(self.db, self.bob.id)), (966, 966))
        self.assertEqual(eco.get_balance(self.db, self.admin.id), 1066)
        self.assertEqual(sdb.get_split(self.db, 1).status, "closed")
        text = "\n".join(m.content for m in thread.sent if m.content)
        self.assertIn("Balance cargado", text)
        self.assertIn(f"<@{self.alice.id}> → +", text)
        self.assertIn("tax looter 100", text)
        self.assertEqual([c.custom_id for c in panel.view.children], ["split:edit"])   # cerrado: solo queda «Editar»
        # cerrar otra vez no paga de nuevo
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("ya está cerrado", i.text)
        run(self.cog.close_split(self.inter(self.council), 1))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 966)
        # y ya no se puede editar
        i = self.inter(self.council, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        self.assertIn("cerrado", i.text)

    def test_close_without_people_is_rejected(self):
        self.create()
        panel = self.panelch.sent[0]
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("no tiene personas", i.text)
        self.assertEqual(sdb.get_split(self.db, 1).status, "open")

    # ---------- cancelar split ----------
    def test_cancel_split_pays_nothing_and_blocks_close(self):
        self.create(rol=self.r_split, total="3000")
        s, thread = self.split_and_thread()
        panel = self.panelch.sent[0]
        i = self.inter(self.alice, message=panel)
        run(self.cog.on_cancel_pressed(i))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council, message=panel)
        run(self.cog.on_cancel_pressed(i))
        self.assertEqual(sdb.get_split(self.db, 1).status, "open")                      # pide confirmación
        confirm = i.response.sent[0][1]["view"]
        self.assertFalse(run(confirm.interaction_check(self.inter(self.alice))))
        j = self.inter(self.council)
        run(confirm.children[0].callback(j))
        self.assertEqual(sdb.get_split(self.db, 1).status, "cancelled")
        self.assertEqual((eco.get_balance(self.db, self.alice.id), eco.get_balance(self.db, self.bob.id)), (0, 0))
        self.assertIsNone(panel.view)
        self.assertIn("cancelado", "\n".join(m.content for m in thread.sent if m.content))
        # ya no se puede cerrar ni editar
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("ya está cerrado", i.text)
        run(self.cog.close_split(self.inter(self.council), 1))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        with self.assertRaises(sdb.SplitError):
            sdb.add_people(self.db, 1, [5])

    def test_cannot_cancel_once_closing_started(self):
        self.create(rol=self.r_split, total="3000")
        sdb.begin_close(self.db, 1, self.council.id)
        self.assertEqual(sdb.cancel_split(self.db, 1, self.council.id), "closing")

    # ---------- !agregarlista ----------
    def agregar(self, user, channel, mentions, text="", roles=()):
        class Msg:
            pass

        class Ctx:
            guild = self.guild
            replies = []

            async def reply(inner, content=None, **kw):
                inner.replies.append(content)

        ctx = Ctx()
        ctx.author, ctx.channel = user, channel
        ctx.message = Msg()
        ctx.message.mentions = mentions
        ctx.message.role_mentions = list(roles)
        ctx.message.content = "!agregarlista " + text
        ctx.replies = []
        run(self.cog.agregarlista.callback(self.cog, ctx, texto=text))
        return ctx.replies[0]

    def test_agregarlista_adds_everyone_mentioned_inside_the_thread(self):
        self.create()
        s, thread = self.split_and_thread()
        out = self.agregar(self.council, thread, [self.alice, self.bob, self.robot])
        self.assertIn("2 persona", out)
        self.assertEqual(sdb.get_split(self.db, 1).people, [self.alice.id, self.bob.id])         # el bot se ignora
        self.assertIn("Bob", thread.sent[0].embeds[0].description)                 # la lista del hilo se actualiza
        self.assertIn("ya estaban", self.agregar(self.council, thread, [self.alice]))
        self.assertIn("Mencioná", self.agregar(self.council, thread, []))

    def test_agregarlista_with_role_adds_its_members(self):
        self.create()
        s, thread = self.split_and_thread()
        rol = discord.Role(9301, "Bomb Norte", self.guild)
        self.guild.roles.append(rol)
        self.bob.roles.append(rol); self.admin.roles.append(rol); self.robot.roles.append(rol)
        out = self.agregar(self.council, thread, [self.alice, self.bob], roles=[rol])
        self.assertIn("3 persona", out)                                          # alice + bob + admin (bob no se repite, el bot no entra)
        self.assertIn("Roles:", out)
        self.assertEqual(sorted(sdb.get_split(self.db, 1).people), sorted([self.alice.id, self.bob.id, self.admin.id]))
        self.council.roles.append(rol)                                          # rol solo escrito en el texto (no mencionable)
        out = self.agregar(self.council, thread, [], text=f"<@&{rol.id}>")
        self.assertIn("1 persona", out)
        self.assertIn(self.council.id, sdb.get_split(self.db, 1).people)

    def test_agregarlista_permissions_and_context(self):
        self.create()
        s, thread = self.split_and_thread()
        self.assertIn("Solo el Council", self.agregar(self.alice, thread, [self.bob]))
        self.assertIn("dentro del hilo", self.agregar(self.council, self.panelch, [self.bob]))
        self.assertIn("2 persona", self.agregar(self.council, self.panelch, [self.alice, self.bob], text="1 @a @b"))    # con el número del split
        sdb.cancel_split(self.db, 1, 1)
        self.assertIn("cerrado", self.agregar(self.council, thread, [self.admin]))

    def test_agregarlistamitad_command(self):
        self.create_ava()
        s, thread = self.split_and_thread()
        class Msg: pass
        class Ctx:
            guild = self.guild
        def go(user, channel, mentions, text=""):
            ctx = Ctx(); ctx.author, ctx.channel = user, channel
            ctx.message = Msg(); ctx.message.mentions = mentions; ctx.replies = []
            async def reply(content=None, **kw): ctx.replies.append(content)
            ctx.reply = reply
            run(self.cog.agregarlistamitad.callback(self.cog, ctx, texto=text))
            return ctx.replies[0]
        self.assertIn("1 persona", go(self.council, thread, [self.alice, self.robot]))
        self.assertEqual(sdb.get_split(self.db, s.id).half, [self.alice.id])
        self.assertIn("Lista mitad", thread.sent[0].embeds[0].fields[0][0])
        self.assertIn("Solo el Council", go(self.bob, thread, [self.alice]))
        self.assertIn("ya estaban", go(self.council, thread, [self.alice]))
        self.create()                                                                  # split normal: no tiene lista mitad
        self.assertIn("solo existe", go(self.council, self.panelch, [self.alice], text="2 @a"))

    # ---------- split automático desde una CTA con track ----------

    def test_create_from_cta_lists_signups_and_is_idempotent(self):
        split, msg, err = run(self.cog.create_from_cta(cta_id=7, title="CTA Avalon #7", tab="Tab 2", city="Bridgewatch",
                                                       user_ids=[self.alice.id, self.bob.id, self.alice.id], creator_id=self.admin.id))
        self.assertIsNone(err)
        self.assertEqual((split.people, split.tab, split.city, split.creator_id), ([self.alice.id, self.bob.id], "Tab 2", "Bridgewatch", self.admin.id))
        self.assertEqual(sdb.find_by_cta(self.db, 7).id, split.id)
        again, msg2, err2 = run(self.cog.create_from_cta(cta_id=7, title="x", tab=None, city=None, user_ids=[self.bob.id], creator_id=1))
        self.assertEqual((again.id, msg2, err2), (split.id, None, None))                      # no se duplica
        self.assertEqual(len(self.panelch.sent), 1)

    def _cierre_de_split_de_cta(self, con_killboard: bool):
        import killboard_db as kbdb
        kbdb.init_schema(self.db)
        if con_killboard:
            P = lambda n, g="Smogg": {"Name": n, "GuildName": g, "AverageItemPower": 1300}
            evs = [{"Killer": P("Alice"), "Victim": P("Rival", "Otros"), "TimeStamp": "2026-10-31T22:00:00Z", "Participants": [], "GroupMembers": []}]
            kbdb.save_battle(self.db, "700", 7, 1, kbdb.parse_events(evs, "Smogg"))
        split, _msg, _err = run(self.cog.create_from_cta(cta_id=7, title="CTA #7", tab="T", city="C", user_ids=[self.alice.id], creator_id=self.admin.id))
        i = self.inter(self.admin, message=self.panelch.sent[0])
        run(self.cog.on_close_pressed(i))
        return i

    def test_cerrar_split_de_cta_sin_killboard_pide_confirmar(self):
        i = self._cierre_de_split_de_cta(con_killboard=False)
        self.assertIn("no tiene killboard cargado", i.text)
        self.assertIn("¿Seguro que querés splitear sin killboard?", i.text)
        view = next(kw["view"] for _c, kw in i.response.sent if kw.get("view"))
        self.assertEqual(view.children[0].label, "Sí, cerrar sin killboard")

    def test_cerrar_split_de_cta_con_killboard_no_avisa(self):
        i = self._cierre_de_split_de_cta(con_killboard=True)
        self.assertNotIn("sin killboard", i.text)
        self.assertIn("¿Cerrar", i.text)

    def test_add_bomb_entra_directo_al_split(self):
        self.assertEqual(run(self.cog.add_bomb(cta_id=7, user_id=self.bob.id, actor_id=self.admin.id)), "nosplit")      # todavía no hay split
        split, _msg, _err = run(self.cog.create_from_cta(cta_id=7, title="CTA #7", tab="T", city="C", user_ids=[self.alice.id], creator_id=self.admin.id))
        self.assertEqual(run(self.cog.add_bomb(cta_id=7, user_id=self.alice.id, actor_id=self.admin.id)), "listed")     # ya estaba
        self.assertEqual(run(self.cog.add_bomb(cta_id=7, user_id=self.bob.id, actor_id=self.admin.id)), "added")
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        self.assertEqual(run(self.cog.add_bomb(cta_id=7, user_id=self.bob.id, actor_id=self.admin.id)), "listed")       # no se duplica
        sdb.cancel_split(self.db, split.id, 1)
        self.assertEqual(run(self.cog.add_bomb(cta_id=7, user_id=self.outsider.id, actor_id=self.admin.id)), "closed")

    def test_repartir_un_split_cuenta_como_tarea_hecha_una_vez(self):
        import encargos, encargos_db as edb
        run(encargos.setup_encargos(self.bot, self.db, GUILD_ID))
        self.create(rol=self.r_split, total="1.000")
        run(self.cog.close_split(self.inter(self.council), 1))
        self.assertEqual(edb.top_users(self.db)[1], [(self.council.id, 1)])
        run(self.cog.close_split(self.inter(self.council), 1))               # ya cerrado
        self.assertEqual(edb.top_users(self.db)[1], [(self.council.id, 1)])

    def test_late_signups_become_requests_the_organizer_decides(self):
        split, _msg, _err = run(self.cog.create_from_cta(cta_id=7, title="CTA #7", tab="T", city="C", user_ids=[self.alice.id], creator_id=self.admin.id))
        thread = self.panelch.sent[0].thread
        self.bot.channels[thread.id] = thread
        self.assertIsNone(run(self.cog.register_late_request(cta_id=99, user_id=self.bob.id, slot_no=3)))        # esa CTA no tiene split
        self.assertEqual(run(self.cog.register_late_request(cta_id=7, user_id=self.alice.id, slot_no=1)), "listed")   # ya estaba anotada
        self.assertEqual(run(self.cog.register_late_request(cta_id=7, user_id=self.bob.id, slot_no=3)), "new")
        self.assertEqual(run(self.cog.register_late_request(cta_id=7, user_id=self.bob.id, slot_no=3)), "pending")    # no se duplica
        self.assertEqual(run(self.cog.register_late_request(cta_id=7, user_id=self.outsider.id, slot_no=None)), "new")
        reqs = [m for m in thread.sent if m.view is not None and "se anotó después" in m.content]
        self.assertEqual(len(reqs), 2)
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id])            # nadie entra solo
        # un miembro sin permiso no puede decidir
        i = self.inter(self.alice, thread, message=reqs[0])
        run(self.cog.on_request_decision(i, True))
        self.assertIn("Solo el Council", i.text)
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id])
        # el organizador agrega a uno y descarta al otro
        i = self.inter(self.council, thread, message=reqs[0])
        run(self.cog.on_request_decision(i, True))
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        self.assertIn("Agregado", i.response.edits[0]["content"])
        i = self.inter(self.council, thread, message=reqs[1])
        run(self.cog.on_request_decision(i, False))
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        self.assertIn("Descartado", i.response.edits[0]["content"])
        i = self.inter(self.council, thread, message=reqs[1])                                 # decidir dos veces
        run(self.cog.on_request_decision(i, True))
        self.assertIn("ya estaba decidida", i.text)
        self.assertIn("Bob", thread.sent[0].embeds[0].description)              # la lista del hilo se actualizó
        # con el split cancelado ya no se registran solicitudes
        sdb.cancel_split(self.db, split.id, 1)
        self.assertEqual(run(self.cog.register_late_request(cta_id=7, user_id=self.robot.id, slot_no=1)), "closed")


    def _split_cta7(self, users):
        split, _m, _e = run(self.cog.create_from_cta(cta_id=7, title="CTA #7", tab="T", city="C", user_ids=users, creator_id=self.admin.id))
        thread = self.panelch.sent[0].thread
        self.bot.channels[thread.id] = thread
        return split, thread

    def test_sugerencia_sacar_anotado_que_no_se_conecto(self):
        split, thread = self._split_cta7([self.alice.id, self.bob.id])
        self.assertEqual(run(self.cog.apply_suggestions(7, [self.bob.id, self.outsider.id], [])), (1, 0))   # outsider no está en la lista: se ignora
        qs = [m for m in thread.sent if m.view is not None and "no estaba conectado" in m.content]
        self.assertEqual(len(qs), 1)
        self.assertEqual(run(self.cog.apply_suggestions(7, [self.bob.id], [])), (0, 0))                     # no se vuelve a preguntar
        i = self.inter(self.alice, thread, message=qs[0])                                                   # sin permiso
        run(self.cog.on_suggestion_decision(i, True))
        self.assertIn("Solo el Council", i.text)
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        i = self.inter(self.council, thread, message=qs[0])
        run(self.cog.on_suggestion_decision(i, True))
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id])
        self.assertIn("Sacado", i.response.edits[0]["content"])
        i = self.inter(self.council, thread, message=qs[0])                                                 # decidir dos veces
        run(self.cog.on_suggestion_decision(i, True))
        self.assertIn("ya estaba decidido", i.text)

    def test_sugerencia_sumar_del_killboard_y_dejarlo(self):
        split, thread = self._split_cta7([self.alice.id])
        self.assertEqual(run(self.cog.apply_suggestions(7, [], [self.bob.id, self.alice.id])), (0, 1))      # alice ya está
        qs = [m for m in thread.sent if m.view is not None and "no estaba anotado" in m.content]
        self.assertEqual(len(qs), 1)
        i = self.inter(self.council, thread, message=qs[0])
        run(self.cog.on_suggestion_decision(i, True))
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        self.assertIn("Sumado", i.response.edits[0]["content"])
        split2, thread2 = split, thread
        self.assertEqual(run(self.cog.apply_suggestions(7, [], [self.outsider.id])), (0, 1))
        q2 = [m for m in thread2.sent if m.view is not None and "no estaba anotado" in m.content][-1]
        i = self.inter(self.council, thread2, message=q2)
        run(self.cog.on_suggestion_decision(i, False))
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        self.assertIn("se queda", i.response.edits[0]["content"].lower())
        self.assertEqual(run(self.cog.apply_suggestions(7, [], [self.outsider.id])), (0, 0))                # dijeron que no: no insiste

    def test_sugerencia_se_anula_si_ya_no_aplica_y_no_hay_con_split_cerrado(self):
        split, thread = self._split_cta7([self.alice.id, self.bob.id])
        run(self.cog.apply_suggestions(7, [self.bob.id], []))
        q = [m for m in thread.sent if m.view is not None and "no estaba conectado" in m.content][0]
        run(self.cog.apply_suggestions(7, [], []))                                                          # cargaron algo que lo corrige
        self.assertIn("Ya no aplica", q.content)
        i = self.inter(self.council, thread, message=q)
        run(self.cog.on_suggestion_decision(i, True))
        self.assertIn("ya estaba decidido", i.text)
        self.assertEqual(sdb.get_split(self.db, split.id).people, [self.alice.id, self.bob.id])
        sdb.cancel_split(self.db, split.id, 1)
        self.assertEqual(run(self.cog.apply_suggestions(7, [self.alice.id], [self.outsider.id])), (0, 0))
        self.assertEqual(run(self.cog.apply_suggestions(99, [self.alice.id], [])), (0, 0))                  # esa CTA no tiene split

    def test_sugerencia_sacar_looter_lo_saca_tambien_de_looters(self):
        split, thread = self._split_cta7([self.alice.id, self.bob.id])
        sdb.add_looters(self.db, split.id, [self.bob.id])
        run(self.cog.apply_suggestions(7, [self.bob.id], []))
        q = [m for m in thread.sent if m.view is not None and "no estaba conectado" in m.content][0]
        i = self.inter(self.council, thread, message=q)
        run(self.cog.on_suggestion_decision(i, True))
        s = sdb.get_split(self.db, split.id)
        self.assertEqual(s.people, [self.alice.id])
        self.assertNotIn(self.bob.id, [getattr(l, 'user_id', l) for l in s.looters])
        self.assertIn("looter", i.response.edits[0]["content"])

    def create_ava(self, user=None, **kw):
        i = self.inter(user or self.council)
        args = dict(titulo="Ava", tab=None, city=None, rol=None, total=None, reparacion=None, bolsitas=None, scouts=None, pago_scout=None)
        args.update(kw)
        run(self.cog.transparencia_ava.callback(self.cog, i, **args))
        return i

    def test_ava_command_creates_split_with_scouts_and_bolsitas(self):
        i = self.create_ava(rol=self.r_split, total="10.000", bolsitas="2.000", pago_scout="500",
                            scouts=f"<@{self.admin.id}>")
        self.assertIn("creado", i.text)
        s, thread = self.split_and_thread()
        self.assertEqual((s.kind, s.bolsitas, s.scout_pay, s.scouts), ("ava", 2000, 500, [self.admin.id]))
        self.assertEqual(s.people, [self.alice.id, self.bob.id])                      # el scout no entra a la lista general
        data = thread.sent[1].embeds
        self.assertTrue(any("Scouts" in (e.title or "") for e in data))
        self.assertNotIn("Looters", " ".join(e.title or "" for e in data))
        self.assertIn("Bolsitas", data[-1].description)
        self.assertIn("Bolsitas", self.panelch.sent[0].embeds[0].description)
        # 10.000 − 25 % − 500 + bolsitas 2.000 = 9.000 → 2 personas
        self.assertIn("4,500", data[-1].description)
        self.assertIn("!agregarlista", data[-1].footer.text if hasattr(data[-1].footer, 'text') else str(data[-1].footer))
        i = self.create_ava(self.alice)
        self.assertIn("Council", i.text)

    def test_ava_close_pays_scouts_and_lists_them(self):
        self.create_ava(rol=self.r_split, total="10.000", pago_scout="500", scouts=f"<@{self.admin.id}>")
        s, thread = self.split_and_thread()
        panel = self.panelch.sent[0]
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        self.assertEqual((eco.get_balance(self.db, self.alice.id), eco.get_balance(self.db, self.admin.id)), (3_500, 500))
        text = "\n".join(m.content for m in thread.sent if m.content)
        self.assertIn("pago de scout", text)

    def test_edit_panel_has_bolsitas_and_scout_buttons_for_ava_only(self):
        self.create_ava()
        _s, thread = self.split_and_thread()
        i = self.inter(self.council, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        labels = [c.label for c in i.response.sent[0][1]["view"].children]
        self.assertIn("Bolsitas", labels)
        self.assertIn("Agregar scouts", labels)
        self.assertNotIn("Agregar looters", labels)
        self.create()
        i = self.inter(self.council, self.bot.channels.get(sdb.get_split(self.db, 2).thread_id) or thread)
        run(self.cog.editar.callback(self.cog, i, 2))
        labels = [c.label for c in i.response.sent[0][1]["view"].children]
        self.assertIn("Bolsitas", labels)
        self.assertIn("Agregar looters", labels)

    def test_bolsitas_and_scout_pay_edits(self):
        self.create_ava(total="1.000")
        run(self.cog.apply_bolsitas(self.inter(self.council), 1, "250"))
        run(self.cog.apply_amounts(self.inter(self.council), 1, "1.000", "0", "20", "5", "300"))
        s = sdb.get_split(self.db, 1)
        self.assertEqual((s.bolsitas, s.scout_pay), (250, 300))
        i = self.inter(self.council)
        run(self.cog.apply_bolsitas(i, 1, "abc"))
        self.assertIn("❌", i.text)
        run(self.cog.apply_user_action(self.inter(self.council), 1, "add_scouts", [self.bob.id]))
        self.assertEqual(sdb.get_split(self.db, 1).scouts, [self.bob.id])


    def test_full_reajuste_flow_from_discord(self):
        self.create(rol=self.r_split, total="3000")
        s, thread = self.split_and_thread()
        panel = self.panelch.sent[0]
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1125)                           # 3000 − 25 % = 2250 / 2
        # /editar sobre un split cerrado: solo Council o administradores, y ofrece reabrir
        i = self.inter(self.alice, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        self.assertIn("Council", i.text)
        i = self.inter(self.admin, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        self.assertIn("ya está cerrado", i.text)
        i = self.inter(self.council, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        reopen = i.response.sent[0][1]["view"]
        j = self.inter(self.council)
        run(reopen.children[0].callback(j))
        self.assertTrue(sdb.get_split(self.db, 1).is_editing)
        self.assertEqual([c.custom_id for c in panel.view.children], ["split:confirm_edit", "split:cancel_edit", "split:edit"])
        self.assertIn("En edición", panel.embeds[0].description)
        # ahora /editar abre el panel normal y los cambios no mueven plata
        i = self.inter(self.council, thread)
        run(self.cog.editar.callback(self.cog, i, None))
        run(self.cog.apply_amounts(self.inter(self.council), 1, "5.000", "0", "20", "5", "10"))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1125)
        # confirmar edición: pide confirmación y reparte de nuevo
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        self.assertIn("resta", i.text)
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1875)                           # 5000 − 25 % = 3750 / 2
        self.assertEqual(sdb.get_split(self.db, 1).pay_round, 2)
        text = "\n".join(m.content for m in thread.sent if m.content)
        self.assertIn("reajustado", text)
        self.assertEqual([c.custom_id for c in panel.view.children], ["split:edit"])

    def test_cancel_edit_button(self):
        self.create(rol=self.r_split, total="3000")
        s, thread = self.split_and_thread()
        panel = self.panelch.sent[0]
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        sdb.reopen(self.db, 1, 1)
        sdb.update_amounts(self.db, 1, total=1)
        run(self.cog.sync_split(1))
        i = self.inter(self.alice, message=panel)
        run(self.cog.on_cancel_edit_pressed(i))
        self.assertIn("Council", i.text)
        i = self.inter(self.council, message=panel)
        run(self.cog.on_cancel_edit_pressed(i))
        s = sdb.get_split(self.db, 1)
        self.assertEqual((s.status, s.total), ("closed", 3000))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1125)

    def test_edit_button_opens_edit_panel_without_slash_command(self):
        self.create(rol=self.r_split, total="3000", looters=f"<@{self.admin.id}>")
        panel = self.panelch.sent[0]
        i = self.inter(self.alice, message=panel)                       # una persona común no puede
        run(self.cog.on_edit_pressed(i))
        self.assertIn("Council", i.text)
        i = self.inter(self.council, message=panel)
        run(self.cog.on_edit_pressed(i))
        view = i.response.sent[0][1]["view"]
        self.assertIn("Montos y taxes", [c.label for c in view.children])
        self.assertIn("Agregar looters", [c.label for c in view.children])
        self.assertIn("Editando", i.text)
        # ya cerrado: el botón ofrece reabrir (solo Council)
        c = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(c))
        run(c.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        i = self.inter(self.alice, message=panel)
        run(self.cog.on_edit_pressed(i))
        self.assertIn("Council", i.text)
        i = self.inter(self.admin, message=panel)
        run(self.cog.on_edit_pressed(i))
        self.assertIn("ya está cerrado", i.text)
        i = self.inter(self.council, message=panel)
        run(self.cog.on_edit_pressed(i))
        self.assertEqual([c.label for c in i.response.sent[0][1]["view"].children], ["Reabrir para editar"])

    def test_old_closed_panels_get_the_edit_button_on_ready(self):
        self.create(rol=self.r_split, total="3000")
        panel = self.panelch.sent[0]
        i = self.inter(self.council, message=panel)
        run(self.cog.on_close_pressed(i))
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        panel.view = None                                            # como un panel cerrado antes del botón
        panel.components = []
        run(self.cog.on_ready())
        self.assertEqual([c.custom_id for c in panel.view.children], ["split:edit"])

    def test_killboard_button_adds_guild_players_found_in_discord(self):
        self.create(total="1000")
        view, s, thread = self.edit_panel()
        calls = []

        async def fake_lookup(guild, text, guild_name):
            calls.append((text, guild_name))
            return {"battles": 2, "players": ["Alice", "Bobby", "Ghost", "Dup"], "found": [("Alice", self.alice), ("Bobby", self.bob)],
                    "not_found": ["Ghost"], "ambiguous": [("Dup", [self.admin, self.council])]}

        sp.set_albion_lookup(fake_lookup)
        try:
            i = self.press(view, "Agregar killboard")
            modal = i.modals[0]
            self.assertEqual(modal.guild_in.default, "Smogg")
            modal.link_in.value, modal.guild_in.value = "https://albionbb.com/battles/123", "Smogg"
            j = self.inter(self.council)
            run(modal.on_submit(j))
            self.assertEqual(calls, [("https://albionbb.com/battles/123", "Smogg")])
            self.assertEqual(sdb.get_split(self.db, 1).people, [self.alice.id, self.bob.id])
            self.assertIn("Agregados a la lista: **2**", j.text)
            self.assertIn("`Ghost`", j.text)
            self.assertIn("Ambiguos", j.text)
            self.assertIn("Alice", thread.sent[0].embeds[0].description)                   # el hilo se actualizó
            j = self.inter(self.council)                                                   # repetir no duplica
            run(modal.on_submit(j))
            self.assertIn("**0**", j.text)
            self.assertEqual(len(sdb.get_split(self.db, 1).people), 2)

            async def failing(guild, text, guild_name):
                return {"error": "No encontré ningún ID de batalla en ese link."}
            sp.set_albion_lookup(failing)
            j = self.inter(self.council)
            run(modal.on_submit(j))
            self.assertIn("No encontré ningún ID", j.text)
        finally:
            sp.set_albion_lookup(None)


if __name__ == "__main__":
    unittest.main()
