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

    def test_combinar_subidas_del_chest(self):
        a = chest(C("00:50:00", "Ana", "Trash"), C("00:50:00", "Ana", "Trash"), C("00:51:00", "Bob", "Beef Stew", 2))
        b = chest(C("00:50:00", "Ana", "Trash"), C("00:52:00", "Cam", "Direwolf"))      # solapa con la primera
        m = ca.merge_chest([a, b])
        self.assertEqual(sorted((e.player, e.item) for e in m),
                         [("Ana", "Trash"), ("Ana", "Trash"), ("Bob", "Beef Stew"), ("Cam", "Direwolf")])
        self.assertEqual(m[0].player, "Cam")                                             # más nuevo primero
        self.assertEqual(ca.merge_chest([]), [])

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
                  L("00:35:30", "Eve", "T6_MOUNT_DIREWOLF", "Direwolf", src="Ana", src_guild="Smogg", by_guild="Ignum"),
                  L("00:35:40", "Ana", "T8_2H_LONGBOW@1", "Elder's Longbow"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Trash")))
        self.assertEqual(r["players"]["ana"].dropped, 0)
        self.assertEqual(r["players"]["ana"].missing_units, 2)

    def test_trash_no_cuenta(self):
        ev = loot(L("00:33:00", "Ana", "T5_TRASH", "Trash", 3), L("00:33:01", "Ana", "T8_TRASH", "Trash"))
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Elder's Longbow")))
        self.assertNotIn("ana", r["players"])

    def test_mementos_y_bolsos(self):
        ev = loot(L("00:33:00", "Ana", "UNIQUE_FURNITUREITEM_KILLTROPHY_OPENWORLD_SMALL", "Combat Memento"),
                  L("00:33:01", "Ana", "T6_BAG@1", "Master's Bag"),
                  L("00:33:02", "Ana", "T8_BAG", "Elder's Bag"),
                  L("00:33:03", "Ana", "T7_BAG_INSIGHT@2", "Grandmaster's Satchel of Insight"),
                  L("00:33:04", "Ana", "T8_BAG@2", "Elder's Bag"),                          # este sí
                  L("00:33:05", "Ana", "T8_BAG_INSIGHT@4", "Elder's Satchel of Insight"))     # y este
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Elder's Longbow")))
        ana = r["players"]["ana"]
        self.assertEqual(ana.looted, 2)
        self.assertEqual(sorted((m["item"], m["enchant"]) for m in ana.missing),
                         [("Elder's Bag", 2), ("Elder's Satchel of Insight", 4)])
        self.assertFalse(ca.ignored("T8_MOUNT_ARMORED_HORSE"))
        self.assertTrue(ca.ignored("T3_MOUNT_OX"))
        self.assertTrue(ca.ignored("T7_2H_TOOL_SIEGEHAMMER"))
        self.assertTrue(ca.ignored("T6_2H_TOOL_SIEGEHAMMER@1"))
        self.assertFalse(ca.ignored("T8_2H_TOOL_SIEGEHAMMER"))
        self.assertFalse(ca.ignored("T8_2H_TOOL_SIEGEHAMMER@2"))

    def test_monturas_y_juguetes(self):
        ev = loot(L("00:33:00", "Ana", "T5_MOUNT_COUGAR_KEEPER@1", "Swiftclaw", 2),
                  L("00:33:01", "Ana", "T6_MOUNT_DIREWOLF", "Direwolf", 1),                    # 3 T4-T6: tolerado
                  L("00:33:02", "Ana", "T3_MOUNT_HORSE", "Journeyman's Riding Horse"),         # T3: ignorado
                  L("00:33:03", "Ana", "TREASURE_SILVERWARE_RARITY2", "Silver Mirror", 2),     # juguete: ignorado
                  L("00:33:04", "Bob", "T5_MOUNT_GREYWOLF_FW_CAERLEON", "Saddled Greywolf", 2),
                  L("00:33:05", "Bob", "T6_MOUNT_DIREWOLF", "Direwolf", 2),                    # 4: se taguea
                  L("00:33:06", "Cam", "T7_MOUNT_ARMORED_HORSE", "Grandmaster's Armored Horse"))  # T7: siempre cuenta
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Elder's Longbow")))
        ana, bob, cam = r["players"]["ana"], r["players"]["bob"], r["players"]["cam"]
        self.assertEqual((ana.looted, ana.missing, ana.filtered), (3, [], True))
        self.assertEqual(bob.missing_units, 4)
        self.assertEqual(cam.missing_units, 1)
        self.assertTrue(ca.ignored("TREASURE_RITUAL_RARITY1"))

    def test_tolerancias_gigantify_y_comidas(self):
        ev = loot(L("00:33:00", "Ana", "T7_POTION_REVIVE", "Major Gigantify Potion", 25),
                  L("00:33:01", "Ana", "T7_MEAL_OMELETTE_AVALON@2", "Avalonian Pork Omelette", 2),
                  L("00:33:02", "Ana", "T8_MEAL_STEW@2", "Beef Stew", 1),                      # 3 comidas: toleradas
                  L("00:33:03", "Bob", "T7_POTION_REVIVE@1", "Major Gigantify Potion", 20),
                  L("00:33:04", "Bob", "T7_POTION_REVIVE", "Major Gigantify Potion", 6),       # 26: cuenta
                  L("00:33:05", "Bob", "T8_MEAL_STEW@2", "Beef Stew", 4),                      # 4 comidas: cuentan
                  L("00:33:06", "Cam", "T7_POTION_REVIVE", "Major Gigantify Potion", 30))
        r = ca.audit([ev], chest(C("00:55:00", "Cam", "Major Gigantify Potion", 0, 10)))     # Cam deja 20 sin depositar
        ana, bob, cam = r["players"]["ana"], r["players"]["bob"], r["players"]["cam"]
        self.assertEqual(ana.missing, [])
        self.assertTrue(ana.filtered)
        self.assertEqual(len(ana.tolerated), 3)
        self.assertEqual(sorted((m["item"], m["qty"]) for m in bob.missing), [("Beef Stew", 4), ("Major Gigantify Potion", 26)])
        self.assertEqual((cam.missing, [m["qty"] for m in cam.tolerated]), ([], [20]))
        self.assertIn("tolerado", ca.report_text(r, "CTA"))


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


class ThreadFilesTests(unittest.TestCase):
    def test_combatlogs_y_ultimo_chestlog_del_bot(self):
        import asyncio
        import test_give_deposits  # noqa: F401  (carga el stub de discord)
        import chestaudit as cog_mod

        class Att:
            _n = 0

            def __init__(self, text, name="log.txt"):
                Att._n += 1
                self.id, self.filename, self._raw = Att._n, name, text.encode("utf-8")

            async def read(self):
                return self._raw

        class Msg:
            def __init__(self, author_id, content, atts):
                self.author, self.content, self.attachments = type("U", (), {"id": author_id})(), content, atts

        class Th:
            def __init__(self, msgs):
                self.msgs = msgs

            async def history(self, limit=None, oldest_first=True):
                for m in self.msgs:
                    yield m

        loot_txt = "\n".join((HEAD, L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood")))
        old_chest = "\n".join((CHEAD, C("00:40:00", "Ana", "Trash")))
        new_chest = "\n".join((CHEAD, C("00:55:00", "Ana", "Elder's Assassin Hood")))
        fake_chest = "\n".join((CHEAD, C("01:55:00", "Ana", "Trash")))
        th = Th([Msg(1, "📎 combatlog", [Att(loot_txt)]),
                 Msg(1, "📦 Chestlog · x", [Att(old_chest)]),
                 Msg(7, "lo subo yo", [Att(fake_chest)]),                    # no es del bot: no cuenta
                 Msg(1, "📦 Chestlog · y", [Att(new_chest)])])
        bot = type("B", (), {"user": type("U", (), {"id": 1})()})()
        c = cog_mod.ChestAuditCog(bot, ":memory:", 1)

        async def fake_thread(guild, tid):
            return th if tid == 50 else None
        c._thread = fake_thread
        files, chest_rows = asyncio.run(c.thread_files(None, {"id": 3, "combatlog_priv_thread_id": 50}))
        self.assertEqual(len(files), 1)
        self.assertEqual([e.item for e in chest_rows], ["Elder's Assassin Hood", "Trash"])        # las dos subidas, sin la del no-bot
        self.assertEqual(ca.audit(files, chest_rows)["players"]["ana"].missing, [])


class TagFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        ca.init_schema(self.db)

    def tearDown(self):
        os.remove(self.db)

    def test_resolver_tags_y_borrar_general(self):
        ca.set_general(self.db, 7, 100, 555)
        t1 = ca.add_tag(self.db, 7, 1, 100, 901, "Ana", 5, "CTA X", 3)
        t2 = ca.add_tag(self.db, 7, 1, 100, 902, "Bob", None, "CTA X", 1)
        ca.set_tag_thread(self.db, t1, 77)
        tag = ca.tag_by_message(self.db, 901)
        self.assertEqual((tag["id"], tag["thread_id"], tag["discord_id"]), (t1, 77, 5))
        self.assertEqual(ca.resolve_tag(self.db, t1, 9), {"pending": 1, "general": None})
        self.assertIsNone(ca.resolve_tag(self.db, t1, 9))                              # no dos veces
        self.assertEqual(ca.resolve_tag(self.db, t2, 9), {"pending": 0, "general": (100, 555)})

    def test_texto_e_imagen_del_tag(self):
        import test_give_deposits  # noqa: F401  (carga el stub de discord)
        import chestaudit as cog
        import chestcard
        ev = loot(L("00:33:00", "Ana", "T8_HEAD_LEATHER_SET3", "Elder's Assassin Hood", 2),
                  L("00:33:01", "Bob", "T7_MEAL_STEW@2", "Beef Stew"))                  # tolerado: no se taguea
        r = ca.audit([ev], chest(C("00:55:00", "Otro", "Elder's Longbow")))
        entries = cog.to_tag(r, {"ana": 5})
        self.assertEqual([(p.name, uid) for p, uid in entries], [("Ana", 5)])
        txt = cog.tag_text(entries[0][0], 5, "CTA X")
        self.assertIn("<@5>", txt); self.assertIn("2× Elder's Assassin Hood", txt)
        self.assertIn("ataque personal", cog.GENERAL_NOTICE)
        items = [{"item": f"Item {i}", "item_id": f"T4_X{i}", "enchant": i % 4, "qty": i + 1} for i in range(35)]
        png = chestcard.render("Ana", items, "CTA X", {})
        self.assertTrue(png.startswith(b"\x89PNG"))
