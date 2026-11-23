#!/usr/bin/env bash
set -euo pipefail

SWARM_DIR="${SWARM_HOME:-$HOME/.local/state/swarm}"

if tmux has-session -t swarm 2>/dev/null; then
    exec tmux attach -t swarm
fi

tmux new-session -d -s swarm -n daemon swarm daemon
tmux new-window -t swarm -n pm swarm-pm
if [ -f "$SWARM_DIR/telegram.toml" ]; then
    tmux new-window -t swarm -n telegram swarm telegram
fi
exec tmux attach -t swarm
