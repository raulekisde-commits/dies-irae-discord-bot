"""Pruebas de /addhorautc (reloj UTC en un canal de voz). Sin red: stub de Discord."""
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import utcclock as uc  # noqa: E402
import utcclock_db as udb  # noqa: E402


def at(h, m, s=0):
    return datetime(2026, 10, 29, h, m, s, tzinfo=timezone.utc)


class LabelTests(unittest.TestCase):
    def test_label_rounds_down_to_ten_minutes_in_utc(self):
        self.assertEqual(udb.label_for(at(14, 0)), "🕒 14:00 UTC")
        self.assertEqual(udb.label_for(at(14, 9, 59)), "🕒 14:00 UTC")
        self.assertEqual(udb.label_for(at(14, 10)), "🕒 14:10 UTC")
        self.assertEqual(udb.label_for(at(0, 59)), "🕒 00:50 UTC")
        from datetime import timedelta
        arg = datetime(2026, 10, 29, 11, 34, tzinfo=timezone(timedelta(hours=-3)))      # 11:34 en Argentina = 14:34 UTC
        self.assertEqual(udb.label_for(arg), "🕒 14:30 UTC")


class UtcClockTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(uc.setup_utcclock(self.bot, self.db, GUILD_ID))
        self.cog.now = lambda: at(14, 34)

    def add(self, user, **kw):
        i = self.inter(user)
        run(self.cog.addhorautc.callback(self.cog, i, **kw))
        return i

    def clock_channel(self):
        cid = udb.get_clock(self.db, GUILD_ID)
        ch = self.guild._channels.get(cid)
        if ch is not None:
            self.bot.channels[cid] = ch
        return ch

    def test_only_council_or_admin_and_creates_view_only_voice_channel(self):
        self.assertIn("Council", self.add(self.alice).text)
        self.assertIsNone(udb.get_clock(self.db, GUILD_ID))
        i = self.add(self.council)
        self.assertIn("Creé", i.text)
        ch = self.clock_channel()
        self.assertEqual(ch.name, "🕒 14:30 UTC")
        everyone = ch.overwrites[self.guild.default_role].perms
        self.assertEqual((everyone["view_channel"], everyone["connect"]), (True, False))      # se ve, pero nadie entra
        self.assertTrue(ch.overwrites[self.guild.me].perms["manage_channels"])                # el bot conserva el permiso de renombrar

    def test_second_call_does_not_create_another(self):
        self.add(self.council)
        self.clock_channel()
        self.assertIn("Ya hay un reloj", self.add(self.admin).text)
        self.assertEqual(len(self.guild.created_channels), 1)

    def test_recreates_if_channel_was_deleted(self):
        self.add(self.council)
        old = self.clock_channel()
        self.bot.channels.pop(old.id)
        self.guild._channels.pop(old.id)
        self.assertIn("Creé", self.add(self.council).text)
        self.assertEqual(len(self.guild.created_channels), 2)

    def test_creation_failure_is_reported(self):
        self.guild.fail_create_channel = True
        self.assertIn("Gestionar canales", self.add(self.council).text)
        self.assertIsNone(udb.get_clock(self.db, GUILD_ID))

    def test_update_renames_only_when_the_ten_minute_block_changes(self):
        self.add(self.council)
        ch = self.clock_channel()
        self.assertEqual(run(self.cog.update_once()), "same")                  # mismo bloque: no toca el canal
        self.cog.now = lambda: at(14, 39, 59)
        self.assertEqual(run(self.cog.update_once()), "same")
        self.cog.now = lambda: at(14, 40)
        self.cog._last_rename = -10_000                                        # pasaron más de 4 minutos reales
        self.assertEqual(run(self.cog.update_once()), "renamed")
        self.assertEqual(ch.name, "🕒 14:40 UTC")
        self.assertEqual(ch.edits, ["🕒 14:40 UTC"])
        self.assertEqual(run(self.cog.update_once()), "same")
        self.assertEqual(ch.edits, ["🕒 14:40 UTC"])                           # una sola edición

    def test_never_renames_more_often_than_the_safety_limit(self):
        self.add(self.council)
        ch = self.clock_channel()
        self.cog.now = lambda: at(14, 40)
        self.assertEqual(run(self.cog.update_once()), "throttled")             # recién creado: espera
        self.assertEqual(ch.name, "🕒 14:30 UTC")

    def test_deleted_channel_disables_the_clock_and_errors_do_not_crash(self):
        self.add(self.council)
        ch = self.clock_channel()
        self.cog.now = lambda: at(14, 50)
        self.cog._last_rename = -10_000
        ch.fail_edit = discord.Forbidden("sin permiso")
        self.assertEqual(run(self.cog.update_once()), "error")                 # no explota
        self.assertEqual(ch.name, "🕒 14:30 UTC")
        self.bot.channels.pop(ch.id)                                           # lo borraron
        self.assertEqual(run(self.cog.update_once()), "deleted")
        self.assertIsNone(udb.get_clock(self.db, GUILD_ID))
        self.assertEqual(run(self.cog.update_once()), "none")


if __name__ == "__main__":
    unittest.main()
