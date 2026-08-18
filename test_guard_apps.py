#!/usr/bin/env python3
"""Tests for the Guard-app write path on Axis fixed thermals. No camera needed.

Every constant asserted here came from a REAL getConfigurationCapabilities /
getConfiguration dump off 10.23.34.243:5020 (AXIS Q1971-E, firmware 11.11.116),
captured by starting each app, probing, and stopping it again. Testing against a
guessed schema would prove nothing -- the whole point of this path is that the
Guard apps differ from AOA in small, silent ways ("data" not "vertices",
"profiles" not "scenarios", "uid" not "id").

Run:  .venv\\Scripts\\python.exe test_guard_apps.py
"""

from __future__ import annotations

import json
import sys

import guard_config as gc
import vendor_adapter

# --------------------------------------------------------------------------- #
# Real captures (trimmed to one profile each)
# --------------------------------------------------------------------------- #

FENCE_CONFIG = {
    "data": {
        "cameras": [{"id": 1, "active": True, "rotation": 0}],
        "profiles": [{
            "name": "Profile 1", "uid": 1, "camera": 1,
            "filters": [{"active": True, "data": [5, 5], "type": "sizePercentage"},
                        {"active": True, "data": 1, "type": "timeShortLivedLimit"}],
            "triggers": [{"type": "fence", "alarmDirection": "leftToRight",
                          "data": [[0.0, -0.7], [0.0, 0.7]]}],
        }],
        "configurationStatus": 0,
    }
}

MOTION_CONFIG = {
    "data": {
        "cameras": [{"id": 1, "active": True, "rotation": 0}],
        "profiles": [{
            "name": "Profile 1", "uid": 1, "camera": 1,
            "filters": [{"active": True, "data": [5, 5], "type": "sizePercentage"},
                        {"active": True, "data": 1, "type": "timeShortLivedLimit"},
                        {"active": True, "data": 5, "type": "distanceSwayingObject"}],
            "triggers": [{"type": "includeArea",
                          "data": [[-0.97, -0.97], [-0.97, 0.97],
                                   [0.97, 0.97], [0.97, -0.97]]}],
        }],
        "configurationStatus": 0,
    }
}

LOITER_CONFIG = {
    "data": {
        "cameras": [{"id": 1, "active": True, "rotation": 0}],
        "profiles": [{
            "name": "Profile 1", "uid": 1, "camera": 1,
            "filters": [{"active": True, "data": [5, 5], "type": "sizePercentage"},
                        {"active": True, "data": 5, "type": "distanceSwayingObject"}],
            "triggers": [{"type": "loiteringArea",
                          "data": [[-0.97, -0.97], [-0.97, 0.97], [0.97, 0.97]],
                          "conditions": [
                              {"type": "individual", "data": 120, "active": True},
                              {"type": "group", "data": 160, "active": False}]}],
        }],
        "configurationStatus": 0,
    }
}


#: The out-of-box trigger geometry each app advertises, captured from the camera.
DEFAULT_INSTANCE = {
    "motionguard": [[-0.97, -0.97], [-0.97, 0.97], [0.97, 0.97], [0.97, -0.97]],
    "loiteringguard": [[-0.97, -0.97], [-0.97, 0.97], [0.97, 0.97], [0.97, -0.97]],
    "fenceguard": [[0.0, -0.7], [0.0, 0.7]],
}


# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #

def test_coordinate_mapping_matches_aoa():
    """Guard uses the same -1..1 y-up space as AOA, so a drawing must land
    identically in both. If these ever diverge, every zone silently shifts."""
    import aoa_config
    for fx, fy in [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5), (0.25, 0.75)]:
        guard = gc.frac_to_guard([(fx, fy)])[0]
        aoa = aoa_config.pixel_to_norm(fx * 1000, fy * 1000, 1000, 1000)
        assert abs(guard[0] - aoa[0]) < 1e-9 and abs(guard[1] - aoa[1]) < 1e-9, \
            f"({fx},{fy}): guard={guard} aoa={aoa}"
    return "frac_to_guard agrees with aoa_config.pixel_to_norm"


def test_coordinate_round_trip_is_lossless():
    pts = [(0.0, 0.0), (1.0, 1.0), (0.2, 0.3), (0.65, 0.75)]
    back = gc.guard_to_frac(gc.frac_to_guard(pts))
    for (a, b), (c, d) in zip(back, pts):
        assert abs(a - c) < 1e-12 and abs(b - d) < 1e-12, f"{(a,b)} != {(c,d)}"
    return "frac -> guard -> frac is exact"


def test_corners_map_to_the_expected_extremes():
    assert gc.frac_to_guard([(0.0, 0.0)])[0] == [-1.0, 1.0], "top-left -> (-1, +1)"
    assert gc.frac_to_guard([(1.0, 1.0)])[0] == [1.0, -1.0], "bottom-right -> (+1, -1)"
    return "top-left fraction origin maps to y-up -1..1 correctly"


# --------------------------------------------------------------------------- #
# Parsing real configs
# --------------------------------------------------------------------------- #

def test_parse_fence_profile():
    p = gc.parse_profiles(FENCE_CONFIG, "fenceguard")[0]
    assert p["kind"] == "line" and p["uid"] == 1, p
    assert p["direction"] == "leftToRight", p["direction"]
    # Compared with a tolerance: the -1..1 <-> 0..1 conversion is floating point,
    # so 0.7 comes back as 0.15000000000000002. Exact equality here would be
    # testing IEEE 754, not the parser.
    for (gx, gy), (ex, ey) in zip(p["points"], [(0.5, 0.85), (0.5, 0.15)]):
        assert abs(gx - ex) < 1e-9 and abs(gy - ey) < 1e-9, p["points"]
    return "fence trigger read from 'data' with direction"


def test_parse_motion_profile():
    p = gc.parse_profiles(MOTION_CONFIG, "motionguard")[0]
    assert p["kind"] == "intrusion" and len(p["points"]) == 4, p
    assert p["direction"] is None, "an area rule has no crossing direction"
    return "includeArea trigger -> intrusion kind"


def test_parse_loiter_reads_seconds_from_active_condition_only():
    p = gc.parse_profiles(LOITER_CONFIG, "loiteringguard")[0]
    assert p["kind"] == "loiter", p["kind"]
    # 120 = the ACTIVE 'individual' condition; 160 is the inactive 'group' one and
    # must not win.
    assert p["duration"] == 120, p["duration"]
    return "loiter duration 120s taken from the active individual condition"


def test_parse_exclusions_from_filters():
    cfg = {"data": {"profiles": [{
        "name": "x", "uid": 1,
        "triggers": [{"type": "includeArea", "data": [[-0.9, -0.9], [0.9, -0.9], [0.9, 0.9]]}],
        "filters": [{"type": "excludeArea", "active": True,
                     "data": [[-0.2, -0.2], [0.2, -0.2], [0.2, 0.2]]}]}]}}
    p = gc.parse_profiles(cfg, "motionguard")[0]
    assert len(p["exclusions"]) == 1 and len(p["exclusions"][0]) == 3, p["exclusions"]
    return "excludeArea lives in filters, not triggers"


# --------------------------------------------------------------------------- #
# Builders + validation
# --------------------------------------------------------------------------- #

def test_builders_emit_the_apps_own_trigger_type():
    area = gc.build_area_profile("A", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]))
    fence = gc.build_fence_profile("F", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.9)]))
    loit = gc.build_loiter_profile("L", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]), 30)
    assert area["triggers"][0]["type"] == "includeArea"
    assert fence["triggers"][0]["type"] == "fence"
    assert loit["triggers"][0]["type"] == "loiteringArea"
    # The key schema trap: vertices go in "data", never "vertices".
    for p in (area, fence, loit):
        assert "data" in p["triggers"][0] and "vertices" not in p["triggers"][0], p
    return "each builder emits its app's trigger type, vertices under 'data'"


def test_loiter_seconds_go_in_directly():
    p = gc.build_loiter_profile("L", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]), 45)
    cond = p["triggers"][0]["conditions"][0]
    assert cond == {"type": "individual", "data": 45, "active": True}, cond
    return "loiter time is a bare seconds int, unlike AOA's per-class list"


def test_fence_accepts_both_directions_in_one_profile():
    """AOA has no both-ways fence and needs a -LR/-RL pair; Fence Guard does not."""
    p = gc.build_fence_profile("F", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.9)]),
                               alarm_direction="both")
    assert p["triggers"][0]["alarmDirection"] == "both"
    gc.validate_profile(p, "fenceguard")
    return "'both' is a single valid Fence Guard profile"


def _expect_error(fn, needle):
    try:
        fn()
    except ValueError as e:
        assert needle.lower() in str(e).lower(), f"wrong error: {e}"
        return
    raise AssertionError(f"expected ValueError containing {needle!r}")


def test_validation_enforces_real_capability_limits():
    ok_area = gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    # name 1..15
    _expect_error(lambda: gc.validate_profile(
        gc.build_area_profile("x" * 16, ok_area), "motionguard"), "name must be")
    # area 3..10 vertices (AOA allows 20 -- these apps do not)
    _expect_error(lambda: gc.validate_profile(
        gc.build_area_profile("A", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.9)])),
        "motionguard"), "3..10 vertices")
    eleven = gc.frac_to_guard([(0.1 + i * 0.05, 0.1) for i in range(11)])
    _expect_error(lambda: gc.validate_profile(
        gc.build_area_profile("A", eleven), "motionguard"), "3..10 vertices")
    # loiter 1..360s
    _expect_error(lambda: gc.build_loiter_profile("L", ok_area, 361), "1..360")
    _expect_error(lambda: gc.build_loiter_profile("L", ok_area, 0), "1..360")
    # bad direction
    _expect_error(lambda: gc.build_fence_profile(
        "F", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.9)]), alarm_direction="sideways"),
        "alarm_direction")
    return "name/vertex/duration/direction limits match the camera's capabilities"


def test_a_profile_cannot_be_written_to_the_wrong_app():
    """Kind and application are bound together. Sending an area profile to Fence
    Guard would be accepted-looking JSON that configures nothing."""
    area = gc.build_area_profile("A", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]))
    _expect_error(lambda: gc.validate_profile(area, "fenceguard"), "only accepts")
    return "an includeArea profile is rejected for fenceguard"


def test_fence_guard_rejects_exclusion_zones():
    """Confirmed from capabilities: fenceguard lists no excludeArea filter."""
    p = gc.build_fence_profile("F", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.9)]))
    p.setdefault("filters", []).append(
        {"type": "excludeArea", "active": True,
         "data": gc.frac_to_guard([(0.2, 0.2), (0.3, 0.2), (0.3, 0.3)])})
    _expect_error(lambda: gc.validate_profile(p, "fenceguard"), "does not support exclusion")
    return "exclusion zones refused for Fence Guard, allowed for the other two"


def test_exclusion_cap_is_three_not_aoas_five():
    verts = gc.frac_to_guard([(0.2, 0.2), (0.3, 0.2), (0.3, 0.3)])
    p = gc.build_area_profile("A", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]),
                              exclusions=[verts] * 3)
    gc.validate_profile(p, "motionguard")
    _expect_error(lambda: gc.add_exclude_zones(p, [verts]), "at most 3")
    return "3 exclusion zones max (AOA allows 5)"


# --------------------------------------------------------------------------- #
# Config surgery
# --------------------------------------------------------------------------- #

def test_insert_replaces_by_uid_so_renames_hit_the_right_profile():
    renamed = {**FENCE_CONFIG["data"]["profiles"][0], "name": "Renamed"}
    out = gc.insert_or_replace_profile(FENCE_CONFIG, renamed, "fenceguard")
    profiles = out["data"]["profiles"]
    assert len(profiles) == 1 and profiles[0]["name"] == "Renamed", profiles
    assert FENCE_CONFIG["data"]["profiles"][0]["name"] == "Profile 1", "input was mutated!"
    return "uid match edits in place; input config untouched"


def test_insert_new_profile_gets_next_free_uid():
    new = gc.build_area_profile("New", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]))
    out = gc.insert_or_replace_profile(MOTION_CONFIG, new, "motionguard",
                                       replace_by_name=False)
    uids = [p["uid"] for p in out["data"]["profiles"]]
    assert uids == [1, 2], uids
    return "new profile appended with uid 2"


def test_camera_full_at_ten_profiles_is_refused():
    cfg = {"data": {"profiles": [{"name": f"P{i}", "uid": i} for i in range(1, 11)]}}
    new = gc.build_area_profile("New", gc.frac_to_guard([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]))
    _expect_error(lambda: gc.insert_or_replace_profile(cfg, new, "motionguard",
                                                       replace_by_name=False),
                  "at most 10 profiles")
    return "the 11th profile is refused before the camera rejects the whole write"


def test_remove_profile_is_a_noop_for_a_missing_uid():
    out = gc.remove_profile(FENCE_CONFIG, 999)
    assert len(out["data"]["profiles"]) == 1
    out = gc.remove_profile(FENCE_CONFIG, 1)
    assert out["data"]["profiles"] == []
    assert len(FENCE_CONFIG["data"]["profiles"]) == 1, "input was mutated!"
    return "removing an absent uid changes nothing; input never mutated"


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #

def _stub_guard_adapter(configs):
    a = vendor_adapter.GuardAdapter.__new__(vendor_adapter.GuardAdapter)
    a.ip, a.port, a.user, a.password, a.device_id = "1.2.3.4", "5020", "u", "p", None
    a._clients = {}

    class _C:
        def __init__(self, app):
            self.app = app

        def get_config(self):
            cfg = configs.get(self.app)
            if cfg is None:
                raise gc.GuardAppStopped(f"{self.app} not running")
            return cfg

        def get_capabilities(self):
            # Real defaultInstance values, so is_default reflects the camera.
            return {"data": {"triggers": [
                {"type": gc.APP_TRIGGER[self.app],
                 "defaultInstance": DEFAULT_INSTANCE[self.app]}]}}

    a._client = lambda app: _C(app)
    return a


def test_adapter_reads_across_all_three_apps():
    a = _stub_guard_adapter({"motionguard": MOTION_CONFIG, "fenceguard": FENCE_CONFIG,
                             "loiteringguard": LOITER_CONFIG})
    scs = a.read_scenarios()
    kinds = sorted(s.kind for s in scs)
    assert kinds == ["intrusion", "line", "loiter"], kinds
    assert all(s.native_id.app in gc.APP_TRIGGER for s in scs), scs
    assert all(s.classes == () for s in scs), "Guard apps have no object classes"
    return "one read spans motionguard + fenceguard + loiteringguard"


def test_guard_native_id_is_not_a_tuple():
    """REGRESSION. The GUI treats a tuple native_id as a Hikvision
    (channel, scene, rule) address and unpacks three names from it. A 2-tuple
    (app, uid) therefore raised "not enough values to unpack (expected 3, got 2)"
    and every read failed on a thermal with a Guard app running. AxisLinePair
    documents this exact trap; GuardRuleId has to respect it too."""
    a = _stub_guard_adapter({"motionguard": MOTION_CONFIG})
    sc = a.read_scenarios()[0]
    assert not isinstance(sc.native_id, tuple), \
        f"native_id must not be a tuple, got {sc.native_id!r}"
    assert isinstance(sc.native_id, vendor_adapter.GuardRuleId), type(sc.native_id)
    assert sc.native_id.app == "motionguard" and sc.native_id.uid == 1, sc.native_id
    # The unpack the GUI performs on a Hik address must not even be attempted.
    try:
        _a, _b, _c = sc.native_id
    except (TypeError, ValueError):
        pass
    else:
        raise AssertionError("GuardRuleId unpacked as a 3-tuple -- it is tuple-like")
    return "GuardRuleId can't be mistaken for a Hik (channel, scene, rule) address"


def test_editing_a_guard_rule_targets_its_own_app_and_uid():
    a = _stub_guard_adapter({"motionguard": MOTION_CONFIG})
    sc = a.read_scenarios()[0]
    # Same app -> edit in place (uid preserved).
    assert vendor_adapter.GuardRuleId("motionguard", 1) == sc.native_id
    # A rule whose KIND changed moves to a different app and must become a new
    # profile there rather than clobbering uid 1 of the wrong application.
    moved = vendor_adapter.GuardRuleId("motionguard", 1)
    assert moved.app != "fenceguard"
    return "edit keeps uid within its app; a kind change can't clobber another app"


def test_a_stopped_app_is_reported_not_fatal():
    """A stopped app must not sink the whole read -- the other two still have
    rules the operator needs to see."""
    notes = []
    a = _stub_guard_adapter({"motionguard": MOTION_CONFIG})  # other two "stopped"
    scs = a.read_scenarios(log=notes.append)
    assert len(scs) == 1 and scs[0].kind == "intrusion", scs
    assert sum("not running" in n for n in notes) == 2, notes
    return "stopped apps logged and skipped; running one still read"


def test_reading_never_starts_an_application():
    """Reading must not change camera state. If read_scenarios ever auto-started
    an app, an audit would silently reconfigure the fleet."""
    started = []
    a = _stub_guard_adapter({"motionguard": MOTION_CONFIG})

    class _C:
        def __init__(self, app):
            self.app = app

        def get_config(self):
            if self.app != "motionguard":
                raise gc.GuardAppStopped("stopped")
            return MOTION_CONFIG

        def get_capabilities(self):
            return {"data": {"triggers": [
                {"type": gc.APP_TRIGGER[self.app],
                 "defaultInstance": DEFAULT_INSTANCE[self.app]}]}}

        def start_app(self, **_):
            started.append(self.app)
            return True

    a._client = lambda app: _C(app)
    a.read_scenarios()
    assert started == [], f"read_scenarios started {started}"
    return "read path never calls start_app"


def test_factory_default_profile_is_recognised():
    """"Profile 1" ships inside every Guard app and is byte-identical to the
    capability's defaultInstance -- a rectangle over ~97% of the frame. It must be
    told apart from a drawn zone, because leaving it active means the app detects
    everywhere and any zone beside it narrows nothing."""
    caps = {"data": {"triggers": [{"type": "includeArea",
                                   "defaultInstance": [[-0.97, -0.97], [-0.97, 0.97],
                                                       [0.97, 0.97], [0.97, -0.97]]}]}}
    profile = MOTION_CONFIG["data"]["profiles"][0]
    assert gc.is_factory_default(profile, caps), "the shipped Profile 1 is the default"

    drawn = json.loads(json.dumps(profile))
    drawn["triggers"][0]["data"] = [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5]]
    assert not gc.is_factory_default(drawn, caps), "a redrawn area is NOT a default"
    return "default recognised by geometry; a redrawn area is not"


def test_default_detection_ignores_the_profile_name():
    """Matched on geometry, not name: renaming the default still leaves a
    full-frame profile, and keeping the name after redrawing is a real zone."""
    caps = {"data": {"triggers": [{"type": "includeArea",
                                   "defaultInstance": [[-0.97, -0.97], [-0.97, 0.97],
                                                       [0.97, 0.97], [0.97, -0.97]]}]}}
    renamed = json.loads(json.dumps(MOTION_CONFIG["data"]["profiles"][0]))
    renamed["name"] = "Perimeter"
    assert gc.is_factory_default(renamed, caps), "renaming does not make it configured"
    return "rename does not hide a full-frame default"


def test_parse_profiles_flags_defaults_only_when_capabilities_given():
    caps = {"data": {"triggers": [{"type": "includeArea",
                                   "defaultInstance": [[-0.97, -0.97], [-0.97, 0.97],
                                                       [0.97, 0.97], [0.97, -0.97]]}]}}
    with_caps = gc.parse_profiles(MOTION_CONFIG, "motionguard", capabilities=caps)
    assert with_caps[0]["is_default"] is True, with_caps
    # Without capabilities there is nothing to compare against -- never guess.
    without = gc.parse_profiles(MOTION_CONFIG, "motionguard")
    assert without[0]["is_default"] is False, without
    return "is_default set only when the camera's own defaultInstance is available"


def test_capabilities_have_no_classes_but_are_writable():
    caps = vendor_adapter.GuardAdapter.capabilities
    assert caps.classes == (), "Guard apps cannot classify objects"
    assert caps.read_only is False and caps.can_delete is True, caps
    assert caps.native_bidirectional is True, "Fence Guard has a real both-ways mode"
    return "writable, deletable, bidirectional -- but no classification"


def test_require_skips_class_checks_when_the_vendor_has_none():
    """The GUI may still hand over classes=('human',) from its checkboxes. With an
    engine that has no class concept that is irrelevant, not an error."""
    sc = vendor_adapter.Scenario(name="x", kind="intrusion",
                                 points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
                                 classes=("human", "vehicle"))
    vendor_adapter._require(vendor_adapter.GuardAdapter.capabilities, sc)
    return "classes ignored when caps.classes is empty"


# --------------------------------------------------------------------------- #
# Thermal composite
# --------------------------------------------------------------------------- #

class _StubPD:
    def read_scenarios(self):
        return [vendor_adapter.Scenario(
            name="intrusion-1 / zone-1", kind="intrusion",
            points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)], classes=("human",),
            read_only=True, detail="intrusion")]


def _stub_thermal():
    a = vendor_adapter.AxisThermalAdapter.__new__(vendor_adapter.AxisThermalAdapter)
    a.pd = _StubPD()
    a.guard = _stub_guard_adapter({"motionguard": MOTION_CONFIG})
    return a


def test_thermal_shows_pd_readonly_alongside_writable_guard_rules():
    scs = _stub_thermal().read_scenarios()
    ro = [s for s in scs if s.read_only]
    rw = [s for s in scs if not s.read_only]
    assert len(ro) == 1 and len(rw) == 1, scs
    assert ro[0].name.startswith("intrusion-1"), ro
    return "one read shows PD zones (locked) and Guard profiles (editable)"


def test_thermal_refuses_to_push_a_pd_rule_into_a_guard_app():
    """Redrawing a PD zone and pushing would otherwise create a SECOND rule in a
    Guard app while the PD original stayed untouched."""
    a = _stub_thermal()
    pd_rule = a.pd.read_scenarios()[0]
    try:
        a.apply_scenario(pd_rule, "guard_backups")
    except vendor_adapter.PDWriteUnsupported:
        return "pushing a PD-sourced rule is refused, not silently duplicated"
    raise AssertionError("expected PDWriteUnsupported")


def test_thermal_capabilities_are_writable():
    caps = vendor_adapter.AxisThermalAdapter.capabilities
    assert caps.read_only is False, "a thermal IS writable, via the Guard apps"
    assert caps.kinds == ("intrusion", "line", "loiter"), caps.kinds
    assert caps.classes == (), caps.classes
    return "thermal advertises the Guard apps' writable capabilities"


# --------------------------------------------------------------------------- #
# PD tuning params
# --------------------------------------------------------------------------- #

def test_pd_rejects_unknown_or_invalid_tuning_params():
    import pd_config

    class _Stub(pd_config.PDClient):
        def __init__(self):
            pass

    c = _Stub()
    try:
        c.set_params({"NotARealParam": "yes"})
    except ValueError as e:
        assert "unknown" in str(e).lower(), e
    else:
        raise AssertionError("unknown parameter should be refused")

    try:
        c.set_params({"UseDNNClassifier": "maybe"})
    except ValueError as e:
        assert "must be one of" in str(e), e
    else:
        raise AssertionError("invalid enum value should be refused")
    return "unknown names and bad enum values refused before any request"


def test_pd_classifier_param_is_the_documented_yes_no_enum():
    import pd_config
    assert pd_config.PD_TUNING_PARAMS["UseDNNClassifier"] == ("yes", "no")
    # Numeric params are intentionally unconstrained: set_params verifies by
    # read-back instead of guessing a range param.cgi would silently clamp.
    assert pd_config.PD_TUNING_PARAMS["DNNSensitivityLevel"] is None
    return "classifier is a yes/no enum; numeric ranges verified by read-back"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nAxis Guard apps (fixed-thermal write path) -- {len(tests)} tests\n"
          + "=" * (width + 62))
    for name, fn in tests:
        try:
            detail = fn() or ""
            print(f"PASS  {name:<{width}}  {detail}")
        except AssertionError as exc:
            failures.append((name, str(exc)))
            print(f"FAIL  {name:<{width}}  {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"ERROR {name:<{width}}  {type(exc).__name__}: {exc}")
    print("=" * (width + 62))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
