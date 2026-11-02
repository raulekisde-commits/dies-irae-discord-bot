import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase, run, STAFF_ROLE, GUILD_ID  # noqa: E402
import types
import discord  # noqa: E402
import invitados as iv  # noqa: E402
import registration_db as rdb  # noqa: E402


class InvBase(DiscordBase):
    def setUp(self):
        super().setUp()
        g = self.guild
        self.r_legacy = discord.Role(iv.LEGACY_ROLE_ID, "Legacy", g)
        self.r_guest = discord.Role(iv.GUEST_ROLE_ID, "Invitado", g)
        self.r_public = discord.Role(iv.PUBLIC_ROLE_ID, "Public", g)
        self.r_staff = discord.Role(iv.STAFF_ROLE_ID, "Staff", g)
        g.roles += [self.r_legacy, self.r_guest, self.r_public, self.r_staff]
        self.r_member.id = iv.MIEMBRO_ROLE_ID if self.r_member.id != iv.MIEMBRO_ROLE_ID else self.r_member.id
        def mk(uid, name, roles):
            m = discord.Member(uid, name, roles=list(roles), guild=g)
            g.members.append(m)
            return m
        self.leg = mk(7001, "Leg", [self.r_member, self.r_legacy])
        self.newbie = mk(7002, "Nuevo", [self.r_public])
        self.stf = mk(7003, "Stf", [self.r_staff])
        rdb.init_schema(self.db)
        char = types.SimpleNamespace(name="AmigoPro", region="eu", guild="Otra", char_id="abc", alliance="", kill_fame=1, death_fame=2)
        self.lookups = []

        class FakeReg:
            async def lookup_character(inner, name, any_guild=False, live=False):
                self.lookups.append((name, any_guild))
                if name == "NoExiste":
                    return types.SimpleNamespace(kind="none", character=None)
                return types.SimpleNamespace(kind="ok", character=char)

            def character_embed(inner, c, title=""):
                return discord.Embed(title=title)

            async def apply_nickname(inner, member, name):
                member.nick = name
                return "✅ apodo"
        self.bot.cogs["RegistrationCog"] = FakeReg()
        self.cog = run(iv.setup_invitados(self.bot, self.db, GUILD_ID))

    def inv(self, who, target, why="amigo de Leg", char="AmigoPro", confirm=True):
        i = self.inter(who)
        run(self.cog.invitar.callback(self.cog, i, target, why, char))
        sent = [x for x in i.response.sent if x[1].get("view") is not None]
        if confirm and sent:                                   # el invitador pulsa «Sí, es mi amigo»
            view = sent[-1][1]["view"]
            j = self.inter(who)

            async def eor(content=None, **kw):
                j.response.edits.append({"content": content})
            j.edit_original_response = eor
            run(view.children[0].callback(j))
            i.confirm = j
            i.confirmed_text = "\n".join(str(e.get("content")) for e in j.response.edits)
            i.view = view
        return i


class Tests(InvBase):
    def test_only_legacy(self):
        self.assertIn("Legacy", self.inv(self.alice, self.newbie).text)
        self.assertNotIn(iv.GUEST_ROLE_ID, [r.id for r in self.newbie.roles])

    def test_invite_swaps_roles_and_records(self):
        i = self.inv(self.leg, self.newbie)
        ids = [r.id for r in self.newbie.roles]
        self.assertIn(iv.GUEST_ROLE_ID, ids)
        self.assertNotIn(iv.PUBLIC_ROLE_ID, ids)
        g = iv.get_guest(self.db, GUILD_ID, self.newbie.id)
        self.assertEqual((g["invited_by"], g["reason"]), (self.leg.id, "amigo de Leg"))
        self.assertIn("ahora es invitado", i.confirmed_text)
        reg = rdb.get_registration(self.db, self.newbie.id)
        self.assertEqual((reg.char_name, reg.method), ("AmigoPro", "guest_invite"))      # quedó registrado como con /registrar
        self.assertEqual(self.lookups[-1], ("AmigoPro", True))
        self.assertIn("ya es invitado", self.inv(self.leg, self.newbie).text)

    def test_needs_confirmation_and_only_inviter_confirms(self):
        i = self.inv(self.leg, self.newbie, confirm=False)
        self.assertNotIn(iv.GUEST_ROLE_ID, [r.id for r in self.newbie.roles])          # sin confirmar no pasa nada
        self.assertIsNone(rdb.get_registration(self.db, self.newbie.id))
        view = [x for x in i.response.sent if x[1].get("view") is not None][-1][1]["view"]
        self.assertFalse(run(view.interaction_check(self.inter(self.alice))))          # otro no puede confirmar
        run(view.children[1].callback(self.inter(self.leg)))                           # «No, cancelar»
        self.assertNotIn(iv.GUEST_ROLE_ID, [r.id for r in self.newbie.roles])

    def test_unknown_character_blocks(self):
        i = self.inv(self.leg, self.newbie, char="NoExiste", confirm=False)
        self.assertIn("No encontré", i.text)
        self.assertNotIn(iv.GUEST_ROLE_ID, [r.id for r in self.newbie.roles])

    def test_character_already_linked_blocks(self):
        rdb.admin_link(self.db, self.alice.id, "AmigoPro", 1)
        i = self.inv(self.leg, self.newbie)
        self.assertIn("ya está vinculado", i.confirmed_text)
        self.assertNotIn(iv.GUEST_ROLE_ID, [r.id for r in self.newbie.roles])

    def test_cannot_invite_member_bot_or_self(self):
        self.assertIn("ya es miembro", self.inv(self.leg, self.bob).text)
        self.assertIn("No podés", self.inv(self.leg, self.leg).text)
        self.assertIn("No podés", self.inv(self.leg, self.robot).text)
        self.assertIn("Tenés que indicar", self.inv(self.leg, self.newbie, "   ").text)

    def test_quien_invito_and_list(self):
        self.inv(self.leg, self.newbie)
        i = self.inter(self.alice)
        run(self.cog.quien_invito.callback(self.cog, i, self.newbie))
        self.assertIn(f"<@{self.leg.id}>", i.text); self.assertIn("amigo de Leg", i.text)
        i = self.inter(self.outsider)
        run(self.cog.quien_invito.callback(self.cog, i, self.newbie))
        self.assertIn("Solo los miembros", i.text)
        i = self.inter(self.alice)
        run(self.cog.invitados.callback(self.cog, i))
        self.assertIn("Solo el Staff", i.text)
        i = self.inter(self.stf)
        run(self.cog.invitados.callback(self.cog, i))
        self.assertIn(f"<@{self.newbie.id}>", i.text)

    def test_guests_get_economy_but_not_casino(self):
        self.assertIn(iv.GUEST_ROLE_ID, self.ecog.default_roles["bal"])
        self.assertIn(iv.GUEST_ROLE_ID, self.ecog.default_roles["top"])
        self.assertNotIn(iv.GUEST_ROLE_ID, self.ecog.default_roles.get("1v1", set()))
        import memberguard
        self.inv(self.leg, self.newbie)
        self.assertIsNone(run(memberguard.button_block_reason(self.bot, self.newbie)))        # el casino se habilita aparte con /eco perms-comando
        self.assertIn("miembros", run(memberguard.button_block_reason(self.bot, self.outsider)))

    def test_info(self):
        i = self.inter(self.alice)
        run(self.cog.info.callback(self.cog, i, discord.app_commands.Choice(name="invitados", value="invitados")))
        self.assertTrue(i.response.sent)

    def test_info_is_user_only_and_info_staff_adds_team_part(self):
        for topic in iv.INFO_TEXTS:
            user_txt = iv.INFO_TEXTS[topic][1]
            for staff_only in ("/casino ban", "/eco perms-canal", "/warning ", "/invitados`", "/panel coaching", "/eco addmoney"):
                self.assertNotIn(staff_only, user_txt, f"{topic}: {staff_only} no va en /info")
            self.assertIn(topic, iv.INFO_STAFF)
        self.assertIn("/casino winrate", iv.INFO_TEXTS["casino"][1])
        self.assertNotIn("/winrate", iv.INFO_TEXTS["casino"][1].replace("/casino winrate", ""))
        emb_u = iv.InvitadosCog.info_embed("casino", False)
        emb_s = iv.InvitadosCog.info_embed("casino", True)
        self.assertNotIn("Para el equipo", emb_u.description)
        self.assertIn("Para el equipo", emb_s.description)
        self.assertIn("/casino ban", emb_s.description)
        self.assertIn(iv.INFO_TEXTS["casino"][1], emb_s.description)
        for topic in iv.INFO_TEXTS:
            self.assertLess(len(iv.InvitadosCog.info_embed(topic, True).description), 4000)

    def test_info_topics_all_have_both_versions_and_new_facts(self):
        names = {c.value for c in iv.InvitadosCog.INFO_CHOICES}
        self.assertEqual(names, set(iv.INFO_TEXTS))
        self.assertEqual(names, set(iv.INFO_STAFF))
        for need in ("splits", "zvz", "reclutamiento", "registro", "timers", "stats"):
            self.assertIn(need, names)
        sp = iv.INFO_TEXTS["splits"][1]
        for frag in ("5%", "20%", "15%", "10%", "1558630638066602094", "Staff decide"):
            self.assertIn(frag, sp)
        self.assertIn("/split-ava", iv.INFO_STAFF["splits"])
        self.assertIn("/split ", iv.INFO_STAFF["splits"])
        self.assertNotIn("transparencia", "".join(t[1] for t in iv.INFO_TEXTS.values()) + "".join(iv.INFO_STAFF.values()))
        self.assertIn("10%", iv.INFO_TEXTS["economia"][1])
        self.assertIn("Ver mi build", iv.INFO_TEXTS["zvz"][1])
        self.assertIn("/cta ", iv.INFO_STAFF["zvz"])
        self.assertIn("/casino winrate", iv.INFO_TEXTS["stats"][1])
        self.assertIn("/killboard", iv.INFO_STAFF["stats"])

    def test_split_commands_renamed(self):
        import splits as sp
        self.assertEqual(sp.SplitsCog.transparencia.name, "split")
        self.assertEqual(sp.SplitsCog.transparencia_ava.name, "split-ava")

    def test_info_staff_permissions(self):
        ch = discord.app_commands.Choice(name="casino", value="casino")
        i = self.inter(self.alice)
        run(self.cog.info_staff.callback(self.cog, i, ch))
        self.assertIn("Solo el Staff", i.text)
        i2 = self.inter(self.stf)
        run(self.cog.info_staff.callback(self.cog, i2, ch))
        self.assertTrue(i2.response.sent)
        self.assertNotIn("Solo el Staff", i2.text or "")


if __name__ == "__main__":
    unittest.main()
