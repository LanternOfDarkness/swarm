"""Launch worker processes and inspect or terminate them."""

import os
import signal
import subprocess
import sys
from pathlib import Path

from swarm import util

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_paths(run_id):
    runs = util.home() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return runs / f"{run_id}.log", runs / f"{run_id}.exit", runs / f"{run_id}.md"


def read_exit(run_id):
    _, exit_file, _ = run_paths(run_id)
    try:
        return int(exit_file.read_text().strip())
    except (OSError, ValueError):
        return None


def worker_env(cfg, run_id):
    env = dict(os.environ)
    env["SWARM_WORKER"] = "1"
    env["SWARM_RUN_ID"] = str(run_id)
    env.update({str(k): str(v) for k, v in cfg.worker_env.items()})
    env.pop("SWARM_PM", None)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


class Launcher:
    def __init__(self):
        self._children = {}

    def launch(self, run_id, argv, cwd, env):
        log, exit_file, _ = run_paths(run_id)
        child_env = dict(env)
        root = str(REPO_ROOT)
        existing = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
        cmd = [
            sys.executable, "-m", "swarm.runwrap",
            "--log", str(log),
            "--exit-file", str(exit_file),
            "--cwd", str(cwd),
            "--", *argv,
        ]
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            env=child_env,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._children[proc.pid] = proc
        return proc.pid

    def _reap(self):
        while True:
            try:
                pid, code = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            proc = self._children.pop(pid, None)
            if proc is not None:
                proc.returncode = code

    def alive(self, pid):
        if pid is None or pid <= 0:
            return False
        self._reap()
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        fields = stat.rsplit(")", 1)[-1].split()
        return bool(fields) and fields[0] != "Z"

    def kill(self, pid):
        if pid is None or pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
