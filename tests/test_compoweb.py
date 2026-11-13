import json, os, sqlite3, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402,F401  (instala el stub de discord)
import compoweb as cw  # noqa: E402
import armas_roles as ar  # noqa: E402

REAL = "/mnt/user-data/attachments/d8681ac9-9c09-5e48-aa31-8a49e26e50a4/189ca596-attachment.txt"
CTA_SCHEMA = """
CREATE TABLE cta_compositions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, created_by INTEGER, updated_at TEXT, builds_url TEXT, legacy_first INTEGER DEFAULT 0);
CREATE TABLE cta_slots (id INTEGER PRIMARY KEY AUTOINCREMENT, composition_id INTEGER, party_no INTEGER, position_no INTEGER, role_name TEXT, emoji TEXT DEFAULT '',
  access_mode TEXT DEFAULT 'any', role_ids_json TEXT DEFAULT '[]', access_match TEXT DEFAULT 'any', UNIQUE(composition_id, party_no, position_no));
"""


def fake_html(layout, options, title="Smogg", icons=None):
    text = ('5:["$","h1",null,{"className":"public-library-detail_title__x","children":"%s"}]\n' % title
            + '6:{"layout":' + json.dumps(layout) + ',"roleOptions":' + json.dumps(options) + '}\n'
            + "".join('7:{"%s":{"src":"/item-icons/%s__t8.png","enchantment":0}}\n' % (k, v) for k, v in (icons or {}).items()))
    esc = json.dumps(text)[1:-1]
    return '<html><script>self.__next_f.push([1,"%s"])</script></html>' % esc


LAYOUT = {"parties": [{"id": "p1", "title": "Party 1", "slots": [["b-mace"], ["b-heal"], ["b-x", "b-mace"], []]}, {"id": "p2", "title": "Party 2", "slots": [["b-mace"]]}], "fills": []}
OPTIONS = [{"id": "b-mace", "name": "1H Mace", "playstyle": "dtank"}, {"id": "b-heal", "name": "Holy", "playstyle": "healer"}, {"id": "b-x", "name": "Raro", "playstyle": "support"}]
BUILDS = {"b-mace": {"build": {"draft": {"mainHand": {"itemId": "MAIN_MACE"}}}}, "b-heal": {"build": {"draft": {"mainHand": {"itemId": "MAIN_HOLYSTAFF"}}}},
          "b-x": {"build": {"draft": {"mainHand": {"itemId": "2H_SHAPESHIFTER_SET1"}}}}}      # sin regla de rol


ALL_ROLES = {"tank": 111, "healer": 222, "supp": 333, "dps": 444, "clump maker": 555, "pierce": 666, "galatinas": 777, "battle mount": 888}


class T(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        conn = sqlite3.connect(self.db); conn.executescript(CTA_SCHEMA); conn.commit(); conn.close()
        cw.init_schema(self.db); ar.init_schema(self.db)
        ar.seed_discord_roles(self.db, 1, ALL_ROLES)

    def test_parse(self):
        p = cw.parse_comp_html(fake_html(LAYOUT, OPTIONS))
        self.assertEqual(p["title"], "Smogg")
        self.assertEqual([len(x["slots"]) for x in p["parties"]], [4, 1])
        self.assertEqual(p["builds"]["b-mace"]["name"], "1H Mace")
        with self.assertRaises(cw.WebError):
            cw.parse_comp_html("<html>nada</html>")

    def test_id_del_link(self):
        self.assertEqual(cw.comp_id_from("https://www.albionzvz.com/comps/4C88EF6B-92ED-46B9-BDFA-8F68F55F5267?x=1"), "4c88ef6b-92ed-46b9-bdfa-8f68f55f5267")
        self.assertIsNone(cw.comp_id_from("hola"))

    def test_posiciones_con_roles(self):
        p = cw.parse_comp_html(fake_html(LAYOUT, OPTIONS))
        defs, notes = cw.build_slot_defs(p, BUILDS, lambda w: ar.role_for(self.db, 1, w), lambda l: ar.discord_role_for(self.db, 1, l))
        by = {(d["party_no"], d["position_no"]): d for d in defs}
        self.assertEqual(by[(1, 1)]["role_ids"], [111])                    # Mace -> tank
        self.assertEqual(by[(1, 1)]["access_mode"], "roles")
        self.assertEqual(by[(1, 2)]["role_ids"], [222])                    # bastón sagrado -> healer
        self.assertEqual(by[(1, 3)]["role_ids"], [111])                    # dos builds en una plaza: la sin regla no suma roles
        self.assertEqual(by[(1, 3)]["role_name"], "Raro / 1H Mace")
        self.assertEqual(by[(1, 4)]["access_mode"], "any")                 # plaza vacía
        self.assertIn("2H_SHAPESHIFTER_SET1", notes["sin_regla"])

    def test_icono_de_la_posicion_es_el_de_su_rol_de_permiso(self):
        import types
        p = cw.parse_comp_html(fake_html(LAYOUT, OPTIONS))
        roles = {111: types.SimpleNamespace(unicode_emoji="🛡️", name="Tank"), 222: types.SimpleNamespace(unicode_emoji=None, name="🌿 Healer")}
        defs, _ = cw.build_slot_defs(p, BUILDS, lambda w: ar.role_for(self.db, 1, w), lambda l: ar.discord_role_for(self.db, 1, l), None,
                                     lambda rid: cw.icon_of_role(roles.get(rid)))
        by = {(d["party_no"], d["position_no"]): d for d in defs}
        self.assertEqual(by[(1, 1)]["emoji"], "🛡️")                         # emoji de rol de Discord
        self.assertEqual(by[(1, 2)]["emoji"], "🌿")                         # emoji al comienzo del nombre del rol
        self.assertEqual(by[(1, 4)]["emoji"], "")                          # plaza vacía: sin rol, sin ícono

    def test_sin_icono_en_el_rol_queda_el_de_siempre(self):
        import types
        p = cw.parse_comp_html(fake_html(LAYOUT, OPTIONS))
        sin = types.SimpleNamespace(unicode_emoji=None, name="Tank")
        defs, _ = cw.build_slot_defs(p, BUILDS, lambda w: ar.role_for(self.db, 1, w), lambda l: ar.discord_role_for(self.db, 1, l), None, lambda rid: cw.icon_of_role(sin))
        self.assertEqual({(d["party_no"], d["position_no"]): d for d in defs}[(1, 1)]["emoji"], cw.LABEL_EMOJI["tank"])

    def test_icon_of_role(self):
        import types
        self.assertEqual(cw.icon_of_role(types.SimpleNamespace(unicode_emoji="💚", name="x")), "💚")
        self.assertEqual(cw.icon_of_role(types.SimpleNamespace(unicode_emoji=None, name="Tank")), "")
        self.assertEqual(cw.icon_of_role(types.SimpleNamespace(unicode_emoji=None, name="⚔️ DPS")), "⚔️")
        self.assertEqual(cw.icon_of_role(None), "")

    def test_crear_diff_y_actualizar(self):
        p = cw.parse_comp_html(fake_html(LAYOUT, OPTIONS))
        f = lambda w: ar.role_for(self.db, 1, w); g = lambda l: ar.discord_role_for(self.db, 1, l)
        defs, _ = cw.build_slot_defs(p, BUILDS, f, g)
        cid = cw.create_composition(self.db, 1, "smogg", 9, "https://x", defs, "now")
        cw.save_link(self.db, cid, "web", "https://x")
        self.assertEqual(cw.link_for(self.db, cid)["web_id"], "web")
        self.assertGreater(cw.cooldown_left(cw.link_for(self.db, cid)), 0)
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        old = [dict(r, role_ids=json.loads(r["role_ids_json"])) for r in conn.execute("SELECT * FROM cta_slots WHERE composition_id=?", (cid,))]
        self.assertEqual(len(old), 5)
        self.assertEqual(cw.diff_slots(old, defs), {"added": [], "removed": [], "changed": [], "same": 5})
        # la web cambió: ahora la plaza 1-2 es otra build y se quitó la party 2
        layout2 = {"parties": [{"id": "p1", "title": "Party 1", "slots": [["b-mace"], ["b-x"], ["b-x", "b-mace"], [], ["b-heal"]]}], "fills": []}
        defs2, _ = cw.build_slot_defs(cw.parse_comp_html(fake_html(layout2, OPTIONS)), BUILDS, f, g)
        d = cw.diff_slots(old, defs2)
        self.assertEqual((d["changed"], d["added"], d["removed"]), ([(1, 2)], [(1, 5)], [(2, 1)]))
        cw.apply_update(self.db, cid, defs2, "later")
        rows = {(r["party_no"], r["position_no"]): r for r in conn.execute("SELECT * FROM cta_slots WHERE composition_id=?", (cid,))}
        self.assertEqual(rows[(1, 2)]["role_name"], "Raro")
        self.assertIn((1, 5), rows); self.assertNotIn((2, 1), rows)
        conn.close()

    def test_arma_real_y_no_el_nombre_del_usuario(self):
        """El ROL sale del arma real (Golem=Earthrune→clump maker, Cobra=Stillgaze→tank...) pero el NOMBRE en el panel es el que escribió el autor de la compo."""
        layout = {"parties": [{"id": "p", "title": "Party 1", "slots": [["11111111-1111-4111-8111-111111111111"], ["22222222-2222-4222-8222-222222222222"], ["33333333-3333-4333-8333-333333333333"], ["44444444-4444-4444-8444-444444444444"], ["8d5f2182-aa41-451a-8565-b021beff69e5"]]}], "fills": []}
        options = [{"id": "11111111-1111-4111-8111-111111111111", "name": "Golem", "playstyle": "offtank"}, {"id": "22222222-2222-4222-8222-222222222222", "name": "Cobra", "playstyle": "dtank"},
                   {"id": "33333333-3333-4333-8333-333333333333", "name": "Damnation", "playstyle": "support"}, {"id": "44444444-4444-4444-8444-444444444444", "name": "Galas", "playstyle": "dps"}]
        icons = {"11111111-1111-4111-8111-111111111111": "2H_SHAPESHIFTER_KEEPER", "22222222-2222-4222-8222-222222222222": "2H_SHAPESHIFTER_CRYSTAL", "33333333-3333-4333-8333-333333333333": "2H_CURSEDSTAFF_MORGANA", "44444444-4444-4444-8444-444444444444": "2H_DUALSCIMITAR_UNDEAD"}
        p = cw.parse_comp_html(fake_html(layout, options, icons=icons))
        self.assertEqual(p["weapons"]["11111111-1111-4111-8111-111111111111"], "2H_SHAPESHIFTER_KEEPER")
        names = ar.weapon_names(self.db, 1)
        defs, notes = cw.build_slot_defs(p, {}, lambda w: ar.role_for(self.db, 1, w), lambda l: ar.discord_role_for(self.db, 1, l), lambda w: names.get(w))
        self.assertEqual([(d["role_name"], d["role_ids"]) for d in defs],
                         [("Golem", [555]), ("Cobra", [111]), ("Damnation", [666]), ("Galas", [777]), ("Battlemount", [888])])
        self.assertEqual(notes, {"sin_regla": [], "sin_discord": [], "sin_datos": []})

    def test_roles_por_defecto_se_actualizan_si_no_se_editaron(self):
        import dbutil
        conn = dbutil.connect(self.db)
        ar.list_rules(self.db, 1)                                                   # siembra
        conn.execute("UPDATE weapon_roles SET role='' WHERE weapon_id='2H_SHAPESHIFTER_CRYSTAL'"); conn.close()
        ar._refreshed.clear()
        self.assertEqual(ar.role_for(self.db, 1, "2H_SHAPESHIFTER_CRYSTAL"), "tank")      # volvió al default nuevo
        ar.set_role(self.db, 1, "2H_SHAPESHIFTER_CRYSTAL", "dps")                         # editado a mano: se respeta
        ar._refreshed.clear()
        self.assertEqual(ar.role_for(self.db, 1, "2H_SHAPESHIFTER_CRYSTAL"), "dps")

    def test_cache_de_builds(self):
        conn = sqlite3.connect(self.db); conn.execute("INSERT INTO compoweb_builds VALUES ('b1','{\"a\":1}',?)", (__import__("time").time(),)); conn.commit(); conn.close()
        self.assertEqual(cw.cached_build(self.db, "b1"), {"a": 1})
        self.assertEqual(cw.fetch_builds(self.db, ["b1"]), ({"b1": {"a": 1}}, []))     # sale de la caché, sin pedir al sitio

    def test_pagina_real(self):
        if not os.path.exists(REAL):
            self.skipTest("sin archivo real")
        p = cw.parse_comp_html(open(REAL, encoding="utf-8").read())
        self.assertEqual([len(x["slots"]) for x in p["parties"]], [20, 20, 20, 20])
        self.assertEqual(len(p["builds"]), 30)
        self.assertEqual(len(p["weapons"]), 30)
        self.assertEqual(p["weapons"]["6699aa46-5fd4-4025-a00b-2b61a053377f"], "2H_SHAPESHIFTER_CRYSTAL")       # Cobra
        self.assertEqual(p["builds"]["84e55694-b3d0-428d-8b95-7b02e7fdc8ce"]["name"], "1H Mace")


if __name__ == "__main__":
    unittest.main()


class VerBuildsTests(unittest.TestCase):
    PARSED = {"title": "x", "parties": [{"title": "P1", "slots": [["b1"], ["b2"], ["b1"]]}, {"title": "P2", "slots": [["b2", "8d5f2182-aa41-451a-8565-b021beff69e5"]]}],
              "weapons": {"b1": "2H_MACE", "b2": "MAIN_HOLYSTAFF"},
              "builds": {"b1": {"name": "Heavy Mace", "playstyle": "dtank"}, "b2": {"name": "Heal", "playstyle": "healer"}, "8d5f2182-aa41-451a-8565-b021beff69e5": {"name": "Fill", "playstyle": ""}}}
    BUILD = {"build": {"draft": {"name": "Heavy Mace", "playstyle": "dtank", "description": "d",
             "mainHand": {"itemId": "2H_MACE", "itemName": "Heavy Mace", "tier": "T8", "enchantment": 1,
                          "abilities": [{"slot": "Q", "abilityId": "A", "abilityName": "Sacred"}, {"slot": "passive", "abilityId": "PASSIVE_X_Y", "abilityName": "PASSIVE_X_Y"}]},
             "food": {"itemId": "MEAL_OMELETTE__T7", "itemName": "Omelette", "tier": "T7", "enchantment": 2}}}}

    def test_lista_y_uso(self):
        import compoweb as cw
        lst = cw.build_list(self.PARSED, lambda w: {"2H_MACE": "Heavy Mace"}.get(w))
        self.assertEqual([b["id"] for b in lst], ["b1", "b2"])
        self.assertEqual(lst[0]["positions"], ["P1-1", "P1-3"])
        self.assertEqual(lst[1]["positions"], ["P1-2", "P2-1"])
        self.assertEqual(lst[0]["weapon"], "Heavy Mace")

    def test_detalle(self):
        import compoweb as cw
        lines = dict(cw.build_detail_lines(self.BUILD))
        self.assertIn("T8.1 Heavy Mace", lines["⚔️ Arma"])
        self.assertIn("Q: Sacred", lines["⚔️ Arma"])
        self.assertIn("Passive: X Y", lines["⚔️ Arma"])
        self.assertEqual(cw.item_type(self.BUILD["build"]["draft"]["food"]), "T7_MEAL_OMELETTE@2")
        self.assertEqual(cw.build_summary(self.BUILD)[:2], ("Heavy Mace", "dtank"))

    def test_caption_con_descripcion(self):
        import compoweb as cw
        t = cw.build_caption("Comp X", "Heavy Mace", "Usada en: P1-1", "Tanque de frontline")
        self.assertIn("📝 Tanque de frontline", t)
        self.assertNotIn("📝", cw.build_caption("Comp X", "Heavy Mace", "Usada en: P1-1", ""))
        self.assertLessEqual(len(cw.build_caption("C", "B", "W", "x" * 5000)), 1900)

    def test_descripcion_corta_de_lista(self):
        import compoweb as cw, tempfile, os
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        cw.init_schema(db)
        self.assertEqual(cw.list_description(db, "nope"), "")

    def test_guardado(self):
        import compoweb as cw, tempfile, os
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        cw.init_schema(db)
        self.assertIsNone(cw.load_parsed(db, 1))
        cw.save_parsed(db, 1, self.PARSED)
        self.assertEqual(cw.load_parsed(db, 1)["weapons"]["b1"], "2H_MACE")


class BuildCardTests(unittest.TestCase):
    def test_render_png(self):
        import tempfile, buildcard as bc
        bc._download = lambda url: None                       # sin red: íconos de reemplazo
        bc.ICON_DIR = tempfile.mkdtemp()
        data = {"build": {"draft": {"name": "X", "playstyle": "dtank", "mainHand": {"itemId": "MAIN_MACE", "itemName": "Mace", "tier": "T8",
                "abilities": [{"slot": "Q", "abilityId": "A", "abilityName": "Q1"}]}, "food": {"itemId": "MEAL_X__T7", "tier": "T7", "itemName": "Comida", "enchantment": 2}}}}
        png = bc.render(data, "Mi Tank")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(bc.to_type(data["build"]["draft"]["food"]), "T7_MEAL_X@2")


class PlateArmorTests(unittest.TestCase):
    def test_armaduras_de_placas_por_party(self):
        def b(iid, name):
            return {"build": {"draft": {"chest": {"itemId": iid, "itemName": name, "tier": "T8"}}}}
        parsed = {"parties": [{"title": "Party 1", "slots": [["r1"], ["r2"], ["j"], ["cloth"], ["r1", "j"], [cw.FILL_BATTLEMOUNT_ID], []]},
                              {"title": "Party 2", "slots": [["cloth"]]}]}
        builds = {"r1": b("ARMOR_PLATE_ROYAL", "Royal Armor"), "r2": b("ARMOR_PLATE_ROYAL", "Royal Armor"),
                  "j": b("ARMOR_PLATE_KEEPER", "Judicator Armor"), "cloth": b("ARMOR_CLOTH_SET1", "Scholar Robe")}
        self.assertEqual(cw.plate_armors_by_party(parsed, builds),
                         [("Party 1", [("Royal Armor", 3), ("Judicator Armor", 1)]), ("Party 2", [])])
