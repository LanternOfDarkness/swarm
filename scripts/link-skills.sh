#!/usr/bin/env bash
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: link-skills.sh REPO_ROOT HOME_DIR" >&2
    exit 2
fi

REPO_ROOT="$1"
HOME_DIR="$2"

link_one() {
    src="$1"
    target="$2"
    if [ -L "$target" ]; then
        current="$(readlink -f "$target" 2>/dev/null || true)"
        wanted="$(readlink -f "$src" 2>/dev/null || true)"
        if [ -n "$current" ] && [ "$current" = "$wanted" ]; then
            return 0
        fi
    fi
    if [ -e "$target" ] || [ -L "$target" ]; then
        echo "kept existing $target (not linked; remove it to use the bundled copy)"
        return 0
    fi
    ln -sfn "$src" "$target"
    echo "linked $target"
}

shopt -s nullglob

claude_dir="$HOME_DIR/.claude/skills"
mkdir -p "$claude_dir"
for src in "$REPO_ROOT"/skills/*/; do
    name="$(basename "${src%/}")"
    [ "$name" = "omp" ] && continue
    link_one "${src%/}" "$claude_dir/$name"
done

if command -v omp >/dev/null 2>&1; then
    omp_dir="$HOME_DIR/.omp/agent/skills"
    mkdir -p "$omp_dir"
    for src in "$REPO_ROOT"/skills/omp/*/; do
        name="$(basename "${src%/}")"
        link_one "${src%/}" "$omp_dir/$name"
    done
else
    echo "omp not found: skipped omp skills"
fi
