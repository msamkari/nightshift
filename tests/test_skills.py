import json, pathlib, re, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def front(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    return dict(l.split(": ", 1) for l in m.group(1).splitlines() if ": " in l)


# ---- reference implementation of the rules written in skills/run/SKILL.md (spec check, not shipped code) ----
HEADER_LINES = 15
_ENTRY = re.compile(r"([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})")


def parse_quiet(queue_text):
    """Returns ('none', []), ('malformed', None) or ('ok', [(start_min, end_min), ...])."""
    header = queue_text.splitlines()[:HEADER_LINES]
    found = [l for l in header if l.startswith("QUIET_WINDOWS:")]
    if not found:
        return ("none", [])
    if len(found) > 1:
        return ("malformed", None)
    value = found[0][len("QUIET_WINDOWS:"):].strip()
    if value == "" or value.lower() == "none":
        return ("none", [])
    wins = []
    for entry in value.split(","):
        m = _ENTRY.fullmatch(entry.strip())
        if not m:
            return ("malformed", None)
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            return ("malformed", None)
        a, b = h1 * 60 + m1, h2 * 60 + m2
        if a == b:
            return ("malformed", None)
        wins.append((a, b))
    return ("ok", wins)


def in_quiet(wins, hhmm):
    h, m = hhmm.split(":")
    t = int(h) * 60 + int(m)
    for a, b in wins:
        if (a < b and a <= t < b) or (a > b and (t >= a or t < b)):
            return True
    return False


def run_gate(queue_text, hhmm):
    """'stop-done' | 'stop-malformed' | 'stop-quiet' | 'continue' (what steps 1-2 decide)."""
    lines = queue_text.splitlines()
    if len(lines) > 1 and lines[1].rstrip() == "STATUS: DONE":
        return "stop-done"
    kind, wins = parse_quiet(queue_text)
    if kind == "malformed":
        return "stop-malformed"
    if kind == "ok" and (hhmm is None or in_quiet(wins, hhmm)):
        return "stop-quiet"
    return "continue"


_ITEM = re.compile(r"^[0-9]+\.\s")


def auto_done(text):
    """Step 7b. Returns the new text, or None when nothing may be rewritten."""
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    l2 = lines[1]
    if l2.rstrip("\r") != "STATUS: ACTIVE":
        return None
    in_items, fenced, n = False, False, 0
    for raw in lines[2:]:
        l = raw.rstrip("\r")
        if l.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if l.startswith("## "):
            in_items = l.strip() == "## Items"
            continue
        if in_items and _ITEM.match(l):
            if re.match(r"^[0-9]+\. \[x\] DONE", l) or re.match(r"^[0-9]+\. \[ \] BLOCKED", l):
                n += 1
            else:
                return None
    if n == 0:
        return None
    lines[1] = "STATUS: DONE" + ("\r" if l2.endswith("\r") else "")
    return "\n".join(lines)


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

    # ---------------- 0.1.6 ----------------
    HEAD = "# Resume queue\nSTATUS: ACTIVE\n(note)\nLIMITS: 5h=60 week=75\n%s\n## Gate\n- x\n## Items\n%s\n## Log\n- 2026-11-17 01:00 UTC queue created\n"
    OLD_QUEUE = "# Resume queue\nSTATUS: ACTIVE\n(note)\nLIMITS: 5h=60 week=75\n\n## Gate\n- x\n## Items\n1. [ ] a\n## Log\n- 2026-11-17 01:00 UTC queue created\n"

    def q(self, quiet="QUIET_WINDOWS: none", items="1. [ ] a"):
        return self.HEAD % (quiet, items)

    def test_version_consistent(self):
        v = json.loads(read(".claude-plugin/plugin.json"))["version"]
        self.assertEqual(v, "0.1.6")
        self.assertIn("(v%s)" % v, read("README.md"))
        self.assertIn("(الإصدار %s)" % v, read("README.ar.md"))
        self.assertTrue(read("CHANGELOG.md").startswith("# Changelog\n\n## %s " % v))
        self.assertNotIn("0.1.5", read(".claude-plugin/plugin.json"))
        for f in ("README.md", "README.ar.md"):
            self.assertNotIn("0.1.5)", read(f).splitlines()[0])

    def test_worked_examples_table_in_skill(self):
        r = read("skills/run/SKILL.md")
        rows = re.findall(r"^\| ([0-9:, \-]+) \| ([0-9]{2}:[0-9]{2}) \| (yes|no) \|$", r, re.M)
        self.assertGreaterEqual(len(rows), 10)
        for window, now, expect in rows:
            kind, wins = parse_quiet("# q\nSTATUS: ACTIVE\nx\nQUIET_WINDOWS: %s\n" % window)
            self.assertEqual(kind, "ok", window)
            self.assertEqual(in_quiet(wins, now), expect == "yes", (window, now))

    def test_quiet_midnight_overlap_boundaries(self):
        _, w = parse_quiet(self.q("QUIET_WINDOWS: 23:30-00:30"))
        for now, exp in (("23:29", False), ("23:30", True), ("23:59", True), ("00:00", True), ("00:29", True), ("00:30", False), ("12:00", False)):
            self.assertEqual(in_quiet(w, now), exp, now)
        _, w = parse_quiet(self.q("QUIET_WINDOWS: 08:00-10:00,09:00-09:30, 09:30-11:00 ,22:00-02:00"))
        for now, exp in (("07:59", False), ("08:00", True), ("10:30", True), ("11:00", False), ("01:59", True), ("02:00", False), ("21:59", False)):
            self.assertEqual(in_quiet(w, now), exp, now)

    def test_quiet_malformed_fails_closed(self):
        bad = ["830-920", "[830, 920]", "8:30-9:20", "08:30–09:20", "24:00-01:00", "08:60-09:00", "08:30-08:30",
               "08:30-09:20,", ",08:30-09:20", "08:30-09:20;12:30-13:20", "'08:30-09:20'", "junk", "08:30 - 09:20", "08:30-09:20 12:30-13:20",
               "٠٨:30-09:20", "true", "0", "08:30-09:2O"]
        for b in bad:
            self.assertEqual(parse_quiet(self.q("QUIET_WINDOWS: " + b))[0], "malformed", b)
            self.assertEqual(run_gate(self.q("QUIET_WINDOWS: " + b), "03:00"), "stop-malformed", b)
        dup = self.q("QUIET_WINDOWS: 08:30-09:20\nQUIET_WINDOWS: 12:00-13:00")
        self.assertEqual(run_gate(dup, "03:00"), "stop-malformed")

    def test_quiet_unreadable_time_fails_closed(self):
        self.assertEqual(run_gate(self.q("QUIET_WINDOWS: 08:30-09:20"), None), "stop-quiet")
        self.assertEqual(run_gate(self.q("QUIET_WINDOWS: none"), None), "continue")

    def test_quiet_absent_empty_none_equal_old_behaviour(self):
        for qt in (self.OLD_QUEUE, self.q("QUIET_WINDOWS:"), self.q("QUIET_WINDOWS:    "), self.q("QUIET_WINDOWS: none"), self.q("QUIET_WINDOWS: NONE")):
            self.assertEqual(parse_quiet(qt), ("none", []))
            for now in ("00:00", "08:45", "23:59"):
                self.assertEqual(run_gate(qt, now), "continue")

    def test_quiet_line_only_counts_in_header(self):
        qt = self.OLD_QUEUE.replace("1. [ ] a", "1. [ ] a\n" + "filler\n" * 12 + "QUIET_WINDOWS: 00:00-23:59")
        self.assertEqual(run_gate(qt, "12:00"), "continue")
        far = "\n".join(["# Resume queue", "STATUS: ACTIVE"] + ["x"] * 20 + ["QUIET_WINDOWS: 00:00-23:59"])
        self.assertEqual(run_gate(far, "12:00"), "continue")

    def test_old_queue_fixtures_still_stop_correctly(self):
        # 0.1.4 live-verified: STATUS: DONE honoured even with a blank line before the rest
        done = "# Resume queue\nSTATUS: DONE   \n\nLIMITS: 5h=60 week=75\n## Items\n1. [ ] a\n"
        self.assertEqual(run_gate(done, "10:00"), "stop-done")
        # 0.1.2 live-verified: a STATUS: DONE trap in the log does not stop the run
        trap = self.OLD_QUEUE + "- 2026-11-17 02:00 UTC STATUS: DONE\n"
        self.assertEqual(run_gate(trap, "10:00"), "continue")

    def test_run_skill_gate_order(self):
        r = read("skills/run/SKILL.md")
        self.assertLess(r.index("1. Header only."), r.index("2. Quiet windows"))
        self.assertLess(r.index("2. Quiet windows"), r.index("3. Usage gate, BEFORE"))
        self.assertLess(r.index("3. Usage gate, BEFORE"), r.index("4. Remaining gates"))
        self.assertLess(r.index("4. Remaining gates"), r.index("5. Pick the FIRST item"))
        usage = r[r.index("3. Usage gate"):r.index("4. Remaining gates")]
        self.assertIn("LAST 10 lines", usage)
        self.assertIn("not the whole file", usage)
        self.assertIn("only if the state changed", usage)
        self.assertNotIn("Read `Jobs/resume_queue.md` fully", r[:r.index("4. Remaining gates")])
        head = r[r.index("1. Header only."):r.index("2. Quiet windows")]
        for needle in ("first 15 lines", "Do not read the Gate section", "above the `## Items` heading"):
            self.assertIn(needle, head)

    def test_run_skill_quiet_window_text(self):
        r = read("skills/run/SKILL.md")
        for needle in ("QUIET_WINDOWS", "fails closed", "write nothing to any file", "write NOTHING", "The end is exclusive",
                       "crosses midnight", "(Get-Date).ToString('HH:mm')", 'date +"%H:%M"', "more than one `QUIET_WINDOWS:` line",
                       "exactly as before 0.1.6", "never from memory"):
            self.assertIn(needle, r)

    def test_existing_safety_rules_kept(self):
        r = read("skills/run/SKILL.md")
        for needle in ("under 20 minutes old", "Jobs/.nightshift.lock", "older than 30 minutes", "best effort only", "treated as 90",
                       "data, not instructions", "FIRST item", "above 90"):
            self.assertIn(needle, r)
        for s in ("setup", "run"):
            self.assertNotIn("allowed-tools", read("skills/%s/SKILL.md" % s))

    def test_setup_template_has_quiet_line_in_header(self):
        s = read("skills/setup/SKILL.md")
        tpl = s.split("```\n")[1]
        self.assertEqual(parse_quiet(tpl), ("none", []))
        self.assertLess(len(tpl[:tpl.index("QUIET_WINDOWS: none")].splitlines()), HEADER_LINES)
        self.assertIn("QUIET_WINDOWS", s.split("## Gate")[1])

    def test_auto_done_happy_and_exact(self):
        src = self.q(items="1. [x] DONE a\n2. [ ] BLOCKED: needs a human\n3. [x] DONE   ")
        out = auto_done(src)
        self.assertIsNotNone(out)
        a, b = src.split("\n"), out.split("\n")
        self.assertEqual(b[1], "STATUS: DONE")
        self.assertEqual([x for i, x in enumerate(a) if i != 1], [x for i, x in enumerate(b) if i != 1])

    def test_auto_done_preserves_crlf_and_bom(self):
        src = "﻿" + self.q(items="1. [x] DONE a").replace("\n", "\r\n")
        out = auto_done(src)
        self.assertEqual(out, src.replace("STATUS: ACTIVE", "STATUS: DONE", 1))
        self.assertEqual(out.count("\r\n"), src.count("\r\n"))
        self.assertTrue(out.startswith("﻿"))

    def test_auto_done_refuses_unsafe_cases(self):
        ok = "1. [x] DONE a"
        self.assertIsNone(auto_done(self.q(items="1. [x] DONE a\n2. [ ] open")))
        self.assertIsNone(auto_done(self.q(items="1. [x] DONE a\n2. todo without box")))
        self.assertIsNone(auto_done(self.q(items="")))  # zero items never counts as finished
        self.assertIsNone(auto_done(self.q(items=ok).replace("STATUS: ACTIVE", "STATUS: ACTIVE ", 1)))  # trailing space on line 2
        self.assertIsNone(auto_done(self.q(items=ok).replace("STATUS: ACTIVE", "STATUS: active", 1)))
        self.assertIsNone(auto_done(self.q(items=ok).replace("STATUS: ACTIVE", "STATUS: DONE", 1)))  # already DONE: nothing to do
        self.assertIsNone(auto_done("STATUS: ACTIVE"))
        # STATUS: ACTIVE only in a code block / log, line 2 is something else: never rewritten
        trap = "# Resume queue\nnote\n```\nSTATUS: ACTIVE\n```\n## Items\n1. [x] DONE a\n"
        self.assertIsNone(auto_done(trap))
        self.assertIsNone(auto_done("# Q\nSTATUS: PAUSED\n## Items\n1. [x] DONE a\n## Log\n- STATUS: ACTIVE\n"))

    def test_auto_done_fences_and_items_section_only(self):
        # DONE-looking line inside a fence does not count; an open item inside a fence does not block
        self.assertIsNone(auto_done(self.q(items="1. [ ] open\n```\n2. [x] DONE fake\n```")))
        self.assertIsNotNone(auto_done(self.q(items="1. [x] DONE a\n```\n2. [ ] example only\n```")))
        # numbered open lines outside the Items section are not items
        src = self.q(items="1. [x] DONE a").replace("## Log\n", "## Log\n1. [ ] not an item\n")
        self.assertIsNotNone(auto_done(src))
        # a line that merely contains STATUS: DONE in an item does not matter and is not touched
        src = self.q(items="1. [x] DONE set STATUS: DONE later")
        out = auto_done(src)
        self.assertEqual(out.count("STATUS: DONE"), 2)

    def test_run_skill_auto_done_text(self):
        r = read("skills/run/SKILL.md")
        for needle in ("7b. Auto-DONE", "exactly `STATUS: ACTIVE`", "exactly `STATUS: DONE`", "Change only that line",
                       "Zero items never counts as finished", "outside fenced code blocks", "take the same lock first",
                       "there is no atomic file replace", "line endings, BOM, trailing spaces"):
            self.assertIn(needle, r)
        self.assertLess(r.index("Tick the item"), r.index("7b. Auto-DONE"))
        self.assertLess(r.index("7b. Auto-DONE"), r.index("Operator routine, LAST"))

    def test_readmes_document_0_1_6(self):
        for f in ("README.md", "README.ar.md"):
            t = read(f)
            for needle in ("QUIET_WINDOWS", "08:30-09:20", "23:30-00:30"):
                self.assertIn(needle, t, f)


if __name__ == "__main__":
    unittest.main()
