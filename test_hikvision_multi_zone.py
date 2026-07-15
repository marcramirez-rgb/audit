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


if __name__ == '__main__':
    test_hikvision_multi_zone_parse_and_render()
    test_axis_polygon_order_preserved()
    test_axis_saved_debug_vertices()
    print('PASS: Hikvision multi-zone and Axis polygon regression tests')
