#!/usr/bin/env bash
# Pitchcatch end-to-end demo: seeds a throwaway store and walks every view.
# Non-interactive (the pitch answers are piped in) and safe to re-run: it works
# in a temporary directory and never touches ~/.pitchcatch/pitchcatch.json.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STORE="$WORK/pitchcatch.json"

run() {
    "$PYTHON" pitchcatch.py --file "$STORE" "$@"
}

step() {
    printf '\n===== %s =====\n' "$1"
}

step "1/9 command reference"
run --help

step "2/9 load demo pitchers and sessions"
run seed

step "3/9 bullpen volume tracker"
run --profile "Ace Ramirez" volume

step "4/9 volume for the last 30 days only"
run --profile "Ace Ramirez" volume --days 30

step "5/9 pitch mix notation grid for the most recent session"
run --profile "Ace Ramirez" mix

step "6/9 mix grid filtered to the 0-2 count"
run --profile "Ace Ramirez" mix --count 0-2

step "7/9 strike-zone heat at 0-2 (profile-wide), then the slider heat"
run --profile "Ace Ramirez" heat --all --count 0-2
run --profile "Ace Ramirez" heat --all --pitch SL

step "8/9 record a new session through the interactive prompt loop"
printf 'bullpen\n20\n2\n5\n3-3\n1-1\ns\n93\n4\n7\n0-2\nf\n84\nu\n7\n8\n0-2\nswing\n\nq\n' | run --profile "Ace Ramirez" log

step "9/9 the PIN gate: three wrong pins, then the right one"
printf '1111\n2222\n3333\n' | run profiles --select "Jordan Blake" || true
printf '4821\n' | run --profile "Jordan Blake" volume

step "done: the session that was just recorded is on file"
run --profile "Ace Ramirez" sessions
