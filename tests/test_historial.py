"""/miembros historial (historial.py): tramos de guild y compañeros en kills, sin red."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase, run, GUILD_ID  # noqa: E402,F401
import historial as hi  # noqa: E402

ME = {"Id": "me", "Name": "Yo"}


def kill(ts, guild, parts=(), group=()):
    return {"EventId": ts, "TimeStamp": ts, "Killer": dict(ME, GuildName=guild),
            "Victim": {"Id": "v", "Name": "V", "GuildName": "Enemy"},
            "Participants": [dict(ME, GuildName=guild)] + [{"Id": i, "Name": n, "GuildName": g} for i, n, g in parts],
            "GroupMembers": [{"Id": i, "Name": n, "GuildName": g} for i, n, g in group]}


def death(ts, guild):
    return {"EventId": "d" + ts, "TimeStamp": ts, "Victim": dict(ME, GuildName=guild),
            "Killer": {"Id": "k", "Name": "K", "GuildName": "Enemy"}, "Participants": []}


class Historial(unittest.TestCase):
    def test_tramos_de_guild(self):
        kills = [kill("2026-11-01T10:00:00Z", "A"), kill("2026-11-10T10:00:00Z", "B")]
        deaths = [death("2026-11-03T10:00:00Z", "A"), death("2026-11-15T10:00:00Z", "A")]
        segs = hi.guild_segments("me", kills, deaths)
        self.assertEqual([s["guild"] for s in segs], ["A", "B", "A"])
        self.assertEqual(segs[0]["events"], 2)

    def test_companeros_sin_duplicar_por_evento(self):
        kills = [kill("2026-11-01T10:00:00Z", "A", parts=[("x", "Ana", "A")], group=[("x", "Ana", "A"), ("y", "Beto", "C")]),
                 kill("2026-11-02T10:00:00Z", "A", parts=[("x", "Ana", "B")])]
        top, by_guild = hi.companions("me", kills)
        self.assertEqual(top[0], ("Ana", "B", 2))          # guild más reciente, contada una vez por evento
        self.assertEqual(top[1], ("Beto", "C", 1))
        self.assertEqual(by_guild["B"], 2)
        self.assertNotIn("me", [t[0] for t in top])

    def test_subcomando_en_miembros(self):
        import guildcheck as gc
        self.assertIn("historial", gc.GuildCheckCog.miembros_g.commands)


if __name__ == "__main__":
    unittest.main()
