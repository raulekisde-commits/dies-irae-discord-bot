import asyncio
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase  # noqa: E402,F401  (instala el stub de discord)
import expulsar as ex  # noqa: E402
import registration as rg  # noqa: E402
import guildshop as gs  # noqa: E402

OWNER = 1


def role(rid, pos):
    return types.SimpleNamespace(id=rid, position=pos)


def member(uid, pos, roles=(), admin=False, bot=False, kick=False):
    r = [role(r_id, pos) for r_id in roles]
    return types.SimpleNamespace(id=uid, bot=bot, roles=r, top_role=role(900 + uid, pos), guild=None, display_name=f"u{uid}",
                                 guild_permissions=types.SimpleNamespace(administrator=admin, kick_members=kick))


def world(bot_pos=50):
    me = member(99, bot_pos, kick=True)
    guild = types.SimpleNamespace(owner_id=OWNER, me=me)
    return guild


class CheckTargetTests(unittest.TestCase):
    def setUp(self):
        self.guild = world()
        self.staff = member(10, 30, roles=(rg.STAFF_ROLE_ID,))
        self.council = member(11, 35, roles=(gs.COUNCIL_ROLE_ID,))
        self.admin = member(12, 40, admin=True)
        self.plain = member(20, 5)

    def test_staff_puede_expulsar_a_un_miembro_comun(self):
        self.assertIsNone(ex.check_target(self.staff, self.plain, self.guild))

    def test_un_miembro_comun_no_puede_expulsar(self):
        self.assertIn("Solo el Staff", ex.check_target(self.plain, member(21, 1), self.guild))

    def test_no_a_uno_mismo_ni_bots_ni_dueno_ni_al_propio_bot(self):
        self.assertIn("vos mismo", ex.check_target(self.staff, self.staff, self.guild))
        self.assertIn("bot", ex.check_target(self.staff, member(30, 1, bot=True), self.guild))
        self.assertIn("dueño", ex.check_target(self.staff, member(OWNER, 1), self.guild))
        self.assertIn("mí mismo", ex.check_target(self.staff, self.guild.me, self.guild))

    def test_el_staff_no_puede_expulsar_a_staff_ni_council_pero_un_admin_si(self):
        otro_staff = member(13, 10, roles=(rg.STAFF_ROLE_ID,))
        self.assertIn("administrador", ex.check_target(self.staff, otro_staff, self.guild))
        self.assertIn("administrador", ex.check_target(self.staff, member(14, 1, roles=(gs.COUNCIL_ROLE_ID,)), self.guild))
        self.assertIsNone(ex.check_target(self.admin, otro_staff, self.guild))

    def test_jerarquia_de_roles(self):
        self.assertIn("igual o más alto", ex.check_target(self.staff, member(22, 30), self.guild))
        self.assertIn("igual o más alto", ex.check_target(self.staff, member(23, 31), self.guild))

    def test_el_bot_necesita_permiso_y_rol_mas_alto(self):
        self.guild.me.guild_permissions.kick_members = False
        self.assertIn("Expulsar miembros", ex.check_target(self.staff, self.plain, self.guild))
        self.guild.me.guild_permissions.kick_members = True
        self.assertIsNone(ex.check_target(self.staff, self.plain, self.guild))
        self.assertIn("rol está igual o por encima del mío", ex.check_target(member(12, 60, admin=True), member(24, 50), world(bot_pos=50)))


class Resp:
    def __init__(self):
        self.done, self.sent, self.edited = False, [], []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))

    async def edit_message(self, content=None, **kw):
        self.done = True
        self.edited.append((content, kw))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.audits, self.kicked = [], []
        outer = self

        class Reg:
            async def audit_log(self, text):
                outer.audits.append(text)
        self.guild = world()
        self.target = member(20, 5)
        self.target.kick = self._kick
        self.guild.get_member = lambda uid: self.target if uid == 20 else None
        bot = types.SimpleNamespace(get_cog=lambda name: Reg() if name == "RegistrationCog" else None)
        self.cog = ex.ExpulsarCog(bot, 123)
        self.actor = member(10, 30, roles=(rg.STAFF_ROLE_ID,))

    async def _kick(self, reason=None):
        self.kicked.append(reason)

    def inter(self):
        return types.SimpleNamespace(guild=self.guild, user=self.actor, response=Resp(),
                                     followup=types.SimpleNamespace(send=self._noop))

    async def _noop(self, *a, **k):
        pass

    def test_confirmar_expulsa_y_deja_registro(self):
        i = self.inter()
        asyncio.run(self.cog.confirmed(i, 10, 20, "spam"))
        self.assertEqual(len(self.kicked), 1)
        self.assertIn("spam", self.kicked[0])
        self.assertIn("fue expulsado", i.response.edited[0][0])
        self.assertTrue(any("expulsó" in a and "spam" in a for a in self.audits))

    def test_al_confirmar_se_revalida(self):
        self.target.top_role = role(1, 31)                           # mientras tanto subió de rol
        i = self.inter()
        asyncio.run(self.cog.confirmed(i, 10, 20, ""))
        self.assertEqual(self.kicked, [])
        self.assertIn("igual o más alto", i.response.edited[0][0])

    def test_si_ya_no_esta_no_hace_nada(self):
        i = self.inter()
        asyncio.run(self.cog.confirmed(i, 10, 77, ""))
        self.assertEqual(self.kicked, [])
        self.assertIn("ya no está", i.response.edited[0][0])


class RegistroTests(unittest.TestCase):
    def test_setup_exime_el_comando_del_registro(self):
        async def run():
            bot = types.SimpleNamespace(add_cog=self._add)
            await ex.setup_expulsar(bot, 123)
        self.added = []
        asyncio.run(run())
        self.assertIn("expulsar", rg.EXEMPT_COMMANDS)
        self.assertEqual(len(self.added), 1)

    async def _add(self, cog, **kw):
        self.added.append(cog)


if __name__ == "__main__":
    unittest.main()
