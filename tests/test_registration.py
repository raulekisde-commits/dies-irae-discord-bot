"""Registro Discord ↔ Albion: base de datos, aceptación por la propia persona, apodo, permisos y filtro central.
Nunca toca la red: la API de Albion se reemplaza por una función falsa."""
import os
import re
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, PANEL_CH, LOG_CH, GUILD_ID  # noqa: E402
import discord  # noqa: E402
import registration as rg  # noqa: E402
import registration_db as rdb  # noqa: E402

PLAYERS = {
    "Cazador": {"Id": "id-cazador", "Name": "Cazador", "GuildName": "Smogg", "AllianceName": "ALY", "KillFame": 1234567, "DeathFame": 4321},
    "Forastero": {"Id": "id-forastero", "Name": "Forastero", "GuildName": "OtraGuild", "AllianceName": "", "KillFame": 5, "DeathFame": 0},
    "Dos": {"Id": "id-dos", "Name": "Dos", "GuildName": "Smogg", "AllianceName": "", "KillFame": 1, "DeathFame": 1},
    "Tres": {"Id": "id-tres", "Name": "Tres", "GuildName": "Smogg", "AllianceName": "", "KillFame": 1, "DeathFame": 1},
}


def mkreq(db, target=1, initiator=1, name="Cazador", **kw):
    return rdb.create_request(db, target_id=target, initiator_id=initiator, char_id=f"id-{name.lower()}", char_name=name,
                              region="Américas", guild_name="Smogg", **kw)


class RegistrationDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite3")
        rdb.init_schema(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_is_saved_until_the_person_accepts(self):
        r = mkreq(self.db)
        self.assertFalse(rdb.is_registered(self.db, 1))
        reg = rdb.accept(self.db, r.id, 1)
        self.assertEqual((reg.char_name, reg.char_id, reg.method, reg.discord_id), ("Cazador", "id-cazador", "confirmed", 1))
        self.assertTrue(rdb.is_registered(self.db, 1))
        with self.assertRaises(rdb.RegError):                                  # la solicitud no se puede reutilizar
            rdb.accept(self.db, r.id, 1)
        self.assertEqual(len(rdb.get_registrations(self.db, 1)), 1)

    def test_nobody_else_can_accept_for_the_target(self):
        r = mkreq(self.db, target=1, initiator=2)
        for impostor in (2, 3):
            with self.assertRaises(rdb.RegError):
                rdb.accept(self.db, r.id, impostor)
        self.assertFalse(rdb.is_registered(self.db, 1))
        self.assertFalse(rdb.is_registered(self.db, 2))
        actions = [row["action"] for row in rdb.recent_audit(self.db, 1, 20)]
        self.assertEqual(actions.count("impersonation_attempt"), 2)             # la auditoría queda aunque la operación falle
        rdb.accept(self.db, r.id, 1)

    def test_one_discord_has_only_one_albion_character(self):
        rdb.accept(self.db, mkreq(self.db, target=1, name="Cazador").id, 1)
        with self.assertRaises(rdb.RegError) as cm:
            mkreq(self.db, target=1, name="Dos")
        self.assertIn("ya tiene un personaje", str(cm.exception))
        self.assertEqual([r.char_name for r in rdb.get_registrations(self.db, 1)], ["Cazador"])

    def test_one_albion_character_cannot_have_two_discords(self):
        rdb.accept(self.db, mkreq(self.db, target=1).id, 1)
        for name in ("Cazador", "CAZADOR"):
            with self.assertRaises(rdb.RegError) as cm:
                mkreq(self.db, target=2, initiator=2, name=name)
            self.assertIn("otra cuenta", str(cm.exception))
            self.assertNotIn("<@", str(cm.exception))                          # no revela quién lo tiene
        with self.assertRaises(rdb.RegError):
            rdb.admin_link(self.db, 2, "cazador", 99)
        self.assertEqual(len(rdb.get_registrations(self.db, 2)), 0)
        actions = [row["action"] for row in rdb.recent_audit(self.db, 2, 20)]
        self.assertIn("dup_character_attempt", actions)

    def test_same_account_asking_again_for_its_own_character_is_told_so(self):
        rdb.accept(self.db, mkreq(self.db, target=1).id, 1)
        with self.assertRaises(rdb.RegError) as cm:
            mkreq(self.db, target=1)
        self.assertIn("ya tiene vinculado", str(cm.exception))

    def test_race_two_requests_for_same_character_only_one_survives(self):
        results = []

        def go(uid):
            try:
                mkreq(self.db, target=uid, initiator=uid)
                results.append("ok")
            except rdb.RegError:
                results.append("err")

        ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 7)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("ok"), 1)

    def test_race_acceptance_cannot_duplicate_a_character(self):
        r1 = mkreq(self.db, target=1)
        conn = rdb.connect(self.db)               # simula un vínculo que se coló antes de aceptar
        conn.execute("INSERT INTO albion_links(discord_id, char_id, char_name, method, registered_at, updated_at) "
                     "VALUES (2, 'id-cazador', 'Cazador', 'admin_override', 'x', 'x')")
        conn.commit()
        conn.close()
        with self.assertRaises(rdb.RegError):
            rdb.accept(self.db, r1.id, 1)
        self.assertFalse(rdb.is_registered(self.db, 1))

    def test_concurrent_accepts_register_once(self):
        r = mkreq(self.db)
        out = []

        def go():
            try:
                rdb.accept(self.db, r.id, 1)
                out.append("ok")
            except rdb.RegError:
                out.append("err")

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(len(rdb.get_registrations(self.db, 1)), 1)

    def test_pending_requests_expire_and_cannot_be_used(self):
        r = mkreq(self.db, target=1)
        conn = rdb.connect(self.db)
        conn.execute("UPDATE albion_link_requests SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (r.id,))
        conn.commit()
        conn.close()
        with self.assertRaises(rdb.RegError) as cm:
            rdb.accept(self.db, r.id, 1)
        self.assertIn("venció", str(cm.exception))
        self.assertEqual(rdb.get_request(self.db, r.id).status, "expired")      # el vencimiento queda asentado
        self.assertFalse(rdb.is_registered(self.db, 1))
        mkreq(self.db, target=1)                                                # y el personaje queda libre para pedirlo de nuevo

    def test_expire_old_returns_requests_to_notify(self):
        r = mkreq(self.db)
        conn = rdb.connect(self.db)
        conn.execute("UPDATE albion_link_requests SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (r.id,))
        conn.commit()
        conn.close()
        self.assertEqual([x.id for x in rdb.expire_old(self.db)], [r.id])
        self.assertEqual(rdb.expire_old(self.db), [])

    def test_rate_limit_per_initiator(self):
        for i in range(rdb.MAX_REQUESTS_PER_HOUR):
            r = mkreq(self.db, target=100 + i, initiator=7, name=f"P{i}")
            rdb.close_request(self.db, r.id, 7, "cancelled")
        with self.assertRaises(rdb.RegError) as cm:
            mkreq(self.db, target=200, initiator=7, name="Pz")
        self.assertIn("demasiadas", str(cm.exception))

    def test_admin_link_respects_one_character_per_account(self):
        rdb.admin_link(self.db, 1, "Char0", 99)
        with self.assertRaises(rdb.RegError):
            rdb.admin_link(self.db, 1, "Extra", 99)
        with self.assertRaises(rdb.RegError):
            mkreq(self.db, target=1, name="Otro")

    def test_admin_link_is_marked_and_audited(self):
        reg = rdb.admin_link(self.db, 5, "Manual", 99, char_id="id-m")
        self.assertEqual((reg.method, reg.linked_by), ("admin_override", 99))
        self.assertTrue(any(r["action"] == "admin_linked" for r in rdb.recent_audit(self.db, 5)))

    def test_unlink_frees_the_character_and_the_account(self):
        rdb.accept(self.db, mkreq(self.db, target=1, name="Cazador").id, 1)
        self.assertEqual([r.char_name for r in rdb.unlink(self.db, 1, 99)], ["Cazador"])
        self.assertFalse(rdb.is_registered(self.db, 1))
        self.assertEqual(rdb.unlink(self.db, 1, 99), [])
        mkreq(self.db, target=2, initiator=2)                                  # el personaje quedó libre para otra cuenta
        mkreq(self.db, target=1, initiator=1, name="Nuevo")                    # y la cuenta puede registrar otro

    def test_unique_indexes_exist(self):
        conn = rdb.connect(self.db)
        names = {r[1] for r in conn.execute("PRAGMA index_list(albion_links)")}
        conn.close()
        self.assertTrue({"ux_albion_links_char_id", "ux_albion_links_char_name"} <= names)


class RegistrationDiscordTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.cog = run(rg.setup_registration(self.bot, self.db, GUILD_ID, required_guild="Smogg"))
        self.logs = discord.TextChannel(PANEL_CH + 7, self.guild, "registros")
        self.guild._channels[self.logs.id] = self.logs
        self.bot.channels[self.logs.id] = self.logs
        rdb.set_flag(self.db, rdb.REVIEW_CHANNEL_KEY, str(self.logs.id))
        self.guild.me.guild_permissions.manage_nicknames = True

        async def fake(url, params=None):
            self.calls.append((url, params))
            if "gameinfo.albiononline.com" in url and "ams" not in url and "sgp" not in url:
                q = (params or {}).get("q", "")
                return {"players": [p for n, p in PLAYERS.items() if q.casefold() in n.casefold()]}
            return {"players": []}

        self.cog.fetch_json = fake

    def test_roster_cache_avoids_the_search_api(self):
        self.cog.albion_guild_id = "GID"
        members = [{"Id": "id-cazador", "Name": "Cazador", "GuildName": "Smogg", "AllianceName": "", "KillFame": 5, "DeathFame": 1}]

        async def roster(url, params=None):
            self.calls.append((url, params))
            if "/guilds/GID/members" in url and "ams" not in url and "sgp" not in url:
                return members
            raise RuntimeError("HTTP 503")

        self.cog.fetch_json = roster
        self.assertTrue(run(self.cog.refresh_roster()))
        self.calls.clear()
        look = run(self.cog.lookup_character("cazador"))                       # sin mayúsculas, desde la lista guardada
        self.assertEqual((look.kind, look.character.name, look.character.guild), ("ok", "Cazador", "Smogg"))
        self.assertEqual(self.calls, [])                                       # no tocó la API
        self.calls.clear()
        look = run(self.cog.lookup_character("NoEsta"))                        # no figura: baja la lista una vez, NO usa la búsqueda
        self.assertEqual(look.kind, "not_in_roster")
        self.assertTrue(all("/guilds/GID/members" in u for u, _ in self.calls) and len(self.calls) == 1)
        self.calls.clear()
        run(self.cog.lookup_character("OtroMas"))                              # dentro de los 5 min: ni siquiera vuelve a bajarla
        self.assertEqual(self.calls, [])

    def test_roster_survives_a_restart(self):
        self.cog.albion_guild_id = "GID"

        async def roster(url, params=None):
            if "/guilds/GID/members" in url and "ams" not in url and "sgp" not in url:
                return [{"Id": "i", "Name": "Cazador", "GuildName": "Smogg"}]
            raise RuntimeError("HTTP 503")

        self.cog.fetch_json = roster
        self.assertTrue(run(self.cog.refresh_roster()))
        again = rg.RegistrationCog(self.bot, self.db, GUILD_ID, required_guild="Smogg", albion_guild_id="GID")

        async def down(url, params=None):
            raise RuntimeError("HTTP 503")

        again.fetch_json = down
        run(again._load_saved_roster())
        self.assertTrue(again.roster_fresh())
        self.assertEqual(run(again.lookup_character("cazador")).kind, "ok")        # con la API caída, igual funciona

    def test_failed_or_empty_refresh_keeps_the_previous_roster(self):
        self.cog.albion_guild_id = "GID"
        good = [{"Id": "i", "Name": "Cazador", "GuildName": "Smogg"}]
        state = {"data": good}

        async def roster(url, params=None):
            if "ams" in url or "sgp" in url:
                raise RuntimeError("HTTP 503")
            return state["data"]

        self.cog.fetch_json = roster
        self.assertTrue(run(self.cog.refresh_roster()))
        state["data"] = []
        self.assertFalse(run(self.cog.refresh_roster()))
        self.assertIn("cazador", self.cog.roster)

    def _gaps_setup(self):
        self.cog.albion_guild_id = "GID"
        self.cog.member_role_id = self.r_member.id
        roster = [{"Id": f"id-{n}", "Name": n, "GuildName": "Smogg"} for n in ("Cazador", "Dos", "Tres")]

        async def fake(url, params=None):
            if "/guilds/GID/members" in url and "ams" not in url and "sgp" not in url:
                return roster
            raise RuntimeError("HTTP 503")

        self.cog.fetch_json = fake
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1, char_id="id-Cazador")

    def test_sin_discord_lists_guild_players_without_discord_and_is_protected(self):
        self._gaps_setup()
        i = self.inter(self.alice)
        run(self.cog.sin_discord.callback(self.cog, i))
        self.assertIn("el Council o los administradores", i.text)
        i = self.inter(self.council)
        run(self.cog.sin_discord.callback(self.cog, i))
        sent = " ".join(str(m) for m in i.response.sent)
        self.assertIn("Dos", sent)
        self.assertIn("Tres", sent)
        self.assertNotIn("• Cazador", sent)                                    # ya vinculado
        self.assertNotIn(str(self.bob.id), sent)                               # esta lista no habla de Discord

    def test_fuera_de_guild_lists_registered_not_in_roster(self):
        self._gaps_setup()
        rdb.admin_link(self.db, self.bob.id, "Ausente", 1, char_id="id-Ausente")
        import invitados
        invitados.ensure_schema(self.db)
        invitados.add_guest(self.db, self.guild.id, self.bob.id, self.council.id, "amigo", kind="premium")
        prev = self.cog.fetch_json

        async def fake(url, params=None):
            if url.endswith("/players/id-Ausente"):
                return {"Id": "id-Ausente", "Name": "Ausente", "GuildName": "OtraGuild"}
            return await prev(url, params)

        self.cog.fetch_json = fake
        i = self.inter(self.alice)
        run(self.cog.fuera_de_guild.callback(self.cog, i))
        self.assertIn("el Council o los administradores", i.text)
        i = self.inter(self.council)
        run(self.cog.fuera_de_guild.callback(self.cog, i))
        sent = " ".join(str(m) for m in i.response.sent)
        self.assertIn("Ausente", sent)
        self.assertIn("OtraGuild", sent)
        self.assertIn("invitado premium", sent)
        self.assertIn(str(self.council.id), sent)
        self.assertNotIn("Cazador", sent)
        self.assertIn("fuera-de-guild", rg.EXEMPT_COMMANDS)

    def test_fuera_de_guild_staff_puede_y_expulsa_marcados(self):
        from unittest import mock
        import expulsar
        self._gaps_setup()
        rdb.admin_link(self.db, self.outsider.id, "Perdido", 1, char_id="id-Perdido")
        staff = discord.Member(777, "Staffer", roles=[discord.Role(rg.STAFF_ROLE_ID, "Staff", self.guild)], guild=self.guild)
        kicked = []

        async def kick(reason=None):
            kicked.append(reason)
        self.outsider.kick = kick
        i = self.inter(staff)
        run(self.cog.fuera_de_guild.callback(self.cog, i))
        view = [x for x in i.response.sent if x[1].get("view") is not None][-1][1]["view"]
        self.assertEqual([o.value for o in view.selects[0].options], [str(self.outsider.id)])
        self.assertFalse(run(view.interaction_check(self.inter(self.council))))
        view.selects[0].values = [str(self.outsider.id)]
        j = self.inter(staff)
        run(view.children[-2].callback(j))                                      # Expulsar marcados → pide confirmación
        self.assertEqual(kicked, [])
        confirm = j.response.edits[-1]["view"]
        k = self.inter(staff)

        async def eor(content=None, **kw):
            k.response.edits.append({"content": content})
        k.edit_original_response = eor
        with mock.patch.object(expulsar, "check_target", return_value=None):
            run(confirm.children[0].callback(k))
        self.assertEqual(len(kicked), 1)
        self.assertIn("Expulsados: **1**", k.response.edits[-1]["content"])

    def test_sin_albion_lists_discord_members_and_mentions_only_when_asked(self):
        self._gaps_setup()
        i = self.inter(self.alice)
        run(self.cog.sin_albion.callback(self.cog, i, False))
        self.assertIn("el Council o los administradores", i.text)
        i = self.inter(self.council)
        run(self.cog.sin_albion.callback(self.cog, i, False))
        sent = " ".join(str(m) for m in i.response.sent)
        self.assertIn(str(self.bob.id), sent)                                  # Bob tiene el rol y no está vinculado
        self.assertNotIn(str(self.robot.id), sent)                             # los bots no cuentan
        self.assertNotIn(str(self.alice.id), sent)                             # Alice ya está vinculada
        self.assertTrue(all(m[1].get("ephemeral") for m in i.response.sent))   # sin mencionar: privado
        i = self.inter(self.council)
        run(self.cog.sin_albion.callback(self.cog, i, True))
        pub = [m for m in i.response.sent if not m[1].get("ephemeral")]
        self.assertTrue(pub)                                                   # mencionando: público
        self.assertIn(f"<@{self.bob.id}>", pub[0][0])
        self.assertIn(self.bob.id, [u.id for u in pub[0][1]["allowed_mentions"].kw["users"]])

    def test_old_command_is_gone_and_new_ones_are_exempt(self):
        self.assertFalse(hasattr(self.cog, "listarregistros"))
        self.assertIn("sin-albion", rg.EXEMPT_COMMANDS)
        self.assertIn("sin-discord", rg.EXEMPT_COMMANDS)

    def _recruit_setup(self):
        self.r_main = discord.Role(7001, "Tank", self.guild)
        self.r_public = discord.Role(7002, "Public", self.guild)
        self.guild.roles += [self.r_main, self.r_public]
        self.cog.member_role_id, self.cog.public_role_id = self.r_member.id, self.r_public.id
        self.cog.albion_guild_id = "GID"
        self.newbie = discord.Member(555000000000000001, "Nuevo", roles=[self.r_public], guild=self.guild)
        self.guild.members.append(self.newbie)
        self.bot.guilds_map = {GUILD_ID: self.guild}
        self.members_in_guild = []

        async def fake(url, params=None):
            if "/guilds/GID/members" in url and "ams" not in url and "sgp" not in url:
                return self.members_in_guild
            return await self._search(url, params)

        self._search = self.cog.fetch_json
        self.cog.fetch_json = fake

    def test_guild_sin_discord_con_archivo(self):
        import types
        export = ('"Character Name"\t"Last Seen"\t"Roles"\n'
                  '"Daitza"\t"11/04/2026 01:34:00"\t"Officer;Siphoned;Recruiter;Attack perms;Council;Guild Master"\n'
                  '"Ramirinho74"\t"Online"\t""\n'
                  '"GLEACHECL"\t"Online"\t"Officer;Siphoned;Council"\n'
                  '"CraftAlt"\t"Online"\t""\n'
                  '"MatiPCK"\t"Online"\t"Siphoned;Recruiter\n')
        self.assertEqual(rg.parse_guild_export(export.encode("utf-16")), ["Daitza", "Ramirinho74", "GLEACHECL", "CraftAlt", "MatiPCK"])
        self.assertEqual(rg.parse_guild_export(export.encode("utf-8")), ["Daitza", "Ramirinho74", "GLEACHECL", "CraftAlt", "MatiPCK"])
        self.cog.member_role_id = self.r_member.id
        rdb.admin_link(self.db, self.alice.id, "Daitza", self.council.id)          # registrada y con Miembro → no aparece
        rdb.admin_link(self.db, self.outsider.id, "GLEACHECL", self.council.id)    # registrado sin Miembro → aviso aparte
        import alters_db
        alters_db.init_schema(self.db)
        alters_db.add_alter(self.db, self.alice.id, "CraftAlt", self.council.id)  # alter enlazado → no se lista

        async def read():
            return export.encode("utf-16")
        archivo = types.SimpleNamespace(size=len(export) * 2, filename="guild.txt", read=read)
        i = self.inter(self.alice)
        run(self.cog.guild_sin_discord.callback(self.cog, i, archivo))
        self.assertIn("el Council o los administradores", i.text)
        i = self.inter(self.council)
        run(self.cog.guild_sin_discord.callback(self.cog, i, archivo))
        out = i.text
        self.assertIn("sin Discord registrado (2)", out)
        self.assertIn("Ramirinho74", out); self.assertIn("MatiPCK", out)
        self.assertNotIn("• Daitza", out)
        self.assertIn(f"GLEACHECL → <@{self.outsider.id}>", out)
        self.assertIn("1 son alters", out)
        self.assertTrue(all(x[1].get("ephemeral") is False for x in i.response.sent))   # mensaje público

    def test_limpiar_miembros(self):
        self._recruit_setup()
        self.admin.roles.append(self.r_member)
        council_member = self.council; council_member.roles.append(self.r_member)
        rdb.admin_link(self.db, self.alice.id, "AliceMain", self.admin.id)            # alice enlazada → se queda
        i = self.inter(self.council)
        run(self.cog.limpiar_miembros.callback(self.cog, i, True))
        self.assertIn("administradores", i.text)                                        # Council no alcanza: solo Admin
        i = self.inter(self.admin)
        run(self.cog.limpiar_miembros.callback(self.cog, i, True))
        self.assertIn("**1** personas", i.text)
        self.assertIn(f"<@{self.bob.id}>", i.text)
        for m in (self.alice, self.admin, self.council, self.robot):
            self.assertNotIn(f"<@{m.id}>", i.text)
        self.assertIn(self.r_member, self.bob.roles)                                    # sin confirmar no cambia nada
        view = [x for x in i.response.sent if x[1].get("view") is not None][-1][1]["view"]
        self.assertFalse(run(view.interaction_check(self.inter(self.council))))
        j = self.inter(self.admin)

        async def eor(content=None, **kw):
            j.response.edits.append({"content": content})
        j.edit_original_response = eor
        run(view.children[0].callback(j))
        self.assertNotIn(self.r_member, self.bob.roles)
        self.assertIn(self.r_public, self.bob.roles)
        self.assertIn(self.r_member, self.alice.roles)
        self.assertIn("**1** personas", j.response.edits[-1]["content"])

    def test_recruit_accept_gives_roles_now_and_waits_confirmation(self):
        self._recruit_setup()
        ok, text, granted = run(self.cog.accept_recruit(self.newbie, "Forastero", self.r_main.id, "Tank", self.council))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn("Todavía no figura en la guild", text)
        self.assertEqual(rdb.get_registration(self.db, self.newbie.id).method, "recruitment")      # queda registrado
        self.assertIn(self.r_member, self.newbie.roles)                                            # y con roles al instante
        self.assertIn(self.r_main, self.newbie.roles)
        self.assertNotIn(self.r_public, self.newbie.roles)
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)                               # en la lista de confirmación
        run(self.cog.check_pending_recruits())                                                     # sigue sin figurar en la guild
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)
        self.members_in_guild = [{"Id": "id-forastero", "Name": "Forastero", "GuildName": "Smogg"}]  # ahora entró
        run(self.cog.check_pending_recruits())
        self.assertEqual(rdb.list_pending_recruits(self.db), [])

    def test_old_pending_recruit_without_roles_gets_them(self):
        self._recruit_setup()
        rdb.admin_link(self.db, self.newbie.id, "Forastero", self.council.id)
        rdb.add_pending_recruit(self.db, self.newbie.id, "Forastero", None, self.r_main.id, "Tank", self.council.id)
        run(self.cog.check_pending_recruits())
        self.assertIn(self.r_member, self.newbie.roles)
        self.assertIn(self.r_main, self.newbie.roles)
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)                               # sigue esperando confirmación

    def test_recruit_extra_roles_given_at_once(self):
        self._recruit_setup()
        extra = discord.Role(7003, "Observacion", self.guild)
        self.guild.roles.append(extra)
        ok, _t, granted = run(self.cog.accept_recruit(self.newbie, "Forastero", self.r_main.id, "Tank, Observación", self.council, (extra.id,)))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn(extra, self.newbie.roles)                                                    # Observación y CTA al instante
        self.assertIn(self.r_main, self.newbie.roles)
        self.assertIn(self.r_member, self.newbie.roles)

    def test_recruit_already_in_guild_gets_roles_immediately(self):
        self._recruit_setup()
        ok, _text, granted = run(self.cog.accept_recruit(self.newbie, "Cazador", self.r_main.id, "Tank", self.council))
        self.assertEqual((ok, granted), (True, True))
        self.assertIn(self.r_member, self.newbie.roles)
        self.assertEqual(rdb.list_pending_recruits(self.db), [])

    def test_recruit_with_unknown_nick_is_linked_by_hand(self):
        self._recruit_setup()
        ok, text, granted = run(self.cog.accept_recruit(self.newbie, "NoExiste", self.r_main.id, "Tank", self.council))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn("a mano", text)
        reg = rdb.get_registration(self.db, self.newbie.id)
        self.assertEqual((reg.char_name, reg.method, reg.char_id), ("NoExiste", "admin_override", None))   # como /admin_vincular, sin ID
        self.assertIn(self.r_member, self.newbie.roles)                                                     # roles al instante
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)

    def test_recruit_with_api_down_is_linked_by_hand(self):
        self._recruit_setup()

        async def caida(url, params=None):
            raise RuntimeError("API caída")
        self.cog.fetch_json = caida
        self.cog.roster = {}
        ok, text, granted = run(self.cog.accept_recruit(self.newbie, "Forastero", self.r_main.id, "Tank", self.council))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn("no respondió", text)
        self.assertEqual(rdb.get_registration(self.db, self.newbie.id).method, "admin_override")
        self.assertEqual(len(rdb.list_pending_recruits(self.db)), 1)                                # queda para confirmar el ingreso
        self.assertIn(self.r_member, self.newbie.roles)

    def test_recruit_with_homonyms_is_linked_by_hand(self):
        self._recruit_setup()
        ambiguo = rg.Lookup("ambiguous", options=[rg.Character("a", "Gemelo", "Américas", "Otra", "", 0, 0), rg.Character("b", "Gemelo", "Europa", "Otra2", "", 0, 0)])

        async def fake_lookup(name, any_guild=False, live=False):
            return ambiguo
        self.cog.lookup_character = fake_lookup
        ok, text, granted = run(self.cog.accept_recruit(self.newbie, "Gemelo", self.r_main.id, "Tank", self.council))
        self.assertEqual((ok, granted), (True, False))
        self.assertIn("varios personajes", text)
        reg = rdb.get_registration(self.db, self.newbie.id)
        self.assertEqual((reg.char_name, reg.method, reg.char_id), ("Gemelo", "admin_override", None))

    def test_recruit_with_taken_nick_is_not_accepted(self):
        self._recruit_setup()
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1, char_id="id-cazador")
        ok, _text, _ = run(self.cog.accept_recruit(self.newbie, "Cazador", self.r_main.id, "Tank", self.council))
        self.assertFalse(ok)                                                                       # ya es de otra cuenta
        self.assertNotIn(self.r_member, self.newbie.roles)

    def test_stale_roster_falls_back_to_live_search(self):
        self.cog.roster = {"cazador": rg.Character("x", "Cazador", "Américas", "Smogg", "", 0, 0)}
        self.cog.roster_at = -10 ** 9
        self.assertFalse(self.cog.roster_fresh())

    def test_stale_roster_is_used_when_the_api_is_down(self):
        self.cog.roster = {"cazador": rg.Character("x", "Cazador", "Américas", "Smogg", "", 0, 0)}
        self.cog.roster_at = -10 ** 9                                       # lista de hace muchísimo
        self.assertFalse(self.cog.roster_fresh())

        async def down(url, params=None):
            raise RuntimeError("HTTP 502")
        self.cog.fetch_json = down
        look = run(self.cog.lookup_character("Cazador"))
        self.assertEqual((look.kind, look.character.name), ("ok", "Cazador"))   # sigue sirviendo la guardada
        self.assertEqual(run(self.cog.lookup_character("Nuevito")).kind, "error")  # el que no figura: error claro, no "no encontrado"

    def test_fresh_roster_miss_with_api_down_is_an_error_not_not_in_guild(self):
        import time
        self.cog.albion_guild_id = "GID"
        self.cog.roster = {"cazador": rg.Character("x", "Cazador", "Américas", "Smogg", "", 0, 0)}
        self.cog.roster_at = time.time()
        self.cog.roster_miss_at = 0

        async def down(url, params=None):
            raise RuntimeError("HTTP 502")
        self.cog.fetch_json = down
        self.assertEqual(run(self.cog.lookup_character("Nuevito")).kind, "error")

    def register(self, user, name, target=None, channel=None):
        i = self.inter(user, channel)
        run(self.cog.registrar.callback(self.cog, i, name, target))
        return i

    def press(self, view, label_start, user, message=None):
        btn = [b for b in view.children if b.label.startswith(label_start)][0]
        i = self.inter(user, message=message)
        run(btn.callback(i))
        return i

    def test_self_registration_flow_sets_nickname(self):
        i = self.register(self.alice, "cazador")                               # tolerante a mayúsculas, guarda el nombre exacto
        emb = i.response.sent[-1][1]["embed"]
        fields = {f[0]: f[1] for f in emb.fields}
        self.assertIn("Cazador", fields["Nombre exacto"])
        self.assertEqual(fields["Guild"], "Smogg")
        self.assertFalse(rdb.is_registered(self.db, self.alice.id))            # todavía nada guardado
        j = self.press(i.response.sent[-1][1]["view"], "Es mi personaje", self.alice)
        reg = rdb.get_registrations(self.db, self.alice.id)[0]
        self.assertEqual((reg.char_name, reg.char_id, reg.guild_name, reg.region), ("Cazador", "id-cazador", "Smogg", "Américas"))
        self.assertEqual(self.alice.nick, "Cazador")
        self.assertIn("Cazador", j.response.edits[0]["content"])
        self.assertTrue(any("Registro #" in m.content for m in self.logs.sent))

    def test_one_account_cannot_register_a_second_character(self):
        i = self.register(self.alice, "Cazador")
        self.press(i.response.sent[-1][1]["view"], "Es mi personaje", self.alice)
        j = self.register(self.alice, "Dos")
        self.assertIn("ya tiene un personaje", j.text)
        self.assertEqual([r.char_name for r in rdb.get_registrations(self.db, self.alice.id)], ["Cazador"])
        i = self.inter(self.alice)
        run(self.cog.mi_registro.callback(self.cog, i))
        self.assertEqual([f[0] for f in i.response.sent[0][1]["embed"].fields], ["Cazador"])

    def test_registering_someone_else_requires_their_own_acceptance(self):
        self.register(self.alice, "Cazador", target=self.bob)
        prompt = self.panelch.sent[-1]
        self.assertIn(str(self.bob.id), prompt.content)
        self.assertIn("Solo vos", prompt.content)
        j = self.press(prompt.view, "Es mi personaje", self.alice, prompt)      # quien inició NO puede aceptar por Bob
        self.assertIn("no es para vos", j.text)
        j = self.press(prompt.view, "No, cancelar", self.admin, prompt)         # ni siquiera un admin
        self.assertIn("no es para vos", j.text)
        self.assertFalse(rdb.is_registered(self.db, self.bob.id))
        self.assertIsNone(self.bob.nick)
        self.assertEqual(len(rdb.pending_requests_for(self.db, self.bob.id)), 1)
        self.press(prompt.view, "Es mi personaje", self.bob, prompt)            # Bob sí
        self.assertEqual(rdb.get_registrations(self.db, self.bob.id)[0].char_name, "Cazador")
        self.assertEqual(self.bob.nick, "Cazador")
        self.assertIsNone(self.alice.nick)                                      # el apodo de quien inició no se toca

    def test_declined_request_changes_nothing(self):
        self.register(self.alice, "Cazador", target=self.bob)
        prompt = self.panelch.sent[-1]
        self.press(prompt.view, "No, cancelar", self.bob, prompt)
        self.assertFalse(rdb.is_registered(self.db, self.bob.id))
        self.assertIsNone(self.bob.nick)
        self.assertEqual(rdb.pending_requests_for(self.db, self.bob.id), [])

    def test_unknown_wrong_guild_and_api_down(self):
        self.assertIn("No encontré", self.register(self.alice, "Fantasma").text)
        self.assertIn("no figura en la guild", self.register(self.alice, "Forastero").text)
        self.assertEqual(rdb.pending_requests_for(self.db, self.alice.id), [])

        async def down(url, params=None):
            raise RuntimeError("HTTP 503")
        self.cog.fetch_json = down
        self.assertIn("No pude consultar", self.register(self.alice, "Cazador").text)
        self.assertEqual(rdb.pending_requests_for(self.db, self.alice.id), [])

    def test_character_already_linked_to_another_discord_is_refused_without_details(self):
        i = self.register(self.alice, "Cazador")
        self.press(i.response.sent[-1][1]["view"], "Es mi personaje", self.alice)
        i = self.register(self.bob, "Cazador")
        self.assertIn("ya está vinculado a otra cuenta", i.text)
        self.assertNotIn("Alice", i.text)
        self.assertNotIn(str(self.alice.id), i.text)
        self.assertFalse(rdb.is_registered(self.db, self.bob.id))

    def test_buttons_survive_a_restart(self):
        self.register(self.alice, "Cazador", target=self.bob)
        prompt = self.panelch.sent[-1]
        self.assertIn("TargetView", {type(v).__name__ for v in self.bot.persistent_views})
        fresh = rg.TargetView(self.cog)                                         # "reinicio": todo sale del id del mensaje + la base de datos
        self.press(fresh, "Es mi personaje", self.bob, prompt)
        self.assertTrue(rdb.is_registered(self.db, self.bob.id))

    def test_sincronizar_apodos(self):
        run(self.cog.admin_vincular.callback(self.cog, self.inter(self.council), self.bob, "Cazador"))
        self.bob.nick = "Otro"
        i = self.inter(self.alice)
        run(self.cog.sincronizar_apodos.callback(self.cog, i, False))
        self.assertIn("Solo el Staff", i.text)
        self.assertEqual(self.bob.nick, "Otro")
        i = self.inter(self.council)
        run(self.cog.sincronizar_apodos.callback(self.cog, i, True))      # solo ver
        self.assertEqual(self.bob.nick, "Otro")
        self.assertIn("Cazador", i.text)
        i = self.inter(self.council)
        run(self.cog.sincronizar_apodos.callback(self.cog, i, False))
        self.assertEqual(self.bob.nick, "Cazador")
        i = self.inter(self.council)
        run(self.cog.sincronizar_apodos.callback(self.cog, i, False))
        self.assertIn("Corregidos: **0**", i.text)

    def test_sincronizar_registros(self):
        look = run(self.cog.lookup_character("Cazador", live=True))
        self.assertEqual(look.kind, "ok")
        rdb.admin_link(self.db, self.bob.id, "Cazador", self.council.id)          # vínculo solo con nombre
        rdb.admin_link(self.db, self.alice.id, "NoExiste", self.council.id)
        i = self.inter(self.alice)
        run(self.cog.sincronizar_registros.callback(self.cog, i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.council)
        run(self.cog.sincronizar_registros.callback(self.cog, i))
        reg = rdb.get_registrations(self.db, self.bob.id)[0]
        self.assertEqual(reg.char_id, "id-cazador")
        self.assertIn("No pude verificar", i.text)
        self.assertIn("NoExiste", i.text)
        self.assertIsNone(rdb.get_registrations(self.db, self.alice.id)[0].char_id)
        i = self.inter(self.council)
        run(self.cog.sincronizar_registros.callback(self.cog, i))
        self.assertIn("NoExiste", i.text)                                          # sigue pendiente

    def test_nickname_failures_keep_the_registration(self):
        self.guild.me.guild_permissions.manage_nicknames = False
        self.assertIn("Gestionar apodos", run(self.cog.apply_nickname(self.alice, "Cazador")))
        self.assertIsNone(self.alice.nick)
        self.guild.me.guild_permissions.manage_nicknames = True
        self.guild.forbid_nick = True
        self.assertIn("Discord me lo impidió", run(self.cog.apply_nickname(self.alice, "Cazador")))
        owner = discord.Member(1, "Dueño", guild=self.guild)
        self.assertIn("dueño", run(self.cog.apply_nickname(owner, "Cazador")))
        i = self.register(self.alice, "Cazador")
        j = self.press(i.response.sent[-1][1]["view"], "Es mi personaje", self.alice)
        self.assertIn("No pude cambiarte el apodo", j.response.edits[0]["content"])
        self.assertTrue(rdb.is_registered(self.db, self.alice.id))

    def test_desvincular_asks_for_confirmation(self):
        self.press(self.register(self.council, "Cazador").response.sent[-1][1]["view"], "Es mi personaje", self.council)
        i = self.inter(self.council)
        run(self.cog.desvincular.callback(self.cog, i))
        view = i.response.sent[0][1]["view"]
        self.assertFalse(run(view.interaction_check(self.inter(self.bob))))     # otro no puede pulsar
        self.assertTrue(rdb.is_registered(self.db, self.council.id))
        run(view.children[0].callback(self.inter(self.council)))
        self.assertFalse(rdb.is_registered(self.db, self.council.id))

    def test_desvincular_is_council_only(self):
        self.press(self.register(self.alice, "Cazador").response.sent[-1][1]["view"], "Es mi personaje", self.alice)
        i = self.inter(self.alice)
        run(self.cog.desvincular.callback(self.cog, i))
        self.assertIn("el Council o los administradores", i.text)
        self.assertTrue(rdb.is_registered(self.db, self.alice.id))

    def test_mi_registro_without_registration(self):
        i = self.inter(self.alice)
        run(self.cog.mi_registro.callback(self.cog, i))
        self.assertIn("Todavía no estás registrado", i.text)

    def test_admin_commands_are_protected_and_audited(self):
        for cmd, args in ((self.cog.admin_registro, (self.bob,)), (self.cog.admin_desvincular, (self.bob, None)),
                          (self.cog.admin_vincular, (self.bob, "Cazador"))):
            i = self.inter(self.alice)
            run(cmd.callback(self.cog, i, *args))
            self.assertIn("el Council o los administradores", i.text)
        self.assertFalse(rdb.is_registered(self.db, self.bob.id))
        i = self.inter(self.council)
        run(self.cog.admin_vincular.callback(self.cog, i, self.bob, "Cazador"))
        reg = rdb.get_registrations(self.db, self.bob.id)[0]
        self.assertEqual((reg.method, reg.char_id), ("admin_override", "id-cazador"))
        self.assertIn("vínculo administrativo", i.text)
        self.assertTrue(any("a mano" in m.content for m in self.logs.sent))
        self.assertNotIn("se lo di", i.text)                                   # bob ya tenía Miembro
        self.cog.member_role_id = self.r_member.id
        rdb.unlink(self.db, self.bob.id, self.council.id)
        self.bob.roles.remove(self.r_member)
        i = self.inter(self.council)
        run(self.cog.admin_vincular.callback(self.cog, i, self.bob, "Cazador"))
        self.assertIn(self.r_member, self.bob.roles)                           # no lo tenía: se lo da
        self.assertIn("se lo di", i.text)
        i = self.inter(self.council)
        run(self.cog.admin_registro.callback(self.cog, i, self.bob))
        self.assertIn("vinculado por un administrador", str(i.response.sent[0][1]["embed"].fields))
        i = self.inter(self.council)
        run(self.cog.admin_desvincular.callback(self.cog, i, self.bob, "error"))
        run(i.response.sent[0][1]["view"].children[0].callback(self.inter(self.council)))
        self.assertFalse(rdb.is_registered(self.db, self.bob.id))

    def test_leaving_the_server_cancels_requests_and_frees_characters(self):
        self.register(self.alice, "Cazador", target=self.bob)
        run(self.cog.on_member_remove(self.bob))
        self.assertEqual(rdb.pending_requests_for(self.db, self.bob.id), [])
        rdb.admin_link(self.db, self.alice.id, "Dos", 1)
        run(self.cog.on_member_remove(self.alice))
        self.assertFalse(rdb.is_registered(self.db, self.alice.id))

    def test_left_members_keep_last_five_and_can_be_restored(self):
        for n in range(1, 7):                                                       # el 6.º que se va pisa al 1.º
            rdb.admin_link(self.db, 1000 + n, f"Char{n}", 1, char_id=f"id{n}")
            rdb.save_left_member(self.db, 1000 + n, name=f"u{n}", nick=None, roles=[(5, "Tank")], regs=rdb.get_registrations(self.db, 1000 + n))
        saved = rdb.list_left_members(self.db)
        self.assertEqual([m["discord_id"] for m in saved], [1006, 1005, 1004, 1003, 1002])
        rdb.save_left_member(self.db, 2000, name="sin registro", nick=None, roles=[], regs=[])      # sin registro no se guarda
        self.assertEqual(len(rdb.list_left_members(self.db)), 5)
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1, char_id="id-cazador")
        r_tank = discord.Role(7101, "Tank", self.guild)
        self.guild.roles.append(r_tank)
        self.alice.roles.append(r_tank)
        run(self.cog.on_member_remove(self.alice))                                   # se va: queda guardado y se libera el personaje
        self.assertFalse(rdb.is_registered(self.db, self.alice.id))
        self.alice.roles.remove(r_tank)
        i = self.inter(self.council)
        run(self.cog.recuperar_lista.callback(self.cog, i))
        self.assertIn("Cazador", i.text)
        i = self.inter(self.bob)
        run(self.cog.recuperar.callback(self.cog, i, self.alice))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.council)
        run(self.cog.recuperar.callback(self.cog, i, self.admin))                   # no está entre los guardados
        self.assertIn("no está entre los 5", i.text)
        self.guild.members.remove(self.alice)                                       # todavía no volvió
        i = self.inter(self.council)
        run(self.cog.recuperar.callback(self.cog, i, self.alice))
        self.assertIn("todavía no volvió", i.text)
        self.assertFalse(rdb.is_registered(self.db, self.alice.id))
        self.guild.members.append(self.alice)                                       # vuelve
        i = self.inter(self.council)
        run(self.cog.recuperar.callback(self.cog, i, self.alice))
        self.assertEqual(rdb.get_registration(self.db, self.alice.id).char_name, "Cazador")
        self.assertIn(r_tank, self.alice.roles)
        self.assertEqual([m["discord_id"] for m in rdb.list_left_members(self.db) if m["discord_id"] == self.alice.id], [])

    def test_estado_api_admin_only_and_reports_down_and_up(self):
        i = self.inter(self.alice)
        run(self.cog.estado_api.callback(self.cog, i))
        self.assertIn("administradores", i.text)

        async def down(url, params=None):
            raise RuntimeError("HTTP 503")
        self.cog.fetch_json = down
        self.admin.guild_permissions.administrator = True
        i = self.inter(self.admin)
        run(self.cog.estado_api.callback(self.cog, i))
        self.assertIn("no está respondiendo", i.text)
        self.assertIn("🔴 **Américas**", i.text)

        async def up(url, params=None):
            return {"players": []}
        self.cog.fetch_json = up
        i = self.inter(self.admin)
        run(self.cog.estado_api.callback(self.cog, i))
        self.assertIn("🟢 **Europa**", i.text)
        self.assertIn("La API responde", i.text)


class GateTests(DiscordBase):
    def setUp(self):
        super().setUp()
        self.cog = run(rg.setup_registration(self.bot, self.db, GUILD_ID))
        self.gate = self.bot.registration_gate

    def cmd_inter(self, user, name):
        i = self.inter(user)
        i.command = type("Cmd", (), {"qualified_name": name, "name": name})()
        return i

    def check(self, user, name):
        i = self.cmd_inter(user, name)
        return run(self.bot.tree.interaction_check(i)), i

    def test_on_by_default_unregistered_members_are_blocked(self):
        ok, _ = self.check(self.alice, "bal")
        self.assertFalse(ok)

    def enable(self):
        rdb.set_flag(self.db, rdb.REQUIRED_KEY, None)
        self.gate.invalidate()

    def test_when_enabled_unregistered_members_get_a_private_hint(self):
        self.enable()
        ok, i = self.check(self.alice, "bal")
        self.assertFalse(ok)
        self.assertIn("/registrar", i.text)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))

    def test_registered_members_pass_and_registration_does_not_grant_admin(self):
        self.enable()
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1)
        self.assertTrue(self.check(self.alice, "bal")[0])
        # los comandos de staff conservan SUS propias restricciones (el registro no las abre)
        i = self.inter(self.alice)
        run(self.cog.admin_desvincular.callback(self.cog, i, self.bob))
        self.assertIn("el Council o los administradores", i.text)

    def test_registration_commands_admins_and_bots_are_exempt(self):
        self.enable()
        for name in ("registrar", "mi_registro", "desvincular", "admin_vincular", "registro_obligatorio"):
            self.assertTrue(self.check(self.alice, name)[0], name)
        self.assertTrue(self.check(self.admin, "bal")[0])                      # administradores nunca quedan fuera
        self.assertTrue(self.check(self.robot, "bal")[0])                      # ni los procesos/bots

    def test_prefix_commands_are_gated_too(self):
        self.enable()

        class Ctx:
            def __init__(self, author, name):
                self.author = author
                self.command = type("C", (), {"qualified_name": name})()
                self.replies = []

            async def reply(self, text, **kw):
                self.replies.append(text)

        ctx = Ctx(self.alice, "bal")
        with self.assertRaises(rg.RegistrationRequired):
            run(self.bot.checks[0](ctx))
        self.assertIn("/registrar", ctx.replies[0])
        self.assertTrue(run(self.bot.checks[0](Ctx(self.admin, "bal"))))
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1)
        self.assertTrue(run(self.bot.checks[0](Ctx(self.alice, "bal"))))

    def test_toggle_command_is_admin_only_and_takes_effect(self):
        choice = lambda v: discord.app_commands.Choice(v, v)  # noqa: E731
        i = self.inter(self.alice)
        run(self.cog.registro_obligatorio.callback(self.cog, i, choice("activar")))
        self.assertIn("el Council o los administradores", i.text)
        self.assertFalse(self.check(self.alice, "bal")[0])                     # sigue exigido
        run(self.cog.registro_obligatorio.callback(self.cog, self.inter(self.council), choice("desactivar")))
        self.assertTrue(self.check(self.alice, "bal")[0])
        run(self.cog.registro_obligatorio.callback(self.cog, self.inter(self.council), choice("activar")))
        self.assertFalse(self.check(self.alice, "bal")[0])

    def test_helper_for_new_commands(self):
        self.assertFalse(run(rg.require_registered_user(self.db, self.alice)))
        rdb.admin_link(self.db, self.alice.id, "Cazador", 1)
        self.assertTrue(run(rg.require_registered_user(self.db, self.alice)))


if __name__ == "__main__":
    unittest.main()
