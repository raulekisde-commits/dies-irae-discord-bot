"""E6: /list_role lo pueden usar Staff, Council y administradores; staff_only_slash (solo Staff) no cambia."""
import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, STAFF_ROLE  # noqa: E402  (instala el stub de discord)
import discord  # noqa: E402

ROOT = os.path.dirname(HERE)
with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as _f:
    _SRC = _f.read()
_TREE = ast.parse(_SRC)


def _source(name):
    for n in _TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(_SRC, n)
    raise AssertionError(f"no encuentro {name} en bot.py")


class _AppCommands:
    @staticmethod
    def check(predicate):
        return predicate


def _load():
    """Ejecuta el código REAL de los chequeos de bot.py (sin importar bot.py entero)."""
    ns = {"discord": discord, "app_commands": _AppCommands, "STAFF_ROLE_ID": STAFF_ROLE}
    for name in ("is_staff_member", "is_council_or_admin", "is_staff_or_council", "staff_only_slash", "staff_council_admin_slash"):
        exec(_source(name), ns)
    return ns


class ListRolePermisos(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(STAFF_ROLE, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.staff = discord.Member(33 + 100000000000000000, "Staffer", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.staff)
        ns = _load()
        self.new_check = ns["staff_council_admin_slash"]()
        self.old_check = ns["staff_only_slash"]()

    def allowed(self, check, user):
        return run(check(self.inter(user)))

    def test_list_role_acepta_staff_council_y_admin(self):
        for user in (self.staff, self.council, self.admin):
            self.assertTrue(self.allowed(self.new_check, user), user.display_name)

    def test_list_role_rechaza_a_los_demas(self):
        for user in (self.alice, self.outsider):
            self.assertFalse(self.allowed(self.new_check, user), user.display_name)

    def test_staff_only_slash_sigue_solo_para_staff(self):
        self.assertTrue(self.allowed(self.old_check, self.staff))
        for user in (self.council, self.admin, self.alice):
            self.assertFalse(self.allowed(self.old_check, user), user.display_name)

    def test_list_role_usa_el_decorador_nuevo(self):
        usado = []
        for n in _TREE.body:
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_role_slash":
                usado = [ast.unparse(d) for d in n.decorator_list]
        self.assertIn("staff_council_admin_slash()", usado)
        self.assertNotIn("staff_only_slash()", usado)


if __name__ == "__main__":
    unittest.main()
