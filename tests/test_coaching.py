import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import coaching as co  # noqa: E402
import coaching_db as cdb  # noqa: E402
import encargos as enc  # noqa: E402
import encargos_db as edb  # noqa: E402
import tempfile  # noqa: E402


def att(i, name, ctype="video/mp4"):
    return SimpleNamespace(id=i, filename=name, content_type=ctype, url=f"https://cdn/{name}")


class PureTests(unittest.TestCase):
    def test_extract_vods(self):
        v = dict(cdb.extract_vods("mirá https://youtu.be/abc123?t=5 y https://www.youtube.com/watch?v=abc123 y https://twitch.tv/videos/99 y https://google.com",
                                  [att(1, "a.mp4"), att(2, "foto.png", "image/png")]))
        self.assertEqual(set(v), {"yt:abc123", "twitch.tv/videos/99", "att:1"})              # mismo video = uno solo; la foto y google no cuentan

    def test_guess_role(self):
        self.assertEqual(cdb.guess_role("Voy de healer en la cta https://youtu.be/x"), "Healer")
        self.assertIsNone(cdb.guess_role("tank y healer"))
        self.assertIsNone(cdb.guess_role("https://youtu.be/tank"))
        self.assertEqual(cdb.guess_role("battle mount"), "Battle Mount")

    def test_week_start_is_monday_utc(self):
        d = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)                               # miércoles
        self.assertEqual(cdb.week_start(d), datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(cdb.week_start(d, -1), datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))

    def test_db_counts_dedupe_and_delete(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "t.db")
        cdb.init_schema(db)
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(cdb.add_vods(db, message_id=1, thread_id=9, user_id=5, is_obs=True, vods=[("yt:a", "u"), ("yt:b", "u")], created_at=now), 2)
        self.assertEqual(cdb.add_vods(db, message_id=2, thread_id=9, user_id=5, is_obs=True, vods=[("yt:a", "u")], created_at=now), 0)   # repetido
        cdb.add_vods(db, message_id=3, thread_id=9, user_id=5, is_obs=True, vods=[("yt:c", "u")], created_at=now - timedelta(days=9))
        cdb.add_vods(db, message_id=4, thread_id=8, user_id=6, is_obs=False, vods=[("yt:z", "u")], created_at=now)
        start = cdb.week_start(now)
        end = start + timedelta(weeks=1)
        self.assertEqual(cdb.count_between(db, start, end, only_obs=True), {5: 2})            # la del viernes anterior queda afuera
        self.assertEqual(cdb.count_between(db, start, end, only_obs=False), {6: 1})
        self.assertEqual(cdb.totals(db), {5: 3, 6: 1})
        self.assertEqual(cdb.remove_message(db, 1), 2)                                       # mensaje borrado: dejan de contar
        self.assertEqual(cdb.count_between(db, start, end, only_obs=True), {})
        tmp.cleanup()


class CoachingDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.r_obs = discord.Role(co.OBSERVACION_ROLE_ID, "Observación", self.guild)
        self.r_coach = discord.Role(co.COACHING_ROLE_ID, "Coaching", self.guild)
        self.guild.roles += [self.r_obs, self.r_coach]
        self.r_miembro = discord.Role(co.MIEMBRO_ROLE_ID, "Miembro", self.guild)
        self.guild.roles.append(self.r_miembro)
        self.alice.roles.append(self.r_miembro)
        self.novato = discord.Member(700000000000000001, "Novato", roles=[self.r_obs], guild=self.guild)
        self.coach = discord.Member(700000000000000002, "Coach", roles=[self.r_coach], guild=self.guild)
        self.guild.members += [self.novato, self.coach]
        self.forum = discord.ForumChannel(co.FORUM_CHANNEL_ID, self.guild, "vods")
        made = []

        async def create_thread(*, name, content=None, **kw):
            th = discord.Thread(8800 + len(made), self.guild, name)
            th.parent_id = co.FORUM_CHANNEL_ID
            made.append(th)
            th.first_kw = dict(kw, content=content)
            self.bot.channels[th.id] = th
            return SimpleNamespace(thread=th, message=None)
        self.forum.create_thread = create_thread
        self.made = made
        self.bot.channels[co.FORUM_CHANNEL_ID] = self.forum
        self.tasks_ch = discord.TextChannel(co.COACHING_TASKS_CHANNEL_ID, self.guild, "tareas")
        self.guild._channels[co.COACHING_TASKS_CHANNEL_ID] = self.tasks_ch
        self.bot.channels[co.COACHING_TASKS_CHANNEL_ID] = self.tasks_ch
        self.ecog = run(enc.setup_encargos(self.bot, self.db, 1))
        self.ecog.ticker.cancel()
        self.cog = run(co.setup_coaching(self.bot, self.db, GUILD_ID))

    def open_post(self, user):
        i = self.inter(user)
        run(self.cog.open_post(i))
        return i

    def video_msg(self, user, thread, content, mid, attachments=()):
        replies = []
        m = SimpleNamespace(id=mid, author=user, channel=thread, content=content, attachments=list(attachments),
                            created_at=datetime.now(timezone.utc), jump_url=f"https://discord.com/channels/1/{thread.id}/{mid}", reactions=[])

        async def add_reaction(e):
            m.reactions.append(e)

        async def reply(text=None, **kw):
            replies.append((text, kw))
        m.add_reaction, m.reply, m.replies = add_reaction, reply, replies
        return m

    def test_one_post_per_person_and_only_members_or_observacion(self):
        self.assertIn("Solo los miembros", self.open_post(self.outsider).text)
        i = self.open_post(self.novato)
        self.assertEqual(len(self.made), 1)
        self.assertIn("tu post", i.text)
        self.open_post(self.novato)                                                         # segunda vez: el mismo
        self.assertEqual(len(self.made), 1)
        self.open_post(self.alice)                                                          # un miembro también tiene el suyo
        self.assertEqual(len(self.made), 2)

    def test_vod_creates_task_for_coaching_and_asks_role(self):
        self.open_post(self.novato)
        th = self.made[0]
        m = self.video_msg(self.novato, th, "mi vod https://youtu.be/abc", 5001)
        run(self.cog.process_message(m))
        panel = self.tasks_ch.sent[0]
        self.assertIn(f"<@&{co.COACHING_ROLE_ID}>", panel.content)
        self.assertIn(f"<@&{co.OPINOLOGO_ROLE_ID}>", panel.content)
        self.assertEqual(co.COACHING_TASKS_CHANNEL_ID, 1566710577173102602)
        self.assertNotIn(f"<@&{enc.STAFF_ROLE_ID}>", panel.content)
        self.assertIn("sin indicar", panel.embeds[0].description)
        self.assertIn("Observación", panel.embeds[0].description)
        self.assertEqual(len(m.replies), 1)                                                  # el bot pregunta el rol
        # elegir el rol actualiza la tarea
        ref = SimpleNamespace(message_id=5001)
        i = self.inter(self.novato, message=SimpleNamespace(reference=ref))
        i.data = {"values": ["Healer"]}
        run(self.cog.on_role_pick(i, "Healer"))
        self.assertIn("Healer", panel.embeds[0].description)
        self.assertIn("quien subió", self.pick_by(self.bob, 5001))
        # el rol escrito en el mensaje evita la pregunta
        m2 = self.video_msg(self.novato, th, "voy de tank https://youtu.be/def", 5002)
        run(self.cog.process_message(m2))
        self.assertEqual(m2.replies, [])
        self.assertIn("Tank", self.tasks_ch.sent[1].embeds[0].description)

    def pick_by(self, user, mid):
        i = self.inter(user, message=SimpleNamespace(reference=SimpleNamespace(message_id=mid)))
        run(self.cog.on_role_pick(i, "DPS"))
        return i.text

    def test_member_vod_is_labeled_and_not_counted_for_quota(self):
        self.open_post(self.alice)
        th = self.made[0]
        run(self.cog.process_message(self.video_msg(self.alice, th, "tank https://youtu.be/q", 6001)))
        self.assertIn("Miembro", self.tasks_ch.sent[0].embeds[0].description)
        self.assertIn("no cuenta", self.tasks_ch.sent[0].embeds[0].description)
        i = self.inter(self.alice)
        run(self.cog.mis_vods.callback(self.cog, i))
        self.assertIn("No tenés mínimo", i.text)
        self.assertIn("1", i.text)

    def test_ignores_other_people_posts_and_non_video_and_bots(self):
        self.open_post(self.novato)
        th = self.made[0]
        run(self.cog.process_message(self.video_msg(self.coach, th, "feedback https://youtu.be/zzz", 7001)))   # el coach comenta: no cuenta
        run(self.cog.process_message(self.video_msg(self.novato, th, "hola sin video", 7002)))
        self.assertEqual(self.tasks_ch.sent, [])
        other = discord.Thread(1, self.guild, "x")
        other.parent_id = 12345
        run(self.cog.process_message(self.video_msg(self.novato, other, "https://youtu.be/q", 7003)))
        self.assertEqual(self.tasks_ch.sent, [])

    def test_coaching_role_can_take_and_reminder_is_slow_without_thread(self):
        self.open_post(self.novato)
        run(self.cog.process_message(self.video_msg(self.novato, self.made[0], "healer https://youtu.be/q", 8001)))
        panel = self.tasks_ch.sent[0]
        i = self.inter(self.coach, message=panel)
        run(self.ecog.on_take(i))
        t = edb.find_by_message(self.db, panel.id)
        self.assertEqual((t.status, t.taker_id), ("taken", self.coach.id))
        self.assertIsNone(t.thread_id)                                                      # sin hilo aparte
        self.assertEqual(run(self.ecog.remind_taken(time.time() + 3700)), 0)                 # no avisa a la hora
        self.assertEqual(run(self.ecog.remind_taken(time.time() + 13 * 3600)), 1)            # sí a las 12 h

    def test_deleted_message_stops_counting(self):
        self.open_post(self.novato)
        run(self.cog.process_message(self.video_msg(self.novato, self.made[0], "healer https://youtu.be/q", 9001)))
        run(self.cog.on_raw_message_delete(SimpleNamespace(message_id=9001)))
        i = self.inter(self.novato)
        run(self.cog.mis_vods.callback(self.cog, i))
        self.assertIn("0/3", i.text)

    def test_mis_vods_and_report(self):
        self.open_post(self.novato)
        th = self.made[0]
        self.assertIn("Te faltan **3**", self.mis(self.novato))
        for n in range(3):
            run(self.cog.process_message(self.video_msg(self.novato, th, f"healer https://youtu.be/v{n}", 9100 + n)))
        self.assertIn("cumpliste", self.mis(self.novato))
        lazy = discord.Member(700000000000000009, "Vago", roles=[self.r_obs], guild=self.guild)
        self.guild.members.append(lazy)
        i = self.inter(self.alice)
        run(self.cog.coaching_reporte.callback(self.cog, i, None))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.coach)
        run(self.cog.coaching_reporte.callback(self.cog, i, None))
        body = "\n".join(str(getattr(e, "description", "")) for e in i.response.sent[-1][1]["embeds"])
        self.assertIn(f"<@{lazy.id}> — **0/3**", body)
        self.assertIn("sin post", body)
        self.assertIn(f"<@{self.novato.id}> — **3/3**", body)

    def mis(self, user):
        i = self.inter(user)
        run(self.cog.mis_vods.callback(self.cog, i))
        return i.text

    def reply_msg(self, user, thread, mid, reply_to=None):
        m = self.video_msg(user, thread, "buen vod, mejorá el posicionamiento", mid)
        m.reference = SimpleNamespace(message_id=reply_to) if reply_to else None
        return m

    def vods(self, n):
        th = self.made[0]
        for k in range(n):
            run(self.cog.process_message(self.video_msg(self.novato, th, f"healer https://youtu.be/w{k}", 4100 + k)))
        return th

    def test_response_in_post_completes_pending_tasks(self):
        self.open_post(self.novato)
        th = self.vods(3)
        self.assertEqual(len(self.tasks_ch.sent), 3)
        run(self.cog.process_message(self.reply_msg(self.novato, th, 4200)))                    # el dueño comenta: no completa
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")
        run(self.cog.process_message(self.reply_msg(self.alice, th, 4201)))                     # un miembro cualquiera: no completa
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")
        run(self.cog.process_message(self.reply_msg(self.coach, th, 4202, reply_to=4101)))      # coach responde a UN vod
        self.assertEqual([edb.find_by_source(self.db, f"coach:{4100 + k}").status for k in range(3)], ["open", "done", "open"])
        self.assertEqual(edb.find_by_source(self.db, "coach:4101").done_by, self.coach.id)
        m = self.reply_msg(self.council, th, 4203)                                              # respuesta general: cierra las que quedan
        run(self.cog.process_message(m))
        self.assertEqual([edb.find_by_source(self.db, f"coach:{4100 + k}").status for k in range(3)], ["done", "done", "done"])
        self.assertIn("✅", m.reactions)
        self.assertEqual(cdb.pending_msgs(self.db, th.id), [])

    def test_done_button_works_for_any_coach_role_and_top_coach(self):
        self.open_post(self.novato)
        th = self.vods(2)
        opi = discord.Member(700000000000000020, "Opi", roles=[discord.Role(enc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)], guild=self.guild)
        run(self.ecog.on_take(self.inter(self.coach, message=self.tasks_ch.sent[0])))
        i = self.inter(opi, message=self.tasks_ch.sent[0])                                      # lo tomó otro, pero el Opinólogo puede cerrarla
        run(self.ecog.on_done(i))
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").done_by, opi.id)
        self.assertIn("quien tomó", self.inter_done(self.alice, self.tasks_ch.sent[1]))          # un miembro no
        run(self.cog.process_message(self.reply_msg(self.coach, th, 4300)))
        i = self.inter(self.alice)
        top = edb.top_coaches(self.db)
        self.assertEqual(sorted(top), sorted([(opi.id, 1), (self.coach.id, 1)]))
        i = self.inter(self.alice)
        run(self.ecog.send_top_coach(i))
        emb = i.response.sent[0][1]["embed"]
        self.assertIn("Top coaches", emb.title)

    def inter_done(self, user, msg):
        i = self.inter(user, message=msg)
        run(self.ecog.on_done(i))
        return i.text

    def test_primer_mensaje_taggea_al_usuario(self):
        self.open_post(self.novato)
        kw = self.made[0].first_kw
        self.assertIn(self.novato.mention, kw["content"])
        self.assertTrue(kw["content"].startswith(self.novato.mention + " este es tu post de VODs."))
        self.assertIn(self.novato, kw["allowed_mentions"].kw["users"])

    def test_verificar_posts(self):
        i = self.inter(self.alice)
        run(self.cog.coaching_verificar_posts.callback(self.cog, i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.council)
        run(self.cog.coaching_verificar_posts.callback(self.cog, i))
        txt = i.text
        self.assertIn("Sin post: **1**", txt)
        self.open_post(self.novato)
        i = self.inter(self.council)
        run(self.cog.coaching_verificar_posts.callback(self.cog, i))
        self.assertIn("Todos tienen su post", i.text)
        del self.bot.channels[self.made[0].id]
        i = self.inter(self.council)
        run(self.cog.coaching_verificar_posts.callback(self.cog, i))
        self.assertIn("Post borrado: **1**", i.text)

    def test_panel_command_is_council_only(self):
        i = self.inter(self.alice)
        run(self.cog.coaching_panel.callback(self.cog, i))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.coaching_panel.callback(self.cog, i))
        self.assertEqual(len(self.panelch.sent), 1)


if __name__ == "__main__":
    unittest.main()
