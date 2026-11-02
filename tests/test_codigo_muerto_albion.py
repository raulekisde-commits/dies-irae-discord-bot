"""E11: constantes de Albion que no usaba nadie (se verificó antes de borrarlas). Esta prueba evita que vuelvan sin querer."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BORRADAS = ["_ALBION_GAMEINFO" + "_BY_REGION", "ALBION_GAMEINFO" + "_BASE", "ALBION_GAMEINFO" + "_ALL",
            "ALBION_TIMEOUT" + "_SECONDS", "ALBION_RETRIES", "ALBION_ROSTER_CACHE" + "_SECONDS", "_albion_roster" + "_cache"]


class CodigoMuertoAlbion(unittest.TestCase):
    def test_no_quedan_referencias(self):
        for name in sorted(os.listdir(ROOT)):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                    src = f.read()
                for viejo in BORRADAS:
                    self.assertNotIn(viejo, src, f"{viejo} en {name}")

    def test_lo_que_si_se_usa_sigue(self):
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
            src = f.read()
        for vivo in ("ALBIONBB_BASE_URL", "ALBIONBB_BATTLE_URL", "ALBIONBB_KILLS_URL", "ALBION_GUILD_NAME", "ALBION_GUILD_ID"):
            self.assertIn(vivo, src)


if __name__ == "__main__":
    unittest.main()
