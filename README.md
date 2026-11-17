# nightshift — a Claude Code plugin (v0.1.6)

Keeps unfinished work moving while you are away, without eating your whole plan. A queue file lists small items; an hourly scheduled run does the first undone item locally, logs it, and stops. Before every run it checks your usage and stops if the 5-hour window is at 60% or more or the weekly window at 75% or more (editable in the queue file).

Skills only: no scripts, no network code, nothing installed.

## Use
1. `/nightshift:setup` — tell it what to continue; it writes `Jobs/resume_queue.md` and offers the hourly task (created only if you say yes).
2. `/nightshift:run` — one iteration; the scheduled task calls it, or run it by hand for one step.

## Read this before enabling the schedule
- **No reviewer.** A scheduled run is unattended and may use a permissive permission mode (for example Bypass). The queue file's HARD RULES are text the model follows, not a sandbox: keep items small and local.
- **The run skill carries its own rules** (project-root-only items, no editing of the queue's HARD RULES/LIMITS/Gate/STATUS lines, queue rules can only be stricter, usage thresholds capped at 90%), so removing or softening the queue's HARD RULES does not widen anything. They are still instructions to a model, not a sandbox, and this has not been tested with a live model.
- **A stuck lock stops the run.** If `Jobs/.nightshift.lock` is a file, a junction/symlink, not empty or dated in the future, a run never clears it: it stops, logs one line, and you must delete it yourself.
- **Defaults are conservative:** local work only; never push, open PRs, merge, publish, deploy, spend, install or run downloaded code.
- **Idle cost.** A run that finds nothing to do (STATUS DONE, usage high, another session active) still starts a session and uses a little usage.
- **Kill switch.** Set `STATUS: DONE` in the queue file, or disable the scheduled task. Finished queues stop by themselves.
- Usage gating needs the session-management `get_usage` tool (Claude desktop app). Without it the run falls back to stopping at the first usage-limit reply and continuing next time.

## Quiet windows and gate order (0.1.6)
- **Optional `QUIET_WINDOWS:` line** in the queue header (line 5 in the template): local-time ranges, for example `QUIET_WINDOWS: 08:30-09:20, 12:30-13:20`. Format is exactly `HH:MM-HH:MM` (24-hour, end excluded); a range such as `23:30-00:30` crosses midnight; ranges may overlap. Inside a window the run stops at once and writes nothing (no log line, no lock): the quietest choice, and a log line would only make the next run stop on the 20-minute check. `none`, an empty value or no line means no windows (see CHANGELOG for what else differs from 0.1.5).
- **A malformed value fails closed and leaves one visible line:** the run does nothing else, but appends ONE line `- <time> UTC CONFIG-ERROR: <reason>` to the queue log (only if the last log line is not already the same CONFIG-ERROR, so there is no hourly spam; CONFIG-ERROR lines are ignored by the 20-minute check). Numbers like `830-920`, an en dash, `24:00`, a zero-length range, a trailing comment, backticks (write the value without them), or two `QUIET_WINDOWS:` lines are malformed. A near-miss key (`quiet_windows:`, `- QUIET_WINDOWS:`, fullwidth colon) or the key placed after the header (above the first `##` heading and within the first 15 lines) is a CONFIG-ERROR too, never silently ignored. If the local time cannot be read while windows are set, the run does nothing but the CONFIG-ERROR line. An unparseable last log line is also a CONFIG-ERROR (it must not switch the 20-minute check off).
- **Checked again, not only at start:** the quiet window is re-checked after the run takes its lock (before it logs RUNNING), before any browser step and before the final write. Work already done is not reverted; the item stays unticked and the next run after the window looks at it again. Local time is this machine's clock (a cloud routine runs in UTC). All day: `00:00-12:00, 12:00-00:00`.
- **One definition of an item:** a list line (`-`, `*`, `+`, `1.`, `1)`, Arabic-Indic digits, indented or not) with a `[ ]`, `[x]` or `[X]` checkbox inside `## Items` (outside code fences). DONE and BLOCKED are exact words (`DONEish` does not count). Auto-DONE and the "first open item" rule use this same definition; an unclosed code fence, an unrecognised line or zero items means it never marks DONE and writes a CONFIG-ERROR line. The Items section ends only at `## Log` or `## Operator routine`; another `##` line inside it is plain text. The run changes the queue only with the Claude Code Edit tool (never PowerShell Set-Content), so BOM, CRLF and non-ASCII text stay.
- **Gate order:** the run reads only the first 15 lines of the queue (STATUS, LIMITS, QUIET_WINDOWS), checks the quiet windows, then usage; only then does it read the rest of the queue and project rules. On a usage stop it reads just the last 10 lines of the queue to add the one dated log line. Reading the header first is unavoidable because the thresholds live in the queue.
- **Auto-DONE:** when every item under `## Items` is DONE or BLOCKED, the run changes line 2 from exactly `STATUS: ACTIVE` to `STATUS: DONE` and says so. If line 2 is anything else (trailing spaces, other text) it rewrites nothing and says so. Idle scheduled runs still fire until you disable the task.

## With persistent-memory
If the project has `MDs/HANDOVER.md` and a Jobs log, each run updates them last (archiving first). Otherwise the queue file alone is the record.

## Privacy
The plugin adds no network code and writes only inside your project; the scheduled task itself is stored by the Claude app, and every run is an ordinary Claude session.

## Licence
MIT.
