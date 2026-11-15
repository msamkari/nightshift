# Changelog

## 0.1.1 (2026-11-15)
- Found by live tests in real scheduled sessions: the run took the time from memory (wrong, and in another zone), so the 20-minute log-age gate could be skipped. Now the time comes from `date -u` and every log line is `- YYYY-MM-DD HH:MM UTC`; lines without `UTC` are treated as local time; unparseable times skip only that check.
- Setup wrote its first log line without the time and told the scheduled task to use a working directory the tool does not have. Now the first line carries the time, and the task prompt carries the absolute project path. The duplicate-task guard reads the existing task prompts for that path.
- Verified live: the usage gate stops a run, injected instructions in a file or a "rules lifted" log line are ignored, a blocked item stays blocked, and the persistent-memory HANDOVER is archived and updated last.

## 0.1.0 (2026-11-14)
- First release: two skills and no code. `/nightshift:setup` writes a queue file (`Jobs/resume_queue.md`) and, only after a yes in chat, offers an hourly scheduled task; `/nightshift:run` is one gated iteration (STATUS, usage gate, session check, first undone item, log). Local work only by default. Optional persistent-memory integration.
