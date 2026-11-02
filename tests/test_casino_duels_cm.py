"""Pruebas de /casino, /casinoban, /1v1 y de los paneles /panel cambiomoneda (venta y compra de Wuju Coins).

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban la API real de Discord.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, FakeInteraction, run, COUNCIL_ROLE_ID, GUILD_ID, PANEL_CH, LOG_CH  # noqa: E402
import casino as cs  # noqa: E402
import casino_db as cdb  # noqa: E402
import deposits as dp  # noqa: E402
import deposits_db as ddb  # noqa: E402
import duels as du  # noqa: E402
import duels_db as dud  # noqa: E402
import economy  # noqa: E402
import economy_db as eco  # noqa: E402
import discord  # noqa: E402
import panels as pn  # noqa: E402
import stats_db  # noqa: E402


class SeqRng:
    def __init__(self, *v):
        self.v = list(v)

    def randint(self, a, b):
        return self.v.pop(0)


# ============================================================
# Casino
# ============================================================
class CasinoDbTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_by_default_and_toggle(self):
        self.assertTrue(cdb.is_open(self.db, 1))
        self.assertTrue(cdb.set_open(self.db, 1, False, 9))
        self.assertFalse(cdb.set_open(self.db, 1, False, 9))              # ya estaba cerrado
        self.assertFalse(cdb.is_open(self.db, 1))
        self.assertTrue(cdb.is_open(self.db, 2))                          # otro servidor, independiente
        self.assertIn("cerrado", cdb.block_reason(self.db, 1, 5))

    def test_ban_toggle_and_list(self):
        self.assertTrue(cdb.toggle_ban(self.db, 1, 7, 9))
        self.assertTrue(cdb.is_banned(self.db, 1, 7))
        self.assertIn("bloqueado", cdb.block_reason(self.db, 1, 7))
        self.assertIn("<@7>", cdb.block_reason(self.db, 1, 6, 7))        # con dos personas aclara quién es
        self.assertEqual(cdb.list_banned(self.db, 1), [7])
        self.assertFalse(cdb.toggle_ban(self.db, 1, 7, 9))
        self.assertEqual(cdb.list_banned(self.db, 1), [])
        self.assertIsNone(cdb.block_reason(self.db, 1, 7))


class CasinoDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(cs.setup_casino(self.bot, self.db, GUILD_ID))

    def casino(self, user, accion):
        i = self.inter(user)
        run(self.cog.casino.callback(self.cog, i, discord.app_commands.Choice(accion, accion)))
        return i

    def ban(self, user, target):
        i = self.inter(user)
        run(self.cog.casinoban.callback(self.cog, i, target))
        return i

    def test_only_council_can_open_and_close(self):
        i = self.casino(self.alice, "cerrar")
        self.assertIn("Council", i.text)
        self.assertTrue(cdb.is_open(self.db, GUILD_ID))
        i = self.casino(self.council, "cerrar")
        self.assertIn("cerró", i.text)
        self.assertFalse(i.response.sent[0][1].get("ephemeral"))           # se anuncia a todos
        self.assertFalse(cdb.is_open(self.db, GUILD_ID))
        self.assertIn("ya estaba cerrado", self.casino(self.council, "cerrar").text)
        self.assertIn("abrió", self.casino(self.admin, "abrir").text)
        self.assertTrue(cdb.is_open(self.db, GUILD_ID))

    def limite(self, user, juego, cantidad):
        i = self.inter(user)
        run(self.cog.limite.callback(self.cog, i, discord.app_commands.Choice(juego, juego), cantidad))
        return i

    def test_limite_is_council_only_validates_and_can_be_removed(self):
        self.assertIn("Council", self.limite(self.alice, "dados", "1000").text)
        self.assertIsNone(eco.get_bet_limit(self.db, "dados"))
        self.assertIn("❌", self.limite(self.council, "dados", "abc").text)
        i = self.limite(self.council, "dados", "1.500.000")
        self.assertIn("1,500,000", i.text)
        self.assertEqual(eco.get_bet_limit(self.db, "dados"), 1_500_000)
        self.assertIsNone(eco.get_bet_limit(self.db, "coinflip"))              # cada juego tiene el suyo
        self.limite(self.admin, "1v1", "250")
        self.assertEqual(eco.get_bet_limit(self.db, "1v1"), 250)
        self.assertIn("Se quitó", self.limite(self.council, "dados", "0").text)
        self.assertIsNone(eco.get_bet_limit(self.db, "dados"))

    def test_casinoban_toggles_and_is_council_only(self):
        self.assertIn("Council", self.ban(self.alice, self.bob).text)
        self.assertFalse(cdb.is_banned(self.db, GUILD_ID, self.bob.id))
        self.assertIn("bloqueado", self.ban(self.council, self.bob).text)
        self.assertTrue(cdb.is_banned(self.db, GUILD_ID, self.bob.id))
        self.assertIn("sacado", self.ban(self.council, self.bob).text)     # repetir lo saca
        self.assertFalse(cdb.is_banned(self.db, GUILD_ID, self.bob.id))
        self.assertIn("bot", self.ban(self.council, self.robot).text)
        self.assertEqual(cs.CasinoCog.casinoban.name, "casinoban")


# ============================================================
# 1v1
# ============================================================
class DuelInter(FakeInteraction):
    """Interacción de /1v1: el mensaje que se envía queda disponible como respuesta original."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._msg = None
        resp = self.response

        async def send_message(content=None, **kw2):
            resp.done = True
            resp.sent.append((content, kw2))
            if not kw2.get("ephemeral"):
                self._msg = await self.channel.send(content, embed=kw2.get("embed"), view=kw2.get("view"))

        resp.send_message = send_message

    async def original_response(self):
        return self._msg


class DuelDbTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        stats_db.init_schema(self.db)
        for u in (1, 2):
            eco.apply_operation(self.db, kind="add", actor_id=9, user_ids=[u], amount=10000, request_key=f"f{u}")

    def tearDown(self):
        self.tmp.cleanup()

    def bal(self, u):
        return eco.get_balance(self.db, u)

    def create(self, stake=3000, now=1000.0):
        return dud.create_duel(self.db, guild_id=1, channel_id=5, challenger_id=1, challenged_id=2, stake=stake, now=now)

    def test_create_charges_challenger_and_has_no_upper_limit(self):
        r = self.create(10000)                                             # todo el balance, sin tope
        self.assertTrue(r.ok)
        self.assertEqual((self.bal(1), self.bal(2)), (0, 10000))
        self.assertEqual(eco.get_history(self.db, 1)[0].mov_type, "duel_stake")

    def test_challenger_must_afford_and_challenged_must_have_enough(self):
        self.assertEqual(self.create(10001).code, "funds")
        eco.apply_operation(self.db, kind="remove", actor_id=9, user_ids=[2], amount=9000, request_key="r2")
        r = self.create(2000)
        self.assertEqual((r.ok, r.code, r.balance), (False, "target_funds", 1000))
        self.assertEqual((self.bal(1), self.bal(2)), (10000, 1000))        # no se cobró nada
        with self.assertRaises(dud.DuelError):
            dud.create_duel(self.db, guild_id=1, channel_id=5, challenger_id=1, challenged_id=1, stake=5)

    def test_accept_resolves_and_pays_winner_everything(self):
        d = self.create(3000).duel
        r = dud.accept_duel(self.db, d.id, 2, now=1001.0, rng=SeqRng(0))   # cara = retador
        self.assertEqual((r.status, r.winner_id, r.loser_id, r.side, r.pool), ("ok", 1, 2, "cara", 6000))
        self.assertEqual((self.bal(1), self.bal(2)), (13000, 7000))
        self.assertEqual(dud.accept_duel(self.db, d.id, 2, now=1002.0, rng=SeqRng(0)).status, "closed")   # no se paga dos veces
        self.assertEqual((self.bal(1), self.bal(2)), (13000, 7000))

    def test_cruz_makes_the_challenged_win(self):
        d = self.create(3000).duel
        r = dud.accept_duel(self.db, d.id, 2, now=1001.0, rng=SeqRng(1))
        self.assertEqual((r.winner_id, r.side), (2, "cruz"))
        self.assertEqual((self.bal(1), self.bal(2)), (7000, 13000))

    def test_only_challenged_can_respond_and_funds_are_rechecked(self):
        d = self.create(3000).duel
        self.assertEqual(dud.accept_duel(self.db, d.id, 1, now=1001.0).status, "notyou")
        self.assertEqual(dud.decline_duel(self.db, d.id, 1).status, "notyou")
        eco.apply_operation(self.db, kind="remove", actor_id=9, user_ids=[2], amount=9000, request_key="spent")
        r = dud.accept_duel(self.db, d.id, 2, now=1001.0)
        self.assertEqual((r.status, r.balance), ("funds", 1000))
        self.assertEqual((self.bal(1), self.bal(2)), (7000, 1000))
        self.assertTrue(dud.get_duel(self.db, d.id).is_open)               # sigue abierto

    def test_decline_and_expiry_refund_the_challenger_once(self):
        d = self.create(3000).duel
        self.assertEqual(dud.decline_duel(self.db, d.id, 2).status, "ok")
        self.assertEqual(self.bal(1), 10000)
        self.assertEqual(dud.decline_duel(self.db, d.id, 2).status, "closed")
        self.assertEqual(self.bal(1), 10000)
        d2 = self.create(4000, now=2000.0).duel
        self.assertIsNone(dud.expire_duel(self.db, d2.id, now=2100.0))     # todavía no vence
        self.assertEqual(dud.due_duels(self.db, now=2301.0), [d2.id])
        self.assertEqual(dud.expire_duel(self.db, d2.id, now=2301.0).status, "expired")
        self.assertIsNone(dud.expire_duel(self.db, d2.id, now=2400.0))
        self.assertEqual(self.bal(1), 10000)
        self.assertEqual(dud.accept_duel(self.db, d2.id, 2, now=2302.0).status, "closed")

    def test_accept_after_deadline_is_refused(self):
        d = self.create(3000, now=1000.0).duel
        self.assertEqual(dud.accept_duel(self.db, d.id, 2, now=1301.0).status, "closed")

    def test_money_is_conserved_and_stats_count_finished_duels_only(self):
        d = self.create(3000).duel
        self.assertEqual(stats_db.get_user_stats(self.db, 1).wagered, 0)   # abierto: no cuenta
        dud.accept_duel(self.db, d.id, 2, now=1001.0, rng=SeqRng(0))
        self.assertEqual(self.bal(1) + self.bal(2), 20000)
        s1, s2 = stats_db.get_user_stats(self.db, 1), stats_db.get_user_stats(self.db, 2)
        self.assertEqual((s1.wagered, s1.won, s1.lost), (3000, 3000, 0))
        self.assertEqual((s2.wagered, s2.won, s2.lost), (3000, 0, 3000))
        d2 = self.create(1000, now=5000.0).duel
        dud.decline_duel(self.db, d2.id, 2)                                # devuelto: no cuenta
        self.assertEqual(stats_db.get_user_stats(self.db, 1).wagered, 3000)

    def test_concurrent_accepts_pay_once(self):
        import threading
        d = self.create(3000).duel
        out = []
        ts = [threading.Thread(target=lambda: out.append(dud.accept_duel(self.db, d.id, 2, now=1001.0, rng=SeqRng(0)).status)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(self.bal(1) + self.bal(2), 20000)


class DuelDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(du.setup_duels(self.bot, self.db, GUILD_ID))
        run(cs.setup_casino(self.bot, self.db, GUILD_ID))
        eco.set_command_channel(self.db, "1v1", PANEL_CH)                 # /1v1 se habilita con /eco perms-canal como dados y coinflip
        self.fund(self.alice, 10000)
        self.fund(self.bob, 6000)

    def challenge(self, a, b, amount):
        i = DuelInter(self.guild, a, self.panelch)
        run(self.cog.duel.callback(self.cog, i, b, amount))
        return i

    def press(self, user, kind, msg):
        i = FakeInteraction(self.guild, user, self.panelch, msg)
        run(getattr(self.cog, f"on_{kind}")(i))
        return i

    def test_1v1_needs_perms_canal_and_only_works_in_that_channel(self):
        eco.clear_command_channel(self.db, "1v1")
        i = self.challenge(self.alice, self.bob, "100")
        self.assertIn("/eco perms-canal", i.text)                              # sin canal configurado está deshabilitado
        self.assertEqual(self.panelch.sent, [])
        other = discord.TextChannel(PANEL_CH + 1, self.guild, "otro")
        self.guild._channels[other.id] = other
        eco.set_command_channel(self.db, "1v1", other.id)
        i = self.challenge(self.alice, self.bob, "100")                    # canal equivocado
        self.assertIn(f"<#{other.id}>", i.text)
        self.assertEqual(self.bal(self.alice), 10000)
        eco.set_command_channel(self.db, "1v1", PANEL_CH)
        self.challenge(self.alice, self.bob, "100")
        self.assertEqual(self.bal(self.alice), 9900)
        # y se puede deshabilitar repitiendo /eco perms-canal en el mismo canal
        cmd = economy.EconomyCog.perms_canal
        inter = self.inter(self.admin, self.panelch)
        run(cmd.callback(self.ecog, inter, discord.app_commands.Choice("1v1", "1v1")))
        self.assertIsNone(eco.get_command_channel(self.db, "1v1"))
        self.assertIn("deshabilitado", inter.text)

    def test_limit_caps_1v1_bets(self):
        eco.set_bet_limit(self.db, "1v1", 1_000)
        i = self.challenge(self.alice, self.bob, "1.001")
        self.assertIn("máxima en 1v1 es 1,000", i.text)
        self.assertEqual(self.bal(self.alice), 10000)
        self.assertEqual(self.panelch.sent, [])
        self.challenge(self.alice, self.bob, "1.000")
        self.assertEqual(self.bal(self.alice), 9000)

    def test_registered_as_1v1(self):
        self.assertEqual(du.DuelsCog.duel.name, "1v1")
        self.assertIn("DuelView", {type(v).__name__ for v in self.bot.persistent_views})

    def test_challenge_publishes_panel_pinging_the_challenged_with_two_buttons(self):
        i = self.challenge(self.alice, self.bob, "4.000")
        msg = self.panelch.sent[-1]
        self.assertIn(str(self.bob.id), msg.content)
        self.assertEqual([c.label for c in msg.view.children], ["Aceptar", "Rechazar"])
        self.assertIn("4,000", msg.embeds[0].description)
        self.assertEqual(self.bal(self.alice), 6000)                       # la apuesta queda retenida

    def test_no_limit_other_than_balance_and_target_must_afford(self):
        self.fund(self.bob, 10 ** 8)
        self.fund(self.alice, 10 ** 8)
        self.challenge(self.alice, self.bob, "50.000.000")                 # muy por encima de los límites de dados/coinflip
        self.assertIsNotNone(dud.find_by_message(self.db, self.panelch.sent[-1].id))
        before = len(self.panelch.sent)
        i = self.challenge(self.alice, self.bob, "999.000.000")
        self.assertIn("saldo suficiente", i.text)
        self.assertEqual(len(self.panelch.sent), before)

    def test_target_without_enough_balance_cannot_be_challenged(self):
        i = self.challenge(self.alice, self.bob, "7.000")                   # bob tiene 6.000
        self.assertIn("no tiene saldo suficiente", i.text)
        self.assertEqual(self.panelch.sent, [])
        self.assertEqual(self.bal(self.alice), 10000)

    def test_invalid_challenges(self):
        for target, amount, word in ((self.alice, "10", "vos mismo"), (self.robot, "10", "bot"), (self.bob, "abc", "❌"), (self.bob, "0", "❌")):
            self.assertIn(word, self.challenge(self.alice, target, amount).text)
        self.assertEqual(self.bal(self.alice), 10000)
        self.assertEqual(self.panelch.sent, [])
        self.assertIn("permiso", self.challenge(self.outsider, self.bob, "5").text)

    def test_accept_only_by_challenged_then_result_is_announced(self):
        self.challenge(self.alice, self.bob, "4.000")
        msg = self.panelch.sent[-1]
        self.assertIn("no es para vos", self.press(self.alice, "accept", msg).text)       # el retador no puede aceptar
        self.assertIn("no es para vos", self.press(self.council, "accept", msg).text)
        i = self.press(self.bob, "accept", msg)
        self.assertEqual(i.response.edits[0]["view"], None)                # el panel queda sin botones
        result = self.panelch.sent[-1].embeds[0]
        self.assertIn("resultado", result.title)
        self.assertIn("8,000", result.description)
        self.assertEqual(self.bal(self.alice) + self.bal(self.bob), 16000)
        self.assertIn("terminó", self.press(self.bob, "accept", msg).text)

    def test_decline_refunds_and_announces(self):
        self.challenge(self.alice, self.bob, "4.000")
        msg = self.panelch.sent[-1]
        self.assertIn("no es para vos", self.press(self.alice, "decline", msg).text)
        self.press(self.bob, "decline", msg)
        self.assertEqual((self.bal(self.alice), self.bal(self.bob)), (10000, 6000))
        self.assertIn(f"<@{self.alice.id}>", self.panelch.sent[-1].content)         # cargada: etiqueta a las dos personas
        self.assertIn(f"<@{self.bob.id}>", self.panelch.sent[-1].content)
        self.assertIn("Se devolvió la apuesta", self.panelch.sent[-1].content)

    def test_accept_fails_if_the_challenged_spent_the_money(self):
        self.challenge(self.alice, self.bob, "4.000")
        msg = self.panelch.sent[-1]
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.bob.id], amount=5000, request_key="spent")
        self.assertIn("saldo suficiente", self.press(self.bob, "accept", msg).text)
        self.assertEqual(self.bal(self.alice), 6000)
        self.assertTrue(dud.find_by_message(self.db, msg.id).is_open)

    def test_expired_duel_refunds_and_closes_panel(self):
        self.challenge(self.alice, self.bob, "4.000")
        msg = self.panelch.sent[-1]
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE duels SET ends_at = 1")
        conn.commit()
        conn.close()
        self.assertEqual(run(self.cog.process_due()), 1)
        self.assertEqual(self.bal(self.alice), 10000)
        self.assertIn("Se devolvió la apuesta", self.panelch.sent[-1].content)
        self.assertEqual(run(self.cog.process_due()), 0)

    def test_closed_casino_and_bans_block_duels(self):
        cdb.set_open(self.db, GUILD_ID, False, 1)
        self.assertIn("cerrado", self.challenge(self.alice, self.bob, "100").text)
        cdb.set_open(self.db, GUILD_ID, True, 1)
        cdb.toggle_ban(self.db, GUILD_ID, self.bob.id, 1)
        self.assertIn("bloqueado", self.challenge(self.alice, self.bob, "100").text)   # el retado está bloqueado
        cdb.toggle_ban(self.db, GUILD_ID, self.bob.id, 1)
        cdb.toggle_ban(self.db, GUILD_ID, self.alice.id, 1)
        self.assertIn("bloqueado", self.challenge(self.alice, self.bob, "100").text)   # el retador está bloqueado
        self.assertEqual(self.bal(self.alice), 10000)
        self.assertEqual(self.panelch.sent, [])


# ============================================================
# /panel cambiomoneda
# ============================================================
def cdb_sell_wait(db, user_id):
    return ddb.sell_cooldown_remaining(db, GUILD_ID, user_id)


class CambioMonedaTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(dp.setup_deposits(self.bot, self.db, GUILD_ID))
        self.cog.close_delay = 0
        run(self.ecog._db(eco.set_setting, "log_channel_id", str(LOG_CH)))

    def publish(self, user=None, channel=None):
        i = self.inter(user or self.council)
        run(self.cog.publish_panels(i, channel))
        return i

    def last_channel(self):
        return self.guild.created_channels[-1]

    def open_sell(self, user=None, confirm=True):
        """Toca «Cobrar balance». Si pide confirmación, la acepta (confirm=False = se queda en la pregunta)."""
        user = user or self.alice
        i = self.inter(user)
        run(self.cog.on_sell_open(i))
        view = i.response.sent[-1][1].get("view") if i.response.sent else None
        if confirm and view is not None:
            j = self.inter(user)
            run(view.children[0].callback(j))
            return j
        return i

    def open_buy(self, user=None, amount="1.500.000"):
        user = user or self.alice
        i = self.inter(user)
        run(self.cog.on_buy_open(i))
        if not i.modals:
            return i
        j = self.inter(user)
        run(self.cog.on_buy_submitted(j, amount))
        return j

    def press(self, name, user, ch, message=None):
        i = self.inter(user, ch, message or ch.sent[0])
        run(getattr(self.cog, name)(i))
        return i

    # ---- /panel group ----
    def test_panel_group_has_both_subcommands(self):
        self.assertEqual(pn.PanelsCog.panel.name, "panel")
        self.assertEqual(set(pn.PanelsCog.panel.commands), {"apuestas", "cambiomoneda", "reclutamiento", "foco", "shop", "coaching"})

    def test_panel_command_delegates_and_reports_missing_modules(self):
        cog = run(pn.setup_panels(self.bot, GUILD_ID))
        i = self.inter(self.council)
        run(cog.cambiomoneda.callback(cog, i, None))
        self.assertEqual(len(self.panelch.sent), 2)
        j = self.inter(self.council)
        run(cog.apuestas.callback(cog, j))
        self.assertIn("no está cargado", j.text)                           # no se cargó RankingsCog en este test

    # ---- paneles ----
    def test_only_council_publishes_two_panels_with_one_button_each(self):
        self.assertIn("Council", self.publish(self.alice).text)
        self.assertEqual(self.panelch.sent, [])
        self.publish(self.council)
        titles = [m.embeds[0].title for m in self.panelch.sent]
        self.assertEqual(titles, ["💰 Cobrar balance", "🪙 Comprar Wuju Coins"])
        self.assertEqual([m.view.children[0].custom_id for m in self.panelch.sent], ["cm:sell_open", "cm:buy_open"])
        buy_text = self.panelch.sent[1].embeds[0].description
        self.assertIn("Depositá el silver", buy_text)
        self.assertIn("foto", buy_text)

    def test_republishing_replaces_old_panels(self):
        self.publish(self.council)
        first = list(self.panelch.sent)
        self.publish(self.council)
        self.assertTrue(all(m.deleted for m in first))
        self.assertEqual(len([m for m in self.panelch.sent if not m.deleted]), 2)

    # ---- venta ----
    def test_sell_asks_for_confirmation_before_opening_the_ticket(self):
        self.fund(self.alice, 5000)
        i = self.open_sell(confirm=False)
        self.assertIn("¿Seguro que querés retirar?", i.text)
        self.assertIn("seguir apostando", i.text)
        self.assertIn("duplicás", i.text)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        self.assertFalse(hasattr(self.guild, "created_channels"))               # todavía no se creó nada
        view = i.response.sent[0][1]["view"]
        self.assertEqual([c.label for c in view.children], ["Sí, quiero retirar", "No, sigo apostando"])
        # otra persona no puede pulsar los botones
        self.assertFalse(run(view.interaction_check(self.inter(self.bob))))
        # «No, sigo apostando» no abre nada ni activa el cooldown
        j = self.inter(self.alice)
        run(view.children[1].callback(j))
        self.assertFalse(hasattr(self.guild, "created_channels"))
        self.assertEqual(cdb_sell_wait(self.db, self.alice.id), 0)
        # «Sí» abre el ticket una sola vez aunque se toque dos veces
        i = self.open_sell(confirm=False)
        view = i.response.sent[0][1]["view"]
        j = self.inter(self.alice)
        run(view.children[0].callback(j))
        self.assertIn("Ticket abierto", j.text)
        run(view.children[0].callback(self.inter(self.alice)))
        self.assertEqual(len(self.guild.created_channels), 1)

    def test_sell_cooldown_of_8_hours_after_opening_a_ticket(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        # cerrar el ticket no libera el cooldown
        conn = ddb.connect(self.db)
        conn.execute("UPDATE cm_tickets SET status='closed' WHERE channel_id=?", (ch.id,))
        conn.commit()
        conn.close()
        i = self.open_sell()
        self.assertIn("Ya abriste un ticket de cobro", i.text)
        self.assertRegex(i.text, r"\d h \d+ min")
        self.assertEqual(len(self.guild.created_channels), 1)
        # otra persona no se ve afectada
        self.fund(self.bob, 100)
        self.assertIn("Ticket abierto", self.open_sell(self.bob).text)
        # pasadas 8 horas puede volver a abrir
        conn = ddb.connect(self.db)
        conn.execute("UPDATE cm_tickets SET created_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (self.alice.id,))
        conn.commit()
        conn.close()
        self.assertIn("¿Seguro", self.open_sell(confirm=False).text)
        self.assertIn("Ticket abierto", self.open_sell().text)

    def test_sell_cooldown_is_checked_again_when_confirming(self):
        self.fund(self.alice, 5000)
        i = self.open_sell(confirm=False)
        view = i.response.sent[0][1]["view"]
        ddb.create_ticket(self.db, GUILD_ID, 123456, self.alice.id, "sell")          # mientras tanto abrió otro ticket
        conn = ddb.connect(self.db)
        conn.execute("UPDATE cm_tickets SET status='closed'")
        conn.commit()
        conn.close()
        j = self.inter(self.alice)
        run(view.children[0].callback(j))
        self.assertIn("Ya abriste un ticket de cobro", j.text)
        self.assertFalse(hasattr(self.guild, "created_channels"))

    def test_sell_ticket_pings_council_shows_bal_and_has_council_buttons(self):
        self.fund(self.alice, 5000)
        self.assertIn("Ticket abierto", self.open_sell().text)
        ch = self.last_channel()
        msg = ch.sent[0]
        self.assertIn(f"<@&{COUNCIL_ROLE_ID}>", msg.content)
        self.assertEqual([c.label for c in msg.view.children], ["Aceptar", "Cerrar"])
        titles = [e.title for e in msg.embeds]
        self.assertIn("Wuju Coins", titles)                                 # el mismo panel que !bal
        bal = next(e for e in msg.embeds if e.title == "Wuju Coins")
        self.assertIn("5,000", bal.description)
        self.assertIn("Balance histórico", " ".join(n for n, _, _ in bal.fields))
        self.assertEqual(ch.overwrites[self.r_council].perms["view_channel"], True)
        self.assertEqual(ch.overwrites[self.guild.default_role].perms["view_channel"], False)

    def test_open_sell_ticket_follows_balance_changes(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        self.bot.channels[ch.id] = ch
        msg = ch.sent[0]
        self.assertEqual(run(self.cog.refresh_sell_tickets()), 0)                    # nada cambió: no se edita
        # la persona juega en el casino y pierde 2.000 mientras el ticket está abierto
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.alice.id], amount=2000, request_key="casino-loss", reason="x")
        self.assertEqual(run(self.cog.refresh_sell_tickets()), 1)
        bal = next(e for e in msg.embeds if e.title == "Wuju Coins")
        self.assertIn("3,000", bal.description)                                      # el precio del ticket quedó al día
        self.assertEqual([c.label for c in msg.view.children], ["Aceptar", "Cerrar"])   # y siguen los botones
        self.assertEqual(run(self.cog.refresh_sell_tickets()), 0)
        # el Council ve el balance actual al aceptar
        i = self.inter(self.council, ch, message=msg)
        run(self.cog.on_sell_accept(i))
        self.assertEqual(i.modals[0].amount.value, "3,000")

    def test_ticket_refresh_skips_processed_and_closed_tickets_and_survives_deleted_messages(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        self.bot.channels[ch.id] = ch
        ticket = ddb.get_by_channel(self.db, ch.id)
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.alice.id], amount=1000, request_key="k1", reason="x")
        ch.sent[0].deleted = True                                                      # borraron el mensaje
        self.assertEqual(run(self.cog.refresh_sell_tickets()), 0)                      # no explota ni insiste
        self.assertEqual(run(self.cog.refresh_sell_tickets()), 0)
        ddb.mark_processed(self.db, ticket.id, 100, 1)
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.alice.id], amount=1000, request_key="k2", reason="x")
        self.assertEqual(ddb.live_sell_tickets(self.db), [])                           # ya retirado: no se toca más

    def test_sell_requires_balance_and_one_ticket_per_person(self):
        self.assertIn("No tenés Wuju Coins", self.open_sell().text)
        self.assertEqual(getattr(self.guild, "created_channels", []), [])
        self.fund(self.alice, 100)
        self.open_sell()
        self.assertIn("Ya tenés un ticket de venta abierto", self.open_sell().text)
        self.assertEqual(len(self.guild.created_channels), 1)

    def test_sell_accept_is_council_only_and_modal_defaults_to_whole_balance(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        for who in (self.alice, self.outsider):
            i = self.press("on_sell_accept", who, ch)
            self.assertIn("Council", i.text)
            self.assertEqual(i.modals, [])
        i = self.press("on_sell_accept", self.council, ch)
        self.assertEqual(i.modals[0].amount.default, "5,000")

    def test_sell_confirm_withdraws_balance_logs_and_blocks_repeat(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        modal = self.press("on_sell_accept", self.council, ch).modals[0]
        j = self.inter(self.council, ch)
        run(modal.on_submit(j))                                             # monto por defecto = todo el balance
        self.assertEqual(self.bal(self.alice), 0)
        self.assertIn("5,000", j.text)
        mov = eco.get_history(self.db, self.alice.id)[0]
        self.assertEqual((mov.mov_type, mov.delta), ("remove-money", -5000))
        self.assertIn("ticket", mov.reason)
        self.assertTrue(any("retirados" in (m.embeds[0].title or "").lower() for m in self.logch.sent if m.embeds))   # quedó en el log
        self.assertTrue(ddb.get_by_channel(self.db, ch.id).processed)
        self.assertEqual(ddb.get_by_channel(self.db, ch.id).status, "closed")           # confirmado el retiro, el ticket se cierra solo
        self.assertTrue(ch.deleted)
        k = self.press("on_sell_accept", self.council, ch)
        self.assertIn("cerrado", k.text)
        j2 = self.inter(self.council, ch)
        run(modal.on_submit(j2))
        self.assertEqual(self.bal(self.alice), 0)
        self.assertIn("ya no está disponible", j2.text)

    def test_sell_partial_amount_and_validation(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        modal = self.press("on_sell_accept", self.council, ch).modals[0]
        for bad in ("abc", "0", "-5", "1.5"):
            j = self.inter(self.council, ch)
            modal.amount.value = bad
            run(modal.on_submit(j))
            self.assertIn("❌", j.text, bad)
        self.assertEqual(self.bal(self.alice), 5000)
        j = self.inter(self.council, ch)
        modal.amount.value = "9.000"                                        # más que el balance
        run(modal.on_submit(j))
        self.assertIn("no alcanza", j.text)
        self.assertEqual(self.bal(self.alice), 5000)
        modal.amount.value = "2.000"
        run(modal.on_submit(self.inter(self.council, ch)))
        self.assertEqual(self.bal(self.alice), 3000)

    def test_sell_submit_by_non_council_changes_nothing(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        modal = self.press("on_sell_accept", self.council, ch).modals[0]
        j = self.inter(self.alice, ch)
        run(modal.on_submit(j))
        self.assertIn("Council", j.text)
        self.assertEqual(self.bal(self.alice), 5000)

    # ---- compra ----
    def test_buy_ticket_asks_amount_explains_deposit_and_has_two_council_buttons(self):
        self.assertIn("Ticket abierto", self.open_buy(amount="2,500").text)
        ch = self.last_channel()
        msg = ch.sent[0]
        self.assertIn(f"<@&{COUNCIL_ROLE_ID}>", msg.content)
        flat = " ".join(f"{n} {v}" for e in msg.embeds for n, v, _ in e.fields)
        self.assertIn("2,500", flat)
        self.assertIn("foto", flat.lower())
        self.assertEqual([c.label for c in msg.view.children], ["Agregar Wuju Coins", "Cerrar"])
        self.assertEqual(ddb.get_by_channel(self.db, ch.id).amount, 2500)
        self.assertEqual(self.bal(self.alice), 0)                           # abrir el ticket no acredita nada

    def test_buy_invalid_amounts_and_single_ticket(self):
        for bad in ("abc", "0", "-3", "1.5"):
            self.assertIn("❌", self.open_buy(amount=bad).text, bad)
        self.assertEqual(getattr(self.guild, "created_channels", []), [])
        self.open_buy()
        self.assertIn("Ya tenés un ticket de compra abierto", self.open_buy().text)
        self.assertEqual(len(self.guild.created_channels), 1)

    def test_buy_add_needs_council_and_a_second_confirmation(self):
        self.open_buy(amount="2.500")
        ch = self.last_channel()
        for who in (self.alice, self.outsider):
            self.assertIn("Council", self.press("on_buy_add", who, ch).text)
        i = self.press("on_buy_add", self.council, ch)
        self.assertEqual(self.bal(self.alice), 0)                           # el primer botón NO agrega nada
        content, kw = i.response.sent[0]
        self.assertIn("2,500", content)
        self.assertTrue(kw["ephemeral"])
        view = kw["view"]
        self.assertEqual([c.label for c in view.children], ["Sí, agregar", "Cancelar"])
        self.assertFalse(run(view.interaction_check(self.inter(self.alice))))      # la confirmación es solo de quien la pidió
        self.assertFalse(run(view.interaction_check(self.inter(self.admin))))
        self.assertTrue(run(view.interaction_check(self.inter(self.council))))

    def test_buy_confirm_adds_coins_with_reason_and_log(self):
        self.open_buy(amount="2.500")
        ch = self.last_channel()
        i = self.press("on_buy_add", self.council, ch)
        view = i.response.sent[0][1]["view"]
        j = self.inter(self.council, ch)
        run(view._ok(j))
        self.assertEqual(self.bal(self.alice), 2500)
        mov = eco.get_history(self.db, self.alice.id)[0]
        self.assertEqual((mov.mov_type, mov.delta), ("addmoney", 2500))
        self.assertIn("Compra de Wuju Coins", mov.reason)
        self.assertIn("ticket", mov.reason)
        self.assertEqual(eco.get_lifetime_deposits(self.db, self.alice.id), 2500)
        self.assertTrue(any("agregados" in (m.embeds[0].title or "").lower() for m in self.logch.sent if m.embeds))
        self.assertEqual([c.label for c in ch.sent[0].view.children], ["Cerrar"])
        self.assertTrue(any("Se agregaron" in m.content for m in ch.sent))
        # repetir: ni el botón ni la confirmación vieja vuelven a agregar
        self.assertIn("ya se hizo", self.press("on_buy_add", self.council, ch).text)
        k = self.inter(self.council, ch)
        run(self.cog.confirm_buy_add(k, ddb.get_by_channel(self.db, ch.id).id, None))
        self.assertEqual(self.bal(self.alice), 2500)

    def test_buy_cancel_adds_nothing(self):
        self.open_buy(amount="2.500")
        ch = self.last_channel()
        view = self.press("on_buy_add", self.council, ch).response.sent[0][1]["view"]
        j = self.inter(self.council, ch)
        run(view._no(j))
        self.assertEqual(self.bal(self.alice), 0)
        self.assertFalse(ddb.get_by_channel(self.db, ch.id).processed)

    def test_close_is_council_only_for_both_kinds_and_deletes_channel(self):
        self.fund(self.alice, 100)
        self.open_sell()
        sell_ch = self.last_channel()
        self.open_buy()
        buy_ch = self.last_channel()
        for ch in (sell_ch, buy_ch):
            self.assertIn("Council", self.press("on_close", self.alice, ch).text)
            self.assertFalse(getattr(ch, "deleted", False))
            self.press("on_close", self.council, ch)
            self.assertTrue(ch.deleted)
            self.assertIsNone(ddb.open_ticket_of(self.db, GUILD_ID, self.alice.id, ddb.get_by_channel(self.db, ch.id).kind))

    def test_admin_without_council_role_can_also_use_ticket_buttons(self):
        self.fund(self.alice, 100)
        self.open_buy(amount="2.500")
        buy_ch = self.last_channel()
        # quien no es Council ni administrador sigue rechazado
        self.assertIn("Council", self.press("on_buy_add", self.alice, buy_ch).text)
        # un administrador (sin rol Council) recibe la confirmación y puede agregar los Wuju Coins
        i = self.press("on_buy_add", self.admin, buy_ch)
        view = i.response.sent[0][1]["view"]
        run(view._ok(self.inter(self.admin, buy_ch)))
        self.assertEqual(self.bal(self.alice), 2500 + 100)
        # y cerrar el ticket
        self.press("on_close", self.admin, buy_ch)
        self.assertTrue(buy_ch.deleted)

    def test_buttons_do_not_cross_ticket_kinds(self):
        self.fund(self.alice, 100)
        self.open_sell()
        sell_ch = self.last_channel()
        self.open_buy()
        buy_ch = self.last_channel()
        self.assertIn("no es un ticket de este tipo", self.press("on_buy_add", self.council, sell_ch).text)
        self.assertIn("no es un ticket de este tipo", self.press("on_sell_accept", self.council, buy_ch).text)

    def test_channel_creation_failure_leaves_no_ticket(self):
        self.guild.fail_create_channel = True
        self.assertIn("No pude crear el ticket", self.open_buy().text)
        self.assertIsNone(ddb.open_ticket_of(self.db, GUILD_ID, self.alice.id, "buy"))

    def test_sell_race_cannot_overdraw(self):
        self.fund(self.alice, 5000)
        self.open_sell()
        ch = self.last_channel()
        modal = self.press("on_sell_accept", self.council, ch).modals[0]
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.alice.id], amount=4000, request_key="spent")
        j = self.inter(self.council, ch)
        run(modal.on_submit(j))                                             # el monto por defecto (5.000) ya no alcanza
        self.assertIn("no alcanza", j.text)
        self.assertEqual(self.bal(self.alice), 1000)


if __name__ == "__main__":
    unittest.main()
