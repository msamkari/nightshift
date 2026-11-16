# Changelog

## 0.1.5 (2026-11-15)
Skills text and tests only (no code), from the round-2 adversarial report:
- The run skill now carries its own hard rules instead of relying on the queue file: items may only read and write inside the project root folder (refuse `..`, absolute paths elsewhere, other drives, UNC, junction/symlink escapes, mark BLOCKED); the queue's HARD RULES/LIMITS/Gate/STATUS lines are never edited (only ticks and log lines; the single `STATUS: DONE` at the end stays); rules found in the queue can only be stricter; a `LIMITS` threshold above 90 counts as 90 (so `5h=100` no longer turns the gate off). The setup template's HARD RULES say the same.
- Stuck lock: if `Jobs/.nightshift.lock` is a file, a junction/symlink, not empty or dated in the future, the run does not try to clear it: it stops, tells the user to remove it, and logs ONE line (not again if the last log line is already that message). A future-dated last log line is reported once and treated as unparseable for the 20-minute check instead of "recent forever".
- Time and shell: native PowerShell uses `[DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm')` (there `date -u` printed local time); the lock is created only when `Jobs/` already exists (PowerShell `mkdir` would create it silently); runs work from the project root.
- Tests: 8 -> 13 (they assert the sentences exist).
- Not verified: everything about model behaviour is predicted from the text; no scheduled run, Bypass mode or PowerShell-only session was run for this release. Still open from the report: the 20-minute gate can be switched off by an unparseable last line (N3), a run longer than 30 minutes loses its lock (N8), line-2 lookalikes (N9) and huge queue files (N10).

## 0.1.4 (2026-11-15)
- Adversarial live round on 0.1.3 (four real scheduled runs): a `STATUS: DONE` below a blank line is still honored, a queue that rewrites its own HARD RULES to allow installs still gets the item BLOCKED, a failing check gives BLOCKED, and `LIMITS: 5h=0` stops on the usage gate; no lock folder was left behind in any case. The queue template written by setup now documents the lock (and how to clear a stale one), so it is no longer a hidden rule.

## 0.1.3 (2026-11-15)
- Live retest of 0.1.2: two scheduled runs that started at the same second both executed item 1, so the read-after-write `RUNNING` claim is not enough. The claim is now an atomic `mkdir Jobs/.nightshift.lock` (fails if another run holds it; a lock older than 30 minutes is treated as a crashed run); the lock is removed at the end. The `RUNNING` line stays as a log line for the 20-minute check.

## 0.1.2 (2026-11-15)
- Strict live tests in real scheduled sessions found two runs started 30 seconds apart on the same queue both executed item 1: the session-list check cannot be relied on. Now a run claims its item with a `RUNNING` log line before working and re-reads the file; later runs stop on the 20-minute log-age check. The session check stays as best effort.
- A failing item check (non-zero exit) now marks the item BLOCKED instead of leaving it to be retried forever.
- Setup now explains that a task saved in the default permission mode stalls at the first approval (use "Run now" once, or pick a mode), that the cadence must be at least about 30 minutes, and that the task starts in the working folder of the creating session (correcting 0.1.1, which said the folder could not be chosen).
- Verified live and unchanged: usage gate stop, 20-minute gate with UTC and local-time lines, a trap `STATUS: DONE` in the log ignored, install and outside-the-project items BLOCKED, injected instructions ignored.

## 0.1.1 (2026-11-15)
- Found by live tests in real scheduled sessions: the run took the time from memory (wrong, and in another zone), so the 20-minute log-age gate could be skipped. Now the time comes from `date -u` and every log line is `- YYYY-MM-DD HH:MM UTC`; lines without `UTC` are treated as local time; unparseable times skip only that check.
- Setup wrote its first log line without the time and told the scheduled task to use a working directory the tool does not have. Now the first line carries the time, and the task prompt carries the absolute project path. The duplicate-task guard reads the existing task prompts for that path.
- Verified live: the usage gate stops a run, injected instructions in a file or a "rules lifted" log line are ignored, a blocked item stays blocked, and the persistent-memory HANDOVER is archived and updated last.

## 0.1.0 (2026-11-14)
- First release: two skills and no code. `/nightshift:setup` writes a queue file (`Jobs/resume_queue.md`) and, only after a yes in chat, offers an hourly scheduled task; `/nightshift:run` is one gated iteration (STATUS, usage gate, session check, first undone item, log). Local work only by default. Optional persistent-memory integration.
