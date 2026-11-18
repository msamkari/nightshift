#!/usr/bin/env python3
"""nightshift queue bookkeeping (stdlib only, Python 3.8+, no network, no shell, no environment dump).

Usage:  queue.py <command> --root <absolute project folder> [options] [-- <text>]

Commands (all take --root):
  check                 read-only; prints ONE JSON object (see CHECK_KEYS)
  lock                  atomic mkdir of Jobs/.nightshift.lock (stale rules, quiet-window re-check)
  unlock                remove the lock only if it is an empty real folder
  log [--kind K] -- T   append one log line; K = running|usage-stop|config-error|result|note (default result)
  tick N                item N: [ ] -> [x] DONE (only if currently open)
  block N -- REASON     item N: mark BLOCKED with the reason (only if currently open)
  done                  auto-DONE (line 2 ACTIVE -> DONE) only when every item is DONE or BLOCKED

Exit codes: 0 ok, 2 refused by a rule (the message says which), 3 lock problem, 4 usage error.
Only <root>/Jobs/resume_queue.md and <root>/Jobs/.nightshift.lock are touched (plus a temp file in
<root>/Jobs/ that is renamed over the queue). Text arguments are DATA: control characters are stripped,
the length is capped, nothing is interpreted. Test clock options (--now, --utc-now) are rejected unless
the environment variable NIGHTSHIFT_TEST_CLOCK is exactly 1.
"""
import json
import os
import re
import stat
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

EXIT_OK, EXIT_REFUSED, EXIT_LOCK, EXIT_USAGE = 0, 2, 3, 4
MAX_QUEUE_BYTES = 1000000
TEXT_CAP = 300
HEADER_LINES = 15
STALE_LOCK_MINUTES = 30
LOCK_FUTURE_SLACK_SECONDS = 120
RECENT_LOG_MINUTES = 20
LOCK_NAME = ".nightshift.lock"
QUEUE_NAME = "resume_queue.md"
CHECK_KEYS = ("ok", "stop", "reason", "config_error", "status_done", "in_quiet_window", "limits",
              "last_log_age_minutes", "next_item", "items", "unclosed_fence", "auto_done_ready")


class Refused(Exception):
    def __init__(self, msg, code=EXIT_REFUSED):
        Exception.__init__(self, msg)
        self.msg = msg
        self.code = code


class UsageError(Refused):
    def __init__(self, msg):
        Refused.__init__(self, msg, EXIT_USAGE)


# ---------------------------------------------------------------- output sanitiser
def san(value, cap=TEXT_CAP):
    """The one sanitiser for anything that came from a file or an argument: drops control, format
    (bidi, zero-width, BOM, tag), line/paragraph separator, surrogate and private-use characters, then caps."""
    out = []
    for ch in str(value):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    s = "".join(out)
    return s[:cap]


def emit(text, stream=None):
    stream = stream or sys.stdout
    data = (san(text, 600) + "\n").encode("utf-8", "replace")
    try:
        stream.buffer.write(data)
        stream.buffer.flush()
    except AttributeError:
        stream.write(data.decode("utf-8", "replace"))


def emit_json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True, sort_keys=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- path confinement
_DEVICE = re.compile(r"(?i)^(con|prn|aux|nul|conin\$|conout\$|com[0-9]|lpt[0-9])(\..*)?$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def validate_root_string(raw, windows=None):
    """Lexical checks on the --root text before anything touches the disk."""
    if windows is None:
        windows = os.name == "nt"
    if not raw or "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise UsageError("--root must be a non-empty path without control characters")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise Refused("REFUSED: UNC or network root paths are not allowed")
    if windows:
        if not re.match(r"^[A-Za-z]:[\\/]", raw):
            raise Refused("REFUSED: --root must be an absolute path with a drive letter")
        rest = raw[2:]
    else:
        if not raw.startswith("/"):
            raise Refused("REFUSED: --root must be an absolute path")
        rest = raw
    for part in re.split(r"[\\/]", rest) if windows else rest.split("/"):
        if part == "":
            continue
        if part == "..":
            raise Refused("REFUSED: --root must not contain '..'")
        if windows:
            if ":" in part:
                raise Refused("REFUSED: --root must not contain ':' (alternate data stream)")
            if part != part.rstrip(" ."):
                raise Refused("REFUSED: --root component ends with a space or dot")
            if _DEVICE.match(part):
                raise Refused("REFUSED: --root contains a device name")


def is_link(path):
    """True for symlinks, Windows junctions and any other reparse point (checked without following)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


class Paths(object):
    def __init__(self, root):
        self.root = root
        self.jobs = os.path.join(root, "Jobs")
        self.queue = os.path.join(root, "Jobs", QUEUE_NAME)
        self.lock = os.path.join(root, "Jobs", LOCK_NAME)


def confine(raw_root):
    validate_root_string(raw_root)
    if not os.path.isabs(raw_root):
        raise Refused("REFUSED: --root must be absolute")
    if is_link(raw_root.rstrip("/\\") or raw_root):
        raise Refused("REFUSED: the project folder is a symlink, junction or reparse point")
    try:
        real = os.path.realpath(raw_root)
    except OSError:
        raise Refused("REFUSED: --root cannot be resolved")
    if not os.path.isdir(real):
        raise Refused("REFUSED: --root is not a folder")
    if is_link(real):
        raise Refused("REFUSED: the project folder is a symlink, junction or reparse point")
    p = Paths(real)
    if not os.path.lexists(p.jobs):
        raise Refused("REFUSED: Jobs/ does not exist (it is never created implicitly)")
    if is_link(p.jobs) or not os.path.isdir(p.jobs):
        raise Refused("REFUSED: Jobs/ is not a real folder (symlink, junction or file)")
    return p


def read_queue(p):
    """Open once, check the handle, read. Returns (bytes, snapshot)."""
    if not os.path.lexists(p.queue):
        raise Refused("REFUSED: Jobs/resume_queue.md does not exist", EXIT_REFUSED)
    if is_link(p.queue):
        raise Refused("REFUSED: the queue file is a symlink, junction or reparse point")
    st0 = os.lstat(p.queue)
    if not stat.S_ISREG(st0.st_mode):
        raise Refused("REFUSED: the queue is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p.queue, flags)
    except OSError:
        raise Refused("REFUSED: the queue file cannot be opened")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("REFUSED: the queue is not a regular file")
        if st.st_nlink > 1:
            raise Refused("REFUSED: the queue file has more than one hard link")
        if st.st_size > MAX_QUEUE_BYTES:
            raise Refused("REFUSED: the queue is larger than 1 MB")
        data = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_QUEUE_BYTES:
                raise Refused("REFUSED: the queue is larger than 1 MB")
    finally:
        os.close(fd)
    return data, (st.st_size, st.st_mtime_ns)


def write_queue(p, new, snap):
    """Temp file in Jobs/ + os.replace when the bytes read back are exactly `new`; otherwise in-place."""
    if len(new) > MAX_QUEUE_BYTES:
        raise Refused("REFUSED: the result would be larger than 1 MB")
    if is_link(p.queue) or is_link(p.jobs):
        raise Refused("REFUSED: the queue or Jobs/ became a link")
    st = os.lstat(p.queue)
    if (st.st_size, st.st_mtime_ns) != snap or st.st_nlink > 1 or not stat.S_ISREG(st.st_mode):
        raise Refused("REFUSED: the queue changed while it was being updated; nothing written, run again")
    tmp = os.path.join(p.jobs, ".nightshift.tmp.%d.%s" % (os.getpid(), os.urandom(4).hex()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    done = False
    try:
        fd = os.open(tmp, flags, 0o600)
        try:
            view = memoryview(new)
            while len(view):
                n = os.write(fd, view)
                view = view[n:]
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)
        with open(tmp, "rb") as f:
            same = f.read() == new
        if same:
            try:
                os.chmod(tmp, stat.S_IMODE(st.st_mode))
            except OSError:
                pass
            os.replace(tmp, p.queue)
            done = True
    except OSError:
        done = False
    finally:
        if not done:
            try:
                if os.path.lexists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    if done:
        return "replace"
    with open(p.queue, "r+b") as f:  # in-place byte-level fallback
        f.seek(0)
        f.write(new)
        f.truncate()
    return "in-place"


# ---------------------------------------------------------------- parsing (the written definitions)
_ENTRY = re.compile(r"([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})")
_DIGITS = "0-9٠-٩۰-۹"
FENCE = re.compile(r"^[ \t]*(```|~~~)")
ITEM = re.compile(r"^([ \t]*(?:[-*+]|[%s]+[.)])[ \t]*)\[( |x|X)\](.*)$" % _DIGITS)
LISTLINE = re.compile(r"^[ \t]*(?:[-*+]|[%s]+[.)])(?:[ \t]|\[)" % _DIGITS)
NUMBERED = re.compile(r"^[ \t]*[%s]+[.)]" % _DIGITS)
BRACKET = re.compile(r"^[ \t]*(?:[-*+]|[%s]+[.)])[ \t]*\[" % _DIGITS)
NEAR = {
    "QUIET_WINDOWS": re.compile(r"(?i)^[^\w\n]*quiet[_ ]?windows[^\w\n]*[:：]"),
    "LIMITS": re.compile(r"(?i)^[^\w\n]*limits[^\w\n]*[:：]"),
}
ITEMS_END = re.compile(r"^## (Log|Operator routine)\b")
KILL = re.compile(r"(?i)^\W*status\W*:?\s*done\b")
LOGTIME = re.compile(r"^- ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})( UTC)?")
CONFIG_LINE = re.compile(r"^- (?:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}(?: UTC)?|unknown-time) CONFIG-ERROR: ")
RESERVED = re.compile(r"^(RUNNING|USAGE STOP|CONFIG-ERROR|STOPPED|AUTO-DONE)\b")


def decode(data):
    return data.decode("utf-8", "surrogateescape")


def split_lines(text):
    return [l[:-1] if l.endswith("\r") else l for l in text.split("\n")]


def fence_map(lines):
    out, ch = [], None
    for l in lines:
        m = FENCE.match(l)
        if m:
            out.append(True)
            if ch is None:
                ch = m.group(1)
            elif ch == m.group(1):
                ch = None
        else:
            out.append(ch is not None)
    return out, ch is not None


def header_end(lines):
    fm, _ = fence_map(lines[:HEADER_LINES])
    for i, l in enumerate(lines[:HEADER_LINES]):
        if l.startswith("## ") or fm[i] or LISTLINE.match(l):
            return i
    return min(len(lines), HEADER_LINES)


def items_heading(lines, fm):
    for i, l in enumerate(lines):
        if not fm[i] and l.rstrip(" \t") == "## Items":
            return i
    return None


def config_scan(text, whole=True):
    lines = split_lines(text)
    if lines and lines[0].startswith("﻿"):
        lines[0] = lines[0][1:]
    if not whole:
        lines = lines[:HEADER_LINES]
    fm, _ = fence_map(lines)
    he = header_end(lines)
    errors, found = [], {"QUIET_WINDOWS": [], "LIMITS": []}
    for i in range(he):
        for key in found:
            if lines[i].startswith(key + ":"):
                found[key].append(lines[i][len(key) + 1:])
            elif NEAR[key].match(lines[i]):
                errors.append("%s is misplaced or misspelled: %s" % (key, san(lines[i][:40])))
    for key, vals in found.items():
        if len(vals) > 1:
            errors.append("%s appears more than once in the header" % key)
    ih = items_heading(lines, fm)
    for i in range(he, len(lines)):
        if fm[i] or ITEM.match(lines[i]):
            continue
        if not (i < HEADER_LINES or (ih is not None and i < ih)):
            continue
        for key in found:
            if lines[i].startswith(key + ":") or NEAR[key].match(lines[i]):
                errors.append("%s is misplaced or misspelled: %s" % (key, san(lines[i][:40])))
    q = found["QUIET_WINDOWS"][0] if len(found["QUIET_WINDOWS"]) == 1 else None
    lim = found["LIMITS"][0] if len(found["LIMITS"]) == 1 else None
    return {"quiet": q, "limits": lim, "errors": errors}


def parse_quiet(text, whole=True):
    """('none', []) | ('malformed', reason) | ('ok', [(start_min, end_min), ...])."""
    cfg = config_scan(text, whole)
    if cfg["errors"]:
        return ("malformed", cfg["errors"][0])
    value = cfg["quiet"]
    if value is None:
        return ("none", [])
    value = value.strip(" \t")
    if value == "" or value.lower() == "none":
        return ("none", [])
    if "`" in value:
        return ("malformed", "QUIET_WINDOWS value contains backticks, write it without them")
    wins = []
    for entry in value.split(","):
        m = _ENTRY.fullmatch(entry.strip(" \t"))
        if not m:
            return ("malformed", "QUIET_WINDOWS is malformed (%s)" % san(value, 80))
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or h1 * 60 + m1 == h2 * 60 + m2:
            return ("malformed", "QUIET_WINDOWS is malformed (%s)" % san(value, 80))
        wins.append((h1 * 60 + m1, h2 * 60 + m2))
    return ("ok", wins)


def in_quiet(wins, hhmm):
    h, m = hhmm.split(":")
    t = int(h) * 60 + int(m)
    for a, b in wins:
        if (a < b and a <= t < b) or (a > b and (t >= a or t < b)):
            return True
    return False


def parse_limits(text):
    five, week = 60, 75
    cfg = config_scan(text, True)
    val = cfg["limits"]
    if val and not cfg["errors"]:
        m5 = re.search(r"(?<![\w])5h=([0-9]{1,3})(?![0-9])", val)
        mw = re.search(r"(?<![\w])week=([0-9]{1,3})(?![0-9])", val)
        if m5:
            five = int(m5.group(1))
        if mw:
            week = int(mw.group(1))
    return {"five_hour": min(five, 90), "weekly": min(week, 90)}


def items_state(text):
    """The single Item definition. items = list of dicts(kind, idx (line index), n (1-based))."""
    lines = split_lines(text)
    fm, unclosed = fence_map(lines)
    in_items, items, extra = False, [], []
    for i, l in enumerate(lines):
        if fm[i]:
            continue
        if l.rstrip(" \t") == "## Items":
            in_items = True
            continue
        if ITEMS_END.match(l):
            in_items = False
            continue
        if not in_items:
            continue
        m = ITEM.match(l)
        if m:
            box, rest = m.group(2), m.group(3).lstrip(" \t")
            if box in "xX":
                kind = "done" if re.match(r"DONE(?!\w)", rest) else "unrecognised"
            else:
                kind = "blocked" if re.match(r"BLOCKED(?!\w)", rest) else "open"
            items.append({"kind": kind, "idx": i, "n": len(items) + 1})
        elif NUMBERED.match(l) or BRACKET.match(l):
            extra.append(i)
    return {"lines": lines, "items": items, "extra": extra, "unclosed_fence": unclosed}


def first_unrecognised(st):
    cands = [it["idx"] for it in st["items"] if it["kind"] == "unrecognised"] + list(st["extra"])
    return min(cands) if cands else None


def log_section(lines):
    """(heading_index, end_index_exclusive) of the Log section or (None, None)."""
    fm, _ = fence_map(lines)
    start = None
    for i, l in enumerate(lines):
        if not fm[i] and re.match(r"^## Log\b", l):
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not fm[j] and lines[j].startswith("## "):
            end = j
            break
    return start, end


def log_lines(lines):
    s, e = log_section(lines)
    if s is None:
        return []
    fm, _ = fence_map(lines)
    return [lines[i] for i in range(s + 1, e) if not fm[i] and lines[i].startswith("- ")]


# ---------------------------------------------------------------- clock
def clocks(opts):
    """Returns (utc_now, local_now) naive datetimes. Injection only with NIGHTSHIFT_TEST_CLOCK=1."""
    inj_now, inj_utc = opts.get("now"), opts.get("utc-now")
    if (inj_now or inj_utc) and os.environ.get("NIGHTSHIFT_TEST_CLOCK") != "1":
        raise UsageError("--now and --utc-now are only accepted in test mode")
    utc = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    local = datetime.now().replace(second=0, microsecond=0)
    if inj_utc:
        try:
            utc = datetime.strptime(inj_utc, "%Y-%m-%d %H:%M")
        except ValueError:
            raise UsageError("--utc-now must be 'YYYY-MM-DD HH:MM'")
        local = utc
    if inj_now:
        m = re.fullmatch(r"([0-9]{2}):([0-9]{2})", inj_now)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise UsageError("--now must be HH:MM")
        local = local.replace(hour=int(m.group(1)), minute=int(m.group(2)))
    return utc, local


def stamp(utc):
    return utc.strftime("%Y-%m-%d %H:%M") + " UTC"


# ---------------------------------------------------------------- check
def last_log_age(lines, utc, local):
    """Returns (state, minutes, reason). state: none|ok|recent|future|bad."""
    real = [l for l in log_lines(lines) if not CONFIG_LINE.match(l)]
    if not real:
        return "none", None, None
    last = real[-1]
    m = LOGTIME.match(last)
    if not m:
        return "bad", None, "last log line has no parseable time: %s" % san(last[:40])
    try:
        then = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return "bad", None, "last log line has no parseable time: %s" % san(last[:40])
    now = utc if m.group(2) else local
    if then > now:
        return "future", None, None
    minutes = int((now - then).total_seconds() // 60)
    return ("recent" if (now - then).total_seconds() < RECENT_LOG_MINUTES * 60 else "ok"), minutes, None


def evaluate(data, utc, local):
    text = decode(data)
    lines = split_lines(text)
    st = items_state(text)
    counts = {"open": 0, "done": 0, "blocked": 0, "unrecognised": 0}
    for it in st["items"]:
        counts[it["kind"]] += 1
    counts["unrecognised"] += len(st["extra"])
    first_open = next((it for it in st["items"] if it["kind"] == "open"), None)
    line2 = lines[1] if len(lines) > 1 else ""
    status_done = bool(len(lines) > 1 and KILL.match(line2))
    res = {
        "ok": True, "stop": False, "reason": None, "config_error": None, "status_done": status_done,
        "in_quiet_window": False, "limits": parse_limits(text), "last_log_age_minutes": None,
        "next_item": None, "items": counts, "unclosed_fence": st["unclosed_fence"],
        "auto_done_ready": False,
    }
    if first_open is not None:
        res["next_item"] = {"index": first_open["n"], "text": san(lines[first_open["idx"]].strip())}
    res["auto_done_ready"] = (
        line2 == "STATUS: ACTIVE" and bool(st["items"]) and counts["open"] == 0 and counts["unrecognised"] == 0
        and not st["unclosed_fence"])
    if status_done:
        res.update(stop=True, reason="status-done")
        return res
    kind, wins = parse_quiet(text, whole=False)
    if kind == "malformed":
        res.update(stop=True, reason="config-error", config_error=wins)
        return res
    if kind == "ok":
        res["in_quiet_window"] = in_quiet(wins, local.strftime("%H:%M"))
        if res["in_quiet_window"]:
            res.update(stop=True, reason="quiet-window")
            return res
    errs = config_scan(text, True)["errors"]
    if errs:
        res.update(stop=True, reason="config-error", config_error=errs[0])
        return res
    state, minutes, why = last_log_age(lines, utc, local)
    res["last_log_age_minutes"] = minutes
    if state == "bad":
        res.update(stop=True, reason="config-error", config_error=why)
        return res
    if state == "future":
        res["last_log_future"] = True
    if state == "recent":
        res.update(stop=True, reason="recent-log")
        return res
    if not st["items"]:
        res.update(stop=True, reason="config-error", config_error="no items found under ## Items")
    return res


def cmd_check(p, opts, pos):
    utc, local = clocks(opts)
    try:
        data, _ = read_queue(p)
    except Refused as e:
        out = {k: None for k in CHECK_KEYS}
        out.update(ok=False, stop=True, reason=san(e.msg))
        emit_json(out)
        return e.code
    emit_json(evaluate(data, utc, local))
    return EXIT_OK


# ---------------------------------------------------------------- log
def make_log_line(kind, text, utc):
    if kind == "running":
        if not re.fullmatch(r"[0-9]{1,6}", text or ""):
            raise UsageError("log --kind running needs the item number as its text")
        return "- %s RUNNING item %d" % (stamp(utc), int(text))
    text = san(" ".join(text.split()) if text else "")
    if not text:
        raise UsageError("log needs non-empty text")
    if kind == "usage-stop":
        return "- %s USAGE STOP: %s" % (stamp(utc), text)
    if kind == "config-error":
        return "- %s CONFIG-ERROR: %s" % (stamp(utc), text)
    if kind == "note":
        return "- %s STOPPED: %s" % (stamp(utc), text)
    if RESERVED.match(text):
        text = "text: " + text
    return "- %s %s" % (stamp(utc), text)


def insert_log(text, line):
    """Insert one line at the end of the Log section, keeping every other byte (CRLF or LF)."""
    lines = text.split("\n")
    parts = split_lines(text)
    s, e = log_section(parts)
    if s is None or e == len(parts):
        if lines[-1] == "":
            crlf = len(lines) > 1 and lines[-2].endswith("\r")
            lines.insert(len(lines) - 1, line + ("\r" if crlf else ""))
        else:
            crlf = len(lines) > 1 and lines[-2].endswith("\r") if len(lines) > 1 else False
            if crlf:
                lines[-1] += "\r"
            lines.append(line)
        return "\n".join(lines)
    j = e
    while j > s + 1 and parts[j - 1].strip() == "":
        j -= 1
    crlf = lines[j - 1].endswith("\r")
    lines.insert(j, line + ("\r" if crlf else ""))
    return "\n".join(lines)


def is_duplicate(kind, line, lines_in_log):
    if not lines_in_log:
        return False
    last = lines_in_log[-1]
    if kind in ("config-error", "note"):
        key = line[line.index("UTC ") + 4:]  # "CONFIG-ERROR: reason" or "STOPPED: reason"
        return last.endswith(key)
    if kind == "usage-stop":
        return bool(re.match(r"^- \S+ \S+ UTC USAGE STOP:", last))
    return False


def append_log(p, kind, text, utc):
    data, snap = read_queue(p)
    line = make_log_line(kind, text, utc)
    parts = split_lines(decode(data))
    if is_duplicate(kind, line, log_lines(parts)):
        return "unchanged (same as the last log line)"
    new = insert_log(decode(data), line).encode("utf-8", "surrogateescape")
    write_queue(p, new, snap)
    return "logged"


def cmd_log(p, opts, pos):
    kind = opts.get("kind", "result")
    if kind not in ("running", "usage-stop", "config-error", "result", "note"):
        raise UsageError("unknown --kind")
    if len(pos) != 1:
        raise UsageError("log takes exactly one text argument (put it after --)")
    utc, _ = clocks(opts)
    emit("OK: " + append_log(p, kind, pos[0], utc))
    return EXIT_OK


# ---------------------------------------------------------------- tick / block / done
def edit_item(p, n_text, mode, reason, utc):
    if not re.fullmatch(r"[0-9]{1,6}", n_text or ""):
        raise UsageError("the item number must be a positive integer")
    n = int(n_text)
    data, snap = read_queue(p)
    text = decode(data)
    st = items_state(text)
    item = next((it for it in st["items"] if it["n"] == n), None)
    if item is None:
        raise Refused("REFUSED: there is no item %d" % n)
    if item["kind"] != "open":
        raise Refused("REFUSED: item %d is not open (it is %s)" % (n, item["kind"]))
    raw = text.split("\n")
    piece = raw[item["idx"]]
    cr = piece.endswith("\r")
    body = piece[:-1] if cr else piece
    m = ITEM.match(body)
    prefix, rest = m.group(1), m.group(3)
    if mode == "tick":
        sep = "" if (rest == "" or rest[0] in " \t") else " "
        new_body = prefix + "[x] DONE" + sep + rest
    else:
        orig = san(rest.strip(), 150)
        new_body = prefix + "[ ] BLOCKED: " + reason + (" | was: " + orig if orig else "")
    raw[item["idx"]] = new_body + ("\r" if cr else "")
    new = "\n".join(raw).encode("utf-8", "surrogateescape")
    write_queue(p, new, snap)
    return "item %d marked %s" % (n, "DONE" if mode == "tick" else "BLOCKED")


def cmd_tick(p, opts, pos):
    if len(pos) != 1:
        raise UsageError("tick takes exactly one item number")
    utc, _ = clocks(opts)
    emit("OK: " + edit_item(p, pos[0], "tick", None, utc))
    return EXIT_OK


def cmd_block(p, opts, pos):
    if len(pos) != 2:
        raise UsageError("block takes an item number and a reason (reason after --)")
    reason = san(" ".join(pos[1].split()), 200)
    if not reason:
        raise UsageError("block needs a non-empty reason")
    utc, _ = clocks(opts)
    emit("OK: " + edit_item(p, pos[0], "block", reason, utc))
    return EXIT_OK


def cmd_done(p, opts, pos):
    if pos:
        raise UsageError("done takes no arguments")
    utc, _ = clocks(opts)
    data, snap = read_queue(p)
    text = decode(data)
    lines = text.split("\n")
    parts = split_lines(text)
    if len(parts) > 1 and KILL.match(parts[1]):
        emit("OK: line 2 is already DONE, nothing changed")
        return EXIT_OK
    st = items_state(text)
    items = st["items"]
    reason = None
    if st["unclosed_fence"]:
        reason = "unclosed code fence"
    else:
        bad = first_unrecognised(st)
        if bad is not None:
            reason = "item line not recognised: %s" % san(parts[bad][:40])
        elif not items:
            reason = "no items found under ## Items"
    if reason is None and any(it["kind"] == "open" for it in items):
        raise Refused("REFUSED: an open item remains, auto-DONE does nothing")
    if reason is None and (len(parts) < 2 or parts[1] != "STATUS: ACTIVE"):
        reason = "queue is finished but line 2 could not be set safely"
    if reason is not None:
        try:
            append_log(p, "config-error", reason, utc)
        except Refused:
            pass
        emit("CONFIG-ERROR: " + reason)
        return EXIT_REFUSED
    lines[1] = "STATUS: DONE" + ("\r" if lines[1].endswith("\r") else "")
    blocked = []
    for it in items:
        if it["kind"] == "blocked":
            m = ITEM.match(parts[it["idx"]])
            blocked.append("item %d: %s" % (it["n"], re.sub(r"^BLOCKED:?\s*", "", san(m.group(3).strip(), 100))))
    msg = "- %s AUTO-DONE: every item is DONE or BLOCKED, line 2 set to STATUS: DONE" % stamp(utc)
    if blocked:
        msg += "; BLOCKED: " + "; ".join(blocked)
    msg = san(msg, 900)
    new_text = insert_log("\n".join(lines), msg)
    write_queue(p, new_text.encode("utf-8", "surrogateescape"), snap)
    emit("OK: line 2 set to STATUS: DONE")
    return EXIT_OK


# ---------------------------------------------------------------- lock
def lock_state(path):
    """'none' | 'bad' | 'future' | ('empty', age_minutes)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "none"
    except OSError:
        return "bad"
    if is_link(path) or not stat.S_ISDIR(st.st_mode):
        return "bad"
    try:
        if os.listdir(path):
            return "bad"
    except OSError:
        return "bad"
    now = datetime.now(timezone.utc).timestamp()
    if st.st_mtime > now + LOCK_FUTURE_SLACK_SECONDS:
        return "bad"
    return ("empty", (now - st.st_mtime) / 60.0)


def quiet_inside_now(p, opts):
    utc, local = clocks(opts)
    try:
        data, _ = read_queue(p)
    except Refused as e:
        return True, e.msg
    text = decode(data)
    kind, wins = parse_quiet(text, whole=False)
    if kind == "malformed":
        return True, "config: " + wins
    if kind == "ok" and in_quiet(wins, local.strftime("%H:%M")):
        return True, "inside a quiet window"
    return False, ""


def cmd_lock(p, opts, pos):
    if pos:
        raise UsageError("lock takes no arguments")
    utc, _ = clocks(opts)
    for attempt in (1, 2):
        try:
            os.mkdir(p.lock)
            break
        except FileExistsError:
            state = lock_state(p.lock)
            if state == "bad":
                try:
                    append_log(p, "note", "Jobs/.nightshift.lock cannot be cleared by a run; the user must remove it", utc)
                except Refused:
                    pass
                emit("LOCK-REFUSED: Jobs/.nightshift.lock is a file, link, non-empty or dated in the future; remove it by hand")
                return EXIT_LOCK
            if state == "none":
                continue
            if state[1] < STALE_LOCK_MINUTES or attempt == 2:
                emit("LOCK-HELD: another run holds Jobs/.nightshift.lock (%d minutes old)" % int(state[1]))
                return EXIT_LOCK
            again = lock_state(p.lock)  # re-check right before removing
            if again == "none" or (again != "bad" and again[1] >= STALE_LOCK_MINUTES):
                try:
                    os.rmdir(p.lock)
                except OSError:
                    pass
            else:
                emit("LOCK-HELD: another run holds Jobs/.nightshift.lock")
                return EXIT_LOCK
        except OSError:
            emit("LOCK-REFUSED: the lock folder cannot be created")
            return EXIT_LOCK
    else:
        emit("LOCK-HELD: another run won the lock")
        return EXIT_LOCK
    inside, why = quiet_inside_now(p, opts)
    if inside:
        try:
            os.rmdir(p.lock)
        except OSError:
            pass
        emit("REFUSED: %s, lock removed, write nothing" % why)
        return EXIT_REFUSED
    emit("OK: lock taken")
    return EXIT_OK


def cmd_unlock(p, opts, pos):
    if pos:
        raise UsageError("unlock takes no arguments")
    if not os.path.lexists(p.lock):
        emit("OK: no lock present")
        return EXIT_OK
    if is_link(p.lock) or not os.path.isdir(p.lock) or os.listdir(p.lock):
        emit("LOCK-REFUSED: Jobs/.nightshift.lock is not an empty real folder; remove it by hand")
        return EXIT_LOCK
    try:
        os.rmdir(p.lock)
    except OSError:
        emit("LOCK-REFUSED: the lock folder cannot be removed")
        return EXIT_LOCK
    emit("OK: lock removed")
    return EXIT_OK


# ---------------------------------------------------------------- strict argument parsing
COMMANDS = {
    "check": (cmd_check, ("root", "now", "utc-now"), ()),
    "lock": (cmd_lock, ("root", "now", "utc-now"), ()),
    "unlock": (cmd_unlock, ("root",), ()),
    "log": (cmd_log, ("root", "kind", "now", "utc-now"), ()),
    "tick": (cmd_tick, ("root", "utc-now"), ()),
    "block": (cmd_block, ("root", "utc-now"), ()),
    "done": (cmd_done, ("root", "utc-now"), ()),
}


def parse_args(argv):
    if not argv:
        raise UsageError("missing command; one of: " + ", ".join(sorted(COMMANDS)))
    cmd = argv[0]
    if cmd not in COMMANDS:
        raise UsageError("unknown command")
    allowed = COMMANDS[cmd][1]
    opts, pos, i, only_pos = {}, [], 1, False
    while i < len(argv):
        a = argv[i]
        if only_pos:
            pos.append(a)
        elif a == "--":
            only_pos = True
        elif a.startswith("--"):
            name = a[2:]
            if "=" in name:
                raise UsageError("write options as '--name value', not '--name=value'")
            if name not in allowed:
                raise UsageError("unknown option for this command")
            if name in opts:
                raise UsageError("option given twice")
            if i + 1 >= len(argv):
                raise UsageError("option needs a value")
            opts[name] = argv[i + 1]
            i += 1
        elif a.startswith("-") and len(a) > 1:
            raise UsageError("unknown option (put text that starts with '-' after --)")
        else:
            pos.append(a)
        i += 1
    if "root" not in opts:
        raise UsageError("--root <absolute project folder> is required")
    return cmd, opts, pos


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        cmd, opts, pos = parse_args(argv)
        p = confine(opts["root"])
        return COMMANDS[cmd][0](p, opts, pos)
    except Refused as e:
        if cmd_is_check(argv) and not isinstance(e, UsageError):
            out = {k: None for k in CHECK_KEYS}
            out.update(ok=False, stop=True, reason=san(e.msg))
            emit_json(out)
            return e.code
        emit(("USAGE: " if e.code == EXIT_USAGE else "") + e.msg)
        return e.code
    except OSError as e:
        emit("REFUSED: file system error (%s)" % san(e.__class__.__name__, 40))
        return EXIT_REFUSED


def cmd_is_check(argv):
    return bool(argv) and argv[0] == "check"


if __name__ == "__main__":
    sys.exit(main())
