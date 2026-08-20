#!/usr/bin/env python3
"""Tests for the audit report's two layouts. No camera needed.

The operator can have the analytics split into one worksheet tab per location
(the default) or flattened onto a single sheet. The property that matters is
that this is PRESENTATION ONLY: the same rows must reach the workbook either
way, carrying the same Client/Location, with the same styling and the same
global Missed Cameras tab. A layout switch that quietly dropped or merged rows
would be indistinguishable from a clean run at a glance, so these tests count
rows as well as tabs.

Run:  .venv\\Scripts\\python.exe test_report_layout.py
"""

from __future__ import annotations

import inspect
import json
import sys

import camera_engine as ce
import ui_theme


DATA_START = 4  # title, timeline context, headers, then rows

LOCS = ["Provo Yard", "Lehi Lot", "Provo Yard", "Lehi Lot", "Provo Yard"]


def _build(split, locations):
    """Drive the same on-demand sheet lookup run_batch uses, and write one row per
    location so the tests can count what landed where."""
    wb, _ws_missed = ce.create_master_workbook()
    timeline = "Batch Processing Timeline Context: TEST"
    sheets, used_titles = {}, set()

    def get_sheet(loc):
        key = ((loc or "").strip() or "Unspecified Location") if split else ce.COMBINED_SHEET_TITLE
        if key not in sheets:
            ws = (ce.add_location_sheet(wb, key, timeline, used_titles) if split
                  else ce.add_combined_sheet(wb, timeline))
            sheets[key] = {"ws": ws, "row": DATA_START}
        return sheets[key]

    for loc in locations:
        entry = get_sheet(loc)
        entry["ws"].cell(row=entry["row"], column=1, value="Test Client")
        entry["ws"].cell(row=entry["row"], column=2, value=loc)
        entry["row"] += 1

    missed = wb["Missed Cameras"]           # mirrors run_batch's final reorder
    wb._sheets.remove(missed)
    wb._sheets.append(missed)
    return wb


def _analytics_sheets(wb):
    return [n for n in wb.sheetnames if n != "Missed Cameras"]


def _data_rows(ws):
    """The Location cell of every written row on a sheet."""
    return [ws.cell(row=r, column=2).value
            for r in range(DATA_START, ws.max_row + 1)
            if ws.cell(row=r, column=2).value is not None]


# --------------------------------------------------------------------------- #

def test_split_makes_one_tab_per_unique_location():
    wb = _build(True, LOCS)
    assert _analytics_sheets(wb) == ["Provo Yard", "Lehi Lot"], _analytics_sheets(wb)
    return "5 cameras across 2 locations -> 2 tabs, not 5"


def test_single_sheet_makes_exactly_one_tab():
    wb = _build(False, LOCS)
    assert _analytics_sheets(wb) == [ce.COMBINED_SHEET_TITLE], _analytics_sheets(wb)
    return f"every camera lands on {ce.COMBINED_SHEET_TITLE!r}"


def test_no_row_is_lost_or_duplicated_by_flattening():
    wb_split, wb_flat = _build(True, LOCS), _build(False, LOCS)
    split_rows = sorted(v for name in _analytics_sheets(wb_split)
                        for v in _data_rows(wb_split[name]))
    flat_rows = sorted(_data_rows(wb_flat[ce.COMBINED_SHEET_TITLE]))
    assert split_rows == flat_rows == sorted(LOCS), f"{split_rows} != {flat_rows}"
    return f"{len(flat_rows)} rows either way, same locations"


def test_flat_sheet_still_names_each_rows_location():
    """The whole reason flattening is safe: Location survives as a column, so the
    grouping the tabs gave you is still recoverable with a sort or filter."""
    ws = _build(False, LOCS)[ce.COMBINED_SHEET_TITLE]
    assert ce.MAIN_HEADERS[1] == "Location", ce.MAIN_HEADERS[1]
    assert ws.cell(row=3, column=2).value == "Location"
    assert set(_data_rows(ws)) == set(LOCS)
    return "Location stays column 2 on the flat sheet"


def test_both_layouts_share_the_same_headers_and_styling():
    tab = _build(True, LOCS)["Provo Yard"]
    flat = _build(False, LOCS)[ce.COMBINED_SHEET_TITLE]
    for col, header in enumerate(ce.MAIN_HEADERS, 1):
        assert tab.cell(row=3, column=col).value == header
        assert flat.cell(row=3, column=col).value == header
    assert tab.freeze_panes == flat.freeze_panes == "A4"
    return f"{len(ce.MAIN_HEADERS)} identical headers, panes frozen the same"


def test_missed_cameras_tab_exists_and_stays_last_in_both_layouts():
    for split in (True, False):
        wb = _build(split, LOCS)
        assert wb.sheetnames[-1] == "Missed Cameras", (split, wb.sheetnames)
        assert len(wb.sheetnames) > 1, "report must not open on Missed Cameras"
    return "global failure tab is unaffected by the layout choice"


def test_flat_sheet_is_titled_for_the_whole_batch():
    """A tab says which location it is; the flat sheet must not claim to be one."""
    flat = _build(False, LOCS)[ce.COMBINED_SHEET_TITLE]
    expected = "Intelligent Analytics Master Report - All Locations"
    assert flat["A1"].value == expected, flat["A1"].value
    return flat["A1"].value


def test_blank_and_missing_locations_land_together_in_both_layouts():
    wb_split = _build(True, ["", None, "   "])
    assert _analytics_sheets(wb_split) == ["Unspecified Location"], _analytics_sheets(wb_split)
    wb_flat = _build(False, ["", None, "   "])
    assert _analytics_sheets(wb_flat) == [ce.COMBINED_SHEET_TITLE], _analytics_sheets(wb_flat)
    return "empty/None/whitespace all bucket together, never one tab each"


def test_run_batch_defaults_to_the_tabbed_layout():
    """Callers that predate the option -- combined.py, older scripts -- must keep
    getting the per-location split they were written against."""
    default = inspect.signature(ce.run_batch).parameters["split_by_location"].default
    assert default is True, default
    return "split_by_location defaults to True"


# --- the operator's saved choice ------------------------------------------- #

def _with_temp_prefs(fn):
    """Run fn against the real prefs file, restoring whatever was there before."""
    path = ui_theme.UI_PREFS
    backup = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        return fn(path)
    finally:
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8")


def test_saved_layout_survives_an_appearance_change():
    """Regression: save_appearance used to rewrite the whole prefs file, so any
    second key would be wiped the next time someone touched the theme toggle."""
    def body(path):
        ui_theme.save_report_layout(ui_theme.LAYOUT_SINGLE)
        ui_theme.save_appearance("Dark")
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["report_layout"] == ui_theme.LAYOUT_SINGLE, stored
        assert stored["appearance"] == "Dark", stored
        assert ui_theme.load_report_layout() == ui_theme.LAYOUT_SINGLE
        return "layout and appearance coexist in ui_prefs.json"
    return _with_temp_prefs(body)


def test_unreadable_or_bogus_prefs_fall_back_to_the_tabbed_default():
    def body(path):
        for junk in ('{"report_layout": "Whatever"}', "not json at all", "[]"):
            path.write_text(junk, encoding="utf-8")
            got = ui_theme.load_report_layout()
            assert got == ui_theme.DEFAULT_REPORT_LAYOUT, f"{junk!r} -> {got!r}"
        return "corrupt/unknown values never pick a layout that doesn't exist"
    return _with_temp_prefs(body)


def test_toggle_labels_are_the_values_that_get_persisted():
    """The segmented button writes its own label straight into prefs, so the two
    lists have to stay in step or a saved choice stops being recognized."""
    assert ui_theme.REPORT_LAYOUTS == (ui_theme.LAYOUT_TABS, ui_theme.LAYOUT_SINGLE)
    assert ui_theme.DEFAULT_REPORT_LAYOUT in ui_theme.REPORT_LAYOUTS
    return f"{ui_theme.REPORT_LAYOUTS}"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\naudit report layout -- {len(tests)} tests\n" + "=" * (width + 58))
    for name, fn in tests:
        try:
            print(f"PASS  {name:<{width}}  {fn() or ''}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL  {name:<{width}}  {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failures.append(name)
            print(f"ERROR {name:<{width}}  {type(exc).__name__}: {exc}")
    print("=" * (width + 58))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
