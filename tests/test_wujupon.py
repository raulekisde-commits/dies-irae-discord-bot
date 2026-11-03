"""Pruebas de Wujupón (gachapon), balance de embo y la carta «Cancelar cooldown de cobro».

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban la API real de Discord.
"""
import os
import random
import sys
import threading
import unittest
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

    def test_costo_esperado(self):
        prizes = [wdb.Prize(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], bool(c[9]), True, c[10], False, bool(c[11]))
                  for c in wdb.CATALOG]
        exp = wdb.expected_cost(prizes)
        silver = exp["wuju"] + exp["silver"] + exp["embo"] * wdb.EMBO_SILVER_VALUE
        self.assertAlmostEqual(silver + exp["val"], 396_600, places=3)      # margen ≈ 20,7 % con la tirada a 500.000
        self.assertLess(silver + exp["val"], wdb.DEFAULT_PRICE * 0.8)

    def test_texto_de_chance(self):
        self.assertEqual(wdb.chance_text(75), "0,075 %")
        self.assertEqual(wdb.chance_text(11000), "11 %")
        self.assertEqual(wdb.chance_text(15200), "15,2 %")


class DbBase(unittest.TestCase):
    def setUp(self):
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
        r = wdb.pull(self.db, 5, "p1", rng=FixedRng(offset_of("wuju_2m")))
        self.assertEqual((r.status, r.prize.key, r.spins_left), ("ok", "wuju_2m", 0))
        self.assertEqual(wdb.pull(self.db, 5, "p1").status, "duplicate")
        self.assertEqual(wdb.pull(self.db, 5, "p2").status, "no_spins")
        self.assertEqual(wdb.inventory(self.db, 5)[0].available, 1)

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
        self.assertEqual(eco.get_balance(self.db, 5), 500_000)
        r2 = wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r2", guild_id=GUILD_ID)
        self.assertEqual(r2.status, "not_available")
        self.assertEqual(eco.get_balance(self.db, 5), 500_000)

    def test_canje_automatico_embo(self):
        cid = self._card("embo_100")
        self.assertEqual(wdb.redeem(self.db, card_id=cid, user_id=5, details="", request_key="r", guild_id=GUILD_ID).status, "auto")
        self.assertEqual(embo_db.get_balance(self.db, 5), 100)

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
        self.assertEqual((p.name, p.description, p.weight), ("Porte VIP a Caerleon", "Con escolta", 800))
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
        self.assertEqual((before.weight, before.rarity), (800, "epic"))
        self.assertEqual((after.weight, after.rarity), (2000, "legendary"))
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "caerleon_port")
        self.assertEqual((p.weight, p.rarity), (2000, "legendary"))
        self.assertEqual(wdb.enabled_total(self.db), wdb.TOTAL_WEIGHT + 1200)
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
    def test_carta_mala_va_directo_al_council(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p", rng=FixedRng(offset_of("bad_wuju_300k")))
        self.assertEqual(r.prize.key, "bad_wuju_300k")
        self.assertTrue(r.prize.forced)
        self.assertIsNotNone(r.request_id)
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "reserved")
        self.assertEqual(wdb.get_request(self.db, r.request_id).status, "pending")
        # no se puede «canjear» desde el inventario
        self.assertEqual(wdb.redeem(self.db, card_id=r.card_id, user_id=5, details="", request_key="x", guild_id=GUILD_ID).status,
                         "not_available")
        # aunque el Council pida devolverla, una carta mala nunca vuelve al inventario
        ch = wdb.reject_request(self.db, r.request_id, 77, refund=True, note="tenía escudo")
        self.assertFalse(ch.request.refunded)
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "consumed")
        self.assertEqual(eco.get_balance(self.db, 5), 0)          # nada automático: no se descontó solo

    def test_cartas_buenas_no_crean_encargo_solas(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=5, count=1, request_key="g", reason="t")
        r = wdb.pull(self.db, 5, "p", rng=FixedRng(offset_of("shield_1h")))
        self.assertIsNone(r.request_id)
        self.assertEqual(wdb.get_card(self.db, r.card_id).status, "available")

    def test_nada_nuevo_es_automatico(self):
        auto = {c[0] for c in wdb.CATALOG if c[5]}
        self.assertEqual(auto, {"sell_cooldown", "wuju_100k", "embo_50", "wuju_500k", "embo_100", "wuju_2m", "embo_500",
                                "wuju_15m", "embo_1000"})

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
        self.assertEqual((p.name, p.weight), ("Anotación libre en CTA", 7000))
        # una edición posterior del Council se mantiene en los reinicios siguientes
        wdb.edit_prize(self.db, "flex_cta", actor_id=1, name="Flex", description="Mía")
        wdb._ready.discard(self.db)
        wdb.init_schema(self.db)
        p = wdb.get_prize(self.db, "flex_cta")
        self.assertEqual((p.name, p.description), ("Flex", "Mía"))

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


class EmboDbTests(DbBase):
    def test_add_remove_set(self):
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="add", amount=50, actor_id=1, request_key="a", reason="").after, 50)
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="remove", amount=20, actor_id=1, request_key="b", reason="").after, 30)
        self.assertEqual(embo_db.apply(self.db, user_id=5, mode="set", amount=7, actor_id=1, request_key="c", reason="").after, 7)
        self.assertTrue(embo_db.apply(self.db, user_id=5, mode="add", amount=50, actor_id=1, request_key="a", reason="").duplicate)
        self.assertEqual(embo_db.get_balance(self.db, 5), 7)

    def test_no_queda_negativo(self):
        with self.assertRaises(embo_db.EmboError):
            embo_db.apply(self.db, user_id=5, mode="remove", amount=1, actor_id=1, request_key="a", reason="")


# ============================================================
# Discord
# ============================================================
class WujuponDiscordTests(DiscordBase):
    def setUp(self):
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
        self.cog.cleanup_delay = 0.01
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        i = self._spy_delete(self.inter(self.alice))
        i2 = self._spy_delete(self.inter(self.alice))

        async def go():
            await self.cog.do_spin(i)             # con tirada: carta
            await self.cog.do_spin(i2)            # sin tiradas: aviso
            self.assertEqual((i.deleted, i2.deleted), (0, 0))
            await asyncio.sleep(0.05)
        run(go())
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
        run(self.cog.gift.callback(self.cog, i, self.alice, 3, None))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.gift.callback(self.cog, i, self.alice, 3, None))
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
        # la descripción aparece en Ver premios
        i = self.inter(self.alice)
        run(self.cog.show_prizes(i))
        self.assertIn("Elegís cualquiera", " ".join(e.description for e in i.response.sent[0][1]["embeds"]))

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
        self.assertIn("103 %", i.text)                                 # avisa que ya no suma 100 %
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
        self.assertIn("Te llevan a Caerleon", " ".join(e.description for e in embeds))
        for m in msgs:
            self.assertLessEqual(sum(len(e) for e in m.embeds), 6000)

    def test_pack_embeds_respeta_limites(self):
        big = [discord.Embed(title="x", description="y" * 3000) for _ in range(4)]
        groups = wj.pack_embeds(big)
        self.assertEqual([len(g) for g in groups], [1, 1, 1, 1])
        small = [discord.Embed(title=str(n)) for n in range(23)]
        self.assertEqual([len(g) for g in wj.pack_embeds(small)], [10, 10, 3])

    def test_carta_mala_se_publica_en_el_council(self):
        import random as _r
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        orig = wdb.draw
        wdb.draw = lambda prizes, rng=None: orig(prizes, FixedRng(offset_of("bad_embo_15")))
        try:
            i = self.inter(self.alice)
            run(self.cog.do_spin(i))
        finally:
            wdb.draw = orig
        self.assertIn("Carta mala", i.response.sent[0][1]["embed"].footer)
        msg = self.councilch.sent[-1]
        self.assertIn("−15 de embo", str(msg.embeds[0].fields))

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
