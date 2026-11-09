"""Pruebas de Wujupón (gachapon), balance de embo y la carta «Cancelar cooldown de cobro».

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban la API real de Discord.
"""
import os
import random
import sys
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, FakeInteraction, run, GUILD_ID, PANEL_CH  # noqa: E402
import discord  # noqa: E402
import deposits_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import embo_db  # noqa: E402
import wujupon as wj  # noqa: E402
import wujupon_db as wdb  # noqa: E402
from dbutil import connect  # noqa: E402

COUNCIL_CH = 3000000000000000010
FEED_CH = 3000000000000000011
PRICE = wdb.DEFAULT_PRICE


class FixedRng:
    """randrange devuelve siempre el mismo valor (elige el premio en esa posición acumulada)."""
    def __init__(self, value):
        self.value = value

    def randrange(self, n):
        return min(self.value, n - 1)


def offset_of(key):
    acc = 0
    for c in wdb.CATALOG:
        if c[0] == key:
            return acc
        acc += c[3]
    raise KeyError(key)


class CatalogTests(unittest.TestCase):
    def test_probabilidades_suman_100(self):
        wdb.validate_catalog()
        self.assertEqual(sum(c[3] for c in wdb.CATALOG), wdb.TOTAL_WEIGHT)

    def test_rarezas_suman_lo_pedido(self):
        by = {}
        for c in wdb.CATALOG:
            by[c[2]] = by.get(c[2], 0) + c[3]
        self.assertEqual(by, {"common": 45000, "rare": 35000, "epic": 15200, "legendary": 4600, "mythic": 200})

    def test_monto_al_azar_entre_1_y_el_tope(self):
        rng = random.Random(7)
        vals = [wdb.random_amount(50, rng) for _ in range(500)] + [wdb.random_amount(50) for _ in range(500)]
        self.assertTrue(all(1 <= v <= 50 for v in vals))
        self.assertGreater(len(set(vals)), 30)

    def test_costo_esperado(self):
        prizes = [wdb.Prize(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], bool(c[9]), True, c[10], False, bool(c[11]))
                  for c in wdb.CATALOG]
        exp = wdb.expected_cost(prizes)
        silver = exp["wuju"] + exp["silver"] + exp["embo"] * wdb.EMBO_SILVER_VALUE
        self.assertAlmostEqual(silver + exp["val"], 249_162.54925, places=3)  # margen ≈ 50 % con la tirada a 500.000 (v13)
        self.assertLess(silver + exp["val"], wdb.DEFAULT_PRICE * 0.8)

    def test_texto_de_chance(self):
        self.assertEqual(wdb.chance_text(75), "0,075 %")
        self.assertEqual(wdb.chance_text(11000), "11 %")
        self.assertEqual(wdb.chance_text(15200), "15,2 %")


def _max_amount(test):
    """Los montos de Wuju/embo salen al azar (1..tope); en los tests que miran saldos se fija el tope."""
    patcher = mock.patch.object(wdb, "random_amount", lambda top, rng=None: top)
    patcher.start()
    test.addCleanup(patcher.stop)


class DbBase(unittest.TestCase):
    def setUp(self):
        _max_amount(self)
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        wdb.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, uid, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[uid], amount=amount, request_key=f"f{uid}{os.urandom(3).hex()}")


class WujuponDbTests(DbBase):
    def test_compra_descuenta_y_es_idempotente(self):
        self.fund(5, PRICE * 3)
        r = wdb.buy_spins(self.db, 5, 2, "k1")
        self.assertEqual((r.status, r.spins, r.balance), ("ok", 2, PRICE))
        again = wdb.buy_spins(self.db, 5, 2, "k1")
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(eco.get_balance(self.db, 5), PRICE)
        self.assertEqual(wdb.get_spins(self.db, 5), 2)

    def test_compra_sin_fondos_no_cambia_nada(self):
        self.fund(5, PRICE - 1)
        r = wdb.buy_spins(self.db, 5, 1, "k")
        self.assertEqual(r.status, "funds")
        self.assertEqual(eco.get_balance(self.db, 5), PRICE - 1)
        self.assertEqual(wdb.get_spins(self.db, 5), 0)

    def test_compras_concurrentes_no_gastan_de_mas(self):
        self.fund(5, PRICE * 3)
        results = []
        ths = [threading.Thread(target=lambda i=i: results.append(wdb.buy_spins(self.db, 5, 1, f"c{i}").status)) for i in range(6)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(results.count("ok"), 3)
        self.assertEqual(eco.get_balance(self.db, 5), 0)
        self.assertEqual(wdb.get_spins(self.db, 5), 3)

    def test_tirada_consume_una_y_crea_carta(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p1", rng=FixedRng(offset_of("caerleon_port")))
        self.assertEqual((r.status, r.prize.key, r.spins_left), ("ok", "caerleon_port", 0))
        self.assertEqual(wdb.pull(self.db, 5, "p1").status, "duplicate")
        self.assertEqual(wdb.pull(self.db, 5, "p2").status, "no_spins")
        self.assertEqual(wdb.inventory(self.db, 5)[0].available, 1)

    def test_wuju_y_embo_se_acreditan_al_salir(self):
        import embo_db
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=2, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p1", rng=FixedRng(offset_of("wuju_2m")))
        self.assertIn("2,600,000", r.credited)
        self.assertEqual(r.gained, "2,600,000 Wuju Coins")
        self.assertEqual(eco.get_balance(self.db, 5), 2_600_000)
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "consumed")
        self.assertEqual(wdb.inventory(self.db, 5), [])
        hist = eco.get_history(self.db, 5)
        self.assertIn("Wujupón: Hasta 2.600.000 Wuju Coins", hist[0].reason)
        r = wdb.pull(self.db, 5, "p2", rng=FixedRng(offset_of("embo_100")))
        self.assertEqual(embo_db.get_balance(self.db, 5), 130)
        self.assertIn("Wujupón", embo_db.history(self.db, 5)[0][4])
        self.assertEqual(wdb.get_stats(self.db).paid.get("wuju"), 2_600_000)      # cuenta en las estadísticas

    def test_distribucion_aproximada(self):
        prizes = wdb.list_prizes(self.db, True)
        rng = random.Random(1)
        n = 200_000
        counts = {}
        for _ in range(n):
            p, _ = wdb.draw(prizes, rng)
            counts[p.rarity] = counts.get(p.rarity, 0) + 1
        self.assertAlmostEqual(counts["common"] / n, 0.45, delta=0.01)
        self.assertAlmostEqual(counts["rare"] / n, 0.35, delta=0.01)
        self.assertAlmostEqual(counts["epic"] / n, 0.152, delta=0.005)

    def _card(self, key, uid=5):
        return wdb.give_card(self.db, actor_id=1, user_id=uid, prize_key=key, reason="t")

    def test_canje_automatico_wuju_una_sola_vez(self):
        cid = self._card("wuju_500k")
        r = wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r1", guild_id=GUILD_ID)
        self.assertEqual(r.status, "auto")
        self.assertEqual(eco.get_balance(self.db, 5), 650_000)
        r2 = wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r2", guild_id=GUILD_ID)
        self.assertEqual(r2.status, "not_available")
        self.assertEqual(eco.get_balance(self.db, 5), 650_000)

    def test_canje_automatico_embo(self):
        cid = self._card("embo_100")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "auto")
        self.assertEqual(embo_db.get_balance(self.db, 5), 130)

    def test_no_se_canjea_carta_ajena(self):
        cid = self._card("wuju_100k", uid=6)
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "not_yours")

    def test_encargo_reserva_y_entrega(self):
        cid = self._card("weapon_73")
        r = wdb.redeem(self.db, card_id=cid, user_id=5, details="espada", request_key="r", guild_id=GUILD_ID)
        self.assertEqual(r.status, "pending")
        self.assertEqual(wdb.get_card(self.db, cid).status, "reserved")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r2", guild_id=GUILD_ID).status,
                         "not_available")
        self.assertTrue(wdb.take_request(self.db, r.request.id, 77).ok)
        self.assertTrue(wdb.approve_request(self.db, r.request.id, 78).ok)
        ch = wdb.deliver_request(self.db, r.request.id, 77)
        self.assertTrue(ch.ok)
        self.assertEqual((ch.request.status, ch.request.delivered_by, ch.request.approved_by), ("delivered", 77, 78))
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")
        self.assertFalse(wdb.deliver_request(self.db, r.request.id, 77).ok)
        st = wdb.get_stats(self.db)
        self.assertEqual(st.paid.get("silver"), 8_000_000)

    def test_rechazo_con_y_sin_devolucion(self):
        a, b = self._card("plot_tax0"), self._card("plot_tax0")
        ra = wdb.redeem(self.db, card_id=a, user_id=5, details="", request_key="ra", guild_id=GUILD_ID)
        rb = wdb.redeem(self.db, card_id=b, user_id=5, details="", request_key="rb", guild_id=GUILD_ID)
        wdb.reject_request(self.db, ra.request.id, 77, refund=True)
        wdb.reject_request(self.db, rb.request.id, 77, refund=False)
        self.assertEqual(wdb.get_card(self.db, a).status, "available")
        self.assertEqual(wdb.get_card(self.db, b).status, "consumed")

    def test_devolver_carta(self):
        cid = self._card("wuju_100k")
        wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID)
        card, req = wdb.return_card(self.db, card_id=cid, actor_id=1, reason="bug")
        self.assertEqual(card.status, "available")
        self.assertIsNone(req)
        with self.assertRaises(wdb.WujuponError):
            wdb.return_card(self.db, card_id=cid, actor_id=1, reason="otra vez")

    def test_devolver_carta_con_encargo_abierto_lo_cierra(self):
        cid = self._card("guild_kick")
        r = wdb.redeem(self.db, card_id=cid, user_id=5, details="x", request_key="r", guild_id=GUILD_ID)
        _, req_id = wdb.return_card(self.db, card_id=cid, actor_id=1, reason="mal usada")
        self.assertEqual(req_id, r.request.id)
        self.assertEqual(wdb.get_request(self.db, req_id).status, "returned")
        self.assertFalse(wdb.deliver_request(self.db, req_id, 77).ok)

    def test_premio_deshabilitado_no_sale_ni_se_canjea(self):
        wdb.set_prize_enabled(self.db, "wuju_15m", False, 1)
        self.assertNotIn("wuju_15m", [p.key for p in wdb.list_prizes(self.db, True)])
        cid = self._card("wuju_15m")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "disabled")
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_compra_bloqueada_con_balance_congelado(self):
        self.fund(5, PRICE)
        ddb.init_schema(self.db)
        conn = connect(self.db)
        conn.execute("INSERT INTO cm_tickets(guild_id, channel_id, user_id, kind, created_at) VALUES (?,?,?,?,?)",
                     (GUILD_ID, 999, 5, "sell", datetime.now(timezone.utc).isoformat()))
        conn.close()
        self.assertEqual(wdb.buy_spins(self.db, 5, 1, "k").status, "frozen")
        self.assertEqual(eco.get_balance(self.db, 5), PRICE)


class CartasEditablesTests(DbBase):
    def test_parse_chance(self):
        self.assertEqual(wdb.parse_chance("0,5"), 500)
        self.assertEqual(wdb.parse_chance("0.075%"), 75)
        self.assertEqual(wdb.parse_chance("11"), 11000)
        for bad in ("0", "abc", "0,0001", "101"):
            with self.assertRaises(wdb.WujuponError):
                wdb.parse_chance(bad)

    def test_agregar_carta_sale_en_tiradas_y_se_canjea_por_encargo(self):
        p = wdb.add_prize(self.db, actor_id=1, name="Cena con el GM", description="Una cena", rarity="epic", weight=500)
        self.assertTrue(p.custom)
        self.assertFalse(p.automatic)
        self.assertEqual(wdb.enabled_total(self.db), wdb.TOTAL_WEIGHT + 500)
        self.assertIn(p.key, [x.key for x in wdb.list_prizes(self.db, True)])
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p", rng=FixedRng(wdb.TOTAL_WEIGHT))      # el último tramo = la carta nueva
        self.assertEqual(r.prize.key, p.key)
        red = wdb.redeem(self.db, card_id=r.card_id, user_id=5, details="", request_key="r", guild_id=GUILD_ID)
        self.assertEqual(red.status, "pending")

    def test_no_se_repite_nombre(self):
        with self.assertRaises(wdb.WujuponError):
            wdb.add_prize(self.db, actor_id=1, name="porte a caerleon", description="", rarity="rare", weight=10)

    def test_editar_nombre_y_descripcion_sobrevive_reinicio(self):
        _, after = wdb.edit_prize(self.db, "caerleon_port", actor_id=1, name="Porte VIP a Caerleon", description="Con escolta")
        self.assertEqual((after.name, after.description), ("Porte VIP a Caerleon", "Con escolta"))
        wdb._ready.discard(self.db)          # simula un reinicio del bot: vuelve a sembrar el catálogo
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "caerleon_port")
        self.assertEqual((p.name, p.description, p.weight), ("Porte VIP a Caerleon", "Con escolta", 600))
        _, cleared = wdb.edit_prize(self.db, "caerleon_port", actor_id=1, description="quitar")
        self.assertEqual(cleared.description, "")

    def test_editar_sin_datos_o_carta_inexistente(self):
        with self.assertRaises(wdb.WujuponError):
            wdb.edit_prize(self.db, "caerleon_port", actor_id=1)
        with self.assertRaises(wdb.WujuponError):
            wdb.edit_prize(self.db, "nope", actor_id=1, name="x")

    def test_migra_tabla_vieja_sin_columnas_nuevas(self):
        import tempfile
        d = tempfile.mkdtemp()
        db = os.path.join(d, "old.sqlite3")
        conn = connect(db)
        conn.execute("CREATE TABLE wjp_prizes (key TEXT PRIMARY KEY, name TEXT NOT NULL, rarity TEXT NOT NULL, weight INTEGER NOT NULL, "
                     "origin TEXT NOT NULL, handler TEXT, amount INTEGER NOT NULL DEFAULT 0, cost INTEGER NOT NULL DEFAULT 0, "
                     "cost_currency TEXT NOT NULL DEFAULT '', third_party INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
                     "sort INTEGER NOT NULL DEFAULT 0)")
        conn.close()
        wdb.init_schema(db)
        self.assertEqual(len(wdb.list_prizes(db)), len(wdb.CATALOG))
        self.assertEqual(wdb.get_prize(db, "caerleon_port").description, "Te portean a Caerleon.")


class ProbabilidadLimiteLimpiezaTests(DbBase):
    def test_cambiar_probabilidad_y_rareza_sobrevive_reinicio(self):
        before, after = wdb.set_prize_odds(self.db, "caerleon_port", actor_id=1, weight=2000, rarity="legendary")
        self.assertEqual((before.weight, before.rarity), (600, "epic"))
        self.assertEqual((after.weight, after.rarity), (2000, "legendary"))
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "caerleon_port")
        self.assertEqual((p.weight, p.rarity), (2000, "legendary"))
        self.assertEqual(wdb.enabled_total(self.db), wdb.TOTAL_WEIGHT + 1400)
        self.assertEqual(wdb.rarity_totals(self.db)["legendary"], 4600 + 2000)

    def test_cambiar_probabilidad_cambia_el_sorteo(self):
        wdb.set_prize_odds(self.db, "flex_cta", actor_id=1, weight=wdb.TOTAL_WEIGHT)
        prizes = wdb.list_prizes(self.db, True)
        rng = random.Random(3)
        hits = sum(wdb.draw(prizes, rng)[0].key == "flex_cta" for _ in range(2000))
        self.assertGreater(hits, 1000)          # ahora pesa más que todo el resto junto

    def test_odds_validaciones(self):
        with self.assertRaises(wdb.WujuponError):
            wdb.set_prize_odds(self.db, "flex_cta", actor_id=1)
        with self.assertRaises(wdb.WujuponError):
            wdb.set_prize_odds(self.db, "flex_cta", actor_id=1, rarity="ultra")
        with self.assertRaises(wdb.WujuponError):
            wdb.set_prize_odds(self.db, "nope", actor_id=1, weight=10)

    def test_limite_diario(self):
        self.fund(5, PRICE * 10)
        wdb.set_setting(self.db, "daily_limit", "3")
        self.assertEqual(wdb.buy_spins(self.db, 5, 2, "a").status, "ok")
        r = wdb.buy_spins(self.db, 5, 2, "b")
        self.assertEqual((r.status, r.remaining_today), ("limit", 1))
        self.assertEqual(eco.get_balance(self.db, 5), PRICE * 8)
        self.assertEqual(wdb.buy_spins(self.db, 5, 1, "c").status, "ok")
        self.assertEqual(wdb.buy_spins(self.db, 5, 1, "d").status, "limit")
        # las compras de ayer no cuentan
        conn = connect(self.db)
        conn.execute("UPDATE wjp_ops SET created_at = ? WHERE kind = 'buy'",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
        conn.close()
        self.assertEqual(wdb.buy_spins(self.db, 5, 3, "e").status, "ok")
        # regalos no cuentan
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=5, request_key="g", reason="t")
        self.assertEqual(wdb.bought_today(self.db, 5), 3)

    def test_precio_configurable(self):
        wdb.set_setting(self.db, "price", "1000")
        self.fund(5, 3000)
        r = wdb.buy_spins(self.db, 5, 3, "k")
        self.assertEqual((r.status, r.cost, r.balance), ("ok", 3000, 0))

    def test_limpiar_todas_las_cartas(self):
        a = wdb.give_card(self.db, actor_id=1, user_id=5, prize_key="wuju_100k", reason="t")
        b = wdb.give_card(self.db, actor_id=1, user_id=6, prize_key="ho_house", reason="t")
        c = wdb.give_card(self.db, actor_id=1, user_id=6, prize_key="wuju_100k", reason="t")
        wdb.redeem(self.db, card_id=c, user_id=6, details="", request_key="rc", guild_id=GUILD_ID)   # ya usada: no se toca
        r = wdb.redeem(self.db, card_id=b, user_id=6, details="", request_key="rb", guild_id=GUILD_ID)
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=4, request_key="g", reason="t")
        res = wdb.wipe_all_cards(self.db, actor_id=1, include_spins=False)
        self.assertEqual((res.cards, res.users, len(res.requests), res.spins), (2, 2, 1, 0))
        self.assertEqual(wdb.inventory(self.db, 5), [])
        self.assertEqual(wdb.inventory(self.db, 6), [])
        self.assertEqual(wdb.get_card(self.db, c).status, "consumed")
        self.assertEqual(wdb.get_request(self.db, r.request.id).status, "wiped")
        self.assertFalse(wdb.deliver_request(self.db, r.request.id, 7).ok)
        self.assertEqual(wdb.redeem(self.db, card_id=a, user_id=5, details="", request_key="ra", guild_id=GUILD_ID).status, "not_available")
        self.assertEqual(wdb.get_spins(self.db, 5), 4)
        res2 = wdb.wipe_all_cards(self.db, actor_id=1, include_spins=True)
        self.assertEqual((res2.cards, res2.spins), (0, 4))
        self.assertEqual(wdb.get_spins(self.db, 5), 0)


class CatalogoV2Tests(DbBase):
    def test_carta_mala_queda_reservada_para_aplicarse_sola(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p", rng=FixedRng(offset_of("bad_wuju_300k")))
        self.assertEqual(r.prize.key, "bad_wuju_300k")
        self.assertTrue(r.prize.forced and r.prize.effect)
        self.assertIsNone(r.request_id)                       # ya no va al Council: la aplica el bot
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "reserved")
        self.assertEqual(wdb.redeem(self.db, card_id=r.card_id, user_id=5, details="", request_key="x", guild_id=GUILD_ID).status,
                         "not_available")

    def test_cartas_buenas_no_crean_encargo_solas(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p", rng=FixedRng(offset_of("shield_1h")))
        self.assertIsNone(r.request_id)
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "available")

    def test_cartas_automaticas(self):
        auto = {c[0] for c in wdb.CATALOG if c[5]}
        base = {"sell_cooldown", "wuju_100k", "embo_50", "wuju_500k", "embo_100", "wuju_2m", "embo_500",
                "wuju_15m", "embo_1000", "gendarme_photo", "shop_discount_15", "shop_discount", "shop_discount_30"}
        self.assertEqual(auto, base | set(wdb.EFFECT_HANDLERS))
        for k in ("undo_coinflip", "coinflip_15m", "coinflip_20m", "coinflip_nolimit", "duel_1v1"):
            self.assertIn(k, auto)

    def test_descripciones_y_nombres_cortos(self):
        for c in wdb.CATALOG:
            self.assertTrue(c[10], c[0])
            self.assertLessEqual(len(c[1]), 40, c[0])
            self.assertLessEqual(len(c[10]), 200, c[0])
        self.assertIn("Top 10", wdb.get_prize(self.db, "duel_1v1").description)

    def test_migracion_aplica_el_catalogo_nuevo_una_vez(self):
        conn = connect(self.db)
        conn.execute("UPDATE wjp_prizes SET name = 'Viejo', weight = 1, name_edited = 1, odds_edited = 1 WHERE key = 'flex_cta'")
        conn.execute("UPDATE wjp_settings SET value = '1' WHERE key = 'catalog_version'")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "flex_cta")
        self.assertEqual((p.name, p.weight), ("Anotación libre en CTA", 700))
        # una edición posterior del Council se mantiene en los reinicios siguientes
        wdb.edit_prize(self.db, "flex_cta", actor_id=1, name="Flex", description="Mía")
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "flex_cta")
        self.assertEqual((p.name, p.description), ("Flex", "Mía"))

    def test_migracion_v3_solo_toca_las_cartas_que_cambiaron(self):
        conn = connect(self.db)
        conn.execute("UPDATE wjp_prizes SET name = 'Casa de antes', name_edited = 1 WHERE key = 'ho_house'")
        conn.execute("UPDATE wjp_prizes SET rarity = 'legendary', weight = 100, name = 'Kick viejo', name_edited = 1, odds_edited = 1 "
                     "WHERE key = 'guild_kick'")
        conn.execute("UPDATE wjp_settings SET value = '2' WHERE key = 'catalog_version'")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        self.assertEqual(wdb.get_prize(self.db, "ho_house").name, "Casa de antes")        # no cambió en v3 ni v4: se respeta
        k = wdb.get_prize(self.db, "guild_kick")
        self.assertEqual((k.name, k.rarity, k.weight), ("Kick de la guild", "mythic", 30))   # cambió en v3: toma lo nuevo

    def test_cambios_v3_del_catalogo(self):
        p = {x.key: x for x in wdb.list_prizes(self.db)}
        self.assertEqual(p["guild_kick"].rarity, "mythic")
        self.assertEqual((p["cta_free_mount"].name, p["cta_free_mount"].rarity), ("CTA con montura de combate libre", "legendary"))
        self.assertIn("la ponés vos", p["cta_free_mount"].description)
        self.assertEqual(p["mount_escort"].name, "Montura libre en un porte de la guild")
        self.assertEqual(p["plot_tax0"].rarity, "epic")
        self.assertEqual((p["alter_ho_24h"].rarity, p["alter_ho_24h"].origin), ("epic", "Staff"))
        self.assertEqual(wdb.rarity_totals(self.db), {"common": 45000, "rare": 35000, "epic": 15200, "legendary": 4600, "mythic": 200})

    def test_cambios_v4_cartas_malas_comunes(self):
        p = {x.key: x for x in wdb.list_prizes(self.db)}
        for k in ("bad_wuju_100k", "bad_embo_15", "bad_selfmute_chat_5", "bad_selfmute_voice_5"):
            self.assertEqual(p[k].rarity, "common", k)
        for k in ("bad_wuju_300k", "bad_embo_30", "bad_selfmute_chat_15", "bad_selfmute_voice_15"):
            self.assertEqual(p[k].rarity, "rare", k)
        self.assertEqual(wdb.rarity_totals(self.db), {"common": 45000, "rare": 35000, "epic": 15200, "legendary": 4600, "mythic": 200})

    def test_migracion_v4_desde_v3(self):
        conn = connect(self.db)
        conn.execute("UPDATE wjp_prizes SET rarity = 'rare', odds_edited = 1 WHERE key = 'bad_embo_15'")
        conn.execute("UPDATE wjp_prizes SET name = 'Mía', name_edited = 1 WHERE key = 'caerleon_port'")
        conn.execute("UPDATE wjp_settings SET value = '3' WHERE key = 'catalog_version'")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        self.assertEqual(wdb.get_prize(self.db, "bad_embo_15").rarity, "common")
        self.assertEqual(wdb.get_prize(self.db, "caerleon_port").name, "Mía")

    def test_cambios_v5(self):
        p = {x.key: x for x in wdb.list_prizes(self.db)}
        self.assertEqual(p["premium_30m"].weight, 20)
        self.assertEqual(p["freeze_24h"].name, "Congelar balance 72 h")
        self.assertEqual((p["mute_chat_180"].name, p["mute_voice_180"].name), ("Mute de chat 48 h", "Mute de voice 48 h"))
        self.assertEqual(p["plot_tax0"].name, "Tax 0 en plots 48 h")
        self.assertEqual((p["shop_discount"].rarity, p["shop_discount"].weight), ("epic", 300))
        self.assertEqual(wdb.rarity_totals(self.db), {"common": 45000, "rare": 35000, "epic": 15200, "legendary": 4600, "mythic": 200})

    def test_cambios_v9(self):
        p = {x.key: x for x in wdb.list_prizes(self.db)}
        self.assertEqual((p["duel_1v1"].rarity, p["duel_1v1"].weight), ("legendary", 150))
        self.assertEqual((p["weapon_73"].rarity, p["party1_slot"].rarity), ("legendary", "epic"))
        self.assertEqual((p["weapon_63"].cost, p["chest_63"].cost, p["chest_63"].rarity), (2_000_000, 2_000_000, "epic"))
        self.assertEqual([p[k].handler for k in ("mute_chat_5", "voice_mute_15", "mute_chat_30", "mute_voice_60", "mute_chat_180",
                                                 "bad_selfmute_chat_5", "bad_selfmute_voice_15")],
                         ["efx:mute_chat:15", "efx:mute_voice:45", "efx:mute_chat:120", "efx:mute_voice:360",
                          "efx:mute_chat:2880", "efx:mute_chat:15", "efx:mute_voice:45"])

    def test_cambios_v10_las_caras_con_menos_chance(self):
        p = {x.key: x.weight for x in wdb.list_prizes(self.db)}
        self.assertEqual([p[k] for k in ("wuju_15m", "weapon_83", "embo_1000", "weapon_73")], [100, 100, 100, 150])
        self.assertEqual([p[k] for k in ("mute_chat_60", "mute_voice_60", "purge_shield", "bad_cd_24h")], [450, 300, 300, 250])

    def test_migracion_v9_actualiza_rarezas(self):
        conn = connect(self.db)
        conn.execute("UPDATE wjp_prizes SET rarity = 'epic', weight = 1300, odds_edited = 1 WHERE key = 'duel_1v1'")
        conn.execute("UPDATE wjp_settings SET value = '8' WHERE key = 'catalog_version'")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "duel_1v1")
        self.assertEqual((p.rarity, p.weight), ("legendary", 150))
        self.assertEqual(wdb.rarity_totals(self.db), {"common": 45000, "rare": 35000, "epic": 15200, "legendary": 4600, "mythic": 200})

    def test_migracion_v7_cartas_malas_de_wuju_pueden_dejar_negativo(self):
        conn = connect(self.db)
        conn.execute("UPDATE wjp_prizes SET description = 'vieja', desc_edited = 1 "
                     "WHERE key IN ('bad_wuju_100k', 'bad_wuju_300k', 'bad_embo_15', 'bad_embo_30')")
        conn.execute("UPDATE wjp_settings SET value = '6' WHERE key = 'catalog_version'")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        for k in ("bad_wuju_100k", "bad_wuju_300k", "bad_embo_15", "bad_embo_30"):
            self.assertIn("negativo", wdb.get_prize(self.db, k).description, k)

    def test_embo_puede_quedar_negativo(self):
        import embo_db
        embo_db.apply(self.db, user_id=1, mode="add", amount=10, actor_id=9, request_key="e1", reason="x")
        ch = embo_db.apply(self.db, user_id=1, mode="remove", amount=30, actor_id=9, request_key="e2", reason="carta mala")
        self.assertEqual((ch.before, ch.after), (10, -20))
        self.assertEqual(embo_db.get_balance(self.db, 1), -20)
        ch = embo_db.apply(self.db, user_id=1, mode="set", amount=-5, actor_id=9, request_key="e3", reason="fijar")
        self.assertEqual(ch.after, -5)
        with self.assertRaises(embo_db.EmboError):
            embo_db.apply(self.db, user_id=1, mode="remove", amount=0, actor_id=9, request_key="e4", reason="x")

    def test_premio_viejo_que_no_esta_en_el_codigo_se_deshabilita(self):
        conn = connect(self.db)
        conn.execute("INSERT INTO wjp_prizes(key, name, rarity, weight, origin, cost_currency, enabled, sort) "
                     "VALUES ('viejo', 'Viejo', 'rare', 100, 'Staff', '', 1, 999)")
        conn.close()
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        self.assertFalse(wdb.get_prize(self.db, "viejo").enabled)
        self.assertEqual(wdb.enabled_total(self.db), wdb.TOTAL_WEIGHT)


class SellCooldownTests(DbBase):
    def _open_ticket(self, uid, ago_hours=1):
        ddb.init_schema(self.db)
        conn = connect(self.db)
        when = (datetime.now(timezone.utc) - timedelta(hours=ago_hours)).isoformat()
        conn.execute("INSERT INTO cm_tickets(guild_id, channel_id, user_id, kind, status, created_at) VALUES (?,?,?,?,?,?)",
                     (GUILD_ID, 1000 + uid, uid, "sell", "closed", when))
        conn.close()

    def test_carta_cancela_el_cooldown(self):
        self._open_ticket(5)
        self.assertGreater(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 0)
        cid = wdb.give_card(self.db, actor_id=1, user_id=5, prize_key="sell_cooldown", reason="t")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "auto")
        self.assertEqual(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 0)

    def test_sin_cooldown_la_carta_se_conserva(self):
        cid = wdb.give_card(self.db, actor_id=1, user_id=5, prize_key="sell_cooldown", reason="t")
        r = wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID)
        self.assertEqual(r.status, "no_effect")
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_un_ticket_nuevo_vuelve_a_poner_cooldown(self):
        self._open_ticket(5, ago_hours=2)
        cid = wdb.give_card(self.db, actor_id=1, user_id=5, prize_key="sell_cooldown", reason="t")
        wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID)
        conn = connect(self.db)
        conn.execute("INSERT INTO cm_tickets(guild_id, channel_id, user_id, kind, created_at) VALUES (?,?,?,?,?)",
                     (GUILD_ID, 5555, 5, "sell", (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()))
        conn.close()
        self.assertGreater(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 0)


class CooldownExtraTests(DbBase):
    def test_sumar_sin_cooldown_previo(self):
        left = ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 4)
        self.assertAlmostEqual(left, 4 * 3600, delta=5)
        self.assertGreater(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 4 * 3600 - 10)

    def test_se_suma_al_cooldown_del_ticket(self):
        ddb.init_schema(self.db)
        conn = connect(self.db)
        conn.execute("INSERT INTO cm_tickets(guild_id, channel_id, user_id, kind, status, created_at) VALUES (?,?,?,?,?,?)",
                     (GUILD_ID, 77, 5, "sell", "closed", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()))
        conn.close()
        left = ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 8)
        self.assertAlmostEqual(left, (6 + 8) * 3600, delta=10)       # le quedaban 6 h + 8 h nuevas
        left2 = ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 4)
        self.assertAlmostEqual(left2, (6 + 8 + 4) * 3600, delta=10)  # se acumula

    def test_sacar_y_carta_de_cancelar(self):
        ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 12)
        self.assertTrue(ddb.clear_sell_cooldown(self.db, GUILD_ID, 5))
        self.assertEqual(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 0)
        self.assertFalse(ddb.clear_sell_cooldown(self.db, GUILD_ID, 5))
        # la carta «Cancelar cooldown de cobro» también saca el extra
        ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 24)
        cid = wdb.give_card(self.db, actor_id=1, user_id=5, prize_key="sell_cooldown", reason="t")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "auto")
        self.assertEqual(ddb.sell_cooldown_remaining(self.db, GUILD_ID, 5), 0)

    def test_horas_invalidas(self):
        with self.assertRaises(ddb.DepositError):
            ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 0)
        with self.assertRaises(ddb.DepositError):
            ddb.add_sell_cooldown(self.db, GUILD_ID, 5, 10_000)


class EmboDbTests(DbBase):
    def test_add_remove_set(self):
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="add", amount=50, actor_id=1, request_key="a", reason="").after, 50)
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="remove", amount=20, actor_id=1, request_key="b", reason="").after, 30)
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="set", amount=7, actor_id=1, request_key="c", reason="").after, 7)
        self.assertTrue(embo_db.apply(self.db, user_id=5, mode="add", amount=50, actor_id=1, request_key="a", reason="").duplicate)
        self.assertEqual(embo_db.get_balance(self.db, 5), 7)

    def test_quitar_puede_dejar_negativo(self):
        ch = embo_db.apply(self.db, user_id=5, mode="remove", amount=1, actor_id=1, request_key="a", reason="")
        self.assertEqual((ch.before, ch.after), (0, -1))


# ============================================================
# Discord
# ============================================================
class WujuponDiscordTests(DiscordBase):
    def setUp(self):
        _max_amount(self)
        super().setUp()
        self.councilch = discord.TextChannel(COUNCIL_CH, self.guild, "council")
        self.feedch = discord.TextChannel(FEED_CH, self.guild, "wujupon")
        self.guild._channels.update({COUNCIL_CH: self.councilch, FEED_CH: self.feedch})
        self.bot.channels.update({COUNCIL_CH: self.councilch, FEED_CH: self.feedch})
        self.cog = run(wj.setup_wujupon(self.bot, self.db, GUILD_ID))
        self.cog.anim_delay = 0
        self.cog.cleanup_delay = 0          # cada prueba de borrado lo activa con un tiempo corto
        wdb.set_setting(self.db, "council_channel", str(COUNCIL_CH))
        wdb.set_setting(self.db, "feed_channel", str(FEED_CH))

    def test_compra_tirada_y_anuncio(self):
        self.fund(self.alice, PRICE)
        i = self.inter(self.alice)
        run(self.cog.do_buy(i, "1"))
        self.assertIn("Compraste", i.text)
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 1)
        i2 = self.inter(self.alice)
        run(self.cog.do_spin(i2))
        self.assertTrue(i2.response.sent[0][1].get("ephemeral"))
        self.assertEqual(len(self.feedch.sent), 1)
        desc = self.feedch.sent[0].embeds[0].description
        self.assertIn(self.alice.mention, desc)
        self.assertIn("probabilidad", desc)

    def _spy_delete(self, i):
        i.deleted = 0

        async def delete_original_response():
            i.deleted += 1
        i.delete_original_response = delete_original_response
        return i

    def test_mensajes_de_tirada_se_borran_solos(self):
        import asyncio
        self.cog.cleanup_delay = 0.3
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        i = self._spy_delete(self.inter(self.alice))
        i2 = self._spy_delete(self.inter(self.alice))

        async def go():
            await self.cog.do_spin(i)             # con tirada: carta
            await self.cog.do_spin(i2)            # sin tiradas: aviso
            self.assertEqual((i.deleted, i2.deleted), (0, 0))
            await asyncio.sleep(0.5)
        orig = wdb.draw
        wdb.draw = lambda prizes, rng=None: orig(prizes, FixedRng(offset_of("caerleon_port")))   # una carta que va al inventario
        try:
            run(go())
        finally:
            wdb.draw = orig
        self.assertEqual((i.deleted, i2.deleted), (1, 1))
        self.assertIn("se borra en", i.response.sent[0][1]["embed"].footer)
        inv = wdb.inventory(self.db, self.alice.id)[0]
        self.assertEqual(inv.available + inv.reserved, 1)                          # la carta sigue guardada

    def test_sin_borrado_si_esta_desactivado(self):
        import asyncio
        self.cog.cleanup_delay = 0
        i = self._spy_delete(self.inter(self.alice))

        async def go():
            await self.cog.do_spin(i)
            await asyncio.sleep(0.02)
        run(go())
        self.assertEqual(i.deleted, 0)

    def test_tirada_no_muestra_descripcion_salvo_carta_mala(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=2, request_key="g", reason="t")
        orig = wdb.draw
        try:
            wdb.draw = lambda prizes, rng=None: orig(prizes, FixedRng(offset_of("caerleon_port")))
            i = self.inter(self.alice)
            run(self.cog.do_spin(i))
            desc = i.response.sent[0][1]["embed"].description
            self.assertNotIn("Te portean a Caerleon", desc)
            self.assertIn("Mi inventario", desc)
            wdb.draw = lambda prizes, rng=None: orig(prizes, FixedRng(offset_of("bad_embo_15")))
            i = self.inter(self.alice)
            run(self.cog.do_spin(i))
            self.assertIn("se te descuentan 15 de embo", i.response.sent[0][1]["embed"].description)
        finally:
            wdb.draw = orig

    def test_tirar_sin_tiradas(self):
        i = self.inter(self.alice)
        run(self.cog.do_spin(i))
        self.assertIn("No tenés tiradas", i.text)

    def test_canje_staff_publica_en_council_y_se_gestiona(self):
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="caerleon_port", reason="t")
        i = self.inter(self.alice)
        run(self.cog.do_redeem(i, cid, "mañana 20 UTC"))
        self.assertIn("Canje enviado al Council", i.text)
        msg = self.councilch.sent[-1]
        self.assertIn("mañana 20 UTC", str([f for f in msg.embeds[0].fields]))
        # alguien que no es Council no puede tocar los botones
        bad = self.inter(self.bob, self.councilch, msg)
        run(self.cog.request_action(bad, "deliver"))
        self.assertIn("Solo el Council", bad.text)
        ok = self.inter(self.council, self.councilch, msg)
        run(self.cog.request_action(ok, "deliver"))
        self.assertIn("entregado", ok.text)
        self.assertIsNone(msg.view)
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")

    def test_rechazo_desde_discord_devuelve_la_carta(self):
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="ho_house", reason="t")
        run(self.cog.do_redeem(self.inter(self.alice), cid, ""))
        msg = self.councilch.sent[-1]
        req = wdb.find_request_by_message(self.db, msg.id)
        run(self.cog.do_reject(self.inter(self.council, self.councilch, msg), req.id, True, msg))
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_comandos_admin_solo_council(self):
        i = self.inter(self.bob)
        run(self.cog.gift.callback(self.cog, i, 3, self.alice, None, None))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, 3, self.alice, None, None))
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 3)

    def test_panel_premios_e_inventario(self):
        i = self.inter(self.council)
        run(self.cog.publish_panel(i))
        self.assertIsInstance(self.panelch.sent[-1].view, wj.PanelView)
        i = self.inter(self.alice)
        run(self.cog.show_prizes(i))
        embeds = [e for _, kw in i.response.sent for e in kw.get("embeds", [])]
        for _, kw in i.response.sent:
            self.assertLessEqual(sum(len(e) for e in kw["embeds"]), 6000)
        self.assertEqual({e.title.split(" — ")[0] for e in embeds if "(cont.)" not in e.title},
                         {f"{em} {n.upper()}" for n, em, _ in wdb.RARITIES.values()})   # una sección por rareza
        self.assertTrue(all(len(e.description) <= 4096 for e in embeds))
        self.assertIn("45 %", embeds[0].title)
        wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="embo_50", reason="t")
        i = self.inter(self.alice)
        run(self.cog.show_inventory(i))
        self.assertIsInstance(i.response.sent[0][1]["view"], wj.InventoryView)
        self.assertIn("×1", i.response.sent[0][1]["embed"].description)

    def test_canal_anuncios_fijar_y_quitar(self):
        i = self.inter(self.bob, self.feedch)
        run(self.cog.feed_channel.callback(self.cog, i, None, False))
        self.assertIn("Solo el Council", i.text)
        run(self.cog.feed_channel.callback(self.cog, self.inter(self.council, self.feedch), None, True))
        self.assertIsNone(wdb.get_setting(self.db, "feed_channel"))
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        run(self.cog.do_spin(self.inter(self.alice)))
        self.assertEqual(self.feedch.sent, [])
        # al reactivarlo no se publica lo que salió mientras estaba desactivado
        i = self.inter(self.council, self.feedch)
        run(self.cog.feed_channel.callback(self.cog, i, None, False))
        self.assertEqual(wdb.get_setting(self.db, "feed_channel"), str(FEED_CH))
        run(self.cog.announce_pending())
        self.assertEqual(self.feedch.sent, [])
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g2", reason="t")
        run(self.cog.do_spin(self.inter(self.alice)))
        self.assertEqual(len(self.feedch.sent), 1)

    def test_canal_encargos(self):
        other = discord.TextChannel(3000000000000000099, self.guild, "otro")
        run(self.cog.council_channel.callback(self.cog, self.inter(self.council, other), None))
        self.assertEqual(wdb.get_setting(self.db, "council_channel"), str(other.id))

    def test_comandos_carta_agregar_y_editar(self):
        rare = discord.app_commands.Choice(name="Rara", value="rare")
        i = self.inter(self.bob)
        run(self.cog.add_card.callback(self.cog, i, "X", rare, "1", None, False))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.add_card.callback(self.cog, i, "Skin gratis", rare, "1", "Una skin", False))
        self.assertIn("Carta creada", i.text)
        self.assertIn("101 %", i.text)                       # avisa que el total ya no es 100 %
        key = [p.key for p in wdb.list_prizes(self.db) if p.name == "Skin gratis"][0]
        i = self.inter(self.council)
        run(self.cog.edit_card.callback(self.cog, i, key, "Skin premium", "Elegís cualquiera"))
        self.assertIn("Skin premium", i.text)
        self.assertEqual(wdb.get_prize(self.db, key).description, "Elegís cualquiera")
        # Ver premios
        i = self.inter(self.alice)
        run(self.cog.show_prizes(i))
        # la descripción NO aparece en Ver premios…
        self.assertNotIn("Elegís cualquiera", " ".join(e.description for _, kw in i.response.sent for e in kw["embeds"]))
        # …solo al abrir la carta desde el inventario
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key=key, reason="t")
        i = self.inter(self.alice)
        run(self.cog.show_card(i, cid))
        self.assertIn("Elegís cualquiera", i.response.sent[0][1]["embed"].description)

    def test_comandos_precio_limite_y_probabilidad(self):
        run(self.cog.publish_panel(self.inter(self.council)))
        panel = self.panelch.sent[-1]
        i = self.inter(self.bob)
        run(self.cog.set_price.callback(self.cog, i, "1.000"))
        self.assertIn("Solo el Council", i.text)
        run(self.cog.set_price.callback(self.cog, self.inter(self.council), "250.000"))
        self.assertEqual(wdb.get_price(self.db), 250_000)
        self.assertIn("250,000", panel.embeds[0].description)          # el panel se actualizó
        run(self.cog.set_daily_limit.callback(self.cog, self.inter(self.council), 2))
        self.assertEqual(wdb.get_daily_limit(self.db), 2)
        self.fund(self.alice, 250_000 * 5)
        i = self.inter(self.alice)
        run(self.cog.do_buy(i, "3"))
        self.assertIn("límite", i.text)
        run(self.cog.set_daily_limit.callback(self.cog, self.inter(self.council), 0))
        self.assertEqual(wdb.get_daily_limit(self.db), 0)
        leg = discord.app_commands.Choice(name="Legendaria", value="legendary")
        i = self.inter(self.council)
        run(self.cog.odds_card.callback(self.cog, i, "flex_cta", "10", leg))
        self.assertIn("Legendaria", i.text)
        self.assertIn("109,3 %", i.text)                               # avisa que ya no suma 100 %
        p = wdb.get_prize(self.db, "flex_cta")
        self.assertEqual((p.weight, p.rarity), (10000, "legendary"))

    def test_limpiar_cartas_solo_admin_y_con_confirmacion(self):
        wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="wuju_100k", reason="t")
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="ho_house", reason="t")
        run(self.cog.do_redeem(self.inter(self.alice), cid, ""))
        req_msg = self.councilch.sent[-1]
        i = self.inter(self.council)                     # Council no alcanza: es solo admin
        run(self.cog.wipe.callback(self.cog, i, False))
        self.assertIn("Solo el dueño", i.text)
        i = self.inter(self.admin)
        run(self.cog.wipe.callback(self.cog, i, False))
        self.assertIsInstance(i.response.sent[0][1]["view"], wj.WipeConfirmView)
        self.assertEqual(len(wdb.inventory(self.db, self.alice.id)), 2)   # todavía no se borró nada
        run(self.cog.do_wipe(self.inter(self.admin), False))
        self.assertEqual(wdb.inventory(self.db, self.alice.id), [])
        self.assertIsNone(req_msg.view)                  # el encargo anulado ya no tiene botones

    def test_publicar_premios_solo_admin(self):
        i = self.inter(self.council, self.feedch)
        run(self.cog.publish_prizes.callback(self.cog, i, None))
        self.assertIn("Solo el dueño", i.text)
        self.assertEqual(self.feedch.sent, [])
        wdb.edit_prize(self.db, "caerleon_port", actor_id=1, description="Te llevan a Caerleon con escolta")
        i = self.inter(self.admin, self.panelch)
        run(self.cog.publish_prizes.callback(self.cog, i, self.feedch))
        self.assertIn("publicada", i.text)
        msgs = self.feedch.sent
        self.assertGreaterEqual(len(msgs), 1)
        self.assertIn("Premios de Wujupón", msgs[0].content)
        self.assertFalse(i.response.sent[0][1].get("ephemeral") is False)
        embeds = [e for m in msgs for e in m.embeds]
        self.assertEqual({e.title.split(" — ")[0] for e in embeds if "(cont.)" not in e.title},
                         {f"{em} {n.upper()}" for n, em, _ in wdb.RARITIES.values()})   # una sección por rareza
        self.assertTrue(all(len(e.description) <= 4096 for e in embeds))
        self.assertNotIn("Te llevan a Caerleon", " ".join(e.description for e in embeds))   # la lista pública no muestra descripciones
        for m in msgs:
            self.assertLessEqual(sum(len(e) for e in m.embeds), 6000)

    def test_pack_embeds_respeta_limites(self):
        big = [discord.Embed(title="x", description="y" * 3000) for _ in range(4)]
        groups = wj.pack_embeds(big)
        self.assertEqual([len(g) for g in groups], [1, 1, 1, 1])
        small = [discord.Embed(title=str(n)) for n in range(23)]
        self.assertEqual([len(g) for g in wj.pack_embeds(small)], [10, 10, 3])

    def test_carta_mala_se_aplica_sola(self):
        import embo_db
        import wujupon_efectos as we
        run(we.setup_efectos(self.bot, self.db, GUILD_ID))
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        orig = wdb.draw
        wdb.draw = lambda prizes, rng=None: orig(prizes, FixedRng(offset_of("bad_embo_15")))
        try:
            i = self.inter(self.alice)
            run(self.cog.do_spin(i))
        finally:
            wdb.draw = orig
        self.assertIn("Carta mala", i.response.sent[0][1]["embed"].footer)
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), -15)     # el embo puede quedar negativo
        self.assertIn("15 de embo", i.text)
        self.assertEqual(self.councilch.sent, [])

    def test_comando_cooldown_cobro(self):
        sumar = discord.app_commands.Choice(name="Sumar horas", value="sumar")
        quitar = discord.app_commands.Choice(name="Sacar cooldown", value="quitar")
        ver = discord.app_commands.Choice(name="Ver", value="ver")
        i = self.inter(self.bob)
        run(self.cog.sell_cooldown_cmd.callback(self.cog, i, self.alice, sumar, 4))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.sell_cooldown_cmd.callback(self.cog, i, self.alice, sumar, None))
        self.assertIn("Indicá cuántas horas", i.text)
        i = self.inter(self.council)
        run(self.cog.sell_cooldown_cmd.callback(self.cog, i, self.alice, sumar, 8))
        self.assertIn("8 h", i.text)
        self.assertGreater(ddb.sell_cooldown_remaining(self.db, GUILD_ID, self.alice.id), 7 * 3600)
        i = self.inter(self.council)
        run(self.cog.sell_cooldown_cmd.callback(self.cog, i, self.alice, ver, None))
        self.assertIn("le faltan", i.text)
        i = self.inter(self.council)
        run(self.cog.sell_cooldown_cmd.callback(self.cog, i, self.alice, quitar, None))
        self.assertIn("ya puede cobrar", i.text)
        self.assertEqual(ddb.sell_cooldown_remaining(self.db, GUILD_ID, self.alice.id), 0)

    class _Att:
        def __init__(self, data=b"\x89PNGfake", filename="foto.png", content_type="image/png"):
            self.data, self.filename, self.content_type, self.size = data, filename, content_type, len(data)

        async def read(self):
            return self.data

    def _nsfw(self, ch, value=True):
        ch.is_nsfw = lambda: value
        return ch

    def test_foto_premio_solo_admin_y_canal_nsfw(self):
        i = self.inter(self.council)
        run(self.cog.set_photo.callback(self.cog, i, self._Att(), False))
        self.assertIn("Solo el dueño", i.text)
        self.assertIsNone(wdb.get_asset(self.db, wdb.PHOTO_ASSET))
        i = self.inter(self.admin)
        run(self.cog.set_photo.callback(self.cog, i, self._Att(filename="x.txt", content_type="text/plain"), False))
        self.assertIn("tiene que ser una imagen", i.text)
        i = self.inter(self.admin)
        run(self.cog.set_photo.callback(self.cog, i, self._Att(), False))
        self.assertIn("Imagen cargada", i.text)
        self.assertIn("canal-nsfw", i.text)
        self.assertEqual(wdb.get_asset(self.db, wdb.PHOTO_ASSET)[1], b"\x89PNGfake")
        # el canal tiene que ser +18
        i = self.inter(self.admin, self._nsfw(self.feedch, False))
        run(self.cog.set_nsfw_channel.callback(self.cog, i, None))
        self.assertIn("no está marcado como canal +18", i.text)
        i = self.inter(self.admin, self._nsfw(self.feedch, True))
        run(self.cog.set_nsfw_channel.callback(self.cog, i, None))
        self.assertEqual(wdb.get_setting(self.db, "nsfw_channel"), str(FEED_CH))
        i = self.inter(self.admin)
        run(self.cog.set_photo.callback(self.cog, i, None, True))
        self.assertIn("Imagen borrada", i.text)
        self.assertIsNone(wdb.get_asset(self.db, wdb.PHOTO_ASSET))

    def test_canjear_foto(self):
        import asyncio
        self.cog.photo_delay = 0.01
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="gendarme_photo", reason="t")
        # sin imagen: la carta no se gasta
        i = self.inter(self.alice)
        run(self.cog.do_redeem(i, cid, ""))
        self.assertIn("no se cargó la foto", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        wdb.set_asset(self.db, wdb.PHOTO_ASSET, filename="premio.png", content=b"img", actor_id=1)
        # con imagen pero sin canal +18: vuelve al inventario
        i = self.inter(self.alice)
        run(self.cog.do_redeem(i, cid, ""))
        self.assertIn("canal +18", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        # con canal +18 configurado: se publica como spoiler y se borra sola
        self._nsfw(self.feedch, True)
        wdb.set_setting(self.db, "nsfw_channel", str(FEED_CH))
        i = self.inter(self.alice)

        async def go():
            await self.cog.do_redeem(i, cid, "")
            msg = self.feedch.sent[-1]
            self.assertFalse(msg.deleted)
            await asyncio.sleep(0.05)
            self.assertTrue(msg.deleted)
        run(go())
        self.assertIn("Tu premio está en", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")
        self.assertIn(self.alice.mention, self.feedch.sent[-1].content)

    def test_foto_en_canal_nsfw_es_privada(self):
        import asyncio
        self.cog.photo_delay = 0.01
        wdb.set_asset(self.db, wdb.PHOTO_ASSET, filename="premio.png", content=b"img", actor_id=1)
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="gendarme_photo", reason="t")
        i = self.inter(self.alice, self._nsfw(self.feedch, True))
        i.deleted = 0

        async def delete_original_response():
            i.deleted += 1
        i.delete_original_response = delete_original_response

        async def go():
            await self.cog.do_redeem(i, cid, "")
            await asyncio.sleep(0.05)
        run(go())
        content, kw = i.response.sent[0]
        self.assertTrue(kw.get("ephemeral"))
        self.assertEqual(kw["file"].filename, "SPOILER_premio.png")
        self.assertEqual(i.deleted, 1)
        self.assertEqual(self.feedch.sent, [])

    def test_pausa_solo_admin_y_bloquea_compra_tirada_y_canje(self):
        run(self.cog.publish_panel(self.inter(self.council)))
        panel = self.panelch.sent[-1]
        i = self.inter(self.council)
        run(self.cog.pause.callback(self.cog, i, True, None))
        self.assertIn("Solo el dueño", i.text)
        self.assertIsNone(wdb.get_setting(self.db, "paused"))
        i = self.inter(self.admin)
        run(self.cog.pause.callback(self.cog, i, True, "Mantenimiento"))
        self.assertIn("pausado", i.text)
        self.assertIn("PAUSADO", panel.embeds[0].title)
        self.fund(self.alice, wdb.DEFAULT_PRICE * 2)
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="wuju_100k", reason="t")
        for action in (lambda i: self.cog.do_buy(i, "1"), self.cog.do_spin, self.cog.open_buy,
                       lambda i: self.cog.start_redeem(i, cid), lambda i: self.cog.do_redeem(i, cid, "")):
            i = self.inter(self.alice)
            run(action(i))
            self.assertIn("pausado", i.text)
            self.assertIn("Mantenimiento", i.text)
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 1)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        self.assertEqual(eco.get_balance(self.db, self.alice.id), wdb.DEFAULT_PRICE * 2)
        # el inventario se puede ver igual
        i = self.inter(self.alice)
        run(self.cog.show_inventory(i))
        self.assertIn("Inventario", i.response.sent[0][1]["embed"].title)
        # y la lista de premios también
        i = self.inter(self.alice)
        run(self.cog.show_prizes(i))
        self.assertTrue(i.response.sent[0][1]["embeds"])
        self.assertIn("Los premios se pueden ver igual", str(panel.embeds[0].fields[-1]))
        i = self.inter(self.alice)
        run(self.cog.do_spin(i))                                 # el aviso de pausa trae el botón «Ver premios»
        view = i.response.sent[-1][1]["view"]
        self.assertIsInstance(view, wj.PrizesButtonView)
        i = self.inter(self.alice)
        run(view.prizes.callback(i))
        self.assertTrue(i.response.sent[0][1]["embeds"])
        # reanudar
        i = self.inter(self.admin)
        run(self.cog.pause.callback(self.cog, i, False, None))
        self.assertIn("reanudado", i.text)
        self.assertNotIn("PAUSADO", panel.embeds[0].title)
        i = self.inter(self.alice)
        run(self.cog.do_spin(i))
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 0)

    def test_estadisticas_monedas_y_premios_solo_admin(self):
        import efectos_db as edb
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=3, request_key="g", reason="t")
        for n, key in enumerate(("wuju_2m", "embo_100", "embo_50")):
            wdb.pull(self.db, self.alice.id, f"p{n}", rng=FixedRng(offset_of(key)))
        for key, action, amount in (("bad_wuju_300k", "wuju", 300_000), ("bad_embo_15", "embo", 15)):
            cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key=key, reason="t")
            edb.apply_forced(self.db, card_id=cid, user_id=self.alice.id, prize_key=key, action=action, amount=amount, minutes=None)
        wuju, embo = wdb.money_stats(self.db)
        self.assertEqual((wuju.given, wuju.removed, wuju.net), (2_600_000, 300_000, 2_300_000))
        self.assertEqual((embo.given, embo.removed, embo.net), (195, 15, 180))
        # premios con costo máximo: un arma entregada por encargo
        cid = wdb.give_card(self.db, actor_id=1, user_id=self.alice.id, prize_key="weapon_73", reason="t")
        r = wdb.redeem(self.db, card_id=cid, user_id=self.alice.id, details="", request_key="w", guild_id=GUILD_ID)
        wdb.deliver_request(self.db, r.request.id, 1)
        rows = wdb.delivered_prizes(self.db)
        self.assertEqual([(x.prize.key, x.count, x.total) for x in rows if x.prize.cost], [("weapon_73", 1, 8_000_000)])
        self.assertNotIn("wuju_2m", [x.prize.key for x in rows])
        # comandos: solo admin
        i = self.inter(self.council)
        run(self.cog.stats_money.callback(self.cog, i, None))
        self.assertIn("Solo el dueño", i.text)
        i = self.inter(self.admin)
        run(self.cog.stats_money.callback(self.cog, i, None))
        e = i.response.sent[0][1]["embed"]
        self.assertIn("2,300,000", " ".join(v for _, v, _ in e.fields))
        self.assertIn("4,100,000", " ".join(v for _, v, _ in e.fields))   # 2,3M + 180 embo × 10.000
        i = self.inter(self.admin)
        run(self.cog.stats_prizes.callback(self.cog, i, None))
        e = i.response.sent[0][1]["embed"]
        self.assertIn("Arma 7.3", e.description)
        self.assertIn("8,000,000", " ".join(v for _, v, _ in e.fields))
        # filtro por días
        self.assertEqual(wdb.money_stats(self.db, 1)[0].net, 2_300_000)

    def test_regalar_tiradas_a_un_rol(self):
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, 2, None, None, None))
        self.assertIn("persona **o** un rol", i.text)
        self.robot.roles.append(self.r_member)
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, 2, None, self.r_member, "evento"))
        self.assertIn("persona(s)", i.text)
        self.assertEqual((wdb.get_spins(self.db, self.alice.id), wdb.get_spins(self.db, self.bob.id)), (2, 2))
        self.assertEqual(wdb.get_spins(self.db, self.robot.id), 0)                 # a los bots no
        # quitar a un rol: nadie queda en negativo
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=3, request_key="x", reason="t")
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, -4, None, self.r_member, None))
        self.assertEqual((wdb.get_spins(self.db, self.alice.id), wdb.get_spins(self.db, self.bob.id)), (1, 0))

    def test_regalar_tiradas_publico_y_log(self):
        run(self.cog.canal_regalos(self.inter(self.council, self.feedch), None, False))
        self.assertEqual(wdb.get_setting(self.db, "gift_log_channel"), str(self.feedch.id))
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, 3, self.alice, None, "evento CTA"))
        self.assertFalse(i.response.sent[-1][1].get("ephemeral"))
        self.assertIn(self.council.mention, i.text)
        self.assertIn(self.alice.mention, i.text)
        self.assertIn("evento CTA", i.text)
        self.assertIn("evento CTA", self.feedch.sent[-1].content)
        run(self.cog.canal_regalos(self.inter(self.bob), None, True))         # no-Council no puede
        self.assertIsNotNone(wdb.get_setting(self.db, "gift_log_channel"))

    def test_tiradas_diarias_gratis(self):
        legacy = discord.Role(1000000000000000077, "Legacy", self.guild)
        self.guild.roles.append(legacy)
        self.alice.roles.append(legacy)
        i = self.inter(self.bob)
        run(self.cog.daily_spins.callback(self.cog, i, self.r_member, 1))
        self.assertIn("Solo el Council", i.text)
        run(self.cog.daily_spins.callback(self.cog, self.inter(self.council), self.r_member, 1))
        run(self.cog.daily_spins.callback(self.cog, self.inter(self.council), legacy, 3))
        i = self.inter(self.council)
        run(self.cog.daily_spins.callback(self.cog, i, None, None))
        self.assertIn("**3** por día", i.response.sent[0][1]["embed"].description)
        roles = [r.id for r in self.alice.roles]
        self.assertEqual(wdb.free_spins_left(self.db, self.alice.id, roles), 3)      # la mayor de sus roles, no la suma
        # se usan antes que las compradas
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="paga", reason="t")
        for n in range(3):
            self.assertEqual(wdb.pull(self.db, self.alice.id, f"f{n}", role_ids=roles).status, "ok")
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 1)                    # la comprada sigue intacta
        self.assertEqual(wdb.free_spins_left(self.db, self.alice.id, roles), 0)
        self.assertEqual(wdb.pull(self.db, self.alice.id, "f4", role_ids=roles).status, "ok")
        self.assertEqual(wdb.get_spins(self.db, self.alice.id), 0)
        self.assertEqual(wdb.pull(self.db, self.alice.id, "f5", role_ids=roles).status, "no_spins")
        # al día siguiente se renuevan (lo no usado no se acumula)
        conn = connect(self.db)
        conn.execute("UPDATE wjp_daily_used SET day = '2000-01-01'")
        conn.close()
        self.assertEqual(wdb.free_spins_left(self.db, self.alice.id, roles), 3)
        # desde Discord: el inventario las muestra y la tirada las usa
        i = self.inter(self.alice)
        run(self.cog.show_inventory(i))
        self.assertIn("🎁 Gratis de hoy", [n for n, _, _ in i.response.sent[0][1]["embed"].fields])
        i = self.inter(self.alice)
        run(self.cog.do_spin(i))
        self.assertEqual(wdb.free_spins_left(self.db, self.alice.id, roles), 2)
        # sacar el rol
        run(self.cog.daily_spins.callback(self.cog, self.inter(self.council), legacy, 0))
        self.assertEqual(wdb.free_spins_left(self.db, self.alice.id, roles), 0)       # Miembro da 1 y ya usó 1 hoy

    def test_bal_muestra_embo(self):
        embo_db.apply(self.db, user_id=self.alice.id, mode="add", amount=42, actor_id=1, request_key="x", reason="")
        emb = run(self.ecog.bal_embed(self.alice))
        self.assertIn("42 embo", " ".join(v for _, v, _ in emb.fields))

    def test_comando_embo_requiere_permiso(self):
        i = self.inter(self.bob)
        run(self.ecog._embo_op(i, "add", self.alice, "10", None))
        self.assertIn("No tenés permiso", i.text)
        i = self.inter(self.admin)
        run(self.ecog._embo_op(i, "add", self.alice, "10", None))
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 10)


if __name__ == "__main__":
    unittest.main()
