#!/usr/bin/env python3
"""Tests for the concurrent-editor guard on the analytics writer. No camera needed.

WHY THIS EXISTS. On 2026-08-19 an operator edited six zones on 10.23.0.148:5015 from
the multi-camera writer. Every push reported "Pushed OK", every push really did land
-- and three of them were silently rolled back. The camera's own AOA web page was
open in a browser the whole time; that page loads the WHOLE configuration when it
opens and writes the WHOLE configuration on Save, so each Save reverted every zone
pushed since the page had been loaded.

Reconstructed from the writer's own backups (each push dumps the pre-write config):

    push 08:59:04 -> s2 edited, and s3/s5/s6 reverted to their 08:56:46 values
    push 09:02:50 -> s2/s5/s6 reverted, s3 carrying a hand-drag

Both are exactly "a full config read at T, hand-edited, saved back at T+minutes".
Read-modify-write does not help: our read and our write are a second apart, and the
other editor's stale copy is minutes old. Only comparing the camera against the state
the edit was DRAWN ON can catch it, which is what these tests pin.

Run:  .venv/Scripts/python.exe test_concurrent_edit.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import aoa_config
import vendor_adapter
from vendor_adapter import Scenario


# --------------------------------------------------------------------------- #
# A camera that keeps its config in memory and records every write.

class FakeAOAClient:
    def __init__(self, cfg):
        self.cfg = copy.deepcopy(cfg)
        self.writes = []
        self.ip, self.port = "10.0.0.1", "5015"

    def get_config(self):
        return copy.deepcopy(self.cfg)

    def set_config(self, cfg):
        self.writes.append(copy.deepcopy(cfg))
        self.cfg = copy.deepcopy(cfg)

    def backup_config(self, backup_dir):
        return Path(backup_dir) / "fake_backup.json", copy.deepcopy(self.cfg)

    def apply_scenario(self, scenario, backup_dir, replace_by_name=True):
        aoa_config.validate_scenario(scenario)
        path, current = self.backup_config(backup_dir)
        self.set_config(aoa_config.insert_or_replace_scenario(
            current, scenario, replace_by_name=replace_by_name))
        return path, self.get_config()


def _scenario(sid, name, y):
    """A 4-vertex motion scenario in AOA space (x, y in [-1, 1], y-up)."""
    return {
        "id": sid, "name": name, "type": "motion",
        "devices": [{"id": 1}], "presets": [2], "perspectives": [],
        "objectClassifications": [{"type": "human"}],
        "filters": [],
        "triggers": [{"type": "includeArea",
                      "vertices": [[-0.5, y], [-0.5, y + 0.2], [0.5, y + 0.2], [0.5, y]]}],
    }


def _config(*scenarios):
    return {"apiVersion": "1.6", "method": "getConfiguration",
            "data": {"devices": [{"id": 1, "type": "camera", "isActive": True}],
                     "perspectives": [], "metadataOverlay": [],
                     "scenarios": [copy.deepcopy(s) for s in scenarios]}}


def _adapter(cfg):
    """An AxisAdapter wired to the fake camera, bypassing the real client's
    constructor (which builds an HTTP session and a snapshot handler)."""
    a = vendor_adapter.AxisAdapter.__new__(vendor_adapter.AxisAdapter)
    a.device_id = None
    a.client = FakeAOAClient(cfg)
    return a


def _edit(sid, points):
    return Scenario(name=f"Zone{sid}", kind="intrusion", points=points,
                    classes=("human",), native_id=sid)


BASE = _config(_scenario(1, "Zone1", -0.6), _scenario(2, "Zone2", -0.2),
               _scenario(3, "Zone3", 0.2))
NEW_POINTS = [(0.1, 0.1), (0.1, 0.4), (0.4, 0.4), (0.4, 0.1)]


# --------------------------------------------------------------------------- #
# fingerprint / drifted_rules

def test_an_untouched_camera_shows_no_drift():
    a = _adapter(BASE)
    fp = vendor_adapter.fingerprint(a.read_scenarios())
    assert vendor_adapter.drifted_rules(fp, fp) == []
    return f"{len(fp)} rules compare equal to themselves"


def test_a_moved_zone_is_reported_by_name():
    a = _adapter(BASE)
    before = vendor_adapter.fingerprint(a.read_scenarios())
    a.client.cfg["data"]["scenarios"][1]["triggers"][0]["vertices"][0] = [-0.9, -0.9]
    drift = vendor_adapter.drifted_rules(before, vendor_adapter.fingerprint(a.read_scenarios()))
    assert drift == ["Zone2"], drift
    return "only the moved rule is named"


def test_a_deleted_zone_is_drift():
    a = _adapter(BASE)
    before = vendor_adapter.fingerprint(a.read_scenarios())
    a.client.cfg["data"]["scenarios"] = a.client.cfg["data"]["scenarios"][:2]
    drift = vendor_adapter.drifted_rules(before, vendor_adapter.fingerprint(a.read_scenarios()))
    assert drift == ["Zone3 (deleted)"], drift
    return "a rule that vanished blocks the push"


def test_a_rule_added_elsewhere_is_not_drift():
    """Refusing on an ADDED rule would stop this tool working alongside any other.
    Nothing we drew was based on a rule that did not exist."""
    a = _adapter(BASE)
    before = vendor_adapter.fingerprint(a.read_scenarios())
    a.client.cfg["data"]["scenarios"].append(_scenario(4, "Zone4", 0.6))
    drift = vendor_adapter.drifted_rules(before, vendor_adapter.fingerprint(a.read_scenarios()))
    assert drift == [], drift
    return "an extra rule is tolerated"


def test_a_rename_names_both_sides():
    a = _adapter(BASE)
    before = vendor_adapter.fingerprint(a.read_scenarios())
    a.client.cfg["data"]["scenarios"][0]["name"] = "Gate"
    drift = vendor_adapter.drifted_rules(before, vendor_adapter.fingerprint(a.read_scenarios()))
    assert drift == ["Zone1 (now 'Gate')"], drift
    return "a renamed rule is traceable from the message"


def test_vendor_coordinate_rounding_is_not_drift():
    """Zones round-trip through AOA's [-1, 1] space; float noise must not read as an
    edit or every second push would be refused."""
    a = _adapter(BASE)
    before = vendor_adapter.fingerprint(a.read_scenarios())
    verts = a.client.cfg["data"]["scenarios"][0]["triggers"][0]["vertices"]
    verts[0] = [verts[0][0] + 1e-9, verts[0][1] - 1e-9]
    drift = vendor_adapter.drifted_rules(before, vendor_adapter.fingerprint(a.read_scenarios()))
    assert drift == [], drift
    return "sub-ulp differences ignored"


# --------------------------------------------------------------------------- #
# apply_scenario

def test_a_clean_push_still_writes():
    a = _adapter(BASE)
    expect = vendor_adapter.fingerprint(a.read_scenarios())
    a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=expect)
    assert len(a.client.writes) == 1, a.client.writes
    landed = next(s for s in a.client.cfg["data"]["scenarios"] if s["id"] == 2)
    assert landed["triggers"][0]["vertices"][0] == [-0.8, 0.8], landed["triggers"][0]["vertices"]
    return "guard is not in the way of a normal edit"


def test_the_incident_a_stale_full_config_save_blocks_the_next_push():
    """THE REGRESSION. Tool loads; the browser page (opened at the same moment) is
    saved, reverting Zone1 and Zone3; the tool then pushes Zone2. Must refuse, and
    must write NOTHING."""
    a = _adapter(BASE)
    expect = vendor_adapter.fingerprint(a.read_scenarios())

    stale = copy.deepcopy(BASE)                       # what the browser still holds
    stale["data"]["scenarios"][0]["triggers"][0]["vertices"][0] = [-0.95, -0.95]
    stale["data"]["scenarios"][2]["triggers"][0]["vertices"][0] = [0.95, 0.95]
    a.client.cfg = stale                              # the browser's Save

    try:
        a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=expect)
    except vendor_adapter.ConcurrentEditError as e:
        assert "Zone1" in str(e) and "Zone3" in str(e), str(e)
        assert a.client.writes == [], "a refused push must not write"
        return "refused, named both reverted zones, wrote nothing"
    raise AssertionError("push was allowed on top of another editor's write")


def test_the_edited_rule_changing_underneath_also_blocks():
    """If the zone you are editing moved on the camera, the geometry on screen was
    drawn over a view that no longer exists -- pushing it discards their change."""
    a = _adapter(BASE)
    expect = vendor_adapter.fingerprint(a.read_scenarios())
    a.client.cfg["data"]["scenarios"][1]["triggers"][0]["vertices"][0] = [-0.95, -0.95]
    try:
        a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=expect)
    except vendor_adapter.ConcurrentEditError as e:
        assert "Zone2" in str(e), str(e)
        return "refused when the edited rule itself drifted"
    raise AssertionError("push was allowed over a change to the rule being edited")


def test_without_a_baseline_the_push_is_unguarded():
    """expect=None is the pre-existing behaviour, kept for callers with nothing read."""
    a = _adapter(BASE)
    a.client.cfg["data"]["scenarios"][0]["triggers"][0]["vertices"][0] = [-0.95, -0.95]
    a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=None)
    assert len(a.client.writes) == 1
    return "no baseline, no refusal"


def test_re_reading_after_a_push_clears_the_drift():
    """What the GUI does after every successful push. Without it the tool's OWN write
    looks like another editor's and the operator's next push is refused."""
    a = _adapter(BASE)
    expect = vendor_adapter.fingerprint(a.read_scenarios())
    a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=expect)

    try:
        a.apply_scenario(_edit(3, NEW_POINTS), ".", expect=expect)   # stale baseline
        raise AssertionError("the tool's own write should look like drift to a stale baseline")
    except vendor_adapter.ConcurrentEditError:
        pass

    refreshed = vendor_adapter.fingerprint(a.read_scenarios())       # the re-read
    a.apply_scenario(_edit(3, NEW_POINTS), ".", expect=refreshed)
    assert len(a.client.writes) == 2, a.client.writes
    return "consecutive pushes work once the baseline is re-read"


def test_the_guard_adds_no_round_trip_to_an_edit():
    """apply_scenario already read the config to find the scenario being edited; the
    guard reuses that read. A check that doubled the round trips on every push would
    get switched off."""
    counts = {}
    for label, expect_fn in (("guarded", lambda a: vendor_adapter.fingerprint(a.read_scenarios())),
                             ("plain", lambda a: None)):
        a = _adapter(BASE)
        expect = expect_fn(a)
        reads = []
        real = a.client.get_config
        a.client.get_config = lambda: (reads.append(1), real())[1]
        a.apply_scenario(_edit(2, NEW_POINTS), ".", expect=expect)
        counts[label] = len(reads)
    assert counts["guarded"] == counts["plain"], counts
    return f"{counts['guarded']} config reads either way"


# --------------------------------------------------------------------------- #
# exclusion zones
#
# Reported from the field as "Push failed: at most 5 exclusion zones per scenario"
# on a scenario that had nowhere near five. Editing an existing scenario applied the
# drawn exclusion zones TWICE: update_scenario_geometry replaces the scenario's
# excludeArea filters with them, and apply_scenario then appended the same list
# again. Under three zones it did not error -- it just silently doubled them onto
# the camera -- so the visible symptom only appeared at the cap.

def _square(offset):
    return [(offset, offset), (offset, offset + 0.05), (offset + 0.05, offset + 0.05)]


def _excl_count(client, sid=1):
    scenario = next(s for s in client.cfg["data"]["scenarios"] if s["id"] == sid)
    return len([f for f in scenario.get("filters", []) if f.get("type") == "excludeArea"])


def test_editing_writes_each_exclusion_zone_exactly_once():
    """THE REGRESSION. Two drawn zones must reach the camera as two, not four."""
    for n in range(0, 6):
        a = _adapter(BASE)
        sc = Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS, classes=("human",),
                      native_id=1, exclusions=[_square(0.1 + i * 0.1) for i in range(n)])
        a.apply_scenario(sc, ".")
        assert _excl_count(a.client) == n, f"drew {n}, camera got {_excl_count(a.client)}"
    return "0-5 zones round-trip exactly on an edit"


def test_three_exclusion_zones_no_longer_trip_the_cap_of_five():
    """The exact failure reported: three drawn zones doubled to six and AOA's cap
    rejected the whole push."""
    a = _adapter(BASE)
    sc = Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS, classes=("human",),
                  native_id=1, exclusions=[_square(0.1), _square(0.3), _square(0.5)])
    a.apply_scenario(sc, ".")
    assert _excl_count(a.client) == 3
    return "three zones push cleanly"


def test_creating_still_gets_its_exclusion_zones():
    """The append is create-only now, so the create path must still carry them --
    build_intrusion does not add exclusions itself."""
    a = _adapter(_config())
    sc = Scenario(name="NewZone", kind="intrusion", points=NEW_POINTS, classes=("human",),
                  native_id=None, exclusions=[_square(0.1), _square(0.3)])
    a.apply_scenario(sc, ".")
    assert _excl_count(a.client) == 2, _excl_count(a.client)
    return "a new scenario keeps both drawn zones"


def test_clearing_the_exclusions_removes_them_from_the_camera():
    """Editing down to zero must delete the old filters, not leave them behind."""
    a = _adapter(BASE)
    first = Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS, classes=("human",),
                     native_id=1, exclusions=[_square(0.1), _square(0.3)])
    a.apply_scenario(first, ".")
    assert _excl_count(a.client) == 2
    cleared = Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS, classes=("human",),
                       native_id=1, exclusions=[])
    a.apply_scenario(cleared, ".")
    assert _excl_count(a.client) == 0, _excl_count(a.client)
    return "exclusions cleared on the camera too"


def test_repeated_edits_do_not_accumulate_exclusions():
    """Five pushes of the same two zones must leave two, not ten."""
    a = _adapter(BASE)
    for _ in range(5):
        a.apply_scenario(Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS,
                                  classes=("human",), native_id=1,
                                  exclusions=[_square(0.1), _square(0.3)]), ".")
    assert _excl_count(a.client) == 2, _excl_count(a.client)
    return "stable across repeated pushes"


def test_non_exclusion_filters_survive_an_exclusion_edit():
    """A size filter set in the camera's web UI must not be swept away by an edit
    that only touches exclusion zones."""
    cfg = copy.deepcopy(BASE)
    cfg["data"]["scenarios"][0]["filters"] = [{"type": "sizePercentage", "width": 5, "height": 5}]
    a = _adapter(cfg)
    a.apply_scenario(Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS,
                              classes=("human",), native_id=1,
                              exclusions=[_square(0.1)]), ".")
    kept = [f for f in a.client.cfg["data"]["scenarios"][0]["filters"]
            if f["type"] == "sizePercentage"]
    assert len(kept) == 1 and _excl_count(a.client) == 1
    return "sizePercentage preserved alongside the new exclusion"


# --------------------------------------------------------------------------- #
# object-size filters
#
# AOA has no positioned size box. getConfigurationCapabilities (aoa_probes/) declares
# only sizePercentage (percent of frame, 3..100) and sizePerspective (real-world cm,
# 10..9999) -- each a MINIMUM width/height pair, with no position and no maximum.
# The reader used to fabricate a rectangle at a fixed bottom-left spot from the first
# of those, which the writer then loaded into its size editor as a draggable "min"
# box: an invented location, an implied max that does not exist, and a control no
# push could write, since Axis capabilities set size_boxes=False. AOA's real spatial
# calibration is the perspective height bars.

def _with_filters(*filters):
    cfg = _config(_scenario(1, "Zone1", -0.2))
    cfg["data"]["scenarios"][0]["filters"] = list(filters)
    return cfg


def test_axis_never_reports_a_positioned_size_box():
    """THE REGRESSION. No AOA size filter may become a min_size/max_size rect."""
    for f in ({"type": "sizePercentage", "width": 3, "height": 3},
              {"type": "sizePerspective", "width": 75, "height": 75}):
        sc = _adapter(_with_filters(f)).read_scenarios()[0]
        assert sc.min_size is None and sc.max_size is None, f"{f['type']} -> {sc.min_size}"
    return "sizePercentage and sizePerspective both produce no box"


def test_size_percentage_is_reported_as_text():
    sc = _adapter(_with_filters({"type": "sizePercentage", "width": 3, "height": 5})).read_scenarios()[0]
    assert sc.size_text == "min object 3%x5% of frame", sc.size_text
    return sc.size_text


def test_size_perspective_is_reported_at_all():
    """It was skipped entirely before -- and it is the common one in the field
    (1346 occurrences across local backups, against 24 for sizePercentage)."""
    sc = _adapter(_with_filters({"type": "sizePerspective", "width": 75, "height": 75})).read_scenarios()[0]
    assert sc.size_text == "min object 75x75cm (perspective bars)", sc.size_text
    return sc.size_text


def test_a_duplicated_size_filter_is_counted_not_repeated():
    """Real cameras carry the same sizePerspective twice; say so once, with a count."""
    f = {"type": "sizePerspective", "width": 75, "height": 75}
    sc = _adapter(_with_filters(f, dict(f))).read_scenarios()[0]
    assert sc.size_text == "min object 75x75cm (perspective bars) x2", sc.size_text
    return sc.size_text


def test_a_scenario_with_no_size_filter_says_nothing():
    sc = _adapter(_with_filters({"type": "timeShortLivedLimit", "time": 2})).read_scenarios()[0]
    assert sc.size_text == "" and sc.min_size is None
    return "no filter, no note"


def test_perspective_bars_are_still_read():
    """The bars ARE the Axis perspective mechanism and must survive this change."""
    cfg = _config(_scenario(1, "Zone1", -0.2))
    cfg["data"]["perspectives"] = [{"id": 3, "bars": [
        {"height": 183, "points": [[-0.5, -0.5], [-0.5, 0.0]]},
        {"height": 183, "points": [[0.5, -0.2], [0.5, 0.3]]}]}]
    cfg["data"]["scenarios"][0]["perspectives"] = [3]
    sc = _adapter(cfg).read_scenarios()[0]
    assert len(sc.perspective) == 2 and sc.perspective[0]["height"] == 183, sc.perspective
    return "2 calibration bars at 183cm"


def test_size_filters_survive_an_edit_untouched():
    """The writer does not manage these filters, so it must neither drop nor
    duplicate them when pushing new geometry."""
    f = {"type": "sizePerspective", "width": 75, "height": 75}
    a = _adapter(_with_filters(f, {"type": "timeShortLivedLimit", "time": 2}))
    a.apply_scenario(Scenario(name="Zone1", kind="intrusion", points=NEW_POINTS,
                              classes=("human",), native_id=1), ".")
    kept = a.client.cfg["data"]["scenarios"][0]["filters"]
    assert sum(1 for x in kept if x["type"] == "sizePerspective") == 1, kept
    assert sum(1 for x in kept if x["type"] == "timeShortLivedLimit") == 1, kept
    return "size + short-lived filters preserved exactly once"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nconcurrent-editor guard -- {len(tests)} tests\n" + "=" * (width + 58))
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
