# Working in this repo (for implementation agents)

- Python 3.12, standard library only. Do not add dependencies or a pyproject.
- Tests: `python3 -m unittest discover -s tests -v`. Every test sets `SWARM_HOME` to a temp dir; no test may touch `~/.local/state/swarm`, `~/.claude` or any other repo.
- Plans live in `docs/plans/`. Follow the plan exactly; list anything you changed from it under DEVIATIONS in your report.
- Commit by explicit path (`git add <files>`), one commit per task, message `<ref> T<n>: <what>`.
- Never `git push`, never rewrite history, never run `scripts/install.sh`.
