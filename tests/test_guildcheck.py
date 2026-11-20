"""/miembros (guildcheck.py): última conexión desde el export, asistencia a CTA, lista para expulsar y avisos de salida de la guild."""
import os
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import coaching_db as cdb  # noqa: E402
import guildcheck as gc  # noqa: E402
import guildcheck_db as gdb  # noqa: E402
import registration as rg  # noqa: E402
import registration_db as rdb  # noqa: E402
from dbutil import connect  # noqa: E402

NOW = datetime(2026, 11, 15, 13, 30, tzinfo=timezone.utc)
EXPORT = ('"Character Name"\t"Last Seen"\t"Roles"\n'
          '"Daitza"\t"Online"\t"Officer;Council"\n'
          '"zabonin"\t"11/15/2026 13:24:04"\t""\n'
          '"Kingnian"\t"10/21/2026 22:57:57"\t""\n'
          '"Andresra"\t"10/20/2026 05:02:02"\t"Siphoned"\n')


class ParseTests(unittest.TestCase):
    def test_parse_last_seen_utf8_y_utf16(self):
        for raw in (EXPORT.encode("utf-8"), EXPORT.encode("utf-16")):
            rows = gdb.parse_last_seen(raw, NOW)
            self.assertEqual([r[0] for r in rows], ["Daitza", "zabonin", "Kingnian", "Andresra"])
            self.assertEqual(rows[0][1:], (NOW, True))                                 # Online = hora del archivo
            self.assertEqual(rows[2][1], datetime(2026, 10, 21, 22, 57, 57, tzinfo=timezone.utc))

    def test_attendance_since(self):
        ctas = [(NOW - timedelta(days=d), {1} if d % 2 else set()) for d in range(28, 0, -1)]
        self.assertEqual(gdb.attendance_since(ctas, 1, NOW - timedelta(days=60), NOW), (14, 20, 28))   # desde el 1er killboard
        self.assertEqual(gdb.attendance_since(ctas, 1, NOW - timedelta(days=7), NOW), (4, 5, 7))       # entró hace 1 semana
        self.assertIsNone(gdb.attendance_since(ctas, 1, NOW - timedelta(hours=10), NOW))              # recién entró
        self.assertIsNone(gdb.attendance_since([], 1, NOW - timedelta(days=7), NOW))

    def test_week_bounds(self):
        s, e = gdb.week_bounds(-1, NOW)                      # domingo 15/11 → semana anterior lun 2/11 a lun 9/11
        self.assertEqual((s.day, e.day, s.weekday()), (2, 9, 0))


class GuildCheckTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.reg = run(rg.setup_registration(self.bot, self.db, GUILD_ID, required_guild="Smogg", albion_guild_id="GID",
                                             member_role_id=self.r_member.id))
        self.cog = run(gc.setup_guildcheck(self.bot, self.db, GUILD_ID))
        self.p = mock.patch.object(gc, "MIEMBRO_ROLE_ID", self.r_member.id)
        self.p.start()
        self.staff = discord.Member(777, "Staffer", roles=[discord.Role(rg.STAFF_ROLE_ID, "Staff", self.guild)], guild=self.guild)
        self.guild.members.append(self.staff)
        rdb.admin_link(self.db, self.alice.id, "Kingnian", 1, char_id="id-k")
        rdb.admin_link(self.db, self.bob.id, "zabonin", 1, char_id="id-z")

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_permisos(self):
        i = self.inter(self.alice)
        run(self.cog.do_inactivos(i))
        self.assertIn("🔒", i.text)
        i = self.inter(self.alice)
        run(self.cog.do_kick_lista(i))
        self.assertIn("🔒", i.text)

    def test_inactivos_con_archivo_y_dueño(self):
        async def read():
            return EXPORT.encode("utf-16")
        archivo = types.SimpleNamespace(size=100, read=read)
        i = self.inter(self.staff)
        run(self.cog.do_inactivos(i, 7, archivo))
        out = i.text
        self.assertIn("**2** de 4", out)
        self.assertIn(f"**Kingnian** → <@{self.alice.id}>", out)
        self.assertIn("**Andresra** → sin Discord", out)
        self.assertNotIn("zabonin", out)

    def _cta(self, cta_id, closes_at, signed, voice=(), kb=(), minutes=30):
        conn = connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cta_runs (id INTEGER PRIMARY KEY, guild_id INTEGER, status TEXT, closes_at TEXT, kind TEXT DEFAULT 'cta');
                CREATE TABLE IF NOT EXISTS cta_positions (cta_id INTEGER, user_id INTEGER, slot_key TEXT, PRIMARY KEY (cta_id, user_id));
                CREATE TABLE IF NOT EXISTS cta_voice_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, cta_id INTEGER, user_id INTEGER, joined_at TEXT, left_at TEXT);
                CREATE TABLE IF NOT EXISTS kb_battles (battle_id TEXT PRIMARY KEY, cta_id INTEGER);
                CREATE TABLE IF NOT EXISTS kb_players (battle_id TEXT, name_norm TEXT, name TEXT);""")
            conn.execute("INSERT INTO cta_runs(id, guild_id, status, closes_at) VALUES (?,?,?,?)", (cta_id, GUILD_ID, "closed", closes_at.isoformat()))
            for u in signed:
                conn.execute("INSERT INTO cta_positions(cta_id, user_id, slot_key) VALUES (?,?,?)", (cta_id, u, "p1"))
            for u in voice:
                conn.execute("INSERT INTO cta_voice_sessions(cta_id, user_id, joined_at, left_at) VALUES (?,?,?,?)",
                             (cta_id, u, closes_at.isoformat(), (closes_at + timedelta(minutes=minutes)).isoformat()))
            conn.execute("INSERT OR IGNORE INTO kb_battles VALUES (?,?)", (f"b{cta_id}", cta_id))
            for n in kb:
                conn.execute("INSERT INTO kb_players VALUES (?,?,?)", (f"b{cta_id}", gdb.norm(n), n))
        finally:
            conn.close()

    def test_asistencia_anotado_y_voz_o_killboard(self):
        start, end = gdb.week_bounds(-1)
        a, b = self.alice.id, self.bob.id
        self._cta(1, start + timedelta(hours=20), [a, b], voice=[a])               # Alice: anotada+voz ✔ · Bob: solo anotado ✘
        self._cta(2, start + timedelta(days=1), [b], kb=["zabonin"])               # Bob: anotado+killboard ✔
        self._cta(3, start + timedelta(days=2), [], voice=[a])                     # en voz sin anotarse: no cuenta
        n, att = gdb.cta_attendance(self.db, GUILD_ID, start, end, gdb.chars_by_user(self.db))
        self.assertEqual(n, 3)
        self.assertEqual(att, {a: 1, b: 1})
        self._cta(4, start + timedelta(days=3), [a], voice=[a], minutes=15)        # anotada pero solo 15 min en voz: no cuenta
        self._cta(5, start + timedelta(days=4), [], kb=["Kingnian"])               # killboard sin anotarse: cuenta
        n, att = gdb.cta_attendance(self.db, GUILD_ID, start, end, gdb.chars_by_user(self.db))
        self.assertEqual((n, att), (5, {a: 2, b: 1}))
        self.assertEqual(gdb.cta_attendance(self.db, GUILD_ID, None, None, gdb.chars_by_user(self.db), user_id=a)[1], {a: 2})

    def test_comando_asistencias(self):
        start, _ = gdb.week_bounds(0)
        self._cta(1, start + timedelta(minutes=1), [self.alice.id], voice=[self.alice.id])
        i = self.inter(self.alice)
        run(self.cog.do_asistencias(i))
        self.assertIn("🔒", i.text)
        i = self.inter(self.staff)
        run(self.cog.do_asistencias(i, "actual"))
        self.assertIn(f"<@{self.alice.id}> · **1/5** ⚠️", i.text)           # el mínimo es 5 aunque haya habido 1 CTA
        self.assertIn(f"<@{self.bob.id}> · **0/5** ⚠️", i.text)
        self.assertIn("Debajo de 5", i.text)
        i = self.inter(self.staff)
        run(self.cog.do_asistencias(i, "actual", self.alice))
        self.assertIn("Esta semana: **1/5** (1 CTA hechas)", i.text)
        self.assertIn("Total: **1**", i.text)

    def test_kick_lista_por_motivo_y_ausencia(self):
        gdb.save_from_export(self.db, EXPORT.encode(), datetime.now(timezone.utc))
        start, end = gdb.week_bounds(-1)
        self._cta(1, start + timedelta(hours=5), [self.bob.id], voice=[self.bob.id])
        import ausencias
        ausencias.init_schema(self.db)
        ausencias.add_ausencia(self.db, GUILD_ID, self.alice.id, "viaje", 10)
        cdb.init_schema(self.db)
        obs = discord.Role(gc.OBSERVACION_ROLE_ID, "Observación", self.guild)
        self.guild.roles.append(obs)
        self.bob.roles.append(obs)
        i = self.inter(self.staff)
        run(self.cog.do_kick_lista(i, "anterior"))
        out = i.text
        inact = out.split("💤")[1].split("⚔️")[0]
        self.assertIn(f"<@{self.alice.id}> (**Kingnian**)", inact)
        self.assertIn("Tiene /ausencia", inact)
        self.assertNotIn(str(self.bob.id), inact)
        cta = out.split("⚔️")[1].split("🎥")[0]
        self.assertIn("menos de 1 de 1", cta)                                       # 1 sola CTA en la semana → se exige 1
        self.assertIn(f"<@{self.alice.id}> · **0/1**", cta)
        self.assertNotIn(f"<@{self.bob.id}>", cta)
        vods = out.split("🎥")[1]
        self.assertIn(f"<@{self.bob.id}> · **0/3**", vods)
        self.assertNotIn(str(self.alice.id), vods)                                  # no es de Observación
        self.assertNotIn(str(self.staff.id), out)                                   # el Staff no se evalúa

    def _leave_setup(self, roster_names, current_guild="OtraGuild"):
        self.alerts = discord.TextChannel(5555, self.guild, "avisos")
        self.guild._channels[self.alerts.id] = self.alerts
        gdb.set_setting(self.db, gdb.CHANNEL_KEY, str(self.alerts.id))

        async def fake(url, params=None):
            if "/guilds/GID/members" in url:
                return [{"Id": f"id-{n[0].lower()}", "Name": n, "GuildName": "Smogg"} for n in roster_names]
            if "/players/" in url:
                return {"Id": url.rsplit("/", 1)[1], "Name": "x", "GuildName": current_guild}
            raise RuntimeError("HTTP 503")
        self.reg.fetch_json = fake

    def test_aviso_de_salida_una_vez_y_expulsar(self):
        self._leave_setup(["Kingnian"])                                             # Bob (zabonin) ya no figura
        self.assertEqual(run(self.cog.check_leavers()), 1)
        self.assertEqual(run(self.cog.check_leavers()), 0)                          # no se repite
        msg = self.alerts.sent[-1]
        self.assertIn("zabonin", msg.embeds[0].description)
        self.assertIn("OtraGuild", msg.embeds[0].description)
        i = self.inter(self.alice, message=msg)
        run(self.cog.alert_kick(i))
        self.assertIn("🔒", i.text)
        kicked = []

        async def kick(reason=None):
            kicked.append(reason)
        self.bob.kick = kick
        i = self.inter(self.staff, message=msg)
        with mock.patch("expulsar.check_target", return_value=None):
            run(self.cog.alert_kick(i))
            confirm = i.response.sent[-1][1]["view"]
            j = self.inter(self.staff)
            run(confirm.children[0].callback(j))
        self.assertEqual(len(kicked), 1)
        self.assertEqual(gdb.alert_by_message(self.db, msg.id)["status"], "kicked")
        i = self.inter(self.staff, message=msg)
        run(self.cog.alert_ignore(i))
        self.assertIn("ya fue resuelto", i.text)

    def test_sin_aviso_si_la_api_dice_que_sigue_en_la_guild_o_si_es_invitado(self):
        self._leave_setup(["Kingnian"], current_guild="Smogg")
        self.assertEqual(run(self.cog.check_leavers()), 0)
        self._leave_setup(["Kingnian"])
        import invitados
        invitados.ensure_schema(self.db)
        invitados.add_guest(self.db, GUILD_ID, self.bob.id, 1, "amigo", kind="premium")
        self.assertEqual(run(self.cog.check_leavers()), 0)

    def test_si_vuelve_a_la_guild_se_borra_el_aviso(self):
        self._leave_setup(["Kingnian"])
        run(self.cog.check_leavers())
        self.assertIn(self.bob.id, gdb.all_alert_ids(self.db))
        self._leave_setup(["Kingnian", "zabonin"])
        self.reg.roster_at = 0
        run(self.cog.check_leavers())
        self.assertNotIn(self.bob.id, gdb.all_alert_ids(self.db))

    def test_caida_brusca_de_la_lista_no_avisa(self):
        gdb.set_setting(self.db, "last_roster_size", "100")
        self._leave_setup(["Kingnian"])
        self.assertEqual(run(self.cog.check_leavers()), 0)

    def test_canal_avisos_solo_council(self):
        i = self.inter(self.staff)
        run(self.cog.do_canal_avisos(i, self.panelch))
        self.assertIn("🔒", i.text)
        i = self.inter(self.council)
        run(self.cog.do_canal_avisos(i, self.panelch))
        self.assertEqual(gdb.get_setting(self.db, gdb.CHANNEL_KEY), str(self.panelch.id))

    def test_panel_privado_y_solo_para_quien_lo_abrio(self):
        i = self.inter(self.staff)
        run(self.cog.panel.callback(self.cog, i))
        content, kw = i.response.sent[-1]
        self.assertTrue(kw.get("ephemeral"))
        view = kw["view"]
        self.assertEqual(view.timeout, gc.PANEL_TIMEOUT)
        self.assertFalse(run(view.interaction_check(self.inter(self.council))))
        self.assertTrue(run(view.interaction_check(self.inter(self.staff))))

    def test_limpiar_registros_sin_rol_miembro(self):
        rdb.admin_link(self.db, self.outsider.id, "Fantasma", 1, char_id="id-f")        # sin rol Miembro: se libera
        obs = discord.Role(gc.OBSERVACION_ROLE_ID, "Observación", self.guild)
        self.council.roles.append(obs)
        rdb.admin_link(self.db, self.council.id, "Obsi", 1, char_id="id-o")             # Observación: exceptuado
        rdb.admin_link(self.db, self.staff.id, "Jefe", 1, char_id="id-j")               # Staff: exceptuado
        i = self.inter(self.alice)
        run(self.cog.do_limpiar_registros(i))
        self.assertIn("🔒", i.text)
        i = self.inter(self.staff)
        run(self.cog.do_limpiar_registros(i))
        self.assertIn("Fantasma", i.text)
        self.assertNotIn("Obsi", i.text)
        self.assertNotIn("Jefe", i.text)
        self.assertNotIn("Kingnian", i.text)                                            # tiene rol Miembro
        view = i.response.sent[0][1]["view"]
        j = self.inter(self.staff)
        run(view.children[0].callback(j))
        self.assertIsNone(rdb.get_registration(self.db, self.outsider.id))
        self.assertIsNotNone(rdb.get_registration(self.db, self.council.id))
        self.assertIsNotNone(rdb.get_registration(self.db, self.alice.id))
        self.assertIn("Liberé el registro de **1**", j.text)

    def test_grupo_y_comandos_viejos_fuera(self):
        names = set(gc.GuildCheckCog.miembros_g.commands)
        self.assertEqual(names, {"panel", "sin-vincular", "fuera-discord", "inactivos", "kick-lista", "fuera-guild", "canal-avisos", "asistencias", "poca-asistencia", "antiguedad", "limpiar-registros"})
        self.assertFalse(hasattr(self.reg.sin_albion, "callback"))


if __name__ == "__main__":
    unittest.main()
