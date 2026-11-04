"""Pruebas de la capa de Discord de Wuju Coins usando el stub (sin red).

Verifican importación, decoradores, permisos, canales, paneles, paginación y reintento de logs.
No prueban que la API real de Discord acepte los mensajes.
"""
import asyncio
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import discord_stub  # noqa: E402

discord_stub.install()

import discord  # noqa: E402
import economy  # noqa: E402
import economy_db as eco  # noqa: E402

MEMBER_ROLE = 1000000000000000001
TREASURER_ROLE = 1000000000000000002
TARGET_ROLE = 1000000000000000003
GUILD_ID = 2000000000000000001
BAL_CHANNEL = 3000000000000000001
ECON_CHANNEL = 3000000000000000002
LOG_CHANNEL = 3000000000000000003
OTHER_CHANNEL = 3000000000000000004


class FakeCtx:
    _mid = 5000000000000000000

    def __init__(self, guild, author, channel, content=""):
        FakeCtx._mid += 1
        self.guild, self.author, self.channel = guild, author, channel
        self.message = discord.Message(channel=channel, content=content, author=author)
        self.message.id = FakeCtx._mid
        self.out = []

    async def reply(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))

    async def send(self, content=None, *, embed=None, **kw):
        self.out.append((content, embed))

    @property
    def texts(self):
        return [c for c, _ in self.out if c]

    @property
    def embeds(self):
        return [e for _, e in self.out if e]


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append(content)

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append(content)


class FakeInteraction:
    def __init__(self, guild, user, channel):
        self.guild, self.user, self.channel_id, self.channel = guild, user, channel.id, channel
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response.sent)

    @property
    def replies(self):
        return self.response.sent


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class EconomyDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        self.bot = discord.ext.commands.Bot()
        self.guild = discord.Guild(GUILD_ID, owner_id=1)
        self.guild.me = discord.Member(999, "bot", guild=self.guild, bot=True)
        self.bot.guilds_map = {GUILD_ID: self.guild}

        self.r_member = discord.Role(MEMBER_ROLE, "Miembro", self.guild)
        self.r_treasurer = discord.Role(TREASURER_ROLE, "Treasurer", self.guild)
        self.r_target = discord.Role(TARGET_ROLE, "Target", self.guild)
        self.guild.roles = [self.r_member, self.r_treasurer, self.r_target]

        self.channels = {c: discord.TextChannel(c, self.guild) for c in (BAL_CHANNEL, ECON_CHANNEL, LOG_CHANNEL, OTHER_CHANNEL)}
        self.guild._channels = self.channels
        self.bot.channels = self.channels

        self.owner = self._member(1, "Owner", [])
        self.admin = self._member(2, "Admin", [], administrator=True)
        self.alice = self._member(100000000000000010, "Alice", [self.r_member])
        self.bob = self._member(100000000000000011, "Bob", [self.r_member, self.r_target])
        self.treasurer = self._member(100000000000000012, "Tess", [self.r_member, self.r_treasurer])
        self.carol = self._member(100000000000000013, "Carol", [self.r_target])
        self.botmember = self._member(100000000000000099, "RoleBot", [self.r_target], bot=True)

        self.cog = run(economy.setup_economy(self.bot, self.db, GUILD_ID, MEMBER_ROLE))
        self.cog = self.bot.cogs["EconomyCog"]

    def tearDown(self):
        self.tmp.cleanup()

    def _member(self, uid, name, roles, **kw):
        m = discord.Member(uid, name, roles=roles, guild=self.guild, **kw)
        self.guild.members.append(m)
        return m

    def cmd(self, name, member, channel_id, args=None):
        ctx = FakeCtx(self.guild, member, self.channels[channel_id])
        callback = getattr(self.cog, name.replace("-", "_")).callback
        if args is None:
            run(callback(self.cog, ctx))
        else:
            run(callback(self.cog, ctx, **args))
        return ctx

    def configure(self):
        for c in ("bal", "top"):
            eco.set_command_channel(self.db, c, BAL_CHANNEL)
        for c in ("addmoney", "addmoney-role", "remove-money"):
            eco.set_command_channel(self.db, c, ECON_CHANNEL)
            eco.grant_command_role(self.db, c, TREASURER_ROLE)
        eco.set_setting(self.db, "log_channel_id", str(LOG_CHANNEL))

    # ---------- registro de comandos ----------
    def test_commands_are_registered_with_expected_names(self):
        prefix = {"bal", "top", "addmoney", "addmoney-role", "remove-money"}
        found = {getattr(self.cog, n).name for n in dir(economy.EconomyCog) if isinstance(getattr(economy.EconomyCog, n, None), discord_stub.Command)}
        self.assertEqual(found, prefix)
        slash = {getattr(economy.EconomyCog, n).name for n in dir(economy.EconomyCog)
                 if isinstance(getattr(economy.EconomyCog, n, None), discord_stub.AppCommand)}
        self.assertEqual(slash, {"config-moneda", "bal-reintentar-registro", "perms-canal", "perms-comando", "perms-ver", "verhistorial", "importar-bal", "set-balance", "eco-stats", "reset", "bal", "top", "addmoney", "addmoney-role", "remove-money", "give-money", "add", "remove", "set", "importar-historico", "cargar-dia", "icono"})

    # ---------- !bal ----------
    def test_bal_disabled_until_channel_configured(self):
        ctx = self.cmd("bal", self.alice, BAL_CHANNEL, {"objetivo": ""})
        self.assertFalse(ctx.embeds)
        self.assertIn("perms-canal", ctx.texts[0])

    def test_bal_requires_member_role_and_right_channel(self):
        self.configure()
        # sin rol de miembro
        ctx = self.cmd("bal", self.carol, BAL_CHANNEL, {"objetivo": ""})
        self.assertFalse(ctx.embeds)
        # canal incorrecto
        ctx = self.cmd("bal", self.alice, OTHER_CHANNEL, {"objetivo": ""})
        self.assertFalse(ctx.embeds)
        self.assertIn(f"<#{BAL_CHANNEL}>", ctx.texts[0])
        # correcto
        ctx = self.cmd("bal", self.alice, BAL_CHANNEL, {"objetivo": ""})
        self.assertEqual(len(ctx.embeds), 1)
        self.assertIn(self.alice.mention, ctx.embeds[0].description)
        self.assertIn("0 Wuju Coins", ctx.embeds[0].description)

    def test_bal_other_by_mention_id_exact_name_and_errors(self):
        self.configure()
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.bob.id], amount=150000, request_key="seed")
        for target in (self.bob.mention, str(self.bob.id), "bob", "BOB"):
            ctx = self.cmd("bal", self.alice, BAL_CHANNEL, {"objetivo": target})
            self.assertEqual(len(ctx.embeds), 1, target)
            self.assertIn(self.bob.mention, ctx.embeds[0].description)
            self.assertIn("-150,000", ctx.embeds[0].description)
            self.assertNotIn(".", ctx.embeds[0].description.replace("<@", ""))
        # parcial NO coincide
        ctx = self.cmd("bal", self.alice, BAL_CHANNEL, {"objetivo": "Bo"})
        self.assertFalse(ctx.embeds)
        self.assertIn("No encontré", ctx.texts[0])
        # ambiguo
        self._member(100000000000000050, "Twin", [self.r_member])
        self._member(100000000000000051, "twin", [self.r_member])
        ctx = self.cmd("bal", self.alice, BAL_CHANNEL, {"objetivo": "twin"})
        self.assertFalse(ctx.embeds)
        self.assertIn("varios usuarios", ctx.texts[0])

    # ---------- addmoney ----------
    def test_addmoney_permissions_and_flow(self):
        self.configure()
        args = {"args": f"{self.alice.mention} 150.000"}
        # sin permiso
        ctx = self.cmd("addmoney", self.alice, ECON_CHANNEL, args)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        self.assertIn("permiso", ctx.texts[0])
        # addmoney funciona en CUALQUIER canal (aunque no sea el configurado)
        ctx = self.cmd("addmoney", self.treasurer, OTHER_CHANNEL, args)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 150000)
        eco.apply_operation(self.db, kind="remove", actor_id=1, user_ids=[self.alice.id], amount=150000, request_key="reset-test")
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        ctx = self.cmd("addmoney", self.treasurer, ECON_CHANNEL, args)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 150000)
        emb = ctx.embeds[0]
        values = {n: v for n, v, _ in emb.fields}
        self.assertIn("150,000", values["Cantidad agregado"])
        self.assertIn("0 Wuju Coins", values["Balance anterior"])
        self.assertIn("150,000", values["Balance nuevo"])
        self.assertIn(self.treasurer.mention, values["Ejecutado por"])
        # registro de auditoría publicado
        log = self.channels[LOG_CHANNEL].sent
        self.assertEqual(len(log), 2)
        self.assertIn("IDs", {n for n, _, _ in log[0].embed.fields})

    def test_addmoney_rejects_bad_amounts(self):
        self.configure()
        for bad in ["0", "-5", "1,5", "1.500,000", "abc", "12 345 x"]:
            ctx = self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} {bad}"})
            self.assertEqual(eco.get_balance(self.db, self.alice.id), 0, bad)
            self.assertTrue(ctx.texts and ctx.texts[0].startswith(("❌", "Uso")), bad)
        self.assertEqual(self.channels[LOG_CHANNEL].sent, [])

    def test_addmoney_rejects_bots_and_non_mentions(self):
        self.configure()
        ctx = self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.botmember.mention} 100"})
        self.assertEqual(eco.get_balance(self.db, self.botmember.id), 0)
        ctx = self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": "Alice 100"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)

    def test_comma_and_dot_amounts_equal(self):
        self.configure()
        self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 1,500,000"})
        self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 1.500.000"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 3_000_000)

    # ---------- por rol ----------
    def test_addmoney_role_excludes_bots_and_reports_everyone(self):
        self.configure()
        ctx = self.cmd("addmoney-role", self.treasurer, ECON_CHANNEL, {"args": f"{self.r_target.mention} 50000"})
        self.assertEqual(eco.get_balance(self.db, self.bob.id), 50000)
        self.assertEqual(eco.get_balance(self.db, self.carol.id), 50000)
        self.assertEqual(eco.get_balance(self.db, self.botmember.id), 0)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        emb = ctx.embeds[0]
        values = {n: v for n, v, _ in emb.fields}
        self.assertEqual(values["Destinatarios"], "2")
        self.assertIn("100,000", values["Total agregado"])
        self.assertIn(self.bob.mention, emb.description)
        self.assertIn(self.carol.mention, emb.description)
        self.assertNotIn(self.botmember.mention, emb.description)

    def test_role_without_eligible_members_changes_nothing(self):
        self.configure()
        empty = discord.Role(1000000000000000009, "Vacio", self.guild)
        self.guild.roles.append(empty)
        ctx = self.cmd("addmoney-role", self.treasurer, ECON_CHANNEL, {"args": f"{empty.mention} 10"})
        self.assertIn("no tiene miembros elegibles", ctx.texts[0])
        self.assertEqual(eco.get_all_balances(self.db), {})

    def test_large_role_is_paginated_within_discord_limits(self):
        self.configure()
        big = discord.Role(1000000000000000010, "Gran", self.guild)
        self.guild.roles.append(big)
        for i in range(500):
            self._member(100000000001000000 + i, f"m{i}", [big])
        ctx = self.cmd("addmoney-role", self.treasurer, ECON_CHANNEL, {"args": f"{big.mention} 1.234.567"})
        self.assertGreater(len(ctx.embeds), 1)
        listed = 0
        for emb in ctx.embeds:
            self.assertLessEqual(len(emb.description), 4096)
            self.assertLessEqual(len(emb), 6000)
            for name, value, _ in emb.fields:
                self.assertLessEqual(len(value), 1024)
            listed += emb.description.count("<@")
        self.assertEqual(listed, 500)
        self.assertTrue(all(eco.get_balance(self.db, 100000000001000000 + i) == 1234567 for i in range(500)))
        # auditoría con balance anterior/posterior, también paginada y completa
        log = self.channels[LOG_CHANNEL].sent
        self.assertGreaterEqual(len(log), len(ctx.embeds))   # el registro lleva balances antes/después: más páginas
        self.assertEqual(sum(m.embed.description.count("→") for m in log), 500)
        for m in log:
            self.assertLessEqual(len(m.embed), 6000)
        self.assertEqual(eco.pending_log_operations(self.db), [])

    def test_remove_money_user_negative_and_role(self):
        self.configure()
        ctx = self.cmd("remove-money", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 25000"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), -25000)
        values = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("-25,000", values["Balance nuevo"])
        ctx = self.cmd("remove-money", self.treasurer, ECON_CHANNEL, {"args": f"{self.r_target.mention} 25000"})
        self.assertEqual(eco.get_balance(self.db, self.bob.id), -25000)
        self.assertEqual(eco.get_balance(self.db, self.carol.id), -25000)
        self.assertEqual(eco.get_balance(self.db, self.botmember.id), 0)
        values = {n: v for n, v, _ in ctx.embeds[0].fields}
        self.assertIn("50,000", values["Total retirado"])

    # ---------- registro: fallo de publicación ----------
    def test_log_failure_never_reapplies_operation_and_retry_publishes(self):
        self.configure()
        self.channels[LOG_CHANNEL].fail_send = True
        self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 1000"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1000)           # el dinero se aplicó una sola vez
        pending = eco.pending_log_operations(self.db)
        self.assertEqual(len(pending), 1)
        # el reintento con el canal caído no cambia balances
        self.assertFalse(run(self.cog.publish_log(pending[0])))
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1000)
        # se recupera el canal: el reintento publica sin duplicar dinero
        self.channels[LOG_CHANNEL].fail_send = False
        self.assertTrue(run(self.cog.publish_log(pending[0])))
        self.assertTrue(run(self.cog.publish_log(pending[0])))                       # idempotente
        self.assertEqual(len(self.channels[LOG_CHANNEL].sent), 1)
        self.assertEqual(eco.pending_log_operations(self.db), [])
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 1000)

    def test_partial_log_resumes_from_failed_page(self):
        self.configure()
        big = discord.Role(1000000000000000011, "Gran2", self.guild)
        self.guild.roles.append(big)
        for i in range(300):
            self._member(100000000002000000 + i, f"n{i}", [big])
        log_ch = self.channels[LOG_CHANNEL]
        original_send = log_ch.send
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise discord.HTTPException("x")
            return await original_send(*a, **k)

        log_ch.send = flaky
        self.cmd("addmoney-role", self.treasurer, ECON_CHANNEL, {"args": f"{big.mention} 10"})
        log_ch.send = original_send
        op_id = eco.pending_log_operations(self.db)[0]
        sent_before = len(log_ch.sent)
        self.assertEqual(sent_before, 1)
        self.assertTrue(run(self.cog.publish_log(op_id)))
        total_pages = len(self.cog.build_pages(eco.get_operation(self.db, op_id), detail=True))
        self.assertEqual(len(log_ch.sent), total_pages)          # ni repetidas ni faltantes
        self.assertEqual(eco.get_balance(self.db, 100000000002000000), 10)

    def test_ops_without_log_channel_stay_pending_then_publish(self):
        self.configure()
        eco.set_setting(self.db, "log_channel_id", None)
        self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 7"})
        self.assertEqual(len(eco.pending_log_operations(self.db)), 1)
        eco.set_setting(self.db, "log_channel_id", str(LOG_CHANNEL))
        run(self.cog.retry_pending_logs.fn(self.cog))
        self.assertEqual(len(self.channels[LOG_CHANNEL].sent), 1)
        self.assertEqual(eco.pending_log_operations(self.db), [])

    # ---------- top ----------
    def test_top_orders_excludes_bots_and_caps_at_ten(self):
        self.configure()
        for i in range(15):
            m = self._member(100000000003000000 + i, f"p{i}", [self.r_member])
            eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[m.id], amount=(i + 1) * 1000, request_key=f"t{i}")
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[self.botmember.id], amount=10 ** 9, request_key="bot")
        ctx = self.cmd("top", self.alice, BAL_CHANNEL)
        lines = ctx.embeds[0].description.split("\n")
        self.assertEqual(len(lines), 10)
        self.assertIn("p14", lines[0])
        self.assertIn("15,000", lines[0])
        self.assertNotIn(str(self.botmember.id), ctx.embeds[0].description)
        self.assertIn("p5", lines[9])

    # ---------- comandos de administración ----------
    def test_admin_commands_reject_non_admins_and_accept_admins(self):
        cmd = economy.EconomyCog.perms_canal
        inter = FakeInteraction(self.guild, self.alice, self.channels[BAL_CHANNEL])
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertIsNone(eco.get_command_channel(self.db, "bal"))
        self.assertIn("administradores", inter.replies[0])
        inter = FakeInteraction(self.guild, self.treasurer, self.channels[BAL_CHANNEL])    # tener un rol "económico" no basta
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertIsNone(eco.get_command_channel(self.db, "bal"))
        inter = FakeInteraction(self.guild, self.admin, self.channels[BAL_CHANNEL])
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertEqual(eco.get_command_channel(self.db, "bal"), BAL_CHANNEL)
        inter = FakeInteraction(self.guild, self.owner, self.channels[ECON_CHANNEL])        # propietario
        run(cmd.callback(self.cog, inter, discord_stub.Choice("top", "top")))
        self.assertEqual(eco.get_command_channel(self.db, "top"), ECON_CHANNEL)

    def test_perms_canal_twice_in_same_channel_disables_the_command(self):
        cmd = economy.EconomyCog.perms_canal
        inter = FakeInteraction(self.guild, self.admin, self.channels[BAL_CHANNEL])
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertEqual(eco.get_command_channel(self.db, "bal"), BAL_CHANNEL)
        inter = FakeInteraction(self.guild, self.admin, self.channels[ECON_CHANNEL])      # otro canal: lo mueve
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertEqual(eco.get_command_channel(self.db, "bal"), ECON_CHANNEL)
        inter = FakeInteraction(self.guild, self.admin, self.channels[ECON_CHANNEL])      # mismo canal: lo deshabilita
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertIsNone(eco.get_command_channel(self.db, "bal"))
        self.assertIn("deshabilitado", inter.replies[0])
        inter = FakeInteraction(self.guild, self.admin, self.channels[ECON_CHANNEL])      # y otra vez: lo vuelve a habilitar
        run(cmd.callback(self.cog, inter, discord_stub.Choice("bal", "bal")))
        self.assertEqual(eco.get_command_channel(self.db, "bal"), ECON_CHANNEL)

    def test_perms_comando_grant_then_revoke_applies_immediately(self):
        eco.set_command_channel(self.db, "addmoney", ECON_CHANNEL)
        args = {"args": f"{self.alice.mention} 500"}
        self.cmd("addmoney", self.carol, ECON_CHANNEL, args)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 0)
        cmd = economy.EconomyCog.perms_comando
        inter = FakeInteraction(self.guild, self.admin, self.channels[ECON_CHANNEL])
        run(cmd.callback(self.cog, inter, self.r_target, discord_stub.Choice("addmoney", "addmoney"), None))
        self.assertIn("ahora puede", inter.replies[0])
        self.cmd("addmoney", self.carol, ECON_CHANNEL, args)             # carol tiene el rol Target
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 500)
        inter = FakeInteraction(self.guild, self.admin, self.channels[ECON_CHANNEL])
        run(cmd.callback(self.cog, inter, self.r_target, discord_stub.Choice("addmoney", "addmoney"), discord_stub.Choice("Revocar", "revocar")))
        self.cmd("addmoney", self.carol, ECON_CHANNEL, args)
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 500)   # ya no puede

    def test_admin_does_not_bypass_channel_restriction(self):
        """bal/top siguen limitados a su canal, incluso para Administrator."""
        self.configure()
        ctx = self.cmd("bal", self.admin, OTHER_CHANNEL, {"objetivo": ""})
        self.assertFalse(ctx.embeds)
        ctx = self.cmd("bal", self.admin, BAL_CHANNEL, {"objetivo": ""})
        self.assertTrue(ctx.embeds)

    def test_admin_money_commands_work_in_any_channel(self):
        self.configure()
        self.cmd("addmoney", self.admin, OTHER_CHANNEL, {"args": f"{self.alice.mention} 500"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 500)
        self.cmd("remove-money", self.admin, OTHER_CHANNEL, {"args": f"{self.alice.mention} 200"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 300)
        self.cmd("addmoney-role", self.admin, OTHER_CHANNEL, {"args": f"{self.r_target.mention} 10"})
        self.assertGreaterEqual(eco.get_balance(self.db, self.carol.id), 10)
        # sin rol sigue sin poder, en cualquier canal
        self.cmd("addmoney", self.bob, OTHER_CHANNEL, {"args": f"{self.alice.mention} 999"})
        self.assertEqual(eco.get_balance(self.db, self.alice.id), 300)

    def test_registro_and_config_moneda_admin_only(self):
        inter = FakeInteraction(self.guild, self.alice, self.channels[BAL_CHANNEL])
        run(economy.EconomyCog.registro_bal(self.cog, inter, self.channels[LOG_CHANNEL]))
        self.assertIsNone(eco.get_setting(self.db, "log_channel_id"))
        inter = FakeInteraction(self.guild, self.admin, self.channels[BAL_CHANNEL])
        run(economy.EconomyCog.registro_bal(self.cog, inter, self.channels[LOG_CHANNEL]))
        self.assertEqual(eco.get_setting(self.db, "log_channel_id"), str(LOG_CHANNEL))

    # ---------- icono ----------
    def test_currency_icon_configuration_and_fallback(self):
        emoji_id = 777000000000000001
        raw = f"<:wuju:{emoji_id}>"
        inter = FakeInteraction(self.guild, self.alice, self.channels[BAL_CHANNEL])
        run(economy.EconomyCog.config_moneda.callback(self.cog, inter, raw))
        self.assertIsNone(eco.get_setting(self.db, "currency_emoji"))                # no admin
        inter = FakeInteraction(self.guild, self.admin, self.channels[BAL_CHANNEL])
        run(economy.EconomyCog.config_moneda.callback(self.cog, inter, raw))         # emoji que el bot no ve
        self.assertIsNone(eco.get_setting(self.db, "currency_emoji"))
        run(economy.EconomyCog.config_moneda.callback(self.cog, inter, "🪙"))         # no personalizado
        self.assertIsNone(eco.get_setting(self.db, "currency_emoji"))
        self.bot.emojis[emoji_id] = discord_stub.Emoji(emoji_id)
        run(economy.EconomyCog.config_moneda.callback(self.cog, inter, raw))
        self.assertEqual(eco.get_setting(self.db, "currency_emoji"), raw)
        self.assertEqual(self.cog.money(150000), f"{raw} 150,000")
        self.assertEqual(self.cog.money(-1500000), f"{raw} -1,500,000")
        del self.bot.emojis[emoji_id]                                               # el emoji deja de estar disponible
        self.assertEqual(self.cog.money(150000), "150,000 Wuju Coins")
        self.assertEqual(eco.get_setting(self.db, "currency_emoji"), raw)            # se conserva el ID configurado

    # ---------- persistencia / reinicio ----------
    def test_configuration_and_balances_survive_restart(self):
        self.configure()
        self.cmd("addmoney", self.treasurer, ECON_CHANNEL, {"args": f"{self.alice.mention} 42.000"})
        bot2 = discord.ext.commands.Bot()
        bot2.guilds_map, bot2.channels = self.bot.guilds_map, self.channels
        run(economy.setup_economy(bot2, self.db, GUILD_ID, MEMBER_ROLE))
        cog2 = bot2.cogs["EconomyCog"]
        ctx = FakeCtx(self.guild, self.alice, self.channels[BAL_CHANNEL])
        run(cog2.bal.callback(cog2, ctx, objetivo=""))
        self.assertIn("42,000", ctx.embeds[0].description)

    def test_unexpected_error_hides_technical_details(self):
        self.configure()

        async def boom(*a, **k):
            raise RuntimeError("SELECT secreto FROM tabla token=abc")

        self.cog._db = boom
        ctx = FakeCtx(self.guild, self.alice, self.channels[BAL_CHANNEL])
        run(self.cog.bal.callback(self.cog, ctx, objetivo=""))
        shown = " ".join(ctx.texts)
        self.assertIn("error inesperado", shown)
        self.assertNotIn("SELECT", shown)
        self.assertNotIn("token", shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
