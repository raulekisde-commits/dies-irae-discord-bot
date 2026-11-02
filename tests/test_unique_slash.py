import glob, os, re, unittest
REPO = "/home/claude/dies-irae-discord-bot"
ROOT = REPO if os.path.isdir(REPO) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class UniqueSlashNames(unittest.TestCase):
    def test_no_duplicate_top_level_slash_commands(self):
        seen = {}
        for path in glob.glob(os.path.join(ROOT, "*.py")):
            base = os.path.basename(path)
            src = open(path, encoding="utf-8").read()
            pat = r'^@bot\.tree\.command\(\s*name="([^"]+)"' if base == "bot.py" else r'^    @app_commands\.command\(\s*name="([^"]+)"'
            for m in re.finditer(pat, src, re.M):
                seen.setdefault(m.group(1), []).append(base)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(dups, {})


if __name__ == "__main__":
    unittest.main()
