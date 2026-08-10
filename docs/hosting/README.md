# Internal Hosting — Design Docs

Blueprint + API specs for hosting the camera **audit** and **analytics writer**
tools as a browser-accessible internal app (no local Python install / .bat
launchers). Target is the existing **Edge Support Toolbox**, not a greenfield app.

## Concrete target (confirmed 2026-08-06)
- **Delivery:** Teleport **App Access** fronts internal web apps. Auth flow is
  Okta tile → Teleport → **Launch** the app → web page. Existing registered apps
  visible under a "tool box" search: `edge-support-tams-prd` (label `tams: true`)
  and `edge-support-ui-prd` (labels `ars-support: true`, `config-support: true`).
- **Host:** the Edge Support Toolbox host **reaches camera IPs directly** — the
  engine's direct HTTP sessions work as-is, no proxy/tunnel hop needed. (This is
  the old "hard constraint" below — now confirmed satisfied.)
- **Ownership:** Edge Support can contribute to the toolbox codebase → deliverable
  is code that matches the existing house style, not a handoff spec.
- **Auth / "who":** Teleport authenticates every user pre-Launch. That identity is
  the operator source the audit trail needs — the shared `root` camera account
  can't attribute changes, so identity is captured tool-side from Teleport. No SSO
  to build.
- **Placement:** **TAM section first.** The **audit** (read-only) tool lands in the
  TAM wedge / `edge-support-tams-prd`. The **writer** (config-write) maps later to
  the CONFIGURATION wedge / `edge-support-ui-prd` (`config-support: true`). This is
  the same audit-first, writer-second sequencing below.

- [`audit-api-spec.md`](audit-api-spec.md) — FastAPI spec for the read-only audit flow (async job model over `camera_engine.run_batch`, SSE progress, `.xlsx` download).
- [`writer-api-spec.md`](writer-api-spec.md) — FastAPI spec for the analytics writer (session + per-camera lease model over `vendor_adapter`; browser canvas does the drawing).
- [`writer-canvas-dataflow.md`](writer-canvas-dataflow.md) — browser-side sketch for the writer canvas: fraction-only coordinate model, draw/drag interactions, `ScenarioDTO` assembly.

## Hard constraint (applies to both) — CONFIRMED SATISFIED 2026-08-06
The host **must sit on a network segment that can route to the camera IPs.** The
tools open direct HTTP sessions to each camera; a cloud box or wrong-VLAN host
reaches nothing. The Edge Support Toolbox host reaches cameras directly, so this
is satisfied — kept here as the invariant to re-verify if the host ever moves.

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
