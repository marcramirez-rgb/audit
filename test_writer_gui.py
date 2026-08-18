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


class _StubWritable:
    """Stands in for an ordinary writable Axis camera (Object Analytics)."""
    vendor = vendor_adapter.AxisAdapter.vendor
    capabilities = vendor_adapter.AxisAdapter.capabilities


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
    app.adapter = _StubWritable()
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
    app.adapter = _StubWritable()
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
    """REGRESSION. This line-builder used to unpack ANY native_id as a Hikvision
    (channel, scene, rule) triple, so a vendor whose id was a different shape
    raised "not enough values to unpack" and the whole read failed. Every shape in
    use has to survive it -- match on length, never on "is a tuple"."""
    shapes = [
        ("Axis AOA (int)", 3),
        ("Hik (channel, scene, rule)", ("2", 2, 7)),
        ("Axis bidirectional line", vendor_adapter.AxisLinePair(4, 5)),
        ("Perimeter Defender (none)", None),
    ]
    for label, native in shapes:
        sc = vendor_adapter.Scenario(
            name="RULE", kind="intrusion", points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
            classes=("human",), native_id=native)
        line = app._scenario_log_line(sc)          # must not raise
        assert "RULE" in line, (label, line)
    hik = vendor_adapter.Scenario(
        name="H", kind="intrusion", points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)],
        classes=("human",), native_id=("2", 3, 4))
    assert "@ch2/s3/r4" in app._scenario_log_line(hik), app._scenario_log_line(hik)
    return f"{len(shapes)} native_id shapes render without unpacking errors"


# --------------------------------------------------------------------------- #
# Fixed thermal (Perimeter Defender -- read-only)
# --------------------------------------------------------------------------- #


def test_a_fully_read_only_camera_still_refuses_and_explains(app):
    """A fixed thermal runs Perimeter Defender, which has no configuration API at
    all. Selecting one of its zones must refuse with the reason and leave Push
    disabled -- only AXIS Perimeter Defender Setup can change that geometry."""
    sc = _readonly_scenario()
    app.adapter = _StubReadOnly()
    app._apply_capabilities()
    _select(app, sc)
    assert app.edit_var.get() == awg.NEW_SCENARIO, app.edit_var.get()
    assert app.editing is None
    assert app.points == [], "a read-only rule must not load into the editor"
    assert str(app.push_button.cget("state")) == "disabled"
    return "read-only camera: selection refused, Push disabled"


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
