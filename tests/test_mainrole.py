import os, sys, unittest
from types import SimpleNamespace as NS
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import mainrole as mr  # noqa: E402
import mainrole_db as mdb  # noqa: E402
from dbutil import connect  # noqa: E402

ID = {name: rid for rid, name in mdb.MAIN_ROLES.items()}
ROOT = os.path.dirname(HERE)


def log_msg(uid, main=None, assigned="", title="✅ POSTULACIÓN ACEPTADA", author=None, confirmed=None):
    fields = [NS(name="👤 Postulante", value=f"<@{uid}>"), NS(name="🧑‍💼 Cerrada por", value="<@1>")]
    if confirmed:
        fields.append(NS(name="⚔ Rol main confirmado", value=confirmed))
    if assigned:
        fields.append(NS(name="🎭 Rol asignado", value=assigned))
    if main is not None:
        fields.append(NS(name="⚔ Rol main", value=main))
    return NS(author=author, embeds=[NS(title=title, fields=fields)], jump_url=f"https://discord.com/x/{uid}")


class FakeChannel:
    def __init__(self, msgs):
        self.msgs = msgs  # del más viejo al más nuevo

    async def history(self, *, limit=None, oldest_first=False):
        for m in (self.msgs if oldest_first else list(reversed(self.msgs))):
            yield m


class ParseTests(unittest.TestCase):
    def test_casos(self):
        casos = {"Healer Sagrado": "Healer", "Tanque, supp.": "Tank", "supp, tanque": "Supp", "DPS": "Dps", "daño": "Dps",
                 "Galatinas": "Galatinas", "battle mount / tank": "Battle Mount", "main heal": "Healer", "Clump maker": "Clump Maker",
                 "juego de todo, pero main pierce": "Pierce", "Support": "Supp", "Bomba": "Bomb", "bomb, dps": "Bomb"}
        for txt, exp in casos.items():
            self.assertEqual(mdb.parse_main(txt), ID[exp], txt)
        for txt in ("", "—", "lo que necesiten", "healerr"):
            self.assertIsNone(mdb.parse_main(txt), txt)


class MainRoleTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(mr.setup_mainrole(self.bot, self.db, GUILD_ID))
        self.me = NS(id=999)
        self.bot.user = self.me

    def test_no_pisa_reclutador_ni_manual(self):
        mdb.set_main(self.db, 1, ID["Tank"], "reclutador")
        self.assertFalse(mdb.set_main(self.db, 1, ID["Healer"], "log", overwrite=False))
        self.assertTrue(mdb.set_main(self.db, 2, ID["Healer"], "log", overwrite=False))
        self.assertTrue(mdb.set_main(self.db, 2, ID["Dps"], "log", overwrite=False))
        self.assertEqual(mdb.get_main(self.db, 1)["role_id"], ID["Tank"])
        self.assertEqual(mdb.get_main(self.db, 2)["role_id"], ID["Dps"])

    def test_sync_desde_logs(self):
        mdb.set_main(self.db, 40, ID["Tank"], "manual")
        ch = FakeChannel([
            log_msg(10, "tank", author=self.me),                       # viejo: lo pisa el más nuevo
            log_msg(10, "Healer Sagrado", author=self.me),
            log_msg(20, "lo que haga falta", "Supp, Observación", author=self.me),   # respaldo
            log_msg(30, "ninguno", "Miembro, Observación", author=self.me),          # sin resolver
            log_msg(40, "dps", author=self.me),                        # manual: no se toca
            log_msg(50, "tank", title="❌ POSTULACIÓN RECHAZADA", author=self.me),
            log_msg(60, "tank", author=NS(id=5)),                      # no es del bot
            log_msg(70, "dps", confirmed="Healer", author=self.me),    # manda lo confirmado por el reclutador
        ])
        saved, kept, review = run(self.cog.sync_from_logs(ch))
        self.assertEqual((saved, kept), (3, 1))
        self.assertEqual(mdb.get_main(self.db, 10)["role_id"], ID["Healer"])
        self.assertEqual(mdb.get_main(self.db, 20)["source"], "log_respaldo")
        self.assertEqual(mdb.get_main(self.db, 70)["role_id"], ID["Healer"])
        self.assertIsNone(mdb.get_main(self.db, 30))
        self.assertIsNone(mdb.get_main(self.db, 50))
        self.assertEqual(sorted(d["user_id"] for d, _e, _u in review), [20, 30])
        f = self.cog.review_csv(self.guild, review)
        self.assertEqual(f.filename, "rol_main_revisar.csv")

    def test_porcentaje_main(self):
        conn = connect(self.db)
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE IF NOT EXISTS cta_positions(cta_id INTEGER, user_id INTEGER, slot_key TEXT, role_id INTEGER, role_name TEXT,
                                                     late INTEGER DEFAULT 0, PRIMARY KEY (cta_id, user_id));
            INSERT INTO cta_runs VALUES (1,'closed'),(2,'closed'),(3,'closed'),(4,'closed'),(5,'open');
            INSERT INTO cta_positions VALUES (1,7,'a',{ID['Healer']},'Healer',0),(2,7,'a',{ID['Tank']},'Tank',0),
                                             (3,7,'a',NULL,'healer',0),(4,7,'a',{ID['Dps']},'Dps',0),(5,7,'a',{ID['Healer']},'Healer',0);
        """)
        conn.close()
        self.assertIsNone(mdb.main_share(self.db, 7))
        mdb.set_main(self.db, 7, ID["Healer"], "manual")
        self.assertEqual(mdb.main_share(self.db, 7), (ID["Healer"], 2, 4))


class TopRolSecundario(MainRoleTests.__mro__[1]):
    def setUp(self):
        super().setUp()
        self.cog = run(mr.setup_mainrole(self.bot, self.db, GUILD_ID))
        conn = connect(self.db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE IF NOT EXISTS cta_positions(cta_id INTEGER, user_id INTEGER, slot_key TEXT, role_id INTEGER, role_name TEXT,
                                                     late INTEGER DEFAULT 0, PRIMARY KEY (cta_id, user_id));
        """)
        conn.executemany("INSERT INTO cta_runs VALUES (?,?)", [(i, "closed") for i in range(1, 11)] + [(11, "open")])
        pos = []
        for i in range(1, 11):                                           # 1: Healer main, 2 de 10 healer, 8 tank
            pos.append((i, 1, "a", ID["Healer"] if i <= 2 else ID["Tank"], None))
            pos.append((i, 2, "a", ID["Dps"] if i <= 9 else ID["Supp"], None))   # 2: Dps main, 90 %
        for i in range(1, 4):                                            # 3: solo 3 CTA → no entra
            pos.append((i, 3, "a", ID["Tank"], None))
        pos.append((11, 1, "a", ID["Tank"], None))                       # CTA abierta: no cuenta
        conn.executemany("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (?,?,?,?,?)", pos)
        conn.close()
        for uid, r in ((1, "Healer"), (2, "Dps"), (3, "Healer")):
            mdb.set_main(self.db, uid, ID[r], "manual")

    def test_orden_y_minimo(self):
        rows = mdb.secondary_top(self.db)
        self.assertEqual([d["user_id"] for d in rows], [1, 2])
        self.assertEqual((rows[0]["main"], rows[0]["total"], rows[0]["top_other_id"], rows[0]["top_other_n"]), (2, 10, ID["Tank"], 8))
        self.assertAlmostEqual(rows[1]["pct"], 90.0)


    def test_send_top_sin_bomb_y_con_nombres(self):
        import discord
        bomb = discord.Role(mr.BOMB_ROLE_ID, "Bomb", self.guild)
        self.guild.roles.append(bomb)
        conn = connect(self.db)                              # 4: Healer main que juega Bomb en CTA (sin tener el rol hoy)
        conn.executemany("INSERT INTO cta_positions(cta_id,user_id,slot_key,role_id,role_name) VALUES (?,?,?,?,?)",
                         [(i, 4, "bomb", mr.BOMB_ROLE_ID, "Bomb") for i in range(1, 7)])
        conn.close()
        mdb.set_main(self.db, 4, ID["Healer"], "manual")
        for uid, roles in ((1, []), (2, [bomb]), (4, [])):
            self.guild.members.append(discord.Member(uid, f"u{uid}", roles=roles, guild=self.guild))
        sent = {}

        async def send_message(embed=None, **kw):
            sent["embed"] = embed
        inter = NS(guild=self.guild, user=NS(id=1), response=NS(send_message=send_message))
        run(self.cog.send_top(inter))
        desc = sent["embed"].description
        self.assertIn("<@1>", desc)
        self.assertNotIn("<@2>", desc)                       # tiene el rol Bomb
        self.assertIn("juega más **Tank**", desc)
        self.assertIn("juega más **Bomb**", desc)
        self.assertNotIn("Rol ", desc)


class ReclutamientoFuente(unittest.TestCase):
    """bot.py no se puede importar en las pruebas: se revisa el código fuente."""
    def test_aceptar_pide_y_guarda_main(self):
        src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
        bloque = src[src.index("class RecruitRolesView"):src.index("class RecruitTicketView")]
        self.assertIn("min_values=1, max_values=1", bloque)
        self.assertIn("main_role_id=self.main_id", bloque)
        self.assertIn("_mrdb_recruit.MAIN_ROLES.items()", bloque)        # Bomb se puede elegir como main
        self.assertIn('"reclutador"', src[src.index("async def accept_player"):src.index("class RecruitRolesView")])
        self.assertIn('setup_mainrole, self, DB_PATH', src)


if __name__ == "__main__":
    unittest.main()
