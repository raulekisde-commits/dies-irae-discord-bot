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
        self.assertIsNone(cdb.guess_role("battle mount"))                                       # solo los 4 roles de coaching
        self.assertEqual(cdb.guess_role("voy de supp"), "Supp")
        self.assertEqual(cdb.guess_role("dps"), "Dps")

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
        self.r_ch = {k: discord.Role(rid, f"Coach {k}", self.guild) for k, (rid, _) in co.COACH_ROLES.items()}
        self.guild.roles += [r for k, r in self.r_ch.items() if k != "Dps"]
        self.coach = discord.Member(700000000000000002, "Coach", roles=[self.r_ch["Healer"]], guild=self.guild)   # coach de healer
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
        self.old_tasks_ch = discord.TextChannel(co.COACHING_TASKS_CHANNEL_ID, self.guild, "tareas")
        self.guild._channels[co.COACHING_TASKS_CHANNEL_ID] = self.old_tasks_ch
        self.bot.channels[co.COACHING_TASKS_CHANNEL_ID] = self.old_tasks_ch
        self.coach_ch = {}
        for k, (_, cid) in co.COACH_ROLES.items():                                          # un canal de pendientes por rol
            ch = discord.TextChannel(cid, self.guild, f"coach-{k.lower()}")
            self.guild._channels[cid] = ch
            self.bot.channels[cid] = ch
            self.coach_ch[k] = ch
        self.tasks_ch = self.coach_ch["Healer"]
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
            sent = SimpleNamespace(content=text, deleted=False)

            async def delete():
                sent.deleted = True
            sent.delete = delete
            m.sent_objs.append(sent)
            return sent
        m.add_reaction, m.reply, m.replies, m.sent_objs = add_reaction, reply, replies, []
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

    def test_vod_task_waits_for_role_and_goes_to_role_channel(self):
        self.open_post(self.novato)
        th = self.made[0]
        m = self.video_msg(self.novato, th, "mi vod https://youtu.be/abc", 5001)
        run(self.cog.process_message(m))
        self.assertEqual([c.sent for c in self.coach_ch.values()], [[], [], [], []])         # sin rol: todavía no hay tarea
        self.assertEqual(len(m.replies), 1)                                                  # el bot pregunta el rol
        self.assertEqual([o.value for o in m.replies[0][1]["view"].children[0].options], ["Dps", "Tank", "Supp", "Healer"])
        self.assertIn("quien subió", self.pick_by(self.bob, 5001))
        ref = SimpleNamespace(message_id=5001)
        i = self.inter(self.novato, message=SimpleNamespace(reference=ref))
        run(self.cog.on_role_pick(i, "Pierce"))                                              # menú viejo: rol que ya no existe
        self.assertEqual(self.tasks_ch.sent, [])
        i = self.inter(self.novato, message=SimpleNamespace(reference=ref))
        run(self.cog.on_role_pick(i, "Healer"))                                              # elegir el rol crea la tarea en su canal
        self.assertIsInstance(i.response.edits[-1]["view"], co.CoachRateView)               # y el mensaje pasa a ser el panel «Puntuar»
        panel = self.tasks_ch.sent[0]
        self.assertIn(f"<@&{co.COACH_ROLES['Healer'][0]}>", panel.content)
        self.assertNotIn(f"<@&{co.OPINOLOGO_ROLE_ID}>", panel.content)
        self.assertNotIn(f"<@&{enc.STAFF_ROLE_ID}>", panel.content)
        self.assertIn("Healer", panel.embeds[0].description)
        self.assertIn("Observación", panel.embeds[0].description)
        self.assertEqual(edb.find_by_source(self.db, "coach:5001").for_role, co.COACH_ROLES["Healer"][0])
        m2 = self.video_msg(self.novato, th, "voy de tank https://youtu.be/def", 5002)    # el rol escrito evita la pregunta
        run(self.cog.process_message(m2))
        self.assertEqual(len(m2.replies), 1)                                                 # no pregunta el rol: pone el panel
        self.assertIsInstance(m2.replies[0][1]["view"], co.CoachRateView)
        self.assertIn("Tank", self.coach_ch["Tank"].sent[0].embeds[0].description)
        self.assertEqual(len(self.tasks_ch.sent), 1)
        self.assertEqual(co.COACH_ROLES["Dps"], (1566691393726980167, 1574275341214556284))
        self.assertEqual(co.COACH_ROLES["Healer"], (1574277110342746232, 1574276898538922035))

    def rate(self, user, vod, grade="7", text="buen posicionamiento, mirá más el minimapa"):
        """Aprieta «Puntuar» en el panel del VOD y manda el formulario. Devuelve (click, submit)."""
        panel = SimpleNamespace(reference=SimpleNamespace(message_id=vod))
        c = self.inter(user, message=panel)
        run(self.cog.on_rate_click(c))
        if not c.response.modals:
            return c, None
        modal = c.response.modals[-1]
        s = self.inter(user, message=panel)
        run(self.cog.on_rate_submit(s, modal.vod_mid, grade, text))
        return c, s

    def test_puntuar_solo_coach_del_rol_y_marca_listo(self):
        wdb = self.wujupon()
        self.open_post(self.novato)
        th = self.vods(2)                                                                    # VODs de healer
        tank = discord.Member(700000000000000030, "CoachTank", roles=[self.r_ch["Tank"]], guild=self.guild)
        self.assertIn("Solo el coach de **Healer**", self.rate(tank, 4100)[0].text)          # coach de otro rol
        self.assertIn("Solo el coach", self.rate(self.council, 4100)[0].text)                # Council tampoco
        run(self.cog.process_message(self.video_msg(self.coach, th, "respuesta larga al video", 4500)))   # responder ya no cierra nada
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")
        c, s = self.rate(self.coach, 4100, "abc")                                            # nota inválida
        self.assertIn("del 1 al 10", s.text)
        _, s = self.rate(self.coach, 4100, "8", "corta")                                     # devolución muy corta
        self.assertIn("mínimo 10", s.text)
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")
        _, s = self.rate(self.coach, 4100, "8")
        t = edb.find_by_source(self.db, "coach:4100")
        self.assertEqual((t.status, t.done_by), ("done", self.coach.id))
        self.assertEqual(cdb.pending_msgs(self.db, th.id), [4101])
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (8.0, 1))
        self.assertEqual(wdb.get_spins(self.db, self.coach.id), 1)                           # tirada para el coach
        edit = s.response.edits[-1]
        self.assertIn("8/10", edit["embed"].title)
        self.assertIn("minimapa", edit["embed"].description)
        self.assertIsInstance(edit["view"], co.CoachRateView)
        self.assertIn("El que sigue", edit["content"])
        self.assertIn("/4101", edit["content"])
        c, _ = self.rate(self.coach, 4100, "9")                                              # cambiar la nota: no vuelve a pagar
        self.assertEqual(c.response.modals[-1].nota.default, "8")
        self.assertEqual(cdb.player_avg(self.db, self.novato.id), (9.0, 1))
        self.assertEqual(wdb.get_spins(self.db, self.coach.id), 1)
        otro = discord.Member(700000000000000031, "Otro coach healer", roles=[self.r_ch["Healer"]], guild=self.guild)
        self.assertIn("solo él puede cambiar", self.rate(otro, 4100)[0].text)
        self.rate(self.coach, 4101, "7")
        self.assertEqual(edb.top_coaches(self.db), [(self.coach.id, 2)])

    def test_segunda_opinion_la_autoriza_el_council(self):
        wdb = self.wujupon()
        self.open_post(self.novato)
        th = self.vods(1)
        vod = self.video_msg(self.novato, th, "healer https://youtu.be/w0", 4100)
        sent = []

        async def fetch_message(mid):
            if mid == 4100:
                return vod
            return next(m for m in sent if m.id == mid)

        async def th_send(content=None, **kw):
            m = SimpleNamespace(id=9900 + len(sent), content=content, kw=kw, reference=None, edits=[])

            async def edit(**e):
                m.edits.append(e)
            m.edit = edit
            sent.append(m)
            return m
        th.fetch_message, th.send = fetch_message, th_send

        async def vod_reply(text=None, **kw):
            m = await th_send(text, **kw)
            m.reference = SimpleNamespace(message_id=4100)
            return m
        vod.reply = vod_reply
        panel = SimpleNamespace(id=9800, reference=SimpleNamespace(message_id=4100))
        i = self.inter(self.novato, message=panel, channel=th)
        run(self.cog.on_appeal_click(i))
        self.assertIn("todavía no tiene nota", i.text)
        _, s = self.rate(self.coach, 4100, "5")
        self.assertIsInstance(s.response.edits[-1]["view"], co.CoachRateView)
        self.assertIn("coach:appeal", [c.custom_id for c in s.response.edits[-1]["view"].children])   # ya puntuado: botón de réplica
        i = self.inter(self.alice, message=panel, channel=th)
        run(self.cog.on_appeal_click(i))
        self.assertIn("Solo quien subió", i.text)
        i = self.inter(self.novato, message=panel, channel=th)
        run(self.cog.on_appeal_click(i))
        self.assertIn("etiqueté", i.text)
        appeal = sent[0]
        self.assertIn(f"<@&{co.COACH_ROLES['Healer'][0]}>", appeal.content)                     # tagea al coach de su rol
        self.assertIsInstance(appeal.kw["view"], co.CoachAppealView)
        i = self.inter(self.novato, message=panel, channel=th)
        run(self.cog.on_appeal_click(i))
        self.assertIn("una sola por VOD", i.text)
        self.assertIn("segunda opinión", self.rate(self.coach, 4100, "6")[0].text)              # el primer coach ya no la cambia
        def propose(user, grade="8"):
            c = self.inter(user, message=appeal, channel=th)
            run(self.cog.on_appeal_propose_click(c))
            if not c.response.modals:
                return c, None
            sb = self.inter(user, message=appeal, channel=th)
            run(self.cog.on_appeal_propose_submit(sb, 4100, grade, "jugó mejor de lo que dice la nota"))
            return c, sb
        self.assertIn("otro coach", propose(self.coach)[0].text)                                # el mismo coach no
        tank = discord.Member(700000000000000030, "CoachTank", roles=[self.r_ch["Tank"]], guild=self.guild)
        self.assertIn("Solo un coach de **Healer**", propose(tank)[0].text)
        otro = discord.Member(700000000000000031, "Otro healer", roles=[self.r_ch["Healer"]], guild=self.guild)
        _, sb = propose(otro, "8")
        self.assertIn("Council tiene que autorizar", sb.text)
        prop = sent[1]
        self.assertIn(f"<@&{co.COUNCIL_ROLE_ID}>", prop.content)
        self.assertEqual(cdb.get_grade(self.db, 4100)["grade"], 5)                              # todavía no cambia
        d = self.inter(otro, message=prop, channel=th)
        run(self.cog.on_appeal_decide(d, True))
        self.assertIn("Solo el Council", d.text)
        d = self.inter(self.council, message=prop, channel=th)
        run(self.cog.on_appeal_decide(d, False))                                                # rechazo: puede proponer otra
        self.assertIn("rechazó", d.response.edits[-1]["content"])
        _, sb = propose(otro, "9")
        prop2 = sent[2]
        d = self.inter(self.council, message=prop, channel=th)                                  # la propuesta vieja ya no vale
        run(self.cog.on_appeal_decide(d, True))
        self.assertEqual(cdb.get_grade(self.db, 4100)["grade"], 5)
        d = self.inter(self.council, message=prop2, channel=th)
        run(self.cog.on_appeal_decide(d, True))
        self.assertIn("de **5/10** a **9/10**", d.response.edits[-1]["content"])
        self.assertEqual((cdb.get_grade(self.db, 4100)["grade"], cdb.get_grade(self.db, 4100)["coach_id"]), (9, otro.id))
        self.assertEqual(wdb.get_spins(self.db, self.coach.id) + wdb.get_spins(self.db, otro.id), 1)   # no vuelve a pagar
        self.assertIn("Segunda opinión ya se cerró".lower(), propose(otro, "3")[0].text.lower())

    def test_vod_con_varios_videos_paga_una_tirada_por_video(self):
        wdb = self.wujupon()
        self.open_post(self.novato)
        th = self.made[0]
        m = self.video_msg(self.novato, th, "healer https://youtu.be/a1 https://youtu.be/a2", 4120)
        run(self.cog.process_message(m))
        self.assertIsInstance(m.replies[0][1]["view"], co.CoachRateView)
        self.rate(self.coach, 4120, "10")
        self.assertEqual(wdb.get_spins(self.db, self.coach.id), 2)
        self.assertEqual(wdb.get_spins(self.db, self.novato.id), 0)                          # el jugador no cobra

    def test_paneles_para_vods_viejos_una_sola_vez(self):
        self.open_post(self.novato)
        th = self.made[0]
        cdb.add_vods(self.db, message_id=4130, thread_id=th.id, user_id=self.novato.id, is_obs=True, vods=[("yt:old", "u")], role_label="DPS")
        cdb.add_vods(self.db, message_id=4131, thread_id=th.id, user_id=self.novato.id, is_obs=True, vods=[("yt:old2", "u")])   # sin rol
        old = self.video_msg(self.novato, th, "vod viejo", 4130)

        async def fetch_message(_id):
            return old
        th.fetch_message = fetch_message
        self.cog.CREATE_DELAY = 0
        cdb.init_schema(self.db)
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM coaching_flags WHERE key=?", (co.RATE_PANELS_FLAG,))
        c.commit(); c.close()
        self.assertEqual(run(self.cog.post_missing_rate_panels()), 1)                        # solo el que tiene rol
        self.assertIsInstance(old.replies[0][1]["view"], co.CoachRateView)
        self.assertIn("Dps", old.replies[0][0])
        self.assertEqual(run(self.cog.post_missing_rate_panels()), 0)                        # una sola vez

    def pick_by(self, user, mid):
        i = self.inter(user, message=SimpleNamespace(reference=SimpleNamespace(message_id=mid)))
        run(self.cog.on_role_pick(i, "Dps"))
        return i.text

    def test_member_vod_is_labeled_and_not_counted_for_quota(self):
        self.open_post(self.alice)
        th = self.made[0]
        run(self.cog.process_message(self.video_msg(self.alice, th, "healer https://youtu.be/q", 6001)))
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

    def test_sin_vods_y_publico(self):
        self.open_post(self.novato)
        run(self.cog.process_message(self.video_msg(self.novato, self.made[0], "healer https://youtu.be/z", 9200)))
        lazy = discord.Member(700000000000000019, "Vago2", roles=[self.r_obs], guild=self.guild)
        self.guild.members.append(lazy)
        i = self.inter(self.alice)
        run(self.cog.coaching_sin_vods.callback(self.cog, i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.coach)
        run(self.cog.coaching_sin_vods.callback(self.cog, i, True))
        sent = i.response.sent[-1][1]
        body = "\n".join(str(getattr(e, "description", "")) for e in sent["embeds"])
        self.assertIn(f"<@{lazy.id}> · sin post", body)
        self.assertNotIn(f"<@{self.novato.id}>", body)
        self.assertFalse(sent.get("ephemeral"))

    def test_reporte_largo_se_parte_en_varios_mensajes(self):
        for k in range(150):
            self.guild.members.append(discord.Member(710000000000000000 + k, f"Obs{k:03d}" * 3, roles=[self.r_obs], guild=self.guild))
        i = self.inter(self.coach)
        run(self.cog.coaching_reporte.callback(self.cog, i, None))
        msgs = [kw for _, kw in i.response.sent if "embeds" in kw]
        self.assertGreater(len(msgs), 1)
        for kw in msgs:
            self.assertLessEqual(len(kw["embeds"]), 10)
            self.assertLessEqual(sum(len(e.title or "") + len(e.description or "") for e in kw["embeds"]), 6000)
            self.assertTrue(kw.get("ephemeral"))

    def mis(self, user):
        i = self.inter(user)
        run(self.cog.mis_vods.callback(self.cog, i))
        return i.text

    def vods(self, n):
        th = self.made[0]
        for k in range(n):
            run(self.cog.process_message(self.video_msg(self.novato, th, f"healer https://youtu.be/w{k}", 4100 + k)))
        return th

    def wujupon(self):
        """Wujupón mínimo para estos tests: solo lo que usa coaching (run → funciones de wujupon_db sobre la misma base)."""
        import wujupon_db as wdb
        wdb.init_schema(self.db)

        class WujuponCog:
            async def run(_s, fn, *a, **kw):
                return fn(self.db, *a, **kw)
        self.bot.cogs["WujuponCog"] = WujuponCog()
        return wdb

    def test_vod_tasks_close_only_with_the_button(self):
        self.open_post(self.novato)
        self.vods(1)
        opi = discord.Member(700000000000000020, "Opi", roles=[discord.Role(enc.OPINOLOGO_ROLE_ID, "Opinólogo", self.guild)], guild=self.guild)
        self.assertNotIn("task:done", [c.custom_id for c in self.tasks_ch.sent[0].view.children])   # sin botón «Tarea terminada»
        self.assertIn("se cierra sola", self.inter_done(opi, self.tasks_ch.sent[0]))                 # ni con permiso se cierra a mano
        self.assertEqual(edb.find_by_source(self.db, "coach:4100").status, "open")

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
        run(self.cog.track_desde_cta.callback(self.cog, i, self.r_ch["Healer"], None, "agregar"))       # el anotado tardío (posiciones)
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
