# Internal Hosting — Design Docs

Blueprint + API specs for hosting the camera **audit** and **analytics writer**
tools as a browser-accessible internal app (no local Python install / .bat
launchers). Handoff material for the dev team; nothing here is built yet.

- [`audit-api-spec.md`](audit-api-spec.md) — FastAPI spec for the read-only audit flow (async job model over `camera_engine.run_batch`, SSE progress, `.xlsx` download).
- [`writer-api-spec.md`](writer-api-spec.md) — FastAPI spec for the analytics writer (session + per-camera lease model over `vendor_adapter`; browser canvas does the drawing).
- [`writer-canvas-dataflow.md`](writer-canvas-dataflow.md) — browser-side sketch for the writer canvas: fraction-only coordinate model, draw/drag interactions, `ScenarioDTO` assembly.

## Hard constraint (applies to both)
The host **must sit on a network segment that can route to the camera IPs.** The
tools open direct HTTP sessions to each camera; a cloud box or wrong-VLAN host
reaches nothing. Confirm reachability before writing code.

## Cross-cutting (both specs)
- Behind LVT SSO; camera credentials over TLS only, never in URLs/logs/disk (prefer a server-side vault).
- **Per-write audit trail is a launch requirement** in a shared hosted context, not optional.
- Containerize with pinned `requirements.txt` — also kills the corporate-SSL/pip onboarding pain for good.

## Suggested sequencing
1. Confirm camera-network reachability + pick the host.
2. Ship the audit flow first (it's form-in / Excel-out — the simpler spec).
3. Build the writer canvas + endpoints (the harder frontend work).
4. Bake in the user-scoped audit trail before enabling writes broadly.
5. Fold in combined audit+writer and the Snowflake unit lookup.
