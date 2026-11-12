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
                (self.stf, 1, 0, 1, 1), (self.council, 1, 0, 1, 1), (self.admin, 1, 0, 1, 1),
                (self.coach, 1, 0, 1, 0), (self.opi, 1, 0, 0, 0), (self.alice, 0, 0, 0, 0)):
            self.assertEqual(rm.can_change(who, "observacion", "dar"), bool(obs_dar), who.name)
            self.assertEqual(rm.can_change(who, "observacion", "quitar"), bool(obs_quitar), who.name)
            self.assertEqual(rm.can_change(who, "tank", "dar"), bool(cta_dar), who.name)
            self.assertEqual(rm.can_change(who, "tank", "quitar"), bool(cta_quitar), who.name)

    def test_dar_observacion_queda_registrado_y_quitar_solo_por_votacion(self):
        self.assertIn("recibió", self.go(self.opi, "dar", "observacion", motivo="entró nuevo"))
        self.assertTrue(self.has("observacion"))
        for who in (self.opi, self.coach, self.council, self.admin):
            self.assertIn("!votacion", self.go(who, "quitar", "observacion"))
        self.assertTrue(self.has("observacion"))
        h = rdb.history(self.db, self.target.id)
        self.assertEqual([(r["action"], r["actor_id"]) for r in h], [("dar", self.opi.id)])
        self.assertEqual(h[0]["reason"], "entró nuevo")

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

    def test_es_un_solo_comando_raiz(self):
        # el servidor admite 100 comandos: /rol cuenta como UNO (dar, quitar e historial son subcomandos)
        self.assertEqual(self.cog.rol.name, "rol")
        self.assertEqual(sorted(self.cog.rol.commands), ["dar", "historial", "quitar"])

    def test_info(self):
        self.assertIn("roles", [c.value for c in iv.InvitadosCog.INFO_CHOICES])
        self.assertIn("queda registrado", iv.InvitadosCog.info_embed("roles", False).description)
        self.assertIn("/rol dar", iv.InvitadosCog.info_embed("roles", True).description)


class ObsVoteTests(DiscordBase):
    """!votacion: única forma de sacar Observación (mayoría de Coaching + Council, o 👑 de un Council)."""
    def setUp(self):
        super().setUp()
        import types
        import coaching_db as cdb
        g = self.guild
        self.r_obs = discord.Role(rm.OBSERVACION_ROLE_ID, "Observación", g)
        self.r_coach = discord.Role(rm.COACHING_ROLE_ID, "Coaching", g)
        g.roles += [self.r_obs, self.r_coach]
        def mk(uid, name, roles):
            m = discord.Member(uid, name, roles=list(roles), guild=g)
            g.members.append(m)
            return m
        self.obs = mk(7301, "Obs", [self.r_obs])
        self.c1, self.c2, self.c3 = (mk(7310 + i, f"C{i}", [self.r_coach]) for i in range(3))
        self.nadie = mk(7320, "Nadie", [])
        # votantes: c1, c2, c3 + self.council (Council) = 4 → mayoría 3
        self.thread = discord.Thread(8801, g, "post-obs")
        self.thread.parent_id = rm.COACHING_FORUM_ID
        cdb.init_schema(self.db)
        cdb.set_post(self.db, self.obs.id, self.thread.id)
        self.cog = run(rm.setup_rolesmgmt(self.bot, self.db, GUILD_ID))
        self.ctx = lambda who, ch=None: types.SimpleNamespace(channel=ch or self.thread, guild=g, author=who, send=(ch or self.thread).send)

    def start(self, who=None):
        run(self.cog.votacion.callback(self.cog, self.ctx(who or self.obs)))
        return self.thread.sent[-1]

    def press(self, who, msg, kind):
        i = self.inter(who, channel=self.thread, message=msg)
        fn = {"si": self.cog.cast, "no": self.cog.cast, "council": None}[kind]
        run(self.cog.council_approve(i) if kind == "council" else fn(i, kind == "si"))
        return i

    def obs_has(self):
        return rm.OBSERVACION_ROLE_ID in {r.id for r in self.obs.roles}

    def test_solo_en_el_post_del_foro(self):
        run(self.cog.votacion.callback(self.cog, self.ctx(self.obs, self.panelch)))
        self.assertIn("solo se usa dentro del post", self.panelch.sent[-1].content)
        run(self.cog.votacion.callback(self.cog, self.ctx(self.nadie)))
        self.assertIn("Pueden abrir", self.thread.sent[-1].content)

    def test_mayoria_saca_el_rol_y_queda_registrado(self):
        msg = self.start()
        self.assertIn("3", msg.embeds[0].description)                       # mayoría de 4 votantes
        self.assertIn("Solo votan", self.press(self.nadie, msg, "si").text)
        self.assertIn("Solo votan", self.press(self.obs, msg, "si").text)
        self.press(self.c1, msg, "si")
        self.press(self.council, msg, "si")                                 # el voto del Council cuenta
        self.assertTrue(self.obs_has())
        i = self.press(self.c2, msg, "si")
        self.assertFalse(self.obs_has())
        self.assertIn("Aprobada", i.response.edits[-1]["embed"].description)
        h = rdb.history(self.db, self.obs.id)
        self.assertEqual((h[0]["action"], h[0]["role_name"]), ("quitar", "Observación"))
        self.assertIn("cerrada", self.press(self.c3, msg, "no").text)

    def test_rechazo_cuando_ya_no_llega_a_mayoria(self):
        msg = self.start()
        self.press(self.c1, msg, "no")
        i = self.press(self.c2, msg, "no")                                  # 4 - 2 = 2 < 3 → rechazada
        self.assertIn("Rechazada", i.response.edits[-1]["embed"].description)
        self.assertTrue(self.obs_has())

    def test_council_aprueba_directo_y_una_sola_abierta(self):
        msg = self.start(self.c1)
        self.start(self.c2)
        self.assertIn("Ya hay una votación abierta", self.thread.sent[-1].content)
        self.assertIn("Solo un **Council**", self.press(self.c1, msg, "council").text)
        self.press(self.council, msg, "council")
        self.assertFalse(self.obs_has())
        self.assertEqual(rdb.history(self.db, self.obs.id)[0]["actor_id"], self.council.id)

    def test_cambiar_el_voto(self):
        msg = self.start()
        self.press(self.c1, msg, "si")
        self.press(self.c1, msg, "no")
        self.assertEqual(rdb.ballots(self.db, rdb.vote_by_message(self.db, msg.id)["id"]), {self.c1.id: False})


if __name__ == "__main__":
    unittest.main()
