import json, pathlib, re, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def front(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    return dict(l.split(": ", 1) for l in m.group(1).splitlines() if ": " in l)


class NightshiftTests(unittest.TestCase):
    def test_manifest(self):
        j = json.loads(read(".claude-plugin/plugin.json"))
        self.assertEqual(j["name"], "nightshift")
        self.assertEqual(j["license"], "MIT")
        self.assertRegex(j["version"], r"^\d+\.\d+\.\d+$")

    def test_skills_have_frontmatter_and_no_broad_tools(self):
        for s in ("setup", "run"):
            t = read(f"skills/{s}/SKILL.md")
            f = front(t)
            self.assertEqual(f["name"], s)
            self.assertTrue(len(f["description"]) > 40)
            self.assertNotIn("allowed-tools", f)  # no pre-approved tools at all

    def test_no_code_no_network(self):
        files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and p.suffix in (".py", ".js", ".mjs", ".sh", ".ps1") and p.name != "test_skills.py"]
        self.assertEqual(files, [])

    def test_setup_rules(self):
        t = read("skills/setup/SKILL.md")
        for needle in ("STATUS: ACTIVE", "60%", "75%", "NEVER push", "ONLY after the user answers yes", "STATUS: DONE", "One item per run"):
            self.assertIn(needle, t)

    def test_run_rules(self):
        t = read("skills/run/SKILL.md")
        for needle in ("DONE, stop immediately", "get_usage", "under 20 minutes", "FIRST item", "never push", "BLOCKED", "STATUS: DONE"):
            self.assertIn(needle, t)

    def test_readmes_warn(self):
        for r, words in (("README.md", ("No reviewer", "Idle cost", "Kill switch")), ("README.ar.md", ("بلا مراجع", "تكلفة الخمول", "مفتاح الإيقاف"))):
            t = read(r)
            for w in words:
                self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
