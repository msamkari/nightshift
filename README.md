# nightshift — a Claude Code plugin (v0.1.5)

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

## With persistent-memory
If the project has `MDs/HANDOVER.md` and a Jobs log, each run updates them last (archiving first). Otherwise the queue file alone is the record.

## Privacy
The plugin adds no network code and writes only inside your project; the scheduled task itself is stored by the Claude app, and every run is an ordinary Claude session.

## Licence
MIT.
