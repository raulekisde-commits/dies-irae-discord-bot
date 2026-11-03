import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import test_give_deposits  # noqa: E402,F401  (instala el stub de discord)
import economy  # noqa: E402


class EcoGroupTests(unittest.TestCase):
    def test_admin_de_economia_vive_bajo_eco(self):
        g = economy.EconomyCog.eco_g
        self.assertEqual(g.name, "eco")
        self.assertEqual(sorted(g.commands), sorted([
            "addmoney", "addmoney-role", "remove-money", "config-moneda", "bal-reintentar-registro", "perms-canal",
            "perms-comando", "perms-ver", "importar-bal", "set-balance", "eco-stats", "historico-bal", "embo"]))

    def test_permisos_siguen_guardados_con_el_mismo_texto(self):
        # los permisos delegados se guardan por clave de texto: agrupar no los pierde
        import economy_db as edb
        for key in ("addmoney", "remove-money", "eco-stats"):
            self.assertIn(key, edb.GRANTABLE_COMMANDS)
        self.assertNotIn("set-balance", edb.GRANTABLE_COMMANDS)          # /eco set-balance es solo Admin y no se delega


if __name__ == "__main__":
    unittest.main()
