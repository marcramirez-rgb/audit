# Audit Flow — FastAPI Endpoint Spec

Part of the internal-hosting blueprint. Wraps the existing **audit** engine
(`camera_engine.run_batch`) as a browser-accessible web service. See
`writer-api-spec.md` for the analytics-writer side.

## Engine contract this maps to (unchanged)
- `run_batch(camera_rows, credentials, output_dir, base_filename, log_cb=None, progress_cb=None)` → returns a `Path` to the written `.xlsx`.
- `camera_rows`: list of dicts `{CLIENT_NM, LOCATION_NM, LIVE_UNIT_SERIAL_NM, IP, MANUFACTURER}`.
- `credentials`: `{AXIS_USER, AXIS_PASS, HIK_USER, HIK_PASS}` (only the vendors actually used are needed).
- helpers: `dedupe_camera_rows()`, `classify_manufacturer()` → `AXIS | HIKVISION | MIXED | None`.
- **`log_cb(str)` and `progress_cb(done, total)` are the hooks the web layer streams from.**

**No changes to `camera_engine.py` required.** The web layer is a thin adapter + job bookkeeping.

## Design decisions (read first)
1. **Async jobs, not blocking requests.** A fleet audit runs a thread pool over many cameras and takes minutes; a synchronous `POST` will hit gateway/browser timeouts. Client starts a job, streams progress, then downloads the report. Single-camera diagnostics use the same model for consistency.
2. **Credentials never touch the URL, logs, or disk.** POST body over TLS only, held in the in-memory job record for the run's duration, zeroed on completion. Long term, pull from a server-side secret store / per-user vault instead of accepting them in the request.
3. **CSV becomes an upload or a JSON row array** — no server file paths. `default_output_dir()` (writes to `~/Downloads`) is **replaced** by a per-job temp dir; the file is served via a download endpoint and reaped on a TTL.
4. **Every endpoint sits behind SSO** and records the authenticated user on the job (feeds the mandatory audit trail).
5. **Cap concurrent jobs** (semaphore) so a few big fleet runs can't exhaust the camera-reachable host.

## Pydantic models
```python
from enum import Enum
from pydantic import BaseModel, Field, IPvAnyAddress

class Manufacturer(str, Enum):
    axis = "axis"; hikvision = "hikvision"; lvt = "lvt"; mixed = "mixed"

class CameraRow(BaseModel):
    ip: IPvAnyAddress
    manufacturer: Manufacturer
    client_nm: str | None = None
    location_nm: str | None = None
    live_unit_serial_nm: str | None = None
    # -> engine keys IP / MANUFACTURER / CLIENT_NM / LOCATION_NM / LIVE_UNIT_SERIAL_NM

class Credentials(BaseModel):
    axis_user: str | None = None
    axis_pass: str | None = Field(None, repr=False)   # repr=False keeps it out of tracebacks/logs
    hik_user:  str | None = None
    hik_pass:  str | None = Field(None, repr=False)

class AuditStartRequest(BaseModel):
    rows: list[CameraRow]                 # single-camera = a 1-element list
    credentials: Credentials
    tag: str | None = None                # -> base_filename

class JobStatus(str, Enum):
    queued = "queued"; running = "running"; done = "done"; error = "error"

class JobState(BaseModel):
    job_id: str
    status: JobStatus
    done: int = 0                         # from progress_cb
    total: int = 0
    message: str | None = None            # last log line / error summary
    report_ready: bool = False
```

## Endpoints

### `POST /api/audit/jobs` — start an audit
- Body: `AuditStartRequest`.
- Before accepting: `classify_manufacturer` each row → **422** if any is `None` (unrecognized vendor — don't guess, mirror the CLI); run `dedupe_camera_rows`; derive `needs_axis`/`needs_hik` and **422** if a required credential set is missing. Front-loads the CLI's validation so the client gets a clean error instead of a half-finished report.
- Launches `run_batch(...)` in a background task with `log_cb`/`progress_cb` wired to the `JobState`, writing to a per-job temp dir.
- **202** → `{ "job_id": "...", "status": "queued" }`.

### `GET /api/audit/jobs/{job_id}` — poll status
- **200** → `JobState`. Fallback for clients that don't do SSE.

### `GET /api/audit/jobs/{job_id}/events` — live progress (SSE)
- `text/event-stream`. Emits a `progress` event per `progress_cb(done, total)` and a `log` event per `log_cb` line; a final `done`/`error` event closes the stream. Direct pipe from the two callbacks the engine already exposes.

### `GET /api/audit/jobs/{job_id}/report` — download the `.xlsx`
- **200** `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment` with the engine's filename (`{base}_Master_{timestamp}.xlsx`).
- **409** if status ≠ `done`; **404** if reaped.

### `POST /api/audit/parse-csv` *(optional convenience)*
- multipart CSV upload → parsed/validated `list[CameraRow]`, reusing the CLI's `load_csv_rows` checks (requires an `IP` column, warns on blank-IP rows). Lets the browser preview + edit rows before starting a job. The Snowflake unit lookup would feed the same `rows` array.

## Engine adapter (the only real glue)
```python
def run_job(job: JobState, rows: list[CameraRow], creds: Credentials, tag: str | None):
    import camera_engine
    camera_rows = [to_engine_row(r) for r in rows]          # snake_case -> ENGINE_KEYS
    camera_rows = camera_engine.dedupe_camera_rows(camera_rows)
    credentials = to_engine_creds(creds)                    # -> AXIS_USER/.../HIK_PASS
    with tempfile.TemporaryDirectory() as out_dir:
        path = camera_engine.run_batch(
            camera_rows, credentials, Path(out_dir),
            base_filename=(tag or default_base_name(rows)),
            log_cb=lambda m: job.push_log(m),
            progress_cb=lambda d, t: job.set_progress(d, t),
        )
        stash_report(job.job_id, path)                      # move out of temp dir before it's cleaned
    creds_wipe(credentials)
```

## Notes to the dev team
- **Interactive credential prompts don't apply here.** `run_audit.py` prompts via `getpass`; the web path supplies `credentials` directly. Reuse the *validation* and *row-building* logic, not the CLI I/O.
- **`'mixed'` and `'lvt'` must survive the mapping:** `lvt`→hikvision-compatible, `mixed`→probe both vendors per port. Keep them first-class `Manufacturer` values.
- **Job store:** in-memory dict is fine for a single-process pilot. For multiple workers, move job state + SSE fan-out to Redis, or pin audit jobs to one worker.
- **Reaping:** delete stashed reports on a TTL (~1h) so the host doesn't accumulate `.xlsx` files.
