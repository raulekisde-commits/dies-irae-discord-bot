import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import economy_db as eco  # noqa: E402


class AmountTests(unittest.TestCase):
    def test_valid_inputs(self):
        for text in ["150000", "150.000", "150,000", "1.500.000", "1,500,000", "+150000", " 150000 "]:
            self.assertIn(eco.parse_amount(text), (150000, 1500000), text)
        self.assertEqual(eco.parse_amount("150.000"), 150000)
        self.assertEqual(eco.parse_amount("1,500,000"), 1500000)
        self.assertEqual(eco.parse_amount("999"), 999)

    def test_rejects_malformed(self):
        for text in ["", "abc", "1.5", "150.00", "1,50", "1.500,000", "1,500.000", "1..000", "12 345",
                     "1e5", "0x10", "007", "1.0000", ".500", "1.", "$100", "10.000.00"]:
            with self.assertRaises(eco.AmountError, msg=text):
                eco.parse_amount(text)

    def test_rejects_zero_negative_and_huge(self):
        for text in ["0", "-5", "-150.000", "0.000"]:
            with self.assertRaises(eco.AmountError, msg=text):
                eco.parse_amount(text)
        with self.assertRaises(eco.AmountError):
            eco.parse_amount(str(eco.MAX_AMOUNT + 1))
        self.assertEqual(eco.parse_amount(str(eco.MAX_AMOUNT)), eco.MAX_AMOUNT)

    def test_format_separates_thousands_with_commas(self):
        self.assertEqual(eco.format_amount(150000), "150,000")
        self.assertEqual(eco.format_amount(1500000), "1,500,000")
        self.assertEqual(eco.format_amount(-150000), "-150,000")
        self.assertEqual(eco.format_amount(0), "0")
        self.assertEqual(eco.format_amount(999), "999")
        self.assertNotIn(".", eco.format_amount(123456789))

    def test_roundtrip(self):
        for n in [1, 999, 1000, 123456, 1234567, 10 ** 15]:
            self.assertEqual(eco.parse_amount(eco.format_amount(n)), n)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.sqlite3")
        eco.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, kind, users, amount, key, role_id=None, actor=1, **kw):
        return eco.apply_operation(self.db, kind=kind, actor_id=actor, user_ids=users, amount=amount,
                                   request_key=key, role_id=role_id, **kw)


class BalanceTests(DbTestCase):
    def test_nonexistent_balance_is_zero_and_creates_nothing(self):
        self.assertEqual(eco.get_balance(self.db, 12345), 0)
        self.assertEqual(eco.get_all_balances(self.db), {})

    def test_add_creates_account(self):
        r = self.op("add", [10], 150000, "k1")
        self.assertTrue(r.created)
        self.assertEqual(r.movements[0].before, 0)
        self.assertEqual(r.movements[0].after, 150000)
        self.assertEqual(eco.get_balance(self.db, 10), 150000)

    def test_remove_can_go_negative(self):
        self.op("add", [10], 1000, "k1")
        r = self.op("remove", [10], 25000, "k2")
        self.assertEqual(r.movements[0].before, 1000)
        self.assertEqual(r.movements[0].after, -24000)
        self.assertEqual(eco.get_balance(self.db, 10), -24000)
        # y un usuario sin cuenta también puede quedar negativo
        r = self.op("remove", [11], 5, "k3")
        self.assertEqual(eco.get_balance(self.db, 11), -5)

    def test_add_to_negative_balance(self):
        self.op("remove", [10], 100, "k1")
        r = self.op("add", [10], 30, "k2")
        self.assertEqual((r.movements[0].before, r.movements[0].after), (-100, -70))

    def test_role_operation_each_member_gets_amount(self):
        members = list(range(100, 200))
        r = self.op("add", members, 50000, "role1", role_id=555)
        self.assertEqual(r.scope, "role")
        self.assertEqual(len(r.movements), 100)
        self.assertEqual(r.per_user_amount, 50000)
        self.assertEqual(r.total_amount, 5_000_000)
        self.assertTrue(all(eco.get_balance(self.db, u) == 50000 for u in members))
        r2 = self.op("remove", members, 25000, "role2", role_id=555)
        self.assertEqual(r2.total_amount, 2_500_000)
        self.assertTrue(all(eco.get_balance(self.db, u) == 25000 for u in members))

    def test_duplicate_users_processed_once(self):
        r = self.op("add", [7, 7, 7, 8], 10, "dup")
        self.assertEqual(len(r.movements), 2)
        self.assertEqual(eco.get_balance(self.db, 7), 10)
        self.assertEqual(r.total_amount, 20)

    def test_invalid_arguments(self):
        for bad in (0, -5, eco.MAX_AMOUNT + 1, 1.5, True):
            with self.assertRaises(ValueError):
                self.op("add", [1], bad, "x")
        with self.assertRaises(ValueError):
            self.op("add", [], 5, "x")
        with self.assertRaises(ValueError):
            self.op("add", ["1"], 5, "x")
        with self.assertRaises(ValueError):
            self.op("steal", [1], 5, "x")
        self.assertEqual(eco.get_all_balances(self.db), {})

    def test_overflow_is_rejected_and_rolled_back(self):
        self.op("add", [1], eco.MAX_AMOUNT, "a")
        # llevar el balance cerca del tope absoluto con operaciones normales sería lento: se fuerza por SQL
        import dbutil
        conn = dbutil.connect(self.db)
        conn.execute("UPDATE eco_accounts SET balance = ? WHERE user_id = 1", (eco.MAX_BALANCE_ABS - 1,))
        conn.close()
        with self.assertRaises(OverflowError):
            self.op("add", [1], 5, "b")
        self.assertEqual(eco.get_balance(self.db, 1), eco.MAX_BALANCE_ABS - 1)


class AtomicityTests(DbTestCase):
    def test_failure_in_mass_operation_leaves_no_partial_changes(self):
        self.op("add", [1, 2], 100, "seed")

        def boom(index, uid):
            if index == 3:
                raise RuntimeError("fallo simulado")

        with self.assertRaises(RuntimeError):
            self.op("add", [1, 2, 3, 4, 5, 6], 500, "mass", role_id=9, _test_hook=boom)

        self.assertEqual(eco.get_balance(self.db, 1), 100)
        self.assertEqual(eco.get_balance(self.db, 2), 100)
        for uid in (3, 4, 5, 6):
            self.assertEqual(eco.get_balance(self.db, uid), 0)
        self.assertEqual(set(eco.get_all_balances(self.db)), {1, 2})  # ni cuentas nuevas
        self.assertIsNone(self._operation_exists("mass"))
        # y la solicitud se puede reintentar limpiamente
        r = self.op("add", [1, 2, 3, 4, 5, 6], 500, "mass", role_id=9)
        self.assertTrue(r.created)
        self.assertEqual(eco.get_balance(self.db, 6), 500)

    def _operation_exists(self, key):
        import dbutil
        conn = dbutil.connect(self.db)
        try:
            return conn.execute("SELECT 1 FROM eco_operations WHERE request_key = ?", (key,)).fetchone()
        finally:
            conn.close()

    def test_same_request_key_is_never_applied_twice(self):
        r1 = self.op("add", [5], 1000, "msg:42")
        r2 = self.op("add", [5], 1000, "msg:42")
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)
        self.assertEqual(r1.op_id, r2.op_id)
        self.assertEqual(eco.get_balance(self.db, 5), 1000)

    def test_balance_equals_sum_of_movements(self):
        import dbutil
        for i in range(20):
            self.op("add" if i % 3 else "remove", [1, 2], 10 + i, f"k{i}")
        conn = dbutil.connect(self.db)
        try:
            for uid in (1, 2):
                total = conn.execute("SELECT SUM(delta) s FROM eco_movements WHERE user_id = ?", (uid,)).fetchone()["s"]
                self.assertEqual(total, eco.get_balance(self.db, uid))
                last = conn.execute("SELECT balance_after FROM eco_movements WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
                self.assertEqual(last["balance_after"], eco.get_balance(self.db, uid))
        finally:
            conn.close()

    def test_concurrent_modifications_same_account_all_counted(self):
        errors = []

        def worker(i):
            try:
                self.op("add", [777], 10, f"c{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(eco.get_balance(self.db, 777), 400)

    def test_concurrent_add_and_remove_mix(self):
        def worker(i):
            self.op("add" if i % 2 == 0 else "remove", [888], 7, f"m{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(eco.get_balance(self.db, 888), 0)

    def test_concurrent_same_request_key_applies_once(self):
        results = []

        def worker():
            results.append(self.op("add", [999], 100, "same-key"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r.created), 1)
        self.assertEqual(eco.get_balance(self.db, 999), 100)


class PersistenceTests(DbTestCase):
    def test_data_survives_reconnect_and_reinit(self):
        self.op("add", [1], 500, "a")
        eco.set_setting(self.db, "currency_emoji", "<:wuju:123456789012345678>")
        eco.grant_command_role(self.db, "addmoney", 42)
        eco.set_command_channel(self.db, "bal", 777)
        # "reinicio": volver a inicializar el esquema no borra nada
        eco.init_schema(self.db)
        eco.init_schema(self.db)
        self.assertEqual(eco.get_balance(self.db, 1), 500)
        self.assertEqual(eco.get_setting(self.db, "currency_emoji"), "<:wuju:123456789012345678>")
        self.assertEqual(eco.get_command_roles(self.db, "addmoney"), {42})
        self.assertEqual(eco.get_command_channel(self.db, "bal"), 777)
        self.assertEqual(len(eco.get_operation(self.db, self._first_op()).movements), 1)

    def _first_op(self):
        import dbutil
        conn = dbutil.connect(self.db)
        try:
            return conn.execute("SELECT op_id FROM eco_operations").fetchone()["op_id"]
        finally:
            conn.close()


class LogRetryTests(DbTestCase):
    def test_pending_then_partial_then_sent(self):
        r = self.op("add", list(range(1, 50)), 10, "role", role_id=3)
        self.assertEqual(eco.pending_log_operations(self.db), [r.op_id])
        eco.set_log_progress(self.db, r.op_id, 1, done=False, failed=True)
        got = eco.get_operation(self.db, r.op_id)
        self.assertEqual((got.log_status, got.log_pages_sent), ("failed", 1))
        self.assertEqual(eco.pending_log_operations(self.db), [r.op_id])  # sigue pendiente de reintento
        eco.set_log_progress(self.db, r.op_id, 3, done=True)
        self.assertEqual(eco.pending_log_operations(self.db), [])
        # el balance no se tocó por reintentar el log
        self.assertEqual(eco.get_balance(self.db, 5), 10)


class TopTests(unittest.TestCase):
    def test_order_ties_and_limit(self):
        balances = {1: 500, 2: 500, 3: -20, 4: 900, 5: 0}
        top = eco.rank_top(balances, [1, 2, 3, 4, 5, 6], limit=10)
        self.assertEqual([(p, u, b) for p, u, b in top],
                         [(1, 4, 900), (2, 1, 500), (3, 2, 500), (4, 5, 0), (5, 6, 0), (6, 3, -20)])

    def test_only_eligible_no_duplicates_and_cap_10(self):
        balances = {i: i * 10 for i in range(1, 30)}
        top = eco.rank_top(balances, list(range(1, 25)) + [3, 3, 4], limit=10)
        self.assertEqual(len(top), 10)
        self.assertEqual([u for _, u, _ in top], list(range(24, 14, -1)))
        self.assertEqual(len({u for _, u, _ in top}), 10)

    def test_excludes_non_eligible_accounts(self):
        top = eco.rank_top({1: 100, 2: 999}, [1], limit=10)  # 2 es un bot / no es miembro
        self.assertEqual(top, [(1, 1, 100)])

    def test_fewer_than_ten(self):
        self.assertEqual(len(eco.rank_top({}, [1, 2, 3])), 3)
        self.assertEqual(eco.rank_top({}, []), [])


class AuthorizeTests(unittest.TestCase):
    def auth(self, **kw):
        base = dict(command="addmoney", is_admin=False, member_role_ids={1}, allowed_role_ids={2},
                    channel_id=10, configured_channel_id=10)
        base.update(kw)
        return eco.authorize(**base)

    def test_member_without_role_denied(self):
        self.assertFalse(self.auth().ok)

    def test_authorized_role_in_right_channel(self):
        self.assertTrue(self.auth(member_role_ids={2}).ok)

    def test_authorized_role_wrong_channel_denied(self):
        r = self.auth(command="bal", member_role_ids={2}, channel_id=99)
        self.assertFalse(r.ok)
        self.assertIn("<#10>", r.reason)

    def test_unconfigured_channel_disabled_for_everyone_including_admin(self):
        self.assertFalse(self.auth(command="bal", member_role_ids={2}, configured_channel_id=None).ok)
        self.assertFalse(self.auth(command="dados", is_admin=True, configured_channel_id=None).ok)

    def test_top_and_stats_default_to_any_channel_until_configured(self):
        for cmd in ("stats",):
            self.assertTrue(self.auth(command=cmd, member_role_ids={2}, configured_channel_id=None, channel_id=77).ok)
            self.assertFalse(self.auth(command=cmd, member_role_ids={2}, configured_channel_id=10, channel_id=77).ok)
            self.assertFalse(self.auth(command=cmd, member_role_ids={5}, configured_channel_id=None).ok)

    def test_top_works_in_any_channel_even_if_a_channel_was_saved(self):
        self.assertTrue(self.auth(command="top", member_role_ids={2}, configured_channel_id=10, channel_id=77).ok)
        self.assertTrue(self.auth(command="top", member_role_ids={2}, configured_channel_id=None, channel_id=77).ok)
        self.assertFalse(self.auth(command="top", member_role_ids={5}, configured_channel_id=None).ok)

    def test_admin_passes_role_but_not_channel(self):
        self.assertTrue(self.auth(command="bal", is_admin=True).ok)
        self.assertFalse(self.auth(command="bal", is_admin=True, channel_id=99).ok)

    def test_money_admin_commands_work_in_any_channel_but_still_need_role(self):
        for cmd in ("addmoney", "addmoney-role", "remove-money"):
            self.assertTrue(self.auth(command=cmd, member_role_ids={2}, channel_id=99, configured_channel_id=None).ok)
            self.assertTrue(self.auth(command=cmd, is_admin=True, channel_id=99, configured_channel_id=None).ok)
            self.assertFalse(self.auth(command=cmd, member_role_ids={1}, channel_id=99, configured_channel_id=None).ok)

    def test_revocation_takes_effect_next_call(self):
        self.assertTrue(self.auth(member_role_ids={2}).ok)
        self.assertFalse(self.auth(member_role_ids={2}, allowed_role_ids=set()).ok)


class PermissionStorageTests(DbTestCase):
    def test_grant_revoke_and_channels(self):
        self.assertTrue(eco.grant_command_role(self.db, "addmoney", 5))
        self.assertFalse(eco.grant_command_role(self.db, "addmoney", 5))
        eco.grant_command_role(self.db, "addmoney", 6)
        self.assertEqual(eco.get_command_roles(self.db, "addmoney"), {5, 6})
        self.assertTrue(eco.revoke_command_role(self.db, "addmoney", 5))
        self.assertFalse(eco.revoke_command_role(self.db, "addmoney", 5))
        self.assertEqual(eco.get_command_roles(self.db, "addmoney"), {6})
        self.assertIsNone(eco.get_command_channel(self.db, "top"))
        eco.set_command_channel(self.db, "top", 1)
        eco.set_command_channel(self.db, "top", 2)
        self.assertEqual(eco.get_command_channel(self.db, "top"), 2)
        overview = eco.get_permissions_overview(self.db)
        self.assertEqual(set(overview), set(eco.ECON_COMMANDS))

    def test_settings_set_and_clear(self):
        eco.set_setting(self.db, "log_channel_id", "123")
        self.assertEqual(eco.get_setting(self.db, "log_channel_id"), "123")
        eco.set_setting(self.db, "log_channel_id", "456")
        self.assertEqual(eco.get_setting(self.db, "log_channel_id"), "456")
        eco.set_setting(self.db, "log_channel_id", None)
        self.assertIsNone(eco.get_setting(self.db, "log_channel_id"))

    def test_custom_emoji_parsing(self):
        self.assertEqual(eco.parse_custom_emoji("<:wuju:123456789012345678>"), ("<:wuju:123456789012345678>", 123456789012345678))
        self.assertEqual(eco.parse_custom_emoji("<a:wuju_anim:123456789012345678>")[1], 123456789012345678)
        for bad in ["🪙", ":wuju:", "<:w:1>", "wuju", "<:wuju:abc>", ""]:
            self.assertIsNone(eco.parse_custom_emoji(bad), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
