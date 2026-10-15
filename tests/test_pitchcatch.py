"""Test suite for Pitchcatch.

Runs with the standard library alone::

    python3 -m unittest discover -s tests
    python3 -m pytest            # if pytest happens to be installed

Every test drives the tool through an injected ``Console`` and a temporary
store file, so the real ``~/.pitchcatch/pitchcatch.json`` is never touched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pitchcatch  # noqa: E402  (import after the path bootstrap)


def make_pitch(pitch_type="FF", zone=5, balls=0, strikes=0, result="ball", velocity=None):
    return {
        "id": pitchcatch.new_id(),
        "pitch_type": pitch_type,
        "zone": zone,
        "balls": balls,
        "strikes": strikes,
        "result": result,
        "velocity_mph": velocity,
        "logged_at": pitchcatch.now_iso(),
    }


class TempStoreCase(unittest.TestCase):
    """Base case that opens a temporary store and never the user's real one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = os.path.join(self._tmp.name, "pitchcatch.json")
        self.store, self.warning = pitchcatch.Store.open(self.store_path)

    def run_cli(self, args, stdin_text="", store_path=None):
        out, err = io.StringIO(), io.StringIO()
        console = pitchcatch.Console(stdin=io.StringIO(stdin_text), stdout=out,
                                     stderr=err, color=False)
        argv = ["--file", store_path or self.store_path, *args]
        code = pitchcatch.main(argv, console=console)
        # Re-open the store so assertions see what the command persisted.
        self.store, _ = pitchcatch.Store.open(self.store_path)
        return code, out.getvalue(), err.getvalue()

    def add_session(self, profile, pitches, *, on_date=None, kind="bullpen", target=None):
        session = self.store.open_session(profile["id"], kind, target, on_date=on_date)
        for pitch in pitches:
            self.store.add_pitch(session, pitch)
        return session


class CountHelperTests(unittest.TestCase):
    def test_parses_balls_and_strikes(self):
        self.assertEqual(pitchcatch.parse_count("1-2"), (1, 2))
        self.assertEqual(pitchcatch.parse_count(" 0-0 "), (0, 0))
        self.assertEqual(pitchcatch.parse_count("3-2"), (3, 2))

    def test_formats_back_to_the_same_text(self):
        self.assertEqual(pitchcatch.format_count(*pitchcatch.parse_count("1-2")), "1-2")

    def test_rejects_counts_outside_the_range_and_bad_shapes(self):
        for text in ("12", "x", "5-0", "1-3", "1-", "-2", "", "1-2-3", "a-b"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    pitchcatch.parse_count(text)
                self.assertEqual(str(caught.exception), "Enter count as balls-strikes, e.g. 1-2")


class ZoneAndNotationTests(unittest.TestCase):
    def test_zone_parses_numbers_and_out(self):
        self.assertEqual(pitchcatch.parse_zone("7"), 7)
        self.assertEqual(pitchcatch.parse_zone("out"), pitchcatch.OUT)
        self.assertEqual(pitchcatch.parse_zone("O"), pitchcatch.OUT)

    def test_zone_rejects_out_of_range_values(self):
        for text in ("10", "0", "x"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    pitchcatch.parse_zone(text)

    def test_zone_notation(self):
        self.assertEqual(pitchcatch.format_zone(7), "Z7")
        self.assertEqual(pitchcatch.format_zone(pitchcatch.OUT), "OUT")
        self.assertEqual(pitchcatch.format_zone(12), "OUT")

    def test_pitch_notation_reads_type_then_zone(self):
        self.assertEqual(pitchcatch.pitch_notation({"pitch_type": "SL", "zone": 7}), "SL Z7")
        self.assertEqual(pitchcatch.pitch_notation({"pitch_type": "SL", "zone": 10}), "SL OUT")
        self.assertEqual(
            pitchcatch.pitch_notation({"pitch_type": "CH", "zone": pitchcatch.OUT}), "CH OUT")


class InputParserTests(unittest.TestCase):
    def test_pitch_type_accepts_known_codes_only(self):
        self.assertEqual(pitchcatch.parse_pitch_type("sl"), "SL")
        with self.assertRaises(ValueError):
            pitchcatch.parse_pitch_type("fastball")

    def test_target_is_optional_and_positive(self):
        self.assertIsNone(pitchcatch.parse_pitch_target(""))
        self.assertEqual(pitchcatch.parse_pitch_target("25"), 25)
        with self.assertRaises(ValueError):
            pitchcatch.parse_pitch_target("0")
        with self.assertRaises(ValueError):
            pitchcatch.parse_pitch_target("lots")

    def test_velocity_is_optional_and_bounded(self):
        self.assertIsNone(pitchcatch.parse_velocity(""))
        self.assertEqual(pitchcatch.parse_velocity("94.2"), 94.2)
        for text in ("abc", "200", "10"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    pitchcatch.parse_velocity(text)

    def test_result_accepts_aliases(self):
        self.assertEqual(pitchcatch.parse_result("c"), "called")
        self.assertEqual(pitchcatch.parse_result("In Play"), "inplay")
        with self.assertRaises(ValueError):
            pitchcatch.parse_result("homer")

    def test_kind_accepts_bullpen_and_game(self):
        self.assertEqual(pitchcatch.parse_kind("b"), "bullpen")
        self.assertEqual(pitchcatch.parse_kind("GAME"), "game")
        with self.assertRaises(ValueError):
            pitchcatch.parse_kind("scrimmage")

    def test_pin_is_four_to_eight_digits(self):
        self.assertIsNone(pitchcatch.parse_pin(""))
        self.assertEqual(pitchcatch.parse_pin("4821"), "4821")
        for text in ("12", "123456789", "abcd"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    pitchcatch.parse_pin(text)

    def test_arsenal_keeps_canonical_order_and_rejects_unknown_codes(self):
        self.assertEqual(pitchcatch.parse_arsenal("CH, FF, SL"), ["FF", "SL", "CH"])
        with self.assertRaises(ValueError):
            pitchcatch.parse_arsenal("FF,ZZ")
        with self.assertRaises(ValueError):
            pitchcatch.parse_arsenal("   ")


class PinHashTests(unittest.TestCase):
    def test_pin_round_trips_through_the_hash(self):
        salt, digest = pitchcatch.hash_pin("4821")
        self.assertTrue(pitchcatch.verify_pin("4821", salt, digest))
        self.assertFalse(pitchcatch.verify_pin("4822", salt, digest))

    def test_each_hash_uses_a_fresh_salt(self):
        first = pitchcatch.hash_pin("4821")
        second = pitchcatch.hash_pin("4821")
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_hash_never_contains_the_pin(self):
        salt, digest = pitchcatch.hash_pin("4821")
        self.assertNotIn("4821", salt)
        self.assertNotIn("4821", digest)


class StoreTests(TempStoreCase):
    def test_first_run_creates_a_versioned_empty_store(self):
        with open(self.store_path, "r", encoding="utf-8") as handle:
            raw = json.loads(handle.read())
        self.assertEqual(raw["schema_version"], pitchcatch.SCHEMA_VERSION)
        self.assertEqual(raw["profiles"], [])
        self.assertEqual(raw["sessions"], [])
        self.assertIsNone(self.warning)

    def test_corrupt_store_is_moved_aside_with_one_warning(self):
        with open(self.store_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        store, warning = pitchcatch.Store.open(self.store_path)
        self.assertIsNotNone(warning)
        self.assertIn(".bad", warning)
        self.assertTrue(os.path.exists(self.store_path + ".bad"))
        self.assertEqual(store.profiles, [])
        self.assertEqual(store.sessions, [])

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        store = pitchcatch.Store(self.store_path, pitchcatch.empty_store())
        store.save()
        leftovers = [name for name in os.listdir(self._tmp.name) if name.startswith(".pitchcatch-")]
        self.assertEqual(leftovers, [])

    def test_two_sessions_survive_a_reopen(self):
        profile = self.store.create_profile("Ace")
        self.add_session(profile, [make_pitch()], on_date="2024-05-01")
        self.add_session(profile, [make_pitch(), make_pitch()], on_date="2024-05-02")
        reopened, _ = pitchcatch.Store.open(self.store_path)
        self.assertEqual(len(reopened.sessions_for(profile["id"])), 2)
        self.assertEqual(len(reopened.sessions_for(profile["id"])[0]["pitches"]), 2)

    def test_profiles_are_found_by_name_or_id_prefix(self):
        profile = self.store.create_profile("Rosa Delgado")
        self.assertEqual(self.store.find_profile("rosa delgado")["id"], profile["id"])
        self.assertEqual(self.store.find_profile(profile["id"][:8])["id"], profile["id"])
        self.assertIsNone(self.store.find_profile("nobody"))

    def test_duplicate_names_are_refused(self):
        self.store.create_profile("Ace")
        with self.assertRaises(pitchcatch.PitchcatchError):
            self.store.create_profile("ace")

    def test_pin_is_stored_hashed_only(self):
        profile = self.store.create_profile("Jordan", pin="4821")
        with open(self.store_path, "r", encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertNotIn("4821", on_disk)
        self.assertTrue(pitchcatch.is_pin_protected(profile))
        self.assertFalse(pitchcatch.is_pin_protected(self.store.create_profile("Open")))

    def test_arsenal_defaults_to_the_full_list_then_can_be_trimmed(self):
        profile = self.store.create_profile("Ace")
        self.assertEqual(pitchcatch.arsenal_of(profile), list(pitchcatch.PITCH_TYPES))
        self.store.set_arsenal(profile, ["FF", "SL", "CH"])
        self.assertEqual(pitchcatch.arsenal_of(profile), ["FF", "SL", "CH"])


class MixGridTests(unittest.TestCase):
    def test_rows_count_zones_and_out_bucket(self):
        pitches = [make_pitch("SL", 7) for _ in range(3)]
        pitches += [make_pitch("FF", 4) for _ in range(2)]
        pitches.append(make_pitch("SL", pitchcatch.OUT))
        rows, grand_total = pitchcatch.build_mix_rows(pitches)
        self.assertEqual(grand_total, 6)
        by_type = {row.pitch_type: row for row in rows}
        self.assertEqual(by_type["SL"].zones[6], 3)
        self.assertEqual(by_type["SL"].out, 1)
        self.assertEqual(by_type["SL"].total, 4)
        self.assertEqual(by_type["FF"].zones[3], 2)
        self.assertAlmostEqual(by_type["FF"].share, 2 / 6)

    def test_rendered_grid_shows_the_sl_row_at_sixty_percent(self):
        pitches = [make_pitch("SL", 7) for _ in range(3)]
        pitches += [make_pitch("FF", 4) for _ in range(2)]
        rows, grand_total = pitchcatch.build_mix_rows(pitches)
        lines = pitchcatch.render_mix_grid(rows, grand_total)
        slider = next(line for line in lines if line.startswith("SL"))
        self.assertIn("3", slider.split())
        self.assertIn("60%", slider)
        self.assertEqual(slider.split()[1:10][6], "3")   # Z7 column
        self.assertEqual(lines[-1].split()[0], "TOTAL")
        self.assertIn("100%", lines[0 - 1])

    def test_grid_fits_a_hundred_columns(self):
        rows, grand_total = pitchcatch.build_mix_rows([make_pitch() for _ in range(50)])
        for line in pitchcatch.render_mix_grid(rows, grand_total):
            self.assertLessEqual(pitchcatch.visible_len(line), 100)

    def test_count_filter_selects_only_that_count(self):
        pitches = [make_pitch("SL", 7, balls=0, strikes=2), make_pitch("FF", 4, 1, 0)]
        selected = pitchcatch.filter_pitches(pitches, count=(0, 2))
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["pitch_type"], "SL")

    def test_pitch_type_filter(self):
        pitches = [make_pitch("SL", 7), make_pitch("FF", 4)]
        self.assertEqual(len(pitchcatch.filter_pitches(pitches, pitch_type="SL")), 1)


class HeatGridTests(unittest.TestCase):
    def test_cells_count_zone_hits_only(self):
        pitches = [make_pitch("SL", 7), make_pitch("SL", 7), make_pitch("FF", 4),
                   make_pitch("FF", pitchcatch.OUT)]
        cells = pitchcatch.build_heat_cells(pitches)
        self.assertEqual(cells[7], 2)
        self.assertEqual(cells[4], 1)
        self.assertEqual(sum(cells.values()), 3)

    def test_shading_grows_with_the_tally(self):
        cells = {zone: 0 for zone in pitchcatch.ZONES}
        cells[5], cells[7] = 6, 1
        hot = pitchcatch.heat_bar(6, 6, color=False)
        cold = pitchcatch.heat_bar(1, 6, color=False)
        self.assertEqual(len(hot), pitchcatch.HEAT_BAR_WIDTH)
        self.assertEqual(len(cold), pitchcatch.HEAT_BAR_WIDTH)
        self.assertGreater(hot.count("#") + hot.count("%") + hot.count("@") + hot.count("*"),
                           cold.count("#") + cold.count("%") + cold.count("@") + cold.count("*"))
        self.assertGreaterEqual(pitchcatch.HEAT_RAMP_ASCII.index(hot.strip()[0]),
                                pitchcatch.HEAT_RAMP_ASCII.index(cold.strip()[0]))

    def test_plain_output_is_ascii_only(self):
        cells = {zone: 0 for zone in pitchcatch.ZONES}
        cells[5] = 4
        lines = pitchcatch.render_heat_grid(cells, 4, color=False)
        self.assertNotIn("\033", "".join(lines))
        self.assertEqual(len(lines), 11)

    def test_colour_output_uses_ansi_backgrounds(self):
        cells = {zone: 0 for zone in pitchcatch.ZONES}
        cells[5] = 4
        lines = pitchcatch.render_heat_grid(cells, 4, color=True)
        self.assertIn("\033[48;2;0;", "".join(lines))
        self.assertNotIn("·", "".join(lines))

    def test_frame_lines_share_one_width(self):
        cells = {zone: 0 for zone in pitchcatch.ZONES}
        cells[1], cells[9] = 2, 5
        lines = pitchcatch.render_heat_grid(cells, 7, color=False)
        widths = {pitchcatch.visible_len(line) for line in lines}
        self.assertEqual(len(widths), 1)

    def test_hottest_and_coldest_zones(self):
        cells = {zone: 0 for zone in pitchcatch.ZONES}
        cells[7] = 5
        cells[5] = 2
        hottest, coldest = pitchcatch.hottest_and_coldest(cells)
        self.assertEqual(hottest, 7)
        self.assertEqual(cells[coldest], 0)


class VolumeTests(TempStoreCase):
    def test_rows_mark_a_session_over_target(self):
        profile = self.store.create_profile("Ace")
        self.add_session(profile, [make_pitch() for _ in range(31)], target=25)
        self.add_session(profile, [make_pitch() for _ in range(10)], target=25,
                         on_date=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))
        rows = pitchcatch.volume_rows(self.store, profile)
        over = [row for row in rows if row["over_target"]]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["pitches"], 31)
        rendered = "\n".join(pitchcatch.render_volume_table(rows))
        self.assertIn("over target (25)", rendered)

    def test_breakdown_counts_each_pitch_type(self):
        profile = self.store.create_profile("Ace")
        self.add_session(profile, [make_pitch("SL") for _ in range(3)] + [make_pitch("FF")])
        rows = pitchcatch.volume_rows(self.store, profile)
        self.assertEqual(rows[0]["breakdown"], [("FF", 1), ("SL", 3)])

    def test_days_window_excludes_old_sessions(self):
        profile = self.store.create_profile("Ace")
        old = (date.today() - timedelta(days=45)).strftime("%Y-%m-%d")
        recent = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")
        self.add_session(profile, [make_pitch()], on_date=old)
        self.add_session(profile, [make_pitch()], on_date=recent)
        self.assertEqual(len(pitchcatch.volume_rows(self.store, profile, days=30)), 1)
        self.assertEqual(len(pitchcatch.volume_rows(self.store, profile)), 2)


class CliViewTests(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.profile = self.store.create_profile("Ace Ramirez")

    def test_mix_grid_renders_a_session_with_totals(self):
        session = self.add_session(self.profile,
                                   [make_pitch("SL", 7) for _ in range(3)]
                                   + [make_pitch("FF", 4) for _ in range(2)])
        code, out, _ = self.run_cli(["mix", "--session", session["id"][:8]], "-")
        self.assertEqual(code, 0)
        self.assertIn("Pitch mix - Ace Ramirez", out)
        slider = next(line for line in out.splitlines() if line.startswith("SL"))
        self.assertIn("3", slider)
        self.assertIn("60%", slider)

    def test_mix_count_filter_changes_the_totals(self):
        self.add_session(self.profile,
                         [make_pitch("SL", 7, balls=0, strikes=2),
                          make_pitch("FF", 4, balls=1, strikes=0)])
        code, out, _ = self.run_cli(["mix", "--count", "0-2"])
        self.assertEqual(code, 0)
        self.assertIn("1 pitches in view", out)
        code, out, _ = self.run_cli(["mix", "--count", "2-2"])
        self.assertEqual(code, 0)
        self.assertIn("No pitches", out)

    def test_mix_on_an_empty_session_prints_the_skeleton_and_the_hint(self):
        session = self.add_session(self.profile, [])
        code, out, _ = self.run_cli(["mix"])
        self.assertEqual(code, 0)
        self.assertIn("No pitches yet - run pitchcatch log", out)
        self.assertIn("TYPE", out)
        self.assertEqual(session["id"][:8], self.store.sessions_for(self.profile["id"])[0]["id"][:8])

    def test_mix_rejects_a_bad_count_with_the_accepted_format(self):
        self.add_session(self.profile, [make_pitch()])
        code, _, err = self.run_cli(["mix", "--count", "12"])
        self.assertEqual(code, 2)
        self.assertIn("Enter count as balls-strikes, e.g. 1-2", err)

    def test_unknown_session_id_lists_the_valid_ids(self):
        session = self.add_session(self.profile, [make_pitch()])
        code, _, err = self.run_cli(["mix", "--session", "doesnotexist"])
        self.assertEqual(code, 2)
        self.assertIn(session["id"][:8], err)

    def test_heat_tally_names_the_hottest_and_coldest_zone(self):
        self.add_session(self.profile,
                         [make_pitch("SL", 7, balls=0, strikes=2) for _ in range(3)]
                         + [make_pitch("FF", 4, balls=0, strikes=2)])
        code, out, _ = self.run_cli(["heat", "--count", "0-2"])
        self.assertEqual(code, 0)
        self.assertIn("Hottest zone Z7", out)
        self.assertIn("coldest Z1 with no pitches", out)
        self.assertIn("3 pitches", out)

    def test_heat_with_no_matching_pitches_still_prints_the_zero_grid(self):
        self.add_session(self.profile, [make_pitch("SL", 7, balls=0, strikes=2)])
        code, out, _ = self.run_cli(["heat", "--count", "2-2"])
        self.assertEqual(code, 0)
        self.assertIn("No pitches at 2-2 yet - log some with pitchcatch log", out)
        self.assertEqual(out.count("0 pitches  0%"), 9)

    def test_heat_pitch_filter_only_tallies_that_type(self):
        self.add_session(self.profile,
                         [make_pitch("SL", 7, balls=0, strikes=1),
                          make_pitch("FF", 4, balls=0, strikes=1)])
        code, out, _ = self.run_cli(["heat", "--count", "0-1", "--pitch", "SL"])
        self.assertEqual(code, 0)
        self.assertIn("1 on the plate", out)
        self.assertIn("Hottest zone Z7", out)

    def test_volume_lists_sessions_with_a_seven_day_total(self):
        self.add_session(self.profile, [make_pitch() for _ in range(24)], target=25)
        self.add_session(self.profile, [make_pitch() for _ in range(31)], target=25,
                         on_date=(date.today() - timedelta(days=2)).strftime("%Y-%m-%d"))
        code, out, _ = self.run_cli(["volume"])
        self.assertEqual(code, 0)
        self.assertIn("7-day total: 55 pitches over 2 sessions", out)
        self.assertIn("over target (25)", out)

    def test_volume_with_no_sessions_points_at_the_log_command(self):
        code, out, _ = self.run_cli(["volume"])
        self.assertEqual(code, 0)
        self.assertIn("No sessions recorded yet", out)
        self.assertIn("pitchcatch log", out)

    def test_volume_days_window_filters_rows(self):
        old = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        self.add_session(self.profile, [make_pitch()], on_date=old)
        self.add_session(self.profile, [make_pitch()], on_date=date.today().strftime("%Y-%m-%d"))
        code, out, _ = self.run_cli(["volume", "--days", "30"])
        self.assertEqual(code, 0)
        self.assertIn("Window total: 1 pitches over 1 session", out)

    def test_mix_all_aggregates_every_session(self):
        self.add_session(self.profile, [make_pitch("SL", 7) for _ in range(3)],
                         on_date=date.today().strftime("%Y-%m-%d"))
        self.add_session(self.profile, [make_pitch("SL", 7) for _ in range(2)],
                         on_date=(date.today() - timedelta(days=2)).strftime("%Y-%m-%d"))
        code, out, _ = self.run_cli(["mix", "--all"])
        self.assertEqual(code, 0)
        self.assertIn("all sessions (2 sessions, 5 pitches)", out)
        slider = next(line for line in out.splitlines() if line.startswith("SL"))
        self.assertIn("5", slider)
        self.assertIn("100%", out)

    def test_heat_all_reports_the_aggregate_scope(self):
        for offset in (0, 1, 2):
            self.add_session(self.profile, [make_pitch("FF", 5)],
                             on_date=(date.today() - timedelta(days=offset)).strftime("%Y-%m-%d"))
        code, out, _ = self.run_cli(["heat", "--all"])
        self.assertEqual(code, 0)
        self.assertIn("all sessions (3 sessions, 3 pitches)", out)

    def test_sessions_command_lists_ids_and_counts(self):
        session = self.add_session(self.profile, [make_pitch(), make_pitch()])
        code, out, _ = self.run_cli(["sessions"])
        self.assertEqual(code, 0)
        self.assertIn(session["id"][:8], out)
        self.assertIn("2", out)

    def test_json_flags_emit_parseable_output(self):
        session = self.add_session(self.profile, [make_pitch("SL", 7)])
        for args, key in ((["mix", "--json"], "rows"), (["volume", "--json"], "sessions"),
                          (["heat", "--json"], "cells"), (["sessions", "--json"], None)):
            with self.subTest(args=args):
                code, out, _ = self.run_cli(args)
                self.assertEqual(code, 0)
                payload = json.loads(out)
                if key is not None:
                    self.assertIn(key, payload)
        code, out, _ = self.run_cli(["mix", "--json", "--session", session["id"][:8]])
        self.assertEqual(json.loads(out)["total"], 1)

    def test_unknown_pitch_filter_is_reported_not_crashed(self):
        self.add_session(self.profile, [make_pitch()])
        code, _, err = self.run_cli(["mix", "--pitch", "ZZ"])
        self.assertEqual(code, 2)
        self.assertIn("Enter a pitch type", err)


class ProfileCliTests(TempStoreCase):
    def test_profiles_lists_created_pitchers_as_json(self):
        code, out, _ = self.run_cli(["profiles", "--new", "Rosa Delgado"])
        self.assertEqual(code, 0)
        self.assertIn("Created profile Rosa Delgado", out)
        code, out, _ = self.run_cli(["profiles", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["profiles"][0]["name"], "Rosa Delgado")
        self.assertFalse(payload["profiles"][0]["pin_protected"])

    def test_duplicate_profile_is_refused(self):
        self.run_cli(["profiles", "--new", "Ace"])
        code, _, err = self.run_cli(["profiles", "--new", "Ace"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_three_wrong_pins_leave_the_profile_locked(self):
        self.run_cli(["profiles", "--new", "Jordan Blake", "--pin", "4821"])
        code, out, err = self.run_cli(
            ["volume", "--profile", "Jordan Blake"], stdin_text="1111\n2222\n3333\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(err.count("That PIN does not match this profile."), 3)
        self.assertIn("Could not unlock", err)
        self.assertNotIn("Bullpen volume", out)

    def test_the_right_pin_unlocks_the_profile(self):
        self.run_cli(["profiles", "--new", "Jordan Blake", "--pin", "4821"])
        code, out, err = self.run_cli(
            ["volume", "--profile", "Jordan Blake"], stdin_text="4821\n")
        self.assertEqual(code, 0)
        self.assertIn("Bullpen volume - Jordan Blake", out)

    def test_pin_never_appears_in_the_store_file(self):
        self.run_cli(["profiles", "--new", "Jordan Blake", "--pin", "4821"])
        with open(self.store_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("4821", raw)
        self.assertIn("pin_hash", raw)

    def test_profiles_scope_sessions_to_one_pitcher(self):
        self.run_cli(["profiles", "--new", "Ace"])
        self.run_cli(["profiles", "--new", "Bo"])
        ace = self.store.find_profile("Ace")
        self.add_session(ace, [make_pitch() for _ in range(4)])
        code, out, _ = self.run_cli(["volume", "--profile", "Bo"])
        self.assertEqual(code, 0)
        self.assertIn("No sessions recorded yet", out)
        code, out, _ = self.run_cli(["volume", "--profile", "Ace"])
        self.assertIn("4", out)

    def test_unknown_profile_is_reported_with_the_known_names(self):
        self.run_cli(["profiles", "--new", "Ace"])
        code, _, err = self.run_cli(["volume", "--profile", "Nobody"])
        self.assertEqual(code, 2)
        self.assertIn("Known profiles: Ace", err)

    def test_several_profiles_without_a_flag_asks_for_one(self):
        self.run_cli(["profiles", "--new", "Ace"])
        self.run_cli(["profiles", "--new", "Bo"])
        code, _, err = self.run_cli(["volume"])
        self.assertEqual(code, 2)
        self.assertIn("--profile", err)

    def test_arsenal_changes_which_types_are_offered(self):
        self.run_cli(["profiles", "--new", "Ace"])
        code, out, _ = self.run_cli(["profiles", "--select", "Ace", "--arsenal", "FF,SL,CH"])
        self.assertEqual(code, 0)
        self.assertIn("arsenal set to: FF, SL, CH", out)
        profile = self.store.find_profile("Ace")
        self.assertEqual(pitchcatch.arsenal_of(profile), ["FF", "SL", "CH"])

    def test_no_profiles_at_all_is_reported_clearly(self):
        code, _, err = self.run_cli(["mix"])
        self.assertEqual(code, 2)
        self.assertIn("No profiles yet", err)


class PitchEntryLoopTests(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.profile = self.store.create_profile("Ace Ramirez")

    def test_prompts_record_a_pitch_and_save_it_immediately(self):
        stdin = "bullpen\n25\nFF\n7\n0-1\ncalled\n94.2\nq\n"
        code, out, _ = self.run_cli(["log"], stdin_text=stdin)
        self.assertEqual(code, 0)
        self.assertIn("Saved 1 pitch to session", out)
        sessions = self.store.sessions_for(self.profile["id"])
        self.assertEqual(len(sessions), 1)
        pitch = sessions[0]["pitches"][0]
        self.assertEqual(pitch["pitch_type"], "FF")
        self.assertEqual(pitch["zone"], 7)
        self.assertEqual((pitch["balls"], pitch["strikes"]), (0, 1))
        self.assertEqual(pitch["result"], "called")
        self.assertEqual(pitch["velocity_mph"], 94.2)
        reopened, _ = pitchcatch.Store.open(self.store_path)
        self.assertEqual(len(reopened.sessions_for(self.profile["id"])[0]["pitches"]), 1)

    def test_enter_at_the_pitch_type_prompt_finishes_the_session(self):
        stdin = "bullpen\n\nFF\n7\n0-0\nball\n\n\n"
        code, out, _ = self.run_cli(["log"], stdin_text=stdin)
        self.assertEqual(code, 0)
        self.assertIn("Saved 1 pitch to session", out)
        session = self.store.sessions_for(self.profile["id"])[0]
        self.assertIsNotNone(session["ended_at"])

    def test_velocity_may_be_skipped_with_enter(self):
        code, _, _ = self.run_cli(["log"], stdin_text="bullpen\n\nSL\n5\n1-2\nswing\n\nq\n")
        self.assertEqual(code, 0)
        pitch = self.store.sessions_for(self.profile["id"])[0]["pitches"][0]
        self.assertIsNone(pitch["velocity_mph"])

    def test_invalid_zone_reprompts_and_keeps_the_type(self):
        stdin = "bullpen\n\nFF\n10\n7\n0-1\ncalled\n\nq\n"
        code, out, err = self.run_cli(["log"], stdin_text=stdin)
        self.assertEqual(code, 0)
        self.assertIn("Enter a zone 1-9", err)
        pitch = self.store.sessions_for(self.profile["id"])[0]["pitches"][0]
        self.assertEqual(pitch["pitch_type"], "FF")
        self.assertEqual(pitch["zone"], 7)

    def test_invalid_count_reprompts_with_the_accepted_format(self):
        stdin = "bullpen\n\nFF\n7\n5-0\n0-1\ncalled\n\nq\n"
        code, _, err = self.run_cli(["log"], stdin_text=stdin)
        self.assertEqual(code, 0)
        self.assertIn("Enter count as balls-strikes, e.g. 1-2", err)
        pitch = self.store.sessions_for(self.profile["id"])[0]["pitches"][0]
        self.assertEqual((pitch["balls"], pitch["strikes"]), (0, 1))

    def test_u_undoes_the_last_pitch_and_rewrites_the_store(self):
        stdin = "bullpen\n\nFF\n7\n0-0\nball\n\nSL\n5\n0-1\nswing\n\nu\nq\n"
        code, out, _ = self.run_cli(["log"], stdin_text=stdin)
        self.assertEqual(code, 0)
        self.assertIn("Undid SL Z5 0-1 swing", out)
        session = self.store.sessions_for(self.profile["id"])[0]
        self.assertEqual(len(session["pitches"]), 1)
        self.assertEqual(session["pitches"][0]["pitch_type"], "FF")

    def test_blank_pitch_type_quits_and_saves(self):
        code, out, _ = self.run_cli(["log"], stdin_text="bullpen\n\n\n")
        self.assertEqual(code, 0)
        self.assertIn("Saved 0 pitches to session", out)

    def test_numbered_arsenal_choices_pick_a_pitch_type(self):
        self.store.set_arsenal(self.profile, ["FF", "SL", "CH"])
        code, out, _ = self.run_cli(["log"], stdin_text="bullpen\n\n2\n7\n0-1\nswing\n\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("1=FF  2=SL  3=CH", out)
        pitch = self.store.sessions_for(self.profile["id"])[0]["pitches"][0]
        self.assertEqual(pitch["pitch_type"], "SL")

    def test_zero_opens_the_full_list_for_one_pitch(self):
        self.store.set_arsenal(self.profile, ["FF", "SL", "CH"])
        code, out, _ = self.run_cli(["log"], stdin_text="bullpen\n\n0\n5\n7\n0-1\nswing\n\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("All pitch types", out)
        pitch = self.store.sessions_for(self.profile["id"])[0]["pitches"][0]
        self.assertEqual(pitch["pitch_type"], "CU")

    def test_keyboard_interrupt_saves_and_exits_one_three_zero(self):
        with mock.patch.object(pitchcatch, "choose_pitch_type",
                               side_effect=["FF", KeyboardInterrupt]):
            code, _, err = self.run_cli(["log"], stdin_text="bullpen\n20\n7\n0-1\ncalled\n94\n")
        self.assertEqual(code, 130)
        self.assertIn("Session saved.", err)
        session = self.store.sessions_for(self.profile["id"])[0]
        self.assertEqual(len(session["pitches"]), 1)
        self.assertIsNotNone(session["ended_at"])

    def test_log_flag_supplied_kind_and_target_skip_the_first_prompts(self):
        code, out, _ = self.run_cli(["log", "--kind", "game", "--target", "35"],
                                    stdin_text="FF\n5\n0-0\nball\n\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("target 35 pitches", out)
        session = self.store.sessions_for(self.profile["id"])[0]
        self.assertEqual(session["kind"], "game")
        self.assertEqual(session["target_pitches"], 35)

    def test_no_input_at_all_records_nothing(self):
        code, _, err = self.run_cli(["log"], stdin_text="")
        self.assertEqual(code, 1)
        self.assertIn("No input received", err)
        self.assertEqual(self.store.sessions_for(self.profile["id"]), [])


class IsolationTests(TempStoreCase):
    def test_the_default_store_is_never_written_by_a_command(self):
        default = pitchcatch.default_store_path()
        self.assertTrue(default.endswith(os.path.join(".pitchcatch", "pitchcatch.json")))
        before = os.path.getmtime(default) if os.path.exists(default) else None
        self.run_cli(["profiles", "--new", "Ace"])
        self.run_cli(["seed"])
        after = os.path.getmtime(default) if os.path.exists(default) else None
        self.assertEqual(before, after)

    def test_seed_is_idempotent(self):
        self.run_cli(["seed"])
        first = len(self.store.sessions)
        self.assertGreater(first, 0)
        self.run_cli(["seed"])
        self.assertEqual(len(self.store.sessions), first)

    def test_seeded_data_renders_every_view(self):
        self.run_cli(["seed"])
        profile = self.store.find_profile("Ace Ramirez")
        for args in (["mix"], ["heat"], ["volume"], ["sessions"]):
            with self.subTest(args=args):
                code, out, err = self.run_cli([*args, "--profile", profile["name"]])
                self.assertEqual(code, 0, err)
                self.assertNotEqual(out.strip(), "")


class MenuTests(TempStoreCase):
    def test_menu_lists_every_option_and_opens_the_mix_view(self):
        profile = self.store.create_profile("Ace Ramirez")
        self.add_session(profile, [make_pitch("SL", 7)])
        code, out, _ = self.run_cli([], stdin_text="m\nq\n")
        self.assertEqual(code, 0)
        for label in ("Log session", "Mix grid", "Heat tally", "Volume", "Profiles", "Quit"):
            self.assertIn(label, out)
        self.assertIn("Active profile: Ace Ramirez", out)
        self.assertIn("Pitch mix - Ace Ramirez", out)

    def test_menu_volume_option_reaches_the_volume_view(self):
        profile = self.store.create_profile("Ace Ramirez")
        self.add_session(profile, [make_pitch()])
        code, out, _ = self.run_cli([], stdin_text="v\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("Bullpen volume - Ace Ramirez", out)

    def test_menu_without_a_profile_invites_creating_one(self):
        code, out, _ = self.run_cli([], stdin_text="Ace\n\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("No pitcher profile yet", out)
        self.assertIn("Created profile Ace", out)

    def test_menu_with_no_input_exits_cleanly(self):
        code, out, _ = self.run_cli([], stdin_text="")
        self.assertEqual(code, 0)
        self.assertIn("No pitcher profile yet", out)

    def test_menu_unknown_choice_is_reported(self):
        self.store.create_profile("Ace")
        code, _, err = self.run_cli([], stdin_text="z\nq\n")
        self.assertEqual(code, 0)
        self.assertIn("Enter l, m, h, v, p or q", err)


class HelpTests(unittest.TestCase):
    def test_help_lists_every_subcommand(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as caught:
                pitchcatch.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = out.getvalue()
        for command in ("log", "mix", "heat", "volume", "profiles", "sessions", "seed"):
            self.assertIn(command, text)

    def test_version_is_printed(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit):
                pitchcatch.main(["--version"])
        self.assertIn(pitchcatch.__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
