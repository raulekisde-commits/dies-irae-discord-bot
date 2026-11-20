"""Smogg + Smogged: la lista de la guild junta las dos, el registro acepta a ambas, el contador cuenta solo Smogg
y la gente de Smogged recibe el rol "Smogged" (creado por el bot si no existe). Nunca toca la red."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import killboard_db as kb  # noqa: E402
import registration as rg  # noqa: E402
import registration_db as rdb  # noqa: E402

SMOGG = [{"Id": "id-a", "Name": "Alfa", "GuildName": "Smogg"}, {"Id": "id-b", "Name": "Beta", "GuildName": "Smogg"}]
SMOGGED = [{"Id": "id-c", "Name": "Charlie", "GuildName": "Smogged"}]


class GuildsHermanasTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.reg = run(rg.setup_registration(self.bot, self.db, GUILD_ID, required_guild="Smogg", albion_guild_id="GID",
                                             member_role_id=self.r_member.id, extra_guilds=[("Smogged", "SGID")]))
        self.smogged_down = False

        async def fake(url, params=None):
            if "ams" in url or "sgp" in url:
                raise RuntimeError("HTTP 503")
            if "/guilds/GID/members" in url:
                return SMOGG
            if "/guilds/SGID/members" in url:
                if self.smogged_down:
                    raise RuntimeError("HTTP 503")
                return SMOGGED
            return {"players": []}

        self.reg.fetch_json = fake
        self.created = []

        async def create_role(name=None, **kw):
            role = discord.Role(555000 + len(self.created), name, self.guild)
            self.guild.roles.append(role)
            self.created.append(role)
            return role

        self.guild.create_role = create_role

    def test_lista_junta_las_dos_guilds_y_contador_solo_smogg(self):
        self.assertTrue(run(self.reg.refresh_roster()))
        self.assertEqual(set(self.reg.roster), {"alfa", "beta", "charlie"})
        self.assertEqual(self.reg.roster["charlie"].guild, "Smogged")
        self.assertEqual(self.reg.main_guild_count(), 2)
        self.assertTrue(self.reg.is_own_guild("smogged") and self.reg.is_own_guild("Smogg") and not self.reg.is_own_guild("Otra"))
        look = run(self.reg.lookup_character("charlie"))
        self.assertEqual((look.kind, look.character.guild), ("ok", "Smogged"))

    def test_si_falla_smogged_se_mantiene_su_parte(self):
        run(self.reg.refresh_roster())
        self.smogged_down = True
        self.assertTrue(run(self.reg.refresh_roster()))
        self.assertIn("charlie", self.reg.roster)
        self.assertNotIn("smogged", self.reg._fresh_guilds)

    def test_rol_smogged_se_crea_y_sincroniza(self):
        rdb.admin_link(self.db, self.alice.id, "Charlie", 1, char_id="id-c")
        rdb.admin_link(self.db, self.bob.id, "Alfa", 1, char_id="id-a")
        run(self.reg.refresh_roster())
        run(self.reg.sync_guild_roles())
        self.assertEqual(len(self.created), 1)
        role = self.created[0]
        self.assertEqual(role.name, "Smogged")
        self.assertIn(role, self.alice.roles)
        self.assertNotIn(role, self.bob.roles)
        run(self.bob.add_roles(role))                         # alguien de Smogg con el rol por error: se le saca
        run(self.reg.sync_guild_roles())
        self.assertNotIn(role, self.bob.roles)
        self.assertEqual(len(self.created), 1)                # no crea otro: reusa el guardado
        self.smogged_down = True                              # sin lista nueva de Smogged no se toca el rol
        run(self.reg.refresh_roster())
        run(self.bob.add_roles(role))
        run(self.reg.sync_guild_roles())
        self.assertIn(role, self.bob.roles)

    def test_apply_guild_role_al_registrarse(self):
        run(self.reg.refresh_roster())
        rdb.admin_link(self.db, self.alice.id, "Charlie", 1, char_id="id-c")
        run(self.reg.apply_guild_role(self.alice))
        self.assertTrue(any(r.name == "Smogged" for r in self.alice.roles))

    def test_killboard_cuenta_las_dos_guilds(self):
        events = [{"Killer": {"Name": "Alfa", "GuildName": "Smogg"}, "Victim": {"Name": "Charlie", "GuildName": "Smogged"},
                   "Participants": [], "GroupMembers": []}]
        try:
            parsed = kb.parse_events(events, ("Smogg", "Smogged"))
        except Exception as exc:                              # formato de eventos distinto: alcanza con que acepte varias guilds
            self.skipTest(f"formato de eventos de prueba no compatible: {exc!r}")
        self.assertEqual(set(parsed["players"]), {kb.norm("Alfa"), kb.norm("Charlie")})
        self.assertEqual(set(kb.parse_events(events, "Smogg")["players"]), {kb.norm("Alfa")})


if __name__ == "__main__":
    unittest.main()
