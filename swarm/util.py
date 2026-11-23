import os
from datetime import datetime, timezone
from pathlib import Path

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> str:
    fixed = os.environ.get("SWARM_NOW")
    if fixed:
        return fixed
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def home() -> Path:
    raw = os.environ.get("SWARM_HOME")
    path = Path(raw) if raw else Path.home() / ".local" / "state" / "swarm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)
