# 0012 — Per-role Claude models, and a usage report to measure them

Spec: `docs/SPEC.md`. Baseline: `8b4e0c2`; every line reference below holds there.
Every decision is made here. If something is not covered, choose the simplest option and
list it under DEVIATIONS. Do not add features that are not listed.

## Goal

Every Claude worker runs on Opus today: the daemon's headless workers through the single
`claude_model` setting, and the PM's in-session subagents because they inherit the PM's
model. A measurement of the first 2.7 days of real use (2026-11-17 → 2026-11-20) showed:

- the PM sessions were 80 % of the project's Claude usage, and two thirds of that was
  their subagents (47 subagents, all `general-purpose`, all Opus);
- by role, the subagent cost split: architect 31 %, Claude implementing directly 24 %,
  read-only research 15 %, plan review 13 %, delegate drivers 9 %, verifier 8 %.

This plan (a) gives each role its own model, Opus only where judgement is the job,
(b) tells the PM which model to give each subagent, and (c) adds `swarm usage`, so the
effect can be measured from the transcripts instead of by hand.

## Operator rulings (2026-11-20)

1. **Architect stays on Opus.** Plan writing is the "brain" job.
2. First plan review stays on Opus; re-reviews (they only check that listed fixes were
   applied) go to Sonnet.
3. Gate judge, delegate drivers and Claude-as-implementer go to Sonnet. The **verifier
   stays on Opus** (ruling 2026-11-20: it is the last gate before merge on code that
   handles live money).
4. Read-only research goes to Sonnet; simple lookups and log summaries to Haiku.
5. Risky git surgery (rebase, conflict resolution, any operator-approved destructive
   action) stays on Opus.
6. Trial for a few days, then compare with the baseline below (see Measurement).

## Who implements what

- **Tasks 1–5** → omp, one worktree. Order: 1 → 2; 3, 4 and 5 are independent.
- **Measurement** → the operator with the main Claude session, after the trial days.

## Global decisions

- Model names are the Claude Code aliases `opus`, `sonnet`, `haiku`. Any other string is
  passed through unchanged (a full model id works too).
- Role names (exactly these): `architect`, `reviewer`, `rereview`, `verifier`, `judge`,
  `implementer`.
- `claude_model` stays as the fallback for a role missing from `models`. Its default
  stays `"opus"`.
- Weighted tokens, used by `swarm usage`: `input + 5*output + 1.25*cache_write +
  0.1*cache_read` (the relative prices of the token kinds within one model). No dollar
  prices anywhere: they change, and the report shows the model next to each number.
- Verify command: `python3 -m unittest discover -s tests -v`.

## Task 1 — per-role models in the config (omp)

Files: `swarm/config.py`, `tests/test_config.py`.

- Add a module constant after `IMPLEMENT_TIMEOUT_GRACE_S`:
  ```python
  DEFAULT_MODELS = {
      "architect": "opus",
      "reviewer": "opus",
      "rereview": "sonnet",
      "verifier": "opus",
      "judge": "sonnet",
      "implementer": "sonnet",
  }
  ```
- Add fields to `Config` after `claude_model` (`swarm/config.py:36`):
  `models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))` and
  `claude_projects_dir: str = "~/.claude/projects"`.
- Add a method `model_for(self, role: str) -> str`: `return self.models.get(role,
  self.claude_model)`.
- In `load()`: a `[models]` table in `config.toml` is **merged over** `DEFAULT_MODELS`
  (keys not given keep their defaults), not a replacement. After building `cfg`, set
  `cfg.models = {**DEFAULT_MODELS, **data.get("models", {})}` when the file exists.
  Expand `claude_projects_dir` with `expanduser()` like `omp_runner`.
- Tests (in `tests/test_config.py`):
  - defaults: `cfg.models == config.DEFAULT_MODELS`; `cfg.model_for("architect") ==
    "opus"`; `cfg.model_for("verifier") == "opus"`; `cfg.model_for("nope") == "opus"`;
    `cfg.claude_projects_dir == str(Path("~/.claude/projects").expanduser())`.
  - a `[models]` table with only `verifier = "sonnet"` gives `model_for("verifier") ==
    "sonnet"` and `model_for("rereview") == "sonnet"`.
  - `claude_model = "haiku"` plus no `[models]` table: `model_for("unknown") ==
    "haiku"`, `model_for("architect") == "opus"`.

## Task 2 — workers use the role's model (omp)

Files: `swarm/workers.py`, `tests/test_workers.py`.

- `claude_argv(cfg, prompt, allowed, system, agent=None, model=None)`
  (`swarm/workers.py:23`): pass `"--model", model or cfg.claude_model`.
- Each builder passes its role's model:
  - `architect` (`:88`): `model=cfg.model_for("architect")`.
  - `reviewer` (`:100`): `cfg.model_for("rereview")` when `plan["review_rounds"] > 0`,
    else `cfg.model_for("reviewer")`.
  - `judge` (`:120`): `cfg.model_for("judge")`.
  - `verifier` (`:137`): `cfg.model_for("verifier")`.
  - `claude_implementer` (`:198`): `cfg.model_for("implementer")`, for both the first
    run and fix rounds.
- `implementer` and `fixer` (omp) are unchanged.
- Tests: update `expected_claude` (`tests/test_workers.py:89`) to take `model` and use it
  in place of `cfg.claude_model`; update every existing builder test to pass the role's
  default model. Add: `reviewer` with `review_rounds=0` → `--model opus`, with
  `review_rounds=1` → `--model sonnet`; a `Config` whose `models["verifier"] = "sonnet"`
  gives `--model sonnet` for `verifier`; `claude_argv` without `model` still uses
  `cfg.claude_model` (`tests/test_workers.py:142` keeps passing).

## Task 3 — the PM picks a model for every subagent (omp)

Files: `agents/swarm-pm.md`.

After the list in "## You do not write code" (the line starting "Verification:"), add this
section verbatim:

```markdown
## Subagent models
Every subagent you start gets an explicit `model` on the Agent tool call; never leave it
to inherit yours. Pick by the job, not by how important the plan feels:

| Job | model |
|---|---|
| Write, revise or fix a plan (architect) | opus |
| First review of a plan (`review-plan`) | opus |
| Re-review after a revision (checks the listed fixes) | sonnet |
| Drive `/delegate` (omp implements, you check its result) | sonnet |
| Implement or fix code directly, when omp is not used | sonnet |
| Verify a report and diff (`verify-review`) | opus |
| Read-only investigation or research ("why does X never happen") | sonnet |
| Simple lookup: find a caller, what a column counts, read a log and summarise it, check a process | haiku |
| Rebase, conflict resolution, or any operator-approved destructive action | opus |

When a job is not in the table, use sonnet. If a sonnet subagent comes back unsure or
wrong on a judgement call, rerun that one job on opus and say so in the run summary.
```

No test (prompt text only).

## Task 4 — `swarm usage` (omp)

Files: `swarm/usage.py` (new), `swarm/cli.py`, `tests/test_usage.py` (new),
`tests/test_cli.py`.

`swarm usage [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--subagents]
[--json]`. Default `--since`: 7 days before today (UTC). `--until` is exclusive, default
tomorrow (UTC).

`swarm/usage.py`:

- `slug(path) -> str`: `re.sub(r"[^A-Za-z0-9]", "-", str(path))` (how Claude Code names
  its per-project folders).
- `project_dirs(root, repo_path) -> list[Path]`: folders directly under `root` whose name
  equals `slug(repo_path)` or starts with `slug(repo_path) + "-"` (plan worktrees are
  `<repo>-<ref>`, `swarm/gitops.py:85`). Without a project: every folder under `root`.
- `collect(dirs, since, until) -> list[dict]`: read every `*.jsonl` under each dir
  recursively. Keep lines whose `message.role == "assistant"` with a `message.usage`,
  skip `message.model == "<synthetic>"`, de-duplicate on
  `(message.id, requestId)` (the same response is written several times), keep
  `since <= timestamp[:10] < until`. Each record: `day`, `model`, `kind`, `session`,
  `input`, `output`, `cache_write`, `cache_read`, `weighted`. `kind` is `"subagent"` when
  the file path contains `/subagents/`, else the line's `entrypoint` value, else
  `"unknown"`. For subagent records also keep `agent` = the file stem and
  `description` = `description` from the sibling `<stem>.meta.json` (empty when
  missing). Ignore unreadable lines and files.
- `summarise(records) -> dict`: totals per `(day, kind, model)` with `calls`, `output`,
  `cache_read`, `weighted`; per-day totals; grand total; and, per model, its share of
  weighted tokens.
- `commits_per_day(repo_path, branch, since, until) -> dict[str, int]`: `git -C repo log
  <branch> --since=<since>T00:00Z --until=<until>T00:00Z --format=%cd
  --date=format-local:%Y-%m-%d` with `TZ=UTC` in the environment; count per day. On any
  git error return `{}`.
- `format_report(summary, commits, subagents=None) -> str`: plain text. One table: day,
  kind, model, calls, output, cache_read, weighted (weighted and cache_read in millions,
  one decimal). Then per day: total weighted, commits, weighted per commit (`-` when no
  commits). Then the grand total and the per-model share in %. With `subagents`, a last
  table sorted by weighted, descending: day, model, weighted (thousands), description.

`swarm/cli.py`: parser `usage` with the flags above; `_cmd_usage` reads
`cfg.claude_projects_dir`, resolves the project (`_project_row`, `swarm/cli.py:217`) for
its `repo_path` and `integration_branch`, and prints `format_report`, or
`json.dumps({"summary": …, "commits": …, "subagents": …})` with `--json`. Without
`--project`, no commits are counted. Add it to `_COMMANDS`.

Tests (`tests/test_usage.py`, all data under a temp dir, `claude_projects_dir` set through
`config.toml` in `SWARM_HOME`; nothing reads `~/.claude`):

- `slug("/home/u/projects/demo") == "-home-u-projects-demo"`; `project_dirs` picks
  `-home-u-projects-demo` and `-home-u-projects-demo-0042`, not
  `-home-u-projects-demolition`.
- duplicates: the same `(id, requestId)` three times counts once.
- `<synthetic>` is skipped; a line outside `since/until` is skipped.
- weighted: input 10, output 2, cache_write 4, cache_read 100 → `10 + 10 + 5 + 10 = 35`.
- a file under `<session>/subagents/agent-x.jsonl` with `agent-x.meta.json`
  `{"agentType": "general-purpose", "description": "Review plan 0042"}` gives
  `kind == "subagent"` and that description.
- `commits_per_day` on a temp git repo with two commits on one day returns `{day: 2}`;
  on a missing branch returns `{}`.
- `tests/test_cli.py`: `swarm usage --project demo --json` on fixture transcripts prints
  JSON whose grand total matches the fixture.

## Task 5 — docs (omp)

Files: `docs/SPEC.md`, `README.md`.

- `docs/SPEC.md` role table (`:42`–`:50`): the Model column becomes the role's default
  (Architect: Opus; Plan reviewer: Opus, re-review Sonnet; Verifier: Opus; the Claude
  implementer row, if present: Sonnet). Add one sentence under the table: "Each role's
  model is set in `config.toml` under `[models]`; the PM gives its own subagents a model
  per job (see `agents/swarm-pm.md`)."
- `README.md`: in the configuration part, a short `[models]` example
  (`verifier = "sonnet"` to move the verifier to Sonnet) and one line for `swarm usage`
  ("weighted Claude tokens per day, by model and session kind, and per commit").

## Measurement (operator + main session, after Tasks 1–5 are merged)

Baseline for the main connected project, PM era 2026-11-17 14:10Z → 2026-11-20 07:30Z
(2.7 days), weighted tokens as defined above:

| Metric | Baseline |
|---|---|
| Weighted Claude tokens per day (project + worktrees) | 18.2M |
| of which PM subagents | 26.5M over the period (≈ 9.7M/day), 100 % Opus |
| Weighted tokens per commit | 223k |
| Commits per day | 82 |

The week before the PM existed (2026-11-16 → 2026-11-17 14:10Z) ran at 20.5M/day and
135k per commit.

After at least 3 working days on the new models, run
`swarm usage --project <name> --since <first trial day> --subagents` and compare.

- **Success:** subagent weighted tokens per day down ≥ 35 %; Opus ≤ 50 % of subagent
  weighted tokens (architect, first review and verifier stay on Opus); weighted per commit down.
- **Quality guard** (from the board): fix rounds and review rounds per plan not higher
  than in the baseline period; no plan whose verification passed and later failed its
  gate or needed a fix after merge.
- **Revert by config, no code change:** if the quality guard fails for a role, set that
  role back to `opus` under `[models]` (or change the PM's table for subagents) and note
  it with `swarm note --kind decision`.

## Done when

- `python3 -m unittest discover -s tests -v` passes.
- `swarm usage --help` lists `--project`, `--since`, `--until`, `--subagents`, `--json`.
- With a default config, a headless reviewer's argv contains `--model opus` on round 0
  and `--model sonnet` on round 1; the verifier's contains `--model opus`.
- `agents/swarm-pm.md` contains the "## Subagent models" section.

## Out of scope

- Dollar prices or budgets from Claude usage (the existing `monthly_budget_usd` is
  opencode spend and stays as it is).
- Per-project model overrides (one global `[models]` table is enough for now).
- Changing the omp/DeepSeek implementer.
- Automatic escalation from Sonnet to Opus by the daemon.
