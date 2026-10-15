#!/usr/bin/env bash
# Pitchcatch setup: verifies the tool builds, then stops.
# Pitchcatch is standard-library only, so there are no dependencies to fetch.
# Safe to re-run. Never starts the app and never touches your pitch store.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found. Install Python 3.9+ first (see INSTALL.md)." >&2
    exit 1
fi

echo "==> Checking the Python version"
"$PYTHON" - <<'PYCHECK'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"error: Python 3.9+ is required, found {sys.version.split()[0]}")
print(f"    ok: Python {sys.version.split()[0]}")
PYCHECK

echo "==> Compiling pitchcatch.py"
"$PYTHON" -m py_compile pitchcatch.py main.py

echo "==> Import self-check (fails loudly if an import is missing)"
"$PYTHON" -c "import pitchcatch; print('    ok: pitchcatch ' + pitchcatch.__version__)"

echo "==> Installing the optional 'pitchcatch' console script"
if "$PYTHON" -m pip install -e . --no-deps --no-build-isolation >/dev/null 2>&1; then
    echo "    ok: run it as: pitchcatch --help"
else
    echo "    note: could not install the console script (offline or no pip);"
    echo "          run it as: $PYTHON pitchcatch.py --help"
fi

echo
echo "Setup complete. Nothing was started and your pitch store was not touched."
echo "Run:  $PYTHON pitchcatch.py --help"
echo "Demo: $PYTHON pitchcatch.py seed   then   $PYTHON pitchcatch.py volume"
