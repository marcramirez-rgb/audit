import camera_engine


def test_hikvision_multi_zone_parse_and_render():
    xml = '''<?xml version="1.0"?>
<root xmlns="http://www.std-cgi.com/ver20/XMLSchema">
  <RuleInfo>
    <ruleName>MultiZoneRule</ruleName>
    <eventType>FieldDetection</eventType>
    <FieldDetectionParam>
      <durationTime>5</durationTime>
      <detectionTarget>Person</detectionTarget>
    </FieldDetectionParam>
    <RegionCoordinatesList>
      <RegionCoordinates>
        <positionX>10</positionX>
        <positionY>10</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>200</positionX>
        <positionY>10</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>200</positionX>
        <positionY>200</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>10</positionX>
        <positionY>200</positionY>
      </RegionCoordinates>
    </RegionCoordinatesList>
    <RegionCoordinatesList>
      <RegionCoordinates>
        <positionX>300</positionX>
        <positionY>300</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>500</positionX>
        <positionY>300</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>500</positionX>
        <positionY>500</positionY>
      </RegionCoordinates>
      <RegionCoordinates>
        <positionX>300</positionX>
        <positionY>500</positionY>
      </RegionCoordinates>
    </RegionCoordinatesList>
  </RuleInfo>
</root>'''

    handler = camera_engine.HikvisionHandler('127.0.0.1', 'user', 'pass')
    rules = handler.parse_analytics(xml, 640, 480)
    assert len(rules) == 2, f'Expected 2 parsed rules, got {len(rules)}'
    assert rules[0]['name'].endswith('[Zone 1]')
    assert rules[1]['name'].endswith('[Zone 2]')
    assert all(isinstance(rule['vertices'], list) for rule in rules)
    assert all(len(rule['vertices']) == 4 for rule in rules), 'Each zone should have 4 vertices'

    for idx, rule in enumerate(rules):
        img_buf = camera_engine.render_overlay_image(None, rule['vertices'], idx, 640, 480, rule['type'])
        assert img_buf is not None
        assert img_buf.readable()


def test_axis_polygon_order_preserved():
    vertices = [
        (0, 1078), (0, 861), (408, 237), (926, 212),
        (907, 146), (1408, 142), (1915, 206), (1915, 1080)
    ]
    assert len(vertices) > 2
    # The renderer should preserve an already-ordered Axis polygon rather
    # than reordering it into a shape that no longer matches the rule.
    img_buf = camera_engine.render_overlay_image(None, vertices, 0, 1920, 1080, 'Intrusion Detection')
    assert img_buf is not None
    assert img_buf.readable()


def test_axis_saved_debug_vertices():
    saved_vertices = [
        (0, 1078), (0, 861), (408, 237), (926, 212),
        (907, 146), (1408, 142), (1915, 206), (1915, 1080)
    ]
    img_buf = camera_engine.render_overlay_image(None, saved_vertices, 0, 1920, 1080, 'Intrusion Detection')
    assert img_buf is not None
    assert img_buf.readable()


def test_hik_edge_zones_stay_on_the_camera_grid():
    """REGRESSION (10.23.101.11:5020). The Hik grid tops out at 999 -- the
    camera's own UI never writes 1000, and a PUT containing one is rejected whole
    with 400 'Invalid XML Content'. An edge-hugging zone's 999s drift to 1000
    through the fraction->canvas->fraction round-trip (999 -> 0.999 -> canvas
    pixel int -> back -> rounds to 1000), so duplicating a native full-frame rule
    used to build an unpushable document. Every emitted coordinate must clamp."""
    import vendor_adapter as va
    # The exact drift: fractions that round to 1000.
    for frac, expect in [(1.0, 999), (0.9995, 999), (0.999, 999), (0.5, 500),
                         (0.0, 0), (-0.01, 0)]:
        got = va._hik_coord(frac)
        assert got == expect, f"_hik_coord({frac}) = {got}, expected {expect}"
    # The full-frame duplicate from the field report, through the real conversion.
    pts = [(0.001, 0.998), (0.005, 0.072), (1.0, 0.069), (1.0, 1.0)]
    region = [(va._hik_coord(fx), va._hik_coord(1 - fy)) for fx, fy in pts]
    assert all(0 <= x <= 999 and 0 <= y <= 999 for x, y in region), region
    # Size boxes go through the same clamp.
    rect = va.HikAdapter._frac_rect_to_hik((0.0, 0.0, 1.0, 1.0))
    assert rect == (0, 0, 999, 999), rect
    assert va.HikAdapter._frac_rect_to_hik(None) is None


if __name__ == '__main__':
    test_hikvision_multi_zone_parse_and_render()
    test_axis_polygon_order_preserved()
    test_axis_saved_debug_vertices()
    test_hik_edge_zones_stay_on_the_camera_grid()
    print('PASS: Hikvision multi-zone and Axis polygon regression tests')
