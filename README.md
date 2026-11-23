# swarm

A multi-agent workflow for software projects. Claude Code plans, reviews and verifies;
omp + DeepSeek implements by default. The sessions are coordinated through a SQLite board,
with a plain-English PM session you talk to and an HTML dashboard you can leave open. The
board is the single source of truth: every session can be restarted, and a fresh one picks
up exactly where the last one stopped.

Design and architecture: [docs/SPEC.md](docs/SPEC.md).

## What you need

- Linux or WSL.
- Python 3.12, git and [Claude Code](https://docs.anthropic.com/en/docs/claude-code).
- For the default implementer: `omp` (oh-my-pi) and an opencode
  subscription (DeepSeek is the default model).
- Without omp, a project can use headless Claude as its implementer instead:
  `swarm project set demo --implementer claude`.

## Install

```sh
scripts/install.sh
```

It links `bin/swarm` and `swarm-pm` into `~/.local/bin` (make sure that is on `PATH`),
links every `agents/*.md` into `~/.claude/agents/`, links the bundled skills with
`scripts/link-skills.sh`, merges swarm's hooks into `~/.claude/settings.json` (keeping a
backup of the previous file), and runs `swarm init`. `link-skills.sh` keeps any skill
folder you already have instead of overwriting it, and only links the omp skills when
`omp` is installed. The installer is idempotent, so re-run it after a pull.

## Connect a repo

```sh
swarm connect ~/projects/demo --dry-run                 # show what would be set up
swarm connect ~/projects/demo --yes --import-plans      # connect and import existing plans
swarm connect ~/projects/demo --yes --import-plans --done 0042-0044
                                                        # …marking those plans finished
swarm project set demo --auto on                        # let the daemon work on it
swarm disconnect demo                                   # hide it; history is kept
```

`swarm connect` reads the repo and detects its name (the folder name), integration branch
(the checked-out branch), plans folder (`docs/plans`, `plans` or `.swarm/plans`), a gates
file (`*GATES*.md` in the plans folder; without one, plans go MERGED → DONE), and a verify
command from `Cargo.toml`, `go.mod`, `package.json`, pytest config or a `tests/` folder of
`test_*.py`. It installs the pre-push guard and leaves `auto` off. Any detected value can
be overridden (`--name`, `--branch`, `--verify-cmd`, `--verify-allow`). `swarm disconnect
NAME` hides the project, turns auto off and removes the guard; its history is kept, and
connecting the same repo again brings it back.

## The PM

```sh
swarm-pm                # start the PM session; the launcher sets SWARM_PM=1
swarm status            # board summary: done / running / waiting on you / next
swarm brief             # short plain-text brief for a fresh session
```

The PM is the session you talk to. It reads and writes the board, turns your requests into
goals, answers "where are we / what's next", and asks you only for destructive actions and
true conflicts. It holds no code context, so it can be restarted at any time. The
`swarm-pm` launcher starts `agents/swarm-pm.md` with `SWARM_PM=1` set, which enables the
context hooks (a `SessionStart` brief and a `Stop` context-size warning). Other Claude
sessions are unaffected.

## The daemon

For a project with `auto` on, the scheduler daemon runs Architect → review → omp →
verify → merge → cleanup by itself, within the configured limits, and asks you for
rulings. `--auto on` refuses until the project has `verify_cmd` and `verify_allow` set and
its push guard is installed.

```sh
swarm goal add demo "Add a retry to the fetcher" --done-when "retry tests pass" --est-minutes 60
swarm daemon --dry-run      # print what the daemon would do; writes nothing
scripts/start.sh            # tmux session "swarm", with the daemon and PM windows
```

`scripts/start.sh` attaches to an existing tmux session named `swarm` or creates one with
a `daemon` window and a `pm` window (and a `telegram` window when the bot is configured).
The daemon checks the board on its interval; idle costs no model tokens.

## The dashboard

```sh
swarm dashboard             # write $SWARM_HOME/dashboard.html and help.html
```

`SWARM_HOME` defaults to `~/.local/state/swarm/`, so the file is normally
`~/.local/state/swarm/dashboard.html`. It is a static, self-refreshing page; the Help link
in its header opens `help.html` next to it. From Windows, open it through the WSL share:

```text
\\wsl.localhost\<distro>\home\<user>\.local\state\swarm\dashboard.html
```

If you set `SWARM_HOME` elsewhere, use that path instead of the default.

## Panel in Claude Code

A side panel (the Claude Code mod in `mods/swarm-panel`) shows the board live. It polls
`swarm status --json` every 15 s and draws, top to bottom:

1. **Header:** daemon on/off with its heartbeat age, spend against the monthly cap with a
   small bar, free disk (yellow near the floor), the git branch of the session's folder,
   and any held locks.
2. **Context:** the PM session's context size against its window, plus each running
   subagent's.
3. **Waiting on you:** asks (with their options and age; destructive ones in red) and
   blocked plans.
4. **Running:** each worker run with a bar of its time against its timeout and its
   context size (yellow from 80 % of either), plus plans in a running state with no run.
5. **Projects:** one line per project with its integration branch (open goals, ready,
   draft, measuring; `auto off` when off), then the next three items.
6. **Done 24 h:** the three newest merged plans and finished runs of the last day.
7. **Footer:** worktree count, how many await cleanup and their target size, and the time
   of the last update (or the last poll's error).

The PM line has two levels: from 150k tokens (`pm_context_warn_tokens`) it turns yellow
with `→ /clean-pm when convenient`, from 200k (`pm_context_alert_tokens`) red with
`→ /clean-pm now`. Nothing clears or compacts by itself. The status line shows
`swarm: N running · N waiting · PM <size>`, and a new ask raises a notification (or an
in-app message if the terminal cannot show one). The panel is read-only except for
`/clean-pm`; answer asks through the PM or `swarm answer`.

Load it with:

```sh
claude --plugin-dir <path-to-this-repo>/mods/swarm-panel
```

`swarm-pm` already does this. In any session, `/swarm` opens or closes the panel and
`/swarm refresh` polls now.

`/clean-pm` hands the PM session over in one step. It asks a fork of the session for a
short handoff (what is in progress, what comes next, decisions not yet on the board),
saves it with `swarm note --kind handoff`, shows it, and runs `/clear`. The fresh session
gets it under "Last handoff" in the brief that the `SessionStart` hook injects. The fork
sees the conversation up to the PM's last request, so say anything important before
running it. If the handoff cannot be written, nothing is saved and nothing is cleared; if
saving fails, the text is shown so you can copy it; if the clear is refused, it says
`handoff saved — type /clear`.

## Telegram

`swarm telegram` runs a bot on your phone. It is a long-running process that long-polls
Telegram over an outgoing connection, so it opens no port. It sends a message when an ask
opens, when a plan moves to blocked, merged or done, and when the daemon turns on or off.
`swarm telegram --once` makes a single pass (send what is pending, read what has arrived)
and exits; the tests and a quick check use it.

The bot needs a config at `$SWARM_HOME/telegram.toml` (default
`~/.local/state/swarm/telegram.toml`), with exactly two keys:

```toml
bot_token = "123456:ABC..."
chat_id = "123456789"
```

Create it yourself with mode 600, never commit it, and never paste the token anywhere
else; swarm never prints or logs it. Only messages from that chat id are accepted, so a
stranger who finds the bot gets silence.

Commands:

- `/status` — the board summary, read-only.
- `/brief` — the short brief.
- `/asks` — open asks, one line each.
- `/runs` — running workers.
- `/answer <id> <text>` — answer an ask.
- `/goal <project> | <title> | <done when> [| <est minutes>]` — queue a goal.
- `/help` — the command list.

Replying to an ASK message answers that ask. Destructive asks are the exception: the bot
shows them but refuses the answer, and you must answer those in the terminal with
`swarm answer`.

## Measurement windows

After a plan is merged, the daemon can measure it: it runs the project's build and boot
tests, starts a measurement window (a soak test or a canary), waits for it, reads the
result and has a Claude job write the gate result into the project's gates file.

The runner config is yours and lives outside the repo, at
`~/.local/state/swarm/projects/<name>.toml` (under `SWARM_HOME`). Swarm only reads it,
fresh on every tick; no worker, Claude job or swarm command writes it. A project without
this file never measures. Format (TOML):

```toml
safety_stop = "verified"          # optional; only you write it
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

A plan measures only if the project has a gates file and a runner config, and the plan
markdown has a line naming one of the config's windows:

```text
Gate window: soak
```

Without a gates file or that line, a merged plan goes straight to DONE with a note saying
which one is missing. A gates file without a runner config keeps the plan MERGED: the gate
is tracked by hand and the goal-done ask is still opened. Every window holds
`instance:<project>`, so a project measures one window at a time; a step holds the locks
it lists.

A pass moves the plan to DONE with the note `gate <w> PASS`. A fail also moves it to DONE,
but first opens a follow-up goal for what a follow-up plan must achieve: the judge writes
a `## FOLLOW-UP` section in its reply, and that text becomes the follow-up goal's Done
when (only the gates file is committed). A safety stop seen in a window log blocks the
plan and asks.

Production windows (`production = true`: live traffic, real users) start on their own
only when the config has `safety_stop = "verified"`. You set that after the project's own
safety stop is proven; swarm never writes it. Otherwise each production window is an ask,
and any answer other than "yes" skips the window and closes the plan.

Check a config before relying on it:

```sh
swarm runner check NAME
```

It prints the steps, windows and safety-stop state, or the file's error.

## Bundled skills

`scripts/install.sh` links these into `~/.claude/skills/` (and the omp skill into
`~/.omp/agent/skills/` when `omp` is present). Third-party skills keep their upstream
text; licence and source details are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

| Skill | What it does | Upstream |
| --- | --- | --- |
| `delegate` | Hand a plan to omp + DeepSeek, then verify the result and loop fixes back into the same session | this repo |
| `review-plan` | Review an implementation plan before work starts: re-check facts, test the design against the code | old-mikser/llm-stuff |
| `verify-review` | Verify a report or review claim by claim against primary sources, then give a verdict and fix plan | old-mikser/llm-stuff |
| `orchestrate-omp` | Execute written plan files inside omp with `task` subagents, then review and report | old-mikser/llm-stuff (adapted for omp) |
| `improve-codebase-architecture` | Scan a codebase for deepening opportunities and present them as an HTML report | mattpocock/skills |
| `codebase-design` | Shared vocabulary for designing deep modules: interface, depth, seam, adapter | mattpocock/skills |
| `domain-modeling` | Build and sharpen a domain model, glossary and ADRs | mattpocock/skills |
| `grilling` | Stress-test a plan, decision or idea with relentless rounds of questions | mattpocock/skills |

## Configuration

Settings live in `$SWARM_HOME/config.toml` (default
`~/.local/state/swarm/config.toml`). Missing keys keep their defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `disk_path` | `"~"` (your home folder) | The disk the floor watches; worktrees and the WSL disk live here |
| `disk_floor_gb` | `50` | Free space that must remain on `disk_path` |
| `worktree_reserve_gb` | `15` | Extra free space reserved for a new worktree |
| `monthly_budget_usd` | `80` | Monthly opencode spend cap |
| `daemon_interval_s` | `20` | Seconds between daemon ticks |
| `max_active_plans` | `3` | Plans allowed in EXECUTING/FIXING/VERIFYING at once |
| `max_claude_jobs` | `2` | Headless Claude jobs allowed at once |
| `pm_context_warn_tokens` | `150000` | PM context size that turns the panel and warning yellow |
| `pm_context_alert_tokens` | `200000` | PM context size that turns them red |
| `arch_review_every` | `8` | Merged plans after which the PM suggests an architecture review; `0` disables |

A launch needs `disk_floor_gb + worktree_reserve_gb` = 65 GB free on
`disk_path`. WSL users should point it at the Windows disk, `/mnt/c`, which holds the WSL
disk image; otherwise free space reported inside the distro does not reflect the real one.

```toml
disk_path = "/mnt/c"
disk_floor_gb = 50
worktree_reserve_gb = 15
monthly_budget_usd = 80
daemon_interval_s = 20
max_active_plans = 3
max_claude_jobs = 2
pm_context_warn_tokens = 150000
pm_context_alert_tokens = 200000
arch_review_every = 8

[models]
verifier = "sonnet"
```

A `[models]` table overrides the per-role Claude models (`architect`, `reviewer`,
`rereview`, `verifier`, `judge`, `implementer`); keys not listed keep their defaults.
`swarm usage` reports weighted Claude tokens per day, by model and session kind, and per
commit.

## Safety

These actions always ask you first:

- `git push` to any remote.
- Force-push, `reset --hard`, rebase, or deleting the integration branch.
- Deleting or pruning a production database.
- Raising production limits or disabling a project's safety stop.
- Deploying or upgrading contracts.
- Moving funds.
- Anything over ssh to a production host.

Workers run with `SWARM_WORKER=1`, and each project gets a `pre-push` hook that refuses
pushes in that state. `swarm connect` installs the guard; reinstall or repair it with
`swarm project guard NAME`. Headless Claude jobs also get the git and ssh commands
disallowed. **Merging into the local integration branch is not destructive** — the daemon
does it without asking.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests are stdlib `unittest` and set `SWARM_HOME` to a temporary directory, so they
never touch your board.
