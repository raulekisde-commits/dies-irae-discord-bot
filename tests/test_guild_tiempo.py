"""Antigüedad y días en la season por guild (Smogg / Smogged): tramos en guild_periods y su suma."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import registration_db as rdb  # noqa: E402
import statscard as sc  # noqa: E402

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class GuildTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        rdb.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def set_period_dates(self, *dates):
        """Pisa start/end de los tramos de la persona 1 en orden de id (para no depender de la hora real)."""
        conn = sqlite3.connect(self.db)
        ids = [r[0] for r in conn.execute("SELECT id FROM guild_periods ORDER BY id")]
        for pid, (a, b) in zip(ids, dates):
            conn.execute("UPDATE guild_periods SET start_at=?, end_at=? WHERE id=?", (iso(a), iso(b) if b else None, pid))
        conn.commit(); conn.close()

    def test_pase_cierra_smogg_y_abre_smogged_sin_perder_lo_anterior(self):
        rdb.admin_link(self.db, 1, "Ana", 9, guild_name="Smogg")
        rdb.set_guild(self.db, 1, "Smogged", 9)
        rdb.set_guild(self.db, 1, "Smogged", 9)                    # mismo destino: no abre otro tramo
        self.set_period_dates((T0, T0 + timedelta(days=10)), (T0 + timedelta(days=10), None))
        now = T0 + timedelta(days=12, hours=5)
        self.assertEqual(rdb.guild_seconds(self.db, 1, now=now), {"Smogg": 10 * 86400, "Smogged": (2 * 24 + 5) * 3600})
        since = T0 + timedelta(days=9)                              # season empezada a mitad del tramo de Smogg
        self.assertEqual(rdb.guild_seconds(self.db, 1, since, now), {"Smogg": 86400, "Smogged": (2 * 24 + 5) * 3600})

    def test_volver_a_smogg_suma_los_dos_tramos(self):
        rdb.admin_link(self.db, 1, "Ana", 9, guild_name="Smogg")
        rdb.set_guild(self.db, 1, "Smogged", 9)
        rdb.set_guild(self.db, 1, "Smogg", 9)
        d = lambda n: T0 + timedelta(days=n)  # noqa: E731
        self.set_period_dates((d(0), d(3)), (d(3), d(5)), (d(5), None))
        self.assertEqual(rdb.guild_seconds(self.db, 1, now=d(6)), {"Smogg": 4 * 86400, "Smogged": 2 * 86400})

    def test_irse_y_volver_no_pierde_lo_del_personaje(self):
        rdb.admin_link(self.db, 1, "Ana", 9, char_id="id-ana", guild_name="Smogg")
        rdb.unlink(self.db, 1, None, None, "el miembro salió del servidor")
        conn = sqlite3.connect(self.db)
        self.assertIsNotNone(conn.execute("SELECT end_at FROM guild_periods").fetchone()[0])   # cerrado, no borrado
        conn.close()
        rdb.admin_link(self.db, 2, "Ana", 9, char_id="id-ana", guild_name="Smogg")             # vuelve con otra cuenta de Discord
        d = lambda n: T0 + timedelta(days=n)  # noqa: E731
        self.set_period_dates((d(0), d(4)), (d(6), None))
        self.assertEqual(rdb.guild_seconds(self.db, 2, now=d(7)), {"Smogg": 5 * 86400})
        self.assertEqual(rdb.guild_seconds(self.db, 1, now=d(7)), {})                          # el dato es del personaje

    def test_completar_id_mueve_los_tramos_del_nombre(self):
        rdb.admin_link(self.db, 1, "Ana", 9)                        # sin ID (API caída)
        reg = rdb.get_registration(self.db, 1)
        rdb.fill_registration(self.db, reg.id, "id-ana", "Américas", "Smogged", 9)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT char_key, guild_name FROM guild_periods").fetchall(), [("id-ana", "Smogged")])
        conn.close()

    def test_registros_viejos_arrancan_desde_registered_at(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO albion_links(discord_id, char_name, guild_name, registered_at, updated_at) VALUES (7, 'Viejo', 'Smogged', ?, ?)",
                     (iso(T0), iso(T0)))
        conn.commit(); conn.close()
        rdb.init_schema(self.db)
        rdb.init_schema(self.db)                                    # idempotente
        self.assertEqual(rdb.guild_seconds(self.db, 7, now=T0 + timedelta(days=2)), {"Smogged": 2 * 86400})

    def test_formato(self):
        self.assertEqual(rdb.fmt_span(32 * 3600 + 59), "1d 8h")
        self.assertEqual(rdb.fmt_span(5 * 3600), "5h")
        self.assertEqual(rdb.fmt_span(0), "0h")

    def test_tarjeta_con_tiempo_en_guild(self):
        data = {"title": "Ana", "display": "Ana", "has_kb": False, "roles": [], "main": None, "cta_total": None,
                "guild_time": [("Smogg", 240, 32), ("Smogged", 53, 0)], "season_n": 35,
                "alters": ["AnaAlt", "AnaFarm" * 30]}
        self.assertTrue(sc.render(data).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
