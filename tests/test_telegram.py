import contextlib
import io
import json
import os
import shutil
import tempfile
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from swarm import board, config, db, status, telegram, util

TOKEN = "123:fake-token"
CHAT = "42"
NOW = "2026-11-17T04:39:00Z"


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def request_json(req):
    return json.loads(req.data.decode())


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name
        self._old_token = telegram._TOKEN
        telegram._TOKEN = ""

    def tearDown(self):
        telegram._TOKEN = self._old_token
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def test_mask_replaces_token_only(self):
        telegram._TOKEN = TOKEN
        self.assertEqual(
            telegram._mask(f"url https://api.telegram.org/bot{TOKEN}/getMe"),
            "url https://api.telegram.org/bot<token>/getMe")
        self.assertEqual(telegram._mask("nothing secret here"),
                         "nothing secret here")

    def test_mask_uses_token_from_api(self):
        api = telegram.TelegramApi(TOKEN)
        self.assertEqual(api.token, TOKEN)
        self.assertEqual(telegram._mask(f"boom {TOKEN}"), "boom <token>")

    def test_load_config_reads_both_values(self):
        home = Path(self.tmp.name)
        (home / "telegram.toml").write_text(
            f'bot_token = "{TOKEN}"\nchat_id = "42"\n')
        self.assertEqual(telegram.load_config(home), (TOKEN, "42"))

    def test_load_config_accepts_numeric_chat_id(self):
        home = Path(self.tmp.name)
        (home / "telegram.toml").write_text(
            f'bot_token = "{TOKEN}"\nchat_id = 42\n')
        self.assertEqual(telegram.load_config(home), (TOKEN, "42"))

    def test_load_config_missing_file(self):
        home = Path(self.tmp.name)
        path = home / "telegram.toml"
        with self.assertRaises(ValueError) as ctx:
            telegram.load_config(home)
        self.assertEqual(
            str(ctx.exception),
            f"telegram.toml needs bot_token and chat_id: {path}")

    def test_load_config_empty_value(self):
        home = Path(self.tmp.name)
        path = home / "telegram.toml"
        path.write_text(f'bot_token = "  "\nchat_id = "{CHAT}"\n')
        with self.assertRaises(ValueError) as ctx:
            telegram.load_config(home)
        self.assertEqual(
            str(ctx.exception),
            f"telegram.toml needs bot_token and chat_id: {path}")

    def test_send_cuts_long_text(self):
        api = telegram.TelegramApi(TOKEN)
        long_text = "x" * 5000
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(
                            {"ok": True,
                             "result": {"message_id": 5}})) as urlopen:
            self.assertEqual(api.send(CHAT, long_text), 5)
        req = urlopen.call_args.args[0]
        self.assertEqual(
            req.full_url,
            f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        body = request_json(req)
        self.assertEqual(body["chat_id"], CHAT)
        self.assertEqual(len(body["text"]), 4000)
        self.assertTrue(body["text"].endswith(telegram.CUT_SUFFIX))

    def test_send_short_text_and_reply_to(self):
        api = telegram.TelegramApi(TOKEN)
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(
                            {"ok": True,
                             "result": {"message_id": 7}})) as urlopen:
            self.assertEqual(api.send(CHAT, "hi", reply_to=3), 7)
        body = request_json(urlopen.call_args.args[0])
        self.assertEqual(body, {"chat_id": CHAT, "text": "hi",
                                "reply_to_message_id": 3})

    def test_send_error_reply_raises(self):
        api = telegram.TelegramApi(TOKEN)
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(
                            {"ok": False, "error_code": 400,
                             "description": "Bad Request: chat not found"})):
            with self.assertRaises(telegram.TelegramError) as ctx:
                api.send(CHAT, "hi")
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(ctx.exception.description,
                         "Bad Request: chat not found")

    def test_http_error_maps_code_and_masks(self):
        api = telegram.TelegramApi(TOKEN)
        body = io.BytesIO(json.dumps(
            {"ok": False, "error_code": 401,
             "description": f"Unauthorized {TOKEN}"}).encode())
        exc = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            401, "Unauthorized", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(telegram.TelegramError) as ctx:
                api.call("sendMessage", {})
        self.assertEqual(ctx.exception.code, 401)
        self.assertNotIn(TOKEN, ctx.exception.description)
        self.assertIn("<token>", ctx.exception.description)

    def test_network_error_masks_token(self):
        api = telegram.TelegramApi(TOKEN)
        exc = urllib.error.URLError(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(telegram.TelegramError) as ctx:
                api.get_updates(0)
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn(TOKEN, ctx.exception.description)
        self.assertIn("<token>", ctx.exception.description)

    def test_get_updates_sends_offset_and_timeout(self):
        api = telegram.TelegramApi(TOKEN)
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(
                            {"ok": True,
                             "result": [{"update_id": 3}]})) as urlopen:
            self.assertEqual(api.get_updates(3), [{"update_id": 3}])
        req = urlopen.call_args.args[0]
        body = request_json(req)
        self.assertEqual(body, {"offset": 3, "timeout": 25,
                                "allowed_updates": ["message"]})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 40)

    def test_get_updates_empty_result(self):
        api = telegram.TelegramApi(TOKEN)
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(
                            {"ok": True, "result": []})):
            self.assertEqual(api.get_updates(0, timeout=0), [])


class FakeApi:
    def __init__(self, start=1, fail_at=None, updates=None):
        self.sent = []
        self.next_id = start
        self.fail_at = fail_at
        self.calls = 0
        self.updates = list(updates or [])

    def get_updates(self, offset, timeout=25):
        return list(self.updates)

    def send(self, chat_id, text, reply_to=None):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise telegram.TelegramError(0, "boom")
        message_id = self.next_id
        self.next_id += 1
        self.sent.append((chat_id, text, reply_to))
        return message_id


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.cfg = config.load()
        self.conn = db.connect()
        board.add_project(self.conn, "demo", self.repo)
        self.api = FakeApi()
        self.state = {
            "offset": 0,
            "last_ask_id": None,
            "last_event_id": None,
            "daemon_on": None,
            "ask_messages": {},
        }

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo, ignore_errors=True)
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_now is None:
            os.environ.pop("SWARM_NOW", None)
        else:
            os.environ["SWARM_NOW"] = self._old_now

    def notify(self, api=None):
        return telegram.notify(
            self.conn, self.cfg, self.api if api is None else api, CHAT,
            self.state)

    def hello(self):
        self.assertEqual(self.notify(), 1)
        self.assertEqual(len(self.api.sent), 1)
        return self.api.sent[0][1]

    def max_event_id(self):
        return self.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def test_first_run_on_empty_board_sends_only_hello(self):
        text = self.hello()
        self.assertEqual(
            text, "Swarm bot is on. Open asks: 0. /help for commands.")
        self.assertEqual(self.api.sent[0], (CHAT, text, None))
        self.assertEqual(self.state["last_ask_id"], 0)
        self.assertEqual(self.state["last_event_id"], self.max_event_id())
        self.assertIs(self.state["daemon_on"], False)
        self.assertEqual(self.state["ask_messages"], {})

        self.assertEqual(self.notify(), 0)
        self.assertEqual(len(self.api.sent), 1)

    def test_first_run_with_open_ask_sends_only_hello(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        self.hello()
        self.assertEqual(self.api.sent[0][1],
                         "Swarm bot is on. Open asks: 1. /help for commands.")
        self.assertEqual(self.state["last_ask_id"], ask_id)
        self.assertEqual(self.api.sent[0][2], None)

    def test_archived_project_ask_and_move_send_nothing(self):
        self.hello()
        self.api.sent.clear()
        board.archive_project(self.conn, "demo")
        board.add_plan(self.conn, "demo", "a", "Archived plan")
        board.move_plan(self.conn, "demo", "a", "BLOCKED")
        ask_id = board.add_ask(self.conn, "Archived?", project="demo")

        self.assertEqual(self.notify(), 0)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.state["last_ask_id"], ask_id)
        self.assertEqual(self.state["last_event_id"], self.max_event_id())

    def test_ask_without_project_is_sent_and_mapped(self):
        self.hello()
        self.api.sent.clear()
        ask_id = board.add_ask(self.conn, "No project question")

        self.assertEqual(self.notify(), 1)
        self.assertEqual(len(self.api.sent), 1)
        chat_id, text, reply_to = self.api.sent[0]
        self.assertEqual(chat_id, CHAT)
        self.assertIsNone(reply_to)
        self.assertIn(f"ASK #{ask_id} · swarm", text)
        self.assertIn("No project question", text)
        self.assertEqual(
            self.state["ask_messages"],
            {str(self.api.next_id - 1): ask_id})
        self.assertEqual(self.state["last_ask_id"], ask_id)

    def test_new_ask_in_live_project(self):
        self.hello()
        self.api.sent.clear()
        plan_id = board.add_plan(self.conn, "demo", "0001", "A plan")
        ask_id = board.add_ask(
            self.conn, "Proceed?", project="demo", plan_id=plan_id)

        self.assertEqual(self.notify(), 1)
        self.assertEqual(len(self.api.sent), 1)
        chat_id, text, reply_to = self.api.sent[0]
        self.assertEqual(chat_id, CHAT)
        self.assertIsNone(reply_to)
        self.assertIn(f"ASK #{ask_id} · demo/0001", text)
        self.assertIn("Proceed?", text)
        self.assertIn("Reply to this message to answer.", text)
        self.assertEqual(
            self.state["ask_messages"],
            {str(self.api.next_id - 1): ask_id})
        self.assertEqual(self.state["last_ask_id"], ask_id)

    def test_destructive_ask_points_to_terminal(self):
        self.hello()
        self.api.sent.clear()
        ask_id = board.add_ask(
            self.conn, "Drop production?", project="demo", destructive=True)

        self.assertEqual(self.notify(), 1)
        text = self.api.sent[0][1]
        self.assertIn(
            f"Destructive: answer it in the terminal with swarm answer {ask_id}",
            text)
        self.assertNotIn("Reply to this message", text)

    def test_moves_to_blocked_merged_done(self):
        board.add_plan(self.conn, "demo", "a", "A")
        board.move_plan(self.conn, "demo", "a", "REVIEW")
        board.add_plan(self.conn, "demo", "b", "B")
        for target in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING"):
            board.move_plan(self.conn, "demo", "b", target)
        board.add_plan(self.conn, "demo", "c", "C")
        for target in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING",
                       "MERGED"):
            board.move_plan(self.conn, "demo", "c", target)
        board.add_plan(self.conn, "demo", "d", "D")

        self.hello()
        self.api.sent.clear()

        board.move_plan(self.conn, "demo", "a", "BLOCKED")
        board.move_plan(self.conn, "demo", "b", "MERGED")
        board.move_plan(self.conn, "demo", "c", "DONE")
        board.move_plan(self.conn, "demo", "d", "REVIEW")

        self.assertEqual(self.notify(), 3)
        self.assertEqual([sent[1] for sent in self.api.sent], [
            "BLOCKED: demo/a REVIEW → BLOCKED",
            "MERGED: demo/b VERIFYING → MERGED",
            "DONE: demo/c MERGED → DONE",
        ])
        self.assertEqual(self.state["last_event_id"], self.max_event_id())

    def test_daemon_on_off_on_sends_two_messages(self):
        heartbeat = Path(self.tmp.name) / "daemon.heartbeat"
        heartbeat.write_text(util.now())
        self.hello()
        self.api.sent.clear()

        heartbeat.write_text("2026-11-17T04:38:00Z")
        self.assertEqual(self.notify(), 1)
        self.assertEqual(self.api.sent[0][1],
                         "Daemon is OFF (last tick 60s ago).")

        heartbeat.write_text(util.now())
        self.assertEqual(self.notify(), 1)
        self.assertEqual(self.api.sent[1][1], "Daemon is on.")
        self.assertEqual(len(self.api.sent), 2)
        self.assertIs(self.state["daemon_on"], True)

    def test_send_failure_retries_remaining_asks(self):
        self.hello()
        first = board.add_ask(self.conn, "First?")
        second = board.add_ask(self.conn, "Second?")

        failing = FakeApi(fail_at=2)
        with self.assertRaises(telegram.TelegramError):
            telegram.notify(
                self.conn, self.cfg, failing, CHAT, self.state)
        self.assertEqual(len(failing.sent), 1)
        self.assertIn(f"ASK #{first}", failing.sent[0][1])
        self.assertEqual(self.state["last_ask_id"], first)
        self.assertEqual(
            self.state["ask_messages"],
            {str(failing.next_id - 1): first})

        working = FakeApi()
        self.assertEqual(
            telegram.notify(self.conn, self.cfg, working, CHAT, self.state),
            1)
        self.assertEqual(len(working.sent), 1)
        self.assertIn(f"ASK #{second}", working.sent[0][1])
        self.assertEqual(self.state["last_ask_id"], second)


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.cfg = config.load()
        self.conn = db.connect()
        board.add_project(self.conn, "demo", self.repo)
        self.api = FakeApi()
        self.state = {
            "offset": 0,
            "last_ask_id": None,
            "last_event_id": None,
            "daemon_on": None,
            "ask_messages": {},
        }

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo, ignore_errors=True)
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_now is None:
            os.environ.pop("SWARM_NOW", None)
        else:
            os.environ["SWARM_NOW"] = self._old_now

    def handle(self, text=None, message_id=10, reply_to_message=None,
               chat_id=42):
        message = {"message_id": message_id, "chat": {"id": chat_id}}
        if text is not None:
            message["text"] = text
        if reply_to_message is not None:
            message["reply_to_message"] = reply_to_message
        telegram.handle(self.conn, self.cfg, self.api, CHAT, self.state,
                        {"update_id": 1, "message": message})
        return self.api.sent[-1] if self.api.sent else None

    def add_plan(self, ref="0007"):
        return board.add_plan(self.conn, "demo", ref, "Plan")

    def ask_row(self, ask_id):
        return self.conn.execute(
            "SELECT state, answer FROM asks WHERE id = ?", (ask_id,)
        ).fetchone()

    def test_help_and_start_send_the_command_list(self):
        expected = "\n".join([
            "/answer <id> <text> — answer an ask",
            "/goal <project> | <title> | <done when> [| <est minutes>] — queue a goal",
            "/status — board summary",
            "/brief — short brief",
            "/asks — open asks",
            "/runs — running workers",
            "Reply to an ASK message to answer it.",
        ])
        self.assertEqual(self.handle("/help")[1], expected)
        self.assertEqual(self.handle("/start")[1], expected)

    def test_status_equals_format_status(self):
        expected = status.format_status(status.build_status(self.conn, self.cfg))
        self.assertEqual(self.handle("/status")[1], expected)

    def test_brief_equals_format_brief(self):
        expected = status.format_brief(self.conn, self.cfg)
        self.assertEqual(self.handle("/brief")[1], expected)

    def test_status_with_bot_suffix(self):
        expected = status.format_status(status.build_status(self.conn, self.cfg))
        self.assertEqual(self.handle("/status@mybot")[1], expected)

    def test_asks_lists_plan_and_projectless_asks(self):
        plan_id = self.add_plan()
        first = board.add_ask(self.conn, "Plan question?", plan_id=plan_id)
        second = board.add_ask(self.conn, "Loose question?")
        self.assertEqual(
            self.handle("/asks")[1],
            f"#{first} demo/0007: Plan question?\n#{second} swarm: Loose question?")

    def test_asks_empty(self):
        self.assertEqual(self.handle("/asks")[1], "No open asks.")

    def test_runs_lists_running_worker(self):
        plan_id = self.add_plan()
        run_id = board.start_run(self.conn, "implementer", plan_id=plan_id,
                                 purpose="impl")
        self.assertEqual(
            self.handle("/runs")[1],
            f"#{run_id} implementer/impl demo/0007 0m $0.00")

    def test_runs_empty(self):
        self.assertEqual(self.handle("/runs")[1], "No workers running.")

    def test_unknown_text(self):
        self.assertEqual(self.handle("hello there")[1], "Unknown command. /help")

    def test_non_text_message(self):
        self.assertEqual(self.handle(None)[1], "Text messages only.")

    def test_reply_to_mapped_ask_answers_it(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        self.state["ask_messages"] = {"77": ask_id}
        reply = self.handle("yes do it", reply_to_message={"message_id": 77})
        self.assertTrue(reply[1].startswith(f"Answered #{ask_id}."))
        row = self.ask_row(ask_id)
        self.assertEqual(row["state"], "ANSWERED")
        self.assertEqual(row["answer"], "yes do it")

    def test_reply_to_destructive_ask_is_refused(self):
        ask_id = board.add_ask(self.conn, "Delete it?", destructive=True)
        self.state["ask_messages"] = {"77": ask_id}
        reply = self.handle("yes do it", reply_to_message={"message_id": 77})
        self.assertEqual(reply[1], "Destructive ask: answer it in the terminal.")
        row = self.ask_row(ask_id)
        self.assertEqual(row["state"], "OPEN")
        self.assertIsNone(row["answer"])

    def test_stranger_chat_gets_no_reply_and_changes_nothing(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        self.state["ask_messages"] = {"77": ask_id}
        self.assertIsNone(self.handle(
            "yes do it", reply_to_message={"message_id": 77}, chat_id=999))
        self.assertIsNone(
            self.handle("/goal demo | t | d", chat_id=999))
        self.assertEqual(self.api.sent, [])
        row = self.ask_row(ask_id)
        self.assertEqual(row["state"], "OPEN")
        self.assertIsNone(row["answer"])
        count = self.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
        self.assertEqual(count, 0)

    def test_answer_command(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        reply = self.handle(f"/answer {ask_id} yes")
        self.assertTrue(reply[1].startswith(f"Answered #{ask_id}."))
        row = self.ask_row(ask_id)
        self.assertEqual(row["state"], "ANSWERED")
        self.assertEqual(row["answer"], "yes")

    def test_answer_command_bad_forms(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        for text in ("/answer", "/answer abc hi", f"/answer {ask_id}"):
            with self.subTest(text=text):
                self.assertEqual(self.handle(text)[1],
                                 "Usage: /answer ID text")

    def test_answer_command_unknown_ask(self):
        self.assertEqual(self.handle("/answer 999 hi")[1], "No ask #999.")

    def test_answer_already_answered(self):
        ask_id = board.add_ask(self.conn, "Ship it?")
        self.handle(f"/answer {ask_id} yes")
        self.assertEqual(self.handle(f"/answer {ask_id} again")[1],
                         f"Ask #{ask_id} is already ANSWERED.")

    def test_goal_command_three_parts(self):
        reply = self.handle("/goal demo | Fix it | tests pass")
        self.assertEqual(reply[1], "Goal #1 queued for demo: Fix it.")
        row = self.conn.execute(
            "SELECT project, title, done_when, est_minutes FROM goals "
            "WHERE id = 1").fetchone()
        self.assertEqual(tuple(row), ("demo", "Fix it", "tests pass", None))

    def test_goal_command_four_parts_sets_estimate(self):
        reply = self.handle("/goal demo | Fix it | tests pass | 30")
        self.assertEqual(reply[1], "Goal #1 queued for demo: Fix it.")
        row = self.conn.execute(
            "SELECT est_minutes FROM goals WHERE id = 1").fetchone()
        self.assertEqual(row["est_minutes"], 30)

    def test_goal_command_usage_errors(self):
        for text in ("/goal a | b", "/goal a | b | ", "/goal a | b | c | x"):
            with self.subTest(text=text):
                self.assertEqual(
                    self.handle(text)[1],
                    "Usage: /goal project | title | done when [| est minutes]")

    def test_goal_command_unknown_project(self):
        self.assertEqual(self.handle("/goal nope | t | d")[1],
                         "no project nope")

    def test_replies_use_incoming_message_id_and_chat(self):
        reply = self.handle("nonsense", message_id=55)
        self.assertEqual(reply, (CHAT, "Unknown command. /help", 55))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.cfg = config.load()
        self.conn = db.connect()
        board.add_project(self.conn, "demo", self.repo)
        self.state_path = Path(self.tmp.name) / "telegram-state.json"
        self.updates = [{"update_id": 7, "message": {
            "message_id": 11, "chat": {"id": 42}, "text": "hi"}}]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo, ignore_errors=True)
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_now is None:
            os.environ.pop("SWARM_NOW", None)
        else:
            os.environ["SWARM_NOW"] = self._old_now

    def test_run_masks_handler_traceback_and_replies(self):
        api = FakeApi(updates=self.updates)

        def boom(*args, **kwargs):
            raise RuntimeError(f"token {TOKEN}")

        stderr = io.StringIO()
        with mock.patch("swarm.telegram.handle", side_effect=boom), \
                contextlib.redirect_stderr(stderr):
            telegram.run(self.conn, self.cfg, api, CHAT, self.state_path,
                         once=True)
        state = telegram.load_state(self.state_path)
        self.assertEqual(state["offset"], 8)
        self.assertTrue(api.sent)
        self.assertTrue(api.sent[-1][1].startswith("Error: RuntimeError:"))
        self.assertEqual(api.sent[-1][2], 11)
        captured = stderr.getvalue()
        self.assertTrue(captured)
        self.assertNotIn(TOKEN, captured)

    def test_network_error_chain_does_not_leak_token(self):
        api = telegram.TelegramApi(TOKEN)
        error = urllib.error.URLError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(telegram.TelegramError) as caught:
                api.send(CHAT, "hi")
        exc = caught.exception
        rendered = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(TOKEN, rendered)
        self.assertIn("<token>", str(exc))

    def test_trim_ask_messages_keeps_newest(self):
        messages = {str(message_id): message_id
                    for message_id in range(1, 503)}
        telegram._trim_ask_messages(messages)
        self.assertEqual(len(messages), 500)
        self.assertEqual(set(messages), {str(i) for i in range(3, 503)})


if __name__ == "__main__":
    unittest.main()
