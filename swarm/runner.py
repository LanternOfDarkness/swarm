"""Operator-owned runner config: $SWARM_HOME/projects/<project>.toml."""

import re
import tomllib
from pathlib import Path

from swarm import config, util

GATE_WINDOW_RE = re.compile(r"^Gate window:\s*(\S+)\s*$")
DEFAULT_STEP_TIMEOUT_S = 1800


def config_path(project_name):
    return util.home() / "projects" / f"{project_name}.toml"


def _bad(path, problem):
    raise ValueError(f"{path}: {problem}")


def _table(path, value, what):
    if not isinstance(value, dict):
        _bad(path, f"{what} is not a table")
    return value


def _duration(path, value, what):
    if not isinstance(value, str):
        _bad(path, f"{what}: bad duration {value!r}")
    try:
        return config.parse_duration(value)
    except ValueError:
        _bad(path, f"{what}: bad duration {value!r}")


def _string_list(path, value, what):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _bad(path, f"{what} must be a list of strings")
    return list(value)


def _steps(path, raw):
    steps = {}
    for name, value in _table(path, raw, "steps").items():
        value = _table(path, value, f"step {name}")
        cmd = value.get("cmd")
        if not isinstance(cmd, str):
            _bad(path, f"step {name} has no cmd")
        timeout_s = _duration(
            path, value.get("timeout", "30m"), f"step {name} timeout"
        )
        locks = _string_list(path, value.get("locks", []), f"step {name} locks")
        steps[name] = {"cmd": cmd, "timeout_s": timeout_s, "locks": locks}
    return steps


def _windows(path, raw, steps):
    windows = {}
    for name, value in _table(path, raw, "windows").items():
        value = _table(path, value, f"window {name}")
        fields = {}
        for key in ("start", "length", "read"):
            item = value.get(key)
            if not isinstance(item, str):
                _bad(path, f"window {name} has no {key}")
            fields[key] = item
        before = _string_list(path, value.get("before", []), f"window {name} before")
        for step in before:
            if step not in steps:
                _bad(path, f"window {name} before names no step {step}")
        stop = value.get("stop")
        if stop is not None and not isinstance(stop, str):
            _bad(path, f"window {name} stop must be a string")
        production = value.get("production", False)
        if not isinstance(production, bool):
            _bad(path, f"window {name} production must be true or false")
        windows[name] = {
            "before": before,
            "start": fields["start"],
            "length_s": _duration(path, fields["length"], f"window {name} length"),
            "stop": stop,
            "read": fields["read"],
            "production": production,
        }
    return windows


def load(project_name):
    """Return the parsed runner config, or None when the file does not exist."""
    path = config_path(project_name)
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        _bad(path, exc)

    pattern = data.get("stop_pattern")
    stop_pattern = None
    if pattern is not None:
        try:
            stop_pattern = re.compile(pattern)
        except (re.error, TypeError):
            _bad(path, f"bad stop_pattern regex: {pattern!r}")

    steps = _steps(path, data.get("steps", {}))
    windows = _windows(path, data.get("windows", {}), steps)
    return {
        "safety_stop": data.get("safety_stop") == "verified",
        "stop_pattern": stop_pattern,
        "steps": steps,
        "windows": windows,
    }


def gate_window(plan_path_abs):
    """The last ``Gate window:`` line of a plan file, or None."""
    try:
        text = Path(plan_path_abs).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = None
    for line in text.splitlines():
        match = GATE_WINDOW_RE.match(line)
        if match is not None:
            found = match.group(1)
    return found
