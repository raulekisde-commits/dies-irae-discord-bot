"""E2: /set-balance es solo del propietario/Administrator y ya no se puede delegar con /perms-comando."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402  (instala el stub de discord)
import economy_db as eco  # noqa: E402


class SetBalanceSoloAdmin(DiscordBase):
    def setbal(self, user, target, amount):
        i = self.inter(user)
        run(self.ecog.set_balance_cmd.callback(self.ecog, i, target, amount))
        return i

    def test_ya_no_es_delegable(self):
        self.assertNotIn("set-balance", eco.GRANTABLE_COMMANDS)
        self.assertNotIn("set-balance", eco.EXTRA_PERMISSION_COMMANDS)

    def test_admin_puede(self):
        self.setbal(self.admin, self.alice, "777")
        self.assertEqual(self.bal(self.alice), 777)

    def test_council_sin_administrator_no_puede(self):
        i = self.setbal(self.council, self.alice, "777")
        self.assertEqual(self.bal(self.alice), 0)
        self.assertIn("propietario del servidor o administradores", i.text)

    def test_miembro_comun_no_puede(self):
        self.setbal(self.bob, self.alice, "777")
        self.assertEqual(self.bal(self.alice), 0)

    def test_rol_delegado_antes_queda_sin_efecto(self):
        # Fila vieja en eco_command_roles (de antes del cambio): el código ya no la consulta para este comando.
        eco.grant_command_role(self.db, "set-balance", self.r_member.id)
        i = self.setbal(self.alice, self.bob, "777")
        self.assertEqual(self.bal(self.bob), 0)
        self.assertIn("propietario del servidor o administradores", i.text)

    def test_otros_comandos_delegables_siguen(self):
        for cmd in ("give-money", "eco-stats", "verhistorial", "stats"):
            self.assertIn(cmd, eco.GRANTABLE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
