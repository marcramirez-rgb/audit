import json
from pathlib import Path
from PIL import Image, ImageDraw
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth

OUTPUT_DIR = Path(r'c:\Users\MarcRamirez\Downloads\axis_api_testing\debug_overlay_tests')
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CAMERA_IP = '192.0.2.10'  # placeholder -- set to the target camera IP before running
PORT = 5010
USERNAME = ''  # fill in before running -- do not commit real credentials
PASSWORD = ''  # fill in before running -- do not commit real credentials
SNAPSHOT_URL = f'http://{CAMERA_IP}:{PORT}/ISAPI/Streaming/channels/101/picture'

JSON_FILE = OUTPUT_DIR / 'overlay_20260706T145800Z_1_vertices.json'

XML_COORDS = [
    (1, 8),
    (3, 722),
    (67, 745),
    (68, 688),
    (329, 565),
    (909, 762),
    (997, 740),
    (999, 0),
]

TRANSFORMS = {
    'direct': lambda x, y: (x, y),
    'flip_x': lambda x, y: (1000 - x, y),
    'flip_y': lambda x, y: (x, 1000 - y),
    'flip_xy': lambda x, y: (1000 - x, 1000 - y),
    'swap': lambda x, y: (y, x),
    'swap_flip_x': lambda x, y: (1000 - y, x),
    'swap_flip_y': lambda x, y: (y, 1000 - x),
    'swap_flip_xy': lambda x, y: (1000 - y, 1000 - x),
}


def fetch_snapshot():
    auths = [HTTPDigestAuth(USERNAME, PASSWORD), HTTPBasicAuth(USERNAME, PASSWORD)]
    for auth in auths:
        try:
            response = requests.get(SNAPSHOT_URL, auth=auth, timeout=(5, 10), stream=True, verify=False)
            if response.status_code == 200:
                out_path = OUTPUT_DIR / 'raw_snapshot.png'
                with open(out_path, 'wb') as f:
                    f.write(response.content)
                print(f'Snapshot saved: {out_path}')
                return out_path
            print(f'Auth {type(auth).__name__} got status {response.status_code}')
        except Exception as exc:
            print(f'Auth {type(auth).__name__} failed: {exc}')
    raise RuntimeError('Snapshot fetch failed')


def load_saved_vertices():
    if not JSON_FILE.exists():
        raise FileNotFoundError(f'JSON file not found: {JSON_FILE}')
    with open(JSON_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return [(int(x), int(y)) for x, y in data['vertices']]


def draw_polygon(img, coords, color, width=6):
    draw = ImageDraw.Draw(img)
    if len(coords) > 1:
        draw.polygon(coords, outline=color, width=width)
    for x, y in coords:
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=color)


def scale_coords(coords, img_w, img_h):
    return [(int((x / 1000.0) * img_w), int((y / 1000.0) * img_h)) for x, y in coords]


def main():
    snapshot_path = fetch_snapshot()
    img = Image.open(snapshot_path).convert('RGBA')
    img_w, img_h = img.size
    print('Snapshot size:', img_w, img_h)

    saved = load_saved_vertices()
    print('Saved vertices:', saved)

    out_current = img.copy()
    draw_polygon(out_current, saved, 'blue')
    out_current.save(OUTPUT_DIR / 'compare_current.png')

    # direct mapping to actual snapshot size
    direct_coords = scale_coords(XML_COORDS, img_w, img_h)
    out_direct = img.copy()
    draw_polygon(out_direct, direct_coords, 'red')
    out_direct.save(OUTPUT_DIR / 'test_direct_actual_size.png')

    # combined overlay of direct and current
    out_combo = img.copy()
    draw_polygon(out_combo, direct_coords, 'red')
    draw_polygon(out_combo, saved, 'blue')
    out_combo.save(OUTPUT_DIR / 'compare_direct_vs_saved.png')

    print('Generated images: compare_current.png, test_direct_actual_size.png, compare_direct_vs_saved.png')
    print('If the red direct overlay matches the web UI and the blue saved overlay does not, the source transform is wrong.')

    # also print transformed variants for debugging
    for name, fn in TRANSFORMS.items():
        coords = scale_coords([fn(x, y) for x, y in XML_COORDS], img_w, img_h)
        print(name, coords)
        out = img.copy()
        draw_polygon(out, coords, 'green')
        out.save(OUTPUT_DIR / f'test_variant_{name}.png')

    print('Also generated variant images test_variant_<name>.png for image-size transform testing.')


if __name__ == '__main__':
    main()
