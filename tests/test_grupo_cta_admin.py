import os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CtaAdminGroupTests(unittest.TestCase):
    """bot.py no se puede importar en las pruebas (arranca el bot): se revisa el código fuente."""
    def setUp(self):
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_subcomandos_bajo_cta_admin(self):
        self.assertIn('Group(name="cta-admin"', self.src)
        self.assertIn("bot.tree.add_command(cta_admin_group, guild=GUILD_OBJ)", self.src)
        for sub in ("activas", "programadas", "cancelar-programada", "cerrar", "asignar", "liberar", "historial"):
            self.assertIn(f'@cta_admin_group.command(name="{sub}"', self.src)

    def test_no_quedan_comandos_cta_sueltos(self):
        sueltos = re.findall(r'bot\.tree\.command\(name="(cta_[a-z_]+)"', self.src)
        self.assertEqual(sueltos, [])


if __name__ == "__main__":
    unittest.main()
