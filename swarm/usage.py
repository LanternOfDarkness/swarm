"""Claude Code transcript usage, aggregated per day, role and model."""

import json
import os
import re
import subprocess
from pathlib import Path


def slug(path) -> str:
    """How Claude Code names its per-project folders."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def project_dirs(root, repo_path) -> list:
    """Project folders under ``root`` belonging to ``repo_path`` (or all)."""
    root = Path(root)
    if not root.is_dir():
        return []
    folders = sorted(path for path in root.iterdir() if path.is_dir())
    if repo_path is None:
        return folders
    base = slug(repo_path)
    return [
        path for path in folders
        if path.name == base or path.name.startswith(base + "-")
    ]


def _is_subagent(path) -> bool:
    return "/subagents/" in path.as_posix()


def _session_name(path) -> str:
    parts = path.parts
    if "subagents" in parts:
        index = len(parts) - 1 - parts[::-1].index("subagents")
        if index > 0:
            return parts[index - 1]
    return path.stem


def _description(path) -> str:
    meta = path.with_name(path.stem + ".meta.json")
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("description")
    return value if isinstance(value, str) else ""


def _record(line, path):
    message = line.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    model = message.get("model")
    if model == "<synthetic>":
        return None
    day = str(line.get("timestamp", ""))[:10]
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    record = {
        "day": day,
        "model": model,
        "kind": "subagent" if _is_subagent(path) else (
            line.get("entrypoint") or "unknown"
        ),
        "session": _session_name(path),
        "input": input_tokens,
        "output": output_tokens,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "weighted": (
            input_tokens
            + 5 * output_tokens
            + 1.25 * cache_write
            + 0.1 * cache_read
        ),
    }
    if _is_subagent(path):
        record["agent"] = path.stem
        record["description"] = _description(path)
    return record


def collect(dirs, since, until) -> list:
    """Usage records from every ``*.jsonl`` transcript under ``dirs``."""
    records = []
    seen = set()
    for directory in dirs:
        try:
            files = sorted(Path(directory).rglob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            if not path.is_file():
                continue
            try:
                handle = path.open()
            except OSError:
                continue
            with handle:
                try:
                    lines = handle.readlines()
                except OSError:
                    continue
            for raw in lines:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(line, dict):
                    continue
                record = _record(line, path)
                if record is None:
                    continue
                if not since <= record["day"] < until:
                    continue
                key = (
                    (line.get("message") or {}).get("id"),
                    line.get("requestId"),
                )
                if key != (None, None) and key in seen:
                    continue
                if key != (None, None):
                    seen.add(key)
                records.append(record)
    return records


def _empty_total():
    return {"calls": 0, "output": 0, "cache_read": 0, "weighted": 0}


def _add(total, record):
    total["calls"] += 1
    total["output"] += record["output"]
    total["cache_read"] += record["cache_read"]
    total["weighted"] += record["weighted"]


def summarise(records) -> dict:
    """JSON-able aggregates: per day/kind/model rows, per day, total, models."""
    grouped = {}
    days = {}
    total = _empty_total()
    models = {}
    for record in records:
        row_key = (record["day"], record["kind"], record["model"])
        row = grouped.get(row_key)
        if row is None:
            row = grouped[row_key] = {
                "day": record["day"],
                "kind": record["kind"],
                "model": record["model"],
                **_empty_total(),
            }
        _add(row, record)
        day = days.get(record["day"])
        if day is None:
            day = days[record["day"]] = {"day": record["day"], **_empty_total()}
        _add(day, record)
        _add(total, record)
        models[record["model"]] = models.get(record["model"], 0) + record["weighted"]
    grand_weighted = total["weighted"]
    model_rows = [
        {
            "model": model,
            "weighted": weighted,
            "share": (100.0 * weighted / grand_weighted) if grand_weighted else 0.0,
        }
        for model, weighted in sorted(models.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "rows": sorted(
            grouped.values(), key=lambda row: (row["day"], row["kind"], row["model"])
        ),
        "days": sorted(days.values(), key=lambda day: day["day"]),
        "total": total,
        "models": model_rows,
    }


def commits_per_day(repo_path, branch, since, until):
    """Commit count per UTC day on ``branch``; ``None`` when git fails."""
    command = [
        "git", "-C", str(repo_path), "log", branch,
        f"--since={since}T00:00Z", f"--until={until}T00:00Z",
        "--format=%cd", "--date=format-local:%Y-%m-%d",
    ]
    env = dict(os.environ)
    env["TZ"] = "UTC"
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    counts = {}
    for line in result.stdout.splitlines():
        day = line.strip()
        if day:
            counts[day] = counts.get(day, 0) + 1
    return counts


def _millions(value) -> str:
    return f"{value / 1e6:.1f}"


def format_report(summary, commits, subagents=None) -> str:
    """Plain-text report for ``summary`` and per-day ``commits``."""
    unavailable = commits is None
    commits = commits or {}
    lines = []
    header = (
        f"{'day':<10}  {'kind':<10}  {'model':<12}  {'calls':>6}  "
        f"{'output':>10}  {'cache_read(M)':>13}  {'weighted(M)':>11}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in summary["rows"]:
        lines.append(
            f"{row['day']:<10}  {row['kind']:<10}  {row['model']:<12}  "
            f"{row['calls']:>6}  {row['output']:>10}  "
            f"{_millions(row['cache_read']):>13}  "
            f"{_millions(row['weighted']):>11}"
        )
    lines.append("")
    lines.append("per day")
    if unavailable:
        lines.append(
            "commits: unavailable (git log failed for the integration branch)"
        )
    for day in summary["days"]:
        if unavailable:
            count = "-"
            per_commit = "-"
        else:
            count = commits.get(day["day"], 0)
            per_commit = (
                f"{day['weighted'] / count / 1e3:.1f}k" if count else "-"
            )
        lines.append(
            f"{day['day']}  weighted {_millions(day['weighted'])}M  "
            f"commits {count}  per commit {per_commit}"
        )
    total = summary["total"]
    lines.append("")
    lines.append(
        f"total  calls {total['calls']}  output {total['output']}  "
        f"cache_read {_millions(total['cache_read'])}M  "
        f"weighted {_millions(total['weighted'])}M"
    )
    for model in summary["models"]:
        lines.append(f"model {model['model']}  {model['share']:.1f}%")
    if subagents:
        lines.append("")
        header = (
            f"{'day':<10}  {'model':<12}  {'calls':>6}  "
            f"{'weighted(k)':>11}  description"
        )
        lines.append("subagents")
        lines.append(header)
        lines.append("-" * len(header))
        for item in sorted(
            subagents,
            key=lambda item: (
                item.get("weighted") is None,
                -(item.get("weighted") or 0),
            ),
        ):
            day = item.get("day") or "-"
            model = item.get("model") or "-"
            calls = item.get("calls")
            calls = "-" if calls is None else str(calls)
            weighted = item.get("weighted")
            weighted = (
                "-" if weighted is None else f"{weighted / 1e3:.1f}"
            )
            lines.append(
                f"{day:<10}  {model:<12}  {calls:>6}  "
                f"{weighted:>11}  {item.get('description') or ''}"
            )
    return "\n".join(lines)
