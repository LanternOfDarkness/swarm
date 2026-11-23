import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from swarm import usage


def make_line(
    day="2026-11-17",
    *,
    model="opus",
    input_tokens=10,
    output_tokens=2,
    cache_write=4,
    cache_read=100,
    message_id="m1",
    request_id="r1",
    role="assistant",
    entrypoint=None,
):
    message = {
        "id": message_id,
        "role": role,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        },
    }
    line = {
        "timestamp": f"{day}T10:00:00Z",
        "requestId": request_id,
        "message": message,
    }
    if entrypoint is not None:
        line["entrypoint"] = entrypoint
    return line


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_jsonl(self, path, lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def test_slug(self):
        self.assertEqual(usage.slug("/home/u/projects/demo"),
                         "-home-u-projects-demo")

    def test_project_dirs(self):
        root = self.root / "projects"
        for name in (
            "-home-u-projects-demo",
            "-home-u-projects-demo-0042",
            "-home-u-projects-demolition",
            "-home-u-projects-other",
        ):
            (root / name).mkdir(parents=True)
        found = usage.project_dirs(root, "/home/u/projects/demo")
        self.assertEqual(
            sorted(path.name for path in found),
            ["-home-u-projects-demo", "-home-u-projects-demo-0042"],
        )
        everything = usage.project_dirs(root, None)
        self.assertEqual(len(everything), 4)

    def test_collect_dedupes_and_skips(self):
        path = self.root / "-p-demo" / "session.jsonl"
        line = make_line(entrypoint="cli")
        self.write_jsonl(path, [
            line,
            line,
            line,
            make_line(model="<synthetic>", message_id="m2", request_id="r2"),
            make_line(day="2026-11-16", message_id="m3", request_id="r3"),
            make_line(day="2026-11-18", message_id="m4", request_id="r4"),
            {**make_line(message_id="m5", request_id="r5"), "message": {
                "id": "m5", "role": "user", "usage": {"input_tokens": 1}}},
        ])
        records = usage.collect(
            [self.root / "-p-demo"], "2026-11-17", "2026-11-18"
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["day"], "2026-11-17")
        self.assertEqual(record["model"], "opus")
        self.assertEqual(record["kind"], "cli")
        self.assertEqual(record["session"], "session")
        self.assertEqual(record["weighted"], 35.0)

    def test_subagent_kind_and_description(self):
        folder = self.root / "-p-demo"
        path = folder / "session-1" / "subagents" / "agent-x.jsonl"
        self.write_jsonl(path, [make_line()])
        (path.with_name("agent-x.meta.json")).write_text(json.dumps({
            "agentType": "general-purpose",
            "description": "Review plan 0042",
        }))
        records = usage.collect([folder], "2026-11-17", "2026-11-18")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["kind"], "subagent")
        self.assertEqual(record["agent"], "agent-x")
        self.assertEqual(record["session"], "session-1")
        self.assertEqual(record["description"], "Review plan 0042")

    def test_summarise(self):
        records = [
            {"day": "2026-11-17", "kind": "cli", "model": "opus",
             "output": 10, "cache_read": 100, "weighted": 100.0},
            {"day": "2026-11-17", "kind": "subagent", "model": "sonnet",
             "output": 5, "cache_read": 50, "weighted": 50.0},
        ]
        summary = usage.summarise(records)
        self.assertEqual(
            [(row["kind"], row["model"]) for row in summary["rows"]],
            [("cli", "opus"), ("subagent", "sonnet")],
        )
        self.assertEqual(summary["total"]["calls"], 2)
        self.assertEqual(summary["total"]["weighted"], 150.0)
        self.assertEqual(len(summary["days"]), 1)
        self.assertEqual(summary["days"][0]["weighted"], 150.0)
        self.assertEqual([model["model"] for model in summary["models"]],
                         ["opus", "sonnet"])
        self.assertAlmostEqual(summary["models"][0]["share"], 100.0 * 2 / 3)
        self.assertAlmostEqual(summary["models"][1]["share"], 100.0 / 3)

    def test_format_report(self):
        summary = usage.summarise([
            {"day": "2026-11-17", "kind": "cli", "model": "opus",
             "output": 1000, "cache_read": 100, "weighted": 446000.0},
        ])
        report = usage.format_report(summary, {"2026-11-17": 2})
        self.assertIn("2026-11-17", report)
        self.assertIn("commits 2", report)
        self.assertIn("total", report)
        self.assertIn("model opus", report)
        self.assertIn("per commit 223.0k", report)
        self.assertNotIn("output(M)", report)
        self.assertIn("output 1000", report)
        row = next(
            line for line in report.splitlines()
            if line.startswith("2026-11-17  cli")
        )
        self.assertEqual(row.split()[4], "1000")
        no_commits = usage.format_report(summary, {})
        self.assertIn("per commit -", no_commits)
        self.assertIn("commits 0", no_commits)
        unavailable = usage.format_report(summary, None)
        self.assertIn(
            "commits: unavailable (git log failed for the integration branch)",
            unavailable,
        )
        self.assertIn("commits -", unavailable)
        self.assertIn("per commit -", unavailable)
        self.assertIn("weighted 0.4M", unavailable)
        subagents = [{
            "day": "2026-11-17", "model": "sonnet", "calls": 3,
            "weighted": 1234.0, "description": "Review plan 0042",
        }]
        with_subagents = usage.format_report(summary, {}, subagents)
        self.assertIn("Review plan 0042", with_subagents)
        self.assertIn("1.2", with_subagents)
        self.assertIn("calls", with_subagents)
        self.assertIn("      3", with_subagents)

    def git(self, repo, *args, env=None):
        base = dict(os.environ)
        base["GIT_CONFIG_GLOBAL"] = os.devnull
        base["GIT_CONFIG_SYSTEM"] = os.devnull
        if env:
            base.update(env)
        return subprocess.run(
            [
                "git", "-C", str(repo),
                "-c", "user.name=t", "-c", "user.email=t@example.com",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                *args,
            ],
            capture_output=True, text=True, check=True, env=base,
        )

    def test_commits_per_day(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        (repo / "README").write_text("hello\n")
        self.git(repo, "add", "-A")
        for hour in ("10", "11"):
            self.git(
                repo, "commit", "--allow-empty", "-m", f"commit {hour}",
                env={
                    "GIT_AUTHOR_DATE": f"2026-11-17T{hour}:00:00Z",
                    "GIT_COMMITTER_DATE": f"2026-11-17T{hour}:00:00Z",
                },
            )
        self.assertEqual(
            usage.commits_per_day(repo, "main", "2026-11-16", "2026-11-18"),
            {"2026-11-17": 2},
        )
        self.assertEqual(
            usage.commits_per_day(repo, "missing", "2026-11-16", "2026-11-18"),
            None,
        )
        self.assertEqual(
            usage.commits_per_day(repo, "main", "2026-11-18", "2026-11-19"),
            {},
        )


if __name__ == "__main__":
    unittest.main()
