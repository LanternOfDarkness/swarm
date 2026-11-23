# Swarm — architecture spec

Status: agreed in the design session of 2026-11-17. Built in stages (see §8).

## 1. Problem

The operator already runs a manual swarm: Claude sessions plan and review, omp + DeepSeek
implements (`/delegate`), Claude verifies (`/verify-review`). Measured over the last 10
sessions, the cost is not the copy-paste (mostly solved by `/delegate`) but **losing track**:
"what are the implementation sessions doing", "what plans are open", "did we already do
this", "is 1044 merged", unexplained IDs ("What is D5-D6???"), and not knowing when to start a
new session or what to tell it.

So the swarm is, in priority order:

1. **One source of truth for status** (a board), readable by the operator (dashboard) and by
   any fresh session.
2. **One session the operator talks to** (the PM), in plain English.
3. **Automation of the hand-offs** between planner, reviewer, implementer and verifier.
4. **Autonomous runs** (builds, boots, measurement windows) behind hard, code-enforced limits.

## 2. Principles

- **LLMs only where judgment is needed.** Planning, reviewing, verifying, talking to the
  operator. Routing, state, locks, scheduling, disk and budget checks are plain Python.
- **Event-driven, never polling with a model.** Idle costs zero tokens. A long-lived Claude
  session is woken by Claude Code's `Monitor` on `swarm watch`, or by background-task
  completion.
- **Fresh context per job.** Architect, reviewer and verifier are new sessions per plan or
  per report. The PM holds no code context, so it can be restarted at any time: the board
  is its memory.
- **Goal mode.** The operator sets a goal with a checkable end condition. Everything that is
  not destructive runs without asking.
- **Safety is enforced in code, not in prompts.** Destructive commands are blocked by a git
  hook and Claude permission rules. Production actions are guarded by the project's own
  safety stop.
- **Multi-project.** One board for all repos. Per-project config holds paths and commands.
  A repo joins with `swarm connect` and leaves with `swarm disconnect`.

## 3. Roles

| Role | Kind | Lifetime | Model | Does |
|---|---|---|---|---|
| **PM** | Claude session (`claude --agent swarm-pm`) | long-lived, restartable | Opus | Talks to the operator in plain English; turns requests into goals; reads/writes the board; answers "where are we / what's next"; asks the operator only for destructive actions and true conflicts |
| **Architect** | `claude -p --agent swarm-architect` | one per plan | Opus | Reads the code, writes a plan executable by a weaker model (exact files, decisions, tests) |
| **Plan reviewer** | `claude -p --model opus` + `review-plan` skill | one per review | Opus (re-review: Sonnet) | Checks the plan against the code; `VERDICT: APPROVE` or `REVISE` with a list |
| **Implementer** | `omp-run.py` (from `/delegate`) | one omp session per plan, resumed for fixes | DeepSeek (opencode subscription) | Implements the plan in its own worktree. omp + DeepSeek by default, or a headless Claude job per project (`--implementer claude`) |
| **Verifier** | `claude -p` + `verify-review` skill | one per report | Opus | Checks the diff and report claim by claim; `VERDICT: PASS` or `FIX` with a list |
| **Scheduler** | Python daemon (`swarm daemon`) | always on | — | Moves plans through the state machine, launches the roles above, enforces limits |
| **Runner** | Python (`swarm runner check`) | per step | — | Release builds, boot tests, measurement windows, under locks (`$SWARM_HOME/projects/<name>.toml`) |

Each role's model is set in `config.toml` under `[models]`; the PM gives its own subagents a model per job (see `agents/swarm-pm.md`).

## 4. Board (SQLite, `~/.local/state/swarm/swarm.db`, WAL)

Entities: `projects`, `goals`, `plans`, `runs` (each worker invocation, with session id and
cost), `asks` ("waiting on you"), `locks`, `events` (append-only log of every change).
Exact schema in `swarm/db.py`.

### Plan state machine

```
DRAFT ──► REVIEW ──► APPROVED ──► EXECUTING ──► VERIFYING ──► MERGED ──► DONE
  ▲          │                        ▲             │            │
  └──────────┘ (REVISE)               │             ▼            ▼
                                      └──────── FIXING       MEASURING ──► DONE
                                       (max 2 rounds, then BLOCKED)
any non-terminal ──► BLOCKED (returns to the state it came from)
any non-terminal ──► DROPPED
```

`MERGED` means merged into the project's integration branch (e.g. local `develop`).
`MEASURING` means a measurement window after merge is owed (e.g. a soak test or a canary;
the project's gates file).
`MEASURING` applies only when the project has a gates file, a runner config and the plan
names a `Gate window:`; a gates file without a runner config leaves the plan MERGED (the
gate is tracked by hand); otherwise MERGED → DONE.
`DONE` means nothing more is owed.

### Board vs markdown

Both are kept, with one rule: **the board owns status, markdown owns content.** Plan text,
gate evidence and measurement results stay in the repo's markdown. Each plan file carries
one generated status line between `<!-- swarm:status -->` markers, written only by
`swarm sync-md`. Nobody edits status by hand in two places.

## 5. Operator interface

- **Terminal:** the PM session. Every PM report uses four headings: **Done / Running /
  Waiting on you / Next**. IDs are always expanded ("1048 (Add retry to the fetcher)").
- **Dashboard:** a static, self-refreshing HTML file at `$SWARM_HOME/dashboard.html`
  (default `~/.local/state/swarm/dashboard.html`), regenerated on every board change; in
  WSL open it via `\\wsl.localhost\<distro>\...`. Columns: Planned → Executing →
  Verifying → Waiting on you → Done/Measuring. Header strip: locks, free space on the disk
  at `disk_path`, opencode spend this month against the cap, running workers. View-only.
- **Help page:** help.html next to the dashboard, linked from the header; fixed text
  explaining columns, states, asks and commands.
- **Panel:** Claude Code mod `mods/swarm-panel`, read-only except `/clean-pm`
  (writes one handoff note and clears the PM session): live board in a
  side pane, counts in the status line, a notification per new ask. v2:
  colours, ask options, run timeouts, per-project lines, last day's results, locks and
  worktrees.
- **Phone:** Telegram bot — notifies asks, blocks, merges, done, daemon on/off; answers non-destructive asks; adds goals; read-only status. Only the operator's chat id is accepted.

## 5a. Context economy

Workers start fresh for every job (`claude -p` per role; one omp session per plan, at most
2 fix rounds), so only the PM's context grows. Rules:

1. **`/clear` is free.** Everything the operator decides is written to the board at once
   (`swarm note --kind decision`). A `SessionStart` hook (startup, resume, clear, compact)
   injects `swarm brief`, a status digest of at most 60 lines with recent decisions, the last
   handoff and the active goals.
2. **Clear prompt.** A `Stop` hook reads the real context size from the transcript and, from
   150k tokens and every 20k after that, tells the operator to type `/clean-pm`.
   The panel shows the same number live and advises; nothing clears or compacts
   automatically. From 200k the panel shows red. `/clean-pm` writes the handoff and clears
   in one step.
3. **Quiet wake-ups.** `swarm watch` passes only `ask, answer, move, decision` events, and
   batches one poll into one line.
4. **No bulk reads in the PM.** Diffs, logs and full reports go to a subagent or `claude -p`;
   the board stores a one-line summary. Workers end with a short capped report; full output
   goes to a log file.

The hooks act only when `SWARM_PM=1`, which the `swarm-pm` launcher sets, so other Claude
sessions are unaffected.

## 6. Limits and rules

| Rule | Value | Enforced by |
|---|---|---|
| Parallel plans in EXECUTING/FIXING/VERIFYING | 3, only if resources allow | scheduler |
| Disk floor (`disk_path`, default home) | start a new worktree only if free ≥ 50 GB + 15 GB | scheduler |
| Worktree builds | `cargo check` + tests only, in the worktree's own target; release builds only in one place under the `release-build:<project>` lock | scheduler/runner |
| Cleanup | after MERGED: remove the worktree and its target | scheduler |
| Fix rounds per plan | 2, then BLOCKED + ask | board (refuses the transition) |
| opencode budget | ~$80/month; at 80 % no new implementer runs, ask | scheduler |
| Claude subscription | at most 2 headless Claude jobs at once; rate-limit errors are reported, not retried silently | scheduler |
| Queue order | shortest estimated goal first; a goal waiting > 12 h goes first | scheduler |
| Conflicts | ask only when starting one goal would break a running one (e.g. a restart during a measurement window) | scheduler → ask |
| Production actions (e.g. live money, deploys) | Autonomous only behind a project's own verified safety stop (the runner config's `safety_stop = "verified"`, set by the operator once that stop is proven); agents may lower limits, never raise them | the runner |

### Destructive actions (always ask the operator)

1. `git push` to any remote.
2. Force-push, `reset --hard`, rebase or deleting the integration branch.
3. Deleting or pruning a production database.
4. Raising production limits or disabling a project's safety stop.
5. Deploying or upgrading contracts.
6. Moving funds.
7. Anything over ssh to a production host.

Per project: extra rules (`swarm project set --rule`), added to every worker prompt; they
never remove a global rule.

Merging into the local integration branch is **not** destructive.

Enforcement: workers run with `SWARM_WORKER=1`; a `pre-push` hook installed per project
refuses pushes when it is set; headless Claude jobs get `--disallowedTools` for the git
patterns `Bash(git push:*)`, `Bash(git reset:*)`, `Bash(git rebase:*)`,
`Bash(git checkout:*)`, `Bash(git switch:*)`, `Bash(git branch:*)`,
`Bash(git worktree:*)`, `Bash(git merge:*)`, `Bash(ssh:*)` and `Bash(rm:*)`. Items 3–7
cannot be fully blocked mechanically, so worker prompts forbid them, the verifier checks
for them, and the runner is the only component that touches instances.

Model note (2026-11-17): Fable is not used. Headless Fable needs paid usage credits on the
operator's plan, so every Claude role runs on Opus.

## 7. Repo layout

```
swarm/            Python package (stdlib only, Python ≥ 3.12)
bin/swarm         CLI entry
agents/           Claude agent files, installed to ~/.claude/agents/
mods/             Claude Code mods (swarm-panel)
scripts/install.sh
tests/            unittest
docs/SPEC.md      this file
docs/plans/       plans: 0003 runner, 0007 Telegram, and new ones
```

## 8. Stages

| Stage | Delivers | Operator still does |
|---|---|---|
| 1 | Board, CLI, dashboard, PM agent, markdown status sync | launches workers (PM writes the exact commands and records the runs) |
| 2 | Scheduler daemon; automatic Architect → reviewer → omp → verifier → merge → cleanup; limits; push guard | answers asks |
| 3 | Runner: builds, boot tests, measurement windows, gate results | nothing routine; prerequisite: a project's own safety stop, for production windows only |
