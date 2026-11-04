import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import discord_stub  # noqa: E402
discord_stub.install()
import economy_db as eco  # noqa: E402
import embo_db  # noqa: E402
import embo_tickets_db as etdb  # noqa: E402
import embo_tickets  # noqa: E402,F401

G, U, COUNCIL = 1, 10, 20


class EmboTicketsDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        eco.init_schema(self.db)
        etdb.init_schema(self.db)
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[U], amount=1000, request_key="f1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_open_ticket_per_user(self):
        tid = etdb.create_ticket(self.db, G, 100, U, "buy", 5)
        self.assertEqual(etdb.open_ticket_of(self.db, G, U).id, tid)
        etdb.close_ticket(self.db, tid, COUNCIL, "cancelled")
        self.assertIsNone(etdb.open_ticket_of(self.db, G, U))
        self.assertFalse(etdb.close_ticket(self.db, tid, COUNCIL, "cancelled"))       # ya cerrado

    def test_settle_charges_wuju_and_adds_embo_once(self):
        tid = etdb.create_ticket(self.db, G, 100, U, "buy", 5)
        r = etdb.settle(self.db, tid, COUNCIL, 5, 100)
        self.assertEqual((r.status, r.total, r.wuju_before, r.wuju_after, r.embo_before, r.embo_after), ("ok", 500, 1000, 500, 0, 5))
        self.assertEqual(eco.get_balance(self.db, U), 500)
        self.assertEqual(embo_db.get_balance(self.db, U), 5)
        self.assertEqual(etdb.get_ticket(self.db, tid).status, "closed")
        self.assertEqual(etdb.settle(self.db, tid, COUNCIL, 5, 100).status, "closed")    # doble clic: no cobra dos veces
        self.assertEqual(eco.get_balance(self.db, U), 500)

    def test_settle_pays_debt(self):
        embo_db.apply(self.db, user_id=U, mode="remove", amount=8, actor_id=1, request_key="d", reason="deuda")
        tid = etdb.create_ticket(self.db, G, 100, U, "debt", 8)
        r = etdb.settle(self.db, tid, COUNCIL, 8, 50)
        self.assertEqual((r.status, r.embo_before, r.embo_after, r.wuju_after), ("ok", -8, 0, 600))

    def test_settle_without_funds_moves_nothing(self):
        tid = etdb.create_ticket(self.db, G, 100, U, "buy", 20)
        r = etdb.settle(self.db, tid, COUNCIL, 20, 100)
        self.assertEqual((r.status, r.total, r.wuju_before), ("funds", 2000, 1000))
        self.assertEqual(eco.get_balance(self.db, U), 1000)
        self.assertEqual(embo_db.get_balance(self.db, U), 0)
        self.assertEqual(etdb.get_ticket(self.db, tid).status, "open")                # el cog lo cierra después

    def test_invalid_amounts(self):
        tid = etdb.create_ticket(self.db, G, 100, U, "buy", 5)
        self.assertEqual(etdb.settle(self.db, tid, COUNCIL, 0, 100).status, "invalid")
        self.assertEqual(etdb.settle(self.db, tid, COUNCIL, 5, 0).status, "invalid")
        with self.assertRaises(etdb.EmboTicketError):
            etdb.parse_amount("abc")


if __name__ == "__main__":
    unittest.main()
