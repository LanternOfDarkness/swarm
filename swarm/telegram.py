"""Telegram bot API client and config for the swarm operator.

The token never appears in output: every error text goes through ``_mask``,
which replaces it with ``<token>``.
"""

import http.client
import json
import os
import sys
import time
import tomllib
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from swarm import board, status

_TOKEN = ""
CUT_SUFFIX = "… (cut — /status in the terminal for more)"
MAX_TEXT = 4000
API_URL = "https://api.telegram.org/bot{token}/{method}"


def _mask(text):
    text = str(text)
    if _TOKEN and _TOKEN in text:
        text = text.replace(_TOKEN, "<token>")
    return text


class TelegramError(Exception):
    def __init__(self, code, description):
        self.code = int(code)
        self.description = str(description)
        super().__init__(self.description)


def load_config(home):
    path = Path(home) / "telegram.toml"
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError:
        raise ValueError(
            f"telegram.toml needs bot_token and chat_id: {path}") from None
    token = str(data.get("bot_token") or "").strip()
    chat = str(data.get("chat_id") or "").strip()
    if not token or not chat:
        raise ValueError(
            f"telegram.toml needs bot_token and chat_id: {path}")
    return token, chat


def _http_error(exc):
    try:
        parsed = json.loads(exc.read())
    except (OSError, ValueError, http.client.HTTPException):
        parsed = None
    if isinstance(parsed, dict) and not parsed.get("ok"):
        return TelegramError(
            parsed.get("error_code") or exc.code,
            _mask(str(parsed.get("description") or exc)))
    return TelegramError(exc.code, _mask(str(exc)))


class TelegramApi:
    def __init__(self, token):
        global _TOKEN
        self.token = token
        _TOKEN = token

    def call(self, method, params):
        url = API_URL.format(token=self.token, method=method)
        body = json.dumps(params).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
            method="POST")
        timeout = int(params.get("timeout", 0)) + 15
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise TelegramError(0, _mask(str(exc))) from None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TelegramError(
                0, _mask(f"bad response: {exc}")) from exc
        if not isinstance(payload, dict):
            raise TelegramError(0, _mask("bad response: not an object"))
        if payload.get("ok"):
            return payload.get("result")
        raise TelegramError(
            payload.get("error_code") or 0,
            _mask(str(payload.get("description") or "telegram error")))

    def get_updates(self, offset, timeout=25):
        result = self.call("getUpdates", {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message"],
        })
        return result or []

    def send(self, chat_id, text, reply_to=None):
        cut = text
        if len(cut) > MAX_TEXT:
            cut = cut[:MAX_TEXT - len(CUT_SUFFIX)] + CUT_SUFFIX
        params = {"chat_id": chat_id, "text": cut}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        result = self.call("sendMessage", params)
        return int(result["message_id"])


ASK_MESSAGES_MAX = 500


def _trim_ask_messages(messages):
    while len(messages) > ASK_MESSAGES_MAX:
        del messages[min(messages, key=int)]


def notify(conn, cfg, api, chat_id, state):
    """Send pending notifications; return how many messages were sent.

    State is mutated in place and never written to disk here. A TelegramError
    from ``api.send`` propagates and leaves the failing item for the next call.
    """
    sent = 0
    if state.get("last_ask_id") is None:
        row = conn.execute("SELECT MAX(id) AS max_id FROM asks").fetchone()
        state["last_ask_id"] = int(row["max_id"] or 0)
        row = conn.execute("SELECT MAX(id) AS max_id FROM events").fetchone()
        state["last_event_id"] = int(row["max_id"] or 0)
        state["daemon_on"] = status._daemon_state(cfg)["on"]
        state.setdefault("ask_messages", {})
        text = (f"Swarm bot is on. Open asks: {len(status._asks(conn))}. "
                "/help for commands.")
        api.send(chat_id, text)
        return 1

    messages = state.setdefault("ask_messages", {})
    rows = conn.execute(
        "SELECT a.id, a.project, a.destructive, a.question, "
        "p.project AS plan_project, p.ref AS plan_ref, "
        "COALESCE((SELECT archived FROM projects WHERE name = a.project), 0) "
        "AS archived "
        "FROM asks a LEFT JOIN plans p ON p.id = a.plan_id "
        "WHERE a.id > ? AND a.state = 'OPEN' ORDER BY a.id",
        (state["last_ask_id"],),
    )
    for row in rows:
        if row["archived"]:
            state["last_ask_id"] = row["id"]
            continue
        if row["plan_project"] and row["plan_ref"]:
            label = f"{row['plan_project']}/{row['plan_ref']}"
        else:
            label = row["project"] or "swarm"
        if row["destructive"]:
            text = (f"ASK #{row['id']} · {label}\n{row['question']}\n\n"
                    "Destructive: answer it in the terminal with "
                    f"swarm answer {row['id']} \"...\"")
        else:
            text = (f"ASK #{row['id']} · {label}\n{row['question']}\n\n"
                    "Reply to this message to answer.")
        message_id = api.send(chat_id, text)
        messages[str(message_id)] = int(row["id"])
        _trim_ask_messages(messages)
        state["last_ask_id"] = row["id"]
        sent += 1

    events = conn.execute(
        "SELECT id, project, kind, text, "
        "COALESCE((SELECT archived FROM projects WHERE name = events.project), 0) "
        "AS archived FROM events WHERE id > ? ORDER BY id",
        (state["last_event_id"],),
    )
    for event in events:
        text = event["text"] or ""
        if event["kind"] == "move" and not event["archived"]:
            for marker, needle in (("BLOCKED", "→ BLOCKED"),
                                   ("MERGED", "→ MERGED"),
                                   ("DONE", "→ DONE")):
                if needle in text:
                    api.send(chat_id, f"{marker}: {text}")
                    sent += 1
                    break
        state["last_event_id"] = event["id"]

    current = status._daemon_state(cfg)
    if current["on"] != state.get("daemon_on"):
        if current["on"]:
            text = "Daemon is on."
        elif current["age_s"] is not None:
            text = f"Daemon is OFF (last tick {current['age_s']}s ago)."
        else:
            text = "Daemon is OFF."
        api.send(chat_id, text)
        sent += 1
        state["daemon_on"] = current["on"]

    return sent


HELP_TEXT = "\n".join([
    "/answer <id> <text> — answer an ask",
    "/goal <project> | <title> | <done when> [| <est minutes>] — queue a goal",
    "/status — board summary",
    "/brief — short brief",
    "/asks — open asks",
    "/runs — running workers",
    "Reply to an ASK message to answer it.",
])

GOAL_USAGE = "Usage: /goal project | title | done when [| est minutes]"
ANSWER_USAGE = "Usage: /answer ID text"


def _answer(conn, ask_id, text):
    row = conn.execute(
        "SELECT state, destructive FROM asks WHERE id = ?", (ask_id,)
    ).fetchone()
    if row is None:
        return f"No ask #{ask_id}."
    if row["state"] != "OPEN":
        return f"Ask #{ask_id} is already {row['state']}."
    if row["destructive"]:
        return "Destructive ask: answer it in the terminal."
    board.answer_ask(conn, ask_id, text)
    return f"Answered #{ask_id}. The daemon picks it up on its next pass."


def _goal(conn, rest):
    parts = [part.strip() for part in rest.split("|")]
    est_minutes = None
    if len(parts) not in (3, 4) or not all(parts):
        return GOAL_USAGE
    if len(parts) == 4:
        try:
            est_minutes = int(parts[3])
        except ValueError:
            return GOAL_USAGE
    try:
        goal_id = board.add_goal(conn, parts[0], parts[1], parts[2],
                                 est_minutes=est_minutes)
    except ValueError as exc:
        return str(exc)
    return f"Goal #{goal_id} queued for {parts[0]}: {parts[1]}."


def _asks_text(conn):
    lines = []
    for item in status._asks(conn):
        label = item["plan"] or item["project"] or "swarm"
        question = item["question"] or ""
        first_line = question.splitlines()[0][:100] if question else ""
        lines.append(f"#{item['id']} {label}: {first_line}")
    return "\n".join(lines) if lines else "No open asks."


def _runs_text(conn):
    rows = conn.execute(
        "SELECT r.id, r.kind, r.purpose, r.started_at, r.cost_usd, "
        "p.project AS plan_project, p.ref AS plan_ref "
        "FROM runs r LEFT JOIN plans p ON p.id = r.plan_id "
        "WHERE r.status = 'RUNNING' ORDER BY r.id"
    )
    lines = []
    for row in rows:
        if row["plan_project"] and row["plan_ref"]:
            plan = f"{row['plan_project']}/{row['plan_ref']}"
        else:
            plan = "-"
        age = status._minutes_since(row["started_at"]) or 0
        cost = row["cost_usd"] or 0
        lines.append(f"#{row['id']} {row['kind']}/{row['purpose'] or '-'} "
                     f"{plan} {age}m ${cost:.2f}")
    return "\n".join(lines) if lines else "No workers running."


def handle(conn, cfg, api, chat_id, state, update):
    """Handle one Telegram update; reply through ``api`` when appropriate."""
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = (message.get("chat") or {}).get("id")
    if str(chat) != str(chat_id):
        return

    reply_to = message.get("message_id")

    def reply(text):
        api.send(chat_id, text, reply_to=reply_to)

    text = message.get("text")
    if not isinstance(text, str):
        reply("Text messages only.")
        return
    text = text.strip()

    messages = state.get("ask_messages") or {}
    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        key = str(replied.get("message_id"))
        if key in messages:
            reply(_answer(conn, int(messages[key]), text))
            return

    first_word = text.split(None, 1)[0] if text else ""
    command = first_word.split("@", 1)[0]
    rest = text[len(first_word):].strip()

    if command == "/answer":
        parts = rest.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            reply(ANSWER_USAGE)
        else:
            reply(_answer(conn, int(parts[0]), parts[1]))
    elif command == "/goal":
        reply(_goal(conn, rest))
    elif command == "/status":
        reply(status.format_status(status.build_status(conn, cfg)))
    elif command == "/brief":
        reply(status.format_brief(conn, cfg))
    elif command == "/asks":
        reply(_asks_text(conn))
    elif command == "/runs":
        reply(_runs_text(conn))
    elif command in ("/start", "/help"):
        reply(HELP_TEXT)
    else:
        reply("Unknown command. /help")


DEFAULT_STATE = {
    "offset": 0,
    "last_ask_id": None,
    "last_event_id": None,
    "daemon_on": None,
    "ask_messages": {},
}


def load_state(path):
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {
            "offset": DEFAULT_STATE["offset"],
            "last_ask_id": DEFAULT_STATE["last_ask_id"],
            "last_event_id": DEFAULT_STATE["last_event_id"],
            "daemon_on": DEFAULT_STATE["daemon_on"],
            "ask_messages": {},
        }
    state = dict(DEFAULT_STATE)
    state.update(data)
    state["ask_messages"] = dict(data.get("ask_messages") or {})
    return state


def save_state(path, state):
    path = Path(path)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _sleep(stop, seconds):
    remaining = float(seconds)
    while remaining > 0:
        if stop():
            return
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step


def run(conn, cfg, api, chat_id, state_path, once=False, stop=lambda: False):
    """Poll Telegram and handle updates until stopped (or one pass with once)."""
    state = load_state(state_path)
    while True:
        try:
            notify(conn, cfg, api, chat_id, state)
            save_state(state_path, state)
            updates = api.get_updates(
                int(state.get("offset") or 0), timeout=0 if once else 25)
            for update in updates:
                state["offset"] = int(update["update_id"]) + 1
                try:
                    handle(conn, cfg, api, chat_id, state, update)
                except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                    sys.stderr.write(_mask(traceback.format_exc()))
                    try:
                        message = update.get("message") or {}
                        api.send(
                            chat_id,
                            _mask(f"Error: {type(exc).__name__}: {exc}"),
                            reply_to=message.get("message_id"),
                        )
                    except Exception:  # noqa: BLE001 - the reply is best effort
                        sys.stderr.write(_mask(traceback.format_exc()))
                save_state(state_path, state)
            save_state(state_path, state)
        except TelegramError as exc:
            if exc.code in (401, 409):
                raise
            save_state(state_path, state)
            print(f"swarm: telegram: {_mask(str(exc))}", file=sys.stderr)
            _sleep(stop, 10)
            if once:
                return
        if once or stop():
            return
