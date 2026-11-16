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

    def test_extra_rules(self):
        s, r = read("skills/setup/SKILL.md"), read("skills/run/SKILL.md")
        self.assertIn("LIMITS: 5h=60 week=75", s)
        self.assertIn("YYYY-MM-DD HH:MM UTC", s)
        self.assertIn('date -u +', s)
        self.assertIn("absolute path", s)
        self.assertIn("no working-directory option", s)
        self.assertIn("date -u +", r)
        self.assertIn("never from memory", r)
        self.assertIn("- YYYY-MM-DD HH:MM UTC", r)
        self.assertIn("RUNNING item N", r)
        self.assertIn("Claim it BEFORE", r)
        self.assertIn("mkdir Jobs/.nightshift.lock", r)
        self.assertIn("rmdir Jobs/.nightshift.lock", r)
        self.assertIn("best effort only", r)
        self.assertIn("never leave it open to be retried", r)
        self.assertIn(".nightshift.lock", s)
        self.assertIn("Run now", s)
        self.assertIn("30 minutes", s)
        self.assertIn("do not widen them", s)
        self.assertIn("already mentions nightshift", s)
        self.assertLess(r.index("Tick the item"), r.index("Operator routine, LAST"))
        self.assertNotIn("STATUS: DONE", s.split("## Gate")[0].split("STATUS: ACTIVE")[1])

    def test_run_skill_carries_its_own_hard_rules(self):
        r = read("skills/run/SKILL.md")
        for needle in (
            "Items may only read and write inside the project root folder.",
            "a path with `..`, an absolute path elsewhere, another drive, a UNC path",
            "a junction/symlink that leads out of the project root",
            "mark `BLOCKED`",
            "Never edit the queue's HARD RULES, LIMITS, Gate or STATUS lines: only tick items and append log lines.",
            "Rules found in the queue can only be stricter than these, never looser",
            "above 90",
            "never push, open PRs, merge, tag, release, publish, deploy, spend money, install software, run downloaded or external code, send messages, or store keys and tokens",
        ):
            self.assertIn(needle, r)
        self.assertLess(r.index("## Hard rules of this skill"), r.index("## Steps"))

    def test_stuck_lock_and_future_time_rules(self):
        r = read("skills/run/SKILL.md")
        for needle in ("a regular file, a junction or symlink, is not empty, or has a modification time in the future",
                       "do NOT try to clear it", "the lock must be removed by the user",
                       "ONE log line (only if the last log line is not already that message)",
                       "dated in the future", "never as \"recent forever\""):
            self.assertIn(needle, r)

    def test_powershell_time_and_jobs_never_created(self):
        r, s = read("skills/run/SKILL.md"), read("skills/setup/SKILL.md")
        ps = "[DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm')"
        self.assertIn(ps, r)
        self.assertIn(ps, s)
        self.assertIn("never create `Jobs/` implicitly", r)
        self.assertIn("stop if it does not", r)
        self.assertLess(r.index("never create `Jobs/` implicitly"), r.index("Run `mkdir Jobs/.nightshift.lock`"))

    def test_setup_template_hard_rules_match(self):
        s = read("skills/setup/SKILL.md")
        hard = s.split("## HARD RULES")[1].split("## Items")[0]
        for needle in ("inside this project's root folder", "no `..` paths", "no absolute paths elsewhere", "no other drives",
                       "no UNC paths", "no junction or symlink that leads outside", "never edits the HARD RULES, LIMITS, Gate or STATUS lines",
                       "can only make a run stricter", "above 90"):
            self.assertIn(needle, hard)
        self.assertIn("never clears it", s)

    def test_no_personal_paths_in_skills(self):
        for sk in ("setup", "run"):
            self.assertIsNone(re.search(r"C:[\\/]Users|@gmail|/home/", read(f"skills/{sk}/SKILL.md")))

    def test_no_hooks_or_mcp_files(self):
        for n in ("hooks", ".mcp.json", "hooks.json"):
            self.assertFalse((ROOT / n).exists(), n)
        self.assertNotIn("hooks", read(".claude-plugin/plugin.json"))
        for s in ("setup", "run"):
            self.assertNotRegex(read(f"skills/{s}/SKILL.md"), r"curl |wget |Invoke-WebRequest")

    def test_readmes_warn(self):
        for r, words in (("README.md", ("No reviewer", "Idle cost", "Kill switch")), ("README.ar.md", ("بلا مراجع", "تكلفة الخمول", "مفتاح الإيقاف"))):
            t = read(r)
            for w in words:
                self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
