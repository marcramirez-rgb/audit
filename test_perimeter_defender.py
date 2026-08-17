#!/usr/bin/env python3
"""Tests for the Axis fixed-thermal (Perimeter Defender) path. No camera needed.

The fixtures are REAL captures from 10.23.34.243:5015, an AXIS Q1971-E thermal
running Perimeter Defender 3.7.0.7430 on firmware 11.11.116 -- not invented
shapes. That matters here: the whole reason this path exists is that PD's data
looks nothing like Object Analytics, so a test against a guessed payload would
prove nothing.

Run:  .venv\\Scripts\\python.exe test_perimeter_defender.py
"""

from __future__ import annotations

import sys

import camera_engine
import pd_config
import vendor_adapter

# --------------------------------------------------------------------------- #
# Real captures
# --------------------------------------------------------------------------- #

# One complete frame off /local/AXISPerimeterDefender/v2/metadata/liveStream.
# Note X2D=384 on point 12: PD emits a coordinate exactly ON the right edge of the
# 384-wide reference frame, which is why parse_zones clamps instead of trusting.
METADATA_FRAME = """<NODE NAME="ivs_results" REFERENTIAL="0" SITE="" OSD_SIZE="384x288" VERSION="6.0.0">
    <FRAMERATE> 8.93612 </FRAMERATE>
    <TIMESTAMP> 2026-08-17T13-48-10-041-004208674_XXXXXXXXXX </TIMESTAMP>
    <FRAME_NUMBER> 4208674 </FRAME_NUMBER>
    <STATUS> RUNNING </STATUS>
    <ACTOR_LIST>
    </ACTOR_LIST>
    <ALERT_LIST>
    </ALERT_LIST>
    <ALERT_ZONE_LIST>
        <ZONE NAME="zone-1">
            <POINT NUMBER="0" X2D="383" Y2D="287" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="1" X2D="114" Y2D="287" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="2" X2D="333" Y2D="152" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="3" X2D="175" Y2D="110" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="4" X2D="122" Y2D="97" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="5" X2D="15" Y2D="65" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="6" X2D="49" Y2D="63" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="7" X2D="168" Y2D="52" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="8" X2D="188" Y2D="38" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="9" X2D="327" Y2D="54" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="10" X2D="350" Y2D="55" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="11" X2D="362" Y2D="54" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
            <POINT NUMBER="12" X2D="384" Y2D="54" X3D="0.0" Y3D="0.0" Z3D="0.0"/>
        </ZONE>
    </ALERT_ZONE_LIST>
</NODE>"""

# GET /local/AXISPerimeterDefender/scenarios.xml on the same camera.
SCENARIOS_XML = """<?xml version="1.0" encoding="ASCII"?>
<SZE-scenarios Version="1.2.0.0">
\t<SZE-scenario name="intrusion-1" type="intrusion" analyticsMode="3D" uuid="e5ac9d6b-5d39-4f31-9ac1-5ab7f27ef0bd" trajectoryBacktraceDuration="-1">
\t\t<zone name="zone-1" min_duration="10000"/>
\t</SZE-scenario>
</SZE-scenarios>"""

# GET /axis-cgi/applications/list.cgi -- trimmed to the two entries that matter.
APP_LIST = """<reply result="ok">
 <application Name="fenceguard" NiceName="AXIS Fence Guard" Status="Stopped" />
 <application Name="AXISPerimeterDefender" NiceName="AXIS Perimeter Defender" Status="Running" License="Valid" />
</reply>"""


# --------------------------------------------------------------------------- #
# Zone geometry
# --------------------------------------------------------------------------- #

def test_zone_parses_every_vertex_in_order():
    zones = pd_config.parse_zones(METADATA_FRAME)
    assert len(zones) == 1, f"expected 1 zone, got {len(zones)}"
    z = zones[0]
    assert z["name"] == "zone-1", z["name"]
    assert len(z["points"]) == 13, f"expected 13 vertices, got {len(z['points'])}"
    # POINT NUMBER ordering must survive: a reordered polygon is a different shape.
    fx0, fy0 = z["points"][0]
    assert abs(fx0 - 383 / 384) < 1e-9 and abs(fy0 - 287 / 288) < 1e-9, z["points"][0]
    return f"13 vertices, first={z['points'][0][0]:.4f},{z['points'][0][1]:.4f}"


def test_points_are_top_left_fractions_in_unit_range():
    pts = pd_config.parse_zones(METADATA_FRAME)[0]["points"]
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in pts), pts
    # X2D=384 on a 384-wide frame is exactly 1.0 -- clamped, not overflowed.
    assert max(x for x, _ in pts) == 1.0, max(x for x, _ in pts)
    return "all vertices within [0,1]; edge point clamps to exactly 1.0"


def test_out_of_range_points_are_clamped_not_dropped():
    """A vertex outside the reference frame must still be a vertex. Dropping it
    silently would change the polygon's shape without telling anyone."""
    frame = METADATA_FRAME.replace('X2D="383" Y2D="287"', 'X2D="999" Y2D="-40"')
    pts = pd_config.parse_zones(frame)[0]["points"]
    assert len(pts) == 13, f"a clamped vertex was dropped: {len(pts)}"
    assert pts[0] == (1.0, 0.0), pts[0]
    return "out-of-frame vertex clamped to a corner, count preserved"


def test_missing_osd_size_falls_back_instead_of_dividing_by_zero():
    for attr in ['OSD_SIZE="384x288"', 'OSD_SIZE="0x0"', 'OSD_SIZE="garbage"']:
        frame = METADATA_FRAME.replace('OSD_SIZE="384x288"', attr)
        pts = pd_config.parse_zones(frame)[0]["points"]
        assert len(pts) == 13 and all(0 <= x <= 1 for x, _ in pts)
    frame = METADATA_FRAME.replace(' OSD_SIZE="384x288"', "")
    assert len(pd_config.parse_zones(frame)[0]["points"]) == 13
    return f"absent/zero/garbage OSD_SIZE -> {pd_config.DEFAULT_OSD_SIZE}"


def test_empty_zone_list_is_not_an_error():
    frame = METADATA_FRAME[:METADATA_FRAME.index("<ALERT_ZONE_LIST>")] + \
        "<ALERT_ZONE_LIST></ALERT_ZONE_LIST></NODE>"
    assert pd_config.parse_zones(frame) == []
    return "a configured-but-empty camera reads as zero zones, not a failure"


def test_unparseable_frame_raises():
    try:
        pd_config.parse_zones("<NODE><ALERT_ZONE")
    except pd_config.PDError:
        return "malformed XML raises PDError"
    raise AssertionError("expected PDError on malformed XML")


# --------------------------------------------------------------------------- #
# scenarios.xml + merge
# --------------------------------------------------------------------------- #

def test_scenarios_xml_gives_name_type_and_dwell():
    scs = pd_config.parse_scenarios_xml(SCENARIOS_XML)
    assert len(scs) == 1, scs
    s = scs[0]
    assert s["name"] == "intrusion-1" and s["type"] == "intrusion", s
    assert s["kind"] == "intrusion", s["kind"]
    assert s["zones"] == ["zone-1"], s["zones"]
    # PD stores milliseconds; the writer's neutral Scenario.duration is seconds.
    assert s["duration"] == 10, s["duration"]
    return "min_duration=10000ms -> 10s"


def test_loitering_maps_to_the_writers_loiter_kind():
    xml = SCENARIOS_XML.replace('type="intrusion"', 'type="loitering"')
    assert pd_config.parse_scenarios_xml(xml)[0]["kind"] == "loiter"
    return "PD 'loitering' -> neutral 'loiter'"


def test_empty_scenarios_xml_is_empty_not_an_error():
    assert pd_config.parse_scenarios_xml("") == []
    assert pd_config.parse_scenarios_xml("   ") == []
    return "an unconfigured camera returns []"


def test_merge_joins_geometry_to_its_scenario():
    rules = pd_config.merge(pd_config.parse_zones(METADATA_FRAME),
                            pd_config.parse_scenarios_xml(SCENARIOS_XML))
    assert len(rules) == 1, rules
    r = rules[0]
    assert r["name"] == "intrusion-1 / zone-1", r["name"]
    assert r["duration"] == 10 and r["pd_type"] == "intrusion", r
    assert len(r["points"]) == 13, len(r["points"])
    return "zone geometry + scenario metadata joined by zone name"


def test_zone_with_no_scenario_is_still_reported():
    """PD keeps zones that no scenario references. They are on the camera, so the
    operator has to see them -- silently hiding one is how a mystery zone in the
    overlay becomes a support ticket."""
    rules = pd_config.merge(pd_config.parse_zones(METADATA_FRAME), [])
    assert len(rules) == 1 and rules[0]["name"] == "zone-1", rules
    assert rules[0]["pd_type"] == "unassigned zone", rules[0]
    return "orphan zone surfaces rather than disappearing"


def test_merge_ignores_a_scenario_whose_zone_is_absent():
    scs = pd_config.parse_scenarios_xml(SCENARIOS_XML.replace('name="zone-1"', 'name="ghost"'))
    rules = pd_config.merge(pd_config.parse_zones(METADATA_FRAME), scs)
    # The real zone still comes back (unclaimed); the ghost contributes nothing.
    assert len(rules) == 1 and rules[0]["name"] == "zone-1", rules
    return "scenario referencing a missing zone can't fabricate geometry"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, *_a, **_kw):
        return self._resp


def test_detect_reports_only_running_apps():
    apps = pd_config.detect(_FakeSession(_FakeResp(APP_LIST)), "1.2.3.4", "5015", [None])
    assert apps == ["AXIS Perimeter Defender"], apps
    assert pd_config.is_perimeter_defender(apps)
    return "stopped Guard apps excluded; PD identified"


def test_is_perimeter_defender_is_false_without_it():
    assert not pd_config.is_perimeter_defender([])
    assert not pd_config.is_perimeter_defender(None)
    assert not pd_config.is_perimeter_defender(["AXIS Object Analytics"])
    return "AOA-only / empty / unreadable app lists don't trip PD mode"


def test_file_token_allowlist_is_enforced_before_any_request():
    for bad in ["context.knp", "context", "../../etc/passwd", ""]:
        try:
            pd_config._require_token(bad)
        except ValueError:
            continue
        raise AssertionError(f"token {bad!r} should have been rejected")
    for good in pd_config.FILE_TOKENS:
        pd_config._require_token(good)
    return f"only {sorted(pd_config.FILE_TOKENS)} accepted"


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #

class _StubPDClient:
    def get_zones(self):
        return pd_config.parse_zones(METADATA_FRAME)

    def get_scenarios_xml(self):
        return SCENARIOS_XML


def _stub_adapter():
    a = vendor_adapter.PerimeterDefenderAdapter.__new__(
        vendor_adapter.PerimeterDefenderAdapter)
    a.client, a.device_id = _StubPDClient(), None
    return a


def test_adapter_returns_read_only_scenarios():
    scs = _stub_adapter().read_scenarios()
    assert len(scs) == 1, scs
    s = scs[0]
    assert s.read_only is True, "PD scenarios must be flagged read-only"
    assert s.native_id is None, "no native id -- there is nothing to edit in place"
    assert s.kind == "intrusion" and s.duration == 10, (s.kind, s.duration)
    assert s.detail == "intrusion", s.detail
    assert len(s.points) == 13, len(s.points)
    return "scenario carries geometry, dwell and a read-only flag"


def test_adapter_survives_a_broken_scenarios_xml():
    """Losing the labels must not lose the zones -- geometry is the part an
    operator actually needs on screen."""
    a = _stub_adapter()
    a.client.get_scenarios_xml = lambda: "<SZE-scenarios><broken"
    scs = a.read_scenarios()
    assert len(scs) == 1 and len(scs[0].points) == 13, scs
    assert scs[0].name == "zone-1", scs[0].name
    return "geometry still read when scenarios.xml is garbage"


def test_adapter_refuses_to_write_with_a_reason():
    sc = vendor_adapter.Scenario(name="x", kind="intrusion", points=[(0, 0), (1, 0), (1, 1)])
    try:
        _stub_adapter().apply_scenario(sc, "pd_backups")
    except vendor_adapter.PDWriteUnsupported as e:
        assert "Perimeter Defender Setup" in str(e), str(e)
        return "push raises PDWriteUnsupported naming the supported alternative"
    raise AssertionError("expected PDWriteUnsupported")


def test_capabilities_advertise_read_only_with_no_writable_kinds():
    caps = vendor_adapter.PerimeterDefenderAdapter.capabilities
    assert caps.read_only is True and caps.kinds == (), caps
    assert caps.can_delete is False and caps.read_only_reason, caps
    # The other two vendors must stay writable -- this flag is opt-in.
    assert vendor_adapter.AxisAdapter.capabilities.read_only is False
    assert vendor_adapter.HikAdapter.capabilities.read_only is False
    return "read_only is set only for Perimeter Defender"


def test_require_blocks_a_write_against_read_only_capabilities():
    sc = vendor_adapter.Scenario(name="x", kind="intrusion", points=[(0, 0), (1, 0), (1, 1)])
    try:
        vendor_adapter._require(vendor_adapter.PerimeterDefenderAdapter.capabilities, sc)
    except ValueError:
        return "_require refuses read-only cameras before any request goes out"
    raise AssertionError("expected ValueError from _require")


# --------------------------------------------------------------------------- #
# Audit engine still agrees with the writer
# --------------------------------------------------------------------------- #

def test_audit_overlay_pixels_match_the_shared_parser():
    """camera_engine now delegates its PD geometry parse to pd_config. The audit
    draws in snapshot PIXELS, so verify the delegation reproduces exactly what the
    old inline implementation produced: int(raw/ref * img), clamped to img-1."""
    handler = camera_engine.AxisHandler("1.2.3.4", "u", "p")
    img_w, img_h = 768, 576
    rules = handler._parse_perimeter_defender(METADATA_FRAME, img_w, img_h)
    assert len(rules) == 1 and not rules[0]["is_placeholder"], rules
    assert rules[0]["name"] == "Perimeter Defender: zone-1", rules[0]["name"]
    verts = rules[0]["vertices"]
    assert len(verts) == 13, len(verts)

    expected = [(max(0, min(img_w - 1, int(x / 384 * img_w))),
                 max(0, min(img_h - 1, int(y / 288 * img_h))))
                for x, y in [(383, 287), (114, 287), (333, 152), (175, 110), (122, 97),
                             (15, 65), (49, 63), (168, 52), (188, 38), (327, 54),
                             (350, 55), (362, 54), (384, 54)]]
    assert verts == expected, f"{verts}\n != {expected}"
    assert all(0 <= x < img_w and 0 <= y < img_h for x, y in verts), verts
    return f"audit pixels unchanged; edge vertex clamps to x={verts[-1][0]}"


def test_audit_placeholder_on_unparseable_metadata():
    handler = camera_engine.AxisHandler("1.2.3.4", "u", "p")
    rules = handler._parse_perimeter_defender("<NODE><broken", 768, 576)
    assert len(rules) == 1 and rules[0]["is_placeholder"], rules
    return "audit degrades to a placeholder row rather than raising"


def test_audit_tagged_payload_is_not_mistaken_for_object_analytics():
    """The PD payload travels as a tagged dict. get_analytics_device_ids must not
    treat it as an AOA config and invent sensor ids for a single-sensor thermal."""
    assert camera_engine.get_analytics_device_ids({"__perimeter_defender__": METADATA_FRAME}) == []
    return "PD payload yields no AOA device ids"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nPerimeter Defender (Axis fixed thermal) -- {len(tests)} tests\n"
          + "=" * (width + 60))
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
    print("=" * (width + 60))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
