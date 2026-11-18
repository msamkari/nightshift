import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = str(ROOT / "scripts" / "queue.py")
import importlib.util  # noqa: E402

# Load by path under another name: a module called "queue" would clash with the standard library one.
_spec = importlib.util.spec_from_file_location("nightshift_queue", SCRIPT)
nsq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nsq)

BASE = ("# Resume queue\nSTATUS: ACTIVE\nLIMITS: 5h=60 week=75\nQUIET_WINDOWS: {q}\n\n## Items\n{items}\n\n"
        "## Operator routine\nx\n\n## Log\n{log}\n")


def Q(q="none", items="1. [ ] first\n2. [ ] second", log="- 2026-11-18 01:00 UTC queue created", eol="\n"):
    return BASE.format(q=q, items=items, log=log).replace("\n", eol)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="nsq_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp.replace("\\", "/")
        os.mkdir(os.path.join(self.tmp, "Jobs"))
        self.queue = os.path.join(self.tmp, "Jobs", "resume_queue.md")
        self.lock = os.path.join(self.tmp, "Jobs", ".nightshift.lock")

    def put(self, text, raw=False):
        with open(self.queue, "wb") as f:
            f.write(text if raw else text.encode("utf-8"))

    def get(self):
        with open(self.queue, "rb") as f:
            return f.read()

    def run_q(self, *args, **kw):
        env = dict(os.environ)
        for k in ("PYTHONUTF8", "PYTHONIOENCODING"):
            env.pop(k, None) if kw.get("plain") else None
        env.pop("NIGHTSHIFT_TEST_CLOCK", None)
        if kw.get("clock", True):
            env["NIGHTSHIFT_TEST_CLOCK"] = "1"
        argv = [sys.executable] + ([] if kw.get("plain") else ["-X", "utf8"]) + [SCRIPT] + list(args)
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip(), r.stderr.decode("utf-8", "replace")

    def cmd(self, name, *rest, **kw):
        return self.run_q(name, "--root", self.root, *rest, **kw)

    def check(self, now="12:00", utc="2026-11-18 12:00", **kw):
        rc, out, _ = self.cmd("check", "--now", now, "--utc-now", utc, **kw)
        self.assertEqual(rc, 0, out)
        return json.loads(out)


class TestCheckWindows(Base):
    def test_worked_table_from_skill(self):
        skill = (ROOT / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8")
        rows = re.findall(r"^\| ([0-9:, -]+) \| ([0-9]{2}:[0-9]{2}) \| (yes|no) \|$", skill, re.M)
        self.assertEqual(len(rows), 10)
        for win, t, inside in rows:
            self.put(Q(q=win))
            res = self.check(now=t)
            self.assertEqual(res["in_quiet_window"], inside == "yes", (win, t))
            self.assertEqual(res["stop"], inside == "yes")
            if inside == "yes":
                self.assertEqual(res["reason"], "quiet-window")

    def test_malformed_values_fail_closed(self):
        for bad in ("830-920", "08:30\u201309:20", "24:00-01:00", "8:30-9:20", "08:30-09:20,", "[08:30-09:20]", "08:30-09:20 # x",
                    "08:30:00-09:20", "08:30-08:30", "00:00-00:00", "08:30-09:20;12:00-13:00"):
            self.put(Q(q=bad))
            res = self.check()
            self.assertTrue(res["stop"], bad)
            self.assertEqual(res["reason"], "config-error", bad)
            self.assertIn("QUIET_WINDOWS is malformed", res["config_error"])

    def test_backticks_message_and_none_forms(self):
        self.put(Q(q="`08:30-09:20`"))
        self.assertIn("backticks", self.check()["config_error"])
        for ok in ("none", "None", "", "  NONE "):
            self.put(Q(q=ok))
            self.assertFalse(self.check()["stop"], repr(ok))

    def test_duplicate_and_misplaced_keys(self):
        self.put(Q().replace("LIMITS:", "QUIET_WINDOWS: none\nLIMITS:", 1))
        self.assertIn("more than once", self.check()["config_error"])
        for line in ("quiet windows: 08:30-09:20", "QUIET_WINDOWS : none", "- QUIET_WINDOWS: none", "Quiet_Windows: none",
                     "QUIET_WINDOWS\uff1a none", "limits : 5h=1"):
            self.put(Q().replace("\n\n## Items", "\n" + line + "\n\n## Items", 1))
            res = self.check()
            self.assertEqual(res["reason"], "config-error", line)
            self.assertIn("misplaced or misspelled", res["config_error"])

    def test_limits_only_from_header_and_capped(self):
        self.put(Q().replace("5h=60 week=75", "5h=100 week=200"))
        self.assertEqual(self.check()["limits"], {"five_hour": 90, "weekly": 90})
        self.put(Q().replace("LIMITS: 5h=60 week=75\n", ""))
        self.assertEqual(self.check()["limits"], {"five_hour": 60, "weekly": 75})
        self.put(Q().replace("## Items\n", "## Items\nLIMITS: 5h=1 week=1\n", 1))
        self.assertEqual(self.check()["reason"], "config-error")

    def test_status_done_forms(self):
        for line2 in ("STATUS: DONE", "STATUS:DONE", "status: done", "STATUS: DONE (paused)"):
            self.put(Q().replace("STATUS: ACTIVE", line2, 1))
            res = self.check()
            self.assertTrue(res["status_done"] and res["stop"], line2)
            self.assertEqual(res["reason"], "status-done")
        for line2 in ("STATUS: ACTIVE (not DONE yet)", "STATUS: ACTIVE"):
            self.put(Q().replace("STATUS: ACTIVE", line2, 1))
            self.assertFalse(self.check()["status_done"], line2)
        self.put(Q(log="- 2026-11-18 01:00 UTC STATUS: DONE"))
        self.assertFalse(self.check()["status_done"])

    def test_test_clock_rejected_without_env(self):
        self.put(Q())
        rc, out, _ = self.cmd("check", "--now", "12:00", clock=False)
        self.assertEqual(rc, 4, out)


class TestCheckItemsAndLog(Base):
    def test_age_rule(self):
        self.put(Q(log="- 2026-11-18 12:00 UTC RUNNING item 1"))
        self.assertTrue(self.check(utc="2026-11-18 12:19")["stop"])
        self.assertEqual(self.check(utc="2026-11-18 12:19")["reason"], "recent-log")
        res = self.check(utc="2026-11-18 12:20")
        self.assertFalse(res["stop"])
        self.assertEqual(res["last_log_age_minutes"], 20)

    def test_config_error_lines_ignored_by_age(self):
        log = "- 2026-11-18 10:00 UTC done earlier\n- 2026-11-18 12:15 UTC CONFIG-ERROR: x"
        self.put(Q(log=log))
        res = self.check(utc="2026-11-18 12:16")
        self.assertFalse(res["stop"])
        self.assertEqual(res["last_log_age_minutes"], 136)

    def test_unparseable_last_line_is_config_error_and_future_is_not_recent(self):
        self.put(Q(log="- yesterday something"))
        res = self.check()
        self.assertEqual(res["reason"], "config-error")
        self.assertIn("no parseable time", res["config_error"])
        self.put(Q(log="- 2026-11-19 12:00 UTC later"))
        res = self.check()
        self.assertFalse(res["stop"])
        self.assertTrue(res["last_log_future"])

    def test_legacy_local_time_line(self):
        self.put(Q(log="- 2026-11-18 12:05 something"))
        self.assertTrue(self.check(now="12:10", utc="2026-11-18 12:10")["stop"])

    def test_counts_fences_and_numbers(self):
        items = "1. [x] DONE a\n2. [ ] BLOCKED: no\n3. [ ] open one\n```\n4. [ ] fenced\nQUIET_WINDOWS: bogus\n```\n   - [ ] indented open\n\u0661. [ ] arabic digit"
        self.put(Q(items=items))
        res = self.check()
        self.assertEqual(res["items"], {"open": 3, "done": 1, "blocked": 1, "unrecognised": 0})
        self.assertEqual(res["next_item"]["index"], 3)
        self.assertFalse(res["unclosed_fence"])

    def test_unrecognised_and_unclosed_fence(self):
        self.put(Q(items="1. [x] DONEish\n2. no box\n3. [] x\n- [ x] y"))
        res = self.check()
        self.assertEqual(res["items"]["unrecognised"], 4)
        self.assertFalse(res["auto_done_ready"])
        self.put(Q(items="1. [x] DONE a\n```\nstuff"))
        res = self.check()
        self.assertTrue(res["unclosed_fence"])
        self.assertFalse(res["auto_done_ready"])

    def test_no_items_and_missing_queue(self):
        self.put(Q(items="just text"))
        self.assertEqual(self.check()["config_error"], "no items found under ## Items")
        os.remove(self.queue)
        rc, out, _ = self.cmd("check")
        res = json.loads(out)
        self.assertEqual(rc, 2)
        self.assertTrue(res["stop"] and not res["ok"])
        self.assertEqual(sorted(res), sorted(nsq.CHECK_KEYS))

    def test_stable_keys_and_auto_done_ready(self):
        self.put(Q(items="1. [x] DONE a\n2. [ ] BLOCKED: b"))
        res = self.check()
        self.assertEqual(list(res)[:12], list(nsq.CHECK_KEYS))
        self.assertTrue(res["auto_done_ready"])
        self.assertIsNone(res["next_item"])

    def test_items_end_only_at_log_or_operator(self):
        self.put(Q(items="1. [x] DONE a\n## notes for item 2\n2. [ ] still an item"))
        self.assertEqual(self.check()["next_item"]["index"], 2)


class TestLog(Base):
    def test_bytes_preserved_crlf_bom_arabic(self):
        text = "\ufeff" + Q(items="1. [ ] \u0645\u0647\u0645\u0629 \u0639\u0631\u0628\u064a\u0629", eol="\r\n")
        raw = text.encode("utf-8")
        self.put(raw, raw=True)
        rc, out, _ = self.cmd("log", "--kind", "result", "--", "\u062a\u0645 \u0627\u0644\u0639\u0645\u0644 \u2713")
        self.assertEqual(rc, 0, out)
        new = self.get()
        self.assertTrue(new.startswith(raw))
        added = new[len(raw):].decode("utf-8")
        self.assertRegex(added, r"^- [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC \u062a\u0645 \u0627\u0644\u0639\u0645\u0644 \u2713\r\n$")
        self.assertNotIn(b"\r\r", new)
        self.assertEqual(new.count(b"\n"), new.count(b"\r\n"))

    def test_no_trailing_newline_and_lf(self):
        raw = Q().rstrip("\n").encode("utf-8")
        self.put(raw, raw=True)
        self.assertEqual(self.cmd("log", "--", "x")[0], 0)
        new = self.get()
        self.assertTrue(new.startswith(raw + b"\n- "))
        self.assertFalse(new.endswith(b"\n"))
        self.assertNotIn(b"\r", new)

    def test_inserted_at_end_of_log_section_when_not_last(self):
        text = Q().replace("## Log\n- 2026-11-18 01:00 UTC queue created\n", "## Log\n- 2026-11-18 01:00 UTC queue created\n\n## Notes\nkeep\n")
        self.put(text)
        self.assertEqual(self.cmd("log", "--", "added")[0], 0)
        out = self.get().decode("utf-8")
        self.assertLess(out.index("UTC added"), out.index("## Notes"))
        self.assertGreater(out.index("UTC added"), out.index("queue created"))
        self.assertTrue(out.endswith("keep\n"))

    def test_running_line(self):
        self.put(Q())
        self.assertEqual(self.cmd("log", "--kind", "running", "--", "7")[0], 0)
        self.assertIn(" UTC RUNNING item 7\n", self.get().decode())
        self.assertEqual(self.cmd("log", "--kind", "running", "--", "x; rm")[0], 4)

    def test_config_error_and_usage_stop_dedupe(self):
        self.put(Q())
        self.put(Q(q="bogus"))
        for kind, text, marker in (("config-error", None, "CONFIG-ERROR: "), ("usage-stop", "5h=70%", "USAGE STOP: ")):
            extra = ("--", text) if text else ()
            self.assertEqual(self.cmd("log", "--kind", kind, *extra)[0], 0)
            n = self.get().decode().count(marker)
            rc, out, _ = self.cmd("log", "--kind", kind, *extra)
            self.assertEqual(rc, 0)
            self.assertIn("unchanged", out)
            self.assertEqual(self.get().decode().count(marker), n)
        self.assertEqual(self.cmd("log", "--kind", "result", "--", "something else")[0], 0)
        self.assertEqual(self.cmd("log", "--kind", "config-error")[0], 0)
        self.assertEqual(self.get().decode().count("CONFIG-ERROR: "), 2)  # a different line in between: written again
        self.put(self.get().decode().replace("QUIET_WINDOWS: bogus", "QUIET_WINDOWS: also bogus"))
        self.assertEqual(self.cmd("log", "--kind", "config-error")[0], 0)
        self.assertEqual(self.get().decode().count("CONFIG-ERROR: "), 3)  # another reason: written
        self.assertEqual(self.cmd("log", "--kind", "config-error", "--", "caller supplied text")[0], 4)
        self.put(Q())
        rc, out, _ = self.cmd("log", "--kind", "config-error")
        self.assertEqual(rc, 0)
        self.assertIn("nothing logged", out)
        self.assertNotIn("CONFIG-ERROR", self.get().decode())

    def test_config_error_reason_never_comes_from_the_command_line(self):
        evil = "limits : `touch x` $(id)"
        self.put(Q().replace("\n\n## Items", "\n" + evil + "\n\n## Items", 1))
        self.assertEqual(self.cmd("log", "--kind", "config-error")[0], 0)
        self.assertIn("LIMITS is misplaced or misspelled: limits : `touch x` $(id)", self.get().decode())

    def test_works_without_the_utf8_flag(self):
        self.put(Q(items="1. [ ] \u0645\u0647\u0645\u0629"))
        rc, out, _ = self.run_q("log", "--root", self.root, "--", "\u062a\u0645", plain=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("UTC \u062a\u0645\n", self.get().decode("utf-8"))
        rc, out, _ = self.run_q("check", "--root", self.root, plain=True)
        self.assertEqual(rc, 0)
        self.assertIn("\\u0645", out)  # JSON is ASCII-escaped, the console code page cannot break it
        self.assertEqual(json.loads(out)["next_item"]["index"], 1)

    def test_option_injection_and_strict_parsing(self):
        self.put(Q())
        before = self.get()
        rc, out, _ = self.cmd("log", "--", "--root", )  # only text, text is "--root"? one positional: allowed as data
        self.assertEqual(rc, 0, out)
        self.assertIn("UTC --root\n", self.get().decode())
        self.put(Q())
        self.assertEqual(self.cmd("log", "--evil", "x", "--", "t")[0], 4)
        self.assertEqual(self.cmd("log", "--kind=result", "--", "t")[0], 4)
        self.assertEqual(self.cmd("log", "-x", "t")[0], 4)
        self.assertEqual(self.cmd("log", "--kind", "bogus", "--", "t")[0], 4)
        self.assertEqual(self.cmd("log", "--", "a", "b")[0], 4)
        self.assertEqual(self.cmd("log", "--root", self.root, "--", "t")[0], 4)  # option given twice
        self.assertEqual(self.run_q("log", "--", "t")[0], 4)  # no --root
        self.assertEqual(self.run_q("frobnicate", "--root", self.root)[0], 4)
        self.assertEqual(self.get(), before)

    def test_control_chars_newlines_cap_and_reserved_words(self):
        self.put(Q())
        evil = "line one\r\n- 2026-11-18 12:00 UTC FORGED\x01\x1b[31m\u202eevil\u200b\u2028x " + "A" * 500
        self.assertEqual(self.cmd("log", "--", evil)[0], 0)
        out = self.get().decode("utf-8")
        added = out.split("queue created\n", 1)[1]
        self.assertEqual(added.count("\n"), 1)
        for bad in ("\x01", "\x1b", "\u202e", "\u200b", "\u2028", "\r"):
            self.assertNotIn(bad, added)
        self.assertLessEqual(len(added), len("- 2026-11-18 12:00 UTC ") + 300 + 1)
        self.put(Q())
        self.assertEqual(self.cmd("log", "--", "CONFIG-ERROR: fake")[0], 0)
        self.assertIn("UTC text: CONFIG-ERROR: fake", self.get().decode())
        self.assertEqual(self.cmd("log", "--", "\x01\x02")[0], 4)  # nothing left after stripping

    def test_sanitiser_unit(self):
        self.assertEqual(nsq.san("a\u202eb\u200bc\x00d\ue000e\U000e0041f\udc80g"), "abcdefg")
        self.assertEqual(len(nsq.san("x" * 1000)), 300)


class TestEdits(Base):
    def test_tick_and_block(self):
        self.put(Q(items="1. [ ] first\n2. [ ] second\n3. [ ] third"))
        self.assertEqual(self.cmd("tick", "1")[0], 0)
        self.assertEqual(self.cmd("block", "3", "--", "--needs a human\nsecond line")[0], 0)
        out = self.get().decode()
        self.assertIn("1. [x] DONE first\n2. [ ] second\n3. [ ] BLOCKED: --needs a human second line | was: third\n", out)
        res = self.check()
        self.assertEqual(res["items"], {"open": 1, "done": 1, "blocked": 1, "unrecognised": 0})

    def test_refusals_leave_file_untouched(self):
        self.put(Q(items="1. [x] DONE a\n2. [ ] BLOCKED: b\n3. [ ] c\n4. [x] DONEish"))
        before = self.get()
        for args in (("tick", "1"), ("tick", "2"), ("tick", "4"), ("tick", "9"), ("block", "1", "--", "r"), ("block", "2", "--", "r")):
            self.assertEqual(self.cmd(*args)[0], 2, args)
        for args in (("tick", "0x1"), ("tick", "-1"), ("tick", "1; ls"), ("tick",), ("block", "3"), ("block", "3", "--", "\x01")):
            self.assertEqual(self.cmd(*args)[0], 4, args)
        self.assertEqual(self.get(), before)

    def test_edits_keep_crlf_bom_arabic_bytes(self):
        text = "\ufeff" + Q(items="1. [ ] \u0627\u062e\u062a\u0628\u0627\u0631\n2. [ ] two", eol="\r\n")
        raw = text.encode("utf-8")
        self.put(raw, raw=True)
        self.assertEqual(self.cmd("tick", "1")[0], 0)
        new = self.get()
        expect = raw.replace("[ ] \u0627".encode(), "[x] DONE \u0627".encode(), 1)
        self.assertEqual(new, expect)

    def test_tick_in_fence_or_wrong_section_not_an_item(self):
        self.put(Q(items="```\n1. [ ] fenced\n```\n1. [ ] real"))
        self.assertEqual(self.cmd("tick", "1")[0], 0)
        out = self.get().decode()
        self.assertIn("1. [ ] fenced", out)
        self.assertIn("1. [x] DONE real", out)

    def test_done_happy_keeps_bytes_and_logs_blocked(self):
        raw = ("\ufeff" + Q(items="1. [x] DONE a\n2. [ ] BLOCKED: \u0633\u0628\u0628", eol="\r\n")).encode("utf-8")
        self.put(raw, raw=True)
        rc, out, _ = self.cmd("done")
        self.assertEqual(rc, 0, out)
        new = self.get()
        self.assertTrue(new.startswith("\ufeff# Resume queue\r\nSTATUS: DONE\r\n".encode("utf-8")))
        self.assertIn(b"AUTO-DONE", new)
        self.assertIn("item 2: \u0633\u0628\u0628".encode("utf-8"), new)
        self.assertEqual(new.count(b"\n"), new.count(b"\r\n"))
        self.assertEqual(self.cmd("done")[0], 0)  # already DONE: nothing more
        self.assertEqual(self.get(), new)

    def test_done_refusals(self):
        cases = {
            "open item": (Q(), 2, None),
            "unclosed fence": (Q(items="1. [x] DONE a\n```\nx"), 2, "unclosed code fence"),
            "unrecognised": (Q(items="1. [x] DONE a\n2. [x] nope"), 2, "item line not recognised"),
            "numbered no box": (Q(items="1. [x] DONE a\n2. note"), 2, "item line not recognised"),
            "no items": (Q(items="text"), 2, "no items found"),
            "line2 spaces": (Q(items="1. [x] DONE a").replace("STATUS: ACTIVE", "STATUS: ACTIVE ", 1), 2, "could not be set safely"),
            "line2 other": (Q(items="1. [x] DONE a").replace("STATUS: ACTIVE", "STATUS: PAUSED", 1), 2, "could not be set safely"),
        }
        for name, (text, code, msg) in cases.items():
            self.put(text)
            rc, out, _ = self.cmd("done")
            self.assertEqual(rc, code, name)
            if msg:
                self.assertIn(msg, out, name)
                self.assertRegex(self.get().decode(), r"CONFIG-ERROR: .*" + re.escape(msg), name)
            else:
                self.assertEqual(self.get().decode(), text, name)
            self.assertNotIn("STATUS: DONE", self.get().decode(), name)

    def test_done_with_open_item_logs_nothing_even_with_other_problems(self):
        for items in ("1. [ ] a\n2. note", "1. [ ] a\n```\nx", "1. [ ] a\n2. [x] nope"):
            self.put(Q(items=items))
            before = self.get()
            self.assertEqual(self.cmd("done")[0], 2, items)
            self.assertEqual(self.get(), before, items)

    def test_block_keeps_long_item_text_verbatim(self):
        long_item = "x" * 400
        self.put(Q(items="1. [ ] " + long_item))
        self.assertEqual(self.cmd("block", "1", "--", "why")[0], 0)
        self.assertIn("| was: " + long_item + "\n", self.get().decode())

    def test_done_config_error_not_repeated(self):
        self.put(Q(items="text"))
        self.cmd("done")
        once = self.get()
        self.cmd("done")
        self.assertEqual(self.get(), once)

    def test_only_expected_files_exist_afterwards(self):
        self.put(Q())
        self.cmd("tick", "1")
        self.cmd("log", "--", "x")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["Jobs"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Jobs")), ["resume_queue.md"])

    def test_in_place_fallback_when_replace_fails(self):
        self.put(Q())
        with mock.patch.object(nsq.os, "replace", side_effect=OSError("no")), mock.patch.object(nsq, "emit"):
            rc = nsq.main(["tick", "1", "--root", self.root])
        self.assertEqual(rc, 0)
        self.assertIn("1. [x] DONE first", self.get().decode())
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Jobs")), ["resume_queue.md"])

    def test_concurrent_change_is_refused(self):
        self.put(Q())
        data, snap = nsq.read_queue(nsq.confine(self.root))
        time.sleep(0.02)
        self.put(Q() + "- 2026-11-18 02:00 UTC edited by hand\n")
        with self.assertRaises(nsq.Refused):
            nsq.write_queue(nsq.confine(self.root), data + b"x", snap)

    def test_size_cap(self):
        self.put(Q() + "- 2026-11-18 01:00 UTC " + "x" * 1000000 + "\n")
        rc, out, _ = self.cmd("check")
        self.assertEqual(rc, 2)
        self.assertIn("1 MB", out)
        self.assertEqual(self.cmd("log", "--", "x")[0], 2)


class TestLock(Base):
    def setUp(self):
        Base.setUp(self)
        self.put(Q())

    def test_take_and_unlock(self):
        rc, out, _ = self.cmd("lock")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.lock))
        rc, out, _ = self.cmd("lock")
        self.assertEqual(rc, 3)
        self.assertIn("LOCK-HELD", out)
        self.assertEqual(self.cmd("unlock")[0], 0)
        self.assertFalse(os.path.exists(self.lock))
        self.assertEqual(self.cmd("unlock")[0], 0)

    def test_stale_lock_is_removed_and_retaken(self):
        os.mkdir(self.lock)
        old = time.time() - 31 * 60
        os.utime(self.lock, (old, old))
        rc, out, _ = self.cmd("lock")
        self.assertEqual(rc, 0, out)
        self.assertGreater(os.stat(self.lock).st_mtime, old + 60)

    def test_young_lock_is_held(self):
        os.mkdir(self.lock)
        old = time.time() - 29 * 60
        os.utime(self.lock, (old, old))
        self.assertEqual(self.cmd("lock")[0], 3)
        self.assertTrue(os.path.isdir(self.lock))

    def test_refused_locks_are_never_cleared_and_logged_once(self):
        # file
        with open(self.lock, "w") as f:
            f.write("x")
        for _ in range(2):
            rc, out, _ = self.cmd("lock")
            self.assertEqual(rc, 3)
            self.assertIn("LOCK-REFUSED", out)
        self.assertTrue(os.path.isfile(self.lock))
        self.assertEqual(self.get().decode().count("STOPPED: Jobs/.nightshift.lock cannot be cleared"), 1)
        self.assertEqual(self.cmd("unlock")[0], 3)
        os.remove(self.lock)
        # non-empty folder
        os.mkdir(self.lock)
        open(os.path.join(self.lock, "inner"), "w").close()
        old = time.time() - 3600
        os.utime(self.lock, (old, old))
        self.assertEqual(self.cmd("lock")[0], 3)
        self.assertEqual(self.cmd("unlock")[0], 3)
        self.assertTrue(os.path.exists(os.path.join(self.lock, "inner")))
        os.remove(os.path.join(self.lock, "inner"))
        os.rmdir(self.lock)
        # future mtime
        os.mkdir(self.lock)
        fut = time.time() + 3600
        os.utime(self.lock, (fut, fut))
        self.assertEqual(self.cmd("lock")[0], 3)
        self.assertTrue(os.path.isdir(self.lock))

    def test_lock_refuses_missing_jobs_and_never_creates_it(self):
        shutil.rmtree(os.path.join(self.tmp, "Jobs"))
        rc, out, _ = self.cmd("lock")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Jobs")))

    def test_quiet_window_after_lock_removes_lock(self):
        self.put(Q(q="08:30-09:20"))
        rc, out, _ = self.cmd("lock", "--now", "09:00")
        self.assertEqual(rc, 2, out)
        self.assertFalse(os.path.exists(self.lock))
        self.assertEqual(self.get().decode(), Q(q="08:30-09:20"))
        self.assertEqual(self.cmd("lock", "--now", "09:20")[0], 0)

    def test_lock_with_malformed_config_fails_closed(self):
        self.put(Q(q="bogus"))
        self.assertEqual(self.cmd("lock")[0], 2)
        self.assertFalse(os.path.exists(self.lock))

    def test_eight_processes_exactly_one_wins(self):
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, "-X", "utf8", SCRIPT, "lock", "--root", self.root],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(8)]
        codes = []
        for p in procs:
            p.communicate(timeout=60)
            codes.append(p.returncode)
        self.assertEqual(codes.count(0), 1, codes)
        self.assertEqual(codes.count(3), 7, codes)

    def test_stale_race_exactly_one_wins(self):
        os.mkdir(self.lock)
        old = time.time() - 40 * 60
        os.utime(self.lock, (old, old))
        procs = [subprocess.Popen([sys.executable, "-X", "utf8", SCRIPT, "lock", "--root", self.root],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        codes = []
        for p in procs:
            p.communicate(timeout=60)
            codes.append(p.returncode)
        # Known limit (documented): a loser that saw the old folder can remove the winner's fresh lock; assert at least one winner
        self.assertGreaterEqual(codes.count(0), 1, codes)


class TestConfinement(Base):
    def setUp(self):
        Base.setUp(self)
        self.put(Q())

    def test_relative_dotdot_unc_refused(self):
        for bad in ("Jobs", ".", "../x", self.root + "/../x", "//host/share/x", "\\\\host\\share\\x"):
            rc, out, _ = self.run_q("check", "--root", bad)
            self.assertIn(rc, (2, 4), bad)

    def test_validate_root_string_windows_rules(self):
        v = nsq.validate_root_string
        v("C:/work/x/proj", windows=True)
        for bad in ("C:/a/../b", "C:/a/CON", "C:/a/nul.txt", "C:/a/COM1", "C:/a/file:stream", "C:/a/dir. ", "C:/a/b.", "D:", "\\\\h\\s", "rel/path", "C:/a/LPT3.log"):
            with self.assertRaises(nsq.Refused, msg=bad):
                v(bad, windows=True)
        v("/home/x/proj", windows=False)
        with self.assertRaises(nsq.Refused):
            v("/home/../x", windows=False)
        with self.assertRaises(nsq.Refused):
            v("/home/x\x00y", windows=False)

    def test_symlinked_jobs_and_queue_refused(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.mkdir(other)
        shutil.copy(self.queue, os.path.join(other, "resume_queue.md"))
        os.remove(self.queue)
        try:
            os.symlink(os.path.join(other, "resume_queue.md"), self.queue)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks need a privilege on this platform")
        self.assertEqual(self.cmd("log", "--", "x")[0], 2)
        self.assertEqual(self.cmd("tick", "1")[0], 2)
        with open(os.path.join(other, "resume_queue.md"), encoding="utf-8") as f:
            self.assertNotIn("UTC x", f.read())
        os.remove(self.queue)
        shutil.rmtree(os.path.join(self.tmp, "Jobs"))
        try:
            os.symlink(other, os.path.join(self.tmp, "Jobs"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need a privilege on this platform")
        self.assertEqual(self.cmd("lock")[0], 2)
        self.assertEqual(os.listdir(other), ["resume_queue.md"])

    def test_symlinked_root_refused(self):
        link = self.tmp + "_link"
        try:
            os.symlink(self.tmp, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need a privilege on this platform")
        self.addCleanup(os.remove, link) if not os.path.isdir(link) or os.name != "nt" else self.addCleanup(os.rmdir, link)
        rc, out, _ = self.run_q("check", "--root", link.replace("\\", "/"))
        self.assertEqual(rc, 2, out)

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows feature")
    def test_junction_jobs_and_root_refused(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.mkdir(other)
        shutil.copy(self.queue, os.path.join(other, "resume_queue.md"))
        shutil.rmtree(os.path.join(self.tmp, "Jobs"))
        r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(self.tmp, "Jobs"), other], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            self.skipTest("mklink /J failed")
        self.addCleanup(lambda: os.rmdir(os.path.join(self.tmp, "Jobs")))
        for name in ("lock", "unlock"):
            self.assertEqual(self.cmd(name)[0], 2, name)
        self.assertEqual(self.cmd("log", "--", "x")[0], 2)
        self.assertFalse(os.path.exists(os.path.join(other, ".nightshift.lock")))
        # junction as the project root itself
        jroot = self.tmp + "_jroot"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", jroot, other], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            self.skipTest("mklink /J failed")
        self.addCleanup(os.rmdir, jroot)
        self.assertEqual(self.run_q("check", "--root", jroot.replace("\\", "/"))[0], 2)

    def test_hard_linked_queue_refused(self):
        twin = os.path.join(self.tmp, "twin.md")
        try:
            os.link(self.queue, twin)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("hard links not available here")
        before = self.get()
        for args in (("log", "--", "x"), ("tick", "1"), ("done",)):
            self.assertEqual(self.cmd(*args)[0], 2, args)
        self.assertEqual(self.get(), before)
        rc, out, _ = self.cmd("check")
        self.assertEqual(rc, 2)
        self.assertIn("hard link", out)

    def test_untrusted_text_is_not_echoed_raw(self):
        self.put(Q(log="- nonsense \x1b[31m\u202e line"))
        rc, out, _ = self.cmd("check")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)


class TestSourceHygiene(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "queue.py").read_text(encoding="utf-8")

    def test_python38_syntax_only(self):
        ast.parse(self.src, feature_version=(3, 8))
        tree = ast.parse(self.src)
        self.assertFalse([n for n in ast.walk(tree) if n.__class__.__name__ in ("Match", "TryStar")])
        for n in ast.walk(tree):  # no `X | Y` in annotations
            if isinstance(n, (ast.FunctionDef, ast.arg)):
                ann = getattr(n, "annotation", None) or getattr(n, "returns", None)
                self.assertFalse(isinstance(ann, ast.BinOp))

    def test_no_network_shell_or_environment_dump(self):
        for needle in ("socket", "urllib", "http", "subprocess", "os.system", "os.popen", "shell=True", "os.environ.items", "dict(os.environ", "os.environ)", "getenv("):
            self.assertNotIn(needle, self.src, needle)
        self.assertEqual(len(re.findall(r"os\.environ", self.src)), 1)  # the single test-clock switch
        self.assertIn('os.environ.get("NIGHTSHIFT_TEST_CLOCK")', self.src)

    def test_no_personal_paths(self):
        self.assertNotRegex(self.src, r"C:[\\/]+Users|@[a-z0-9-]+\.[a-z]{2,}")

    def test_stdlib_queue_not_imported_by_script(self):
        self.assertNotRegex(self.src, r"^\s*(import|from) queue\b")


if __name__ == "__main__":
    unittest.main()
