import camera_engine

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
      <RegionCoordinates><positionX>10</positionX><positionY>10</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>200</positionX><positionY>10</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>200</positionX><positionY>200</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>10</positionX><positionY>200</positionY></RegionCoordinates>
    </RegionCoordinatesList>
    <RegionCoordinatesList>
      <RegionCoordinates><positionX>300</positionX><positionY>300</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>500</positionX><positionY>300</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>500</positionX><positionY>500</positionY></RegionCoordinates>
      <RegionCoordinates><positionX>300</positionX><positionY>500</positionY></RegionCoordinates>
    </RegionCoordinatesList>
  </RuleInfo>
</root>'''

handler = camera_engine.HikvisionHandler('127.0.0.1', 'user', 'pass')
rules = handler.parse_analytics(xml, 640, 480)
print('parsed rule count:', len(rules))
for i, r in enumerate(rules, 1):
    print(i, r['name'], 'vertices count', len(r['vertices']), 'vertices:', r['vertices'])

for i, r in enumerate(rules, 1):
    filename = f'replay_hikviz_zone_{i}.jpg'
    with open(filename, 'wb') as f:
        img = camera_engine.render_overlay_image(None, r['vertices'], i-1, 640, 480, r['type'])
        f.write(img.getvalue())
    print('wrote', filename)
