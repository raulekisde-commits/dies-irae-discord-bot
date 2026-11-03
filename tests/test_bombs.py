import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import bombs as bm  # noqa: E402
import bombs_db as bdb  # noqa: E402


class BombsTests(DiscordBase):
    def setUp(self):
        super().setUp()
        g = self.guild
        self.r_staff = discord.Role(bm.STAFF_ROLE_ID, "Staff", g)
        self.r_rec = discord.Role(bm.RECRUITER_ROLE_ID, "Reclutador", g)
        self.r_b1 = discord.Role(9101, "Bomb Norte", g)
        self.r_b2 = discord.Role(9102, "Bomb Sur", g)
        g.roles += [self.r_staff, self.r_rec, self.r_b1, self.r_b2]
        self.stf = discord.Member(7200, "Stf", roles=[self.r_staff], guild=g)
        self.rec = discord.Member(7201, "Rec", roles=[self.r_rec], guild=g)
        g.members += [self.stf, self.rec]
        self.cog = run(bm.setup_bombs(self.bot, self.db, GUILD_ID))

    def call(self, cmd, who, *a):
        i = self.inter(who)
        run(getattr(self.cog, cmd).callback(self.cog, i, *a))
        return i.text

    def test_solo_council_define(self):
        for who in (self.stf, self.rec, self.alice):
            self.assertIn("Council", self.call("crear", who, "Norte", self.r_b1, self.bob))
        self.assertEqual(bdb.list_bombs(self.db, GUILD_ID), [])
        self.assertIn("creada", self.call("crear", self.council, "Norte", self.r_b1, self.bob))
        self.assertIn("Ya existe", self.call("crear", self.admin, "norte", self.r_b2, self.bob))
        self.assertIn("ya es el de la bomb", self.call("crear", self.admin, "Otra", self.r_b1, self.bob))
        self.assertIn("Council", self.call("editar", self.stf, "Norte", "X", None, None))
        self.assertIn("actualizada", self.call("editar", self.council, "Norte", None, None, self.alice))
        self.assertEqual(bdb.find_bomb(self.db, GUILD_ID, "Norte")["caller_id"], self.alice.id)
        self.assertIn("conservan el rol viejo", self.call("editar", self.council, "Norte", "Norte 2", self.r_b2, None))
        b = bdb.find_bomb(self.db, GUILD_ID, "Norte 2")
        self.assertEqual(b["role_id"], self.r_b2.id)
        self.assertIn("Council", self.call("eliminar", self.rec, "Norte 2", False))

    def test_agregar_sacar_y_listar(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.assertIn("Solo el Staff", self.call("agregar", self.alice, "Norte", self.outsider))
        self.assertIn("agregado", self.call("agregar", self.rec, "Norte", self.outsider))
        self.assertIn(self.r_b1, self.outsider.roles)
        self.assertIn("ya estaba", self.call("agregar", self.stf, "Norte", self.outsider))
        self.assertIn("No existe", self.call("agregar", self.stf, "Nada", self.outsider))
        self.assertIn("bot", self.call("agregar", self.stf, "Norte", self.robot))
        m = bdb.members(self.db, bdb.find_bomb(self.db, GUILD_ID, "Norte")["id"])
        self.assertEqual([(r["user_id"], r["added_by"]) for r in m], [(self.outsider.id, self.rec.id)])

        self.assertIn("Solo el Staff", self.call("listar", self.alice, None))
        text = self.call("listar", self.bob, None)                        # bob es el caller: puede listar
        self.assertIn("Norte", text); self.assertIn("1 integrantes", text)
        text = self.call("listar", self.stf, "Norte")
        self.assertIn(f"<@{self.outsider.id}> · agregado por <@{self.rec.id}>", text)

        self.assertIn("salió", self.call("sacar", self.stf, "Norte", self.outsider))
        self.assertNotIn(self.r_b1, self.outsider.roles)
        self.assertIn("no está", self.call("sacar", self.stf, "Norte", self.outsider))

    def test_eliminar(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.call("agregar", self.stf, "Norte", self.outsider)
        self.assertIn("Se les sacó el rol a 1", self.call("eliminar", self.council, "Norte", True))
        self.assertNotIn(self.r_b1, self.outsider.roles)
        self.assertIsNone(bdb.find_bomb(self.db, GUILD_ID, "Norte"))

    def test_autocomplete_y_un_solo_comando_raiz(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.call("crear", self.council, "Sur", self.r_b2, self.bob)
        res = run(self.cog.bomb_autocomplete(self.inter(self.stf), "no"))
        self.assertEqual([c.value for c in res], ["Norte"])
        self.assertEqual(sorted(self.cog.bomb_g.commands), ["agregar", "crear", "editar", "eliminar", "listar", "sacar"])


if __name__ == "__main__":
    unittest.main()
