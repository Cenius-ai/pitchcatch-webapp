#!/usr/bin/env python3
"""Pitchcatch -- a bullpen pitch log you can read, diff and script.

One file, standard library only.  Pitches are recorded through an interactive
prompt loop, saved to a local JSON store, and read back three ways: a pitch
mix notation grid, a strike-zone heat tally by count, and a bullpen session
volume tracker.

Run ``python3 pitchcatch.py --help`` for the command reference.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

APP_NAME = "Pitchcatch"
__version__ = "0.1.0"

SCHEMA_VERSION = 1
DEFAULT_STORE_DIR = ".pitchcatch"
DEFAULT_STORE_NAME = "pitchcatch.json"

PITCH_TYPES = ("FF", "SI", "FC", "SL", "CU", "CB", "CH", "KN", "OTHER")
ZONES = tuple(range(1, 10))
OUT = "OUT"
RESULTS = ("ball", "called", "swing", "foul", "inplay")
RESULT_ALIASES = {
    "b": "ball",
    "ball": "ball",
    "c": "called",
    "called": "called",
    "cs": "called",
    "s": "swing",
    "sw": "swing",
    "swing": "swing",
    "f": "foul",
    "foul": "foul",
    "i": "inplay",
    "inplay": "inplay",
    "in play": "inplay",
}

# --- committed design direction -------------------------------------------
# calm-precise / terminal dark / monospace; ONE accent for primary state.
ACCENT_HEX = "#008f91"                        # oklch(0.58 0.12 196)
ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"
ANSI_ACCENT = "\033[38;2;0;143;145m"          # ACCENT_HEX as true-colour foreground
HEAT_RAMP_ASCII = " .:-=+*#%@"                # non-TTY density ramp (pure ASCII)
RULE_CHAR = "-"
RULE_WIDTH = 66
HEAT_CELL_WIDTH = 15
HEAT_BAR_WIDTH = HEAT_CELL_WIDTH - 2

PIN_MIN_DIGITS = 4
PIN_MAX_DIGITS = 8
PBKDF2_ITERATIONS = 120_000
MAX_PIN_ATTEMPTS = 3

DATE_FORMAT = "%Y-%m-%d"


class PitchcatchError(Exception):
    """A user-facing failure: printed to stderr, never a traceback."""


class InputExhausted(PitchcatchError):
    """Standard input ended (a pipe closed or < /dev/null): stop cleanly."""


# ---------------------------------------------------------------------------
# store: paths, atomic writes, tolerant reads
# ---------------------------------------------------------------------------


def default_store_path() -> str:
    """Return the default store path (~/.pitchcatch/pitchcatch.json)."""
    return os.path.join(os.path.expanduser("~"), DEFAULT_STORE_DIR, DEFAULT_STORE_NAME)


def empty_store() -> dict:
    return {"schema_version": SCHEMA_VERSION, "profiles": [], "sessions": []}


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return date.today().strftime(DATE_FORMAT)


def save_store(path: str, data: dict) -> None:
    """Write the store atomically: temp file in the same directory + os.replace."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".pitchcatch-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Never leave a half-written temp file behind, and never swallow the error.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_store(path: str) -> tuple[dict, str | None]:
    """Read the store, recovering from a corrupt file.

    Returns ``(store, warning)``.  A corrupt store is renamed to ``<path>.bad``
    and an empty store is returned so the tool still runs.
    """
    if not os.path.exists(path):
        store = empty_store()
        save_store(path, store)
        return store, None
    detail = ""
    data = None
    try:
        # Read the bytes, then parse with the safe JSON parser (no object hooks).
        with open(path, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
        data = json.loads(raw_text)
    except (OSError, ValueError) as exc:
        detail = str(exc)
    if not isinstance(data, dict):
        bad_path = path + ".bad"
        try:
            os.replace(path, bad_path)
            return empty_store(), f"{path} was unreadable and has been moved to {bad_path}"
        except OSError:
            return empty_store(), f"{path} was unreadable ({detail}); continuing with an empty store"
    store = empty_store()
    store["schema_version"] = int(data.get("schema_version", SCHEMA_VERSION))
    store["profiles"] = [item for item in data.get("profiles", []) if isinstance(item, dict)]
    store["sessions"] = [item for item in data.get("sessions", []) if isinstance(item, dict)]
    return store, None


class Store:
    """Thin, explicit wrapper over the JSON document."""

    def __init__(self, path: str, data: dict) -> None:
        self.path = path
        self.data = data

    @classmethod
    def open(cls, path: str) -> tuple["Store", str | None]:
        data, warning = load_store(path)
        return cls(path, data), warning

    def save(self) -> None:
        save_store(self.path, self.data)

    @property
    def profiles(self) -> list[dict]:
        return self.data["profiles"]

    @property
    def sessions(self) -> list[dict]:
        return self.data["sessions"]

    def find_profile(self, ref: str) -> dict | None:
        needle = (ref or "").strip()
        if not needle:
            return None
        lowered = needle.lower()
        for profile in self.profiles:
            if profile.get("id") == needle or str(profile.get("name", "")).lower() == lowered:
                return profile
        matches = [p for p in self.profiles if str(p.get("id", "")).startswith(lowered)]
        return matches[0] if len(matches) == 1 else None

    def create_profile(self, name: str, pin: str | None = None,
                       arsenal: list[str] | None = None) -> dict:
        clean_name = (name or "").strip()
        if not clean_name:
            raise PitchcatchError("A pitcher name is required.")
        if self.find_profile(clean_name) is not None:
            raise PitchcatchError(f"A profile named {clean_name!r} already exists.")
        pin_salt: str | None = None
        pin_hash: str | None = None
        if pin:
            pin_salt, pin_hash = hash_pin(pin)
        profile = {
            "id": new_id(),
            "name": clean_name,
            "pin_salt": pin_salt,
            "pin_hash": pin_hash,
            "created_at": now_iso(),
            "arsenal": list(arsenal) if arsenal else list(PITCH_TYPES),
        }
        self.profiles.append(profile)
        self.save()
        return profile

    def set_arsenal(self, profile: dict, types: list[str]) -> None:
        profile["arsenal"] = list(types)
        self.save()

    def sessions_for(self, profile_id: str) -> list[dict]:
        rows = [s for s in self.sessions if s.get("profile_id") == profile_id]
        return sorted(rows, key=lambda s: (s.get("date", ""), s.get("started_at", "")), reverse=True)

    def find_session(self, ref: str, profile_id: str | None = None) -> dict | None:
        needle = (ref or "").strip()
        if not needle:
            return None
        pool = [s for s in self.sessions if profile_id is None or s.get("profile_id") == profile_id]
        for session in pool:
            if session.get("id") == needle:
                return session
        matches = [s for s in pool if str(s.get("id", "")).startswith(needle)]
        return matches[0] if len(matches) == 1 else None

    def open_session(self, profile_id: str, kind: str, target_pitches: int | None = None,
                     on_date: str | None = None) -> dict:
        session = {
            "id": new_id(),
            "profile_id": profile_id,
            "date": on_date or today_str(),
            "kind": kind,
            "target_pitches": target_pitches,
            "started_at": now_iso(),
            "ended_at": None,
            "pitches": [],
        }
        self.sessions.append(session)
        self.save()
        return session

    def close_session(self, session: dict) -> dict:
        session["ended_at"] = now_iso()
        self.save()
        return session

    def add_pitch(self, session: dict, pitch: dict) -> None:
        session.setdefault("pitches", []).append(pitch)
        self.save()

    def pop_pitch(self, session: dict) -> dict | None:
        pitches = session.setdefault("pitches", [])
        if not pitches:
            return None
        removed = pitches.pop()
        self.save()
        return removed


# ---------------------------------------------------------------------------
# pure helpers: counts, zones, notation
# ---------------------------------------------------------------------------


def parse_count(text: str) -> tuple[int, int]:
    """Parse '1-2' into (balls, strikes).  Raises ValueError carrying the hint."""
    parts = (text or "").strip().split("-")
    if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
        balls, strikes = int(parts[0].strip()), int(parts[1].strip())
        if 0 <= balls <= 3 and 0 <= strikes <= 2:
            return balls, strikes
    raise ValueError("Enter count as balls-strikes, e.g. 1-2")


def format_count(balls: int, strikes: int) -> str:
    return f"{int(balls)}-{int(strikes)}"


def parse_zone(text: str) -> int | str:
    """Parse a zone answer: 1-9, or out/o/off for off the plate."""
    raw = (text or "").strip().lower()
    if raw in ("out", "o", "off", "outside"):
        return OUT
    if raw.isdigit() and 1 <= int(raw) <= 9:
        return int(raw)
    raise ValueError("Enter a zone 1-9 (Z1 is top-left, Z9 bottom-right) or 'out' for off the plate")


def format_zone(zone: int | str) -> str:
    if isinstance(zone, str) and zone.strip().upper() == OUT:
        return OUT
    try:
        number = int(zone)
    except (TypeError, ValueError):
        return OUT
    return f"Z{number}" if 1 <= number <= 9 else OUT


def pitch_notation(pitch: dict) -> str:
    return f"{pitch.get('pitch_type', 'OTHER')} {format_zone(pitch.get('zone'))}"


def parse_pitch_type(text: str, allowed: tuple[str, ...] = PITCH_TYPES) -> str:
    raw = (text or "").strip().upper()
    if raw in allowed:
        return raw
    raise ValueError(f"Enter a pitch type: {', '.join(allowed)}")


def parse_pitch_target(text: str) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("Enter a whole number of pitches, or press Enter for no target")
    return int(raw)


def parse_velocity(text: str) -> float | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        velocity = float(raw)
    except ValueError:
        raise ValueError("Enter a velocity in mph (30-110), or press Enter to skip") from None
    if not 30 <= velocity <= 110:
        raise ValueError("Enter a velocity in mph (30-110), or press Enter to skip")
    return round(velocity, 1)


def parse_pin(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not PIN_MIN_DIGITS <= len(raw) <= PIN_MAX_DIGITS:
        raise ValueError(f"PIN must be {PIN_MIN_DIGITS}-{PIN_MAX_DIGITS} digits, or press Enter to skip")
    return raw


def parse_arsenal(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError(f"Enter a comma-separated list from: {', '.join(PITCH_TYPES)}")
    chosen: list[str] = []
    for part in raw.replace(";", ",").split(","):
        code = part.strip().upper()
        if not code:
            continue
        if code not in PITCH_TYPES:
            raise ValueError(f"{code!r} is not a pitch type; choose from {', '.join(PITCH_TYPES)}")
        if code not in chosen:
            chosen.append(code)
    if not chosen:
        raise ValueError(f"Enter at least one pitch type from: {', '.join(PITCH_TYPES)}")
    return [code for code in PITCH_TYPES if code in chosen]


def parse_kind(text: str) -> str:
    raw = (text or "").strip().lower()
    if raw in ("bullpen", "b", "pen"):
        return "bullpen"
    if raw in ("game", "g"):
        return "game"
    raise ValueError("Enter 'bullpen' or 'game'")


def parse_result(text: str) -> str:
    raw = (text or "").strip().lower()
    if raw in RESULT_ALIASES:
        return RESULT_ALIASES[raw]
    raise ValueError(f"Enter a result: {', '.join(RESULTS)}")


# ---------------------------------------------------------------------------
# profiles and the PIN gate
# ---------------------------------------------------------------------------


def hash_pin(pin: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_pin(pin: str, salt_hex: str, hash_hex: str) -> bool:
    if not salt_hex or not hash_hex:
        return False
    _, candidate = hash_pin(pin, salt_hex)
    return hmac.compare_digest(candidate, hash_hex)


def is_pin_protected(profile: dict) -> bool:
    return bool(profile.get("pin_hash") and profile.get("pin_salt"))


def arsenal_of(profile: dict) -> list[str]:
    arsenal = [code for code in profile.get("arsenal") or [] if code in PITCH_TYPES]
    return arsenal or list(PITCH_TYPES)


# ---------------------------------------------------------------------------
# derived views (raw pitches in, aggregates out -- nothing is pre-computed)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MixRow:
    pitch_type: str
    zones: list[int]
    out: int
    total: int
    share: float

    @property
    def cells(self) -> list[int]:
        return list(self.zones) + [self.out]


def filter_pitches(pitches: list[dict], count: tuple[int, int] | None = None,
                   pitch_type: str | None = None) -> list[dict]:
    selected = []
    for pitch in pitches:
        if count is not None and (pitch.get("balls"), pitch.get("strikes")) != count:
            continue
        if pitch_type is not None and pitch.get("pitch_type") != pitch_type:
            continue
        selected.append(pitch)
    return selected


def build_mix_rows(pitches: list[dict]) -> tuple[list[MixRow], int]:
    grand_total = len(pitches)
    rows: list[MixRow] = []
    for pitch_type in PITCH_TYPES:
        zone_counts = [0] * 9
        out_count = 0
        for pitch in pitches:
            if pitch.get("pitch_type") != pitch_type:
                continue
            zone = pitch.get("zone")
            if isinstance(zone, int) and 1 <= zone <= 9:
                zone_counts[zone - 1] += 1
            else:
                out_count += 1
        total = sum(zone_counts) + out_count
        share = (total / grand_total) if grand_total else 0.0
        rows.append(MixRow(pitch_type, zone_counts, out_count, total, share))
    return rows, grand_total


def build_heat_cells(pitches: list[dict]) -> dict[int, int]:
    cells = {zone: 0 for zone in ZONES}
    for pitch in pitches:
        zone = pitch.get("zone")
        if isinstance(zone, int) and 1 <= zone <= 9:
            cells[zone] += 1
    return cells


def sessions_within(sessions: list[dict], days: int, today: date | None = None) -> list[dict]:
    reference = today or date.today()
    cutoff = reference - timedelta(days=days - 1)
    within = []
    for session in sessions:
        try:
            session_date = datetime.strptime(str(session.get("date", "")), DATE_FORMAT).date()
        except (TypeError, ValueError):
            continue
        if cutoff <= session_date <= reference:
            within.append(session)
    return within


def pitch_type_breakdown(pitches: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for pitch in pitches:
        code = pitch.get("pitch_type", "OTHER")
        counts[code] = counts.get(code, 0) + 1
    return [(code, counts[code]) for code in PITCH_TYPES if code in counts]


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


class Console:
    """All terminal I/O goes through here, so tests can drive it with strings."""

    def __init__(self, stdin=None, stdout=None, stderr=None, color: bool | None = None) -> None:
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        if color is None:
            is_tty = bool(getattr(self.stdout, "isatty", lambda: False)())
            color = is_tty and not os.environ.get("NO_COLOR")
        self.color = bool(color)

    def is_interactive(self) -> bool:
        return bool(getattr(self.stdin, "isatty", lambda: False)())

    def write(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def error(self, text: str) -> None:
        self.stderr.write(text + "\n")

    def hint(self, text: str) -> None:
        self.write(self._dim(text) if self.color else text)

    def rule(self, text: str = "") -> None:
        if text:
            self.write(self._accent(text))
        self.write(RULE_CHAR * RULE_WIDTH)

    def prompt(self, label: str, default: str | None = None) -> str:
        """Read one line.  Raises EOFError when the input stream runs out."""
        suffix = f" [{default}]" if default not in (None, "") else ""
        self.stdout.write(f"{label}{suffix}: ")
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise EOFError(label)
        answer = line.rstrip("\n").rstrip("\r")
        if answer.strip() == "" and default is not None:
            return default
        return answer

    def _accent(self, text: str) -> str:
        return f"{ANSI_ACCENT}{text}{ANSI_RESET}" if self.color else text

    def _dim(self, text: str) -> str:
        return f"{ANSI_DIM}{text}{ANSI_RESET}" if self.color else text


def visible_len(text: str) -> int:
    """Length of a string ignoring ANSI escape sequences."""
    length = 0
    index = 0
    while index < len(text):
        if text[index] == "\033":
            end = text.find("m", index)
            index = len(text) if end == -1 else end + 1
            continue
        length += 1
        index += 1
    return length


def pad(text: str, width: int, align: str = "left") -> str:
    fill = max(0, width - visible_len(text))
    if align == "right":
        return " " * fill + text
    if align == "center":
        left = fill // 2
        return " " * left + text + " " * (fill - left)
    return text + " " * fill


def share_text(count: int, total: int) -> str:
    if not count or total <= 0:
        return "0%"
    return f"{count * 100.0 / total:.0f}%"


def render_mix_grid(rows: list[MixRow], grand_total: int, *, color: bool = False) -> list[str]:
    """Notation grid: a row per pitch type, Z1-Z9 plus OUT, totals and usage."""
    type_width = 7
    col_width = 4
    head = pad("TYPE", type_width) + " " + " ".join(
        pad(f"Z{zone}", col_width, "right") for zone in ZONES
    )
    head += " " + pad("OUT", col_width, "right") + " " + pad("TOT", 5, "right")
    head += " " + pad("USE", 5, "right")
    lines = [head]
    for row in rows:
        line = pad(row.pitch_type, type_width) + " " + " ".join(
            pad(str(value) if value else ".", col_width, "right") for value in row.cells
        )
        line += " " + pad(str(row.total), 5, "right")
        line += " " + pad(share_text(row.total, grand_total) if row.total else "-", 5, "right")
        lines.append(f"{ANSI_DIM}{line}{ANSI_RESET}" if color and not row.total else line)
    totals = [sum(row.cells[column] for row in rows) for column in range(10)]
    total_line = pad("TOTAL", type_width) + " " + " ".join(
        pad(str(value) if value else ".", col_width, "right") for value in totals
    )
    total_line += " " + pad(str(grand_total), 5, "right")
    total_line += " " + pad("100%" if grand_total else "-", 5, "right")
    lines.append(RULE_CHAR * visible_len(total_line))
    lines.append(total_line)
    return lines


def heat_bar(count: int, hottest: int, color: bool) -> str:
    """One cell's shading: ANSI background on a TTY, ASCII density ramp otherwise."""
    if count <= 0 or hottest <= 0:
        return " " * HEAT_BAR_WIDTH
    ratio = count / hottest
    filled = max(1, min(HEAT_BAR_WIDTH, round(ratio * HEAT_BAR_WIDTH)))
    level = max(1, min(len(HEAT_RAMP_ASCII) - 1, round(ratio * (len(HEAT_RAMP_ASCII) - 1))))
    if color:
        intensity = level / (len(HEAT_RAMP_ASCII) - 1)
        shade = f"\033[48;2;0;{round(143 * intensity)};{round(145 * intensity)}m"
        return shade + " " * filled + ANSI_RESET + " " * (HEAT_BAR_WIDTH - filled)
    return (HEAT_RAMP_ASCII[level] * filled).ljust(HEAT_BAR_WIDTH)


def render_heat_grid(cells: dict[int, int], total: int, *, color: bool = False) -> list[str]:
    """3x3 tally.  Both the shading and the raw count are always printed."""
    hottest = max(cells.values()) if cells else 0
    border = " " * 3 + "+" + "+".join("-" * HEAT_CELL_WIDTH for _ in range(3)) + "+"
    head = " " * 4 + " ".join(f"Z{index + 1}".center(HEAT_CELL_WIDTH)
                              for index in range(3)) + " "
    lines = [head, border]
    for row_index in range(3):
        bars = []
        counts = []
        for column_index in range(3):
            zone = row_index * 3 + column_index + 1
            count = cells.get(zone, 0)
            bars.append(" " + heat_bar(count, hottest, color) + " ")
            label = f"{count} pitch{'es' if count != 1 else ''}  {share_text(count, total)}"
            counts.append(pad(label, HEAT_CELL_WIDTH, "center"))
        lines.append("   |" + "|".join(bars) + "|")
        lines.append("   |" + "|".join(counts) + "|")
        lines.append(border)
    return lines


def hottest_and_coldest(cells: dict[int, int]) -> tuple[int, int]:
    hottest = max(ZONES, key=lambda zone: (cells.get(zone, 0), -zone))
    coldest = min(ZONES, key=lambda zone: (cells.get(zone, 0), zone))
    return hottest, coldest


# ---------------------------------------------------------------------------
# console views
# ---------------------------------------------------------------------------


def store_summary(store: Store, profile: dict) -> str:
    sessions = store.sessions_for(profile["id"])
    total = sum(len(session.get("pitches", [])) for session in sessions)
    plural = "s" if len(sessions) != 1 else ""
    return f"{len(sessions)} session{plural}, {total} pitches logged"


def print_store_warning(console: Console, warning: str | None) -> None:
    if warning:
        console.error(f"warning: {warning}")


def profile_pitches(store: Store, profile: dict) -> tuple[list[dict], list[dict]]:
    sessions = store.sessions_for(profile["id"])
    pitches = [pitch for session in sessions for pitch in session.get("pitches", [])]
    return sessions, pitches


def view_mix(console: Console, store: Store, profile: dict, session: dict | None, *,
             count: tuple[int, int] | None = None, pitch_type: str | None = None,
             aggregate: bool = False, as_json: bool = False) -> None:
    if aggregate:
        sessions, pitches = profile_pitches(store, profile)
        scope = f"all sessions ({len(sessions)} sessions, {len(pitches)} pitches)"
    else:
        pitches = list(session.get("pitches", [])) if session else []
        scope = (f"session {str(session.get('id'))[:8]} ({session.get('date')}, "
                 f"{session.get('kind')})") if session else "no session"
    selected = filter_pitches(pitches, count=count, pitch_type=pitch_type)
    rows, grand_total = build_mix_rows(selected)
    filters = []
    if count is not None:
        filters.append(f"count {format_count(*count)}")
    if pitch_type:
        filters.append(f"pitch {pitch_type}")
    filter_text = "  |  ".join(filters)
    if as_json:
        console.write(json.dumps({
            "profile": profile["name"],
            "scope": scope,
            "count": format_count(*count) if count else None,
            "pitch_type": pitch_type,
            "total": grand_total,
            "rows": [
                {"pitch_type": row.pitch_type, "zones": row.zones, "out": row.out,
                 "total": row.total, "share": round(row.share, 4)}
                for row in rows if row.total
            ],
        }, indent=2))
        return
    title = f"Pitch mix - {profile['name']}"
    if filter_text:
        title += f"  |  {filter_text}"
    console.rule(title)
    console.write(f"{scope}  |  {grand_total} pitches in view")
    for line in render_mix_grid(rows, grand_total, color=console.color):
        console.write(line)
    if not pitches:
        console.write("No pitches yet - run pitchcatch log")
    elif not selected:
        console.write(f"No pitches {filter_text} - widen the filter or run pitchcatch log")


def view_heat(console: Console, store: Store, profile: dict, session: dict | None, *,
              count: tuple[int, int] | None = None, pitch_type: str | None = None,
              aggregate: bool = False, as_json: bool = False) -> None:
    if aggregate:
        sessions, pitches = profile_pitches(store, profile)
        scope = f"all sessions ({len(sessions)} sessions, {len(pitches)} pitches)"
    else:
        pitches = list(session.get("pitches", [])) if session else []
        scope = (f"session {str(session.get('id'))[:8]} ({session.get('date')}, "
                 f"{session.get('kind')})") if session else "no session"
    selected = filter_pitches(pitches, count=count, pitch_type=pitch_type)
    cells = build_heat_cells(selected)
    on_plate = sum(cells.values())
    off_plate = len(selected) - on_plate
    if as_json:
        console.write(json.dumps({
            "profile": profile["name"],
            "scope": scope,
            "count": format_count(*count) if count else None,
            "pitch_type": pitch_type,
            "on_plate": on_plate,
            "off_plate": off_plate,
            "cells": {f"Z{zone}": cells[zone] for zone in ZONES},
        }, indent=2))
        return
    filters = []
    if count is not None:
        filters.append(f"count {format_count(*count)}")
    if pitch_type:
        filters.append(f"pitch {pitch_type}")
    title = f"Strike-zone heat - {profile['name']}"
    if filters:
        title += "  |  " + "  |  ".join(filters)
    console.rule(title)
    console.write(f"{scope}  |  {on_plate} on the plate, {off_plate} off the plate")
    for line in render_heat_grid(cells, on_plate, color=console.color):
        console.write(line)
    if on_plate == 0:
        label = f" at {format_count(*count)}" if count else ""
        console.write(f"No pitches{label} yet - log some with pitchcatch log")
        return
    hottest, coldest = hottest_and_coldest(cells)
    hot_count, cold_count = cells[hottest], cells[coldest]
    summary = (f"Hottest zone Z{hottest} ({hot_count} pitch{'es' if hot_count != 1 else ''}, "
               f"{share_text(hot_count, on_plate)})")
    if cold_count == 0:
        summary += f"; coldest Z{coldest} with no pitches"
    else:
        summary += (f"; coldest Z{coldest} ({cold_count} pitch{'es' if cold_count != 1 else ''}, "
                    f"{share_text(cold_count, on_plate)})")
    console.write(summary)


def volume_rows(store: Store, profile: dict, days: int | None = None) -> list[dict]:
    sessions = store.sessions_for(profile["id"])
    if days is not None:
        sessions = sessions_within(sessions, days)
    rows = []
    for session in sessions:
        pitches = session.get("pitches", [])
        target = session.get("target_pitches")
        rows.append({
            "id": str(session.get("id", "")),
            "date": session.get("date", ""),
            "kind": session.get("kind", "bullpen"),
            "pitches": len(pitches),
            "target": target,
            "over_target": bool(target) and len(pitches) > target,
            "breakdown": pitch_type_breakdown(pitches),
            "open": not session.get("ended_at"),
        })
    return rows


def render_volume_table(rows: list[dict]) -> list[str]:
    lines = [pad("ID", 8) + " " + pad("DATE", 10) + " " + pad("KIND", 8) + " "
             + pad("PITCHES", 7, "right") + " " + pad("TARGET", 6, "right") + "  STATUS"]
    for row in rows:
        status = []
        if row["over_target"]:
            status.append(f"over target ({row['target']})")
        if row["open"]:
            status.append("open")
        lines.append(pad(row["id"][:8], 8) + " " + pad(row["date"], 10) + " " + pad(row["kind"], 8)
                     + " " + pad(str(row["pitches"]), 7, "right") + " "
                     + pad(str(row["target"]) if row["target"] else "-", 6, "right")
                     + "  " + ", ".join(status))
        mix = "  ".join(f"{code} {count}" for code, count in row["breakdown"])
        if mix:
            for piece in textwrap.wrap(f"mix: {mix}", width=88) or [f"mix: {mix}"]:
                lines.append(" " * 8 + " " + piece)
    return lines


def view_volume(console: Console, store: Store, profile: dict, *, days: int | None = None,
                as_json: bool = False) -> None:
    rows = volume_rows(store, profile, days)
    all_sessions = store.sessions_for(profile["id"])
    window_sessions = sessions_within(all_sessions, days) if days is not None else all_sessions
    window_pitches = sum(len(s.get("pitches", [])) for s in window_sessions)
    week_sessions = sessions_within(all_sessions, 7)
    week_pitches = sum(len(s.get("pitches", [])) for s in week_sessions)
    if as_json:
        console.write(json.dumps({
            "profile": profile["name"],
            "days": days,
            "sessions": rows,
            "week": {"sessions": len(week_sessions), "pitches": week_pitches},
            "window": {"sessions": len(window_sessions), "pitches": window_pitches},
        }, indent=2))
        return
    scope = f"last {days} days" if days else "all sessions"
    console.rule(f"Bullpen volume - {profile['name']}  |  {scope}")
    if not rows:
        console.write("No sessions recorded yet")
        console.write("Record one with: pitchcatch log")
        return
    for line in render_volume_table(rows):
        console.write(line)
    console.write(RULE_CHAR * RULE_WIDTH)
    week_avg = week_pitches / len(week_sessions) if week_sessions else 0.0
    console.write(f"7-day total: {week_pitches} pitches over {len(week_sessions)} "
                  f"session{'' if len(week_sessions) == 1 else 's'} ({week_avg:.1f} avg)")
    window_avg = window_pitches / len(window_sessions) if window_sessions else 0.0
    console.write(f"Window total: {window_pitches} pitches over {len(window_sessions)} "
                  f"session{'' if len(window_sessions) == 1 else 's'} ({window_avg:.1f} avg)")


def view_sessions(console: Console, store: Store, profile: dict, *, as_json: bool = False) -> None:
    sessions = store.sessions_for(profile["id"])
    if as_json:
        console.write(json.dumps([
            {"id": s.get("id"), "date": s.get("date"), "kind": s.get("kind"),
             "pitches": len(s.get("pitches", [])), "target": s.get("target_pitches"),
             "open": not s.get("ended_at")}
            for s in sessions
        ], indent=2))
        return
    console.rule(f"Sessions - {profile['name']}")
    if not sessions:
        console.write("No sessions recorded yet - run pitchcatch log")
        return
    console.write(pad("ID", 8) + " " + pad("DATE", 10) + " " + pad("KIND", 8) + " "
                  + pad("PITCHES", 7, "right") + " " + pad("TARGET", 6, "right") + "  STATUS")
    for session in sessions:
        console.write(pad(str(session.get("id", ""))[:8], 8) + " "
                      + pad(str(session.get("date", "")), 10) + " "
                      + pad(str(session.get("kind", "")), 8) + " "
                      + pad(str(len(session.get("pitches", []))), 7, "right") + " "
                      + pad(str(session.get("target_pitches") or "-"), 6, "right") + "  "
                      + ("open" if not session.get("ended_at") else "closed"))


def view_profiles(console: Console, store: Store, profile: dict | None, *,
                  as_json: bool = False) -> None:
    profiles = store.profiles
    if as_json:
        entries = []
        for item in profiles:
            sessions = store.sessions_for(item["id"])
            entries.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "pin_protected": is_pin_protected(item),
                "sessions": len(sessions),
                "last_session": sessions[0].get("date") if sessions else None,
                "arsenal": arsenal_of(item),
            })
        console.write(json.dumps({"active": profile["name"] if profile else None,
                                  "profiles": entries}, indent=2))
        return
    console.rule("Pitcher profiles")
    if not profiles:
        console.write('No profiles yet - create one with: pitchcatch profiles --new "Ace Ramirez"')
        return
    for index, item in enumerate(profiles, start=1):
        sessions = store.sessions_for(item["id"])
        last = sessions[0].get("date") if sessions else "no sessions"
        marker = "  PIN protected" if is_pin_protected(item) else ""
        active = "  *active*" if profile and item.get("id") == profile.get("id") else ""
        console.write(f"  {index}. {pad(str(item.get('name')), 16)} {pad(str(last), 12)} "
                      f"{len(sessions)} session{'' if len(sessions) == 1 else 's'}{marker}{active}")
    console.write("")
    console.write(f'  Select with: pitchcatch profiles --select "{profiles[0].get("name")}"')


# ---------------------------------------------------------------------------
# interactive pitch entry
# ---------------------------------------------------------------------------


def ask(console: Console, label: str, parser, default: str | None = None):
    """Prompt until the parser accepts the answer; the message names the range."""
    while True:
        raw = console.prompt(label, default)
        try:
            return parser(raw)
        except ValueError as exc:
            console.error(str(exc))


def choose_pitch_type(console: Console, profile: dict) -> str | None:
    """Return a pitch type, the sentinel 'UNDO', or None to finish the session."""
    arsenal = arsenal_of(profile)
    listing = "  ".join(f"{index + 1}={code}" for index, code in enumerate(arsenal))
    console.write(f"Pitch type  {listing}  0=full list  u=undo  q=finish")
    use_full_list = False
    while True:
        try:
            answer = console.prompt("Pitch type", None).strip()
        except EOFError:
            return None
        if answer == "" or answer.lower() == "q":
            return None
        if answer.lower() == "u":
            return "UNDO"
        if answer == "0":
            use_full_list = True
            full = "  ".join(f"{index + 1}={code}" for index, code in enumerate(PITCH_TYPES))
            console.write(f"All pitch types  {full}")
            continue
        pool = PITCH_TYPES if use_full_list else arsenal
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(pool):
                return pool[index]
            console.error(f"Enter a number from 1 to {len(pool)}, a pitch code, u or q")
            continue
        try:
            return parse_pitch_type(answer)
        except ValueError as exc:
            console.error(str(exc))


def describe_pitch(pitch: dict) -> str:
    text = (f"{pitch_notation(pitch)} "
            f"{format_count(pitch.get('balls', 0), pitch.get('strikes', 0))} {pitch.get('result')}")
    if pitch.get("velocity_mph"):
        text += f" {float(pitch['velocity_mph']):.1f} mph"
    return text


def run_log(console: Console, store: Store, profile: dict, *, kind: str | None = None,
            target: int | None = None, on_date: str | None = None) -> int:
    """Open a session and record pitches until the user finishes."""
    try:
        if kind is None:
            kind = ask(console, "Session kind (bullpen/game)", parse_kind, "bullpen")
        if target is None:
            target = ask(console, "Pitch target (Enter for none)", parse_pitch_target, "")
    except EOFError:
        console.error("No input received - nothing was recorded.")
        return 1
    session = store.open_session(profile["id"], kind, target, on_date=on_date)
    console.rule(f"Log session - {profile['name']}")
    console.write(f"Session {str(session['id'])[:8]}  |  {session['date']}  |  {kind}"
                  + (f"  |  target {target} pitches" if target else ""))
    console.write("First prompt is pitch type. Answer q (or press Enter) to finish and save.")
    last_pitch: dict | None = None
    interrupted = False
    try:
        while True:
            pitches = session["pitches"]
            header = f"[{len(pitches)} pitch{'es' if len(pitches) != 1 else ''}]"
            if last_pitch is not None:
                header += (f"  count {format_count(last_pitch['balls'], last_pitch['strikes'])}"
                           f"  last: {describe_pitch(last_pitch)}")
            console.hint(header)
            answer = choose_pitch_type(console, profile)
            if answer is None:
                break
            if answer == "UNDO":
                removed = store.pop_pitch(session)
                if removed is None:
                    console.error("Nothing to undo - no pitches logged in this session yet")
                else:
                    console.write(f"Undid {describe_pitch(removed)}; session now has "
                                  f"{len(session['pitches'])} pitches")
                last_pitch = session["pitches"][-1] if session["pitches"] else None
                continue
            zone = ask(console, "Zone (1-9, or 'out')", parse_zone,
                       format_zone(last_pitch["zone"]) if last_pitch else "5")
            count = ask(console, "Count", parse_count,
                        format_count(last_pitch["balls"], last_pitch["strikes"]) if last_pitch else "0-0")
            result = ask(console, f"Result ({'/'.join(RESULTS)})", parse_result,
                         last_pitch["result"] if last_pitch else "ball")
            velocity = ask(console, "Velocity mph (Enter to skip)", parse_velocity, "")
            pitch = {
                "id": new_id(),
                "pitch_type": answer,
                "zone": zone,
                "balls": count[0],
                "strikes": count[1],
                "result": result,
                "velocity_mph": velocity,
                "logged_at": now_iso(),
            }
            store.add_pitch(session, pitch)
            last_pitch = pitch
            console.write(f"Logged {describe_pitch(pitch)} ({len(session['pitches'])} in session)")
    except KeyboardInterrupt:
        interrupted = True
    store.close_session(session)
    total = len(session["pitches"])
    if interrupted:
        console.error("Session saved.")
        return 130
    console.write(f"Saved {total} pitch{'' if total == 1 else 'es'} "
                  f"to session {str(session['id'])[:8]}")
    return 0


# ---------------------------------------------------------------------------
# profile flows
# ---------------------------------------------------------------------------


def unlock_profile(console: Console, store: Store, profile: dict) -> bool:
    """Ask for the PIN; three failures return to the menu without unlocking."""
    if not is_pin_protected(profile):
        return True
    for attempt in range(1, MAX_PIN_ATTEMPTS + 1):
        try:
            pin = console.prompt(f"PIN for {profile['name']} "
                                 f"(attempt {attempt}/{MAX_PIN_ATTEMPTS})", None)
        except EOFError:
            console.error("No PIN supplied - returning to the menu.")
            return False
        if verify_pin(pin.strip(), profile["pin_salt"], profile["pin_hash"]):
            return True
        console.error("That PIN does not match this profile.")
    console.error(f"Could not unlock {profile['name']} after {MAX_PIN_ATTEMPTS} attempts "
                  "- returning to the menu.")
    return False


def create_profile_flow(console: Console, store: Store, *, name: str | None = None,
                        pin: str | None = None) -> dict:
    if name is None:
        try:
            name = console.prompt("Pitcher name", None).strip()
        except EOFError:
            raise InputExhausted("No input received - run `pitchcatch --help` "
                                 "for the command list.") from None
    if pin is None:
        raw = console.prompt("PIN (4-8 digits, Enter for none)", "")
        pin = parse_pin(raw)
    profile = store.create_profile(name, pin)
    console.write(f"Created profile {profile['name']}"
                  + (" (PIN protected)" if pin else " (no PIN)"))
    return profile


def pick_profile(console: Console, store: Store) -> dict | None:
    view_profiles(console, store, None)
    while True:
        try:
            answer = console.prompt("Profile number or name (Enter to cancel)", None).strip()
        except EOFError:
            return None
        if answer == "":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(store.profiles):
            return store.profiles[int(answer) - 1]
        chosen = store.find_profile(answer)
        if chosen is None:
            console.error("No profile matches that - enter a listed number or name")
            continue
        return chosen


def resolve_profile(console: Console, store: Store, ref: str | None) -> dict | None:
    """Pick the active profile for this run, then clear its PIN gate."""
    if not store.profiles:
        return None
    profile = store.find_profile(ref) if ref else None
    if ref and profile is None:
        known = ", ".join(str(item.get("name")) for item in store.profiles)
        raise PitchcatchError(f"No profile matches {ref!r}. Known profiles: {known}")
    if profile is None and len(store.profiles) == 1:
        profile = store.profiles[0]
    if profile is None:
        if not console.is_interactive():
            known = ", ".join(str(item.get("name")) for item in store.profiles)
            raise PitchcatchError(f"Several profiles exist - pass --profile NAME. Known profiles: {known}")
        profile = pick_profile(console, store)
        if profile is None:
            return None
    if not unlock_profile(console, store, profile):
        return None
    return profile


# ---------------------------------------------------------------------------
# main menu
# ---------------------------------------------------------------------------

MENU_ITEMS = (
    ("l", "Log session", "open a session and record pitches"),
    ("m", "Mix grid", "pitch mix notation grid"),
    ("h", "Heat tally", "strike-zone heat by count"),
    ("v", "Volume", "bullpen session volume"),
    ("p", "Profiles", "select, create or unlock a pitcher"),
    ("q", "Quit", "exit Pitchcatch"),
)


def print_menu_items(console: Console) -> None:
    for key, label, blurb in MENU_ITEMS:
        console.write(f"  [{key}] {pad(label, 12)} {blurb}")


def show_menu(console: Console, store: Store, profile: dict | None) -> dict | None:
    """Paint the menu and return the (possibly newly created) active profile."""
    console.rule(f"{APP_NAME} {__version__} - bullpen pitch log")
    if profile is None:
        console.write("No pitcher profile yet.")
        console.write("Pitchcatch records a bullpen session pitch by pitch, then shows the pitch mix")
        console.write("grid, the strike-zone heat at each count and how much you have thrown lately.")
        print_menu_items(console)
        console.write("")
        console.write("Create your first pitcher profile to begin - a name is enough, the PIN is optional.")
        return create_profile_flow(console, store)
    sessions = store.sessions_for(profile["id"])
    latest = sessions[0] if sessions else None
    console.write(f"Active profile: {profile['name']}"
                  + ("  (PIN protected)" if is_pin_protected(profile) else ""))
    if latest:
        console.write(f"Last session: {latest.get('date')} {latest.get('kind')}, "
                      f"{len(latest.get('pitches', []))} pitches")
    else:
        console.write("No sessions logged yet")
    console.write(store_summary(store, profile))
    console.write("")
    print_menu_items(console)
    console.write("")
    return profile


def run_menu(console: Console, store: Store, profile: dict | None) -> int:
    while True:
        try:
            profile = show_menu(console, store, profile)
        except InputExhausted as exc:
            console.write(str(exc))
            return 0
        except PitchcatchError as exc:
            console.error(f"error: {exc}")
            return 2
        if profile is None:
            return 1
        try:
            answer = console.prompt("Choice", None).strip().lower()
        except EOFError:
            console.write("No input received - run `pitchcatch --help` for the command list.")
            return 0
        if answer in ("q", "quit", "exit"):
            return 0
        if answer in ("l", "log"):
            code = run_log(console, store, profile)
            if code != 0:
                return code
            continue
        if answer in ("m", "mix"):
            sessions = store.sessions_for(profile["id"])
            view_mix(console, store, profile, sessions[0] if sessions else None)
            continue
        if answer in ("h", "heat"):
            sessions = store.sessions_for(profile["id"])
            view_heat(console, store, profile, sessions[0] if sessions else None)
            continue
        if answer in ("v", "volume"):
            view_volume(console, store, profile)
            continue
        if answer in ("p", "profiles"):
            chosen = pick_profile(console, store)
            if chosen is not None and unlock_profile(console, store, chosen):
                profile = chosen
            continue
        console.error("Enter l, m, h, v, p or q")


# ---------------------------------------------------------------------------
# demo data (seed)
# ---------------------------------------------------------------------------

DEMO_PROFILES = (
    {"name": "Ace Ramirez", "pin": None, "arsenal": list(PITCH_TYPES)},
    {"name": "Jordan Blake", "pin": "4821", "arsenal": ["FF", "SL", "CH"]},
    {"name": "Rosa Delgado", "pin": None, "arsenal": ["SI", "SL", "CH", "CU"]},
)

VELOCITY_BY_TYPE = {
    "FF": (92.0, 96.5),
    "SI": (90.0, 94.0),
    "FC": (86.0, 90.0),
    "SL": (81.0, 85.5),
    "CU": (76.0, 80.0),
    "CB": (73.0, 78.0),
    "CH": (82.0, 86.5),
    "KN": (72.0, 77.0),
    "OTHER": (80.0, 88.0),
}
ZONE_WEIGHTS = (2, 3, 2, 3, 5, 3, 2, 3, 2, 2)  # Z1..Z9 then OUT
COUNT_CHOICES = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2),
                 (2, 0), (2, 1), (2, 2), (3, 0), (3, 1), (3, 2))
COUNT_WEIGHTS = (4, 4, 6, 3, 4, 5, 2, 3, 3, 1, 1, 2)  # pitcher-friendly counts come up most
STRIKE_RESULTS = ("called", "swing", "foul", "inplay", "foul")
BALL_RESULTS = ("ball", "ball", "ball", "foul", "swing")

DEMO_PLANS = {
    "Ace Ramirez": (
        (38, "bullpen", 25, 24),
        (31, "bullpen", 25, 22),
        (27, "game", 30, 18),
        (22, "bullpen", 30, 28),
        (16, "bullpen", 25, 31),   # over target on purpose
        (11, "game", 35, 21),
        (6, "bullpen", 25, 20),
        (3, "bullpen", 25, 26),
        (1, "bullpen", 25, 23),
    ),
    "Jordan Blake": (
        (20, "bullpen", 20, 17),
        (9, "bullpen", 20, 19),
        (2, "bullpen", 20, 12),
    ),
    "Rosa Delgado": (
        (24, "game", 40, 26),
        (13, "bullpen", 30, 25),
        (4, "bullpen", 30, 28),
    ),
}


def seed_demo_data(store: Store, console: Console | None = None) -> list[dict]:
    """Create the demo pitchers with realistic sessions.  Safe to run twice."""
    rng = random.Random(20240512)
    profiles = []
    for spec in DEMO_PROFILES:
        profile = store.find_profile(spec["name"])
        if profile is None:
            profile = store.create_profile(spec["name"], spec["pin"], spec["arsenal"])
        profiles.append(profile)
        if store.sessions_for(profile["id"]):
            continue
        for day_offset, kind, target, pitch_count in DEMO_PLANS.get(spec["name"], ()):
            session_date = date.today() - timedelta(days=day_offset)
            stamp = datetime.combine(session_date, datetime.min.time()).astimezone()
            session = store.open_session(profile["id"], kind, target,
                                         on_date=session_date.strftime(DATE_FORMAT))
            session["started_at"] = stamp.isoformat(timespec="seconds")
            for _ in range(pitch_count):
                store.add_pitch(session, demo_pitch(rng, spec["arsenal"], stamp))
            store.close_session(session)
            session["ended_at"] = stamp.isoformat(timespec="seconds")
            store.save()
    if console is not None:
        total = sum(len(store.sessions_for(profile["id"])) for profile in profiles)
        console.write(f"Demo data ready: {len(profiles)} pitchers, {total} sessions "
                      f"({', '.join(str(p['name']) for p in profiles)})")
    return profiles


def demo_pitch(rng: random.Random, arsenal: list[str], stamp: datetime) -> dict:
    pitch_type = rng.choice(arsenal)
    zone = rng.choices(list(ZONES) + [OUT], weights=ZONE_WEIGHTS, k=1)[0]
    balls, strikes = rng.choices(COUNT_CHOICES, weights=COUNT_WEIGHTS, k=1)[0]
    result = rng.choice(STRIKE_RESULTS if zone != OUT else BALL_RESULTS)
    low, high = VELOCITY_BY_TYPE[pitch_type]
    return {
        "id": new_id(),
        "pitch_type": pitch_type,
        "zone": zone,
        "balls": balls,
        "strikes": strikes,
        "result": result,
        "velocity_mph": round(rng.uniform(low, high), 1),
        "logged_at": stamp.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def add_global_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Global flags, accepted both before and after the subcommand."""
    value_default = argparse.SUPPRESS if suppress_defaults else None
    flag_default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("--file", dest="store_path", default=value_default,
                        help="store path (default: ~/.pitchcatch/pitchcatch.json)")
    parser.add_argument("--profile", dest="profile", default=value_default,
                        help="pitcher profile by name or id (default: the only profile, else ask)")
    parser.add_argument("--no-color", action="store_true", default=flag_default,
                        help="disable ANSI shading even on a TTY")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pitchcatch",
        description="Bullpen pitch log: pitch mix notation grid, strike-zone heat tally by count "
                    "and a session volume tracker. Standard library only, fully offline.",
        epilog="Examples:\n"
               "  pitchcatch log                     record a bullpen session\n"
               "  pitchcatch sessions                list session ids and pitch counts\n"
               "  pitchcatch mix --count 0-2         mix grid for one count\n"
               "  pitchcatch heat --count 0-2        heat tally at 0-2\n"
               '  pitchcatch volume --days 30        last 30 days of volume\n'
               '  pitchcatch profiles --new "Ace"    create a pitcher profile\n'
               "  pitchcatch seed                    load demo pitchers and sessions\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", metavar="command")

    log = sub.add_parser("log", help="record a session pitch by pitch (interactive)")
    log.add_argument("--kind", choices=("bullpen", "game"), default=None,
                     help="session kind; prompts when omitted")
    log.add_argument("--target", type=int, default=None, help="pitch target for the session")
    log.add_argument("--date", dest="session_date", default=None,
                     help="session date as YYYY-MM-DD (default: today)")

    sessions = sub.add_parser("sessions", help="list the active profile's sessions")
    sessions.add_argument("--json", action="store_true", help="machine-readable output")

    mix = sub.add_parser("mix", help="pitch mix notation grid (a row per pitch type, Z1-Z9 plus OUT)")
    mix.add_argument("--session", dest="session_id", default=None,
                     help="session id (default: the most recent)")
    mix.add_argument("--count", dest="count", default=None,
                     help="only pitches thrown at this count, e.g. 1-2")
    mix.add_argument("--pitch", dest="pitch_type", default=None, help="only this pitch type, e.g. SL")
    mix.add_argument("--all", dest="aggregate", action="store_true",
                     help="aggregate every session of the active profile")
    mix.add_argument("--json", action="store_true", help="machine-readable output")

    heat = sub.add_parser("heat", help="strike-zone heat tally, filterable by count")
    heat.add_argument("--session", dest="session_id", default=None,
                      help="session id (default: the most recent)")
    heat.add_argument("--count", dest="count", default=None,
                      help="only pitches thrown at this count, e.g. 0-2")
    heat.add_argument("--pitch", dest="pitch_type", default=None, help="only this pitch type, e.g. SL")
    heat.add_argument("--all", dest="aggregate", action="store_true",
                      help="aggregate every session of the active profile")
    heat.add_argument("--json", action="store_true", help="machine-readable output")

    volume = sub.add_parser("volume", help="session volume with a rolling 7-day total")
    volume.add_argument("--days", type=int, default=None, help="only sessions inside this many days")
    volume.add_argument("--json", action="store_true", help="machine-readable output")

    profiles = sub.add_parser("profiles", help="list, create, select or re-arsenal pitcher profiles")
    profiles.add_argument("--new", dest="new_name", default=None, metavar="NAME",
                          help="create a profile with this pitcher name")
    profiles.add_argument("--pin", dest="new_pin", default=None,
                          help="PIN for the new profile (4-8 digits; stored only as a PBKDF2 hash)")
    profiles.add_argument("--select", dest="select", default=None, metavar="NAME",
                          help="unlock this profile for the current run")
    profiles.add_argument("--arsenal", dest="arsenal", default=None, metavar="LIST",
                          help='set the profile arsenal, e.g. "FF,SL,CH"')
    profiles.add_argument("--json", action="store_true", help="machine-readable output")

    seed = sub.add_parser("seed", help="load demo pitchers and sessions into the store")
    seed.add_argument("--quiet", action="store_true", help="no summary line")

    sub.add_parser("menu", help="open the interactive menu (same as running with no command)")
    for subparser in sub.choices.values():
        add_global_flags(subparser, suppress_defaults=True)
    return parser


def resolve_pitch_type(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parse_pitch_type(value)
    except ValueError as exc:
        raise PitchcatchError(str(exc)) from None


def resolve_count(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        return parse_count(value)
    except ValueError as exc:
        raise PitchcatchError(str(exc)) from None


def resolve_session(store: Store, profile: dict, session_ref: str | None) -> dict:
    sessions = store.sessions_for(profile["id"])
    if not sessions:
        raise PitchcatchError("No sessions recorded yet - run pitchcatch log")
    if session_ref is None:
        return sessions[0]
    session = store.find_session(session_ref, profile["id"])
    if session is None:
        known = ", ".join(str(s.get("id", ""))[:8] for s in sessions)
        raise PitchcatchError(f"No session matches {session_ref!r}. Known session ids: {known}")
    return session


def command_seed(console: Console, store: Store, args) -> int:
    seed_demo_data(store, None if args.quiet else console)
    return 0


def command_profiles(console: Console, store: Store, args) -> int:
    if args.new_name:
        pin = parse_pin(args.new_pin) if args.new_pin is not None else None
        if args.new_pin is not None and pin is None:
            raise PitchcatchError("--pin must be 4-8 digits")
        profile = store.create_profile(args.new_name, pin)
        console.write(f"Created profile {profile['name']}"
                      + (" (PIN protected)" if pin else " (no PIN)"))
        return 0
    active: dict | None = None
    if args.select:
        active = store.find_profile(args.select)
        if active is None:
            known = ", ".join(str(p.get("name")) for p in store.profiles)
            raise PitchcatchError(f"No profile matches {args.select!r}. Known profiles: {known}")
        if not unlock_profile(console, store, active):
            return 1
    elif args.profile:
        active = resolve_profile(console, store, args.profile)
    elif len(store.profiles) == 1 and not is_pin_protected(store.profiles[0]):
        active = store.profiles[0]
    if args.arsenal:
        if active is None:
            raise PitchcatchError("Pass --select NAME together with --arsenal.")
        try:
            types = parse_arsenal(args.arsenal)
        except ValueError as exc:
            raise PitchcatchError(str(exc)) from None
        store.set_arsenal(active, types)
        console.write(f"{active['name']} arsenal set to: {', '.join(types)}")
        return 0
    view_profiles(console, store, active, as_json=args.json)
    return 0


def command_sessions(console: Console, store: Store, profile: dict, args) -> int:
    view_sessions(console, store, profile, as_json=args.json)
    return 0


def dispatch(console: Console, store: Store, args) -> int:
    command = args.command
    if command == "seed":
        return command_seed(console, store, args)
    if command == "profiles":
        return command_profiles(console, store, args)
    if command in (None, "menu"):
        profile = resolve_profile(console, store, args.profile)
        if profile is None and store.profiles:
            return 1
        return run_menu(console, store, profile)
    profile = resolve_profile(console, store, args.profile)
    if profile is None:
        if store.profiles:
            raise PitchcatchError("Profile locked - run: pitchcatch profiles --select NAME")
        raise PitchcatchError('No profiles yet - create one with: pitchcatch profiles --new "Ace Ramirez"')
    if command == "sessions":
        return command_sessions(console, store, profile, args)
    if command == "log":
        return run_log(console, store, profile, kind=args.kind, target=args.target,
                       on_date=args.session_date)
    if command == "mix":
        count = resolve_count(args.count)
        pitch_type = resolve_pitch_type(args.pitch_type)
        session = None if args.aggregate else resolve_session(store, profile, args.session_id)
        view_mix(console, store, profile, session, count=count, pitch_type=pitch_type,
                 aggregate=args.aggregate, as_json=args.json)
        return 0
    if command == "heat":
        count = resolve_count(args.count)
        pitch_type = resolve_pitch_type(args.pitch_type)
        session = None if args.aggregate else resolve_session(store, profile, args.session_id)
        view_heat(console, store, profile, session, count=count, pitch_type=pitch_type,
                  aggregate=args.aggregate, as_json=args.json)
        return 0
    if command == "volume":
        if args.days is not None and args.days <= 0:
            raise PitchcatchError("--days must be a positive number of days")
        view_volume(console, store, profile, days=args.days, as_json=args.json)
        return 0
    raise PitchcatchError(f"Unknown command {command!r}")


def main(argv: list[str] | None = None, console: Console | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if console is None:
        console = Console()
    if getattr(args, "no_color", False):
        console.color = False
    store_path = args.store_path or default_store_path()
    try:
        store, warning = Store.open(store_path)
        print_store_warning(console, warning)
        return dispatch(console, store, args)
    except PitchcatchError as exc:
        console.error(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        console.error("Interrupted.")
        return 130
    except OSError as exc:
        console.error(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
