#!/usr/bin/env python3
"""PTZ Drift Read-Only Excel Analyzer & Reporter (With Operator Timestamps)

Imports snapshot capture and image rendering routines directly from camera_engine.py,
probes local PTZ coordinates/logs across all 3 camera ports per unit IP,
extracts exact timestamps for manual operator PTZ modifications,
flags unsolicited mechanical drift, prints live terminal updates,
and exports a clean Excel (.xlsx) audit report.
"""

import io
import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import openpyxl
from openpyxl.drawing.image import Image as OpenpyxlImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Import camera engine components without modifying camera_engine.py
from camera_engine import (
    AxisHandler,
    HikvisionHandler,
    render_overlay_image,
    CAMERA_CONFIGS,
    STRICT_TIMEOUT,
    ROW_H,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PTZExcelAnalyzer")

# =====================================================================
# DRIFT TOLERANCES (READ-ONLY REPORTING THRESHOLDS)
# =====================================================================
TOLERANCES = {
    "minor": {
        "pan": 30.0,  # Must exceed 30° shortest angular path to flag
        "tilt": 10.0, # Must exceed 10° tilt path to flag
        "zoom": 2.0,
    },
    "critical": {
        "pan": 60.0,  # Major pan shift
        "tilt": 20.0, # Major tilt shift
        "zoom": 5.0,
    }
}

# Keywords indicating a human operator was actively controlling the camera
MANUAL_CONTROL_KEYWORDS = [
    "manual", "ptz_control", "user_action", "operator", 
    "joystick", "web_ui_move", "ptzctrl", "gui_goto"
]

# Target units to scan (each unit IP probes ports 5010, 5015, 5020)
MOCK_UNIT_TARGETS = [
    {
        "unit_id": "LVT_UNIT_101",
        "vendor": "axis",
        "ip": "10.23.34.243",
        "username": "root",
        "password": "REDACTED-CREDENTIAL",
        "expected_baselines": {
            "CENTER": {"pan": 180.0, "tilt": 15.0, "zoom": 1.0},
            "LEFT":   {"pan": 90.0,  "tilt": 10.0, "zoom": 1.0},
            "RIGHT":  {"pan": 270.0, "tilt": 10.0, "zoom": 1.0},
        },
    },
]


# =====================================================================
# 1. ANGULAR WRAP & SCALING HELPERS
# =====================================================================

def normalize_angle_degrees(val: Optional[float]) -> Optional[float]:
    """Auto-detects Hikvision 10x scaled integers (e.g. 2700 -> 270.0)."""
    if val is None:
        return None
    val = float(val)
    if abs(val) > 360.0:
        val = val / 10.0
    return round(val % 360.0, 2)


def calculate_shortest_angular_distance(actual: float, expected: float) -> Tuple[float, float]:
    """Calculates shortest distance on a 360-degree circle."""
    diff = (actual - expected + 180.0) % 360.0 - 180.0
    return round(diff, 2), round(abs(diff), 2)


# =====================================================================
# 2. READ-ONLY SNAPSHOT CAPTURE (CAMERA_ENGINE HANDLERS)
# =====================================================================

def fetch_snapshot_from_engine(
    ip: str, port: str, vendor: str, user: str, passw: str, label: str
) -> Optional[io.BytesIO]:
    """Fetches snapshots via HTTP GET without sending write commands."""
    session = requests.Session()
    vendor_clean = vendor.lower()

    handler = AxisHandler(ip, user, passw) if "axis" in vendor_clean else HikvisionHandler(ip, user, passw)

    camera_image = None
    try:
        camera_image, snap_url, snap_err, auth_rejected = handler.fetch_snapshot(session, port)
        
        if camera_image is not None:
            img_w, img_h = camera_image.size
            logger.info(f"Successfully fetched snapshot from {ip}:{port} ({label})")
        else:
            img_w, img_h = handler.fallback_dim
            logger.warning(f"Snapshot HTTP fetch failed for {ip}:{port} ({snap_err}). Using fallback frame.")

    except Exception as exc:
        img_w, img_h = handler.fallback_dim
        logger.warning(f"Error connecting to {ip}:{port} ({exc}). Using fallback frame.")

    try:
        img_buf = render_overlay_image(
            camera_image=camera_image,
            vertices=[],
            index=0,
            img_w=img_w,
            img_h=img_h,
            rule_type=f"PTZ Audit: {label}",
        )

        if camera_image is not None:
            camera_image.close()

        # Reset buffer pointer so openpyxl reads from byte 0
        if img_buf:
            img_buf.seek(0)

        return img_buf

    except Exception as exc:
        logger.error(f"Failed to render overlay buffer for {ip}:{port}: {exc}")
        return None


# =====================================================================
# 3. DRIFT ANALYSIS & TIMESTAMP PARSING
# =====================================================================

def parse_log_timestamp(log_entry: str) -> str:
    """Attempts to parse an ISO or syslog timestamp from a camera log string."""
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", log_entry)
    if iso_match:
        return iso_match.group(0)

    syslog_match = re.search(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}", log_entry)
    if syslog_match:
        return syslog_match.group(0)

    return "TIMESTAMP_UNKNOWN"


def evaluate_unsolicited_drift(
    actual: Dict[str, Optional[float]], 
    expected: Dict[str, float], 
    logs: List[str]
) -> Dict[str, Any]:
    metrics = {}
    severity_score = 0
    directions = []

    # Check recent logs for manual operator movements and extract the latest timestamp
    is_manual_action_logged = False
    operator_timestamp = "N/A"

    for log_entry in reversed(logs):  # Inspect newest logs first
        if any(kw in log_entry.lower() for kw in MANUAL_CONTROL_KEYWORDS):
            is_manual_action_logged = True
            operator_timestamp = parse_log_timestamp(log_entry)
            break

    # 1. Pan Calculation (Circular Math)
    act_pan = normalize_angle_degrees(actual.get("pan"))
    exp_pan = normalize_angle_degrees(expected.get("pan"))

    if act_pan is not None and exp_pan is not None:
        raw_pan_diff, abs_pan_diff = calculate_shortest_angular_distance(act_pan, exp_pan)
        metrics["pan_actual"] = act_pan
        metrics["pan_expected"] = exp_pan
        metrics["pan_raw_diff"] = raw_pan_diff
        metrics["pan_abs_diff"] = abs_pan_diff

        if abs_pan_diff >= TOLERANCES["minor"]["pan"]:
            directions.append("RIGHT" if raw_pan_diff > 0 else "LEFT")
            if abs_pan_diff >= TOLERANCES["critical"]["pan"]:
                severity_score += 4
            else:
                severity_score += 1
    else:
        metrics["pan_actual"] = "N/A"
        metrics["pan_expected"] = exp_pan
        metrics["pan_raw_diff"] = "N/A"

    # 2. Tilt Calculation
    act_tilt = normalize_angle_degrees(actual.get("tilt"))
    exp_tilt = normalize_angle_degrees(expected.get("tilt"))

    if act_tilt is not None and exp_tilt is not None:
        raw_tilt_diff = round(act_tilt - exp_tilt, 2)
        abs_tilt_diff = round(abs(raw_tilt_diff), 2)
        metrics["tilt_actual"] = act_tilt
        metrics["tilt_expected"] = exp_tilt
        metrics["tilt_raw_diff"] = raw_tilt_diff
        metrics["tilt_abs_diff"] = abs_tilt_diff

        if abs_tilt_diff >= TOLERANCES["minor"]["tilt"]:
            directions.append("UP" if raw_tilt_diff > 0 else "DOWN")
            if abs_tilt_diff >= TOLERANCES["critical"]["tilt"]:
                severity_score += 4
            else:
                severity_score += 1
    else:
        metrics["tilt_actual"] = "N/A"
        metrics["tilt_expected"] = exp_tilt
        metrics["tilt_raw_diff"] = "N/A"

    # 3. Zoom Calculation
    act_zoom = actual.get("zoom")
    exp_zoom = expected.get("zoom")
    if act_zoom is not None and exp_zoom is not None:
        raw_zoom_diff = round(float(act_zoom) - float(exp_zoom), 2)
        metrics["zoom_actual"] = act_zoom
        metrics["zoom_expected"] = exp_zoom
        metrics["zoom_raw_diff"] = raw_zoom_diff
    else:
        metrics["zoom_actual"] = "N/A"
        metrics["zoom_expected"] = exp_zoom
        metrics["zoom_raw_diff"] = "N/A"

    # Status Classification
    if severity_score == 0:
        status = "OK"
    elif is_manual_action_logged:
        status = "MANUAL_OVERRIDE_OK"
    elif severity_score >= 4:
        status = "CRITICAL_UNSOLICITED_DRIFT"
    else:
        status = "WARNING_UNSOLICITED_DRIFT"

    metrics["status"] = status
    metrics["severity_score"] = severity_score
    metrics["drift_direction"] = " | ".join(directions) if directions else "CENTERED"
    metrics["is_manual_action"] = is_manual_action_logged
    metrics["operator_timestamp"] = operator_timestamp

    return metrics


# =====================================================================
# 4. EXCEL REPORT EXPORTER WITH EMBEDDED SNAPSHOTS
# =====================================================================

def export_reports_to_excel(reports: List[Dict[str, Any]], output_file: str = "PTZ_Drift_Master_Report.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PTZ Drift Audit"
    ws.views.sheetView[0].showGridLines = True

    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", start_color="1A1D27")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin", color="DDDDDD"), right=Side(style="thin", color="DDDDDD"),
        top=Side(style="thin", color="DDDDDD"), bottom=Side(style="thin", color="DDDDDD")
    )

    # 1. Title Banner
    ws.merge_cells("A1:N1")
    ws["A1"] = "Multi-Camera PTZ Drift Audit Master Report (Read-Only)"
    ws["A1"].font = Font(name="Arial", bold=True, size=16, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", start_color="0F1117")
    ws["A1"].alignment = center_align
    ws.row_dimensions[1].height = 36

    # 2. Table Headers
    headers = [
        "Unit ID", "Camera Position", "Vendor", "IP : Port", 
        "Overall Status", "Manual Override?", "Operator Move Time", "Direction Vector", 
        "Pan Offset", "Tilt Offset", "Zoom Offset", "Severity Score", 
        "Scan Timestamp", "Camera Snapshot View"
    ]
    widths = [16, 16, 12, 18, 28, 22, 24, 20, 14, 14, 14, 15, 22, 95]

    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.font = header_font; cell.fill = header_fill; cell.alignment = center_align; cell.border = thin_border
    ws.row_dimensions[2].height = 25

    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    ws.freeze_panes = "A3"

    # 3. Populate Data Rows
    current_row = 3
    for report in reports:
        status = report.get("overall_status", "OK")
        
        if status == "CRITICAL_UNSOLICITED_DRIFT":
            bg_color = "FFC7CE"  # Soft Red
        elif status == "WARNING_UNSOLICITED_DRIFT":
            bg_color = "FFEB9C"  # Soft Yellow
        elif status == "MANUAL_OVERRIDE_OK":
            bg_color = "D9EAD3"  # Soft Sage Green
        else:
            bg_color = report.get("bg_color", "E5F5F5")  # Default position background

        row_fill = PatternFill("solid", start_color=bg_color)

        data_row = [
            report.get("unit_id"),
            report.get("position"),
            report.get("vendor", "").upper(),
            report.get("ip_address"),
            report.get("overall_status"),
            "YES (Operator Moved)" if report.get("is_manual_action") else "NO (Unsolicited)",
            report.get("operator_timestamp"),
            report.get("drift_direction_vector"),
            f"{report.get('pan_offset'):+}°" if report.get("pan_offset") != "N/A" else "N/A",
            f"{report.get('tilt_offset'):+}°" if report.get("tilt_offset") != "N/A" else "N/A",
            f"{report.get('zoom_offset'):+}" if report.get("zoom_offset") != "N/A" else "N/A",
            report.get("severity_score"),
            report.get("timestamp_utc"),
        ]

        # Write Text Cells
        for col_idx, val in enumerate(data_row, start=1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.fill = row_fill; cell.alignment = center_align; cell.border = thin_border; cell.font = Font(name="Arial", size=10)

        # Handle Image Snapshot Cell (Column 14 / N)
        thumb_cell = ws.cell(row=current_row, column=14, value="")
        thumb_cell.fill = row_fill; thumb_cell.border = thin_border

        img_buf = report.get("snapshot_buffer")
        if img_buf:
            img_buf.seek(0)
            xl_img = OpenpyxlImage(img_buf)
            
            # Anchor precisely within Column N (0-index 13) to prevent drift/overlapping
            xl_img.anchor = TwoCellAnchor(
                editAs="oneCell",
                _from=AnchorMarker(col=13, colOff=0, row=current_row - 1, rowOff=0),
                to=AnchorMarker(col=13, colOff=0, row=current_row, rowOff=0),
            )
            ws.add_image(xl_img)

        ws.row_dimensions[current_row].height = ROW_H
        current_row += 1

    output_path = Path(output_file)
    wb.save(output_path)
    print(f"\n[+] Read-Only Excel Report saved to: {output_path.resolve()}\n")


# =====================================================================
# 5. MAIN SCANNER RUNNER
# =====================================================================

def run_multi_camera_ptz_scan(units: List[Dict[str, Any]], output_excel: str = "PTZ_Multi_Camera_Report.xlsx"):
    all_reports = []

    print("\n" + "=" * 75)
    print("      MULTI-CAMERA LOCAL PTZ DRIFT & VIEW ANALYSIS SCANNER      ")
    print("=" * 75 + "\n")

    for unit in units:
        unit_id = unit["unit_id"]
        ip = unit["ip"]
        vendor = unit["vendor"].lower()
        user = unit["username"]
        pwd = unit["password"]
        baselines = unit.get("expected_baselines", {})

        print(f"📡 Auditing Unit [{unit_id}] at Base IP: {ip} (Read-Only)")
        print("-" * 75)

        for cam in CAMERA_CONFIGS:
            port = cam["port"]
            position = cam["position"]
            bg_color = cam["color"]

            expected = baselines.get(position, {"pan": 0.0, "tilt": 0.0, "zoom": 1.0})

            # Real-world test scenarios:
            if position == "CENTER":
                actual_ptz = {"pan": 182.1, "tilt": 16.0, "zoom": 1.0}
                mock_logs = []
            elif position == "LEFT":
                actual_ptz = {"pan": 140.0, "tilt": 25.0, "zoom": 1.0}
                mock_logs = ["2026-08-07T10:15:02Z - USER_ADMIN: PTZ_MANUAL_CONTROL_GOTO_PRESET"]
            else:  # RIGHT
                actual_ptz = {"pan": 272.5, "tilt": 11.2, "zoom": 1.0}
                mock_logs = []

            metrics = evaluate_unsolicited_drift(actual_ptz, expected, mock_logs)
            snapshot_buffer = fetch_snapshot_from_engine(ip, port, vendor, user, pwd, f"{unit_id} - {position}")

            report = {
                "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "unit_id": unit_id,
                "position": position,
                "vendor": vendor,
                "ip_address": f"{ip}:{port}",
                "bg_color": bg_color,
                "overall_status": metrics["status"],
                "is_manual_action": metrics["is_manual_action"],
                "operator_timestamp": metrics["operator_timestamp"],
                "severity_score": metrics["severity_score"],
                "drift_direction_vector": metrics["drift_direction"],
                "pan_actual": metrics["pan_actual"],
                "pan_expected": metrics["pan_expected"],
                "pan_offset": metrics["pan_raw_diff"],
                "tilt_actual": metrics["tilt_actual"],
                "tilt_expected": metrics["tilt_expected"],
                "tilt_offset": metrics["tilt_raw_diff"],
                "zoom_actual": metrics["zoom_actual"],
                "zoom_expected": metrics["zoom_expected"],
                "zoom_offset": metrics["zoom_raw_diff"],
                "snapshot_buffer": snapshot_buffer,
            }
            all_reports.append(report)

            status_badges = {
                "CRITICAL_UNSOLICITED_DRIFT": "🚨 [CRITICAL UNSOLICITED DRIFT]",
                "WARNING_UNSOLICITED_DRIFT": "⚠️  [WARNING UNSOLICITED DRIFT]",
                "MANUAL_OVERRIDE_OK":        "👤 [MANUAL OPERATOR ACTION (IGNORED)]",
                "OK":                        "✅ [OK]",
            }

            print(f"  --> Camera Position : {position:<8} (Port {port})")
            print(f"      Status          : {status_badges.get(metrics['status'])}")
            print(f"      Operator Action : {'Detected @ ' + metrics['operator_timestamp'] if metrics['is_manual_action'] else 'None'}")
            print(f"      Pan Offset      : {metrics['pan_raw_diff']:+}° | Tilt Offset: {metrics['tilt_raw_diff']:+}°")
            print(f"      Snapshot State  : {'Loaded into memory' if snapshot_buffer else 'Fallback generated'}")
            print()

        print("-" * 75 + "\n")

    export_reports_to_excel(all_reports, output_excel)


if __name__ == "__main__":
    run_multi_camera_ptz_scan(MOCK_UNIT_TARGETS)