# AgOpenWeb integration: honest scope and next technical steps

## Current status
This standalone FleetPhoenix prototype is a working **fleet agent orchestration simulator**, **not** a modified AgOpenWeb build.

**What is connected (built and verified with AgOpenWeb 26.7.6 on macOS):** a **read-only GPS feed**. `agopenweb_bridge.py` sends standard NMEA GPS sentences (`$GPGGA` position, `$GPVTG` course and speed, checksummed) for **one** simulated FleetPhoenix tractor to AgOpenWeb on UDP 9999, four times a second. With AgOpenWeb's own simulator switched off, AgOpenWeb reports *RTK Fixed* and draws that tractor driving its passes over the "FleetPhoenix Farm" field. By default the feed follows the work: it starts on the tractor plowing North Ridge, switches to whichever tractor takes over a job after a breakdown, and moves on to the follow-up job when one finishes. If the followed tractor loses signal, FleetPhoenix stops sending, and AgOpenWeb shows "GPS data lost".

**What is not connected:** FleetPhoenix sends **no commands** to AgOpenWeb (no steering, sections, jobs or settings), and reads nothing back from it. AgOpenWeb shows one vehicle at a time. We have not found or used any official AgOpenWeb command API. When judging, say "FleetPhoenix feeds simulated GPS for one tractor into the official AgOpenWeb guidance view", not "FleetPhoenix controls AgOpenWeb".

### Run it
1. Create a dedicated AgOpenWeb profile (it switches AgOpenWeb's simulator off and adds the field; your normal AgOpenWeb settings are untouched):
   `python3 agopenweb_setup.py agopenweb-data`
2. Start AgOpenWeb with that profile. macOS: `AGOPENWEB_DATA="$PWD/agopenweb-data" /Applications/AgOpenWeb.app/Contents/MacOS/AgOpenWeb.Desktop --headless`. Windows: set the `AGOPENWEB_DATA` environment variable to that folder and start AgOpenWeb.
3. Open `http://localhost:5174`, then **Field Operations → Fields and Jobs → FleetPhoenix Farm → Open Field Only**.
4. Start FleetPhoenix (`python3 app.py`). The banner shows the AgOpenWeb feed line, and the dashboard footer shows "sending simulated GPS for …" once AgOpenWeb is found. Use **Show in AgOpenWeb** to pick a tractor, or leave it on Auto.

Settings: `FLEET_AGOPENWEB=off` disables the feed; `FLEET_AGOPENWEB_URL` and `FLEET_AGOPENWEB_UDP` change where it looks and sends. The simulation runs 60 times faster than real time, so the reported speed is scaled down to a real-world equivalent.

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
