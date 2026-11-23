import contextlib
import os
import tempfile
import unittest
from pathlib import Path

from swarm import config, workers
from swarm.workers import (
    DENY,
    GIT_READ,
    HEADLESS,
    architect,
    claude_argv,
    claude_implementer,
    fixer,
    implementer,
    is_usage_limit,
    judge,
    last_line_value,
    parse_claude_json,
    parse_omp,
    parse_rulings,
    parse_verdict,
    preverify,
    reviewer,
    rules_text,
    section,
    verifier,
    verify_allow,
)

ARCHITECT_SYSTEM = (
    HEADLESS + " Commit only the plan file, with: git add <file> && git commit -m "
    "\"<ref>: plan <title>\" -- <file>. The last three lines must be exactly: "
    "PLAN: <path relative to the repo>, REF: <plan ref>, TITLE: <one-line title>."
)
REVIEWER_SYSTEM = (
    HEADLESS + " Do not edit files. List choices that belong to the operator under "
    "'Rulings needed'. The second-to-last line must be 'RULINGS: none' or "
    "'RULINGS: <count>'. The last line must be exactly 'VERDICT: APPROVE' or "
    "'VERDICT: REVISE'."
)
VERIFIER_SYSTEM = (
    HEADLESS + " Do not edit files. The report is the implementer's; verify it against "
    "plan docs/plans/0007-fake.md and the commits abc123..HEAD in this worktree. "
    "Run the verify command yourself: python3 -m unittest. If anything must be fixed, "
    "write a section '## FIX LIST' with numbered items (file:line, expected vs actual, "
    "failing output). The last line must be exactly 'VERDICT: PASS' or 'VERDICT: FIX'."
)
JUDGE_SYSTEM = (
    HEADLESS + " Record the result in the gates file in the style of its existing "
    "rows (date, plan ref, window, numbers from the read output, PASS or FAIL). "
    "Commit only the gates file: git add <file> && git commit -m "
    "\"<ref>: gate <window> <PASS|FAIL>\" -- <file>. If it failed, write a "
    "section '## FOLLOW-UP' with one line: what a follow-up plan must achieve. "
    "The last line must be exactly 'VERDICT: PASS' or 'VERDICT: FAIL'."
)


def make_cfg():
    cfg = config.Config()
    cfg.claude_bin = "/fake/claude"
    cfg.omp_runner = "/fake/omp-run.py"
    return cfg


def make_project(**overrides):
    row = {"name": "demo", "repo_path": "/repo/demo", "plans_dir": "docs/plans",
           "integration_branch": "main", "verify_cmd": "python3 -m unittest",
           "verify_allow": None, "rules": None,
           "gates_file": "docs/plans/PENDING-GATES.md"}
    row.update(overrides)
    return row


def make_plan(**overrides):
    row = {"ref": "0007", "title": "Fake plan", "path": "docs/plans/0007-fake.md",
           "worktree": "/tmp/wt", "base_sha": "abc123", "omp_session": None,
           "operator_note": None, "branch": "plan/0007-fake", "review_rounds": 0}
    row.update(overrides)
    return row


def make_goal(**overrides):
    row = {"id": 3, "title": "Ship it", "done_when": "tests pass"}
    row.update(overrides)
    return row


def expected_claude(cfg, prompt, allowed, system, agent=None, model=None):
    argv = [cfg.claude_bin, "-p", prompt]
    if agent is not None:
        argv += ["--agent", agent]
    argv += ["--output-format", "stream-json", "--verbose",
             "--model", model or cfg.claude_model,
             "--permission-mode", "dontAsk", "--append-system-prompt", system,
             "--allowedTools", *allowed, "--disallowedTools", *DENY]
    return argv


@contextlib.contextmanager
def temp_home():
    old = os.environ.get("SWARM_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SWARM_HOME"] = tmp
        try:
            yield Path(tmp)
        finally:
            if old is None:
                os.environ.pop("SWARM_HOME", None)
            else:
                os.environ["SWARM_HOME"] = old


def write_runner(home, name="demo", text=None):
    directory = home / "projects"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(text or "")
    return path


class ConstantsTest(unittest.TestCase):
    def test_deny(self):
        self.assertEqual(DENY, [
            "Bash(git push:*)", "Bash(git reset:*)", "Bash(git rebase:*)",
            "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git branch:*)",
            "Bash(git worktree:*)", "Bash(git merge:*)", "Bash(ssh:*)", "Bash(rm:*)"])
        self.assertEqual(GIT_READ, ["Bash(git log:*)", "Bash(git show:*)",
                                    "Bash(git diff:*)", "Bash(git status:*)"])

    def test_headless(self):
        self.assertEqual(
            HEADLESS,
            "Non-interactive run for the swarm scheduler: nobody can answer you. "
            "Do not wait for rulings. Keep the report under 40 lines.")


class ClaudeArgvTest(unittest.TestCase):
    def test_without_agent(self):
        cfg = make_cfg()
        argv = claude_argv(cfg, "prompt", ["Read"], "system")
        self.assertEqual(argv, [
            "/fake/claude", "-p", "prompt", "--output-format", "stream-json",
            "--verbose", "--model", "opus", "--permission-mode", "dontAsk",
            "--append-system-prompt", "system", "--allowedTools", "Read",
            "--disallowedTools", *DENY])
        self.assertIn("stream-json", argv)
        self.assertNotIn("json", argv)
        self.assertEqual(argv[-len(DENY):], DENY)
        self.assertEqual(argv[-len(DENY) - 1], "--disallowedTools")
        self.assertNotIn("--agent", argv)

    def test_agent_comes_right_after_prompt(self):
        cfg = make_cfg()
        argv = claude_argv(cfg, "prompt", ["Read"], "system", agent="swarm-architect")
        self.assertEqual(argv[:5], ["/fake/claude", "-p", "prompt",
                                    "--agent", "swarm-architect"])
        self.assertEqual(argv[-len(DENY):], DENY)


class RulesTextTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(rules_text(make_project(rules=None)), "")
        self.assertEqual(rules_text(make_project(rules="[]")), "")
        self.assertEqual(rules_text(make_project(rules="oops")), "")

    def test_two_rules(self):
        project = make_project(rules='["never drop the prod db", "never push"]')
        self.assertEqual(
            rules_text(project),
            " Project rules — never do these; they need the operator: "
            "never drop the prod db; never push.")


class BuilderTest(unittest.TestCase):
    def test_architect_create(self):
        cfg = make_cfg()
        prompt = ("Write a new plan for this goal.\n"
                  "Project: demo, plans dir: docs/plans, integration branch: main.\n"
                  "Goal #3: Ship it\n"
                  "Done when: tests pass")
        allowed = ["Read", "Grep", "Glob", "Edit(docs/plans/**)", "Write(docs/plans/**)",
                   "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ]
        with temp_home():
            argv, cwd = architect(cfg, make_project(), make_goal(),
                                  make_plan(path=None))
        self.assertEqual(cwd, "/repo/demo")
        self.assertEqual(argv, expected_claude(cfg, prompt, allowed, ARCHITECT_SYSTEM,
                                               agent="swarm-architect",
                                               model=cfg.model_for("architect")))

    def test_architect_revise_with_note_and_review(self):
        cfg = make_cfg()
        prompt = ("Revise plan docs/plans/0007-fake.md using the review below.\n"
                  "Project: demo, plans dir: docs/plans, integration branch: main.\n"
                  "Goal #3: Ship it\n"
                  "Done when: tests pass\n"
                  "Operator answer: use sqlite\n"
                  "Review to address:\nFix X.\nFix Y.")
        allowed = ["Read", "Grep", "Glob", "Edit(docs/plans/**)", "Write(docs/plans/**)",
                   "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ]
        with temp_home():
            argv, cwd = architect(cfg, make_project(), make_goal(), make_plan(),
                                  review_text="Fix X.\nFix Y.",
                                  operator_note="use sqlite")
        self.assertEqual(cwd, "/repo/demo")
        self.assertEqual(argv, expected_claude(cfg, prompt, allowed, ARCHITECT_SYSTEM,
                                               agent="swarm-architect",
                                               model=cfg.model_for("architect")))

    def test_architect_lists_measurement_windows(self):
        cfg = make_cfg()
        with temp_home() as home:
            write_runner(home, text="""
[windows.soak]
start = "./start.sh"
length = "30m"
read = "./read.sh"

[windows.canary]
start = "./canary.sh"
length = "5m"
read = "./read-canary.sh"
""")
            argv, cwd = architect(cfg, make_project(), make_goal(), make_plan())
        self.assertEqual(cwd, "/repo/demo")
        prompt = argv[2]
        self.assertIn(
            "Measurement windows for this project: canary, soak. If the change "
            "needs measuring after merge, add a line 'Gate window: <name>' to the "
            "plan.", prompt)
        lines = prompt.splitlines()
        self.assertEqual(lines[3], "Done when: tests pass")
        self.assertTrue(lines[4].startswith("Measurement windows for this project:"))

    def test_architect_omits_window_hint_without_windows(self):
        cfg = make_cfg()
        prompt = ("Write a new plan for this goal.\n"
                  "Project: demo, plans dir: docs/plans, integration branch: main.\n"
                  "Goal #3: Ship it\n"
                  "Done when: tests pass")
        with temp_home():
            argv, _ = architect(cfg, make_project(), make_goal(), make_plan())
        self.assertEqual(argv[2], prompt)
        self.assertNotIn("Measurement windows", argv[2])

    def test_architect_omits_window_hint_when_config_is_bad(self):
        cfg = make_cfg()
        with temp_home() as home:
            write_runner(home, text="steps = 3\n")
            argv, _ = architect(cfg, make_project(), make_goal(), make_plan())
        self.assertNotIn("Measurement windows", argv[2])

    def test_reviewer(self):
        cfg = make_cfg()
        argv, cwd = reviewer(cfg, make_project(), make_plan())
        self.assertEqual(cwd, "/repo/demo")
        self.assertEqual(argv, expected_claude(
            cfg, "/review-plan docs/plans/0007-fake.md",
            ["Read", "Grep", "Glob", "Skill", *GIT_READ], REVIEWER_SYSTEM,
            model=cfg.model_for("reviewer")))
        self.assertNotIn("--agent", argv)

    def test_reviewer_first_round_is_opus(self):
        cfg = make_cfg()
        argv, _ = reviewer(cfg, make_project(), make_plan(review_rounds=0))
        model = argv[argv.index("--model") + 1]
        self.assertEqual(model, "opus")

    def test_reviewer_rereview_is_sonnet(self):
        cfg = make_cfg()
        argv, _ = reviewer(cfg, make_project(), make_plan(review_rounds=1))
        model = argv[argv.index("--model") + 1]
        self.assertEqual(model, "sonnet")

    def test_judge(self):
        cfg = make_cfg()
        project = make_project(gates_file="docs/plans/GATES.md")
        prompt = ("Write the gate result for plan docs/plans/0007-fake.md, "
                  "window soak.\n"
                  "Read output: /runs/12-out.log\n"
                  "Gates file: docs/plans/GATES.md")
        allowed = ["Read", "Grep", "Glob", "Edit(docs/plans/GATES.md)",
                   "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ]
        argv, cwd = judge(cfg, project, make_plan(), "soak", "/runs/12-out.log")
        self.assertEqual(cwd, "/repo/demo")
        self.assertEqual(argv, expected_claude(cfg, prompt, allowed, JUDGE_SYSTEM,
                                               model=cfg.model_for("judge")))
        tools = argv[argv.index("--allowedTools") + 1:
                    argv.index("--disallowedTools")]
        self.assertEqual([tool for tool in tools if tool.startswith("Edit(")],
                         ["Edit(docs/plans/GATES.md)"])
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn('git commit -m "<ref>: gate <window> <PASS|FAIL>" -- <file>',
                      system)
        self.assertIn("The last line must be exactly 'VERDICT: PASS' or "
                      "'VERDICT: FAIL'.", system)

    def test_verifier(self):
        cfg = make_cfg()
        project = make_project(verify_allow="Bash(cargo test:*), Bash(pytest:*)")
        argv, cwd = verifier(cfg, project, make_plan(), "/runs/42.md")
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(argv, expected_claude(
            cfg, "/verify-review /runs/42.md",
            ["Read", "Grep", "Glob", "Skill", *GIT_READ,
             "Bash(cargo test:*)", "Bash(pytest:*)"], VERIFIER_SYSTEM,
            model=cfg.model_for("verifier")))

    def test_verifier_uses_configured_model(self):
        cfg = make_cfg()
        cfg.models["verifier"] = "sonnet"
        argv, _ = verifier(cfg, make_project(), make_plan(), "/runs/42.md")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_verify_allow_empty(self):
        self.assertEqual(verify_allow(make_project()), [])
        self.assertEqual(verify_allow(make_project(verify_allow="")), [])
        self.assertEqual(verify_allow(make_project(verify_allow="  ,  ")), [])
        self.assertEqual(verify_allow(make_project(verify_allow="Read,Bash(npm:*)")),
                         ["Read", "Bash(npm:*)"])

    def test_rules_in_prompts(self):
        cfg = make_cfg()
        project = make_project(rules='["never push"]')
        note = " Project rules — never do these; they need the operator: never push."
        with temp_home():
            argv, _ = architect(cfg, project, make_goal(), make_plan())
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(note, system)
        argv, _ = verifier(cfg, project, make_plan(), "/runs/42.md")
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(note, system)
        self.assertIn("Treat a breach of a project rule as a FIX item.", system)
        argv, _ = implementer(cfg, project, make_plan())
        self.assertIn(note, argv[-1])
        argv, _ = fixer(cfg, project, make_plan(), "/runs/9-fix.md")
        self.assertIn(note, argv[-1])

    def test_no_rules_adds_nothing(self):
        cfg = make_cfg()
        argv, _ = verifier(cfg, make_project(), make_plan(),
                           "/runs/42.md")
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertNotIn("project rule", system)
        self.assertNotIn("Project rules", system)
        argv, _ = implementer(cfg, make_project(), make_plan())
        self.assertNotIn("Project rules", argv[-1])

    def test_implementer(self):
        cfg = make_cfg()
        prompt = ("Load skill://orchestrate-omp and follow it exactly. "
                  "Plan: docs/plans/0007-fake.md. Verify command: python3 -m unittest. "
                  "Read AGENTS.md or CLAUDE.md first if present. "
                  "Commit message prefix '0007 T<n>:'. Never push. "
                  "Do not touch files outside /tmp/wt.")
        argv, cwd = implementer(cfg, make_project(), make_plan())
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(argv, ["python3", "/fake/omp-run.py", "--cwd", "/tmp/wt",
                                "--max-cost", "3.0", "--max-time", "60m", prompt])
        self.assertNotIn("--resume", argv)

    def test_implementer_operator_note(self):
        cfg = make_cfg()
        argv, cwd = implementer(cfg, make_project(),
                                make_plan(operator_note="keep the old flag"))
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(
            argv[-1],
            "Load skill://orchestrate-omp and follow it exactly. "
            "Plan: docs/plans/0007-fake.md. Verify command: python3 -m unittest. "
            "Read AGENTS.md or CLAUDE.md first if present. "
            "Commit message prefix '0007 T<n>:'. Never push. "
            "Do not touch files outside /tmp/wt. Operator answer: keep the old flag")

    def test_fixer(self):
        cfg = make_cfg()
        prompt = ("Verification found problems; the fix list is in /runs/9-fix.md. "
                  "Fix them via workers per orchestrate-omp, keep the verify command green, "
                  "then give the same final report.")
        argv, cwd = fixer(cfg, make_project(), make_plan(omp_session="sess-1"),
                          "/runs/9-fix.md")
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(argv, ["python3", "/fake/omp-run.py", "--cwd", "/tmp/wt",
                                "--resume", "sess-1", "--max-cost", "1.0",
                                "--max-time", "30m", prompt])

    def test_fixer_without_session_and_with_note(self):
        cfg = make_cfg()
        argv, cwd = fixer(cfg, make_project(), make_plan(operator_note="use the cache"),
                          "/runs/9-fix.md")
        self.assertEqual(cwd, "/tmp/wt")
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[2:6], ["--cwd", "/tmp/wt", "--max-cost", "1.0"])
        self.assertTrue(argv[-1].endswith(" Operator answer: use the cache"))

    def test_preverify(self):
        argv, cwd = preverify(make_project(), make_plan())
        self.assertEqual(argv, ["sh", "-c", "python3 -m unittest"])
        self.assertEqual(cwd, "/tmp/wt")


class ClaudeImplementerTest(unittest.TestCase):
    def test_implement(self):
        cfg = make_cfg()
        project = make_project(verify_allow="Bash(pytest:*)")
        prompt = (
            "Implement plan docs/plans/0007-fake.md in this worktree. "
            "Verify command: python3 -m unittest. "
            "Read AGENTS.md or CLAUDE.md first if present. "
            "Commit per task with message prefix '0007 T<n>:'."
            " Never push. Do not touch files outside /tmp/wt.")
        allowed = ["Read", "Grep", "Glob", "Edit(/tmp/wt/**)", "Write(/tmp/wt/**)",
                   "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ,
                   "Bash(pytest:*)"]
        system = (HEADLESS + " End with a section '## BLOCKED' only if you cannot "
                  "finish. The last line must be exactly 'RESULT: done', "
                  "'RESULT: partial' or 'RESULT: blocked'.")
        argv, cwd = claude_implementer(cfg, project, make_plan())
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(argv, expected_claude(cfg, prompt, allowed, system,
                                               model=cfg.model_for("implementer")))
        self.assertEqual(argv[:2], [cfg.claude_bin, "-p"])

    def test_rules_in_system(self):
        cfg = make_cfg()
        project = make_project(rules='["never push"]')
        argv, _ = claude_implementer(cfg, project, make_plan())
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(" Project rules — never do these; they need the operator: "
                      "never push.", system)
        self.assertNotIn("Project rules", argv[2])

    def test_fix_resumes_session(self):
        cfg = make_cfg()
        argv, cwd = claude_implementer(cfg, make_project(),
                                       make_plan(omp_session="sess-1"),
                                       "/runs/9-fix.md")
        prompt = ("Verification found problems; the fix list is in /runs/9-fix.md. "
                  "Fix them, keep the verify command green, commit.")
        self.assertEqual(cwd, "/tmp/wt")
        self.assertEqual(argv[2], prompt)
        self.assertEqual(argv[3:5], ["--resume", "sess-1"])

    def test_fix_without_session_and_with_note(self):
        cfg = make_cfg()
        argv, _ = claude_implementer(cfg, make_project(),
                                     make_plan(operator_note="use the cache"),
                                     "/runs/9-fix.md")
        self.assertNotIn("--resume", argv)
        self.assertTrue(argv[2].endswith(" Operator answer: use the cache"))
        self.assertEqual(argv[argv.index("--model") + 1],
                         cfg.model_for("implementer"))


class ParseClaudeJsonTest(unittest.TestCase):
    def test_result_object(self):
        text = ('noise\n{"result": "hello", "is_error": false, '
                '"total_cost_usd": 0.25, "session_id": "s-1"}\n')
        parsed = parse_claude_json(text)
        self.assertEqual(parsed, {"result": "hello", "is_error": False,
                                  "cost": 0.25, "session": "s-1"})

    def test_last_json_line_wins(self):
        text = ('{"result": "old", "session_id": "a"}\n'
                "not json\n"
                '{"result": "new", "session_id": "b"}\n')
        self.assertEqual(parse_claude_json(text)["result"], "new")

    def test_scans_backwards_past_non_objects(self):
        text = "plain text\n[1, 2]\n{\"result\": \"ok\"}\ntrailing junk\n"
        parsed = parse_claude_json(text)
        self.assertEqual(parsed["result"], "ok")
        self.assertFalse(parsed["is_error"])

    def test_defaults_for_missing_keys(self):
        parsed = parse_claude_json('{"session_id": "s"}\n')
        self.assertEqual(parsed, {"result": "", "is_error": False,
                                  "cost": 0.0, "session": "s"})

    def test_non_string_result(self):
        parsed = parse_claude_json('{"result": 42}')
        self.assertEqual(parsed["result"], "")

    def test_unparseable(self):
        text = '{"result": "unterminated'
        parsed = parse_claude_json(text)
        self.assertEqual(parsed, {"result": text, "is_error": True,
                                  "cost": 0.0, "session": None})

    def test_unparseable_is_truncated_to_last_2000_chars(self):
        long_text = "x" * 2500
        self.assertEqual(parse_claude_json(long_text)["result"], long_text[-2000:])

    def test_empty(self):
        self.assertEqual(parse_claude_json(""),
                         {"result": "", "is_error": True, "cost": 0.0, "session": None})


class UsageLimitTest(unittest.TestCase):
    def test_matches(self):
        for text in ("Claude usage limit reached", "RATE LIMIT",
                     "This requires usage credits", "Limit reached for this window"):
            self.assertTrue(is_usage_limit(text), text)

    def test_does_not_match(self):
        self.assertFalse(is_usage_limit("all good"))
        self.assertFalse(is_usage_limit(""))


class LastLineValueTest(unittest.TestCase):
    def test_last_match(self):
        text = "PLAN: a.md\nREF: 0001\nPLAN: b.md \n"
        self.assertEqual(last_line_value(text, "PLAN"), "b.md")
        self.assertEqual(last_line_value(text, "REF"), "0001")

    def test_missing(self):
        self.assertIsNone(last_line_value("PLAN: a.md", "TITLE"))

    def test_key_does_not_match_longer_key(self):
        self.assertIsNone(last_line_value("MYPLAN: a.md", "PLAN"))


class VerdictTest(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(parse_verdict("text\n\nVERDICT: APPROVE\n",
                                       {"APPROVE", "REVISE"}), "APPROVE")
        self.assertEqual(parse_verdict("VERDICT:  FIX  ", {"PASS", "FIX"}), "FIX")

    def test_not_last_line(self):
        self.assertIsNone(parse_verdict("VERDICT: PASS\nsome caveat",
                                        {"PASS", "FIX"}))

    def test_not_allowed(self):
        self.assertIsNone(parse_verdict("VERDICT: MAYBE", {"PASS", "FIX"}))

    def test_empty_and_missing(self):
        self.assertIsNone(parse_verdict("", {"PASS"}))
        self.assertIsNone(parse_verdict("All fine.", {"PASS"}))


class RulingsTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(parse_rulings("Rulings needed\nRULINGS: none\nVERDICT: APPROVE"), 0)
        self.assertEqual(parse_rulings("RULINGS: NONE"), 0)

    def test_count(self):
        self.assertEqual(parse_rulings("RULINGS: 3\nVERDICT: REVISE"), 3)

    def test_malformed(self):
        self.assertIsNone(parse_rulings("RULINGS: many"))
        self.assertIsNone(parse_rulings("no rulings here"))

    def test_last_wins(self):
        self.assertEqual(parse_rulings("RULINGS: none\nRULINGS: 2"), 2)


class SectionTest(unittest.TestCase):
    TEXT = """# Plan
Intro.

## Rulings needed
- A or B?

### Detail
More.

## FIX LIST
1. a.py:2 expected x
2. b.py:3 y

## Other
done
"""

    def test_section_stops_at_same_level_heading(self):
        self.assertEqual(section(self.TEXT, "FIX LIST"),
                         "1. a.py:2 expected x\n2. b.py:3 y")

    def test_heading_contains_case_insensitive_and_keeps_subsections(self):
        self.assertEqual(section(self.TEXT, "rulings"),
                         "- A or B?\n\n### Detail\nMore.")

    def test_absent(self):
        self.assertEqual(section(self.TEXT, "MISSING"), "")

    def test_higher_level_heading_continues_through_subsections(self):
        text = "# One\n## Alpha\na\n## Beta\nb\n# Two\nc\n"
        self.assertEqual(section(text, "One"), "## Alpha\na\n## Beta\nb")

    def test_no_headings(self):
        self.assertEqual(section("plain text\n", "anything"), "")

    def test_exact_heading_beats_substring(self):
        text = "## RESULT: blocked\nbody\n## BLOCKED\nreason\n"
        self.assertEqual(section(text, "BLOCKED"), "reason")


class ParseOmpTest(unittest.TestCase):
    REPORT = """SESSION: fake-1
STATUS:  ok
COST:    $0.0300
LOG:     /dev/null
---- worker final message ----
## RESULT: done

Work complete.

## BLOCKED
none
"""

    def test_real_report(self):
        parsed = parse_omp(self.REPORT)
        self.assertEqual(parsed["session"], "fake-1")
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["cost"], 0.03)
        self.assertEqual(parsed["result"], "done")
        self.assertEqual(parsed["blocked"], "none")
        self.assertTrue(parsed["final"].startswith("## RESULT: done"))
        self.assertNotIn("SESSION:", parsed["final"])

    def test_blocked_and_partial(self):
        text = ("SESSION: s\nSTATUS: ok\nCOST: $1.5\n"
                "---- worker final message ----\n"
                "## RESULT: blocked\n\n"
                "## BLOCKED\nNeed a token: missing API key.\n"
                "### Details\nit failed\n")
        parsed = parse_omp(text)
        self.assertEqual(parsed["result"], "blocked")
        self.assertEqual(parsed["blocked"],
                         "Need a token: missing API key.\n### Details\nit failed")
        self.assertEqual(parsed["cost"], 1.5)
        self.assertEqual(parse_omp(text.replace("blocked", "partial"))["result"], "partial")

    def test_unknown_result_is_none(self):
        parsed = parse_omp("---- worker final message ----\n## RESULT: maybe\n")
        self.assertIsNone(parsed["result"])

    def test_missing_marker_and_fields(self):
        parsed = parse_omp("SESSION: s\nnothing else\n")
        self.assertEqual(parsed["session"], "s")
        self.assertIsNone(parsed["status"])
        self.assertIsNone(parsed["result"])
        self.assertEqual(parsed["cost"], 0.0)
        self.assertEqual(parsed["final"], "")
        self.assertEqual(parsed["blocked"], "")

    def test_malformed_cost(self):
        self.assertEqual(parse_omp("COST: $abc\n")["cost"], 0.0)
        self.assertEqual(parse_omp("COST: 1.5\n")["cost"], 0.0)
        self.assertEqual(parse_omp("")["cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
