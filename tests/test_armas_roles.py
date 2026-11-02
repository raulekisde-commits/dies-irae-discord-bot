import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402,F401  (instala el stub de discord)
import armas_roles as ar  # noqa: E402
from armas_roles_default import DEFAULT_WEAPONS


class T(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        ar.init_schema(self.db)

    def test_default_cargado(self):
        self.assertEqual(len(ar.list_rules(self.db, 1)), len(DEFAULT_WEAPONS))
        self.assertEqual(ar.role_for(self.db, 1, "2H_MACE"), "tank")
        self.assertEqual(ar.role_for(self.db, 1, "T8_2H_MACE@2"), "tank")
        self.assertEqual(ar.role_for(self.db, 1, "2H_HOLYSTAFF"), "healer")

    def test_sin_regla_y_desconocida(self):
        self.assertIsNone(ar.role_for(self.db, 1, "2H_SHAPESHIFTER_SET1"))
        self.assertIsNone(ar.role_for(self.db, 1, "NO_EXISTE"))

    def test_editar_no_se_pisa_y_es_por_servidor(self):
        self.assertEqual(ar.set_role(self.db, 1, "2H_MACE", " DPS ")["role"], "dps")
        self.assertEqual(ar.role_for(self.db, 1, "2H_MACE"), "dps")
        self.assertEqual(ar.role_for(self.db, 2, "2H_MACE"), "tank")
        self.assertEqual(ar.role_for(self.db, 1, "2H_MACE"), "dps")      # releer no re-siembra
        self.assertIsNone(ar.set_role(self.db, 1, "NO_EXISTE", "tank"))

    def test_quitar_y_restablecer(self):
        ar.set_role(self.db, 1, "2H_MACE", "")
        self.assertIsNone(ar.role_for(self.db, 1, "2H_MACE"))
        ar.reset_defaults(self.db, 1)
        self.assertEqual(ar.role_for(self.db, 1, "2H_MACE"), "tank")

    def test_buscar_y_resumen(self):
        rules = ar.list_rules(self.db, 1)
        self.assertEqual(ar.find_weapon(rules, "heavy mace")["weapon_id"], "2H_MACE")
        self.assertEqual(ar.find_weapon(rules, "Gran bastón sagrado")["weapon_id"], "2H_HOLYSTAFF")
        lines = ar.summary_lines(rules, "tank")
        self.assertEqual(len(lines), 1)
        for p in ar._chunks(ar.summary_lines(rules)):
            self.assertLessEqual(len(p), 2000)


if __name__ == "__main__":
    unittest.main()


class TDiscord(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        ar.init_schema(self.db)

    def test_evensong_supp(self):
        self.assertEqual(ar.role_for(self.db, 1, "2H_ARCANE_RINGPAIR_AVALON"), "supp")

    def test_mapeo_discord(self):
        ar.seed_discord_roles(self.db, 1, {"tank": 11, "SUPP": 22, "dps": 0})
        self.assertEqual(ar.discord_role_for(self.db, 1, "Tank"), 11)
        self.assertEqual(ar.discord_role_for(self.db, 1, "supp"), 22)
        self.assertIsNone(ar.discord_role_for(self.db, 1, "dps"))
        ar.set_discord_role(self.db, 1, "tank", 99)
        ar.seed_discord_roles(self.db, 1, {"tank": 11})                   # no pisa lo configurado
        self.assertEqual(ar.discord_role_for(self.db, 1, "tank"), 99)
        ar.set_discord_role(self.db, 1, "tank", None)
        ar.seed_discord_roles(self.db, 2, {"tank": 11})
        self.assertIsNone(ar.discord_role_for(self.db, 1, "tank"))
        self.assertEqual(ar.discord_role_for(self.db, 2, "tank"), 11)
        lines = ar.summary_lines(ar.list_rules(self.db, 1), "supp", {"supp": 22})
        self.assertIn("<@&22>", lines[0])
