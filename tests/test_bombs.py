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
        self.r_common = discord.Role(bm.BOMB_COMMON_ROLE_ID, "Bomb", g)
        g.roles += [self.r_staff, self.r_rec, self.r_b1, self.r_b2, self.r_common]
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

    def test_cargar(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.call("agregar", self.stf, "Norte", self.outsider)              # ya cargado por comando
        self.alice.roles.append(self.r_b1)                                   # rol dado a mano
        self.robot.roles.append(self.r_b1)                                   # los bots no se cargan
        self.assertIn("no fue cargado", self.call("listar", self.stf, "Norte"))
        self.assertIn("Solo el Staff", self.call("cargar", self.alice, "Norte"))
        text = self.call("cargar", self.rec, "Norte")
        self.assertIn("**1** personas", text); self.assertIn(f"<@{self.alice.id}>", text)
        m = {r["user_id"]: r["added_by"] for r in bdb.members(self.db, bdb.find_bomb(self.db, GUILD_ID, "Norte")["id"])}
        self.assertEqual(m, {self.outsider.id: self.stf.id, self.alice.id: self.rec.id})
        self.assertIn("ya están cargados", self.call("cargar", self.rec, "Norte"))

    def test_rol_comun_al_agregar_y_cargar(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.call("crear", self.council, "Sur", self.r_b2, self.alice)
        self.call("agregar", self.stf, "Norte", self.outsider)
        self.assertIn(self.r_common, self.outsider.roles)                   # rol propio + rol común
        self.outsider.roles.remove(self.r_common)
        self.assertIn("ya estaba", self.call("agregar", self.stf, "Norte", self.outsider))
        self.assertIn(self.r_common, self.outsider.roles)                   # volver a agregar lo repone
        self.outsider.roles.remove(self.r_common)                            # anotado pero sin rol común
        self.council.roles.append(self.r_b2)                                 # rol de bomb dado a mano, otra bomb
        self.assertIn("**1** personas", self.call("cargar", self.rec, "Sur"))
        self.assertIn(self.r_common, self.council.roles)
        self.assertIn("ya están cargados", self.call("cargar", self.rec, "Norte"))
        self.assertIn(self.r_common, self.outsider.roles)                    # cargar repone el rol común a los ya anotados

    def test_caller_solo_agrega_en_su_bomb(self):
        self.call("crear", self.council, "Norte", self.r_b1, self.bob)
        self.call("crear", self.council, "Sur", self.r_b2, self.alice)
        self.assertIn("agregado", self.call("agregar", self.bob, "Norte", self.outsider))      # bob es caller de Norte
        self.assertIn(self.r_b1, self.outsider.roles)
        self.assertIn("tu bomb", self.call("agregar", self.bob, "Sur", self.outsider))
        self.assertNotIn(self.r_b2, self.outsider.roles)
        self.assertIn("Solo el Staff", self.call("cargar", self.bob, "Norte"))                  # el caller solo agrega
        self.assertIn("Solo el Staff", self.call("sacar", self.bob, "Norte", self.outsider))

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
        self.assertEqual(sorted(self.cog.bomb_g.commands), ["agregar", "cargar", "crear", "editar", "eliminar", "listar", "sacar"])


if __name__ == "__main__":
    unittest.main()


class BombsSplitYCTA(unittest.TestCase):
    def test_parse_cta_bomb_text(self):
        import ctautil as cu
        self.assertEqual(cu.parse_cta_bomb_text("bomb"), "join")
        self.assertEqual(cu.parse_cta_bomb_text("BOMB!"), "join")
        self.assertEqual(cu.parse_cta_bomb_text("- bomb"), "leave")
        self.assertEqual(cu.parse_cta_bomb_text("-bomb"), "leave")
        self.assertIsNone(cu.parse_cta_bomb_text("bombas"))
        self.assertIsNone(cu.parse_cta_bomb_text("21"))

    def test_group_by_bomb(self):
        import splits
        bombs = [{"name": "A", "caller_id": 9, "members": {1, 2}}, {"name": "B", "caller_id": 8, "members": {2, 3}}]
        groups, rest = splits.group_by_bomb([5, 3, 2, 1, 4], bombs)
        self.assertEqual([(b["name"], m) for b, m in groups], [("A", [2, 1]), ("B", [3])])
        self.assertEqual(rest, [5, 4])
