"""Run a worker command detached from the daemon, recording its log and exit code."""

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="swarm.runwrap")
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.argv and args.argv[0] == "--":
        args.argv = args.argv[1:]
    if not args.argv:
        parser.error("missing command after --")
    return args


def _write_exit(path, code):
    if code < 0:
        code = 128 - code
    tmp = f"{path}.tmp"
    Path(tmp).write_text(f"{code}\n")
    os.replace(tmp, path)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    exit_file = Path(args.exit_file)
    child = None

    def forward(signum, frame):
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    previous = signal.signal(signal.SIGTERM, forward)
    try:
        log = Path(args.log)
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open("ab") as fh:
                child = subprocess.Popen(
                    args.argv,
                    cwd=args.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                )
                code = child.wait()
        except OSError:
            _write_exit(exit_file, 127)
            return 127
        _write_exit(exit_file, code)
        return code if code >= 0 else 128 - code
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
