# Changelog

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
