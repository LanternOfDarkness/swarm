# 0003 — Runner: builds, boot tests and measurement windows (Stage 3)

Spec: `docs/SPEC.md` §3 (Runner row), §4 (`MEASURING`), §6 (production row). Builds on
the earlier stages (merged). Baseline: `1f01ded`; every line reference holds there.
Written for an implementation agent: every decision is made here. If something is not
covered, choose the simplest option and list it under DEVIATIONS. Do not add features
that are not listed.

## Goal

After a plan is `MERGED`, the swarm measures it without the operator when the project
asks for it: it runs the project's build and boot-test steps, runs a **measurement
window** (e.g. a soak test or a canary) for a set time, reads the result, and has a
Claude job write the gate result into the project's gates file. A pass moves the plan to
`DONE`; a fail moves it to `DONE` with a follow-up goal. Production windows (a live
environment) start on their own **only** when the project's own safety stop is marked
verified by the operator; otherwise each one is an ask.

## Today (facts at `1f01ded`)

- `MERGED → MEASURING → DONE` exist (`swarm/states.py:13-14`), but nothing moves a plan
  into `MEASURING`: `_merge_step` moves a plan straight to `DONE` when the project has
  no gates file (`swarm/scheduler.py:527-529`) and leaves it `MERGED` otherwise.
- The plan pass only looks at plans whose goal is `ACTIVE`
  (`swarm/scheduler.py:663-681`); answering "done" to the goal ask moves `MERGED` plans
  to `DONE` (`:639-660`). So a `MEASURING` plan needs its own pass.
- Runs already have a `runner` kind (`swarm/states.py:22`), used by `preverify`
  (`swarm/scheduler.py:18`, `:304-313`): a plain `sh -c` command under `runwrap`, judged
  by exit code. Runner steps reuse exactly that path.
- Locks live in the `locks` table (`swarm/board.py:490-530`) and already show in
  `swarm status` and the dashboard header (`swarm/status.py:416-430`,
  `swarm/dashboard.py:333-341`).
- The reaper kills a run at `timeout_s + 300` s (`swarm/scheduler.py:184-188`) and
  ignores runs with `timeout_s` NULL.
- `_main_ready` (`swarm/scheduler.py:830-846`) checks the main checkout before a Claude
  job commits a file there (the architect does this today).

## Global decisions

- **Runner config is operator-owned and outside every repo:**
  `$SWARM_HOME/projects/<project>.toml`. Swarm only reads it, fresh on every tick; no
  worker, Claude job or swarm command writes it. A project without this file never
  measures (today's behaviour).
- **Format** (TOML; `tomllib`):
  ```toml
  safety_stop = "verified"          # optional; only the operator writes it
  stop_pattern = "SAFETY STOP"      # optional regex, watched in window logs

  [steps.build]
  cmd = "make release"
  timeout = "30m"                   # parse_duration format, default "30m"
  locks = ["release-build"]         # optional; names get ":<project>" appended

  [steps.boot_test]
  cmd = "./scripts/boot-test.sh"
  timeout = "10m"

  [windows.soak]
  before = ["build", "boot_test"]   # step names, run in order; default []
  start = "./scripts/run.sh --mode canary"
  length = "2h"
  stop = "./scripts/stop.sh"        # optional; run after the window process is ended
  read = "./scripts/read-gate.sh soak"
  production = false                # default false
  ```
  Commands run with `sh -c` in the project's `repo_path` (the main checkout, on the
  integration branch, which holds the merged code), with `procs.worker_env`.
- **Which plans measure:** a plan measures only if all three hold: the project has a
  gates file, the project has a runner config, and the plan's markdown has a line
  matching `^Gate window:\s*(\S+)\s*$` naming a window in the config. No gates file →
  `DONE` with the note `no gates file`. No runner config → the plan stays `MERGED`, the
  goal-done ask is still created (today's behaviour). No `Gate window:` line → `DONE`
  with the note `no gate window`. A named window that is not in the config → the plan
  is blocked: `Plan <label> names gate window <w>, which is not in <path>.`
- **Locks:** every window holds `instance:<project>` (one window per project at a time);
  a step holds the locks it lists, suffixed `:<project>`. Holder is `run:<run id>`.
  Order: first check that every needed lock is free (no row in `locks`); a busy lock →
  skip with `<lock> busy`, retry next tick. Then `_launch`; if it returns a run id, take
  each lock with holder `run:<id>`. Locks are released when the run ends, by any path
  (`_end`). The single daemon makes check-then-take safe.
- **Merges wait for builds:** `_merge_step` defers (like it does for an architect run,
  `swarm/scheduler.py:505-507`) while `release-build:<project>` is held, with
  `merge of <label> deferred: a release build is running`.
- **Order:** among `MEASURING` plans waiting to start a window in the same project, the
  shortest window goes first (then plan id). Two windows of one project never overlap;
  there is no ask for that (the outline's "conflict ask" is dropped, see Changes).
- **No new plan state and no new transition.** A failed gate becomes a follow-up goal
  and the plan goes to `DONE` (note `gate FAIL; follow-up goal #N`), because a merged
  plan cannot return to `DRAFT`.
- **Safety:** swarm never edits limits; the runner only runs the configured commands.
  The only way production windows start without an ask is `safety_stop = "verified"`
  in the operator's own file.
- Verify command: `python3 -m unittest discover -s tests -v`.

## Task 1 — runner config and board columns

Files: `swarm/runner.py` (new), `swarm/db.py`, `swarm/board.py`, `swarm/cli.py`,
`tests/test_runner.py` (new), `tests/test_cli.py`, `tests/test_db.py`.

1. `db.NEW_COLUMNS`: `plans` gains `window TEXT`, `window_ok INTEGER NOT NULL DEFAULT 0`,
   `measure_from INTEGER NOT NULL DEFAULT 0`; `runs` gains `step TEXT`. Add the same
   columns to `SCHEMA` **at the end of each table, in the same order**, because
   `tests/test_db.py:333` checks that a migrated DB has the fresh column order. Bump
   `VERSION` to 5 (`swarm/db.py:131`). `board.PLAN_FIELDS`
   (`swarm/board.py:9`) gains `window`, `window_ok`, `measure_from`.
   `board.start_run` gains `step=None`, stored in the new column.
2. `runner.py`:
   - `config_path(project_name) -> Path` = `util.home() / "projects" /
     f"{project_name}.toml"`.
   - `load(project_name) -> dict | None`: `None` when the file is missing. Otherwise
     returns `{"safety_stop": bool, "stop_pattern": re.Pattern | None, "steps":
     {name: {"cmd", "timeout_s", "locks"}}, "windows": {name: {"before", "start",
     "length_s", "stop", "read", "production"}}}`. `safety_stop` is true only for the
     exact string `"verified"`. Durations through `config.parse_duration`. Raises
     `ValueError("<path>: <problem>")` for: bad TOML; a step without `cmd`; a window
     without `start`, `length` or `read`; a `before` entry that is not a step; a bad
     duration; a bad regex; `locks` not a list of strings.
   - `gate_window(plan_path_abs) -> str | None`: the last `Gate window:` line of the
     plan file, or `None` (also when the file cannot be read).
3. CLI `swarm runner check NAME`: loads it; prints `no runner config at <path>` (exit 0)
   or one line per step (`step <name>: <timeout>m, locks …`) and window (`window <name>:
   <length>m, before …, production yes/no`) plus `safety stop: verified|not verified`;
   a `ValueError` → its text on stderr, exit 1. Unknown project → the usual board error.

Tests: missing file → `None`; full example above parses; each error case; `safety_stop`
only for `"verified"`; `gate_window` finds the last line, ignores a non-matching one,
returns `None` for a missing file; CLI check prints the summary and exits 1 on a bad
file. `tests/test_db.py`: add `V5_COLUMNS`, check them in the fresh-DB test
(`:306`), rename the `migrates_to_v4` tests (`:324-331`) to `_to_current` and add a v4
legacy DB case; the column-order test must still pass.

## Task 2 — the measuring pass

Files: `swarm/scheduler.py`, `swarm/workers.py`, `tests/test_scheduler.py`.

1. **Into `MEASURING`** (`_merge_step`, replacing `:527-529`): apply the "Which plans
   measure" rule. When it measures: `_move(..., "MEASURING", window=<w>,
   measure_from=<max run id of the plan>)`. The goal-done ask that follows stays as
   today.
2. `PURPOSE_KIND` gains `"step": "runner"`, `"window": "runner"`, `"read": "runner"`.
   `workers.runner_cmd(project, cmd) -> (["sh", "-c", cmd], project["repo_path"])`.
3. **`_measure(conn, cfg, launcher, git, ctx, now_ts, actions)`**, called in `tick`
   after `_plans` (`swarm/scheduler.py:34-43`). It takes every plan in `MEASURING`
   whose project has `auto = 1` and `archived = 0` (goal state does not matter), and no
   `RUNNING` run, ordered by window length then id. Per plan, inside a try like `_plans`:
   - `cfg_r = runner.load(project)`; `ValueError` → block with its text; `None` or the
     window gone → block `runner config for <project> no longer has window <w>`.
   - Done runs = runs of this plan with `id > measure_from`, `status = 'OK'`, purpose in
     `step/window/read/judge`. Next action, first that applies:
     1. the first `before` step without an OK `step` run of that name → launch purpose
        `step`, `step=<name>`, timeout the step's `timeout_s`, after taking its locks;
     2. no OK `window` run → if the window is production, the project's `safety_stop`
        is false and `window_ok` is 0: open (dedupe) the ask `Start production window
        <w> for <label>? It runs "<start>" for <length>m. Answer "yes" to start; any
        other answer skips the window and closes the plan.` with action
        `window:<project>/<ref>` and stop here. Else take `instance:<project>` and
        launch purpose `window` with **`timeout_s` NULL** (the reaper must not kill it)
        and `step=<w>`;
     3. no OK `read` run → launch purpose `read`, timeout 600 s;
     4. no OK `judge` run → Task 3.
4. **Window watch** `_windows(conn, cfg, launcher, now_ts, actions)`, called in `tick`
   right after `_reap`. For each `RUNNING` run with purpose `window`:
   - if the config has a `stop_pattern` and it matches the last 256 KiB of the run log
     (`procs.run_paths`): `launcher.kill(pid)`, run the stop command (below), end the run
     `FAILED` with summary `safety stop`, and block the plan: `Safety stop seen in window
     <w> for <label>. Log: <log>. Answer to measure again.`;
   - else if `now - started_at >= length_s`: kill, run the stop command, end the run
     `OK` with summary `window ended`.
   The **stop command** runs synchronously with `subprocess.run(["sh", "-c", stop],
   cwd=repo_path, env=procs.worker_env(cfg, run_id), timeout=300)`, output appended to
   the window's log; a non-zero exit or timeout adds the action line `stop command for
   <w> failed: …` and does not change the outcome above.
5. **Finishing** (`_finish`, a new branch for purposes `step`, `window`, `read`, before
   the final `_end`): exit 0 → `_end` OK (for `window`, a process that exits on its own
   before `length_s` is a failure: summary `window process exited after <n>m`); non-zero
   → `_fail_run` (which blocks the plan with the log path, as today).
6. **Locks released by `_end`:** after `board.end_run`, drop every lock whose holder is
   `run:<id>` (`DELETE FROM locks WHERE holder = ?`, in `board.release_run_locks(conn,
   run_id)`, new). Covers OK, FAILED, timeout and vanished runs.
7. **Answers** (`_answers`, `swarm/scheduler.py:580-595`):
   - `window:<project>/<ref>`: an answer starting with `yes` → `window_ok = 1`; anything
     else → move the plan to `DONE` with note `production window declined: <answer>`.
   - `_answer_unblock`: when the plan returns to `MEASURING`, set `measure_from` to the
     plan's max run id, so the whole sequence runs again from the first step.
8. `_merge_step`: defer while `release-build:<project>` is held (see Global decisions).

Tests (temp board, `FakeLauncher`/`FakeGit`, a runner toml written into the temp
`SWARM_HOME`): a merge with no gates file / no `Gate window:` line → `DONE` with the
note, no runner config → stays `MERGED` and asks goal-done; with all three →
`MEASURING` with `window` set; an unknown window name → blocked; steps launch in order,
one per tick, each only after the previous is OK;
a step with `locks` holds `release-build:<project>` while running and releases it when it
ends (OK and FAILED); a busy lock skips; a failed step blocks the plan; the window run has
`timeout_s` NULL and holds `instance:<project>`; two plans of one project → the shorter
window starts first and the other waits; window end after `length_s` → kill called, run
OK, lock dropped, then `read` launches; `stop_pattern` in the log → run FAILED, plan
BLOCKED, ask opened; window process exiting early → blocked; production window without
`safety_stop` → ask, no launch; answer "yes" → launches next tick; other answer → `DONE`;
production with `safety_stop = "verified"` → launches without an ask; unblock from a
failed step reruns from the first step; a merge is deferred while `release-build` is held;
a `MEASURING` plan whose goal is `DONE` still advances.

## Task 3 — the judge job and the architect hint

Files: `swarm/scheduler.py`, `swarm/workers.py`, `agents/swarm-architect.md`,
`tests/test_scheduler.py`, `tests/test_workers.py`.

1. `PURPOSE_KIND["judge"] = "verifier"`; `CLAUDE_PURPOSES` gains `"judge"` (so it uses
   a Claude slot and the Claude result parsing at `swarm/scheduler.py:224-256`).
2. `workers.judge(cfg, project, plan, window, read_log) -> (argv, repo_path)`: prompt
   `Write the gate result for plan <path>, window <w>.` plus `Read output: <read_log>`
   and `Gates file: <gates_file>`. Allowed: `Read`, `Grep`, `Glob`,
   `Edit(<gates_file>)`, `Bash(git add:*)`, `Bash(git commit:*)`, `GIT_READ`. System:
   `HEADLESS` + "Record the result in the gates file in the style of its existing rows
   (date, plan ref, window, numbers from the read output, PASS or FAIL). Commit only the
   gates file: git add <file> && git commit -m \"<ref>: gate <window> <PASS|FAIL>\" --
   <file>. If it failed, write a section '## FOLLOW-UP' with one line: what a follow-up
   plan must achieve. The last line must be exactly 'VERDICT: PASS' or 'VERDICT: FAIL'."
   + `rules_text(project)`.
3. In `_measure`, step 4: needs a Claude slot (`_claude_slot`, else skip `Claude slot
   busy`) and `_main_ready` (else skip `main not ready: …`, which also opens the existing
   main-dirty ask). The read log is `procs.run_paths(<latest OK read run>)[0]`.
4. `_finish_judge`: add an explicit `elif purpose == "judge":` in the `CLAUDE_PURPOSES`
   branch of `_finish` (`swarm/scheduler.py:243-251`) **before** the final `else`,
   which today sends every other Claude purpose to `_finish_verify`. It does
   `parse_verdict(result, {"PASS",
   "FAIL"})`; none → end FAILED `no verdict` and block. `PASS` → end OK, move `DONE`
   note `gate <w> PASS`. `FAIL` → end OK, `board.add_goal(conn, project, f"Follow-up:
   {plan title}", done_when=<FOLLOW-UP section, or "gate <w> passes">)`, move `DONE`
   note `gate <w> FAIL; follow-up goal #<id>`.
5. `_creative_running` (`swarm/scheduler.py:492-499`) also counts `judge`, so merges wait
   while a judge commits to the main checkout.
6. `workers.architect` (`swarm/workers.py:52`): when `runner.load(project name)` returns
   windows, add the prompt line `Measurement windows for this project: <names>. If the
   change needs measuring after merge, add a line 'Gate window: <name>' to the plan.`
   (a `ValueError` from `load` adds nothing). `agents/swarm-architect.md`: one bullet
   saying the same.

Tests: `judge` argv (allowed tools contain `Edit(<gates_file>)` and no other Edit;
system names the commit form and the verdict line); PASS → `DONE` with note; FAIL →
follow-up goal with the FOLLOW-UP text and `DONE`; no verdict → blocked; no Claude slot →
skip; main not ready → skip; merge deferred while a judge runs; architect prompt lists
windows only when the project has them.

## Task 4 — docs

Files: `docs/SPEC.md`, `README.md`, `agents/swarm-pm.md`, `swarm/helppage.py`,
`tests/test_helppage.py` (only if a test pins the MEASURING text).

- SPEC §3 Runner row (`docs/SPEC.md:49`): "Release builds, boot tests, measurement
  windows, under locks (`$SWARM_HOME/projects/<name>.toml`)". §6 production row
  (`:134`): "Production windows: autonomous only when the project's runner config has
  `safety_stop = \"verified\"` (set by the operator after the project's own safety stop
  is proven); otherwise each window is an ask. Agents may lower limits, never raise
  them." §8 stage 3 row (`:181`): "Runner: builds, boot tests, measurement windows,
  gate results" and "prerequisite: a project's own safety stop, for production windows
  only"; drop the project-specific prerequisite text and `:183`'s "Plan 0003 is …" outline
  sentence.
- README: a "Measurement windows" section: the toml file (the example above), the
  `Gate window:` line, what happens on pass/fail, the production rule, `swarm runner
  check NAME`.
- `agents/swarm-pm.md`: in plain English, how to explain a `MEASURING` plan and the
  production-window ask; never write the runner toml for the operator without being
  asked, and never set `safety_stop`.
- `swarm/helppage.py:48`: MEASURING text → "a measurement window is running or owed
  (projects with a runner config and a gates file)".

## Done when

- `python3 -m unittest discover -s tests -v` passes.
- On a temp `SWARM_HOME` with a fake project whose steps are `true`, `sleep 1` style
  commands, the scheduler tests walk a plan `MERGED → MEASURING → DONE` through
  build, window, read and judge.
- No worker runs a real project's runner commands; the operator writes the first real
  runner toml and checks it with `swarm runner check`.

## Out of scope

Health checks during a window; restarting a running window for another plan; several
windows per plan; reading results anywhere but the `read` step's output; writing or
editing the runner toml from swarm; any production limit handling inside swarm.

## Changes from the outline

- Generic: no project names; production limits are a project's own safety stop, marked by
  `safety_stop = "verified"` in the operator's runner toml.
- Step names are free (`steps.<name>`), not a fixed list; `health` dropped.
- Failed gates become a follow-up goal and `DONE` (the state machine has no
  `MEASURING → DRAFT`, `swarm/states.py:14`).
- No conflict ask: one window per project at a time, shortest first.
- Plans opt in with a `Gate window:` line.

## Revision log

| Date | Change |
|---|---|
| 2026-11-20 | Outline detailed into a full plan at `1f01ded` (operator ruling: write it now, then review). |
| 2026-11-20 | Review at `1f01ded` applied: lock order is check → launch → take (holder needs the run id); new DB columns appended in order (`tests/test_db.py:333`) with v5 migration tests; `judge` gets an explicit branch in `_finish` (the `else` goes to `_finish_verify`). |
| 2026-11-20 | Operator ruling: gates file without runner config stays MERGED (today's behaviour). |
