import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402
import discord  # noqa: E402
import diagnostico as dg  # noqa: E402
import registration as rg  # noqa: E402
import registration_db as rdb  # noqa: E402


class Perms:
    def __init__(self, **kw):
        self.administrator = False
        self.__dict__.update(kw)


class DiagnoseTests(unittest.TestCase):
    def member(self, roles=(1,), **kw):
        m = type("M", (), {})()
        m.id, m.roles, m.display_name = 5, [type("R", (), {"id": r})() for r in roles], "Pepe"
        m.__dict__.update(kw)
        return m

    def chan(self, **perms):
        c = type("C", (), {})()
        c.permissions_for = lambda who: Perms(**perms)
        return c

    def test_finds_the_denied_application_commands_permission(self):
        items = dg.diagnose(self.member(), self.chan(view_channel=True, use_application_commands=False), member_role_id=1, registered=True, command_count=60)
        bad = [t for ok, t in items if not ok]
        self.assertEqual(len(bad), 1)
        self.assertIn("Usar comandos de aplicación", bad[0])
        self.assertIn("DENEGADO", bad[0])

    def test_finds_pending_screening_timeout_missing_role_and_registration(self):
        items = dg.diagnose(self.member(roles=(), pending=True, timed_out_until="x"), self.chan(view_channel=True, use_application_commands=True),
                            member_role_id=1, registered=False, command_count=None)
        bad = " ".join(t for ok, t in items if not ok)
        for piece in ("NO tiene el rol", "NO está registrado", "reglas del servidor", "aislado"):
            self.assertIn(piece, bad)

    def test_all_good_and_command_limit_warning(self):
        items = dg.diagnose(self.member(), self.chan(view_channel=True, use_application_commands=True), member_role_id=1, registered=True, command_count=60)
        self.assertTrue(all(ok for ok, _ in items))
        near = dg.diagnose(self.member(), None, member_role_id=1, registered=True, command_count=97)
        self.assertFalse(all(ok for ok, _ in near))


class CommandTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(dg.setup_diagnostico(self.bot, self.db, 1))

    def test_command_works_for_unregistered_and_is_exempt_from_the_gate(self):
        self.assertIn("diagnostico", rg.EXEMPT_COMMANDS)
        rdb.init_schema(self.db)
        i = self.inter(self.alice)
        run(self.cog.diagnostico.callback(self.cog, i, None))
        self.assertIn("Diagnóstico de", i.text)
        self.assertIn("NO está registrado", i.text)
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1)
        i = self.inter(self.alice)
        run(self.cog.diagnostico.callback(self.cog, i, None))
        self.assertIn("Está **registrado**", i.text)
        self.assertIn("Integraciones", i.text)                                  # sin problemas propios: muestra los consejos del lado de Discord

    def test_only_staff_can_diagnose_someone_else(self):
        i = self.inter(self.alice)
        run(self.cog.diagnostico.callback(self.cog, i, self.bob))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.diagnostico.callback(self.cog, i, self.bob))
        self.assertIn("Diagnóstico de", i.text)


if __name__ == "__main__":
    unittest.main()
