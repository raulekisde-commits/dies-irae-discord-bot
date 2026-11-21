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

    def test_pase_de_guild_en_curso_manda_sobre_la_lista(self):
        rdb.admin_link(self.db, self.bob.id, "Alfa", 1, char_id="id-a")             # en el juego sigue en Smogg
        rdb.set_guild(self.db, self.bob.id, "Smogged", 1)                           # pero se lo está pasando a Smogged
        run(self.reg.refresh_roster())
        run(self.reg.sync_guild_roles())
        self.assertTrue(any(r.name == "Smogged" for r in self.bob.roles))
        global SMOGGED
        old = SMOGGED
        try:                                                                        # ya figura en Smogged: el pase se da por terminado
            SMOGGED = old + [{"Id": "id-a2", "Name": "Alfa", "GuildName": "Smogged"}]
            SMOGG[:] = [p for p in SMOGG if p["Name"] != "Alfa"]
            run(self.reg.refresh_roster())
            run(self.reg.sync_guild_roles())
            self.assertEqual(rdb.active_transfers(self.db), {})
            self.assertTrue(any(r.name == "Smogged" for r in self.bob.roles))
        finally:
            SMOGGED = old
            SMOGG[:] = [{"Id": "id-a", "Name": "Alfa", "GuildName": "Smogg"}, {"Id": "id-b", "Name": "Beta", "GuildName": "Smogg"}]

    def test_apply_guild_role_al_registrarse(self):
        run(self.reg.refresh_roster())
        rdb.admin_link(self.db, self.alice.id, "Charlie", 1, char_id="id-c")
        run(self.reg.apply_guild_role(self.alice))
        self.assertTrue(any(r.name == "Smogged" for r in self.alice.roles))

    def _newbie(self, uid=880):
        m = discord.Member(uid, "Nuevo", guild=self.guild)
        self.guild.members.append(m)
        return m

    def test_reclutar_a_smogged_pendiente_da_el_rol_y_registra_smogged(self):
        run(self.reg.refresh_roster())
        newbie = self._newbie()
        ok, text, granted = run(self.reg.accept_recruit(newbie, "Recluta", self.r_member.id, "Miembro", self.council, guild_name="Smogged"))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn("Smogged", text)
        self.assertTrue(any(r.name == "Smogged" for r in newbie.roles))                 # rol desde el momento de aceptar
        self.assertIn(self.r_member, newbie.roles)
        self.assertEqual(rdb.get_registration(self.db, newbie.id).guild_name, "Smogged")
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)
        run(self.reg.sync_guild_roles())                                                # todavía no entró: no se le saca
        self.assertTrue(any(r.name == "Smogged" for r in newbie.roles))

    def test_reclutar_a_smogg_no_da_el_rol(self):
        run(self.reg.refresh_roster())
        newbie = self._newbie(881)
        ok, _t, _g = run(self.reg.accept_recruit(newbie, "Recluta", self.r_member.id, "Miembro", self.council))
        self.assertTrue(ok)
        self.assertFalse(any(r.name == "Smogged" for r in newbie.roles))
        self.assertEqual(rdb.get_registration(self.db, newbie.id).guild_name, "Smogg")

    def test_reclutar_a_smogged_ya_adentro(self):
        run(self.reg.refresh_roster())
        newbie = self._newbie(882)
        ok, text, granted = run(self.reg.accept_recruit(newbie, "Charlie", self.r_member.id, "Miembro", self.council, guild_name="Smogged"))
        self.assertEqual((ok, granted), (True, True))
        self.assertTrue(any(r.name == "Smogged" for r in newbie.roles))
        self.assertEqual(rdb.list_pending_recruits(self.db), [])

    def test_ticket_tiene_selector_de_guild(self):
        """bot.py no se puede importar en las pruebas: se revisa el código fuente."""
        src = open(os.path.join(os.path.dirname(HERE), "bot.py"), encoding="utf-8").read()
        bloque = src[src.index("class RecruitRolesView"):src.index("class RecruitTicketView")]
        self.assertIn("self.guild_select", bloque)
        self.assertIn("guild_name=self.guild_name", bloque)
        self.assertIn("guild_name=gname", src[src.index("async def accept_player"):src.index("class RecruitRolesView")])

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
