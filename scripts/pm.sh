#!/usr/bin/env sh
ROOT=$(dirname "$(dirname "$(readlink -f "$0")")")
SWARM_PM=1 exec claude --agent swarm-pm --plugin-dir "$ROOT/mods/swarm-panel" "$@"
