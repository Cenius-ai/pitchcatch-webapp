# Installing Pitchcatch

Pitchcatch is a single-file, standard-library-only Python command-line tool. Installation
is a version check and a compile check — there are no third-party packages to fetch and
no services to configure.

## Prerequisites (system, installed by you — not by install.sh)

| Requirement | Why | How to check |
| --- | --- | --- |
| Python 3.9 or newer (CPython) | Pitchcatch runs on the system interpreter | `python3 --version` |
| A POSIX shell (`bash`/`sh`) | Runs `install.sh` and `demo.sh` | `bash --version` |

No compiler, no `libsqlite3-dev`, no database server, no Node, no network access.
On Debian/Ubuntu a Python 3.9+ interpreter comes from `apt-get install python3`; on
macOS `brew install python3`; on Windows install Python from python.org and use
`python` in place of `python3`.

## Step 1 — run the installer

```bash
bash install.sh
```

It does exactly four things, in order, and then **exits** (it never starts the app):

1. verifies Python is 3.9 or newer;
2. byte-compiles `pitchcatch.py` and `main.py`;
3. imports `pitchcatch` as a self-check, so a missing standard-library module fails here
   rather than at first use;
4. installs the optional `pitchcatch` console script (`pip install -e . --no-deps
   --no-build-isolation`). If pip or the network is unavailable, the note printed instead
   is harmless: `python3 pitchcatch.py` always works.

The script is safe to re-run. It never writes to your pitch store.

## Step 2 — (optional) load demo data

```bash
python3 pitchcatch.py seed
```

Creates the three demo pitchers in the default store (`~/.pitchcatch/pitchcatch.json`)
with realistic recent sessions. Idempotent — run it as often as you like. Use
`--file PATH` to seed somewhere else, e.g. `python3 pitchcatch.py --file /tmp/demo.json seed`.

This step is optional: with an empty store, `python3 pitchcatch.py` invites you to create
your first pitcher profile, and `python3 pitchcatch.py log` records straight into it.

## Step 3 — run it

```bash
python3 pitchcatch.py --help                     # every command, one line each
python3 pitchcatch.py                            # the menu
python3 pitchcatch.py --profile "Ace Ramirez" volume
```

Installed console script (if step 1 installed it):

```bash
pitchcatch volume --profile "Ace Ramirez"
```

Both forms are the same program; a hardcoded port is irrelevant because Pitchcatch never
opens a socket.

## Step 4 — (optional) see every view in one go

```bash
bash demo.sh
```

Seeds a throwaway store in a temporary directory, then walks the volume table, the mix
grid, the heat tally, the interactive entry loop (answers piped in) and the PIN gate. It
leaves your own store untouched and always exits 0.

## Step 5 — run the tests

```bash
python3 -m unittest discover -s tests
```

## Uninstalling

```bash
python3 -m pip uninstall pitchcatch       # remove the console script, if installed
rm -rf ~/.pitchcatch                      # remove the local pitch log (this deletes data)
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `python3: command not found` | Python is not on PATH; install Python 3.9+ or set `PYTHON=/path/to/python3 bash install.sh`. |
| `error: Python 3.9+ is required` | An older interpreter is first on PATH; use a newer `python3`. |
| `warning: ... was unreadable and has been moved to .../pitchcatch.json.bad` | The store file was corrupt (hand-edited or truncated). Pitchcatch recovered automatically; the backup is kept for inspection. |
| `error: Several profiles exist - pass --profile NAME` | More than one pitcher is in the store; name the one you want (or run the menu, which prompts). |
| Heat grid shows no colour | stdout is not a terminal, or `NO_COLOR` is set. That is the scriptable plain-ASCII mode by design; run it in a terminal to see shading. |
