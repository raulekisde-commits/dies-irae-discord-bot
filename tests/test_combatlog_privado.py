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


if __name__ == "__main__":
    unittest.main()
