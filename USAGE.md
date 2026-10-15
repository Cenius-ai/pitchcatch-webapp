# Using Pitchcatch

Every example below is copy-pasteable. They assume you have run
`python3 pitchcatch.py seed` once (see INSTALL.md step 2); without it, start with
`python3 pitchcatch.py profiles --new "Your Pitcher"`.

## 1. Record a bullpen session

```bash
python3 pitchcatch.py log --profile "Ace Ramirez"
```

Pitchcatch asks for the session kind and an optional pitch target, then loops on one
pitch at a time:

```
Session kind (bullpen/game) [bullpen]:
Pitch target (Enter for none): 25
Session 28740eba  |  2026-10-15  |  bullpen  |  target 25 pitches
First prompt is pitch type. Answer q (or press Enter) to finish and save.
[0 pitches]
Pitch type  1=FF  2=SI  3=FC  4=SL  5=CU  6=CB  7=CH  8=KN  9=OTHER  0=full list  u=undo  q=finish
```

The header line above each prompt is the running state: how many pitches are in the
session, the count of the last pitch and the last pitch itself
(`[7 pitches]  count 0-2  last: SL Z7 0-2 swing 85.0 mph`).

| Answer | Meaning |
| --- | --- |
| `FF` (or `1` with the default arsenal) | Pitch type; the accepted codes are listed. |
| `7` / `out` | Zone 1–9 (Z1 top-left, Z9 bottom-right) or off the plate. |
| `1-2` | Count, balls then strikes. `5-0` and `12` are rejected with the accepted form. |
| `swing` (`s`), `called` (`c`), `ball` (`b`), `foul` (`f`), `inplay` (`i`) | Result. |
| *(Enter)* | Velocity is skipped and stored as empty. |
| `u` | Undo the previous pitch (rewrites the store). |
| `q` or *(Enter)* | Finish: the session is closed and saved. |

Every pitch is written to disk as soon as it is committed, so an interrupted session
keeps what was already logged. Ctrl-C mid-loop prints `Session saved.` and exits 130.

Answers can come from a file, so a session can be scripted end to end:

```bash
printf 'bullpen\n20\n2\n5\n1-1\ns\n93\nq\n' | python3 pitchcatch.py log --profile "Ace Ramirez"
```

## 2. Pitch mix notation grid

The default session is the most recent one.

```bash
python3 pitchcatch.py mix --profile "Ace Ramirez"
```

```
Pitch mix - Ace Ramirez
------------------------------------------------------------------
session 28740eba (2026-10-15, bullpen)  |  24 pitches in view
TYPE      Z1   Z2   Z3   Z4   Z5   Z6   Z7   Z8   Z9  OUT   TOT   USE
FF         .    .    .    1    2    .    .    1    .    1     5   21%
SI         1    .    .    1    2    .    2    1    .    .     7   29%
...
---------------------------------------------------------------------
TOTAL      2    3    4    5    2    .    .    4    2    1    24  100%
```

- One row per pitch type (`FF SI FC SL CU CB CH KN OTHER`), one column per zone in
  reading order (`Z1`–`Z9`), plus `OUT` for pitches off the plate.
- `TOT` is the row total and `USE` its share of the pitches in view; the `TOTAL` row is
  the column sums and the grand total.
- `--count 1-2` counts only pitches thrown in that count — useful to see what you throw
  when behind in the count.
- `--session ID` picks a specific session (any unique id prefix works);
  `pitchcatch sessions` lists the ids.
- `--pitch SL` restricts the grid to one pitch type.
- `--all` aggregates every session of the profile and says so in the header
  (`all sessions (9 sessions, 213 pitches)`).
- A session with no pitches prints the empty skeleton and
  `No pitches yet - run pitchcatch log`.

## 3. Strike-zone heat tally

```bash
python3 pitchcatch.py heat --profile "Ace Ramirez" --count 0-2
```

```
Strike-zone heat - Ace Ramirez  |  count 0-2
------------------------------------------------------------------
session 28740eba (2026-10-15, bullpen)  |  6 on the plate, 1 off the plate
           Z1              Z2              Z3
   +---------------+---------------+---------------+
   | @@@@@@@@@@@@@ | =====         |               |
   | 4 pitches  67%| 2 pitches  33%| 0 pitches  0% |
   +---------------+---------------+---------------+
   ...
Hottest zone Z1 (4 pitches, 67%); coldest Z3 with no pitches
```

- Without `--count`, every count is tallied.
- Shading density follows the tally, so the hottest zone is the darkest/blockiest cell;
  the raw count and share are printed in every cell, so colour is never the only signal.
- On a terminal the shading is ANSI background colour; piped, with `NO_COLOR`, or with
  `--no-color`, it is the plain ASCII ramp (` .:-=+*#%@`) with no escape codes.
- `--pitch SL` heats one pitch type; `--all` aggregates the whole profile.
- A count with no pitches still prints the zero grid, plus
  `No pitches at 2-2 yet - log some with pitchcatch log`.

## 4. Bullpen volume tracker

```bash
python3 pitchcatch.py volume --profile "Ace Ramirez"
python3 pitchcatch.py volume --profile "Ace Ramirez" --days 30
```

```
Bullpen volume - Ace Ramirez  |  all sessions
------------------------------------------------------------------
ID       DATE       KIND     PITCHES TARGET  STATUS
28740eba 2026-10-15 bullpen       24     25
         mix: FF 5  SI 7  SL 4  CH 4  CB 2  CU 2
8ff2e28e 2026-09-29 bullpen       31     25  over target (25)
         mix: FF 4  SI 7  FC 3  SL 1  CU 4  CB 4  CH 3  KN 2  OTHER 3
------------------------------------------------------------------
7-day total: 69 pitches over 3 sessions (23.0 avg)
Window total: 213 pitches over 9 sessions (23.7 avg)
```

- One row per session: id, date, kind, pitch count, target and a status
  (`over target` when the count exceeds the target, `open` for a session still running).
- The indented `mix:` line is the per-pitch-type breakdown of that session.
- `--days 30` counts only sessions inside the window (rows and both totals).
- Only the active profile's sessions are counted; `--profile NAME` switches pitcher.
- With nothing recorded: `No sessions recorded yet` / `Record one with: pitchcatch log`.

## 5. Pitchers, PINs and the arsenal

```bash
python3 pitchcatch.py profiles                                  # list
python3 pitchcatch.py profiles --new "Jordan Blake" --pin 4821  # create (PIN optional)
python3 pitchcatch.py profiles --select "Jordan Blake"          # unlock this run
python3 pitchcatch.py profiles --select "Jordan Blake" --arsenal "FF,SL,CH"
```

- The PIN is optional, 4–8 digits, and stored only as a salted PBKDF2-HMAC-SHA256 hash.
- A PIN-protected profile is unlocked per run; three wrong attempts print one
  non-enumerating message and return you to the menu without unlocking.
- The chosen profile is held in memory for that process only — nothing is written back
  as a "logged in" flag, so two pitchers' sessions never mix.
- A trimmed arsenal changes which numbered choices the entry prompt offers; typing `0` at
  that prompt opens the full list of pitch types for one pitch.

## 6. The menu

Running `pitchcatch` with no command shows the active profile, the last session and a
one-line volume summary, then the menu:

```
  [l] Log session  open a session and record pitches
  [m] Mix grid     pitch mix notation grid
  [h] Heat tally   strike-zone heat by count
  [v] Volume       bullpen session volume
  [p] Profiles     select, create or unlock a pitcher
  [q] Quit         exit Pitchcatch
```

With an empty store the same screen explains what the tool does and asks for your first
pitcher's name.

## 7. Scripting

`mix`, `heat`, `volume`, `sessions` and `profiles` accept `--json`, and piped output is
plain ASCII, so results can be diffed or consumed by other tools:

```bash
python3 pitchcatch.py heat --count 0-2 --json | python3 -c "import json,sys; print(json.load(sys.stdin)['cells'])"
python3 pitchcatch.py volume --json --profile "Ace Ramirez" > volume.json
```

Exit codes: `0` success, `1` interrupted/refused, `2` a usage or lookup error (the message
on stderr names the accepted form or the valid ids).
