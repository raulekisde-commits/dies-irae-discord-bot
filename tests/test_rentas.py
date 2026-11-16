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
             nivel3=False, ponemos=False, n3=0, pon=0, user=10, defensas=0, pack=False, defensa_monto=0, pack_monto=0):
        self._n = getattr(self, "_n", 0) + 1
        tid = rdb.create_ticket(self.db, 1000 + self._n, user, "es", tipo, self.mapa.id, nivel3, ponemos, holder, nombre,
                                defensas=defensas, pack=pack)
        tk = rdb.ticket_by_channel(self.db, 1000 + self._n)
        self.assertEqual(tk.id, tid)
        self.assertTrue(rdb.close_ticket(self.db, tid, "approved", COUNCIL))
        canal = rdb.find_canal(self.db, self.mapa.id, nombre) or rdb.create_canal(self.db, 5000 + user, self.mapa.id, holder, nombre, "es")
        rid = rdb.create_renta(self.db, canal_id=canal.id, ticket=tk, precio=precio, modalidad=modalidad, inicio=inicio, actor_id=COUNCIL,
                               nivel3_monto=n3, ponemos_monto=pon, defensa_monto=defensa_monto, pack_monto=pack_monto)
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
        canal, rid = self.alta(nivel3=True, ponemos=True, n3=5_000_000, pon=2_000_000)          # «ponemos» queda por compatibilidad
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


class DefensasYPack(Base):
    def test_defensa_mitad_adelantada_y_resto_una_vez(self):
        canal, rid = self.alta(defensas=2, defensa_monto=80_000_000)
        cargos = {c.concepto: c.monto for c in rdb.cargos_renta(self.db, rid)}
        self.assertEqual(cargos, {"prorrateo": 4_000_000, "defensa_ant": 40_000_000})
        self.assertEqual([c.monto for c in rdb.cargar_resto(self.db, canal.id)], [40_000_000])
        self.assertEqual(rdb.cargar_resto(self.db, canal.id), [])                 # no se carga dos veces
        self.assertEqual(rdb.deuda_canal(self.db, canal.id), 84_000_000)

    def test_pack_semana_gratis(self):
        canal, rid = self.alta(pack=True, defensas=2, pack_monto=120_000_000)    # miércoles: gratis hasta el miércoles siguiente
        cargos = {c.concepto: c.monto for c in rdb.cargos_renta(self.db, rid)}
        self.assertEqual(cargos, {"pack_ant": 60_000_000})                       # sin prorrateo (días gratis) y sin defensa aparte
        rdb.run_weekly(self.db, SUN)                                              # semana dom 22 - sáb 28: gratis hasta el mié 25 → 4 días
        semana = [c.monto for c in rdb.cargos_renta(self.db, rid) if c.concepto == "semana"]
        self.assertEqual(semana, [4_000_000])
        self.assertEqual(rdb.get_renta(self.db, rid).gratis_hasta, date(2026, 11, 25))

    def test_pack_vencido(self):
        canal, rid = self.alta(pack=True, modalidad="vencido", pack_monto=140_000_000)
        rdb.run_weekly(self.db, SUN)                                              # mié-sáb: todo gratis
        rdb.run_weekly(self.db, date(2026, 11, 29))                               # dom 22 - sáb 28: cobra mié 25 a sáb 28 = 4 días
        self.assertEqual([c.monto for c in rdb.cargos_renta(self.db, rid) if c.concepto == "semana"], [4_000_000])

    def test_extras_no_aplican_fuera_de_ho(self):
        canal, rid = self.alta(tipo="terri", defensas=2, pack=True, defensa_monto=80_000_000, pack_monto=120_000_000)
        self.assertEqual([c.concepto for c in rdb.cargos_renta(self.db, rid)], ["prorrateo"])


class Tickets(Base):
    def test_validaciones(self):
        with self.assertRaises(rdb.RentaError):
            rdb.create_ticket(self.db, 1, 10, "es", "terri", self.mapa.id, False, False, "personaje", "Pepe")
        tid = rdb.create_ticket(self.db, 1, 10, "es", "permiso", self.mapa.id, False, False, "personaje", "  Pepe  ")
        self.assertEqual(rdb.open_ticket_of(self.db, 10).titular_nombre, "Pepe")
        self.assertTrue(rdb.close_ticket(self.db, tid, "rejected", COUNCIL, "no"))
        self.assertFalse(rdb.close_ticket(self.db, tid, "approved", COUNCIL))     # doble clic
        self.assertIsNone(rdb.open_ticket_of(self.db, 10))


class Tarifas(Base):
    def test_mapa_con_calidad_bioma_y_lista(self):
        m = rdb.add_mapa(self.db, "Deadpine", "Q6", "Martlock")
        rdb.set_precio(self.db, "ho", m.id, "guild", 1_500_000)
        rdb.set_precio(self.db, "ho", m.id, "alianza", 9_000_000)                # la de alianza no se muestra
        rdb.set_mapa_detalle(self.db, self.mapa.id, "Q3", "Thetford")
        rows = [(x.nombre, x.detalle, p) for x, p in rdb.lista_tarifas(self.db)]
        self.assertEqual(rows, [("Sleetwater", "Q3 · Thetford", None), ("Deadpine", "Q6 · Martlock", 1_500_000)])
        with self.assertRaises(rdb.RentaError):
            rdb.add_mapa(self.db, "Otro", "Q9", "Thetford")

    def test_tarifas_unicas(self):
        self.assertIsNone(rdb.get_tarifa_fija(self.db, "defensa"))
        for k, v in (("defensa", 40_000_000), ("nivel3", 5_000_000), ("recargo_alianza", 50_000_000), ("recargo_sin", 30_000_000),
                     ("pack_q5", 120_000_000), ("pack_q6", 140_000_000)):
            rdb.set_tarifa_fija(self.db, k, v)
            self.assertEqual(rdb.get_tarifa_fija(self.db, k), v)

    def test_migra_base_vieja(self):
        import sqlite3
        old = os.path.join(self.tmp.name, "old.sqlite3")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE rent_mapas (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE COLLATE NOCASE, activo INTEGER NOT NULL DEFAULT 1)")
        c.execute("INSERT INTO rent_mapas(nombre) VALUES('Viejo')")
        c.commit(); c.close()
        rdb.init_schema(old)
        self.assertEqual(rdb.find_mapa(old, "viejo").detalle, "")
        rdb.set_mapa_detalle(old, 1, "Q4", "Lymhurst")
        self.assertEqual(rdb.find_mapa(old, "Viejo").detalle, "Q4 · Lymhurst")

    def test_panel_de_tarifas(self):
        cog = rentas.RentasCog(discord_stub.Bot(), self.db, 1, COUNCIL)
        rdb.set_tarifa_fija(self.db, "defensa", 40_000_000)
        rdb.set_tarifa_fija(self.db, "pack_q5", 120_000_000)
        rdb.set_mapa_detalle(self.db, self.mapa.id, "Q5", "Bridgewatch")
        rdb.set_precio(self.db, "ho", self.mapa.id, "guild", 1_500_000)
        emb = asyncio.run(cog.tarifas_embed("en"))
        vals = [f["value"] if isinstance(f, dict) else getattr(f, "value", f) for f in emb.fields]
        txt = str(vals)
        self.assertIn("40M per phase", txt)
        self.assertIn("HO on Q5: 2-phase defense + 1 free week: **120M**", txt)
        self.assertIn("half is paid upfront", emb.description)
        self.assertIn("ask us", txt)                                              # nivel 3 sin cargar
        self.assertIn("Sleetwater** · Q5 · Bridgewatch — 1.5M", txt)


class Roles(Base):
    def test_etiqueta(self):
        self.assertEqual(rdb.parse_guild_label(" [Timba]  Smogg "), ("Timba", "Smogg"))
        self.assertEqual(rdb.parse_guild_label("Smogg"), (None, "Smogg"))
        self.assertEqual(rdb.parse_guild_label(""), (None, None))
        with self.assertRaises(rdb.RentaError):
            rdb.parse_guild_label("[Timbaaa] Smogg")                            # más de 5
        self.assertEqual(rdb.guild_role_name("Smogg", "Timba"), "[Timba] Smogg")
        self.assertEqual(rdb.guild_role_name("Smogg", None), "Smogg")

    def test_colores_distintos_y_cambio_de_guild(self):
        colores = set()
        for i in range(30):
            c = rdb.next_color(self.db)
            self.assertNotIn(c, colores)
            colores.add(c)
            rdb.save_guild_role(self.db, f"G{i}", None, 100 + i, c)
        a, b = rdb.get_guild_role(self.db, "G1"), rdb.get_guild_role(self.db, "g2")
        self.assertIsNone(rdb.set_user_guild(self.db, 10, a.id))
        self.assertEqual(rdb.set_user_guild(self.db, 10, b.id), a.id)
        self.assertEqual(rdb.user_guild(self.db, 10).guild_nombre, "G2")
        self.assertEqual(rdb.set_guild_tag(self.db, "G2", "Timba").name, "[Timba] G2")     # alianza agregada después
        self.assertEqual(rdb.set_guild_tag(self.db, "G2", None).name, "G2")                # y sacada

    def test_ticket_guarda_guild(self):
        rdb.create_ticket(self.db, 1, 10, "es", "permiso", self.mapa.id, False, False, "personaje", "Pepe", "Smogg", None)
        tk = rdb.ticket_by_channel(self.db, 1)
        self.assertEqual((tk.guild_nombre, tk.alianza_tag), ("Smogg", None))


class Discord(Base):
    def test_textos_completos(self):
        for lang in rdb.IDIOMAS:
            self.assertEqual(set(rentas.T[lang]), set(rentas.T["es"]), lang)

    def test_formularios(self):
        cog = rentas.RentasCog(discord_stub.Bot(), self.db, 1, COUNCIL)
        mapas = rdb.list_mapas(self.db)
        m = rentas.RequestModal(cog, "pt", "rent", mapas, "ho")
        self.assertIsNotNone(m.map_select)
        self.assertEqual(len(m.children), 5)                                    # mapa, extras, para quién, nombre, guild/tag
        self.assertEqual(len(rentas.RequestModal(cog, "es", "rent", mapas, "terri").children), 4)
        many = [rdb.Mapa(i, f"M{i}") for i in range(30)]
        self.assertIsNotNone(rentas.RequestModal(cog, "en", "perm", many).map_text)
        rdb.create_ticket(self.db, 1, 10, "es", "ho", self.mapa.id, True, False, "guild", "Smogg", "Smogg", "Timba", defensas=2)
        am = rentas.ApproveModal(cog, rdb.ticket_by_channel(self.db, 1), 1, 2, 80_000_000, None)
        self.assertEqual(len(am.children), 5)                                   # modalidad, precio, nivel 3, defensa, rol (máx. 5)
        self.assertEqual(am.defensa.default, "80M")
        self.assertIsNone(am.pack)
        opts = [o.value for o in rentas.RequestModal(cog, "es", "rent", mapas, "ho").extras.options]
        self.assertEqual(opts, ["nivel3", "def1", "def2"])                       # sin mapas Q5/Q6 no se ofrece el pack
        rdb.set_mapa_detalle(self.db, self.mapa.id, "Q5", "Thetford")
        opts = [o.value for o in rentas.RequestModal(cog, "es", "rent", rdb.list_mapas(self.db), "ho").extras.options]
        self.assertEqual(opts, ["nivel3", "def1", "def2", "pack"])
        self.assertEqual(am.rol.default, "[Timba] Smogg")
        self.assertEqual(len(rentas.TipoChoiceView(cog, "es", 10).children), 2)
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
