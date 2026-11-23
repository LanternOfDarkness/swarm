#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

mkdir -p "$HOME/.local/bin"
ln -sfn "$ROOT/bin/swarm" "$HOME/.local/bin/swarm"
echo "linked $ROOT/bin/swarm -> $HOME/.local/bin/swarm"

mkdir -p "$HOME/.claude/agents"
shopt -s nullglob
for file in "$ROOT"/agents/*.md; do
    target="$HOME/.claude/agents/$(basename "$file")"
    ln -sfn "$file" "$target"
    echo "linked $file -> $target"
done
shopt -u nullglob

"$ROOT/scripts/link-skills.sh" "$ROOT" "$HOME"

ln -sfn "$ROOT/scripts/pm.sh" "$HOME/.local/bin/swarm-pm"
echo "linked $ROOT/scripts/pm.sh -> $HOME/.local/bin/swarm-pm"

python3 - "$ROOT" "$HOME" <<'PY'
import json
import sys
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from swarm import hooks

settings = home / ".claude" / "settings.json"
existing = {}
if settings.exists():
    with settings.open(encoding="utf-8") as fh:
        existing = json.load(fh)
    backup = settings.with_name("settings.json.bak-swarm")
    backup.write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"backed up {settings} -> {backup}")
settings.parent.mkdir(parents=True, exist_ok=True)
merged = hooks.merge_settings(existing, f"{home}/.local/bin/swarm")
settings.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
print(f"merged swarm hooks into {settings}")
PY

"$ROOT/bin/swarm" init
echo "initialised the swarm board"
