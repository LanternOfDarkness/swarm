import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from swarm import util

_DURATION_RE = re.compile(r"^(\d+)([smh])$")
_MNT_RE = re.compile(r"^/mnt/([a-z])(/|$)")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}
IMPLEMENT_TIMEOUT_GRACE_S = 600

DEFAULT_MODELS = {
    "architect": "opus",
    "reviewer": "opus",
    "rereview": "sonnet",
    "verifier": "opus",
    "judge": "sonnet",
    "implementer": "sonnet",
}


def parse_duration(s: str) -> int:
    match = _DURATION_RE.match(s.strip())
    if not match:
        raise ValueError(f"bad duration: {s!r}")
    value, unit = match.groups()
    return int(value) * _DURATION_UNITS[unit]


@dataclass
class Config:
    disk_path: str = "~"
    disk_floor_gb: int = 50
    disk_label: str = ""
    monthly_budget_usd: float = 80.0
    budget_warn_ratio: float = 0.8
    dashboard_refresh_s: int = 15
    pm_context_warn_tokens: int = 150000
    pm_context_warn_step: int = 20000
    pm_context_alert_tokens: int = 200000
    claude_context_window: int = 200000
    omp_context_window: int = 1000000
    claude_bin: str = "claude"
    claude_model: str = "opus"
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    claude_projects_dir: str = "~/.claude/projects"
    omp_runner: str = "~/.claude/skills/delegate/scripts/omp-run.py"
    daemon_interval_s: int = 20
    max_active_plans: int = 3
    max_claude_jobs: int = 2
    worktree_reserve_gb: int = 15
    max_goal_wait_h: int = 12
    timeout_architect_s: int = 2700
    timeout_review_s: int = 1200
    timeout_verify_s: int = 1800
    timeout_preverify_s: int = 2700
    omp_max_cost: float = 3.0
    omp_max_time: str = "60m"
    omp_fix_max_cost: float = 1.0
    omp_fix_max_time: str = "30m"
    worker_env: dict = field(default_factory=dict)
    arch_review_every: int = 8

    def disk_name(self) -> str:
        if self.disk_label:
            return self.disk_label
        match = _MNT_RE.match(self.disk_path)
        if match:
            return match.group(1).upper() + ":"
        if Path(self.disk_path).expanduser().resolve() == Path.home().resolve():
            return "home"
        return self.disk_path

    def model_for(self, role: str) -> str:
        return self.models.get(role, self.claude_model)

    def impl_timeout_s(self) -> int:
        return parse_duration(self.omp_max_time) + IMPLEMENT_TIMEOUT_GRACE_S

    def fix_timeout_s(self) -> int:
        return parse_duration(self.omp_fix_max_time) + IMPLEMENT_TIMEOUT_GRACE_S


def load() -> Config:
    path = util.home() / "config.toml"
    if not path.exists():
        cfg = Config()
    else:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        known = {f.name for f in fields(Config)}
        cfg = Config(**{k: v for k, v in data.items() if k in known})
        cfg.models = {**DEFAULT_MODELS, **data.get("models", {})}
    cfg.disk_path = str(Path(cfg.disk_path).expanduser())
    cfg.omp_runner = str(Path(cfg.omp_runner).expanduser())
    cfg.claude_projects_dir = str(Path(cfg.claude_projects_dir).expanduser())
    return cfg
