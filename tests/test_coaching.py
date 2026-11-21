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

    LONG_REPLY = ("buen vod, mejorá el posicionamiento: en las peleas quedate detrás del tanque, "
                  "usá el escape antes de que te encierren y mirá más el minimapa")

    def reply_msg(self, user, thread, mid, reply_to=None, text=None):
        m = self.video_msg(user, thread, text or self.LONG_REPLY, mid)
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
        self.assertEqual([edb.find_by_source(self.db, f"coach:{4100 + k}").reward_spins for k in range(3)], [0, 0, 0])  # la devolución no paga
        m = self.reply_msg(self.council, th, 4203)                                              # respuesta general: cierra las que quedan
        run(self.cog.process_message(m))
        self.assertEqual([edb.find_by_source(self.db, f"coach:{4100 + k}").status for k in range(3)], ["done", "done", "done"])
        self.assertIn("✅", m.reactions)
        self.assertEqual(cdb.pending_msgs(self.db, th.id), [])

    def paid(self, mids):
        """Tiradas que cobraron esas tareas (Wujupón no está cargado en estos tests: se mira reward_spins de las cerradas)."""
        ts = [edb.find_by_source(self.db, f"coach:{m}") for m in mids]
        return sum(t.reward_spins for t in ts if t.status == "done")

    def wujupon(self):
        """Wujupón mínimo para estos tests: solo lo que usa coaching (run → funciones de wujupon_db sobre la misma base)."""
        import wujupon_db as wdb
        wdb.init_schema(self.db)

        class WujuponCog:
            async def run(_s, fn, *a, **kw):
                return fn(self.db, *a, **kw)
        self.bot.cogs["WujuponCog"] = WujuponCog()
        return wdb

    def test_la_devolucion_no_paga_y_cada_video_con_nota_da_una_tirada_al_jugador(self):
        wdb = self.wujupon()
        self.open_post(self.novato)
        th = self.vods(2)
        run(self.cog.process_message(self.video_msg(self.novato, th, "healer https://youtu.be/a1 https://youtu.be/a2", 4120)))   # 2 videos
        m = self.reply_msg(self.coach, th, 4250, reply_to=4100)
        run(self.cog.process_message(m))
        self.assertEqual(self.paid([4100, 4101, 4120]), 0)                                      # el coach no cobra por devolver
        self.assertEqual(wdb.get_spins(self.db, self.novato.id), 0)                             # sin nota todavía
        i = self.grade_pick(self.coach, 4250, 7)
        self.assertEqual(wdb.get_spins(self.db, self.novato.id), 1)
        self.assertIn("+1 tirada", i.response.sent[-1][1].get("content", "") if i.response.sent else i.text)
        self.grade_pick(self.coach, 4250, 9)                                                    # cambiar la nota no vuelve a pagar
        self.assertEqual(wdb.get_spins(self.db, self.novato.id), 1)
        self.assertEqual(wdb.get_spins(self.db, self.coach.id), 0)
        m2 = self.reply_msg(self.coach, th, 4251, reply_to=4120, text=self.LONG_REPLY + " 8/10")  # nota escrita: VOD con 2 videos
        run(self.cog.process_message(m2))
        self.assertIn("🎰", m2.reactions)
        self.assertEqual(wdb.get_spins(self.db, self.novato.id), 3)

    def test_devolucion_corta_ya_no_se_revisa_ni_penaliza(self):
        self.open_post(self.novato)
        th = self.vods(1)
        before = len(self.tasks_ch.sent)
        m = self.reply_msg(self.coach, th, 4400, text="buen vod")                             # corta: completa igual, sin aviso al Staff
        run(self.cog.process_message(m))
        t = edb.find_by_source(self.db, "coach:4100")
        self.assertEqual((t.status, t.done_by), ("done", self.coach.id))
        self.assertEqual(len(self.tasks_ch.sent), before)
        self.assertNotIn("⏳", m.reactions)

    def grade_pick(self, user, reply_id, grade):
        i = self.inter(user, message=SimpleNamespace(reference=SimpleNamespace(message_id=reply_id)))
        run(self.cog.on_grade_pick(i, grade))
        return i

    def test_nota_del_coach_y_promedios(self):
        self.open_post(self.novato)
        th = self.vods(2)
        m = self.reply_msg(self.coach, th, 4900, reply_to=4100)                                 # sin nota escrita: el bot la pide
        run(self.cog.process_message(m))
        self.assertEqual(len(m.replies), 1)
        self.assertIn("nota del 1 al 10", m.replies[0][0])
        self.assertIsInstance(m.replies[0][1]["view"], co.CoachGradeView)
        self.assertIn("Solo el coach", self.grade_pick(self.alice, 4900, 3).text)              # otro no puede
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (None, 0))
        self.grade_pick(self.coach, 4900, 7)
        self.grade_pick(self.coach, 4900, 8)                                                   # la puede cambiar
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (8.0, 1))
        m2 = self.reply_msg(self.council, th, 4901, reply_to=4101, text=self.LONG_REPLY + " Nota: 10")   # nota escrita: sin menú
        run(self.cog.process_message(m2))
        self.assertEqual(m2.replies, [])
        self.assertIn("⭐", m2.reactions)
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (9.0, 2))
        self.assertEqual(cdb.coach_avgs(self.db), [(self.coach.id, 8.0, 1), (self.council.id, 10.0, 1)] if self.coach.id < self.council.id
                         else [(self.council.id, 10.0, 1), (self.coach.id, 8.0, 1)])
        i = self.inter(self.novato)                                                            # sus propias notas
        run(self.cog.coaching_notas.callback(self.cog, i, None))
        self.assertIn("9.0/10", i.text)
        i = self.inter(self.alice)
        run(self.cog.coaching_notas.callback(self.cog, i, self.novato))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.coach)
        run(self.cog.coaching_notas_coaches.callback(self.cog, i, None))
        body = "\n".join(str(getattr(e, "description", "")) for _, kw in i.response.sent + getattr(i, "followups", []) for e in [kw.get("embed")] if e)
        self.assertIn("8.0/10", body)
        self.assertEqual(cdb.players_between(self.db, 8, 9), [(self.novato.id, 9.0, 2)])
        self.assertEqual(cdb.players_between(self.db, 9.5, 10), [])
        i = self.inter(self.coach)
        run(self.cog.coaching_nota_lista.callback(self.cog, i, 9, 7))                          # el orden de los números no importa
        body = "\n".join(str(getattr(e, "description", "")) for _, kw in i.response.sent + getattr(i, "followups", []) for e in [kw.get("embed")] if e)
        self.assertIn(f"<@{self.novato.id}> — **9.0/10**", body)
        i = self.inter(self.alice)
        run(self.cog.coaching_nota_lista.callback(self.cog, i, 1, 10))
        self.assertIn("Solo el Staff", i.text)
        run(self.cog.on_raw_message_delete(SimpleNamespace(message_id=4100)))                  # VOD borrado: su nota no cuenta
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (10.0, 1))

    def test_devolucion_corta_del_staff_no_se_revisa(self):
        self.open_post(self.novato)
        th = self.vods(1)
        run(self.cog.process_message(self.reply_msg(self.council, th, 4600, text="bien")))
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "done")

    def test_vod_tasks_close_only_when_a_coach_replies_and_top_coach(self):
        self.open_post(self.novato)
        th = self.vods(2)
        opi = discord.Member(700000000000000020, "Opi", roles=[discord.Role(enc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)], guild=self.guild)
        self.assertNotIn("task:done", [c.custom_id for c in self.tasks_ch.sent[0].view.children])   # sin botón «Tarea terminada»
        self.assertIn("se cierra sola", self.inter_done(opi, self.tasks_ch.sent[0]))                 # ni con permiso se cierra a mano
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")
        run(self.cog.process_message(self.reply_msg(self.coach, th, 4300)))                          # el coach contesta: se cierran
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").done_by, self.coach.id)
        self.assertEqual(edb.top_coaches(self.db), [(self.coach.id, 2)])
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

    def test_crear_posts_faltantes(self):
        self.cog.CREATE_DELAY = 0
        otro = discord.Member(700000000000000003, "Otro", roles=[self.r_obs], guild=self.guild)
        self.guild.members.append(otro)
        self.open_post(self.novato)                                   # novato ya tiene; falta otro
        self.made[0].first_kw = None
        i = self.inter(self.council)
        run(self.cog.coaching_verificar_posts.callback(self.cog, i))
        view = i.response.sent[0][1]["view"]
        self.assertEqual([m.id for m in view.members], [otro.id])
        i2 = self.inter(self.alice)                                   # sin permiso
        run(self.cog.create_missing(i2, view.members))
        self.assertIn("No tenés permiso", i2.text)
        self.assertEqual(len(self.made), 1)
        i3 = self.inter(self.council)
        run(self.cog.create_missing(i3, [novato_ya := self.novato, otro]))   # novato se saltea
        self.assertEqual(len(self.made), 2)
        self.assertIn("Posts creados: **1**", i3.text)
        self.assertIn("ya tenían: **1**", i3.text)
        kw = self.made[1].first_kw
        self.assertTrue(kw["content"].startswith(otro.mention + " este es tu post de VODs."))
        self.assertIn(otro, kw["allowed_mentions"].kw["users"])
        self.assertIsNotNone(co.cdb.get_post(self.db, otro.id))

    def test_track_cta(self):
        i = self.inter(self.alice)
        run(self.cog.track_crear.callback(self.cog, i, "CTA martes", f"<@{self.novato.id}>"))
        self.assertIn("Solo el Staff", i.text)
        self.open_post(self.novato)
        th = self.made[0]
        run(self.cog.process_message(self.video_msg(self.novato, th, "healer https://youtu.be/antes", 3000)))  # antes del track: no cuenta
        i = self.inter(self.coach)
        run(self.cog.track_crear.callback(self.cog, i, "CTA martes", f"<@{self.novato.id}> <@{self.alice.id}>"))
        self.assertIn("**2** persona", i.text)
        i = self.inter(self.coach)
        run(self.cog.track_crear.callback(self.cog, i, "Otra", f"<@{self.alice.id}>"))
        self.assertIn("Ya hay un track activo", i.text)                                       # solo uno a la vez
        m = self.video_msg(self.novato, th, "healer https://youtu.be/cta1", 3001)
        run(self.cog.process_message(m))
        self.assertIn("📌", m.reactions)
        i = self.inter(self.coach)
        run(self.cog.track_ver.callback(self.cog, i))
        sent = i.response.sent[-1]
        body = "\n".join(e.description for e in sent[1]["embeds"])
        self.assertIn("CTA martes", sent[0])
        self.assertIn("1/2", sent[0])
        self.assertIn(f"<@{self.novato.id}> — ", body)
        self.assertIn(f"/{th.id}/3001", body)
        self.assertIn(f"<@{self.alice.id}> · sin post", body)
        run(self.cog.on_raw_message_delete(SimpleNamespace(message_id=3001)))               # borra el VOD: vuelve a faltar
        self.assertIsNone(cdb.track_members(self.db)[0]["done_at"])
        i = self.inter(self.coach)
        run(self.cog.track_quitar.callback(self.cog, i, self.alice))
        self.assertEqual([r["user_id"] for r in cdb.track_members(self.db)], [self.novato.id])
        i = self.inter(self.coach)
        run(self.cog.track_cerrar.callback(self.cog, i))
        self.assertIn("cerrado", i.text)
        self.assertIsNone(cdb.get_track(self.db))
        i = self.inter(self.coach)
        run(self.cog.track_crear.callback(self.cog, i, "Otra", f"<@{self.alice.id}>"))
        self.assertIn("creado", i.text)

    def test_track_desde_cta(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.executescript("CREATE TABLE IF NOT EXISTS cta_runs(id INTEGER PRIMARY KEY, thread_id INTEGER);"
                        "CREATE TABLE IF NOT EXISTS cta_signups(cta_id INTEGER, user_id INTEGER, slot_key TEXT);"
                        "CREATE TABLE IF NOT EXISTS cta_positions(cta_id INTEGER, user_id INTEGER);")
        c.execute("INSERT INTO cta_runs(id, thread_id) VALUES (7, 5555)")
        c.executemany("INSERT INTO cta_signups VALUES (7, ?, 'k')", [(self.novato.id,), (self.alice.id,)])
        c.execute("INSERT INTO cta_positions VALUES (7, ?)", (self.coach.id,))
        c.commit(); c.close()
        cta_th = discord.Thread(5555, self.guild, "CTA 22 UTC")
        otro = discord.Thread(5556, self.guild, "x")
        i = self.inter(self.coach, channel=otro)
        run(self.cog.track_desde_cta.callback(self.cog, i, self.r_obs, None, None))
        self.assertIn("dentro del hilo de una CTA", i.text)
        i = self.inter(self.coach, channel=cta_th)
        run(self.cog.track_desde_cta.callback(self.cog, i, self.r_obs, None, None))
        self.assertIn("CTA 22 UTC", i.text)
        self.assertEqual([r["user_id"] for r in cdb.track_members(self.db)], [self.novato.id])   # solo el anotado con Observación
        i = self.inter(self.coach, channel=cta_th)
        run(self.cog.track_desde_cta.callback(self.cog, i, self.r_obs, None, None))
        self.assertIn("Ya hay un track activo", i.text)
        i = self.inter(self.coach, channel=cta_th)
        run(self.cog.track_desde_cta.callback(self.cog, i, self.r_coach, None, "agregar"))       # el anotado tardío (posiciones)
        self.assertIn("agregué **1**", i.text)
        self.assertEqual({r["user_id"] for r in cdb.track_members(self.db)}, {self.novato.id, self.coach.id})

    def test_panel_command_is_council_only(self):
        i = self.inter(self.alice)
        run(self.cog.coaching_panel(i))
        self.assertIn("Solo el Council", i.text)
        i = self.inter(self.council)
        run(self.cog.coaching_panel(i))
        self.assertEqual(len(self.panelch.sent), 1)


if __name__ == "__main__":
    unittest.main()
