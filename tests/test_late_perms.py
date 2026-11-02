import os, sys, types, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
from test_give_deposits import DiscordBase  # noqa: E402,F401  (carga el stub de discord)
import discord  # noqa: E402
import splits  # noqa: E402


class T(unittest.TestCase):
    def test_who_decides(self):
        g = discord.Guild(1, owner_id=999)
        mk = lambda uid, rid=None, admin=False: discord.Member(uid, "m", roles=[discord.Role(rid, "r", g)] if rid else [], guild=g, administrator=admin)
        s = types.SimpleNamespace(creator_id=50)
        self.assertTrue(splits.can_decide_late(mk(1, splits.STAFF_ROLE_ID), s))
        self.assertTrue(splits.can_decide_late(mk(2, splits.CALLER_ROLE_ID), s))
        self.assertTrue(splits.can_decide_late(mk(3, admin=True), s))
        self.assertTrue(splits.can_decide_late(mk(50), s))                 # quien creó el split
        self.assertFalse(splits.can_decide_late(mk(4), s))


if __name__ == "__main__":
    unittest.main()
