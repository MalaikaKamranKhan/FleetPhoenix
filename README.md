# FleetPhoenix — AGxARIZONA Track 2

An interactive, **multi-agent orchestration** proof of concept for Track 2, with a local-network QR judge controller. The tractor movements are simulated; no physical tractor is ever controlled.

**What we built (not just planned):**
- An **Orchestrator** coordinating discrete Monitor, Planner, Recovery, Memory and optional LLM Advisor agents.
- An independent **Constraint Guard** that is the only component authorized to commit simulated assignments.
- **Directed A2A messages** with message count, payload bytes, and suppression of idle planning chatter, all visible on the dashboard.
- Persistent **incident memory** at `runtime/agent_memory.jsonl`; completed work remains authoritative in the active simulator.
- An **LLM advisor** supporting a bring-your-own OpenAI-compatible chat-completions endpoint. It analyzes tradeoffs but never executes machine actions or changes safety rules.
- Judge-controlled **6-minute delayed telemetry**, signal loss, safe-stop confirmation, breakdown, battery failure, and new-operator-job scenarios.

**Important integration boundary:** AgOpenWeb is an actual, separately installed open-source guidance app. We link to it at `http://localhost:5174` when installed, but this prototype does **not** claim a working AgOpenWeb API integration. See `AGOPENWEB_INTEGRATION.md` for a truthful adapter plan.

**Laptop shows:** four tractors, field coverage, operator jobs, time-stamped decisions, explanation, compatibility/energy/dependency checks, and live QR code.

**Judge phone can:** start/pause/reset simulation, pick any tractor to fail, make it lose connectivity, simulate a 6-minute network delay, explicitly confirm a safe stop, reconnect/repair it, drain/recharge its battery, and submit a surprise job.

## Windows PowerShell — run it

1. Install Python 3.10 or later if needed.
2. Extract this ZIP. Open PowerShell inside the `FleetPhoenix` folder (Shift + right-click in File Explorer, **Open in Terminal**).
3. Run:

   ```powershell
   py -m pip install -r requirements.txt
   py app.py
   ```

   If `py` is unavailable but `python` works, use `python -m pip ...` and `python app.py`.
4. On your laptop browser open **http://localhost:5000**. The QR code and judge link appear on the right side of the dashboard.
5. Connect the judge's phone to the **same Wi-Fi network or phone hotspot** as the laptop, then scan the QR. A Windows Firewall prompt may require allowing Python on **private networks**.
6. Press **Start simulation** on laptop or **Start time** on judge phone. One real-world second = one simulated minute.

## Important: If phone scanning fails

- Make sure both devices are on a reachable network. Many university/guest Wi-Fi networks **block device-to-device communication**. Prefer your own phone hotspot (connect laptop to phone hotspot; the same hotspot phone can sometimes access laptop, but a second phone is more reliable).
- Run `ipconfig` in PowerShell; find your laptop's Wi-Fi **IPv4 Address** (e.g., `192.168.1.47`).
- If the QR advertises a wrong IP, stop the program (Ctrl+C) and restart with:

  ```powershell
  $env:FLEET_HOST_IP = '192.168.1.47'   # replace with your actual IPv4 address
  py app.py
  ```

- From the phone try `http://YOUR_LAPTOP_IP:5000`. If unreachable, check Windows Firewall rules for Python and private network profile.
- **Offline fallback:** Open the dashboard and judge controller in *two browser windows on the same laptop*. The `Judge controller` button opens a token-protected controller in a new tab. This still demonstrates live synchronization.
- The QR and admin information endpoints are restricted to the laptop loopback address; open the dashboard as `http://localhost:5000`, not by its LAN IP. Do **not** share your judge QR publicly. Anyone with its short-lived controller link can change your simulated farm. A fresh token is created every time you restart the app.

## Suggested judge demo (under 3 minutes)

1. Click **Reset**. Point out the initial job list, battery and implement constraints. Click **Start**; let work progress for 6–8 seconds.
2. Judge presses **Lose connection** on an actively working tractor. See **SIGNAL LOST**; its task is **RESERVED**, not given to another tractor because its physical state is unknown.
3. Judge presses **Confirm safely stopped**. The original progress is preserved and only the remaining work is offered to compatible, sufficiently charged tractors.
4. Judge then chooses a *different* active tractor and presses **Confirm breakdown**. The scheduler reacts again; event log and explanation update automatically.
5. Judge submits a new urgent **plowing** job. Show whether it's assigned, queued, or prevented by incompatible implements/battery.
6. Point to the zero-constraint-violations metric, event log, and field coverage progress. If your judges prefer the scenario on the official slide, use **Confirm breakdown** first and show successful reassignment.

## Bring your own LLM (optional, real API call)

The project runs offline without a model. If you have an OpenAI-compatible endpoint (for example, a locally running Ollama compatible API or another provider), set three environment variables *before* `py app.py` in PowerShell:

```powershell
$env:FLEET_LLM_ENDPOINT = 'http://localhost:11434/v1/chat/completions'
$env:FLEET_LLM_MODEL = 'llama3.2'   # an example model; must already be available in your provider
$env:FLEET_LLM_API_KEY = ''         # provider-specific; keep secrets out of source control
py app.py
```

Alternatively, set your compatible provider's `https://.../v1/chat/completions` endpoint, actual model name, and API key. The button **Ask advisor to analyze fleet** becomes available in the laptop dashboard. It sends compact sanitized fleet state (tractor IDs, batteries, jobs, statuses) to the configured provider. **Do not use a cloud endpoint with private farm telemetry without permission.** The advisor response is *advisory only*; the Planner and Guard still own assignments. No external LLM is connected unless you configure it.

The UI shows whether the model is connected, thinking, offline, or errored. If the provider is inaccessible, the deterministic agents continue operating.

## The agents and their actual tools

| Role | Trigger | Tool or outcome |
|---|---|---|
| Monitor | Breakdown, lost signal, delay, recovery | Classify known stop vs unknown state |
| Orchestrator | Operator request or status event | Send short, directed messages to specialists |
| Recovery | Monitor observation | Decide whether unfinished work may be released or must remain reserved |
| Planner | Free eligible tractor and pending job | Propose compatible assignments by priority/deadline/energy/distance |
| Constraint Guard | Proposed assignment | Validate implements, battery, dependencies, one job per tractor |
| Memory | Incident or operator verification | Record compact facts in durable JSONL incident history |
| Advisor | User presses request button and BYO LLM is configured | Summarize schedule trade-offs, with **no actuator authority** |

**Efficiency demo:** The agent mesh skips message emissions during idle planning cycles and sends compact event payloads only to relevant specialist roles. This is evidence of reduced application-level message traffic, **not** a measured LLM token or dollar-cost reduction.

## Technical approach

- Python 3 standard-library `ThreadingHTTPServer`, with no Flask or external services.
- `qrcode[pil]` is the only Python package required. All HTML/CSS/JS are bundled. No CDN.
- Single simulation engine is authoritative for field progress and assignments. Specialized agents communicate through an explicit directed message log. Phone and laptop poll its REST API and stay synchronized.
- Agentic Planner + independent Constraint Guard: deterministic heuristic scheduling with hard constraints on implement compatibility, available battery, job dependency, and exclusive assignment; chooses the lowest-cost eligible tractor based on a simple score for deadline lateness, travel, duration, and energy. This is **not an optimal MILP solver**.
- Connectivity loss is **not treated as evidence of a physical stop**. The job remains reserved and progress is frozen in the simulation until the operator confirms a safe stop or communication is restored.
- After confirmed breakdowns, jobs resume from the last recorded progress instead of restarting from zero.
- User-facing explanations and timestamped event log are generated from the actual scheduling state, not invented by an LLM.
- Dashboard shows the number of internal assignment-invariant violations; it is not a certified vehicle-safety system.
- Field-progress and active assignments are simulated and held in memory: restarting resets the active run. Incident memories are persisted locally in JSONL. No real hardware control, navigation, collision avoidance, or physical verification is implemented.

## Team extensions, if time permits

1. Add a proper constraint optimization solver (OR-Tools CP-SAT) and show objective-value comparisons.
2. Add field-specific route reservations; this proof of concept enforces one-job-per-tractor and one-tractor-per-job, not geometric collision avoidance.
3. Upgrade the timed delay demo to a real queued transport layer with sequence numbers, acknowledgements, retry budgets, and a proper out-of-order network simulator.
4. Add a replay of all timed events and alternative recovery schedules.
5. Add a CSV export of completed field segments, or persist full fleet state to SQLite (the current trace is downloadable as JSON).
6. Integrate with AgOpenWeb *only if you have compatible data interfaces and sufficient testing time*; it is optional for the hackathon and is not integrated in this build.

## Run tests

```powershell
py -m unittest discover -p test_engine.py -v
```

Only Python's standard library is needed for the tests.
