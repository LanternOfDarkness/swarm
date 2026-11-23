"""Claude Code context hooks for the swarm PM session."""

import copy
import json
import os
from pathlib import Path

from swarm import config, db, gitscan, status, util

SESSION_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _active() -> bool:
    return os.environ.get("SWARM_PM") == "1"


def _token_count(usage: dict) -> int:
    total = 0
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += int(value)
    return total


def _last_usage_tokens(path: str):
    tokens = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                tokens = _token_count(usage)
    return tokens


def _warn_path(session_id: str):
    return util.home() / f"pm-warn-{session_id}"


def _last_warned(session_id) -> int:
    if not session_id:
        return 0
    try:
        return int(_warn_path(session_id).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _offer_connect(conn, data) -> None:
    cwd = data.get("cwd")
    if not cwd:
        return
    try:
        top = Path(gitscan.git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    except (OSError, ValueError):
        return
    if not str(top):
        return
    for row in conn.execute("SELECT repo_path FROM projects WHERE archived = 0"):
        if row["repo_path"] and Path(row["repo_path"]).resolve() == top:
            return
    print(
        f"This repo ({top}) is not connected to the swarm. If the operator wants "
        f"swarm to work on it, offer: swarm connect {top} --dry-run, then the "
        "same with --yes."
    )


def session_start(stdin_text: str) -> int:
    try:
        if not _active():
            return 0
        data = json.loads(stdin_text)
        source = data.get("source")
        conn = db.connect()
        try:
            cfg = config.load()
            if source in SESSION_SOURCES:
                print(
                    "Swarm PM context (from the board):\n"
                    + status.format_brief(conn, cfg)
                )
                _offer_connect(conn, data)
            db.add_event(conn, "note", f"PM session {source}")
        finally:
            conn.close()
    except Exception:
        return 0
    return 0


def stop(stdin_text: str) -> int:
    try:
        if not _active():
            return 0
        data = json.loads(stdin_text)
        if not isinstance(data, dict):
            return 0
        transcript = data.get("transcript_path")
        if not transcript:
            return 0
        tokens = _last_usage_tokens(transcript)
        if tokens is None:
            return 0
        cfg = config.load()
        session_id = data.get("session_id")
        last_warned = _last_warned(session_id)
        if (
            tokens >= cfg.pm_context_warn_tokens
            and tokens >= last_warned + cfg.pm_context_warn_step
        ):
            message = (
                f"PM context is {tokens // 1000}k tokens. Type /clean-pm (or ask "
                "for a handoff, then /clear) — the board keeps everything."
            )
            print(json.dumps({"systemMessage": message}))
            if session_id:
                _warn_path(session_id).write_text(str(tokens), encoding="utf-8")
    except Exception:
        return 0
    return 0


def _has_command(entry, command: str) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict) and hook.get("command") == command for hook in hooks
    )


def merge_settings(existing: dict, command_prefix: str) -> dict:
    merged = copy.deepcopy(existing)
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        merged["hooks"] = hooks

    entries = (
        (
            "SessionStart",
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{command_prefix} hook session-start",
                    }
                ],
            },
        ),
        (
            "Stop",
            {
                "hooks": [
                    {"type": "command", "command": f"{command_prefix} hook stop"}
                ]
            },
        ),
    )

    for event, entry in entries:
        current = hooks.get(event)
        if not isinstance(current, list):
            current = []
            hooks[event] = current
        command = entry["hooks"][0]["command"]
        if any(_has_command(item, command) for item in current):
            continue
        current.append(copy.deepcopy(entry))
    return merged
