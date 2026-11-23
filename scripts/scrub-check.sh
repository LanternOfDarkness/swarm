#!/usr/bin/env bash
set -u

if [ "$#" -gt 0 ]; then
    patterns_file=$1
elif [ -n "${SWARM_SCRUB_PATTERNS:-}" ]; then
    patterns_file=$SWARM_SCRUB_PATTERNS
else
    patterns_file=${HOME:-}/.config/swarm/scrub-patterns
fi

if [ ! -f "$patterns_file" ]; then
    echo "scrub: no patterns file at $patterns_file" >&2
    exit 2
fi

patterns=()
while IFS= read -r line || [ -n "$line" ]; do
    case $line in
        '' | '#'*) continue ;;
    esac
    patterns+=("$line")
done < "$patterns_file"

if [ "${#patterns[@]}" -eq 0 ]; then
    echo "scrub: no patterns file at $patterns_file" >&2
    exit 2
fi

pattern_args=()
for pattern in "${patterns[@]}"; do
    pattern_args+=(-e "$pattern")
done

hits=$(git grep -n -iE "${pattern_args[@]}")
status=$?
if [ "$status" -eq 0 ]; then
    printf '%s\n' "$hits"
    exit 1
fi
if [ "$status" -eq 1 ]; then
    echo "scrub: clean"
    exit 0
fi
exit "$status"
