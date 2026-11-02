import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import rolesmgmt as rm  # noqa: E402
import roles_db as rdb  # noqa: E402
import invitados as iv  # noqa: E402


class RolesTests(DiscordBase):
    def setUp(self):
        super().setUp()
        g = self.guild
        self.roles = {}
        for key, (n, rid) in rm.ROLE_CHOICES.items():
            self.roles[key] = discord.Role(rid, n, g)
            g.roles.append(self.roles[key])
        def mk(uid, name, rids):
            m = discord.Member(uid, name, roles=[discord.Role(r, str(r), g) for r in rids], guild=g)
            g.members.append(m)
            return m
        self.stf = mk(7201, "Stf", [rm.STAFF_ROLE_ID])
        self.coach = mk(7202, "Coach", [rm.COACHING_ROLE_ID])
        self.opi = mk(7203, "Opi", [rm.OPINOLOGO_ROLE_ID])
        self.target = mk(7204, "Target", [])
        self.cog = run(rm.setup_rolesmgmt(self.bot, self.db, GUILD_ID))

    def go(self, who, action, key, target=None, motivo=None):
        i = self.inter(who)
        cmd = self.cog.rol_dar if action == "dar" else self.cog.rol_quitar
        run(cmd.callback(self.cog, i, target or self.target, key, motivo))
        return i.text

    def has(self, key):
        return rm.ROLE_CHOICES[key][1] in {r.id for r in self.target.roles}

    def test_matriz_de_permisos(self):
        for who, obs_dar, obs_quitar, cta_dar, cta_quitar in (
                (self.stf, 1, 1, 1, 1), (self.council, 1, 1, 1, 1), (self.admin, 1, 1, 1, 1),
                (self.coach, 1, 1, 1, 0), (self.opi, 1, 0, 0, 0), (self.alice, 0, 0, 0, 0)):
            self.assertEqual(rm.can_change(who, "observacion", "dar"), bool(obs_dar), who.name)
            self.assertEqual(rm.can_change(who, "observacion", "quitar"), bool(obs_quitar), who.name)
            self.assertEqual(rm.can_change(who, "tank", "dar"), bool(cta_dar), who.name)
            self.assertEqual(rm.can_change(who, "tank", "quitar"), bool(cta_quitar), who.name)

    def test_dar_y_quitar_observacion_queda_registrado(self):
        self.assertIn("recibió", self.go(self.opi, "dar", "observacion", motivo="entró nuevo"))
        self.assertTrue(self.has("observacion"))
        self.assertIn("No tenés permiso", self.go(self.opi, "quitar", "observacion"))
        self.assertTrue(self.has("observacion"))
        self.assertIn("se le quitó", self.go(self.coach, "quitar", "observacion"))
        self.assertFalse(self.has("observacion"))
        h = rdb.history(self.db, self.target.id)
        self.assertEqual([(r["action"], r["actor_id"]) for r in h], [("quitar", self.coach.id), ("dar", self.opi.id)])
        self.assertEqual(h[1]["reason"], "entró nuevo")

    def test_cta_coach_da_pero_no_quita(self):
        self.assertIn("recibió", self.go(self.coach, "dar", "flex"))
        self.assertIn("No tenés permiso", self.go(self.coach, "quitar", "flex"))
        self.assertTrue(self.has("flex"))
        self.assertIn("se le quitó", self.go(self.stf, "quitar", "flex"))
        self.assertIn("No tenés permiso", self.go(self.alice, "dar", "tank"))

    def test_reglas_basicas(self):
        self.go(self.stf, "dar", "bomb")
        self.assertIn("ya tiene", self.go(self.stf, "dar", "bomb"))
        self.assertIn("no tiene", self.go(self.stf, "quitar", "tank"))
        self.assertIn("vos mismo", self.go(self.stf, "dar", "tank", target=self.stf))
        self.assertIn("no se puede", self.go(self.stf, "dar", "caller"))
        self.assertEqual(len(rdb.history(self.db)), 1)                      # solo el cambio real quedó registrado

    def test_historial_y_permisos(self):
        self.go(self.stf, "dar", "dps")
        i = self.inter(self.coach)
        run(self.cog.rol_historial.callback(self.cog, i, self.target))
        self.assertIn(f"<@{self.stf.id}>", i.text)
        i = self.inter(self.alice)
        run(self.cog.rol_historial.callback(self.cog, i, None))
        self.assertIn("No tenés permiso", i.text)

    def test_info(self):
        self.assertIn("roles", [c.value for c in iv.InvitadosCog.INFO_CHOICES])
        self.assertIn("queda registrado", iv.InvitadosCog.info_embed("roles", False).description)
        self.assertIn("/rol", iv.InvitadosCog.info_embed("roles", True).description)


if __name__ == "__main__":
    unittest.main()
