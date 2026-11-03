"""/config: canales configurables (botsettings) y permisos de comandos desde un panel dentro de Discord."""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID, PANEL_CH, LOG_CH  # noqa: E402
import discord  # noqa: E402
import botsettings as bs  # noqa: E402
import configpanel as cp  # noqa: E402
import economy_db as eco  # noqa: E402
import encargos  # noqa: E402,F401  (registra "tasks" y "task_log")


class ConfigBase(DiscordBase):
    def setUp(self):
        super().setUp()
        self._saved = dict(bs.REGISTRY)
        bs.REGISTRY.clear()
        bs.register("log", "Logs generales", LOG_CH, "ayuda")
        bs.register("tasks", "Panel de tareas", 111)
        bs.register("content", "Content", lambda: bs.get("tasks"))
        bs.configure(self.db)
        self.other = discord.TextChannel(5550, self.guild, "otro")
        self.guild._channels[5550] = self.other
        self.cog = run(cp.setup_config(self.bot, self.db, GUILD_ID))

    def tearDown(self):
        bs._over.clear()
        bs._db = None
        bs.REGISTRY.clear()
        bs.REGISTRY.update(self._saved)                     # no pisar los ajustes que registraron los demás módulos
        super().tearDown()


class SettingsTests(ConfigBase):
    def test_default_hasta_que_se_cambia(self):
        self.assertEqual((bs.get("log"), bs.override("log")), (LOG_CH, None))
        old, new = bs.set_value("log", 5550, self.admin.id)
        self.assertEqual((old, new, bs.get("log")), (LOG_CH, 5550, 5550))
        old, new = bs.reset("log", self.admin.id)
        self.assertEqual((old, new, bs.get("log")), (5550, LOG_CH, LOG_CH))

    def test_default_calculado_y_persistencia(self):
        self.assertEqual(bs.get("content"), 111)                       # sigue al de tareas mientras no se toque
        bs.set_value("tasks", 222, self.admin.id)
        self.assertEqual(bs.get("content"), 222)
        bs._over.clear()
        bs.configure(self.db)                                           # "reinicio": se vuelve a leer de la base
        self.assertEqual(bs.get("tasks"), 222)

    def test_sin_configurar_devuelve_default(self):
        bs._db = None
        bs._over.clear()
        self.assertEqual(bs.get("log"), LOG_CH)
        with self.assertRaises(RuntimeError):
            bs.set_value("log", 1, 1)


class PanelTests(ConfigBase):
    def test_solo_admin_abre_el_panel(self):
        i = self.inter(self.alice)
        run(self.cog.config.callback(self.cog, i))
        self.assertIn("solo para el propietario", i.text)
        i = self.inter(self.council)
        run(self.cog.config.callback(self.cog, i))
        self.assertIn("solo para el propietario", i.text)                 # ni el Council: es de administradores
        i = self.inter(self.admin)
        run(self.cog.config.callback(self.cog, i))
        self.assertTrue(i.response.sent[0][1].get("embed") is not None)

    def test_cambiar_y_restaurar_canal_con_aviso_en_logs(self):
        txt = run(self.cog.set_channel(self.guild, self.admin, "tasks", self.other))
        self.assertTrue(txt.startswith("✅"))
        self.assertEqual(bs.get("tasks"), 5550)
        self.assertTrue(any("cambió" in m.content and "Panel de tareas" in m.content for m in self.logch.sent))
        txt = run(self.cog.reset_channel(self.guild, self.admin, "tasks"))
        self.assertTrue(txt.startswith("✅"))
        self.assertEqual(bs.get("tasks"), 111)

    def test_un_no_admin_no_cambia_nada(self):
        txt = run(self.cog.set_channel(self.guild, self.council, "tasks", self.other))
        self.assertTrue(txt.startswith("❌"))
        self.assertIsNone(bs.override("tasks"))
        self.assertTrue(run(self.cog.reset_channel(self.guild, self.alice, "tasks")).startswith("❌"))

    def test_rechaza_canal_inexistente_clave_inexistente_y_sin_permisos(self):
        ghost = discord.TextChannel(9999, self.guild, "fantasma")
        self.assertTrue(run(self.cog.set_channel(self.guild, self.admin, "tasks", ghost)).startswith("❌"))
        self.assertTrue(run(self.cog.set_channel(self.guild, self.admin, "no-existe", self.other)).startswith("❌"))
        self.other.permissions_for = lambda who: discord.Permissions(view_channel=True, send_messages=False)
        txt = run(self.cog.set_channel(self.guild, self.admin, "tasks", self.other))
        self.assertIn("No puedo escribir", txt)
        self.assertIsNone(bs.override("tasks"))

    def test_registro_de_economia(self):
        txt = run(self.cog.set_channel(self.guild, self.admin, cp.ECO_LOG_KEY, self.other))
        self.assertTrue(txt.startswith("✅"))
        self.assertEqual(eco.get_setting(self.db, "log_channel_id"), "5550")
        self.assertEqual(run(self.cog.current(cp.ECO_LOG_KEY))[0], 5550)

    def test_permisos_de_comandos(self):
        role = self.r_member
        txt = run(self.cog.change_command_role(self.guild, self.admin, "dados", role, True))
        self.assertIn("ahora puede usar", txt)
        self.assertIn(role.id, eco.get_command_roles(self.db, "dados"))
        self.assertIn("ya podía", run(self.cog.change_command_role(self.guild, self.admin, "dados", role, True)))
        self.assertIn("ya no puede", run(self.cog.change_command_role(self.guild, self.admin, "dados", role, False)))
        self.assertNotIn(role.id, eco.get_command_roles(self.db, "dados"))
        # validaciones
        self.assertTrue(run(self.cog.change_command_role(self.guild, self.council, "dados", role, True)).startswith("❌"))
        self.assertTrue(run(self.cog.change_command_role(self.guild, self.admin, "no-existe", role, True)).startswith("❌"))
        everyone = discord.Role(self.guild.id, "@everyone", self.guild)
        self.assertTrue(run(self.cog.change_command_role(self.guild, self.admin, "dados", everyone, True)).startswith("❌"))

    def test_pantallas_y_vistas(self):
        emb = run(self.cog.main_embed())
        text = str(emb.fields)
        self.assertIn("Logs generales", text)
        view = cp.MainView(self.cog, self.admin.id)
        self.assertTrue(any(isinstance(c, discord.ui.Select) for c in view.children))
        cv = cp.ChannelView(self.cog, self.admin.id, "tasks")
        self.assertTrue(any(isinstance(c, discord.ui.ChannelSelect) for c in cv.children))
        run(self.cog.command_embed("dados"))
        run(cp.CommandView.build(self.cog, self.admin.id, "dados", self.guild))

    def test_la_vista_es_de_quien_la_abrio_y_revalida_admin(self):
        view = cp.MainView(self.cog, self.admin.id)
        i = self.inter(self.alice)
        self.assertFalse(run(view.interaction_check(i)))
        i = self.inter(self.admin)
        self.assertTrue(run(view.interaction_check(i)))
        self.admin.guild_permissions.administrator = False             # le sacaron el permiso con el panel abierto
        self.guild.owner_id = 0
        i = self.inter(self.admin)
        self.assertFalse(run(view.interaction_check(i)))


class FuenteTests(unittest.TestCase):
    def test_bot_usa_los_ajustes(self):
        root = os.path.dirname(HERE)
        src = open(os.path.join(root, "bot.py"), encoding="utf-8").read()
        for key in ("log", "cta", "content", "stats", "bomb", "combatlog", "split_panel", "split_log"):
            self.assertIn(f'bs.register("{key}"', src)
        self.assertIn("bs.configure(DB_PATH)", src)
        self.assertIn('setup_config', src)
        for old in ("guild.get_channel(LOG_CHANNEL_ID)", "guild.get_channel(STATS_CHANNEL_ID)", "guild.get_channel(BOMB_CHANNEL_ID)",
                    "guild.get_channel(COMBATLOG_CHANNEL_ID)"):
            self.assertNotIn(old, src)


if __name__ == "__main__":
    unittest.main()
