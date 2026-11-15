# AgOpenWeb integration: honest scope and next technical steps

## Current status
This standalone FleetPhoenix prototype is a working **fleet agent orchestration simulator**, **not** a modified AgOpenWeb build. The official AgOpenWeb app can run separately at `http://localhost:5174`, and the FleetPhoenix dashboard links to that URL. We **have not verified** a stable external HTTP or WebSocket API for commanding simulated tractor operations in the current AgOpenWeb release. Do not say the two applications are connected during judging unless you build and validate that bridge.

Upstream: https://github.com/AgOpenGPS-Official/AgOpenWeb
Releases: https://github.com/AgOpenGPS-Official/AgOpenWeb/releases

AgOpenWeb uses a .NET 10 host with a web client. The public repository describes a single-writer GPS-cycle architecture: UI issues intents, the pipeline processes them, and it emits state snapshots. A proper integration must respect that execution model, never mutate UI state directly.

## Suggested compatible bridge contract (our own design; **not** an AgOpenWeb official API)

Incoming **read-only** telemetry to an adapter:

```json
{"source":"AgOpenWeb","tractor_id":"T1","sequence":47,"simulated":true,"connected":true,"position":{"x_m":30,"y_m":18},"battery_percent":64,"completed_work_units":17,"timestamp_utc":"2026-11-15T19:00:00Z"}
```

Outgoing **simulated task intent** after Guard verification:

```json
{"action":"assign_simulated_task","tractor_id":"T1","job_id":"J2","field":"East Orchard","implement":"irrigate","lease_revision":9,"simulated":true}
```

These are **proposed JSON shapes** for your custom adapter, not demonstrated AgOpenWeb features.

## Integration steps for the team

1. Launch the official AgOpenWeb Windows build and verify its simulator works. Do not install or test with real tractors.
2. Inspect upstream code/maintainer guidance for supported integration methods and simulated machine state events.
3. Implement a **read-only state adapter** first (position, field/coverage, simulated telemetry). Check monotonic sequence numbers and reject stale messages.
4. Expose an intent queue to the simulator only if an official supported command path is documented; never reach directly into its cycle-worker state.
5. Keep the orchestration layer independent of GPS steering: assign jobs, track progress and incidents, and recover from failures; let AgOpenWeb handle guidance visualization.
6. Make the judge's QR controller operate the fleet orchestration layer; when it injects failure, use the simulator's supported method if available. Otherwise label it as an orchestration-only simulated failure.

## Separate scoreable features you can confidently demonstrate now

- Tool-based assignment constraints and dependency-aware job list
- Agent-to-agent message trace showing which agent made which decision
- Partial job preservation during a confirmed breakdown
- Network-unknown state and exclusive lease held until verified stop
- Auto-resolving injected telemetry delay
- Persistent incident memory
- BYO LLM advisory explanation with deterministic fallback
- Judge QR controller and laptop visualization
