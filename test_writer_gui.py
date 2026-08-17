#!/usr/bin/env python3
"""GUI-level tests for the analytics writer. No camera needed.

These exist because the module-level tests cannot catch a whole class of bug: the
writer's state lives in Tk widgets, and a change to how the UI is re-gated can
silently destroy what the operator drew. That happened -- re-gating capabilities
on every adapter build wiped the drawing, so Push reported "needs an area of at
least 3 points" for a zone that was plainly on screen. Unit tests could not see
it; this file can.

ONE window is created for the whole run and reset between tests. Tk supports a
single root per process; creating one per test poisons the interpreter (the
second window's images resolve against a dead root -- 'image pyimageN doesn't
exist') and every test then fails for reasons that have nothing to do with the
code under test.

Needs a display -- these are desktop tools, so that is the real environment. If
Tk cannot start at all, the suite reports SKIP rather than failing.

Run:  .venv\\Scripts\\python.exe test_writer_gui.py
"""

from __future__ import annotations

import sys

from PIL import Image

import analytics_writer_gui as awg
import vendor_adapter


def _reset(app):
    """Return the shared window to a just-loaded-a-snapshot state."""
    app.adapter = None
    app.editing = None
    app.points, app.exclude_zones, app.current_exclude = [], [], []
    app.edit_size, app.perspective_bars, app.current_bar = [], [], []
    app.size_mode = app.size_first = None
    app.bar_mode = False
    app.existing_scenarios, app.existing_overlays = [], []
    app.edit_label_map = {}
    app.edit_var.set(awg.NEW_SCENARIO)
    app.name_var.set("")
    app.mfg_var.set("Axis")
    app._apply_capabilities()
    app.rule_var.set("Intrusion")
    app._refresh_type_frames()
    # A real snapshot so canvas<->fraction maths and _redraw work for real.
    app._display_image(Image.new("RGB", (640, 480), "gray"))
    app.update()
    return app


def _drawn_area(app, n=4):
    """Put a believable area on the canvas."""
    app.points = [app._frac_to_canvas(fx, fy) for fx, fy in
                  [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)][:n]]
    return list(app.points)


def _readonly_scenario():
    return vendor_adapter.Scenario(
        name="intrusion-1 / zone-1", kind="intrusion",
        points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.3, 0.6), (0.1, 0.9)],
        classes=("human",), duration=15, read_only=True, detail="intrusion")


def _select(app, sc):
    app.existing_scenarios = [sc]
    app.edit_label_map = {sc.name: sc}
    app.edit_var.set(sc.name)
    app._on_edit_select(sc.name)


class _StubThermal:
    """Stands in for a connected fixed thermal (writable via Guard apps)."""
    vendor = vendor_adapter.AxisThermalAdapter.vendor
    capabilities = vendor_adapter.AxisThermalAdapter.capabilities
    guard = object()          # presence of .guard is what marks a thermal


class _StubReadOnly:
    vendor = vendor_adapter.PerimeterDefenderAdapter.vendor
    capabilities = vendor_adapter.PerimeterDefenderAdapter.capabilities


# --------------------------------------------------------------------------- #
# The regression
# --------------------------------------------------------------------------- #

def test_re_gating_capabilities_does_not_erase_the_drawing(app):
    """THE REGRESSION. _apply_capabilities runs on every adapter build -- including
    the one inside Push -- so it must never clear the canvas. When it did, Push
    rejected a zone that was visibly drawn."""
    before = _drawn_area(app)
    app.adapter = _StubThermal()
    app._apply_capabilities()
    assert app.points == before, (
        f"capability re-gating erased the drawing: {len(before)} -> {len(app.points)}")
    return f"{len(before)} drawn points survive _apply_capabilities"


def test_push_still_has_its_geometry_after_building_an_adapter(app):
    """End-to-end shape of the same bug: _build_scenario must succeed immediately
    after the capability re-gate that Push triggers."""
    _drawn_area(app)
    app.name_var.set("ZONE-A")
    app.rule_var.set("Intrusion")
    app.adapter = _StubThermal()
    app._apply_capabilities()              # what _make_adapter does at the end
    sc, reason = app._build_scenario()
    assert sc is not None, f"Push would have refused: {reason}"
    assert len(sc.points) == 4, len(sc.points)
    return "scenario builds cleanly after re-gating"


def test_changing_rule_type_still_clears_the_drawing(app):
    """The counterpart: an intentional type change SHOULD discard geometry, since
    an area and a line are not the same shape."""
    _drawn_area(app)
    app.rule_var.set("Line Crossing")
    app._on_rule_change()
    assert app.points == [], f"rule change left {len(app.points)} stale points"
    return "explicit rule-type change clears points, as intended"


# --------------------------------------------------------------------------- #
# Reading back rules of every vendor shape
# --------------------------------------------------------------------------- #

def test_log_line_handles_every_native_id_shape(app):
    """REGRESSION. Reported from the field as:

        [!] Read failed: not enough values to unpack (expected 3, got 2)

    ...on the first read AFTER a push to a thermal. Pushing starts the owning
    Guard app, so the next read returns Guard rules -- and this line-builder
    unpacked any native_id as a Hikvision (channel, scene, rule) triple. Every
    vendor's shape has to survive it."""
    shapes = [
        ("Axis AOA (int)", 3),
        ("Hik (channel, scene, rule)", ("2", 2, 7)),
        ("Axis bidirectional line", vendor_adapter.AxisLinePair(4, 5)),
        ("Axis Guard app", vendor_adapter.GuardRuleId("motionguard", 2)),
        ("Perimeter Defender (none)", None),
    ]
    for label, native in shapes:
        sc = vendor_adapter.Scenario(
            name="RULE", kind="intrusion", points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
            classes=("human",), native_id=native)
        line = app._scenario_log_line(sc)          # must not raise
        assert "RULE" in line, (label, line)
    guard = vendor_adapter.Scenario(
        name="G", kind="intrusion", points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
        classes=(), native_id=vendor_adapter.GuardRuleId("fenceguard", 9))
    assert "@fenceguard/p9" in app._scenario_log_line(guard), app._scenario_log_line(guard)
    hik = vendor_adapter.Scenario(
        name="H", kind="intrusion", points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
        classes=("human",), native_id=("2", 3, 4))
    assert "@ch2/s3/r4" in app._scenario_log_line(hik), app._scenario_log_line(hik)
    return f"{len(shapes)} native_id shapes render without unpacking errors"


def test_read_after_push_sequence_does_not_crash(app):
    """The exact field sequence: push starts a Guard app, so the following read
    returns a mix of PD (read-only, native_id None) and Guard rules."""
    scenarios = [
        vendor_adapter.Scenario(name="intrusion-1 / zone-1", kind="intrusion",
                                points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
                                classes=("human",), duration=15,
                                read_only=True, detail="intrusion"),
        vendor_adapter.Scenario(name="NEWZONE", kind="intrusion",
                                points=[(0.2, 0.2), (0.8, 0.2), (0.8, 0.8)],
                                classes=(), detail="AXIS motionguard",
                                native_id=vendor_adapter.GuardRuleId("motionguard", 2)),
    ]
    lines = [app._scenario_log_line(s) for s in scenarios]
    assert "(read-only)" in lines[0], lines[0]
    assert "@motionguard/p2" in lines[1], lines[1]
    # And the edit dropdown must build over the mixed list without blowing up.
    labels = app._build_edit_labels(scenarios)
    assert len(labels) == 2, labels
    return "PD + Guard rules read back together cleanly"


# --------------------------------------------------------------------------- #
# Fixed-thermal workflow
# --------------------------------------------------------------------------- #

def test_selecting_a_pd_zone_copies_it_as_a_new_rule(app):
    """The fixed-thermal workflow: a Perimeter Defender zone can't be edited, so
    picking it must hand over its SHAPE for a new Guard rule -- not dead-end."""
    sc = _readonly_scenario()
    app.adapter = _StubThermal()
    app._apply_capabilities()
    _select(app, sc)

    assert app.editing is None, "must be a NEW rule, not an in-place edit"
    assert len(app.points) == len(sc.points), \
        f"geometry not copied: {len(app.points)} vs {len(sc.points)}"
    assert app.name_var.get() and app.name_var.get() != sc.name, \
        f"copy must get its own name, got {app.name_var.get()!r}"
    assert len(app.name_var.get()) <= 15, "Guard caps names at 15 chars"
    return f"{len(sc.points)} points copied, renamed {app.name_var.get()!r}, editing=None"


def test_copied_geometry_round_trips_back_to_the_same_fractions(app):
    """The copy goes fractions -> canvas -> fractions. If that drifts, the pushed
    Guard rule would not sit where the PD zone does."""
    sc = _readonly_scenario()
    app.adapter = _StubThermal()
    app._apply_capabilities()
    _select(app, sc)
    back = [app._canvas_to_frac(x, y) for (x, y) in app.points]
    worst = max(max(abs(a - c), abs(b - d)) for (a, b), (c, d) in zip(back, sc.points))
    assert worst < 0.01, f"copied geometry drifted by {worst}"
    return f"max round-trip error {worst:.5f}"


def test_a_copied_pd_zone_builds_a_pushable_scenario(app):
    """The copy must actually be pushable -- and must NOT carry read_only, or the
    thermal adapter would refuse its own copy."""
    sc = _readonly_scenario()
    app.adapter = _StubThermal()
    app._apply_capabilities()
    _select(app, sc)
    app.name_var.set("ZONE-COPY")
    built, reason = app._build_scenario()
    assert built is not None, f"copy is not pushable: {reason}"
    assert built.read_only is False, "a copy must be writable"
    assert built.native_id is None, "a copy must not target the PD rule"
    return "copy builds a writable scenario with no native_id"


def test_a_fully_read_only_camera_still_refuses_and_explains(app):
    """With no writable engine at all (PD-only adapter), copying is pointless --
    the operator gets the reason instead."""
    sc = _readonly_scenario()
    app.adapter = _StubReadOnly()
    app._apply_capabilities()
    _select(app, sc)
    assert app.edit_var.get() == awg.NEW_SCENARIO, app.edit_var.get()
    assert app.editing is None
    assert app.points == [], "nothing to copy into on a read-only camera"
    assert str(app.push_button.cget("state")) == "disabled"
    return "read-only camera: selection refused, Push disabled"


def test_thermal_is_writable_and_hides_object_classes(app):
    app.adapter = _StubThermal()
    app._apply_capabilities()
    assert str(app.push_button.cget("state")) == "normal"
    assert str(app.rule_menu.cget("state")) == "normal"
    # Guard apps have no classification -- both boxes must be dead.
    assert str(app.class_human.cget("state")) == "disabled"
    assert str(app.class_vehicle.cget("state")) == "disabled"
    return "thermal: Push enabled, class checkboxes disabled"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    try:
        app = awg.WriterApp()
        app.geometry("1200x800+50+50")
        app.withdraw()
        app.update()
    except Exception as exc:                                      # noqa: BLE001
        print(f"\nSKIP writer GUI tests -- no usable display "
              f"({type(exc).__name__}: {exc})\n")
        return 0

    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nanalytics writer GUI -- {len(tests)} tests\n" + "=" * (width + 58))
    try:
        for name, fn in tests:
            try:
                detail = fn(_reset(app)) or ""
                print(f"PASS  {name:<{width}}  {detail}")
            except AssertionError as exc:
                failures.append((name, str(exc)))
                print(f"FAIL  {name:<{width}}  {exc}")
            except Exception as exc:                              # noqa: BLE001
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"ERROR {name:<{width}}  {type(exc).__name__}: {exc}")
    finally:
        # Cancel the repeating queue poll before teardown, or Tk prints an
        # 'invalid command name' traceback that reads like a failure.
        try:
            for job in app.tk.call("after", "info"):
                try:
                    app.after_cancel(job)
                except Exception:                                 # noqa: BLE001
                    pass
            app.destroy()
        except Exception:                                         # noqa: BLE001
            pass

    print("=" * (width + 58))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
