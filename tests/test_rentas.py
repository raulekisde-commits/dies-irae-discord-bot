import asyncio, os, sys, tempfile, unittest
from datetime import date, datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import discord_stub  # noqa: E402
discord_stub.install()
import rentas_db as rdb  # noqa: E402
import rentas  # noqa: E402

WED = date(2026, 11, 18)          # miércoles
SUN = date(2026, 11, 22)          # domingo siguiente
COUNCIL = 99


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        rdb.init_schema(self.db)
        self.mapa = rdb.add_mapa(self.db, "Sleetwater")

    def tearDown(self):
        self.tmp.cleanup()

    def alta(self, tipo="ho", modalidad="adelantado", precio=7_000_000, inicio=WED, nombre="Smogg", holder="guild",
             nivel3=False, ponemos=False, n3=0, pon=0, user=10):
        self._n = getattr(self, "_n", 0) + 1
        tid = rdb.create_ticket(self.db, 1000 + self._n, user, "es", tipo, self.mapa.id, nivel3, ponemos, holder, nombre)
        tk = rdb.ticket_by_channel(self.db, 1000 + self._n)
        self.assertEqual(tk.id, tid)
        self.assertTrue(rdb.close_ticket(self.db, tid, "approved", COUNCIL))
        canal = rdb.find_canal(self.db, self.mapa.id, nombre) or rdb.create_canal(self.db, 5000 + user, self.mapa.id, holder, nombre, "es")
        rid = rdb.create_renta(self.db, canal_id=canal.id, ticket=tk, precio=precio, modalidad=modalidad, inicio=inicio, actor_id=COUNCIL,
                               nivel3_monto=n3, ponemos_monto=pon)
        return canal, rid


class Montos(unittest.TestCase):
    def test_parse_y_formato(self):
        for txt, val in (("1.5M", 1_500_000), ("750k", 750_000), ("1500000", 1_500_000), ("1.500.000", 1_500_000),
                         ("1,500,000", 1_500_000), ("2,5m", 2_500_000), ("1b", 1_000_000_000)):
            self.assertEqual(rdb.parse_silver(txt), val, txt)
        with self.assertRaises(rdb.RentaError):
            rdb.parse_silver("mucho")
        self.assertEqual([rdb.fmt_silver(n) for n in (1_500_000, 750_000, 1_250_000, 999, 2_000_000_000)], ["1.5M", "750K", "1.25M", "999", "2B"])

    def test_semanas(self):
        self.assertEqual(rdb.week_start(WED), date(2026, 11, 15))
        self.assertEqual(rdb.week_start(SUN), SUN)
        self.assertEqual(rdb.days_to_saturday(WED), 4)            # miércoles a sábado
        self.assertEqual(rdb.days_to_saturday(SUN), 7)
        self.assertEqual(rdb.days_to_saturday(date(2026, 11, 21)), 1)


class Mapas(Base):
    def test_lista_editar_quitar(self):
        with self.assertRaises(rdb.RentaError):
            rdb.add_mapa(self.db, "sleetwater")                    # sin distinguir mayúsculas
        rdb.rename_mapa(self.db, "Sleetwater", "Sleetwater Ridge")
        self.assertEqual([m.nombre for m in rdb.list_mapas(self.db)], ["Sleetwater Ridge"])
        self.alta()
        with self.assertRaises(rdb.RentaError):                    # con rentas activas no se puede quitar
            rdb.remove_mapa(self.db, "Sleetwater Ridge")

    def test_quitar_sin_rentas(self):
        rdb.remove_mapa(self.db, "Sleetwater")
        self.assertEqual(rdb.list_mapas(self.db), [])

    def test_categorias(self):
        self.assertEqual(rdb.category_name("Mapa", 1), "Mapa")
        self.assertEqual(rdb.category_name("Mapa", 2), "Mapa (2)")


class Cobro(Base):
    def test_adelantado_prorrateo_y_fijos(self):
        canal, rid = self.alta(nivel3=True, ponemos=True, n3=5_000_000, pon=2_000_000)
        cargos = {c.concepto: c.monto for c in rdb.cargos_renta(self.db, rid)}
        self.assertEqual(cargos, {"prorrateo": 4_000_000, "nivel3": 5_000_000, "ponemos": 2_000_000})
        self.assertEqual(rdb.deuda_canal(self.db, canal.id), 11_000_000)
        self.assertEqual(rdb.marcar_pagado_canal(self.db, canal.id, COUNCIL), 11_000_000)
        self.assertEqual(rdb.marcar_pagado_canal(self.db, canal.id, COUNCIL), 0)

    def test_extras_solo_ho(self):
        canal, rid = self.alta(tipo="terri", nivel3=True, n3=5_000_000)
        self.assertEqual([c.concepto for c in rdb.cargos_renta(self.db, rid)], ["prorrateo"])

    def test_semanal_adelantado_idempotente(self):
        canal, rid = self.alta()
        self.assertEqual(rdb.run_weekly(self.db, SUN), [canal.id])
        rdb.run_weekly(self.db, SUN)                                # reintento: no duplica
        semana = [c for c in rdb.cargos_renta(self.db, rid) if c.concepto == "semana"]
        self.assertEqual([(c.periodo, c.monto, c.lote) for c in semana], [(SUN.isoformat(), 7_000_000, SUN.isoformat())])

    def test_adelantado_que_empieza_domingo_no_paga_doble(self):
        canal, rid = self.alta(inicio=SUN)
        rdb.run_weekly(self.db, SUN)
        self.assertEqual([(c.concepto, c.monto) for c in rdb.cargos_renta(self.db, rid)], [("prorrateo", 7_000_000)])

    def test_vencido(self):
        canal, rid = self.alta(modalidad="vencido")
        self.assertEqual(rdb.cargos_renta(self.db, rid), [])           # no paga al aprobar
        rdb.run_weekly(self.db, SUN)                                    # semana que terminó: mié-sáb = 4 días
        rdb.run_weekly(self.db, date(2026, 11, 29))                     # semana completa
        self.assertEqual([c.monto for c in rdb.cargos_renta(self.db, rid)], [4_000_000, 7_000_000])
        self.assertEqual(rdb.deuda_canal(self.db, canal.id), 11_000_000)

    def test_baja_deuda_queda_al_titular(self):
        canal, rid = self.alta(modalidad="vencido")
        rdb.run_weekly(self.db, SUN)
        r = rdb.dar_baja(self.db, rid, date(2026, 11, 24))              # martes: dom-mar = 3 días usados
        self.assertFalse(r.activa)
        self.assertIsNone(rdb.dar_baja(self.db, rid, date(2026, 11, 24)))
        self.assertEqual(sorted(c.monto for c in rdb.cargos_renta(self.db, rid)), [3_000_000, 4_000_000])
        self.assertEqual(rdb.deuda_canal(self.db, canal.id), 0)        # ya no cuenta como deuda activa
        self.assertEqual(rdb.deudas_baja(self.db), [("guild", "Smogg", "Sleetwater", 7_000_000)])
        self.assertEqual(rdb.deudas_baja(self.db, "smo")[0][3], 7_000_000)
        self.assertEqual(rdb.resumen(self.db, date(2026, 11, 24)).deuda_activa, 0)

    def test_resumen(self):
        canal, rid = self.alta()
        rdb.run_weekly(self.db, SUN)
        rdb.marcar_pagado_canal(self.db, canal.id, COUNCIL)
        s = rdb.resumen(self.db, date(2026, 11, 23))
        self.assertEqual((s.ultimo_domingo, s.proximo_domingo), (SUN, date(2026, 11, 29)))
        self.assertEqual((s.ultimo_generado, s.ultimo_pagado, s.proximo_estimado, s.deuda_activa), (7_000_000, 7_000_000, 7_000_000, 0))

    def test_un_canal_por_titular_por_mapa(self):
        c1, _ = self.alta(tipo="ho", user=10)
        c2, _ = self.alta(tipo="permiso", holder="guild", user=11)
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(len(rdb.rentas_canal(self.db, c1.id)), 2)


class Tickets(Base):
    def test_validaciones(self):
        with self.assertRaises(rdb.RentaError):
            rdb.create_ticket(self.db, 1, 10, "es", "terri", self.mapa.id, False, False, "personaje", "Pepe")
        tid = rdb.create_ticket(self.db, 1, 10, "es", "permiso", self.mapa.id, False, False, "personaje", "  Pepe  ")
        self.assertEqual(rdb.open_ticket_of(self.db, 10).titular_nombre, "Pepe")
        self.assertTrue(rdb.close_ticket(self.db, tid, "rejected", COUNCIL, "no"))
        self.assertFalse(rdb.close_ticket(self.db, tid, "approved", COUNCIL))     # doble clic
        self.assertIsNone(rdb.open_ticket_of(self.db, 10))


class Discord(Base):
    def test_textos_completos(self):
        for lang in rdb.IDIOMAS:
            self.assertEqual(set(rentas.T[lang]), set(rentas.T["es"]), lang)

    def test_formularios(self):
        cog = rentas.RentasCog(discord_stub.Bot(), self.db, 1, COUNCIL)
        mapas = rdb.list_mapas(self.db)
        m = rentas.RequestModal(cog, "pt", "rent", mapas)
        self.assertIsNotNone(m.map_select)
        self.assertLessEqual(len(m.children), 5)
        many = [rdb.Mapa(i, f"M{i}") for i in range(30)]
        self.assertIsNotNone(rentas.RequestModal(cog, "en", "perm", many).map_text)
        tid = rdb.create_ticket(self.db, 1, 10, "es", "ho", self.mapa.id, True, True, "guild", "Smogg")
        self.assertEqual(len(rentas.ApproveModal(cog, rdb.ticket_by_channel(self.db, 1), 1, 2, 3).children), 4)
        self.assertEqual(len(rentas.PanelView(cog, "es").children), 3)
        self.assertEqual(rentas.slug("Los Pibes ¡Ñu!"), "los-pibes-ñu")

    def test_tick_una_vez_por_domingo(self):
        cog = rentas.RentasCog(discord_stub.Bot(), self.db, 1, COUNCIL)
        self.alta()
        now = datetime(2026, 11, 22, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(asyncio.run(cog.tick(now)), ["cobro"])
        self.assertEqual(asyncio.run(cog.tick(now)), [])
        self.assertEqual(asyncio.run(cog.tick(datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc))), ["aviso"])


if __name__ == "__main__":
    unittest.main()
