import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from swarm import board, db, hooks

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PM_SCRIPT = REPO_ROOT / "scripts" / "pm.sh"
PREFIX = "/home/tester/.local/bin/swarm"


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_pm = os.environ.get("SWARM_PM")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ.pop("SWARM_PM", None)

    def tearDown(self):
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_pm is None:
            os.environ.pop("SWARM_PM", None)
        else:
            os.environ["SWARM_PM"] = self._old_pm

    def run_hook(self, func, text):
        out = io.StringIO()
        with redirect_stdout(out):
            code = func(text)
        return code, out.getvalue()

    def transcript(self, tokens):
        path = pathlib.Path(self.tmp.name) / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input_tokens": tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            },
            {"type": "user", "message": {"role": "user", "content": "more"}},
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return str(path)

    def stop_payload(self, tokens, session_id="sess-1"):
        return json.dumps(
            {
                "session_id": session_id,
                "transcript_path": self.transcript(tokens),
            }
        )

    def test_disabled_prints_nothing(self):
        for func in (hooks.session_start, hooks.stop):
            code, out = self.run_hook(func, '{"source": "clear"}')
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_malformed_json_returns_zero(self):
        for enabled in (False, True):
            if enabled:
                os.environ["SWARM_PM"] = "1"
            else:
                os.environ.pop("SWARM_PM", None)
            for func in (hooks.session_start, hooks.stop):
                code, out = self.run_hook(func, "not json {")
                self.assertEqual(code, 0)
                self.assertEqual(out, "")

    def test_session_start_prints_brief_and_notes(self):
        os.environ["SWARM_PM"] = "1"
        code, out = self.run_hook(hooks.session_start, '{"source": "clear"}')
        self.assertEqual(code, 0)
        self.assertIn("Swarm PM context (from the board):", out)
        self.assertIn("Recent decisions", out)
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT text FROM events WHERE kind = 'note'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row["text"] for row in rows], ["PM session clear"])

    def _repo(self):
        path = pathlib.Path(self.tmp.name) / "repo"
        path.mkdir()
        subprocess.run(
            ["git", "-C", str(path), "init", "-q"],
            check=True, capture_output=True,
        )
        return path.resolve()

    def test_session_start_offers_connect_for_unconnected_repo(self):
        repo = self._repo()
        os.environ["SWARM_PM"] = "1"
        payload = json.dumps({"source": "clear", "cwd": str(repo)})
        code, out = self.run_hook(hooks.session_start, payload)
        self.assertEqual(code, 0)
        self.assertIn(
            f"This repo ({repo}) is not connected to the swarm. If the operator "
            f"wants swarm to work on it, offer: swarm connect {repo} --dry-run, "
            "then the same with --yes.",
            out,
        )
        self.assertEqual(out.count("is not connected to the swarm"), 1)

    def test_session_start_no_offer_for_connected_repo(self):
        repo = self._repo()
        conn = db.connect()
        try:
            board.add_project(conn, "connected", str(repo))
        finally:
            conn.close()
        os.environ["SWARM_PM"] = "1"
        payload = json.dumps({"source": "clear", "cwd": str(repo)})
        code, out = self.run_hook(hooks.session_start, payload)
        self.assertEqual(code, 0)
        self.assertNotIn("is not connected to the swarm", out)

    def test_session_start_no_offer_outside_a_repo(self):
        plain = pathlib.Path(self.tmp.name) / "plain"
        plain.mkdir()
        os.environ["SWARM_PM"] = "1"
        payload = json.dumps({"source": "clear", "cwd": str(plain)})
        code, out = self.run_hook(hooks.session_start, payload)
        self.assertEqual(code, 0)
        self.assertNotIn("is not connected to the swarm", out)

    def test_session_start_no_offer_when_inactive(self):
        repo = self._repo()
        payload = json.dumps({"source": "clear", "cwd": str(repo)})
        code, out = self.run_hook(hooks.session_start, payload)
        self.assertEqual(code, 0)
        self.assertNotIn("is not connected to the swarm", out)

    def test_stop_warns_then_holds_then_warns_again(self):
        os.environ["SWARM_PM"] = "1"
        code, out = self.run_hook(hooks.stop, self.stop_payload(160000))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("160k tokens", payload["systemMessage"])
        self.assertIn("/clean-pm", payload["systemMessage"])

        code, out = self.run_hook(hooks.stop, self.stop_payload(170000))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

        code, out = self.run_hook(hooks.stop, self.stop_payload(180000))
        self.assertEqual(code, 0)
        self.assertIn("180k tokens", json.loads(out)["systemMessage"])

    def test_stop_silent_below_threshold(self):
        os.environ["SWARM_PM"] = "1"
        code, out = self.run_hook(hooks.stop, self.stop_payload(140000, "sess-2"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_merge_empty_settings(self):
        merged = hooks.merge_settings({}, PREFIX)
        starts = merged["hooks"]["SessionStart"]
        stops = merged["hooks"]["Stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["matcher"], "startup|resume|clear|compact")
        self.assertEqual(
            starts[0]["hooks"],
            [{"type": "command", "command": f"{PREFIX} hook session-start"}],
        )
        self.assertEqual(
            stops[0]["hooks"],
            [{"type": "command", "command": f"{PREFIX} hook stop"}],
        )

    def test_merge_preserves_existing(self):
        existing = {
            "model": "opus",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
                ],
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "other"}],
                    }
                ],
            },
        }
        merged = hooks.merge_settings(existing, PREFIX)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 2)
        self.assertEqual(merged["hooks"]["SessionStart"][0]["matcher"], "startup")
        self.assertEqual(merged["hooks"]["SessionStart"][1]["matcher"],
                         "startup|resume|clear|compact")

    def test_merge_is_idempotent(self):
        once = hooks.merge_settings({}, PREFIX)
        twice = hooks.merge_settings(once, PREFIX)
        self.assertEqual(twice, once)
        self.assertEqual(len(twice["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(twice["hooks"]["Stop"]), 1)

    def test_pm_script(self):
        self.assertTrue(PM_SCRIPT.exists())
        self.assertTrue(os.access(PM_SCRIPT, os.X_OK))
        self.assertTrue(PM_SCRIPT.read_text().startswith("#!/usr/bin/env sh\n"))


if __name__ == "__main__":
    unittest.main()
