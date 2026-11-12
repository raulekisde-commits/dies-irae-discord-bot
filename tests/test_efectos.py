"""Efectos automáticos de las cartas de Wujupón: mutes por rol, congelamientos, escudos, limpiezas, inmunes, casino y cartas malas.

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban la API real de Discord (roles y permisos de canal).
"""
import os
import sys
import time
import types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import casino_db  # noqa: E402
import deposits_db as ddb  # noqa: E402
import economy_db as eco  # noqa: E402
import efectos_db as edb  # noqa: E402
import embo_db  # noqa: E402
import wujupon as wj  # noqa: E402
import wujupon_db as wdb  # noqa: E402
import wujupon_efectos as we  # noqa: E402
from dbutil import connect  # noqa: E402

EFX_CH = 3000000000000000020
CHAT_ROLE = 1000000000000000050
VOICE_ROLE = 1000000000000000051


class EfectosTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.efxch = discord.TextChannel(EFX_CH, self.guild, "efectos")
        self.guild._channels[EFX_CH] = self.efxch
        self.bot.channels[EFX_CH] = self.efxch
        self.r_chat = discord.Role(CHAT_ROLE, "Muteado chat", self.guild)
        self.r_voice = discord.Role(VOICE_ROLE, "Muteado voice", self.guild)
        self.guild.roles += [self.r_chat, self.r_voice]
        self.wcog = run(wj.setup_wujupon(self.bot, self.db, GUILD_ID))
        self.wcog.anim_delay = 0
        self.wcog.cleanup_delay = 0
        self.cog = run(we.setup_efectos(self.bot, self.db, GUILD_ID))
        wdb.set_setting(self.db, "mute_chat_role", str(CHAT_ROLE))
        wdb.set_setting(self.db, "mute_voice_role", str(VOICE_ROLE))
        wdb.set_setting(self.db, "efx_channel", str(EFX_CH))

    def card(self, key, user=None):
        return wdb.give_card(self.db, actor_id=1, user_id=(user or self.alice).id, prize_key=key, reason="t")

    def use(self, key, target=None, user=None):
        """Canjea la carta desde el inventario y, si pide objetivo, lo elige."""
        user = user or self.alice
        cid = self.card(key, user)
        i = self.inter(user)
        run(self.wcog.start_redeem(i, cid))
        if target is not None:
            view = i.response.sent[-1][1]["view"]
            self.assertIsInstance(view, we.TargetView)
            i = self.inter(user)
            run(self.cog.use_on_target(i, cid, target))
        return cid, i

    # ---------------- mutes ----------------
    def test_mute_de_chat_pone_y_saca_el_rol(self):
        cid, i = self.use("mute_15", self.bob)
        self.assertIn("aplicada", i.text)
        self.assertIn(self.r_chat, self.bob.roles)
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")
        self.assertIn("Mute de chat", self.efxch.sent[-1].content)
        conn = connect(self.db)
        conn.execute("UPDATE wjp_effects SET until = ? WHERE user_id = ?", (time.time() - 1, self.bob.id))
        conn.close()
        run(self.cog.expire_loop.fn(self.cog))
        self.assertNotIn(self.r_chat, self.bob.roles)

    def test_mute_de_voice(self):
        self.use("voice_mute_15", self.bob)
        self.assertIn(self.r_voice, self.bob.roles)

    def test_mute_de_voice_lo_mueve_y_lo_devuelve(self):
        here = discord.VoiceChannel(990001, self.guild, "Party")
        afk = discord.VoiceChannel(990002, self.guild, "AFK")
        self.guild.afk_channel, self.guild.voice_channels = afk, [here, afk]
        moves = []

        async def move_to(ch, reason=None):
            moves.append(ch.name)
        self.bob.voice = type("V", (), {"channel": here})()
        self.bob.move_to = move_to
        cid, _ = self.use("voice_mute_15", self.bob)
        self.assertEqual(moves, ["AFK", "Party"])
        conn = connect(self.db)                                 # al vencer, también lo reconecta para que pueda hablar
        conn.execute("UPDATE wjp_effects SET until = ? WHERE user_id = ?", (time.time() - 1, self.bob.id))
        conn.close()
        run(self.cog.expire_loop.fn(self.cog))
        self.assertNotIn(self.r_voice, self.bob.roles)
        self.assertEqual(moves, ["AFK", "Party", "AFK", "Party"])

    def test_sin_roles_configurados_la_carta_no_se_gasta(self):
        wdb.set_setting(self.db, "mute_chat_role", None)
        cid = self.card("mute_15")
        i = self.inter(self.alice)
        run(self.wcog.start_redeem(i, cid))
        self.assertIn("roles de mute", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_no_a_bots(self):
        cid = self.card("mute_15")
        i = self.inter(self.alice)
        run(self.cog.use_on_target(i, cid, self.robot))
        self.assertIn("bot", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    # ---------------- escudo, purgar, inmunes ----------------
    def test_escudo_bloquea_y_no_gasta_la_carta(self):
        self.use("shield_1h", user=self.bob)
        cid, i = self.use("mute_15", self.bob)
        self.assertIn("escudo", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        self.assertNotIn(self.r_chat, self.bob.roles)
        # purgar rompe el escudo y después el mute entra
        self.use("purge_shield", self.bob)
        self.assertIsNone(edb.active_until(self.db, self.bob.id, "shield"))
        i = self.inter(self.alice)
        run(self.cog.use_on_target(i, cid, self.bob))
        self.assertIn(self.r_chat, self.bob.roles)

    def test_purgar_sin_escudo_no_se_gasta(self):
        cid, i = self.use("purge_shield", self.bob)
        self.assertIn("no tiene escudo", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    def test_inmunes_solo_admin_y_bloquean(self):
        i = self.inter(self.council)
        run(self.cog.inm_add.callback(self.cog, i, self.bob, None))
        self.assertIn("Solo el dueño", i.text)
        i = self.inter(self.admin)
        run(self.cog.inm_add.callback(self.cog, i, self.bob, "Council"))
        self.assertTrue(edb.is_immune(self.db, self.bob.id))
        cid, i = self.use("freeze_30m", self.bob)
        self.assertIn("inmune", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        cid2, i = self.use("purge_shield", self.bob)
        self.assertIn("inmune", i.text)
        i = self.inter(self.admin)
        run(self.cog.inm_remove.callback(self.cog, i, self.bob))
        self.assertFalse(edb.is_immune(self.db, self.bob.id))

    # ---------------- congelamientos ----------------
    def test_congelar_balance_bloquea_casino_y_give_pero_no_splits(self):
        self.use("freeze_30m", self.bob)
        self.assertTrue(eco.is_frozen(self.db, self.bob.id))
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.assertEqual(casino_db.block_reason(self.db, GUILD_ID, self.bob.id), eco.FROZEN_TEXT)
        # recibir dinero sigue funcionando (splits, addmoney)
        self.fund(self.bob, 1000)
        self.assertEqual(self.bal(self.bob), 1000)

    def test_congelar_wujupon_bloquea_tirar(self):
        self.use("wjp_freeze_1h", self.bob)
        wdb.gift_spins(self.db, actor_id=1, user_id=self.bob.id, count=1, request_key="g", reason="t")
        i = self.inter(self.bob)
        run(self.wcog.do_spin(i))
        self.assertIn("Wujupón congelado", i.text)
        self.assertEqual(wdb.get_spins(self.db, self.bob.id), 1)

    def test_limpiezas(self):
        self.use("mute_15", self.bob)
        self.use("freeze_30m", self.bob)
        cid, i = self.use("clean_all", user=self.bob)
        self.assertIn("se limpió", i.text)
        self.assertNotIn(self.r_chat, self.bob.roles)
        self.assertFalse(eco.is_frozen(self.db, self.bob.id))
        cid, i = self.use("clean_chat", user=self.bob)
        self.assertIn("nada que limpiar", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")

    # ---------------- casino ----------------
    def test_abrir_y_cerrar_casino(self):
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        cid, i = self.use("casino_open")
        self.assertIn("ya está abierto", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        self.use("casino_close")
        self.assertFalse(casino_db.is_open(self.db, GUILD_ID))

    def test_mute_y_bloqueo_de_casino_se_acumulan(self):
        self.use("mute_15", self.bob)
        self.use("mute_15", self.bob)
        until = edb.active_until(self.db, self.bob.id, "mute_chat")
        self.assertAlmostEqual(until - time.time(), 90 * 60, delta=60)          # 45 + 45 min
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("casino_block", self.bob)
        self.use("casino_block_6h", self.bob)
        until = edb.active_until(self.db, self.bob.id, "casino_block")
        self.assertAlmostEqual(until - time.time(), 8 * 3600, delta=60)         # 2 h + 6 h
        self.use("shield_1h")                                                    # el escudo no se acumula
        self.use("shield_1h")
        self.assertAlmostEqual(edb.active_until(self.db, self.alice.id, "shield") - time.time(), 3600, delta=60)

    def test_bloqueos_de_casino_por_rareza(self):
        p = {x.key: x for x in wdb.list_prizes(self.db)}
        self.assertEqual([(p[k].rarity, p[k].handler) for k in ("casino_block", "casino_block_6h", "casino_block_24h",
                                                                 "casino_block_72h", "casino_block_7d")],
                         [("common", "efx:casino_block:120"), ("rare", "efx:casino_block:360"), ("epic", "efx:casino_block:1440"),
                          ("legendary", "efx:casino_block:4320"), ("mythic", "efx:casino_block:10080")])

    def test_bloqueo_de_casino_por_tiempo(self):
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("casino_block", self.bob)
        self.assertIsNotNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))
        # cerrar y abrir el casino ya no lo corta: dura 2 h
        casino_db.set_open(self.db, GUILD_ID, False, 1)
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.assertIsNotNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))
        until = edb.active_until(self.db, self.bob.id, "casino_block")
        self.assertAlmostEqual(until - time.time(), 120 * 60, delta=60)
        with mock.patch.object(edb, "_now", return_value=time.time() + 121 * 60), \
                mock.patch("time.time", return_value=time.time() + 121 * 60):
            edb.expire_due(self.db)
            self.assertIsNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))

    def test_bloqueo_de_casino_no_corre_con_el_casino_cerrado(self):
        t0 = time.time()
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("casino_block", self.bob)                                       # 2 h
        with mock.patch.object(edb, "_now", return_value=t0 + 30 * 60):
            casino_db.set_open(self.db, GUILD_ID, False, 1)                      # cierra con 1 h 30 min restantes
        eff = edb.list_active(self.db, self.bob.id)[0]
        self.assertIsNone(eff.until)
        self.assertAlmostEqual(eff.paused_left, 90 * 60, delta=60)
        self.assertIn("quedan **1 h 30 min**", edb.effect_text(eff))
        with mock.patch.object(edb, "_now", return_value=t0 + 10 * 3600):
            self.assertEqual(edb.expire_due(self.db), [])                        # cerrado no vence
            casino_db.set_open(self.db, GUILD_ID, True, 1)                       # reanuda
        eff = edb.list_active(self.db, self.bob.id)[0]
        self.assertIsNone(eff.paused_left)
        self.assertAlmostEqual(eff.until - (t0 + 10 * 3600), 90 * 60, delta=60)

    def test_bloqueo_de_casino_con_el_casino_cerrado_arranca_pausado(self):
        casino_db.set_open(self.db, GUILD_ID, False, 1)
        self.use("casino_block", self.bob)
        self.use("casino_block_6h", self.bob)                                    # se suma al pausado
        eff = edb.list_active(self.db, self.bob.id)[0]
        self.assertIsNone(eff.until)
        self.assertAlmostEqual(eff.paused_left, 8 * 3600, delta=60)
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.assertAlmostEqual(edb.active_until(self.db, self.bob.id, "casino_block") - time.time(), 8 * 3600, delta=60)

    def test_comando_suerte_rol_solo_admin(self):
        i = self.inter(self.bob)
        run(self.cog.suerte_rol.callback(self.cog, i, self.r_member, 20))
        self.assertIn("Solo el dueño", i.text)
        i = self.inter(self.admin)
        run(self.cog.suerte_rol.callback(self.cog, i, self.r_member, 20))
        self.assertIn("+20 %", i.text)
        self.assertEqual(wdb.list_luck_roles(self.db), [(self.r_member.id, 20)])
        i = self.inter(self.admin)
        run(self.cog.suerte_rol.callback(self.cog, i, None, None))
        self.assertIn("+20 %", i.response.sent[-1][1]["embed"].description)
        self.bob.roles.append(self.r_member) if self.r_member not in self.bob.roles else None
        i = self.inter(self.bob)
        run(self.wcog.show_effects(i))
        self.assertIn("+20 %", " ".join(v for _, v, _ in i.response.sent[-1][1]["embed"].fields))

    def test_boton_mis_efectos(self):
        i = self.inter(self.bob)
        run(self.wcog.show_effects(i))
        self.assertIn("ningún efecto", i.response.sent[-1][1]["embed"].description)
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("casino_block", self.bob)
        i = self.inter(self.bob)
        run(self.wcog.show_effects(i))
        self.assertIn("Bloqueado del casino", i.response.sent[-1][1]["embed"].description)

    def test_congelar_balance_bloquea_el_casino(self):
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("freeze_30m", self.bob)
        self.assertIn("congelado", casino_db.block_reason(self.db, GUILD_ID, self.bob.id))

    # ---------------- cartas malas ----------------
    def _pull(self, key, user=None):
        user = user or self.alice
        wdb.gift_spins(self.db, actor_id=1, user_id=user.id, count=1, request_key=f"g{key}", reason="t")

        class Fixed:
            def __init__(s, v): s.v = v
            def randrange(s, n): return min(s.v, n - 1)
        acc = 0
        for c in wdb.CATALOG:
            if c[0] == key:
                break
            acc += c[3]
        orig = wdb.draw
        wdb.draw = lambda prizes, rng=None: orig(prizes, Fixed(acc))
        try:
            i = self.inter(user)
            run(self.wcog.do_spin(i))
        finally:
            wdb.draw = orig
        return i

    def test_carta_mala_wuju_puede_dejar_negativo(self):
        i = self._pull("bad_wuju_100k")
        self.assertEqual(self.bal(self.alice), -100_000)
        self.assertIn("100,000", i.text)

    def test_carta_mala_autosilencio(self):
        self._pull("bad_selfmute_voice_5")
        self.assertIn(self.r_voice, self.alice.roles)

    def _in_voice(self, member):
        member.voice = types.SimpleNamespace(channel=types.SimpleNamespace(id=777))
        member.move_to = mock.AsyncMock()
        return member.move_to

    def test_kick_de_voice(self):
        cid, i = self.use("voice_kick", self.bob)
        self.assertIn("no está en un voice", i.text)
        self.assertEqual(wdb.get_card(self.db, cid).status, "available")
        move = self._in_voice(self.bob)
        i = self.inter(self.alice)
        run(self.cog.use_on_target(i, cid, self.bob))
        move.assert_awaited_once()
        self.assertIsNone(move.await_args.args[0])
        self.assertEqual(wdb.get_card(self.db, cid).status, "consumed")

    def test_carta_mala_desconecta_del_voice(self):
        move = self._in_voice(self.alice)
        i = self._pull("bad_voice_dc")
        move.assert_awaited_once()
        self.assertIn("desconectó", i.text)

    def test_carta_mala_cooldown(self):
        self._pull("bad_cd_8h")
        self.assertGreater(ddb.sell_cooldown_remaining(self.db, GUILD_ID, self.alice.id), 7 * 3600)

    def test_escudo_bloquea_carta_mala(self):
        self.use("shield_1h")
        i = self._pull("bad_embo_30")
        self.assertEqual(embo_db.get_balance(self.db, self.alice.id), 0)
        self.assertIn("escudo", i.text)

    def test_reconciliar_aplica_cartas_malas_pendientes_y_roles(self):
        wdb.gift_spins(self.db, actor_id=1, user_id=self.alice.id, count=1, request_key="g", reason="t")
        self.bot.cogs.pop("EfectosCog", None)              # como si el módulo no estuviera al salir la carta
        i = self._pull("bad_selfmute_chat_5")
        self.assertNotIn(self.r_chat, self.alice.roles)
        self.bob.roles.append(self.r_chat)                  # rol colgado sin efecto
        run(self.cog.reconcile())
        self.assertIn(self.r_chat, self.alice.roles)
        self.assertNotIn(self.r_chat, self.bob.roles)

    # ---------------- Council ----------------
    def test_ver_y_quitar_efectos(self):
        self.use("mute_15", self.bob)
        i = self.inter(self.bob)
        run(self.cog.quitar.callback(self.cog, i, self.bob))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.ver.callback(self.cog, i, None))
        self.assertIn(str(self.bob.id), i.response.sent[0][1]["embed"].description)
        i = self.inter(self.council)
        run(self.cog.quitar.callback(self.cog, i, self.bob))
        self.assertIn("1 efecto", i.text)
        self.assertNotIn(self.r_chat, self.bob.roles)

    def test_roles_mute_solo_admin_y_crea_roles(self):
        i = self.inter(self.council)
        run(self.cog.roles_mute.callback(self.cog, i))
        self.assertIn("Solo el dueño", i.text)
        wdb.set_setting(self.db, "mute_chat_role", None)
        wdb.set_setting(self.db, "mute_voice_role", None)
        created, perms = [], []

        async def create_role(name, reason=None, **kw):
            r = discord.Role(1000000000000000090 + len(created), name, self.guild)
            self.guild.roles.append(r)
            created.append(r)
            return r

        async def set_permissions(target, overwrite=None, reason=None, ch=None):
            perms.append(target.name)
        self.guild.create_role = create_role
        self.guild.channels = [self.efxch]
        self.efxch.set_permissions = lambda target, overwrite=None, reason=None: set_permissions(target)
        i = self.inter(self.admin)
        run(self.cog.roles_mute.callback(self.cog, i))
        self.assertEqual([r.name for r in created], ["Muteado chat", "Muteado voice"])
        self.assertIn("Muteado chat", perms)
        self.assertEqual(wdb.get_setting(self.db, "mute_chat_role"), str(created[0].id))
        self.assertIn("Roles listos", i.text)
