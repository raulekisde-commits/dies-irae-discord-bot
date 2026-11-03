"""/content (CTA de content), flag split-ava de las composiciones y tareas «para» un rol.
bot.py no se puede importar en las pruebas: lo suyo se revisa en el código fuente; el resto, con los módulos reales."""
import os, re, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import encargos as enc  # noqa: E402
import encargos_db as edb  # noqa: E402
import splits as sp  # noqa: E402
import splits_db as sdb  # noqa: E402

ROOT = os.path.dirname(HERE)


class ContentFuente(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_comando_y_permiso(self):
        self.assertIn('@bot.tree.command(name="content"', self.src)
        self.assertIn("CONTENT_CREATOR_ROLE_ID = 1258562816512884808", self.src)
        self.assertIn("any(r.id == CONTENT_CREATOR_ROLE_ID for r in member.roles)", self.src)
        self.assertIn('kind="content"', self.src)
        self.assertIn('content_command.autocomplete("nombre")', self.src)

    def test_content_sin_bomb_ni_combatlog(self):
        self.assertIn("if not cta_is_content(run_id):", self.src)                       # sin botón «Agregar Bomb»
        self.assertIn('if run.get("kind") != "content":', self.src)                      # sin panel/tarea de combatlog
        self.assertIn('if cog is not None and run.get("kind") != "content":', self.src)  # sin tarea de revisar el combat log

    def test_migraciones(self):
        self.assertIn('"cta_runs", "kind"', self.src)
        self.assertIn('"cta_compositions", "ava"', self.src)

    def test_compo_ava_y_split_ava(self):
        self.assertIn('@app_commands.command(name="ava"', self.src)
        self.assertIn('"ava": bool(comp.get("ava"))', self.src)
        self.assertIn('kind="ava" if is_ava else "normal"', self.src)

    def test_quien_lanzo_el_content_puede_cerrarlo_o_cancelarlo(self):
        self.assertIn('run.get("kind") == "content" and int(run["created_by"]) == interaction.user.id', self.src)

    def test_cancelar_borra_tareas(self):
        self.assertIn('enc.cancel_by_prefix(f"cta:{run_id}:"', self.src)


class SplitAva(DiscordBase):
    def test_split_de_cta_puede_ser_ava(self):
        cog = sp.SplitsCog(self.bot, self.db, GUILD_ID, 12345)
        run(self.bot.add_cog(cog))
        ch = discord.TextChannel(12345, self.guild, "splits")
        self.bot.channels[12345] = ch
        split, _m, err = run(cog.create_from_cta(cta_id=31, title="Content", tab=None, city=None, user_ids=[self.alice.id],
                                                  creator_id=self.admin.id, kind="ava"))
        self.assertIsNone(err)
        self.assertEqual(sdb.get_split(self.db, split.id).kind, "ava")
        normal, _m, _e = run(cog.create_from_cta(cta_id=32, title="CTA", tab=None, city=None, user_ids=[self.alice.id], creator_id=self.admin.id))
        self.assertEqual(sdb.get_split(self.db, normal.id).kind, "normal")


class TareasParaRol(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(enc.STAFF_ROLE_ID, "Staff", self.guild)
        self.r_art = discord.Role(777000000000000009, "Arte", self.guild)
        self.guild.roles += [self.r_staff, self.r_art]
        self.stf = discord.Member(9001, "Stf", roles=[self.r_staff], guild=self.guild)
        self.art = discord.Member(9003, "Art", roles=[self.r_art], guild=self.guild)
        self.guild.members += [self.stf, self.art]
        self.cog = run(enc.setup_encargos(self.bot, self.db, GUILD_ID))

    def mk(self, for_role=None):
        t = edb.create_task(self.db, guild_id=GUILD_ID, channel_id=1, text="x", created_by=1, for_role=for_role, ping_staff=for_role is None)
        from dbutil import connect
        c = connect(self.db); c.execute("UPDATE tasks SET status='open', message_id=? WHERE id=?", (500 + t.id, t.id)); c.close()
        return edb.get_task(self.db, t.id)

    def test_helper_permisos(self):
        comun, para = self.mk(), self.mk(for_role=self.r_art.id)
        self.assertTrue(enc.can_work_on(self.stf, comun))
        self.assertFalse(enc.can_work_on(self.art, comun))                 # un rol cualquiera no toma tareas comunes
        self.assertTrue(enc.can_work_on(self.art, para))                   # el rol destinatario sí
        self.assertFalse(enc.can_work_on(self.stf, para))                  # el Staff no, si no está en el rol
        self.assertTrue(enc.can_work_on(self.council, para))               # el Council siempre

    def test_tomar_y_terminar(self):
        para = self.mk(for_role=self.r_art.id)
        async def no_thread(**kw):
            raise discord.HTTPException()
        msg = type("M", (), {"id": 500 + para.id, "content": None, "create_thread": staticmethod(no_thread)})()
        i = self.inter(self.stf, message=msg)
        run(self.cog.on_take(i))
        self.assertIn("solo para el rol", i.text)
        self.assertEqual(edb.get_task(self.db, para.id).status, "open")
        i = self.inter(self.art, message=msg)
        run(self.cog.on_take(i))
        self.assertEqual(edb.get_task(self.db, para.id).status, "taken")

    def test_embed_y_ping_solo_al_rol(self):
        para = self.mk(for_role=self.r_art.id)
        fields = self.cog.embed(para).fields
        self.assertTrue(any((f[0] if isinstance(f, tuple) else f.name) == "Para" for f in fields))
        self.assertFalse(para.ping_staff)
        self.assertEqual(para.for_role, self.r_art.id)


if __name__ == "__main__":
    unittest.main()
