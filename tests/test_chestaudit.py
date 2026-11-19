import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import chestaudit_db as ca  # noqa: E402
import killboard_db as kb  # noqa: E402
from dbutil import connect  # noqa: E402

HEAD = ("timestamp_utc;looted_by__alliance;looted_by__guild;looted_by__name;item_id;item_name;quantity;"
        "looted_from__alliance;looted_from__guild;looted_from__name;server__region")


def L(ts, by, item_id, name, qty=1, src="Enemy", by_guild="Smogg", src_guild="Take Care"):
    return f"2026-11-19T{ts}.000Z;TIMBA;{by_guild};{by};{item_id};{name};{qty};CURE;{src_guild};{src};West"


def loot(*rows):
    return ca.parse_loot_log("\n".join((HEAD,) + rows))


def C(ts, player, item, ench=0, amount=1, q=4):
    when = ts if "/" in ts else f"11/19/2026 {ts}"
    return f'"{when}"\t"{player}"\t"{item}"\t"{ench}"\t"{q}"\t"{amount}"'


CHEAD = '"Date"\t"Player"\t"Item"\t"Enchantment"\t"Quality"\t"Amount"'


def chest(*rows):
    return ca.parse_chest_log("\n".join((CHEAD,) + rows))


class ParseTests(unittest.TestCase):
    def test_loot_log(self):
        ev = loot(L("00:32:50", "Ana", "T5_OFF_TORCH_CRYSTAL@3", "Expert's Blueflame Torch"),
                  L("00:32:51", "Ana", "T7_POTION_REVIVE", "Major Gigantify Potion", 5))
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0].enchant, 3)
        self.assertEqual(ev[1].qty, 5)
        self.assertTrue(ca.is_loot_log(HEAD + "\n"))
        self.assertFalse(ca.is_loot_log(CHEAD))

    def test_chest_log_header_repetido(self):
        ch = chest(C("00:55:50", "Ana", "Master's Royal Armor", 3), CHEAD, C("00:55:18", "Ana", "Trash", 0, -1))
        self.assertEqual(len(ch), 2)
        self.assertEqual(ch[0].enchant, 3)
        self.assertEqual(ch[1].amount, -1)

    def test_pociones_sin_encantamiento(self):
        self.assertEqual(ca.item_key("Major Gigantify Potion", 1), ca.item_key("major gigantify potion", 0))
        self.assertNotEqual(ca.item_key("Beef Stew", 2), ca.item_key("Beef Stew", 0))


class AuditTests(unittest.TestCase):
    def test_dedupe_entre_archivos_no_dentro(self):
        a = loot(L("00:50:01", "Ana", "T6_OFF_SPIKEDSHIELD_MORGANA@2", "Master's Facebreaker"),
                 L("00:50:02", "Ana", "T6_OFF_SPIKEDSHIELD_MORGANA@2", "Master's Facebreaker"))   # dos reales
        b = loot(L("00:50:01", "Ana", "T6_OFF_SPIKEDSHIELD_MORGANA@2", "Master's Facebreaker"))   # el mismo, visto por otro
        self.assertEqual(len(ca.merge_logs([a, b])), 2)
        self.assertEqual(len(ca.merge_logs([a, a])), 2)

    def test_faltante_y_ventana_y_retiros(self):
        ev = loot(L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood", 1),
                  L("00:33:01", "Ana", "T7_POTION_REVIVE@1", "Major Gigantify Potion", 5),
                  L("00:33:02", "Bob", "T8_2H_LONGBOW@1", "Elder's Longbow", 1),
                  L("00:33:03", "Zed", "T8_2H_LONGBOW@1", "Elder's Longbow", 1, by_guild="Ignum"))   # no auditado
        ch = chest(C("00:55:00", "Ana", "Elder's Assassin Hood", 0),
                   C("00:55:01", "Ana", "Major Gigantify Potion", 0, 5),
                   C("00:55:02", "Bob", "Elder's Longbow", 1, -1),                                     # retiro: no cuenta
                   C("11/18/2026 23:00:00", "Bob", "Elder's Longbow", 1))                                          # viejo: no cuenta
        r = ca.audit([ev], ch)
        self.assertNotIn("zed", r["players"])
        self.assertEqual(r["players"]["ana"].missing, [])
        self.assertEqual(r["players"]["bob"].missing_units, 1)

    def test_deposito_antes_del_loot_no_cuenta(self):
        ev = loot(L("00:40:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood"))
        r = ca.audit([ev], chest(C("00:39:00", "Ana", "Elder's Assassin Hood"), C("00:59:00", "Otro", "Trash")))
        self.assertEqual(r["players"]["ana"].missing_units, 1)

    def test_muerte_transferencia_y_perdon(self):
        ev = loot(L("00:33:00", "Ana", "T5_OFF_TORCH_CRYSTAL@3", "Expert's Blueflame Torch"),
                  L("00:33:01", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood"),
                  L("00:33:02", "Ana", "T7_MEAL_STEW@2", "Beef Stew"),
                  # muere Ana: Bob (Smogg) saca la antorcha, un enemigo el hood, la comida no la saca nadie
                  L("00:35:00", "Bob", "T5_OFF_TORCH_CRYSTAL@3", "Expert's Blueflame Torch", src="Ana", src_guild="Smogg"),
                  L("00:35:01", "Bob", "T8_ARMOR_PLATE_SET3", "Elder's Guardian Armor", src="Ana", src_guild="Smogg"),  # equipo propio
                  L("00:35:02", "Rival", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood", src="Ana", src_guild="Smogg",
                    by_guild="Take Care"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        ana, bob = r["players"]["ana"], r["players"]["bob"]
        self.assertEqual(ana.missing, [])
        self.assertEqual((ana.looted, ana.dropped), (3, 3))
        self.assertEqual(bob.missing_units, 2)                             # la antorcha pasa a Bob + la armadura
        picked = {l.item_name: l.picked_by for l in r["losses"]}
        self.assertEqual(picked, {"Expert's Blueflame Torch": "Bob", "Elder's Assassin Hood": "Rival", "Beef Stew": None})

    def test_deposito_antes_de_morir_no_es_drop(self):
        ev = loot(L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood"),
                  L("00:49:00", "Rival", "T8_ARMOR_PLATE_SET3", "Elder's Guardian Armor", src="Ana", src_guild="Smogg",
                    by_guild="Take Care"))
        r = ca.audit([ev], chest(C("00:45:00", "Ana", "Elder's Assassin Hood"), C("00:55:00", "Otro", "Trash")))
        self.assertEqual((r["players"]["ana"].dropped, r["players"]["ana"].missing_units), (0, 0))

    def test_si_sigue_looteando_no_murio(self):
        ev = loot(L("00:35:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood"),
                  L("00:35:30", "Eve", "T5_BAG@2", "Expert's Bag", src="Ana", src_guild="Smogg", by_guild="Ignum"),
                  L("00:35:40", "Ana", "T5_BAG", "Expert's Bag"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        self.assertEqual(r["players"]["ana"].dropped, 0)
        self.assertEqual(r["players"]["ana"].missing_units, 2)

    def test_filtro_una_comida(self):
        ev = loot(L("00:33:00", "Ana", "T7_MEAL_OMELETTE_AVALON@2", "Avalonian Pork Omelette"),
                  L("00:33:01", "Bob", "T7_MEAL_OMELETTE_AVALON@2", "Avalonian Pork Omelette", 2))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        self.assertTrue(r["players"]["ana"].filtered)
        self.assertFalse(r["players"]["bob"].filtered)
        self.assertIn("Faltantes", ca.report_text(r, "CTA"))


class PersistTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        ca.init_schema(self.db); kb.init_schema(self.db)

    def tearDown(self):
        os.remove(self.db)

    def test_save_tag_y_promedios(self):
        ev = loot(L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood", 4),
                  L("00:35:00", "Rival", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood", 2, src="Ana", src_guild="Smogg",
                    by_guild="Take Care"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        ca.save_audit(self.db, 1, 10, 99, r, 1)
        aid = ca.save_audit(self.db, 1, 10, 99, r, 1)                     # se repite: vale la última
        self.assertTrue(ca.mark_tagged(self.db, aid, 99))
        self.assertFalse(ca.mark_tagged(self.db, aid, 99))
        conn = connect(self.db)
        try:
            for bid, cta in (("b1", 10), ("b2", 11), ("b3", None)):        # CTA 11: con killboard y sin auditoría → 0
                conn.execute("INSERT INTO kb_battles(battle_id, cta_id, loaded_by, loaded_at) VALUES (?, ?, 1, 'x')", (bid, cta))
                conn.execute("INSERT INTO kb_players(battle_id, name_norm, name) VALUES (?, 'ana', 'Ana')", (bid,))
            conn.execute("INSERT INTO albion_links(discord_id, char_name, registered_at, updated_at) VALUES (5, 'ANA', 'x', 'x')")
        except Exception:
            conn.execute("CREATE TABLE albion_links(discord_id INTEGER, char_name TEXT)")
            conn.execute("INSERT INTO albion_links VALUES (5, 'ANA')")
        finally:
            conn.close()
        self.assertEqual(ca.loot_averages(self.db, ["ana"]), (2.0, 2.0, 2))   # 4 looteado y 4 dropeado (muere con todo) en 2 CTAs
        self.assertEqual(ca.loot_averages(self.db, ["nadie"]), (0.0, 0.0, 0))
        self.assertEqual(ca.discord_ids_by_name(self.db, ["Ana"]), {"ana": 5})


if __name__ == "__main__":
    unittest.main()


class CogTextTests(unittest.TestCase):
    def test_tag_lines_y_chunks(self):
        import test_give_deposits  # noqa: F401  (carga el stub de discord)
        import chestaudit as cog
        ev = loot(L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood"),
                  L("00:33:01", "Bob", "T8_2H_LONGBOW@1", "Elder's Longbow"),
                  L("00:33:02", "Cam", "T7_MEAL_STEW@2", "Beef Stew"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        lines = cog.tag_lines(r, {"ana": 5})
        self.assertEqual(len(lines), 2)                                      # Cam: 1 comida, filtrado
        self.assertIn("<@5>", lines[0] + lines[1])
        self.assertTrue(any("sin Discord vinculado" in ln and "Elder's Longbow .1" in ln for ln in lines))
        msgs = cog.chunk_lines("H", ["x" * 1000] * 3)
        self.assertTrue(all(len(m) <= 1900 for m in msgs))
        self.assertEqual(cog.bs.default_of("chest_audit"), 1462185027247931660)
