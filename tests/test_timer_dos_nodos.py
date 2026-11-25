import ast
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_give_deposits import DiscordBase  # noqa: E402,F401  (instala el stub de discord)
import discord  # noqa: E402

ROOT = os.path.dirname(HERE)


def _class_source(name):
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef) and n.name == name)
    return ast.get_source_segment(src, node)


class FakeResponse:
    def __init__(self, inter):
        self.inter = inter

    async def edit_message(self, content=None, view=None):
        self.inter.texts.append(content)


class FakeInteraction:
    def __init__(self, uid):
        self.user = type("U", (), {"id": uid, "mention": f"<@{uid}>"})()
        self.guild = None
        self.texts = []
        self.response = FakeResponse(self)

    async def edit_original_response(self, content=None, view=None):
        self.texts.append(content)


class TimerDosNodosTests(unittest.TestCase):
    def setUp(self):
        self.spawn = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
        self.entries = [{"id": 1, "material": "fibra", "tier": "8.4", "mapa": "Mapa", "spawn_at": self.spawn.isoformat(), "created_by": 7}]
        self.saved = []

        async def save(interaction, mat, tr, mapa, target, replaced=None):
            self.saved.append((mat, tr, mapa, target, replaced))
            return "✅ guardado"

        async def log(guild, text):
            pass

        ns = {"discord": discord, "Optional": __import__("typing").Optional, "datetime": datetime, "timedelta": timedelta,
              "TIMER_CONFIRM_TIMEOUT": 120, "TIMER_DUP_TOLERANCE_MINUTES": 2,
              "timer_same_target": lambda *a: [dict(e) for e in self.entries],
              "_timer_parse_dt": lambda s: datetime.fromisoformat(s), "_timer_label": lambda *a: "label",
              "timer_save_and_announce": save, "timer_log": log}
        exec(_class_source("TimerConflictView"), ns)
        self.View = ns["TimerConflictView"]

    @staticmethod
    def _press(view, label_start, inter):
        btn = [b for b in view.children if b.label.startswith(label_start)][0]
        asyncio.run(btn.callback(inter))

    def _view(self, target):
        return self.View(1, 9, "fibra", "8.4", "Mapa", target, self.entries)

    def test_otro_nodo_agrega_sin_reemplazar(self):
        v = self._view(self.spawn + timedelta(minutes=40))
        inter = FakeInteraction(9)
        self._press(v, "➕", inter)
        self.assertEqual(len(self.saved), 1)
        self.assertIsNone(self.saved[0][4])                                      # el timer anterior no se toca
        self.assertEqual(inter.texts[-1], "✅ guardado")

    def test_otro_nodo_no_duplica_si_ya_esta(self):
        v = self._view(self.spawn + timedelta(minutes=1))                         # a ±2 min = el mismo timer
        inter = FakeInteraction(9)
        self._press(v, "➕", inter)
        self.assertEqual(self.saved, [])
        self.assertIn("ya cargó ese mismo timer", inter.texts[-1])

    def test_confirmar_sigue_reemplazando(self):
        v = self._view(self.spawn + timedelta(minutes=40))
        self._press(v, "✅", FakeInteraction(9))
        self.assertEqual([e["id"] for e in self.saved[0][4]], [1])


if __name__ == "__main__":
    unittest.main()
