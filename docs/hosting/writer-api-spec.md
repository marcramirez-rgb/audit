# Analytics Writer Flow — FastAPI Endpoint Spec

Part of the internal-hosting blueprint. Wraps the existing **analytics writer**
engine (`vendor_adapter` + `aoa_config` / `hik_config`) as a browser-accessible
web service. See `audit-api-spec.md` for the read-only audit side.

This is the harder of the two: the desktop writer is stateful and interactive
(connect → snapshot → draw on canvas → push to camera). The split for the web
version: **drawing happens client-side in the browser; the server does
connect / snapshot / read / apply / backup-restore.**

## Engine contract this maps to (unchanged)
- `make_adapter(vendor, ip, port, user, password, channel=None)` → an `AxisAdapter` or `HikAdapter` with a uniform interface:
  - `fetch_snapshot()` → PIL `Image` (live JPEG frame).
  - `read_scenarios()` → `list[Scenario]` (current config, vendor-neutral).
  - `apply_scenario(sc, backup_dir)` → `(backup_path, new_config)`. Creates or edits-in-place (by `native_id`); **auto-backs-up before every write**.
  - `.capabilities` → `Capabilities` describing what this vendor can write.
- `capabilities_for(vendor)` → `Capabilities` without connecting.
- `Scenario` (vendor-neutral DTO): `name, kind ("intrusion"|"line"|"loiter"), points ([0,1] top-left fractions), classes, duration, direction, exclusions, native_id, min_size, max_size, perspective`.
- `aoa_config.validate_scenario()` and `vendor_adapter._require(caps, sc)` reject unsupported geometry/kind/class per vendor.

**Key win:** points are already `[0,1]` fractions, so the browser canvas speaks
fractions directly — **no server-side pixel math** (`pixel_to_norm`/`norm_to_pixel`
were Tkinter-canvas helpers and aren't needed in the web version). `Scenario`
serializes almost 1:1 to a JSON DTO.

## Design decisions (read first)
1. **Session-oriented, not job-oriented.** Unlike audit (fire-and-forget batch), the writer holds a live authenticated HTTP session to one camera across several calls (snapshot, read, repeated applies). Model it as a server-side **writer session** keyed by `session_id`, holding the adapter instance, with a TTL.
2. **One writer per camera at a time (lock/lease).** The blueprint requires serializing writes per camera so two users don't clobber one camera's config. Creating a session takes a lease on that camera IP; **409** if already leased. Lease auto-expires on TTL / released on session close.
3. **Capabilities drive the UI.** Return `capabilities` on connect so the frontend enables/disables tools per vendor (e.g. Hik: `can_delete=false`, `exclusions=false`; Axis: full replace, perspective, exclusions). Don't hardcode vendor rules in the frontend — read them from the response.
4. **Snapshot served as an image, dimensions in JSON.** `fetch_snapshot()` returns a PIL image; serve it from a dedicated `image/png` endpoint (cacheable, avoids bloating JSON). The canvas overlays fraction-space scenarios on top, so it only needs the aspect ratio.
5. **Audit trail is per-write and mandatory.** Every `apply_scenario` records `{user, camera_ip, vendor, scenario_name, kind, before/after or backup_path, timestamp}`. This is a launch requirement in a shared hosted context, not optional.
6. **Credentials:** POST body over TLS on connect only; held inside the adapter's live session for the session's life, never persisted or logged. Prefer a server-side vault long term.

## Pydantic models
```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, IPvAnyAddress

class Vendor(str, Enum):
    axis = "axis"; hikvision = "hikvision"

class ConnectRequest(BaseModel):
    vendor: Vendor
    ip: IPvAnyAddress
    port: int = 80
    channel: int | None = None            # Hik only
    user: str
    password: str = Field(..., repr=False)

class CapabilitiesDTO(BaseModel):
    kinds: list[str]; classes: list[str]
    multi_class: bool; exclusions: bool; direction: bool; can_delete: bool
    perspective: bool = False; size_boxes: bool = False; intrusion_duration: bool = False
    notes: str = ""

class ScenarioDTO(BaseModel):
    name: str
    kind: Literal["intrusion", "line", "loiter"]
    points: list[tuple[float, float]]                 # [0,1] top-left fractions
    classes: list[str] = ["human"]
    duration: int = 0                                 # loiter/dwell seconds
    direction: str | None = None                      # line only: leftToRight|rightToLeft
    exclusions: list[list[tuple[float, float]]] = []  # Axis only
    native_id: str | int | None = None                # set = edit-in-place; null = create new
    min_size: tuple[float, float, float, float] | None = None
    max_size: tuple[float, float, float, float] | None = None
    perspective: list[dict] | None = None             # Axis calibration bars

class SessionState(BaseModel):
    session_id: str
    vendor: Vendor
    ip: str
    image_w: int
    image_h: int
    capabilities: CapabilitiesDTO
    scenarios: list[ScenarioDTO]
```

## Endpoints

### `POST /api/writer/sessions` — connect to a camera
- Body: `ConnectRequest`. Server: acquire the per-IP lease (**409** if held by another user), `make_adapter(...)`, `fetch_snapshot()`, `read_scenarios()`.
- **201** → `SessionState` (session id + image dims + capabilities + current scenarios). The snapshot bytes are fetched separately (next endpoint).
- Errors: **401** on camera auth failure (`AOAAuthError`/Hik auth), **502** if the camera is unreachable, **422** on unknown vendor.

### `GET /api/writer/sessions/{id}/snapshot` — live frame
- **200** `image/png`. Re-calls `fetch_snapshot()` on demand (a "refresh snapshot" button). Consider a short server-side cache to avoid hammering the camera.

### `GET /api/writer/sessions/{id}/scenarios` — re-read current config
- **200** → `list[ScenarioDTO]`. Refresh after external changes.

### `POST /api/writer/sessions/{id}/scenarios` — create / edit a scenario
- Body: `ScenarioDTO`. `native_id` null → create; set → edit-in-place.
- Server: `validate_scenario` + `_require(caps, sc)` → **422** on unsupported kind/class/geometry for the vendor (e.g. loiter on Hik, exclusions on Hik). Then `apply_scenario(sc, backup_dir)` (auto-backup first). Write the audit-trail record.
- **200** → `{ "backup_id": "...", "scenarios": [ ...updated ScenarioDTO... ] }`.
- Serialize applies within a session (and the per-camera lease already prevents cross-user races).

### `GET /api/writer/sessions/{id}/backups` — list backups
- **200** → `[{ "backup_id", "created_at", "scenario_count" }]`. Each write produced one (from `apply_scenario`'s `backup_dir`).

### `POST /api/writer/sessions/{id}/restore` — one-click restore
- Body: `{ "backup_id": "..." }`. Restores that backup to the camera (mirrors the desktop writer's restore-from-backup). Records an audit-trail entry. **200** → refreshed `list[ScenarioDTO]`.

### `DELETE /api/writer/sessions/{id}` — close session
- Releases the per-camera lease and drops the adapter/credentials. **204**.

## Adapter glue (sketch)
```python
def open_session(req: ConnectRequest, user: str) -> SessionState:
    import vendor_adapter
    acquire_lease(str(req.ip), user)                        # 409 if held
    adapter = vendor_adapter.make_adapter(
        req.vendor.value, str(req.ip), req.port, req.user, req.password, channel=req.channel)
    img = adapter.fetch_snapshot()                          # PIL Image
    scenarios = adapter.read_scenarios()                    # list[Scenario]
    sid = new_session_id()
    SESSIONS[sid] = {"adapter": adapter, "user": user, "img": img, "ip": str(req.ip)}
    return SessionState(
        session_id=sid, vendor=req.vendor, ip=str(req.ip),
        image_w=img.width, image_h=img.height,
        capabilities=cap_to_dto(adapter.capabilities),
        scenarios=[scenario_to_dto(s) for s in scenarios],
    )

def apply(sid: str, dto: ScenarioDTO, user: str):
    sess = SESSIONS[sid]; adapter = sess["adapter"]
    sc = dto_to_scenario(dto)                               # ScenarioDTO -> vendor_adapter.Scenario
    aoa_config.validate_scenario_or_422(sc); vendor_adapter._require(adapter.capabilities, sc)
    backup_path, _new_cfg = adapter.apply_scenario(sc, backup_dir_for(sid))
    audit_log(user, sess["ip"], adapter.vendor, sc, backup_path)   # mandatory
    return {"backup_id": id_for(backup_path),
            "scenarios": [scenario_to_dto(s) for s in adapter.read_scenarios()]}
```

`scenario_to_dto` / `dto_to_scenario` are near-identity maps — `Scenario` is
already a plain dataclass of JSON-friendly fields.

## Notes to the dev team
- **The canvas is the real frontend work.** Reimplement the draw / drag-vertices interaction (HTML5 canvas or SVG). All geometry is `[0,1]` fractions, so it's resolution-independent: multiply by rendered image size for display, store fractions. The overlay/reorder math already lives in the engine — the browser only needs to collect vertices and POST a `ScenarioDTO`.
- **Vendor asymmetry is data, not code.** Read `capabilities` and gray out what a vendor can't do (Hik can't delete, no exclusions, no loiter yet). Don't fork the UI per vendor.
- **Edit-in-place hinges on `native_id`.** Populate it from `read_scenarios()` so an edit preserves perspective/presets/filters (Axis) instead of creating a duplicate.
- **Sessions + leases need a TTL reaper.** A user who closes the tab without `DELETE` must not lock a camera forever.
- **Same host constraint as everything else:** the server must be on a network that can route to the camera IPs.
