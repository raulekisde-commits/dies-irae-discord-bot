import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class CombatlogPrivadoTests(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(HERE, "..", "bot.py"), encoding="utf-8").read()

    def test_canal_privado_configurable(self):
        self.assertIn('bs.register("combatlog_privado"', self.src)
        self.assertIn('"1571247913215070379"', self.src)
        self.assertNotIn("guild.get_channel(COMBATLOG_PRIVATE_CHANNEL_ID)", self.src)

    def test_boton_formulario_y_vista_persistente(self):
        self.assertIn('custom_id="combatlog:upload"', self.src)
        self.assertIn("discord.ui.FileUpload(", self.src)
        self.assertIn("bot.add_view(CombatlogPanelView())", self.src)
        self.assertIn("view=CombatlogPanelView()", self.src)

    def test_archivos_al_hilo_privado_y_tarea(self):
        i = self.src.index("async def cta_combatlog_upload(")
        body = self.src[i:self.src.index("\n\n\n", i)]
        self.assertIn("await cta_combatlog_private_thread(run)", body)
        self.assertIn("files=out", body)
        self.assertIn('complete_by_source(f"cta:{cta_id}:combatlog"', body)
        self.assertIn("cta_combatlog_reward_user(", body)
        self.assertNotIn("public.send(f\"✅ {user.mention} subió su combatlog.\", files", body)   # el archivo nunca va al hilo público


    def test_subir_logs_requiere_rol_de_logs(self):
        self.assertIn("LOG_UPLOADER_ROLE_ID = 1462619849221079235", self.src)
        for fn in ("async def upload(", "async def chestlog(", "async def cta_combatlog_upload(", "async def cta_chestlog_upload("):
            i = self.src.index(fn)
            self.assertIn("if not can_upload_logs(interaction.user):", self.src[i:i + 400])

    def test_boton_chestlog_solo_staff_council_al_hilo_privado(self):
        self.assertIn('custom_id="combatlog:chestlog"', self.src)
        i = self.src.index("async def chestlog(")
        btn = self.src[i:self.src.index("\n\n\n", i)]
        self.assertIn("can_upload_logs(interaction.user)", btn)
        self.assertIn("ChestlogUploadModal(", btn)
        i = self.src.index("async def cta_chestlog_upload(")
        body = self.src[i:self.src.index("\n\n\n", i)]
        self.assertIn("can_upload_logs(interaction.user)", body)
        self.assertIn("await cta_combatlog_private_thread(", body)
        self.assertIn("files=out", body)
        self.assertNotIn("complete_by_source", body)              # no cierra la tarea del combatlog
        self.assertNotIn("reward", body)                          # ni da tirada

    def test_boton_auditar_solo_staff_council(self):
        self.assertIn('custom_id="combatlog:audit"', self.src)
        i = self.src.index("    async def audit(self, interaction")
        btn = self.src[i:self.src.index("\n\n\n", i)]
        self.assertIn("is_staff_or_council(interaction.user)", btn)
        self.assertIn('get_cog("ChestAuditCog")', btn)
        self.assertIn("audit_from_panel(", btn)

if __name__ == "__main__":
    unittest.main()

    def test_boton_solucionado_persistente(self):
        src = open(os.path.join(HERE, "..", "chestaudit.py"), encoding="utf-8").read()
        self.assertIn('custom_id="chestaudit:solved"', src)
        self.assertIn("self.bot.add_view(SolvedView(self))", src)
        i = src.index("    async def solve(")
        body = src[i:src.index("\n\n\n", i)] if "\n\n\n" in src[i:] else src[i:]
        self.assertIn("is_staff_or_council(interaction.user)", body)
        self.assertIn("th.delete()", body)
        self.assertIn("interaction.message.delete()", body)
        self.assertIn("WarnView(", body)

