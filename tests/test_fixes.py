"""Tests for the 0.1.7 fix batch (red-team findings M1-M5, L1, L3, L4, L5, L9)."""
import json
import os
import pathlib
import re
import shutil
import tempfile
import time
import unittest

import test_queue as tq

Q = tq.Q
nsq = tq.nsq
ROOT = tq.ROOT


class TestM1RootBinding(tq.Base):
    def other(self):
        """A second project whose queue is bound to ITSELF."""
        d = os.path.realpath(tempfile.mkdtemp(prefix="nsq_other_"))
        self.addCleanup(shutil.rmtree, d, True)
        os.mkdir(os.path.join(d, "Jobs"))
        text = tq.Q().replace("\n", "\n", 1)
        lines = text.split("\n")
        lines.insert(2, "NIGHTSHIFT_ROOT: " + d.replace("\\", "/"))
        q = os.path.join(d, "Jobs", "resume_queue.md")
        with open(q, "wb") as f:
            f.write("\n".join(lines).encode("utf-8"))
        return d.replace("\\", "/"), q

    def test_foreign_root_refused_for_every_command(self):
        self.put(Q())
        other_root, other_q = self.other()
        # the queue of THIS project names this project; asking for it through another root is the attack shape:
        # here we point --root at the other project but the other queue names itself, so use a queue that names us
        with open(other_q, "rb") as f:
            before = f.read()
        foreign = before.replace(other_root.encode(), self.root.encode())  # a queue that belongs to another folder
        with open(other_q, "wb") as f:
            f.write(foreign)
        for args in (("tick", "1"), ("block", "1", "--", "x"), ("done",), ("lock",), ("unlock",), ("log", "--", "x"),
                     ("log", "--kind", "config-error")):
            rc, out, _ = self.run_q(args[0], "--root", other_root, *args[1:])
            self.assertEqual(rc, 2, (args, out))
            self.assertIn("NIGHTSHIFT_ROOT", out)
        with open(other_q, "rb") as f:
            self.assertEqual(f.read(), foreign)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(other_q), ".nightshift.lock")))
        rc, out, _ = self.run_q("check", "--root", other_root)
        self.assertEqual(rc, 2)
        res = json.loads(out)
        self.assertFalse(res["ok"])
        self.assertTrue(res["stop"])
        self.assertIn("NIGHTSHIFT_ROOT", res["reason"])

    def test_matching_root_spellings_work(self):
        self.put(Q())
        for spelling in (self.root, self.root + "/", self.root.replace("/", "\\") if os.name == "nt" else self.root):
            rc, out, _ = self.run_q("check", "--root", spelling)
            self.assertEqual(rc, 0, (spelling, out))
            self.assertTrue(json.loads(out)["root_bound"])
        if os.name == "nt":
            rc, out, _ = self.run_q("check", "--root", self.root.upper())
            self.assertEqual(rc, 0, out)

    def test_header_wrong_or_duplicated_is_refused(self):
        for hdr in ("NIGHTSHIFT_ROOT: C:/does/not/exist" if os.name == "nt" else "NIGHTSHIFT_ROOT: /does/not/exist",
                    "NIGHTSHIFT_ROOT:", "NIGHTSHIFT_ROOT: a\x01b"):
            self.put(Q().replace("\nLIMITS", "\n" + hdr + "\nLIMITS", 1))
            self.assertEqual(self.cmd("tick", "1")[0], 2, hdr)
        self.put(Q().replace("\nLIMITS", "\nNIGHTSHIFT_ROOT: %s\nNIGHTSHIFT_ROOT: %s\nLIMITS" % (self.root, self.root), 1))
        self.assertEqual(self.cmd("tick", "1")[0], 2)  # two header lines: ambiguous, refused

    def test_legacy_queue_without_header_works_with_a_note(self):
        self.legacy = True
        self.put(Q())
        rc, out, _ = self.cmd("check")
        self.assertEqual(rc, 0, out)
        res = json.loads(out)
        self.assertFalse(res["root_bound"])
        self.assertIn("NIGHTSHIFT_ROOT", res["note"])
        self.assertFalse(res["stop"])
        rc, out, err = self.cmd("tick", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("legacy", err)
        self.assertIn("[x] DONE", self.get().decode())

    def test_bound_queue_has_no_note(self):
        self.put(Q())
        res = self.check()
        self.assertTrue(res["root_bound"])
        self.assertIsNone(res["note"])
        rc, out, err = self.cmd("tick", "1")
        self.assertEqual(rc, 0)
        self.assertNotIn("legacy", err)

    def test_header_below_the_header_block_does_not_count(self):
        self.legacy = True
        self.put(Q().replace("## Items\n", "## Items\nNIGHTSHIFT_ROOT: C:/elsewhere\n", 1))
        self.assertFalse(self.check()["root_bound"])

    def test_setup_template_writes_the_header(self):
        setup = (ROOT / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        tpl = setup.split("```")[1]
        lines = tpl.split("\n")
        idx = [i for i, l in enumerate(lines) if l.startswith("NIGHTSHIFT_ROOT:")]
        self.assertEqual(len(idx), 1)
        self.assertLess(idx[0], 15)
        self.assertLess(idx[0], [i for i, l in enumerate(lines) if l.startswith("## ")][0])


class TestM2LogSection(tq.Base):
    NOLOG = "# Q\nSTATUS: ACTIVE\nLIMITS: 5h=60 week=75\nQUIET_WINDOWS: none\n\n## Items\n- [ ] one\n- [ ] two\n"

    def test_missing_log_is_config_error(self):
        self.put(self.NOLOG)
        res = self.check()
        self.assertTrue(res["stop"])
        self.assertEqual(res["reason"], "config-error")
        self.assertEqual(res["config_error"], "no ## Log section")

    def test_log_adds_heading_once_and_dedupes(self):
        self.put(self.NOLOG)
        for _ in range(3):
            rc, out, _ = self.cmd("log", "--kind", "config-error")
            self.assertEqual(rc, 0, out)
        text = self.get().decode()
        self.assertEqual(text.split(chr(10)).count("## Log"), 1)
        self.assertEqual(text.count("CONFIG-ERROR: no ## Log section"), 1)
        self.assertTrue(text.index("## Log") > text.index("- [ ] two"))  # not inside the Items section
        self.assertEqual(len(tq.nsq.items_state(text)["items"]), 2)
        self.assertEqual(self.check()["config_error"], None)  # now a Log section exists

    def test_running_line_lands_in_a_log_section_and_guard_fires(self):
        self.put(self.NOLOG)
        rc, out, _ = self.cmd("log", "--kind", "running", "--", "1")
        self.assertEqual(rc, 0, out)
        text = self.get().decode()
        self.assertEqual(len(nsq.items_state(text)["items"]), 2)
        self.assertTrue(text.rstrip().split("\n")[-2] == "## Log")
        res = self.cmd("check")
        self.assertEqual(json.loads(res[1])["reason"], "recent-log")

    def test_crlf_and_no_trailing_newline(self):
        for body in (self.NOLOG.replace("\n", "\r\n"), self.NOLOG.rstrip("\n"), self.NOLOG.rstrip("\n").replace("\n", "\r\n")):
            self.put(body)
            self.assertEqual(self.cmd("log", "--kind", "config-error")[0], 0)
            data = self.get().decode()
            self.assertTrue(data.startswith(self.bound(body).rstrip("\r\n")))
            if "\r\n" in body:
                self.assertEqual(data.count("\n"), data.count("\r\n"))
            self.assertEqual(data.replace(chr(13), "").split(chr(10)).count("## Log"), 1)


class TestM3ItemId(tq.Base):
    def test_check_reports_id_and_tick_with_matching_id(self):
        self.put(Q())
        nid = self.check()["next_item"]["id"]
        self.assertRegex(nid, r"^[0-9a-f]{12}$")
        rc, out, err = self.cmd("tick", "1", "--id", nid)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("NOTE", err)

    def test_stale_id_after_user_inserts_an_item_is_refused(self):
        self.put(Q())
        nid = self.check()["next_item"]["id"]
        edited = self.get().decode().replace("1. [ ] first", "0. [ ] inserted by user\n1. [ ] first", 1)
        self.put(edited, raw=False)
        before = self.get()
        for args in (("tick", "1", "--id", nid), ("block", "1", "--id", nid, "--", "why")):
            rc, out, _ = self.cmd(*args)
            self.assertEqual(rc, 2, out)
            self.assertIn("changed since check", out)
            self.assertEqual(self.get(), before)

    def test_block_with_matching_id_and_crlf_id_stable(self):
        for eol in ("\n", "\r\n"):
            self.put(Q(eol=eol))
            nid = self.check()["next_item"]["id"]
            self.assertEqual(self.cmd("block", "1", "--id", nid, "--", "no")[0], 0)
        a = nsq.item_id("1. [ ] first")
        self.assertEqual(a, nsq.item_id("1. [ ] first\r"))

    def test_bad_id_format_is_a_usage_error_and_missing_id_still_works(self):
        self.put(Q())
        for bad in ("XYZ", "abc", "A" * 12, "0123456789abc"):
            self.assertEqual(self.cmd("tick", "1", "--id", bad)[0], 4, bad)
        rc, out, err = self.cmd("tick", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("--id", err)  # backward-safe: allowed, with a note

    def test_id_is_of_the_second_item_after_the_first_is_done(self):
        self.put(Q())
        a = self.check()["next_item"]
        self.assertEqual(self.cmd("tick", "1", "--id", a["id"])[0], 0)
        b = self.check(utc="2026-11-18 13:00")["next_item"]
        self.assertEqual(b["index"], 2)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.cmd("tick", "2", "--id", a["id"])[0], 2)  # an id of another item is refused


class TestM4Lock(tq.Base):
    def age_lock(self, minutes):
        t = time.time() - minutes * 60
        os.utime(self.lock, (t, t))

    def test_every_queue_write_refreshes_the_lock(self):
        self.put(Q())
        self.assertEqual(self.cmd("lock")[0], 0)
        for args in (("log", "--kind", "running", "--", "1"), ("tick", "1"), ("block", "2", "--", "r"), ("log", "--", "done")):
            self.age_lock(25)
            self.assertEqual(self.cmd(*args)[0], 0, args)
            self.assertLess(time.time() - os.stat(self.lock).st_mtime, 120, args)

    def test_done_refreshes_the_lock(self):
        self.put(Q(items="1. [x] DONE a"))
        self.assertEqual(self.cmd("lock")[0], 0)
        self.age_lock(25)
        self.assertEqual(self.cmd("done")[0], 0)
        self.assertLess(time.time() - os.stat(self.lock).st_mtime, 120)

    def test_a_long_run_with_heartbeat_is_not_stolen(self):
        self.put(Q(log="- 2026-01-01 00:00 UTC old"))
        self.assertEqual(self.cmd("lock")[0], 0)
        self.age_lock(29)
        self.assertEqual(self.cmd("log", "--", "progress")[0], 0)  # heartbeat
        self.age_lock(0)
        self.assertEqual(self.cmd("lock")[0], 3)

    def test_stale_takeover_leaves_no_tombstone(self):
        self.put(Q())
        os.mkdir(self.lock)
        self.age_lock(31)
        rc, out, _ = self.cmd("lock")
        self.assertEqual(rc, 0, out)
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "Jobs")) if ".stale." in n], [])
        self.assertLess(time.time() - os.stat(self.lock).st_mtime, 120)

    def test_steal_never_removes_a_fresh_lock(self):
        self.put(Q())
        os.mkdir(self.lock)  # fresh: the state of a lock that was re-taken between check and rename
        nsq.steal_stale(nsq.Paths(self.tmp))
        self.assertTrue(os.path.isdir(self.lock))
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "Jobs")) if ".stale." in n], [])

    def test_run_skill_unlocks_before_the_operator_routine(self):
        skill = (ROOT / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8")
        steps = skill.split("## Steps")[1].split("## Fallback")[0]
        op = steps.index("Operator routine")
        self.assertIn("`unlock`", steps[:op])
        self.assertRegex(steps[op:op + 600], r"lock (is )?already (removed|released)|already unlocked")


class TestM5Docs(unittest.TestCase):
    def read(self, *parts):
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_run_skill_first_body_line_stops_a_hand_reader(self):
        body = self.read("skills", "run", "SKILL.md").split("---", 2)[2].lstrip("\n")
        first = body.split("\n")[0]
        self.assertRegex(first, r"(?i)by hand")
        self.assertRegex(first, r"(?i)stop")
        self.assertIn("Skill tool", first)

    def test_setup_prompt_names_the_skill_tool(self):
        setup = self.read("skills", "setup", "SKILL.md")
        self.assertIn("Skill tool", setup)
        self.assertIn("nightshift:run", setup)
        self.assertIn("do not open or read", setup)

    def test_readmes_document_the_task_prompt(self):
        for name in ("README.md", "README.ar.md"):
            text = self.read(name)
            self.assertIn("nightshift:run", text, name)
            self.assertIn("Skill", text, name)
            self.assertIn("NIGHTSHIFT_ROOT", text, name)
        # nothing may claim an untested allow-rule form works
        self.assertNotIn("allow rule works", self.read("README.md"))


class TestLimitsStrict(tq.Base):
    def lim(self, value):
        return self.check()

    def test_malformed_limits_are_config_errors(self):
        for bad in ("5h = 30 week = 40", "five hours 30%", "5h=30%", "5h=abc", "5h=30 5h=40", "week=1000", "", "5h=30, week=40",
                    "5h=30 junk", "5H=30"):
            self.put(Q().replace("5h=60 week=75", bad, 1))
            res = self.check()
            self.assertTrue(res["stop"], repr(bad))
            self.assertEqual(res["reason"], "config-error", repr(bad))
            self.assertIn("LIMITS is malformed", res["config_error"], repr(bad))

    def test_valid_limits_forms(self):
        for good, exp in (("5h=60 week=75", (60, 75)), ("5h=30", (30, 75)), ("week=40", (60, 40)), ("week=40 5h=30", (30, 40)),
                          ("  5h=10   week=20  ", (10, 20)), ("5h=100 week=200", (90, 90))):
            self.put(Q().replace("5h=60 week=75", good, 1))
            res = self.check()
            self.assertFalse(res["stop"], good)
            self.assertEqual((res["limits"]["five_hour"], res["limits"]["weekly"]), exp, good)


class TestL3Encoding(tq.Base):
    def test_utf16_and_nul_queues_are_refused_and_never_written(self):
        for data in ("\ufeff# Q\nSTATUS: ACTIVE\n".encode("utf-16-le"), b"\xff\xfe" + Q().encode("utf-16-le"),
                     b"\xfe\xff" + Q().encode("utf-16-be"), Q().encode("utf-8")[:20] + b"\x00" + Q().encode("utf-8")[20:]):
            self.put(data, raw=True)
            rc, out, _ = self.cmd("check")
            res = json.loads(out)
            self.assertFalse(res["ok"])
            self.assertIn("not UTF-8", res["reason"])
            for args in (("log", "--kind", "config-error"), ("log", "--", "x"), ("tick", "1"), ("done",), ("lock",)):
                rc, out, _ = self.cmd(*args)
                self.assertEqual(rc, 2, (args, out))
                self.assertEqual(self.get(), data)
            self.assertFalse(os.path.exists(self.lock))

    def test_invalid_utf8_and_bom_utf8_still_work(self):
        self.put(b"\xef\xbb\xbf" + Q().encode("utf-8") + b"- x \xe3 cp1256 byte\n", raw=True)
        self.assertEqual(self.cmd("check")[0], 0)


class TestL4SecondItemsHeading(tq.Base):
    def test_second_items_heading_after_log_does_not_reopen(self):
        text = "# Q\nSTATUS: ACTIVE\nLIMITS: 5h=60 week=75\nQUIET_WINDOWS: none\n\n## Items\n1. [x] DONE one\n## Log\n- 2026-01-01 00:00 UTC old\n## Items\n- [ ] hidden\n"
        self.put(text)
        res = self.check()
        self.assertIsNone(res["next_item"])
        self.assertEqual(res["items"], {"open": 0, "done": 1, "blocked": 0, "unrecognised": 0})
        self.assertEqual(self.cmd("tick", "2")[0], 2)

    def test_second_heading_inside_first_section_is_plain_text(self):
        self.put(Q(items="1. [ ] a\n## Items\n2. [ ] b"))
        self.assertEqual(self.check()["items"]["open"], 2)  # the section is still open, the second heading adds nothing


class TestL5Dedupe(tq.Base):
    def test_ordinary_line_ending_like_a_config_error_does_not_suppress_the_real_one(self):
        text = Q(items="1. [x] DONE a").replace("STATUS: ACTIVE", "STATUS: ACTIVE ", 1)
        self.put(text)
        reason = "queue is finished but line 2 could not be set safely"
        self.assertEqual(self.cmd("log", "--", "fixed it. CONFIG-ERROR: " + reason)[0], 0)
        rc, out, _ = self.cmd("done")
        self.assertEqual(rc, 2, out)
        real = [l for l in self.get().decode().split("\n") if re.match(r"^- \S+ \S+ UTC CONFIG-ERROR: ", l)]
        self.assertEqual(len(real), 1, real)
        rc, out, _ = self.cmd("done")  # the same real error again is not repeated
        real = [l for l in self.get().decode().split("\n") if re.match(r"^- \S+ \S+ UTC CONFIG-ERROR: ", l)]
        self.assertEqual(len(real), 1, real)

    def test_note_dedupe_needs_the_same_reason(self):
        self.put(Q())
        self.assertEqual(self.cmd("log", "--kind", "note", "--", "one reason")[0], 0)
        self.assertIn("unchanged", self.cmd("log", "--kind", "note", "--", "one reason")[1])
        self.assertIn("logged", self.cmd("log", "--kind", "note", "--", "other reason")[1])
        self.assertIn("logged", self.cmd("log", "--", "x STOPPED: other reason")[1])
        self.assertIn("logged", self.cmd("log", "--kind", "note", "--", "other reason")[1])


class TestL9Clock(tq.Base):
    def test_injection_only_via_env_with_the_switch(self):
        self.put(Q(q="08:30-09:20"))
        res = self.check(now="09:00", utc="2026-11-18 09:00")
        self.assertTrue(res["in_quiet_window"])
        self.assertFalse(self.check(now="09:20")["in_quiet_window"])
        rc, out, _ = self.cmd("check", clock_value="2026-11-18 09:00|25:61")
        self.assertEqual(rc, 4, out)
        for src in ("--now", "--utc-now"):
            self.assertNotIn(src, (ROOT / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8"))

    def test_value_only_local_and_only_utc(self):
        self.put(Q(q="08:30-09:20", log="- 2026-11-18 08:00 UTC RUNNING item 1"))
        rc, out, _ = self.cmd("check", clock_value="|09:00")
        self.assertTrue(json.loads(out)["in_quiet_window"])
        rc, out, _ = self.cmd("check", clock_value="2026-11-18 08:10")
        self.assertEqual(json.loads(out)["reason"], "recent-log")


if __name__ == "__main__":
    unittest.main()
