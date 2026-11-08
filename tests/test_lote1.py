import csv
import io
import os
import sys
import threading
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run  # noqa: E402
import discord  # noqa: E402
import duels  # noqa: E402
import economy as economy_mod  # noqa: E402
import economy_db as eco  # noqa: E402
import encargos as enc  # noqa: E402
import encargos_db as edb  # noqa: E402


class TauntTests(unittest.TestCase):
    class D:
        challenged_id, challenger_id = 111, 222

    def test_every_taunt_formats_and_mentions_both(self):
        for pool in (duels.DECLINE_TAUNTS, duels.TIMEOUT_TAUNTS):
            self.assertGreaterEqual(len(pool), 5)
            for line in pool:
                text = line.format(d="<@111>", c="<@222>")
                self.assertIn("<@111>", text, line)
                self.assertIn("<@222>", text, line)
        m = duels.both_mentions(self.D)
        self.assertEqual({u.id for u in m.kw["users"]}, {111, 222})
        self.assertFalse(m.kw["everyone"])
        self.assertIn("<@111>", duels.taunt(duels.DECLINE_TAUNTS, self.D))


class FullHistoryTests(DiscordBase):
    def move(self, user, amount):
        eco.apply_operation(self.db, kind="add", actor_id=1, user_ids=[user.id], amount=amount, request_key=os.urandom(5).hex())

    def test_csv_has_all_rows_oldest_first_and_excel_bom(self):
        for n in range(1, 6):
            self.move(self.alice, n * 100)
        rows = eco.get_history(self.db, self.alice.id, 10_000_000)
        files = economy_mod.EconomyCog.history_csv_files(rows, "h")
        self.assertEqual(len(files), 1)
        name, data = files[0]
        self.assertEqual(name, "h.csv")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        parsed = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(parsed[0][:3], ["fecha_utc", "tipo", "monto"])
        self.assertEqual([r[2] for r in parsed[1:]], ["100", "200", "300", "400", "500"])
        self.assertEqual(parsed[-1][3], "1500")

    def test_big_history_is_split_in_files(self):
        old = economy_mod.EconomyCog.HISTORY_CSV_MAX_BYTES
        economy_mod.EconomyCog.HISTORY_CSV_MAX_BYTES = 400
        try:
            for n in range(30):
                self.move(self.alice, 10)
            rows = eco.get_history(self.db, self.alice.id, 10_000_000)
            files = economy_mod.EconomyCog.history_csv_files(rows, "h")
        finally:
            economy_mod.EconomyCog.HISTORY_CSV_MAX_BYTES = old
        self.assertGreater(len(files), 1)
        total = sum(len(list(csv.reader(io.StringIO(d.decode("utf-8-sig"))))) - 1 for _, d in files)
        self.assertEqual(total, 30)
        self.assertEqual(files[0][0], "h_parte1.csv")

    def test_command_sends_private_file_with_cooldown_and_permissions(self):
        for n in range(3):
            self.move(self.alice, 100)
        i = self.inter(self.alice)
        run(self.ecog.verhistorial.callback(self.ecog, i, None, True))
        sent = i.response.sent[-1]
        self.assertTrue(sent[1].get("ephemeral"))
        self.assertEqual(sent[1]["files"][0].filename, f"historial_{self.alice.id}.csv")
        i = self.inter(self.alice)
        run(self.ecog.verhistorial.callback(self.ecog, i, None, True))
        self.assertIn("Esperá", i.text)                                   # cooldown
        i = self.inter(self.bob)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice, True))
        self.assertIn("Solo el staff", i.text)                            # no puede ver el de otro
        i = self.inter(self.admin)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice, True))
        self.assertTrue(i.response.sent[-1][1]["files"])
        i = self.inter(self.bob)
        run(self.ecog.verhistorial.callback(self.ecog, i, None, True))
        self.assertIn("Todavía no", i.text)                               # sin movimientos

    def test_without_flag_it_behaves_as_before(self):
        self.move(self.alice, 100)
        i = self.inter(self.admin)
        run(self.ecog.verhistorial.callback(self.ecog, i, self.alice, False))
        self.assertFalse(i.response.sent[-1][1].get("files"))
        self.assertTrue(i.response.sent[-1][1].get("embed"))


class TaskDbTests(DiscordBase):
    def setUp(self):
        super().setUp()
        edb.init_schema(self.db)

    def mk(self, **kw):
        return edb.create_task(self.db, guild_id=1, channel_id=2, text="Revisar el split", created_by=10, now=1000.0, **kw)

    def test_parse_when(self):
        now = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(datetime.fromtimestamp(edb.parse_when("22:30", now), timezone.utc), datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc))
        self.assertEqual(datetime.fromtimestamp(edb.parse_when("09:00", now), timezone.utc), datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(edb.parse_when("+90m", now), now.timestamp() + 5400)
        self.assertEqual(edb.parse_when("+2h", now), now.timestamp() + 7200)
        self.assertEqual(edb.parse_when("+1h30", now), now.timestamp() + 5400)
        for bad in ("", "25:00", "22:75", "mañana", "+0m", "+9999h"):
            with self.assertRaises(edb.TaskError, msg=bad):
                edb.parse_when(bad, now)

    def test_validation(self):
        with self.assertRaises(edb.TaskError):
            edb.create_task(self.db, guild_id=1, channel_id=2, text="   ", created_by=1)
        with self.assertRaises(edb.TaskError):
            edb.create_task(self.db, guild_id=1, channel_id=2, text="x" * 1001, created_by=1)

    def test_publish_claim_happens_once(self):
        t = self.mk()
        self.assertEqual(edb.due_for_publish(self.db, now=1000.0), [t.id])
        results = []
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            results.append(edb.claim_publish(self.db, t.id))

        ts = [threading.Thread(target=go) for _ in range(6)]
        [x.start() for x in ts]
        [x.join() for x in ts]
        self.assertEqual(sum(results), 1)
        edb.mark_published(self.db, t.id, 777)
        self.assertEqual(edb.get_task(self.db, t.id).status, "open")
        self.assertEqual(edb.find_by_message(self.db, 777).id, t.id)
        self.assertEqual(edb.due_for_publish(self.db, now=9999.0), [])

    def test_future_task_is_not_due_yet(self):
        t = self.mk(publish_at=5000.0)
        self.assertEqual(edb.due_for_publish(self.db, now=4999.0), [])
        self.assertEqual(edb.due_for_publish(self.db, now=5000.0), [t.id])

    def test_crash_while_publishing_is_retried_on_restart(self):
        t = self.mk()
        edb.claim_publish(self.db, t.id)
        edb.init_schema(self.db)
        self.assertEqual(edb.get_task(self.db, t.id).status, "scheduled")

    def test_take_release_finish_rules_and_history(self):
        t = self.mk()
        edb.claim_publish(self.db, t.id)
        edb.mark_published(self.db, t.id, 5)
        self.assertEqual(edb.take(self.db, t.id, 20).status, "ok")
        self.assertEqual(edb.take(self.db, t.id, 21).status, "taken")           # ya la tiene otra persona
        self.assertEqual(edb.release(self.db, t.id, 21).status, "notyou")
        self.assertEqual(edb.finish(self.db, t.id, 21, override=False).status, "notyou")
        self.assertEqual(edb.release(self.db, t.id, 20).status, "ok")
        self.assertEqual(edb.take(self.db, t.id, 21).status, "ok")
        res = edb.finish(self.db, t.id, 99, override=True)                      # staff/council la cierra aunque no la tomó
        self.assertEqual((res.status, res.task.done_by, res.task.status), ("ok", 99, "done"))
        self.assertEqual(edb.finish(self.db, t.id, 21, override=False).status, "closed")
        self.assertEqual(edb.take(self.db, t.id, 22).status, "closed")
        kinds = [(k, a) for _, k, a in edb.get_task(self.db, t.id).events]
        self.assertEqual(kinds, [("created", 10), ("taken", 20), ("released", 20), ("taken", 21), ("done", 99)])

    def test_owner_can_finish_without_override(self):
        t = self.mk()
        edb.claim_publish(self.db, t.id)
        edb.mark_published(self.db, t.id, 5)
        edb.take(self.db, t.id, 20)
        self.assertEqual(edb.finish(self.db, t.id, 20, override=False).status, "ok")

    def test_simultaneous_take_has_one_winner(self):
        t = self.mk()
        edb.claim_publish(self.db, t.id)
        edb.mark_published(self.db, t.id, 5)
        out = []
        barrier = threading.Barrier(8)

        def go(uid):
            barrier.wait()
            out.append(edb.take(self.db, t.id, uid).status)

        ts = [threading.Thread(target=go, args=(100 + n,)) for n in range(8)]
        [x.start() for x in ts]
        [x.join() for x in ts]
        self.assertEqual(out.count("ok"), 1)

    def test_automatic_task_is_created_once_per_source(self):
        a = self.mk(source="cta:7:combatlog")
        b = self.mk(source="cta:7:combatlog")
        self.assertEqual(a.id, b.id)


class TaskPingTests(DiscordBase):
    def setUp(self):
        super().setUp()
        edb.init_schema(self.db)

    def test_pings_se_guardan(self):
        t = edb.create_task(self.db, guild_id=1, channel_id=2, text="x", created_by=3, source="a")
        self.assertEqual((t.ping_roles, t.ping_users, t.ping_staff), ([], [], True))
        t = edb.create_task(self.db, guild_id=1, channel_id=2, text="y", created_by=3, source="b",
                            ping_roles=[10, 10, 11], ping_users=[20], ping_staff=False)
        t = edb.get_task(self.db, t.id)
        self.assertEqual((t.ping_roles, t.ping_users, t.ping_staff), ([10, 11], [20], False))

    def test_migracion_de_base_vieja(self):
        import sqlite3
        db = self.db + ".old"
        c = sqlite3.connect(db)
        c.executescript("""CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER,
            text TEXT NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL, publish_at REAL, status TEXT NOT NULL, taker_id INTEGER, done_by INTEGER,
            done_at TEXT, source TEXT, link TEXT);
            INSERT INTO tasks(guild_id,channel_id,text,created_by,created_at,status) VALUES (1,2,'vieja',3,'2026-01-01','open');""")
        c.close()
        edb.init_schema(db)
        t = edb.get_task(db, 1)
        self.assertEqual((t.text, t.ping_staff, t.ping_roles), ("vieja", True, []))


def run_text(case, user, channel):
    i = case.inter(user, channel=channel)
    run(case.cog.canal_log_tareas(i, None))
    return i.text


class TaskDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_staff = discord.Role(enc.STAFF_ROLE_ID, "Staff", self.guild)
        self.r_opi = discord.Role(enc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)
        self.guild.roles += [self.r_staff, self.r_opi]
        self.staff = discord.Member(300000000000000001, "Staff1", roles=[self.r_staff], guild=self.guild)
        self.opi = discord.Member(300000000000000002, "Opi", roles=[self.r_opi], guild=self.guild)
        self.guild.members += [self.staff, self.opi]
        self.tasks_ch = discord.TextChannel(enc.TASKS_CHANNEL_ID, self.guild, "tareas")
        self.guild._channels[enc.TASKS_CHANNEL_ID] = self.tasks_ch
        self.bot.channels[enc.TASKS_CHANNEL_ID] = self.tasks_ch
        self.cog = run(enc.setup_encargos(self.bot, self.db, 1))
        self.cog.ticker.cancel()

    def press(self, user, action, msg):
        i = self.inter(user, message=msg)
        run(getattr(self.cog, f"on_{action}")(i))
        return i

    def tarea(self, who, titulo="Tarea", desc="hacer algo", tags=None, cuando=None, foto=False, tiradas=1):
        i = self.inter(who)
        run(self.cog.crear.callback(self.cog, i, titulo, desc, tags, foto, cuando, tiradas))
        return i

    def test_permissions_to_create_only_council(self):
        for who in (self.alice, self.staff):
            self.assertIn("Solo el Council", self.tarea(who).text)
        self.assertEqual(self.tasks_ch.sent, [])
        for who in (self.council, self.admin):
            self.tarea(who)
        self.assertEqual(len(self.tasks_ch.sent), 2)

    def test_panel_without_tags_pings_staff_and_shows_title(self):
        self.tarea(self.council, "Killboard", "Cargar el killboard")
        msg = self.tasks_ch.sent[0]
        self.assertIn(f"<@&{enc.STAFF_ROLE_ID}>", msg.content)
        self.assertIn("**Killboard**", msg.embeds[0].description)
        self.assertIn("Cargar el killboard", msg.embeds[0].description)

    def test_no_take_button_and_done_directly(self):
        self.assertEqual(sorted(c.custom_id for c in enc.TaskView(self.cog).children), ["task:cancel", "task:done"])
        self.tarea(self.council, desc="Cargar el killboard")
        msg = self.tasks_ch.sent[0]
        self.assertIn("No podés", self.press(self.alice, "done", msg).text)                  # un miembro común no puede
        self.press(self.opi, "done", msg)                                                     # Opinólogo la termina sin tomarla
        t = edb.find_by_message(self.db, msg.id)
        self.assertEqual((t.status, t.done_by, t.taker_id), ("done", self.opi.id, None))

    def test_tags_people_and_roles_only_they_and_council(self):
        self.tarea(self.council, tags=f"<@{self.alice.id}> <@&{enc.OPINOLOGO_ROLE_ID}> <@!{self.alice.id}>")
        msg = self.tasks_ch.sent[0]
        self.assertNotIn(f"<@&{enc.STAFF_ROLE_ID}>", msg.content)                            # con tags no se etiqueta al Staff
        self.assertIn(f"<@{self.alice.id}>", msg.content)
        self.assertIn(f"<@&{enc.OPINOLOGO_ROLE_ID}>", msg.content)
        t = edb.find_by_message(self.db, msg.id)
        self.assertEqual((t.ping_users, t.ping_roles, t.ping_staff), ([self.alice.id], [enc.OPINOLOGO_ROLE_ID], False))
        self.assertIn("No podés", self.press(self.staff, "done", msg).text)                  # Staff no etiquetado: no
        self.press(self.opi, "done", msg)                                                     # miembro del rol etiquetado: sí
        self.assertEqual(edb.find_by_message(self.db, msg.id).done_by, self.opi.id)
        self.tarea(self.council, tags=f"<@{self.alice.id}>")
        msg2 = self.tasks_ch.sent[1]
        self.assertIn("No podés", self.press(self.opi, "done", msg2).text)
        self.press(self.alice, "done", msg2)                                                  # persona etiquetada: sí
        self.assertEqual(edb.find_by_message(self.db, msg2.id).done_by, self.alice.id)
        self.tarea(self.council, tags=f"<@{self.alice.id}>")
        self.press(self.council, "done", self.tasks_ch.sent[2])                               # el Council siempre
        self.assertEqual(edb.find_by_message(self.db, self.tasks_ch.sent[2].id).done_by, self.council.id)

    def test_bad_tags_are_rejected(self):
        self.assertIn("mencioná personas o roles", self.tarea(self.council, tags="juan").text)
        self.assertIn("no existe", self.tarea(self.council, tags="<@&999>").text)
        self.assertEqual(self.tasks_ch.sent, [])

    def test_scheduled_task_publishes_when_due_and_flags_late(self):
        import time
        self.tarea(self.council, desc="Tarea futura", cuando="+30m")
        self.assertEqual(self.tasks_ch.sent, [])
        self.assertEqual(run(self.cog.process_due(time.time() + 60)), 0)
        self.assertEqual(run(self.cog.process_due(time.time() + 31 * 60)), 1)
        self.assertEqual(len(self.tasks_ch.sent), 1)
        self.assertNotIn("retraso", self.tasks_ch.sent[0].content)
        self.tarea(self.council, desc="Otra", cuando="+10m")
        self.assertEqual(run(self.cog.process_due(time.time() + 3 * 3600)), 1)                # el bot estuvo caído
        self.assertIn("retraso", self.tasks_ch.sent[1].content)
        self.assertEqual(run(self.cog.process_due(time.time() + 4 * 3600)), 0)                # nunca se publica dos veces

    def test_bad_when_is_rejected(self):
        self.assertIn("No entendí", self.tarea(self.council, cuando="mañana").text)
        self.assertEqual(self.tasks_ch.sent, [])

    def task_log(self):
        log = discord.TextChannel(enc.TASK_LOG_CHANNEL_ID, self.guild, "logs")
        self.bot.channels[log.id] = log
        return log

    def test_automatic_tasks_have_no_done_button_and_close_only_by_detection(self):
        log = self.task_log()
        run(self.cog.create(text="Cargar el killboard", created_by=1, source="cta:5:killboard"))
        msg = self.tasks_ch.sent[0]
        self.assertEqual([c.custom_id for c in msg.view.children], ["task:cancel"])           # sin «Tarea terminada»
        self.assertIn("se cierra sola", self.press(self.council, "done", msg).text)           # ni el Council la cierra a mano
        self.assertEqual(edb.find_by_message(self.db, msg.id).status, "open")
        self.assertTrue(run(self.cog.complete_by_source("cta:5:killboard", self.staff.id)))   # la cierra el detector
        self.assertEqual(edb.find_by_source(self.db, "cta:5:killboard").status, "done")
        self.assertEqual(len(log.sent), 1)
        run(self.cog.create(text="Revisar el combat log", created_by=1, source="cta:5:combatlog_review"))
        msg2 = self.tasks_ch.sent[1]                                                           # la única automática con botón
        self.assertIn("task:done", [c.custom_id for c in msg2.view.children])
        self.press(self.staff, "done", msg2)
        self.assertEqual(edb.find_by_message(self.db, msg2.id).status, "done")

    def test_command_tasks_log_to_their_own_channel(self):
        import botsettings as bs
        bs.configure(self.db)
        self.addCleanup(lambda: (bs._over.clear(), setattr(bs, "_db", None)))
        log = self.task_log()
        cmd_log = discord.TextChannel(777000000000000001, self.guild, "log-tareas")
        self.bot.channels[cmd_log.id] = cmd_log
        self.tarea(self.council, desc="sin canal propio")
        self.press(self.opi, "done", self.tasks_ch.sent[0])
        self.assertEqual(len(log.sent), 1)                                                     # sin configurar: va al log de siempre
        i = self.inter(self.council, channel=cmd_log)
        run(self.cog.canal_log_tareas(i, None))
        self.assertIn("configurado", i.text)
        self.assertEqual(bs.get("task_log_cmd"), cmd_log.id)
        self.tarea(self.council, desc="con canal propio")
        self.press(self.opi, "done", self.tasks_ch.sent[1])
        run(self.cog.create(text="Cargar conectados", created_by=1, source="cta:6:conectados"))
        run(self.cog.complete_by_source("cta:6:conectados", self.staff.id))
        self.assertEqual(len(cmd_log.sent), 2)                                                 # aviso de configuración + la tarea de comando
        self.assertIn("Tarea #", cmd_log.sent[1].content)
        self.assertEqual(len(log.sent), 2)                                                     # la automática sigue en el log de siempre
        self.assertIn("Solo el rol **Council**", run_text(self, self.staff, cmd_log))

    def test_photo_task_requires_image_and_logs_it(self):
        log = self.task_log()
        self.tarea(self.council, desc="Limpiar el banco", foto=True)
        msg = self.tasks_ch.sent[0]
        t = edb.find_by_message(self.db, msg.id)
        self.assertTrue(t.require_photo)
        i = self.press(self.opi, "done", msg)
        self.assertEqual(len(i.modals), 1)                                                     # pide la foto, no la cierra
        self.assertEqual(edb.find_by_message(self.db, msg.id).status, "open")
        modal = i.modals[0]

        class Att:
            def __init__(self, ctype, name):
                self.content_type, self.filename = ctype, name

            async def to_file(self, filename=None):
                return discord.File(io.BytesIO(b"x"), filename=filename)

        bad = self.inter(self.opi, message=msg)
        modal.upload.values = [Att("text/plain", "a.txt")]
        run(modal.on_submit(bad))
        self.assertIn("imagen", bad.text)
        self.assertEqual(edb.find_by_message(self.db, msg.id).status, "open")
        ok = self.inter(self.opi, message=msg)
        modal.upload.values = [Att("image/jpeg", "Foto.JPG")]
        run(modal.on_submit(ok))
        t = edb.find_by_message(self.db, msg.id)
        self.assertEqual((t.status, t.done_by), ("done", self.opi.id))
        self.assertTrue(msg.deleted)
        self.assertEqual(log.sent[-1].file.filename, f"tarea_{t.id}.jpg")
        self.assertEqual(log.sent[-1].embeds[0].image, f"attachment://tarea_{t.id}.jpg")
        self.assertIn("foto", ok.text)

    def test_reward_spins_shown_in_panel_and_given(self):
        given = []

        class FakeWujupon:
            async def run(self, fn, **kw):
                given.append((kw["user_id"], kw["count"], kw["request_key"]))
                return 7
        self.bot.get_cog = lambda name: FakeWujupon() if name == "WujuponCog" else None
        log = self.task_log()
        self.tarea(self.council, desc="vale 5", tiradas=5)
        self.tarea(self.council, desc="no vale nada", tiradas=0)
        p5, p0 = self.tasks_ch.sent
        self.assertIn(("Recompensa", "🎰 5 tiradas de Wujupón", True), p5.embeds[0].fields)
        self.assertNotIn("Recompensa", [f[0] for f in p0.embeds[0].fields])
        self.press(self.opi, "done", p5)
        self.press(self.opi, "done", p0)
        t5 = edb.find_by_message(self.db, p5.id)
        self.assertEqual(given, [(self.opi.id, 5, f"task:{t5.id}")])                          # la de 0 no regala nada
        self.assertIn("+5 tiradas", log.sent[0].content)
        self.assertNotIn("tirada", log.sent[1].content)
        auto = run(self.cog.create(text="Cargar conectados", created_by=1, source="cta:8:conectados"))
        self.assertEqual(auto.reward_spins, 1)                                                 # las automáticas siguen dando 1

    def test_photo_option_refused_without_library_support(self):
        old, enc.PHOTO_SUPPORTED = enc.PHOTO_SUPPORTED, False
        try:
            self.assertIn("discord.py 2.6", self.tarea(self.council, foto=True).text)
            self.assertEqual(self.tasks_ch.sent, [])
        finally:
            enc.PHOTO_SUPPORTED = old

    def test_create_api_for_automatic_tasks(self):
        t = run(self.cog.create(text="Activar el combatlog", created_by=1, source="cta:9:combatlog", link="https://x"))
        self.assertEqual(t.status, "open")
        again = run(self.cog.create(text="Activar el combatlog", created_by=1, source="cta:9:combatlog"))
        self.assertEqual(again.id, t.id)
        self.assertEqual(len(self.tasks_ch.sent), 1)


if __name__ == "__main__":
    unittest.main()
