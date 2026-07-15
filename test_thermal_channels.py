import os
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from PIL import Image as PILImage, ImageDraw
import urllib3

# Suppress self-signed certificate warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- TEST TARGET CONFIGURATION ---
CAMERA_IP = "192.0.2.10"    # Replace with your test thermal camera IP
PORT = "5010"               # Replace with the targeted interface port
USERNAME = ""               # Fill in before running -- do not commit real credentials
PASSWORD = ""               # Fill in before running -- do not commit real credentials

STRICT_TIMEOUT = (3.05, 5.0)

def fetch_active_rules(session, auth_strategies):
    """Probes channels 1 and 2 to find active analytic perimeters."""
    for channel_id in [1, 2]:
        rule_url = f"http://{CAMERA_IP}:{PORT}/ISAPI/Intelligent/channels/{channel_id}/behaviorRule/1"
        for auth in auth_strategies:
            try:
                response = session.get(rule_url, auth=auth, timeout=STRICT_TIMEOUT, verify=False)
                if response.status_code == 200:
                    temp_xml = response.text
                    if temp_xml and ("positionX" in temp_xml or "RegionCoordinates" in temp_xml):
                        print(f"[+] Successfully found active rules on Channel {channel_id}")
                        return temp_xml
            except requests.exceptions.RequestException:
                continue
    return None

def parse_analytics_xml(xml_data, img_w, img_h):
    """Parses Hikvision XML payloads and extracts coordinate matrices matching production logic."""
    namespaces = {'ns': 'http://www.std-cgi.com/ver20/XMLSchema'}
    parsed_rules = []
    
    try:
        root = ET.fromstring(xml_data)
        rules = root.findall('.//ns:RuleInfo', namespaces)
    except Exception as e:
        print(f"[!] XML Parsing Error: {e}")
        return []

    for rule in rules:
        rule_name = rule.find('ns:ruleName', namespaces).text
        
        event_type_raw = rule.find('ns:eventType', namespaces)
        event_type = event_type_raw.text if event_type_raw is not None else "Unknown"
        if "field" in event_type.lower() or "intrusion" in event_type.lower():
            event_type = "Intrusion Detection"
        elif "line" in event_type.lower() or "cross" in event_type.lower():
            event_type = "Line Crossing"

        region_lists = rule.findall('.//ns:RegionCoordinatesList', namespaces)
        for region in region_lists:
            vertices = []
            for coord in region.findall('ns:RegionCoordinates', namespaces):
                raw_x = float(coord.find('ns:positionX', namespaces).text)
                raw_y = float(coord.find('ns:positionY', namespaces).text)
                
                # 0..1000 mapping space normalized to pixel canvas dimension
                pixel_x = int((raw_x / 1000.0) * img_w)
                pixel_y = int((raw_y / 1000.0) * img_h)
                
                # Vertical flip adjustment matching verified production layout
                pixel_y = img_h - 1 - pixel_y
                pixel_x = max(0, min(img_w - 1, pixel_x))
                pixel_y = max(0, min(img_h - 1, pixel_y))
                vertices.append((pixel_x, pixel_y))
                
            if vertices:
                parsed_rules.append({
                    "name": rule_name,
                    "type": event_type,
                    "vertices": vertices
                })
    return parsed_rules

def run_overlay_test():
    print("==================================================")
    print(f"STARTING THERMAL OVERLAY PLOT TEST FOR {CAMERA_IP}:{PORT}")
    print("==================================================")
    
    session = requests.Session()
    auth_strategies = [HTTPDigestAuth(USERNAME, PASSWORD), HTTPBasicAuth(USERNAME, PASSWORD)]
    
    # 1. Fetch XML Rules
    print("[*] Step 1: Querying camera intelligence perimeters...")
    xml_data = fetch_active_rules(session, auth_strategies)
    if not xml_data:
        print("[-] Failure: Could not retrieve active analytics metadata from the camera.")
        return

    # 2. Fetch Thermal Snapshot (Channel 201)
    print("[*] Step 2: Fetching thermal snapshot stream from channel 201...")
    snap_url = f"http://{CAMERA_IP}:{PORT}/ISAPI/Streaming/channels/201/picture"
    camera_image = None
    
    for auth in auth_strategies:
        try:
            response = session.get(snap_url, auth=auth, timeout=STRICT_TIMEOUT, verify=False)
            if response.status_code == 200:
                camera_image = PILImage.open(BytesIO(response.content)).convert("RGBA")
                break
        except requests.exceptions.RequestException:
            continue
            
    if camera_image is None:
        print("[-] Failure: Could not pull snapshot image from channel 201.")
        return
        
    img_w, img_h = camera_image.size
    print(f"[+] Snapshot captured successfully. Size: {img_w}x{img_h}")

    # 3. Parse Rules
    print("[*] Step 3: Parsing geometric zone coordinate matrix...")
    rules = parse_analytics_xml(xml_data, img_w, img_h)
    if not rules:
        print("[-] Warning: Camera responded successfully, but has 0 rules configured.")
        return
        
    print(f"[+] Found {len(rules)} active rule perimeter shape(s).")

    # 4. Render Overlays via Pillow
    print("[*] Step 4: Rendering shapes onto the thermal canvas...")
    overlay = PILImage.new("RGBA", camera_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # Use Kingfisher Normal teal tuple (0, 161, 154) matching your theme configurations
    teal_fill = (0, 161, 154, 76)
    teal_outline = (0, 161, 154, 255)
    
    for r_idx, rule in enumerate(rules, 1):
        verts = rule["vertices"]
        print(f"    -> Plotting rule '{rule['name']}' ({rule['type']}) with {len(verts)} vertices: {verts}")
        
        if "Line" in rule["type"] or len(verts) <= 2:
            draw.line(verts, fill=teal_outline, width=4)
        else:
            draw.polygon(verts, fill=teal_fill)
            draw.polygon(verts, outline=teal_outline, width=3)
            
        for (x, y) in verts:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 255, 0, 255), outline=(0, 0, 0, 255), width=1)

    # Composite the canvas layers
    final_img = PILImage.alpha_composite(camera_image, overlay).convert("RGB")
    output_filename = "thermal_overlay_test.jpg"
    final_img.save(output_filename, format="JPEG")
    
    print("==================================================")
    print(f"[SUCCESS] Test run complete.")
    print(f"[+] Saved master overlay file to: {os.path.abspath(output_filename)}")
    print("[!] Open this image file to confirm that the teal zone aligns perfectly with your physical scene landmarks.")
    print("==================================================")

if __name__ == "__main__":
    run_overlay_test()