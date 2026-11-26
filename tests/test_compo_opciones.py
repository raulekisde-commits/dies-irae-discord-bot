import os, sqlite3, tempfile, unittest
from contextlib import contextmanager
from typing import Optional  # noqa: F401  (lo usa el código de bot.py)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OpcionesDelSlot(unittest.TestCase):
    """bot.py no se puede importar (arranca el bot): se ejecutan solo las funciones de opciones del slot."""
    def setUp(self):
        src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
        self.src = src
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        c = sqlite3.connect(db)
        c.executescript("CREATE TABLE cta_compositions(id INTEGER PRIMARY KEY, updated_at TEXT);"
                        "CREATE TABLE cta_slots(id INTEGER PRIMARY KEY AUTOINCREMENT, composition_id INT, party_no INT, position_no INT, role_name TEXT);"
                        "CREATE TABLE cta_slot_options(slot_id INTEGER NOT NULL, name TEXT NOT NULL, PRIMARY KEY(slot_id,name));"
                        "INSERT INTO cta_compositions VALUES(1,'');"
                        "INSERT INTO cta_slots(composition_id,party_no,position_no,role_name) VALUES(1,3,17,'GALATINAS');")
        c.commit(); c.close()

        @contextmanager
        def db_immediate():
            conn = sqlite3.connect(db, isolation_level=None)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            finally:
                conn.close()

        def db_fetchall(q, p=()):
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            try:
                return conn.execute(q, p).fetchall()
            finally:
                conn.close()

        self.ns = {"db_immediate": db_immediate, "db_fetchall": db_fetchall, "cta_now_iso": lambda: "x", "Optional": Optional}
        exec(src[src.index("CTA_SLOT_NAME_MAX = 80"):src.index("def cta_ensure_brawl")], self.ns)

    def test_agregar_quitar_y_actualizar(self):
        ns = self.ns
        self.assertEqual(ns["cta_add_slot_option"](1, 1, "Spiked off cd"), "GALATINAS / Spiked off cd")
        self.assertEqual(ns["cta_add_slot_option"](1, 1, "spiked off cd"), "GALATINAS / Spiked off cd")      # no se repite
        comp = {"slots": [{"id": 1, "party_no": 3, "position_no": 17}]}
        defs = ns["cta_defs_with_options"](comp, [{"party_no": 3, "position_no": 17, "role_name": "GALATINAS", "role_ids": [7]}])
        self.assertEqual(defs[0]["role_name"], "GALATINAS / Spiked off cd")                                 # /compo actualizar no la pierde
        self.assertEqual(defs[0]["role_ids"], [7])                                                          # el rol pedido no cambia
        self.assertEqual(ns["cta_remove_slot_option"](1, 1, "Spiked off cd"), "GALATINAS")
        self.assertEqual(ns["cta_slot_options"](1), [])

    def test_boton_en_el_editor_y_tabla(self):
        self.assertIn('label="Opciones del slot"', self.src)
        self.assertIn("CREATE TABLE IF NOT EXISTS cta_slot_options", self.src)
        self.assertIn("defs = cta_defs_with_options(comp, defs)", self.src)


if __name__ == "__main__":
    unittest.main()
