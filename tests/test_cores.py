"""/core y /vortex: reparto, aprobación, rechazo, edición y estadísticas."""
import os, random, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID, MEMBER_ROLE, PANEL_CH  # noqa: E402
import discord  # noqa: E402
import botsettings as bs  # noqa: E402
import cores  # noqa: E402
import cores_db as odb  # noqa: E402
import wujupon_db as wdb  # noqa: E402

GENERAL_CH = 3000000000000000099


class Allocate(unittest.TestCase):
    def test_partes_iguales(self):
        self.assertEqual(odb.allocate(4, [1, 2]), {1: 2, 2: 2})

    def test_sobrante_sorteado(self):
        out = odb.allocate(5, [1, 2], random.Random(1))
        self.assertEqual(sorted(out.values()), [2, 3])

    def test_mas_personas_que_tiradas(self):
        out = odb.allocate(1, [1, 2, 3], random.Random(2))
        self.assertEqual(sum(out.values()), 1)
        self.assertEqual(len(out), 1)

    def test_sin_repetidos(self):
        self.assertEqual(odb.allocate(3, [1, 1, 2, 3]), {1: 1, 2: 1, 3: 1})


class FakeWuju:
    def __init__(self, db):
        self.db = db

    async def run(self, fn, *a, **k):
        return fn(self.db, *a, **k)


class FakeAttachment:
    content_type, filename = "image/png", "core.png"

    async def read(self):
        return b"\x89PNGfoto"

    async def to_file(self):
        return discord.File(b"png", filename=self.filename)


class CoresDiscord(DiscordBase):
    def setUp(self):
        super().setUp()
        self.old_role = cores.MIEMBRO_ROLE_ID
        cores.MIEMBRO_ROLE_ID = MEMBER_ROLE
        self.addCleanup(lambda: setattr(cores, "MIEMBRO_ROLE_ID", self.old_role))
        bs.configure(self.db)
        bs.set_value("cores", PANEL_CH, 1)
        bs.set_value("general", GENERAL_CH, 1)
        self.addCleanup(lambda: bs._over.clear())
        self.general = discord.TextChannel(GENERAL_CH, self.guild, "general")
        self.guild._channels[GENERAL_CH] = self.general
        self.bot.channels[GENERAL_CH] = self.general
        self.bot.cogs["WujuponCog"] = FakeWuju(self.db)
        self.cog = run(cores.setup_cores(self.bot, self.db, GUILD_ID))

    def report(self, user, rareza="azul", kind="core"):
        i = self.inter(user)
        run(self.cog.submit(i, kind, "no", rareza, FakeAttachment()))
        return i, self.panelch.sent[-1]

    def test_solo_miembros(self):
        i = self.inter(self.outsider)
        run(self.cog.submit(i, "core", "si", "verde", FakeAttachment()))
        self.assertIn("Solo los miembros", i.text)
        self.assertEqual(self.panelch.sent, [])

    def test_foto_obligatoria(self):
        i = self.inter(self.alice)
        run(self.cog.submit(i, "core", "si", "verde", None))
        self.assertIn("todos los datos", i.text)

    def test_aprobar_da_tiradas_y_avisa_en_general(self):
        i, msg = self.report(self.alice, "morado")
        self.assertIn("Recibimos", i.text)
        self.assertIsNotNone(msg.file)
        a = self.inter(self.council, message=msg)
        run(self.cog.approve(a, msg))
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 3)
        self.assertIn(self.alice.mention, self.general.sent[-1].content)
        self.assertEqual(self.general.sent[-1].file.fp.getvalue(), b"\x89PNGfoto")    # el aviso lleva la foto (guardada en disco)
        self.assertEqual(self.cog._photo_paths(odb.get_by_message(self.db, msg.id)["id"]), [])   # y después se borra
        self.assertIsNone(msg.view)
        self.assertEqual(odb.user_stats(self.db, self.alice.id)["core"]["morado"], 1)
        a2 = self.inter(self.council, message=msg)                      # no se aprueba dos veces
        run(self.cog.approve(a2, msg))
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 3)

    def test_solo_staff_aprueba(self):
        _i, msg = self.report(self.alice)
        a = self.inter(self.bob, message=msg)
        run(self.cog.approve(a, msg))
        self.assertIn("Solo el Staff", a.text)
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 0)

    def test_rechazar(self):
        _i, msg = self.report(self.alice)
        a = self.inter(self.council, message=msg)
        run(self.cog.reject(a, msg, "foto borrosa"))
        self.assertIn("rechazado", self.general.sent[-1].content)
        self.assertIn("foto borrosa", self.general.sent[-1].content)
        self.assertEqual(self.general.sent[-1].file.fp.getvalue(), b"\x89PNGfoto")
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 0)
        self.assertEqual(odb.user_stats(self.db, self.alice.id)["core"]["azul"], 0)

    def test_editar_color(self):
        _i, msg = self.report(self.alice, "verde", kind="vortex")
        run(self.cog.edit_rarity(self.inter(self.council, message=msg), msg, "dorado"))
        run(self.cog.approve(self.inter(self.council, message=msg), msg))
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 5)
        self.assertEqual(odb.user_stats(self.db, self.alice.id)["vortex"]["dorado"], 1)

    def test_repartir(self):
        _i, msg = self.report(self.alice, "verde")
        a = self.inter(self.council, message=msg)
        run(self.cog.share(a, msg, [self.bob]))
        total = wdb.get_spins(self.db, self.alice.id) + wdb.get_spins(self.db, self.bob.id)
        self.assertEqual(total, 1)                                     # 1 tirada entre 2: se sorteó
        winner = self.alice if wdb.get_spins(self.db, self.alice.id) else self.bob
        loser = self.bob if winner is self.alice else self.alice
        self.assertEqual(odb.user_stats(self.db, winner.id)["core"]["verde"], 1)
        self.assertEqual(odb.user_stats(self.db, loser.id)["core"]["verde"], 0)
        self.assertIn(self.bob.mention, self.general.sent[-1].content)
        self.assertIn("Sumados por el Staff", self.general.sent[-1].content)
        self.assertEqual(self.general.sent[-1].file.fp.getvalue(), b"\x89PNGfoto")

    def test_foto_del_adjunto_si_no_esta_en_disco(self):
        _i, msg = self.report(self.alice)
        self.cog._drop_photo(odb.get_by_message(self.db, msg.id)["id"])         # reporte viejo: sin foto guardada
        msg.attachments = [FakeAttachment()]
        run(self.cog.approve(self.inter(self.council, message=msg), msg))
        self.assertEqual(self.general.sent[-1].file.filename, "core.png")

    def test_companeros_en_el_comando(self):
        i = self.inter(self.alice)
        run(self.cog.submit(i, "core", "si", "dorado", FakeAttachment(), f"{self.bob.mention} {self.council.mention}"))
        self.assertIn("Solo se puede repartir entre miembros", i.text)                 # el consejero no es miembro
        self.assertEqual(self.panelch.sent, [])
        i = self.inter(self.alice)
        run(self.cog.submit(i, "core", "no", "dorado", FakeAttachment(), self.bob.mention))
        self.assertIn("Repartir: No", i.text)
        i = self.inter(self.alice)
        run(self.cog.submit(i, "core", "si", "dorado", FakeAttachment(), f"{self.bob.mention} {self.alice.mention}"))
        msg = self.panelch.sent[-1]
        self.assertEqual(odb.mates_of(odb.get_by_message(self.db, msg.id)), [self.bob.id])     # sin repetir a quien reporta
        self.assertTrue(any("Repartir con" in f[0] and self.bob.mention in f[1] for f in msg.embeds[0].fields))
        run(self.cog.share(self.inter(self.council, message=msg), msg, []))                   # botón «Repartir con los que puso»
        self.assertIn(f"las eligió {self.alice.mention} al poner el core", self.general.sent[-1].content)
        self.assertNotIn("Sumados por el Staff", self.general.sent[-1].content)
        self.assertEqual(wdb.get_spins(self.db, self.alice.id) + wdb.get_spins(self.db, self.bob.id), 5)
        self.assertGreaterEqual(min(wdb.get_spins(self.db, self.alice.id), wdb.get_spins(self.db, self.bob.id)), 2)

    def test_repartir_si_obliga_a_poner_companeros(self):
        i = self.inter(self.alice)
        run(self.cog.submit(i, "vortex", "si", "verde", FakeAttachment()))
        self.assertIn("tenés que poner con quiénes", i.text)
        self.assertEqual(self.panelch.sent, [])

    def test_companeros_sin_menciones(self):
        i = self.inter(self.alice)
        run(self.cog.submit(i, "core", "si", "verde", FakeAttachment(), "Bob"))
        self.assertIn("mencioná", i.text)

    def test_repartir_solo_miembros(self):
        _i, msg = self.report(self.alice, "dorado")
        a = self.inter(self.council, message=msg)
        run(self.cog.share(a, msg, [self.outsider]))
        self.assertIn("Solo se puede repartir entre miembros", a.text)
        self.assertEqual(odb.get_by_message(self.db, msg.id)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
