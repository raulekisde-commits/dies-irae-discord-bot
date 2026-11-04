import os, sys, types, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID, STAFF_ROLE  # noqa: E402
import discord  # noqa: E402
import embo_db  # noqa: E402
import registration_db as rdb  # noqa: E402

HISTORICO = ("Fecha y hora\tJugador\tMotivo\tCantidad\tID interno\tFecha de importacion\tOrigen\tNota\n"
             "2026-11-04 16:06:09\tSraGendarme\tDeposit\t10\t7324\t2026-11-04 13:20:44\timport\t\n"
             "2026-11-04 15:59:00\tAnDre04\tWithdrawal\t-10\t7323\t2026-11-04 13:20:44\timport\t\n"
             "2026-11-04 15:25:39\tElNikitooo\tWithdrawal\t-10\t7322\t2026-11-04 13:20:44\timport\t\n"
             "2026-11-04 15:20:12\tElNikitooo\tWithdrawal\t-10\t7321\t2026-11-04 13:20:44\timport\t\n"
             "2026-11-03 14:36:45\tygolue\tWithdrawal\t-10\t7320\t2026-11-04 13:20:44\timport\t\n")
DIARIO = ('"Date"\t"Player"\t"Reason"\t"Amount"\n'
          '"2026-11-04 16:06:09"\t"SraGendarme"\t"Deposit"\t"10"\n'
          '"2026-11-04 15:59:00"\t"AnDre04"\t"Withdrawal"\t"-10"\n'
          '"2026-11-04 11:09:42"\t"Nakesito"\t"Deposit"\t"11"\n'
          '"2026-11-03 07:27:58"\t"CreddyJ"\t"Withdrawal"\t"-10"\n'
          '"2026-11-05 06:44:20"\t"Cozmiic"\t"Withdrawal"\t"-10"\n')


def archivo(text, enc="utf-8"):
    data = text.encode(enc)

    async def read():
        return data
    return types.SimpleNamespace(size=len(data), filename="embo.csv", read=read)


class EmboParseTests(unittest.TestCase):
    def test_lee_los_dos_formatos(self):
        h = embo_db.parse_movements(HISTORICO.encode())
        self.assertEqual(len(h), 5)
        self.assertEqual((h[0].when, h[0].player, h[0].reason, h[0].amount, h[0].day),
                         ("2026-11-04 16:06:09", "SraGendarme", "Deposit", 10, "2026-11-04"))
        self.assertEqual(h[1].amount, -10)
        d = embo_db.parse_movements(DIARIO.encode("utf-16"))
        self.assertEqual([m.player for m in d], ["SraGendarme", "AnDre04", "Nakesito", "CreddyJ", "Cozmiic"])
        self.assertEqual(d[2].amount, 11)

    def test_encabezado_desconocido(self):
        with self.assertRaises(embo_db.EmboError):
            embo_db.parse_movements(b"a\tb\tc\n1\t2\t3\n")


class EmboImportTests(DiscordBase):
    def setUp(self):
        super().setUp()
        rdb.init_schema(self.db)
        rdb.admin_link(self.db, self.alice.id, "SraGendarme", self.admin.id)
        rdb.admin_link(self.db, self.bob.id, "ElNikitooo", self.admin.id)
        rdb.admin_link(self.db, self.council.id, "Nakesito", self.admin.id)
        self.r_staff = discord.Role(STAFF_ROLE, "Staff", self.guild)
        self.guild.roles.append(self.r_staff)
        self.stf = discord.Member(7300, "Stf", roles=[self.r_staff], guild=self.guild)
        self.guild.members.append(self.stf)

    def call(self, cmd, who, *a):
        i = self.inter(who)
        run(getattr(self.ecog, cmd).callback(self.ecog, i, *a))
        return i.text

    def test_historico_solo_admin_carga_y_lista_no_encontrados(self):
        self.assertIn("administrador", self.call("embo_import_history", self.stf, archivo(HISTORICO)))
        self.assertIn("administrador", self.call("embo_import_history", self.council, archivo(HISTORICO)))
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 0)
        text = self.call("embo_import_history", self.admin, archivo(HISTORICO))
        self.assertIn("cargados ahora: **3**", text)
        self.assertIn("Jugadores no encontrados (2)", text)
        self.assertIn("AnDre04", text); self.assertIn("ygolue", text)
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 10)
        self.assertEqual(embo_db.get_balance(self.db, self.bob.id), -20)                 # dos retiros: queda negativo
        text = self.call("embo_import_history", self.admin, archivo(HISTORICO))         # subirlo otra vez no duplica
        self.assertIn("cargados ahora: **0**", text); self.assertIn("ya estaban cargados: **3**", text)
        self.assertEqual(embo_db.get_balance(self.db, self.bob.id), -20)

    def test_cargar_dia_solo_toma_ese_dia_y_no_duplica_con_el_historico(self):
        self.call("embo_import_history", self.admin, archivo(HISTORICO))                # ya cargó SraGendarme 16:06:09
        self.assertIn("Solo el Staff", self.call("embo_load_day", self.alice, archivo(DIARIO), "04/11/2026"))
        text = self.call("embo_load_day", self.stf, archivo(DIARIO, "utf-16"), "04/11/2026")
        self.assertIn("04/11/2026", text)
        self.assertIn("Movimientos de ese día: **3**", text)
        self.assertIn("cargados ahora: **1**", text)                                     # solo Nakesito es nuevo
        self.assertIn("ya estaban cargados: **1**", text)
        self.assertIn("Filas de otros días ignoradas: **2**", text)
        self.assertIn("AnDre04", text); self.assertNotIn("CreddyJ", text); self.assertNotIn("Cozmiic", text)
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 10)
        self.assertEqual(embo_db.get_balance(self.db, self.council.id), 11)
        self.assertIn("Fecha inválida", self.call("embo_load_day", self.stf, archivo(DIARIO), "ayer"))

    def test_encargo_diario_y_se_cierra_al_cargar(self):
        import encargos, encargos_db as edb
        from datetime import datetime, timedelta, timezone
        import botsettings as bs
        tasks_ch = discord.TextChannel(bs.get("tasks"), self.guild, "tareas")             # canal donde se publica el encargo
        self.guild._channels[tasks_ch.id] = tasks_ch
        self.bot.channels[tasks_ch.id] = tasks_ch
        run(encargos.setup_encargos(self.bot, self.db, GUILD_ID))
        run(self.ecog.ensure_embo_daily_task())
        run(self.ecog.ensure_embo_daily_task())                                  # no se repite
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        t = edb.find_by_source(self.db, f"embo:dia:{day}")
        self.assertIsNotNone(t)
        self.assertIn("embotelladas", t.text); self.assertIn("/eco embo cargar-dia", t.text)
        self.assertTrue(tasks_ch.sent)                                                   # se publicó con mención al Staff
        conn = __import__("dbutil").connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM tasks WHERE source LIKE 'embo:dia:%'").fetchone()["c"], 1)
        conn.close()
        self.assertEqual(self.ecog._oldest_open_embo_day(), day)                         # sin fecha: toma el día del encargo
        line = f'"{day} 10:00:00"\t"SraGendarme"\t"Deposit"\t"5"\n'
        self.call("embo_load_day", self.stf, archivo('"Date"\t"Player"\t"Reason"\t"Amount"\n' + line), None)
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 5)
        self.assertEqual(edb.find_by_source(self.db, f"embo:dia:{day}").status, "done")


if __name__ == "__main__":
    unittest.main()
