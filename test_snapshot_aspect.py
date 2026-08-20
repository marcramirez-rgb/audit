#!/usr/bin/env python3
"""Tests for the aspect-aware Axis snapshot ladder. No camera needed.

WHY. VAPIX image.cgi satisfies an off-aspect `resolution=` request by CROPPING
the frame, silently. This tool used to hard-code 16:9 sizes for every Axis
camera; on a 4:3 unit (measured live on the P3747-PLVE quad at
10.23.135.100:5010) that returned the middle 75% of the scene -- the top and
bottom 12.5% of the field of view were simply gone. AOA normalises zone
coordinates over the FULL frame, so audits rendered zones on a short picture
and the writer pushed zones drawn against the wrong canvas. The fix reads the
camera's own image parameters and builds the resolution ladder in ITS aspect.

The property these tests protect: a 16:9 camera must produce EXACTLY the
ladder the tool always used (the PTZ dome fleet must not change), a 4:3 camera
must get 4:3 sizes, and every failure to learn the aspect must fall back to
the legacy ladder rather than guessing.

Run:  .venv\\Scripts\\python.exe test_snapshot_aspect.py
"""

from __future__ import annotations

import io
import sys

import camera_engine as ce


# Trimmed from the real param.cgi output of the P3747-PLVE quad-sensor unit at
# 10.23.135.100:5010 (fw 12.7.53). Four 4:3 sensors plus the I4 quad view.
P3747_PARAMS = """\
root.Properties.Image.Format=jpeg,mjpeg,h264,h265
root.Properties.Image.NbrOfViews=5
root.Properties.Image.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I0.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I0.JPEG.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I1.JPEG.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I2.JPEG.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I3.JPEG.Resolution=2592x1944,1920x1440,1440x1080,1280x960,640x480
root.Properties.Image.I4.JPEG.Resolution=3840x2880,2560x1920,1920x1440,1280x960
root.ImageSource.NbrOfSources=4
root.ImageSource.I0.Rotation=0
root.ImageSource.I1.Rotation=0
root.ImageSource.I2.Rotation=0
root.ImageSource.I3.Rotation=0
"""

# What a Q6135-LE-style 16:9 dome reports.
Q6135_PARAMS = """\
root.Properties.Image.Resolution=1920x1080,1280x720,800x450,640x360,480x270,320x180
root.ImageSource.NbrOfSources=1
root.ImageSource.I0.Rotation=0
"""

# Corridor format: sensor list stays landscape, the source is rotated 90.
CORRIDOR_PARAMS = """\
root.Properties.Image.Resolution=1920x1080,1280x720,640x360
root.ImageSource.NbrOfSources=1
root.ImageSource.I0.Rotation=90
"""

LEGACY_LADDER = ("1280x720", "640x360", "320x180")


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    # fetch_snapshot uses `with session.get(...) as response:`
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ParamSession:
    """Answers param.cgi with canned text; everything else is a 404. Counts
    param.cgi hits so the caching test can prove one read serves the whole unit."""

    def __init__(self, param_text=None, param_status=200):
        self.param_text = param_text
        self.param_status = param_status
        self.param_hits = 0

    def get(self, url, **_kw):
        if "param.cgi" in url:
            self.param_hits += 1
            return _Resp(self.param_status, self.param_text or "")
        return _Resp(404)


def _handler():
    return ce.AxisHandler("203.0.113.10", "user", "pw")


# --------------------------------------------------------------------------- #

def test_a_43_camera_gets_a_43_ladder():
    h = _handler()
    ladder = h._resolution_ladder(_ParamSession(P3747_PARAMS), "5010", channel_idx=2)
    assert ladder == ("1280x960", "640x480", "320x240"), ladder
    return "P3747 sensor 2 -> 1280x960 / 640x480 / 320x240"


def test_every_p3747_view_reads_as_43_including_the_quad_overview():
    h = _handler()
    s = _ParamSession(P3747_PARAMS)
    for ch in (None, 1, 2, 3, 4, 5):
        ladder = h._resolution_ladder(s, "5010", channel_idx=ch)
        assert ladder == ("1280x960", "640x480", "320x240"), (ch, ladder)
    return "camera=None,1..5 all 4:3"


def test_a_169_camera_keeps_the_exact_legacy_ladder():
    """The dome fleet must be bit-for-bit unaffected by this change."""
    h = _handler()
    ladder = h._resolution_ladder(_ParamSession(Q6135_PARAMS), "5015", channel_idx=None)
    assert ladder == LEGACY_LADDER, ladder
    return "16:9 -> unchanged " + "/".join(LEGACY_LADDER)


def test_unreadable_params_fall_back_to_the_legacy_ladder():
    h = _handler()
    ladder = h._resolution_ladder(_ParamSession(None, param_status=401), "5010", None)
    assert ladder == LEGACY_LADDER, ladder
    return "param.cgi 401 -> legacy ladder"


def test_garbage_params_fall_back_to_the_legacy_ladder():
    h = _handler()
    ladder = h._resolution_ladder(_ParamSession("# Error: Error -1 getting param"), "5010", None)
    assert ladder == LEGACY_LADDER, ladder
    return "unparseable text -> legacy ladder"


def test_corridor_rotation_transposes_the_ladder():
    h = _handler()
    ladder = h._resolution_ladder(_ParamSession(CORRIDOR_PARAMS), "5010", None)
    # 16:9 rotated 90 delivers 9:16 frames: heights are now 16/9 of the width.
    assert ladder == ("1280x2276", "640x1138", "320x568"), ladder
    return "Rotation=90 -> portrait sizes"


def test_one_param_read_serves_the_whole_unit():
    """A quad-sensor audit asks per sensor; the camera must be asked once."""
    h = _handler()
    s = _ParamSession(P3747_PARAMS)
    for ch in (None, 1, 2, 3, 4):
        h._resolution_ladder(s, "5010", channel_idx=ch)
        h.fallback_size(s, "5010", channel_idx=ch)
    assert s.param_hits == 1, s.param_hits
    return "10 lookups, 1 request"


def test_failed_param_reads_are_cached_too():
    """A dead camera must not pay the param timeout once per resolution ladder.
    (The first lookup itself may try both auth strategies -- what matters is
    that later lookups add no requests at all.)"""
    h = _handler()
    s = _ParamSession(None, param_status=401)
    h._resolution_ladder(s, "5010", None)
    first = s.param_hits
    h._resolution_ladder(s, "5010", None)
    h.fallback_size(s, "5010")
    assert s.param_hits == first, (first, s.param_hits)
    return "negative result cached"


def test_fallback_canvas_matches_the_camera_aspect():
    # One handler per camera, as in production -- the params are cached per port.
    quad = _handler().fallback_size(_ParamSession(P3747_PARAMS), "5010", channel_idx=1)
    dome = _handler().fallback_size(_ParamSession(Q6135_PARAMS), "5015")
    dead = _handler().fallback_size(_ParamSession(None, 401), "5010")
    assert quad == (1280, 960), quad
    assert dome == (1280, 720), dome
    assert dead == (1280, 720), dead
    return "4:3 -> 1280x960, 16:9/unknown -> 1280x720"


def test_fetch_snapshot_requests_the_aspect_correct_url_first():
    """End to end through fetch_snapshot: the first image.cgi URL asked of a 4:3
    camera must be 1280x960 -- the off-aspect 1280x720 request is the whole bug."""
    from PIL import Image as PILImage

    jpg = io.BytesIO()
    PILImage.new("RGB", (1280, 960)).save(jpg, "JPEG")

    class _SnapSession(_ParamSession):
        def __init__(self):
            super().__init__(P3747_PARAMS)
            self.image_urls = []

        def get(self, url, **kw):
            if "image.cgi" in url:
                self.image_urls.append(url)
                r = _Resp(200)
                r.content = jpg.getvalue()
                return r
            return super().get(url, **kw)

    s = _SnapSession()
    img, url, err, rej = _handler().fetch_snapshot(s, "5010", channel_idx=2)
    assert img is not None and img.size == (1280, 960), (img, err)
    assert s.image_urls[0].endswith("resolution=1280x960&camera=2"), s.image_urls[0]
    return "first request is 1280x960&camera=2"


def test_the_hik_handler_still_answers_fallback_size():
    """The audit calls handler.fallback_size on both vendors; the base-class
    default must keep serving Hik its fixed dimensions."""
    h = ce.HikvisionHandler("203.0.113.11", "user", "pw")
    assert h.fallback_size(_ParamSession(None, 404), "5010") == h.fallback_dim
    return f"Hik -> {ce.HikvisionHandler('x','u','p').fallback_dim}"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nsnapshot aspect ladder -- {len(tests)} tests\n" + "=" * (width + 58))
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
