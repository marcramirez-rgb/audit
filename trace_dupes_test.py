import camera_engine
import requests
from PIL import Image

# Monkeypatch HikvisionHandler methods to return deterministic results

def fake_fetch_analytics(self, session, port):
        # Return XML with two identical RuleInfo entries to exercise the real parser
        xml = '''<?xml version="1.0"?>
<root xmlns="http://www.std-cgi.com/ver20/XMLSchema">
    <RuleInfo>
        <ruleName>TestRule</ruleName>
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
                <positionX>100</positionX>
                <positionY>10</positionY>
            </RegionCoordinates>
            <RegionCoordinates>
                <positionX>100</positionX>
                <positionY>100</positionY>
            </RegionCoordinates>
            <RegionCoordinates>
                <positionX>10</positionX>
                <positionY>100</positionY>
            </RegionCoordinates>
        </RegionCoordinatesList>
    </RuleInfo>
    <RuleInfo>
        <ruleName>TestRule</ruleName>
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
                <positionX>100</positionX>
                <positionY>10</positionY>
            </RegionCoordinates>
            <RegionCoordinates>
                <positionX>100</positionX>
                <positionY>100</positionY>
            </RegionCoordinates>
            <RegionCoordinates>
                <positionX>10</positionX>
                <positionY>100</positionY>
            </RegionCoordinates>
        </RegionCoordinatesList>
    </RuleInfo>
</root>'''
        return xml, f'http://{self.ip}:{port}/ISAPI/Intelligent/channels/1/behaviorRule/1', None, False


def fake_fetch_snapshot(self, session, port):
    # Return a tiny blank image
    img = Image.new('RGB', (640, 480), color=(255,255,255))
    return img, f'http://{self.ip}:{port}/snapshot', None, False


def fake_parse_analytics(self, xml_data, img_w, img_h):
    # Return duplicate rules to simulate the duplicate-row bug
    rule = {"is_placeholder": False, "name": "TestRule", "type": "Intrusion Detection", "target": "Person", "duration": "5", "vertices": [(10,10),(100,10),(100,100),(10,100)]}
    return [rule, rule]

# Apply monkeypatch
camera_engine.HikvisionHandler.fetch_analytics = fake_fetch_analytics
camera_engine.HikvisionHandler.fetch_snapshot = fake_fetch_snapshot

# Prepare inputs
session = requests.Session()
credentials = {"HIK_USER": "user", "HIK_PASS": "pass"}
row_data = {"CLIENT_NM": "ClientA", "LOCATION_NM": "Loc1", "LIVE_UNIT_SERIAL_NM": "SN123", "IP": "192.0.2.5", "MANUFACTURER": "LVT"}

# Run
res = camera_engine.process_camera_row((1, row_data, session, credentials))

print('Logs:')
for l in res['logs']:
    print(l)
print('\nMain rows:')
for r in res['main']:
    print(r['data'])
print('\nTotal main rows:', len(res['main']))

# Show dedupe_main_rows behavior (as used at export time)
deduped = camera_engine.dedupe_main_rows(res['main'])
print('\nAfter dedupe_main_rows:')
for r in deduped:
    print(r['data'])
print('\nTotal main rows after dedupe:', len(deduped))
