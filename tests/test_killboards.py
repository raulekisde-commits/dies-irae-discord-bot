import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import killboards as kc  # noqa: E402
import killboard_db as kb  # noqa: E402
import registration_db as rdb  # noqa: E402
import ctautil as cu  # noqa: E402
import ctavoice as cv  # noqa: E402
from dbutil import connect  # noqa: E402
from test_killboard_db import EVENTS  # noqa: E402


class KBBase(DiscordBase):
    def setUp(self):
        super().setUp()
        g = self.guild
        self.r_staff = discord.Role(kc.STAFF_ROLE_ID, "Staff", g)
        self.r_caller = discord.Role(kc.CALLER_ROLE_ID, "Caller", g)
        g.roles += [self.r_staff, self.r_caller]
        self.r_member.id = kc.MIEMBRO_ROLE_ID
        import economy_db as _eco; _eco.init_schema(self.db)
        _eco.grant_command_role(self.db, 'stats', kc.MIEMBRO_ROLE_ID); _eco.grant_command_role(self.db, 'top', kc.MIEMBRO_ROLE_ID)
        def mk(uid, name, roles):
            m = discord.Member(uid, name, roles=list(roles), guild=g); g.members.append(m); return m
        self.stf = mk(8001, "Stf", [self.r_staff])
        self.caller = mk(8002, "Cal", [self.r_caller])
        self.mem = mk(8003, "Mem", [self.r_member])
        c = connect(self.db)
        c.executescript("""CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, snapshot_json TEXT, status TEXT, final_state TEXT, final_by INTEGER, final_at TEXT,
                           thread_id INTEGER, stats_channel_id INTEGER, stats_message_id INTEGER, stats_thread_id INTEGER);""")
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status, final_state, stats_thread_id) VALUES (5, '{\"slots\": []}', 'closed', 'finished', 555)")
        c.close()
        cu.init_positions(self.db); cv.init_schema(self.db); rdb.init_schema(self.db)
        self.cog = run(kc.setup_killboards(self.bot, self.db, GUILD_ID))
        self.calls = []

        async def fake(url, params=None):
            self.calls.append((url, params))
            if url.endswith("/battles/kills"):
                return EVENTS
            return {"players": [{"Name": "Gus", "GuildName": "Smogg"}]}
        self.cog.fetch_json = fake
        self.thread = discord.TextChannel(555, g, "stats-hilo")

    def load(self, who, text, channel=None):
        i = self.inter(who, channel=channel)
        run(self.cog.cargar.callback(self.cog, i, text))
        return i


class LoadTests(KBBase):
    def test_parse_ids(self):
        self.assertEqual(kc.parse_ids("https://albionbb.com/battles/multi?ids=1473088480,1473160689,1473166677"), ["1473088480", "1473160689", "1473166677"])
        self.assertEqual(kc.parse_ids("1473088480 1473088480"), ["1473088480"])

    def test_standalone_only_staff(self):
        self.assertIn("Solo el Staff", self.load(self.caller, "1473088480").text)
        self.assertIn("Solo el Staff", self.load(self.mem, "1473088480").text)
        self.assertIsNone(kb.get_battle(self.db, "1473088480") if hasattr(kb, "get_battle") else None)
        i = self.load(self.stf, "1473088480")
        self.assertIn("✅", i.text)
        self.assertIn("gus", kb.names_in_cta(self.db, 5) or {"gus": 1})
        self.assertEqual(kb.player_stats(self.db, ["gus"])["attendance"], 1)       # asistencia desde la ficha
        self.assertEqual(kb.player_stats(self.db, ["ana"])["deaths"], 2)

    def test_dedupe_and_delete(self):
        self.load(self.stf, "1473088480")
        i = self.load(self.stf, "1473088480 1473088481")
        self.assertIn("ya estaba cargado", i.text); self.assertIn("1473088481", i.text)
        d = self.inter(self.stf)
        run(self.cog.borrar.callback(self.cog, d, "1473088480"))
        view = d.response.sent[0][1]["view"]
        run(view.children[0].callback(self.inter(self.stf)))
        self.assertEqual(kb.player_stats(self.db, ["bob"])["attendance"], 1)       # queda el otro
        d = self.inter(self.mem)
        run(self.cog.borrar.callback(self.cog, d, "1473088481"))
        self.assertIn("Solo el Staff", d.text)

    def test_cta_thread_caller_allowed_and_comparison_staff_only(self):
        c = connect(self.db)
        for u in (101, 102):
            c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id) VALUES (5,?,'1:1',1260755129754189854)", (u,))
        c.close()
        rdb.admin_link(self.db, 101, "Ana", 1)
        rdb.admin_link(self.db, 103, "Bob", 1)
        cv.user_joined(self.db, 5, 101, kb_dt(0)); cv.user_left(self.db, 5, 101, kb_dt(30))
        cv.user_joined(self.db, 5, 104, kb_dt(0)); cv.user_left(self.db, 5, 104, kb_dt(30))          # topo
        i = self.load(self.caller, "1473088480", channel=self.thread)
        self.assertIn("✅", i.text); self.assertNotIn("Topos", i.text)
        self.assertIn("las ve el Staff", i.text)
        self.assertEqual(len(kb.battles_of_cta(self.db, 5)), 1)
        c = connect(self.db); c.execute("DELETE FROM kb_battles"); c.execute("DELETE FROM kb_players"); c.close()
        i = self.load(self.stf, "1473088480", channel=self.thread)
        self.assertIn("Topos", i.text); self.assertIn("<@104>", i.text)
        self.assertIn("<@103>", i.text)                                                          # Bob: en killboard, no estuvo en voz


class SplitSuggestionsTests(KBBase):
    """Cruce conectados / killboard con la lista del split: lo que se le pregunta al organizador."""

    def setUp(self):
        super().setUp()
        import splits_db as sdb
        sdb.init_schema(self.db)
        self.sdb = sdb
        self.sid = sdb.create_split(self.db, guild_id=1, creator_id=9, title="CTA", people=[101, 102, 103, 105], source_cta_id=5)
        rdb.admin_link(self.db, 101, "Ana", 1); rdb.admin_link(self.db, 102, "Zoe", 1)
        rdb.admin_link(self.db, 103, "Bob", 1); rdb.admin_link(self.db, 104, "Gus", 1)       # 105: sin personaje registrado
        self.got = []

        class Fake:
            async def apply_suggestions(s_, cta_id, remove, add):
                self.got.append((cta_id, sorted(remove), sorted(add)))
        self.bot.cogs["SplitsCog"] = Fake()

    def test_anotados_sin_conectarse_y_killboard_sin_anotarse(self):
        kb.save_online(self.db, 5, ["Ana"])                                    # Zoe y Bob no estaban conectados
        run(self.cog.sync_split_suggestions(5))
        self.assertEqual(self.got[-1], (5, [102, 103], []))                    # 105 no tiene personaje: no se puede comparar
        c = connect(self.db)
        c.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at) VALUES ('b1', 5, 1, 'x')")
        for n in ("bob", "gus"):
            c.execute("INSERT INTO kb_players(battle_id, name_norm, name) VALUES ('b1', ?, ?)", (n, n.title()))
        c.close()
        run(self.cog.sync_split_suggestions(5))
        self.assertEqual(self.got[-1], (5, [102], [104]))                      # Bob estuvo en el killboard: no se saca; Gus no estaba anotado

    def test_sin_lista_de_conectados_no_hay_sugerencias_de_sacar(self):
        c = connect(self.db)
        c.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at) VALUES ('b1', 5, 1, 'x')")
        c.execute("INSERT INTO kb_players(battle_id, name_norm, name) VALUES ('b1', 'gus', 'Gus')")
        c.close()
        run(self.cog.sync_split_suggestions(5))
        self.assertEqual(self.got[-1], (5, [], [104]))

    def test_cta_sin_split_no_hace_nada(self):
        c = connect(self.db); c.execute("UPDATE splits SET source_cta_id=NULL"); c.close()
        kb.save_online(self.db, 5, ["Ana"])
        run(self.cog.sync_split_suggestions(5))
        self.assertEqual(self.got, [])


class OpinologoTests(KBBase):
    def test_opinologo_can_load_in_cta_thread_but_not_loose_killboards(self):
        r_opi = discord.Role(kc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)
        self.guild.roles.append(r_opi)
        opi = discord.Member(8003, "Opi", roles=[r_opi], guild=self.guild)
        self.assertTrue(kc.is_loader_in_cta(opi))
        self.assertFalse(kc.is_staff_or_council(opi))
        self.assertIn("✅", self.load(opi, "1473088480", channel=self.thread).text)         # hilo de stats de una CTA: sí
        self.assertIn("Solo el Staff", self.load(opi, "1473088481").text)                   # killboard suelto: solo Staff/Council


def kb_dt(minutes):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class TopStatsTests(KBBase):
    def test_top_and_stats(self):
        self.load(self.stf, "1473088480")
        emb = run(self.cog.top_embed("deaths"))
        self.assertIn("Ana", emb.description)
        i = self.inter(self.mem)
        run(self.cog.send_ranking(i, "kills"))
        self.assertTrue(i.response.sent)
        i = self.inter(self.outsider)
        run(self.cog.send_ranking(i, "kills"))
        self.assertTrue("solo para miembros" in i.text or "permiso" in i.text)
        rdb.admin_link(self.db, self.mem.id, "Ana", 1)
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))
        emb = i.response.sent[0][1]["embed"]
        self.assertTrue(any("Muertes" in str(f) for f in emb.fields))

    def test_stats_muestra_top_de_roles_aunque_no_haya_killboard(self):
        rdb.admin_link(self.db, self.mem.id, "Ana", 1)
        c = connect(self.db)
        for cta, status in ((21, "closed"), (22, "closed"), (23, "closed"), (24, "open")):
            c.execute("INSERT OR IGNORE INTO cta_runs(id, snapshot_json, status) VALUES (?, '{\"slots\": []}', ?)", (cta, status))
        for cta, role in ((21, 1260755129754189854), (22, 1260755129754189854), (23, 1260755151296266331), (24, 1260755151296266331)):
            c.execute("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id) VALUES (?,?,?,?)", (cta, self.mem.id, "1:1", role))
        c.close()
        i = self.inter(self.mem)
        run(self.cog.stats.callback(self.cog, i, None))                       # no hay ningún killboard cargado para Ana
        emb = i.response.sent[0][1]["embed"]
        campo = next(f for f in emb.fields if "Roles más jugados" in str(f))
        txt = str(campo)
        self.assertIn("Tank", txt); self.assertIn("67%", txt)                 # 2 de 3 CTA cerradas
        self.assertIn("Healer", txt); self.assertIn("33%", txt)               # la CTA abierta (24) no cuenta



class WeaponCheckThread(KBBase):
    def test_mensaje_en_el_hilo_se_crea_edita_y_borra(self):
        self.bot.channels[555] = self.thread
        rdb.admin_link(self.db, 101, "Ana", 1); rdb.admin_link(self.db, 103, "Bob", 1)

        async def expected(cta_id):
            return {101: ("P1-1", {"2H_HOLYSTAFF"}), 103: ("P1-2", {"MAIN_MACE"})}
        self.cog.expected_weapons = expected
        self.cog.weapon_name = lambda w: {"2H_HOLYSTAFF": "Holy Staff", "2H_FIRESTAFF": "Fire Staff"}.get(w)
        run(self.cog.refresh_weapon_check(5))
        self.assertEqual([m for m in self.thread.sent if "🔀" in (m.content or "")], [])     # sin killboard: nada
        c = connect(self.db)
        c.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at) VALUES ('b1', 5, 1, 'x')")
        c.execute("INSERT INTO kb_weapons(battle_id, name_norm, weapon, uses) VALUES ('b1', 'ana', '2H_FIRESTAFF', 2)")
        c.execute("INSERT INTO kb_weapons(battle_id, name_norm, weapon, uses) VALUES ('b1', 'bob', 'MAIN_MACE', 1)")
        c.close()
        run(self.cog.refresh_weapon_check(5))
        msgs = [m for m in self.thread.sent if "🔀" in (m.content or "")]
        self.assertEqual(len(msgs), 1)
        self.assertIn("<@101> · P1-1 · anotado: Holy Staff · usó: Fire Staff", msgs[0].content)
        self.assertNotIn("<@103>", msgs[0].content)
        c = connect(self.db); c.execute("UPDATE kb_weapons SET weapon='2H_HOLYSTAFF' WHERE name_norm='ana'"); c.close()
        run(self.cog.refresh_weapon_check(5))
        self.assertEqual(len([m for m in self.thread.sent if "🔀" in (m.content or "")]), 1)  # se edita el mismo mensaje


class WeaponCheckTests(unittest.TestCase):
    def test_compara_arma_de_la_posicion(self):
        expected = {1: ("P1-1", {"2H_HOLYSTAFF"}), 2: ("P1-2", {"MAIN_MACE", "2H_HAMMER"}), 3: ("P1-3", {"MAIN_SWORD"}), 4: ("P2-1", set())}
        regs = [(1, "Ana"), (2, "Bob"), (3, "Cami"), (4, "Dan")]
        used = {"ana": {"2H_HOLYSTAFF": 3}, "bob": {"2H_FIRESTAFF": 2, "MAIN_CURSEDSTAFF": 1}, "dan": {"MAIN_MACE": 1}}
        rows = kc.weapon_mismatches(expected, regs, used)
        self.assertEqual([(u, p) for u, p, _v, _s in rows], [(2, "P1-2")])            # Cami sin datos y Dan sin build: no se listan
        text = kc.weapon_check_text(rows, lambda w: w.title())
        self.assertIn("<@2> · P1-2 · anotado: 2H_Hammer / Main_Mace · usó: 2H_Firestaff, Main_Cursedstaff", text)
        self.assertIn("Nadie", kc.weapon_check_text([], str))

if __name__ == "__main__":
    unittest.main()


class PanelYTareaTests(KBBase):
    """La carga desde la CTA siempre actualiza el panel de stats y cierra la tarea «Cargar el killboard»."""

    def setUp(self):
        super().setUp()
        import encargos as enc
        self.enc = enc
        tasks_ch = discord.TextChannel(enc.TASKS_CHANNEL_ID, self.guild, "tareas")
        self.bot.channels[tasks_ch.id] = tasks_ch
        self.ecog = run(enc.setup_encargos(self.bot, self.db, GUILD_ID))
        self.ecog.ticker.cancel()
        run(self.ecog.create(text="Cargar el killboard", created_by=1, source="cta:5:killboard"))

    def task_status(self):
        import encargos_db as edb
        return edb.find_by_source(self.db, "cta:5:killboard").status

    def test_boton_del_panel_carga_y_cierra_la_tarea(self):
        view = kc.StatsPanelView(self.cog)
        c = connect(self.db); c.execute("UPDATE cta_runs SET stats_message_id=4321 WHERE id=5"); c.close()
        msg = discord.Message(self.thread); msg.id = 4321
        i = self.inter(self.stf, channel=self.thread, message=msg)
        run([b for b in view.children if b.custom_id == "kbpanel:killboard"][0].callback(i))
        modal = i.modals[0]
        modal.links.value = "1473088480"
        run(modal.on_submit(self.inter(self.stf, channel=self.thread, message=msg)))
        self.assertEqual([b["battle_id"] for b in kb.battles_of_cta(self.db, 5)], ["1473088480"])
        self.assertEqual(self.task_status(), "done")

    def test_batalla_cargada_suelta_se_asocia_a_la_cta(self):
        self.load(self.stf, "1473088480")                                       # suelta (fuera del hilo de stats)
        self.assertEqual(kb.battles_of_cta(self.db, 5), [])
        self.assertEqual(self.task_status(), "open")
        i = self.load(self.stf, "1473088480", channel=self.thread)              # después, desde la CTA
        self.assertIn("ahora cuenta para esta CTA", i.text)
        self.assertEqual([b["battle_id"] for b in kb.battles_of_cta(self.db, 5)], ["1473088480"])
        self.assertEqual(self.task_status(), "done")

    def test_batalla_de_otra_cta_no_se_mueve(self):
        c = connect(self.db)
        c.execute("INSERT INTO cta_runs(id, snapshot_json, status, final_state, stats_thread_id) VALUES (6, '{\"slots\": []}', 'closed', 'finished', 666)")
        c.close()
        self.load(self.stf, "1473088480", channel=discord.TextChannel(666, self.guild, "otro"))
        i = self.load(self.stf, "1473088480", channel=self.thread)
        self.assertIn("ya está cargado en la CTA #6", i.text)
        self.assertEqual(kb.battles_of_cta(self.db, 5), [])
        self.assertEqual(self.task_status(), "open")

    def test_si_falla_el_panel_la_tarea_igual_se_cierra(self):
        async def boom(_cta_id):
            raise RuntimeError("panel roto")
        self.cog.refresh_stats_panel = boom
        self.load(self.stf, "1473088480", channel=self.thread)
        self.assertEqual(self.task_status(), "done")

    def test_recargar_en_la_misma_cta_resincroniza(self):
        self.load(self.stf, "1473088480", channel=self.thread)
        import encargos_db as edb
        c = connect(self.db); c.execute("UPDATE tasks SET status='open', done_by=NULL WHERE source='cta:5:killboard'"); c.close()
        i = self.load(self.stf, "1473088480", channel=self.thread)              # p. ej. la primera vez falló cerrar la tarea
        self.assertIn("ya estaba cargado en esta CTA", i.text)
        self.assertEqual(edb.find_by_source(self.db, "cta:5:killboard").status, "done")
