"""Static help page for the swarm board.

The text is fixed and explains the dashboard columns, plan states, asks and
the everyday commands.  It reuses the dashboard style and embeds no external
resources and no JavaScript.
"""

import html

from swarm import dashboard

# Quotes defaults from config.py, states.py, dashboard.py and cli.py; update when they change.
HELP_SECTIONS: list[tuple[str, str]] = [
    (
        "What this is",
        "<p>The board tracks goals, plans, worker runs and questions for you (asks). "
        "The PM session is where you talk; this page and the dashboard are read-only "
        "views. The dashboard rewrites itself after every board change and reloads "
        "every few seconds.</p>",
    ),
    (
        "Columns",
        "<dl>"
        "<dt>Planned</dt><dd>Plans being written, reviewed, or approved and waiting "
        "for a free slot (DRAFT, REVIEW, APPROVED).</dd>"
        "<dt>Executing</dt><dd>omp is implementing the plan in its own worktree, or "
        "fixing what the verifier found (EXECUTING, FIXING). The extra line shows the "
        "running worker.</dd>"
        "<dt>Verifying</dt><dd>A verifier checks the work, then the project's verify "
        "command runs on the branch brought up to date (VERIFYING).</dd>"
        "<dt>Waiting on you</dt><dd>Open asks and blocked plans. Nothing here moves "
        "until you answer.</dd>"
        "<dt>Done / measuring</dt><dd>Merged plans, plans being measured, and plans "
        "done in the last 7 days.</dd>"
        "</dl>",
    ),
    (
        "Plan states",
        "<dl>"
        "<dt>DRAFT</dt><dd>the Architect writes or revises the plan</dd>"
        "<dt>REVIEW</dt><dd>a reviewer reads it; a plan needs APPROVE to go on</dd>"
        "<dt>APPROVED</dt><dd>waits for a slot: at most 3 active plans, enough free "
        "disk, budget left</dd>"
        "<dt>EXECUTING</dt><dd>omp implements it in a worktree</dd>"
        "<dt>VERIFYING</dt><dd>verifier, then the verify command, then the merge</dd>"
        "<dt>FIXING</dt><dd>omp fixes the verifier's list; at most 2 rounds</dd>"
        "<dt>MERGED</dt><dd>in the integration branch; the worktree is removed</dd>"
        "<dt>MEASURING</dt><dd>a measurement window is running or owed (projects with "
        "a runner config and a gates file)</dd>"
        "<dt>DONE</dt><dd>finished</dd>"
        "<dt>BLOCKED</dt><dd>stopped until you answer its ask; it returns to the "
        "state it came from</dd>"
        "<dt>DROPPED</dt><dd>abandoned</dd>"
        "</dl>",
    ),
    (
        "Asks",
        "<p>An ask is a question only you can answer: a ruling on a plan, a failed or "
        'blocked worker, a merge that cannot go ahead, or whether a goal is done. '
        "Answer it in the PM session, or directly with "
        '<code>swarm answer ID "text"</code>. The daemon reads the answer on its next '
        "pass (about 20 s) and continues the plan. For a usage-limit ask, answer "
        "anything once the limit has reset. A goal-done ask takes <code>done</code>, "
        "or what is still missing. A budget ask cannot be answered away: work resumes "
        "when the month changes or when <code>monthly_budget_usd</code> in "
        "<code>config.toml</code> is raised.</p>",
    ),
    (
        "Header strip",
        "<dl>"
        "<dt>Disk</dt><dd>free space on the disk that holds the worktrees; new "
        "worktrees need the floor plus 15 GB.</dd>"
        "<dt>opencode $</dt><dd>omp spend this month against the budget; at the "
        "warning level no omp implementation or fix starts.</dd>"
        "<dt>locks</dt><dd>who holds a shared resource.</dd>"
        "<dt>daemon</dt><dd><code>on</code> when the scheduler has run in the last "
        "minute; <code>off</code> means nothing moves automatically.</dd>"
        "</dl>",
    ),
    (
        "Worktrees",
        "<p>One row per git worktree and loose branch. Badges: "
        "<code>main</code> the main checkout, <code>active</code> belongs to a plan in "
        "progress, <code>merged</code> merged and clean but not linked to a plan (your "
        "call), <code>cleanup</code> merged, clean and its plan finished: safe to "
        "remove, <code>missing</code> the folder is gone.</p>",
    ),
    (
        "Commands",
        "<dl>"
        "<dt><code>swarm status</code></dt><dd>what is waiting, running, next and "
        "done</dd>"
        "<dt><code>swarm brief</code></dt><dd>short summary for a new PM session</dd>"
        '<dt><code>swarm asks</code> / <code>swarm answer ID "text"</code></dt>'
        "<dt><code>swarm runs</code></dt><dd>running workers and their log files; "
        "<code>--all</code> for the last 30</dd>"
        "<dt><code>swarm goal add …</code></dt><dd>a new goal; see "
        "<code>--help</code></dd>"
        "<dt><code>swarm daemon --dry-run</code></dt><dd>what the daemon would do now, "
        "changing nothing</dd>"
        "<dt><code>swarm worktrees</code></dt><dd>the worktree map</dd>"
        "<dt><code>swarm dashboard --open</code></dt><dd>rewrite and open the "
        "dashboard</dd>"
        "</dl>",
    ),
]

EXTRA_CSS = """
main.help { max-width: 860px; }
main.help dt { font-weight: 600; margin-top: .6em; }
main.help dd { margin-left: 1.2em; color: var(--muted); }
main.help code { background: var(--badge-bg); padding: 0 .3em; border-radius: 4px; }
"""


def render(board_name: str = "dashboard.html") -> str:
    sections = "\n".join(
        f"<section><h2>{heading}</h2>{body}</section>"
        for heading, body in HELP_SECTIONS
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Swarm help</title>\n"
        f"<style>{dashboard.STYLE}{EXTRA_CSS}</style>\n"
        "</head>\n<body>\n"
        '<header class="strip">'
        f'<a class="item" href="{html.escape(board_name, quote=True)}">← Board</a>'
        "</header>\n"
        '<main class="help">\n'
        "<h1>Swarm help</h1>\n"
        f"{sections}\n"
        "</main>\n</body>\n</html>\n"
    )
