"""Efectos automáticos de las cartas de Wujupón: mutes por rol, congelamientos, escudos, limpiezas, inmunes, casino y cartas malas.

Lógica con SQLite real y capa de Discord con el stub (sin red). NO prueban la API real de Discord (roles y permisos de canal).
"""
import os
import sys
import time

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

    def test_bloqueo_de_casino_hasta_cierre_y_reapertura(self):
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.use("casino_block", self.bob)
        self.assertIsNotNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))
        casino_db.set_open(self.db, GUILD_ID, False, 1)
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.assertIsNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))

    def test_bloqueo_con_casino_cerrado_termina_al_abrir(self):
        casino_db.set_open(self.db, GUILD_ID, False, 1)
        self.use("casino_block", self.bob)
        casino_db.set_open(self.db, GUILD_ID, True, 1)
        self.assertIsNone(casino_db.block_reason(self.db, GUILD_ID, self.bob.id))

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
