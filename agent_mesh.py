"""Explicit multi-agent orchestration layer for FleetPhoenix.

Agents communicate through a small directed message log. The orchestration layer is
simulation-only; actuator commands are deliberately absent. A deterministic
constraint guard is the only component authorized to commit simulated jobs.

Orchestration protocol for every replan (AgentMesh.replan):
  bid       one Tractor agent per machine bids in parallel, each under a timeout
  award     the Planner agent awards jobs from the bids
  verify    the Guard checks the whole proposal; violations go back to the Planner (2 rounds max)
  fallback  after 2 failed rounds, or while the Planner's circuit breaker is open,
            the deterministic fallback planner proposes instead, so the fleet never stops
  commit    the Guard alone commits, pair by pair, re-checking every hard constraint

LLM swap points: TractorAgent.bid and PlannerAgent.award. Replace their bodies with model
calls returning the same JSON shapes; timeouts, verification and fallback already apply.
"""
from __future__ import annotations
import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error

AGENT_TIMEOUT_S = float(os.environ.get('FLEET_AGENT_TIMEOUT_S', '1.0'))
BREAKER_MINUTES = int(os.environ.get('FLEET_BREAKER_MINUTES', '10'))


class FaultableAgent:
    """Fault injection for the demo: 'garbage' returns a wrong answer, 'timeout' never
    answers in time, 'flaky' alternates garbage then timeout (a realistic bad model)."""
    name = 'Agent'
    fault = None
    failed = False
    _flip = 0

    def run(self, timeout, method, *args):
        mode = self.fault
        if mode == 'flaky':
            self._flip += 1
            mode = 'garbage' if self._flip % 2 else 'timeout'
        if mode == 'timeout':
            time.sleep(timeout + 0.5)       # past the deadline; the orchestrator has moved on
        out = getattr(self, method)(*args)
        return self.corrupt(method, out, *args) if mode == 'garbage' else out

    def corrupt(self, method, out, *args):
        return out


class TractorAgent(FaultableAgent):
    """Speaks for one tractor: decides whether to bid on each waiting job, and at what cost."""
    def __init__(self, tid):
        self.tid = tid
        self.name = f'Tractor {tid}'
        self.recent = deque(maxlen=12)      # private memory: its own recent bids

    def bid(self, farm, waiting):
        t = farm.tractor(self.tid)
        out = []
        for job in waiting:
            base = {'tractor': self.tid, 'job': job['id']}
            if t['status'] != 'idle' or t['job']:
                out.append({**base, 'decline': True, 'reason': f"busy ({t['status']})"})
            elif job['implement'] not in t['implements']:
                out.append({**base, 'decline': True, 'reason': f"no {job['implement']} implement"})
            elif not farm.battery_ok(t, job):
                reserve = getattr(farm, 'reserve', 0)
                out.append({**base, 'decline': True,
                            'reason': f"battery {t['battery']:.0f}% cannot cover {farm.battery_cost(job):.0f}%"
                                      + (f" plus the {reserve}% reserve" if reserve else '')})
            else:
                cost, travel, finish = PlannerAgent.score(farm, t, job)
                out.append({**base, 'cost': cost, 'travel_min': travel, 'eta_min': round(finish),
                            'battery_after': round(t['battery'] - farm.battery_cost(job) + 8),
                            'reason': f"{job['implement']} attached, {t['battery']:.0f}% battery"})
        self.recent.extend(b for b in out if not b.get('decline'))
        return out

    def corrupt(self, method, out, farm, waiting):
        # Hallucination: a bid on behalf of a tractor that does not exist.
        if not waiting:
            return out
        return out + [{'tractor': 'T9', 'job': waiting[0]['id'], 'cost': 0, 'travel_min': 0,
                       'eta_min': farm.minute + 1, 'battery_after': 99, 'reason': 'T9 is free and closest'}]


class MemoryAgent:
    name = 'Memory'
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.facts = deque(maxlen=120)
        if self.path.exists():
            try:
                for line in self.path.read_text(encoding='utf-8').splitlines()[-120:]:
                    self.facts.append(json.loads(line))
            except (OSError, ValueError):
                pass
        self.prefs_path = self.path.parent / 'preferences.json'
        self.checkpoints = {}             # (run, job) -> highest saved progress, this run only

    # ---- operator preferences that survive reset and restart (like FarmOS's fuel reserve) ----
    def preference(self, key, default=None):
        try:
            return json.loads(self.prefs_path.read_text(encoding='utf-8')).get(key, default)
        except (OSError, ValueError, AttributeError):
            return default

    def save_preference(self, key, value):
        try:
            prefs = json.loads(self.prefs_path.read_text(encoding='utf-8')) if self.prefs_path.exists() else {}
        except (OSError, ValueError):
            prefs = {}
        prefs[key] = value
        try:
            self.prefs_path.write_text(json.dumps(prefs), encoding='utf-8')
            return True
        except OSError:
            return False

    # ---- progress checkpoints (like FarmOS's row checkpoint) ----
    def checkpoint(self, minute, run, tractor, job):
        key = (run, job['id'])
        self.checkpoints[key] = max(self.checkpoints.get(key, 0), job['done'])
        return self.remember(minute, 'checkpoint', tractor,
                             f"{job['name']} saved at {job['done']}/{job['total']} units when {tractor} stopped.",
                             job=job['id'], done=job['done'], total=job['total'], run=run)

    def saved_progress(self, run, job_id):
        return self.checkpoints.get((run, job_id))

    def last_checkpoint(self):
        return next((f for f in reversed(self.facts) if f.get('event') == 'checkpoint'), None)

    def remember(self, minute, kind, tractor, summary, **extra):
        fact = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'sim_minute': minute, 'event': kind, 'tractor': tractor,
                'summary': summary[:350], **extra}
        self.facts.append(fact)
        try:
            with self.path.open('a', encoding='utf-8') as fp:
                fp.write(json.dumps(fact, separators=(',', ':')) + '\n')
        except OSError:
            pass  # Losing optional persistence must not stop the scheduling simulation.
        return fact

    def recall(self, tractor=None, limit=6):
        hits = [f for f in self.facts if not tractor or f['tractor'] == tractor]
        return hits[-limit:]


class MonitorAgent:
    name = 'Monitor'
    def classify(self, event, tractor):
        if event == 'fail':
            return {'health': 'confirmed_stopped', 'confidence': 'operator', 'release_safe': True}
        if event == 'confirm_stop':
            return {'health': 'operator_verified_stop', 'confidence': 'operator', 'release_safe': True}
        if event in ('disconnect', 'delay'):
            return {'health': 'unknown', 'confidence': 'insufficient', 'release_safe': False}
        if event == 'drain':
            return {'health': 'low_energy', 'confidence': 'simulation', 'release_safe': True}
        if event in ('restore', 'recharge'):
            return {'health': 'available', 'confidence': 'simulation', 'release_safe': False}
        return {'health': 'normal', 'confidence': 'simulation', 'release_safe': False}


class RecoveryAgent:
    name = 'Recovery'
    def decide(self, observation):
        # IMPORTANT: network loss/late heartbeat never proves a physical safe stop.
        safe = bool(observation['release_safe'])
        if observation['health'] == 'unknown':
            return {'release': False, 'instruction': 'RESERVE_WORK',
                    'reason': 'Unknown tractor state; the previous lease stays exclusive.'}
        if safe:
            return {'release': True, 'instruction': 'REPLAN_REMAINING',
                    'reason': 'A stop was explicitly confirmed; retain previously committed progress.'}
        return {'release': False, 'instruction': 'UPDATE_HEALTH',
                'reason': 'Update machine status without duplicating assignments.'}


class ConstraintGuard:
    name = 'Guard'
    def verify(self, farm, proposals):
        """Check a whole proposal without changing anything. Returns a list of violations."""
        violations, tractors, jobs = [], set(), set()
        for p in proposals:
            t, j = farm.tractor(p['tractor']), farm.job_by_id(p['job'])
            if t is None:
                violations.append({'rule': 'unknown_tractor', 'tractor': p['tractor'],
                                   'detail': f"tractor {p['tractor']} does not exist"})
                continue
            if j is None:
                violations.append({'rule': 'unknown_job', 'detail': f"job {p['job']} does not exist"})
                continue
            if p['tractor'] in tractors or p['job'] in jobs or j['assigned'] is not None or t['job'] is not None:
                violations.append({'rule': 'double_lease', 'detail': f"{p['job']} or {p['tractor']} would hold two leases"})
            elif j['implement'] not in t['implements']:
                violations.append({'rule': 'implement', 'tractor': p['tractor'],
                                   'detail': f"{t['name']} has no {j['implement']} implement for {p['job']}"})
            elif not farm.ready(j) or j['done'] >= j['total']:
                violations.append({'rule': 'dependency', 'detail': f"{p['job']} is blocked or already complete"})
            elif (saved := farm.agents.memory.saved_progress(getattr(farm, 'run', None), j['id'])) is not None and j['done'] < saved:
                violations.append({'rule': 'progress_lost', 'detail': f"{p['job']} would restart at {j['done']} units, below its {saved}-unit checkpoint"})
            elif not farm.eligible(t, j):
                reserve = getattr(farm, 'reserve', 0)
                violations.append({'rule': 'battery_or_availability', 'tractor': p['tractor'],
                                   'detail': f"{t['name']} lacks battery"
                                             + (f" (with the {reserve}% reserve)" if reserve else '') + f" or availability for {p['job']}"})
            tractors.add(p['tractor'])
            jobs.add(p['job'])
        return violations

    def commit(self, farm, tractor, job):
        if not farm.ready(job) or job['done'] >= job['total']:
            return False, 'dependency or completion block'
        if job['assigned'] is not None or tractor['job'] is not None:
            return False, 'existing lease'
        if not farm.eligible(tractor, job):
            return False, 'implement, battery or availability mismatch'
        job['assigned'] = tractor['id']
        tractor['job'] = job['id']
        tractor['status'] = 'working'
        tractor['field'] = job['field']
        return True, 'all hard constraints passed'


class PlannerAgent(FaultableAgent):
    """Awards jobs from the tractor bids. propose() is the deterministic fallback planner."""
    name = 'Planner'

    @staticmethod
    def waiting(farm):
        return sorted((j for j in farm.jobs if j['done'] < j['total'] and
                       j['assigned'] is None and farm.ready(j)),
                      key=lambda j: (-j['priority'], j['deadline'], j['id']))

    def award(self, farm, bids, feedback):
        banned = set(feedback.get('bad_tractors', ()))
        by_job = {}
        for b in bids:
            if not b.get('decline') and b['tractor'] not in banned:
                by_job.setdefault(b['job'], []).append(b)
        out, used = [], set()
        for job in self.waiting(farm):
            cands = [b for b in by_job.get(job['id'], []) if b['tractor'] not in used]
            if cands:
                best = min(cands, key=lambda b: (b['cost'], b['tractor']))
                out.append({'tractor': best['tractor'], 'job': job['id'], 'cost': best['cost'],
                            'travel_min': best['travel_min'], 'eta_min': best['eta_min']})
                used.add(best['tractor'])
        return out

    def corrupt(self, method, out, farm, *args):
        # Garbage: hand the first job to a tractor without the right implement.
        out = [dict(p) for p in out]
        for p in out:
            job = farm.job_by_id(p['job'])
            wrong = next((t for t in farm.tractors if job['implement'] not in t['implements']), None)
            if wrong:
                p['tractor'] = wrong['id']
                break
        return out

    @staticmethod
    def score(farm, tractor, job):
        travel = 0 if tractor['field'] == job['field'] else 5
        remaining = job['total'] - job['done']
        finish = farm.minute + travel + remaining / tractor['speed']
        tardy = max(0, finish - job['deadline'])
        return (round(tardy * 35 + travel * 2 + remaining / tractor['speed'] +
                      (100 - tractor['battery']) * .04, 2), travel, finish)

    def propose(self, farm):
        # Deterministic fallback planner: one proposal per newly available job.
        waiting = self.waiting(farm)
        proposals = []
        used = set()
        for job in waiting:
            eligible = [t for t in farm.tractors if t['id'] not in used and farm.eligible(t, job)]
            if eligible:
                tractor = min(eligible, key=lambda t: (self.score(farm,t,job)[0], t['id']))
                cost, travel, finish = self.score(farm, tractor, job)
                proposals.append({'tractor': tractor['id'], 'job': job['id'],
                                  'cost': cost, 'travel_min': travel, 'eta_min': round(finish)})
                used.add(tractor['id'])
        return proposals


class AdvisorAgent:
    """Bring-your-own LLM via a standard OpenAI-compatible Chat Completions endpoint.

    LLM can offer advisory explanations. It NEVER commits assignments or controls machines.
    Ollama and other local OpenAI-compatible providers can be used by configuration.
    """
    name = 'Advisor'
    def __init__(self):
        self.endpoint = os.environ.get('FLEET_LLM_ENDPOINT', '').strip()
        self.api_key = os.environ.get('FLEET_LLM_API_KEY', '').strip()
        self.model = os.environ.get('FLEET_LLM_MODEL', 'local-model').strip()
        self.last = 'LLM not configured. Deterministic agents remain fully operational.'
        self.state = 'offline'
        self.pending = False
        self._lock = threading.Lock()

    def ask_async(self, snapshot, on_done):
        if not self.endpoint:
            return False, 'Set FLEET_LLM_ENDPOINT and FLEET_LLM_MODEL to enable a BYO LLM.'
        with self._lock:
            if self.pending:
                return False, 'Advisor is already working on a request.'
            self.pending = True
            self.state = 'thinking'
        thread = threading.Thread(target=self._invoke, args=(snapshot,on_done), daemon=True)
        thread.start()
        return True, 'Advisor is analyzing the current fleet state.'

    def _invoke(self, snapshot, on_done):
        try:
            brief = {
                'minute': snapshot['minute'],
                'tractors': [{k:t[k] for k in ('id','status','job','implements','battery')} for t in snapshot['tractors']],
                'jobs': [{k:j[k] for k in ('id','name','implement','done','total','deadline','assigned','status')} for j in snapshot['jobs']],
                'explanation': snapshot['explanation'],
            }
            body = json.dumps({'model': self.model, 'temperature': 0.1, 'max_tokens': 220,
                'messages': [
                    {'role':'system', 'content': 'You are an advisory farm fleet scheduling agent. Give 3 concise, evidence-grounded sentences explaining current tradeoffs and a practical next action. Never claim you have controlled equipment. If status is unknown, insist on human verified safe stop before task reassignment. Avoid inventing facts.'},
                    {'role':'user','content': json.dumps(brief, separators=(',', ':'))}] }).encode()
            headers = {'Content-Type':'application/json'}
            if self.api_key:
                headers['Authorization'] = 'Bearer ' + self.api_key
            req = request.Request(self.endpoint, data=body, headers=headers, method='POST')
            with request.urlopen(req, timeout=9) as resp:
                data = json.load(resp)
            content = str(data['choices'][0]['message']['content']).strip()
            with self._lock:
                self.last = content[:1300]
                self.state = 'connected'
            on_done(True, self.last)
        except (error.HTTPError, error.URLError, OSError, KeyError, TypeError, ValueError) as exc:
            with self._lock:
                self.last = 'LLM request unavailable: ' + str(exc)[:200]
                self.state = 'error'
            on_done(False, self.last)
        finally:
            with self._lock:
                self.pending = False

    def status(self):
        with self._lock:
            return {'configured': bool(self.endpoint), 'model': self.model if self.endpoint else 'None',
                    'status': self.state, 'pending': self.pending, 'advice': self.last}


class AgentMesh:
    """Directed structured messages + narrowly scoped agent tools + long-lived incident memory."""
    def __init__(self, farm, memory_path):
        self.farm = farm
        self.monitor = MonitorAgent()
        self.recovery = RecoveryAgent()
        self.planner = PlannerAgent()
        self.guard = ConstraintGuard()
        self.memory = MemoryAgent(memory_path)
        self.advisor = AdvisorAgent()
        self.tractor_agents = {}          # one per tractor, created on first use
        self.timeout = AGENT_TIMEOUT_S
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='agent')
        self.messages = deque(maxlen=75)
        self.sent = 0
        self.bytes_sent = 0
        self.skipped_idle = 0
        self._sequence = 0
        self.blocked_plans = 0            # proposals the Guard refused
        self.fallback_rounds = 0          # replans the fallback planner had to make
        self.breaker_until = -1           # sim minute until which the Planner is bypassed
        self.last_round = None
        self.last_explanation = ''

    # ---------------- agent calls with timeouts ----------------
    def _ask(self, agent, method, *args):
        """Call one agent under the timeout. Returns None (and logs why) when it fails."""
        future = self.pool.submit(agent.run, self.timeout, method, *args)
        done, _ = wait_futures([future], timeout=self.timeout)
        if not done:
            agent.failed = True
            self.send('Orchestrator', agent.name, 'timeout', {'method': method, 'limit_s': self.timeout})
            return None
        try:
            result = future.result()
            agent.failed = bool(agent.fault)
            return result
        except Exception as exc:  # an LLM-backed agent can fail in any way
            agent.failed = True
            self.send('Orchestrator', agent.name, 'error', {'method': method, 'error': str(exc)[:120]})
            return None

    def _collect_bids(self, waiting):
        """Call for bids goes to every Tractor agent at once; late answers count as declines."""
        agents = [self.tractor_agents.setdefault(t['id'], TractorAgent(t['id'])) for t in self.farm.tractors]
        for a in agents:
            self.send('Orchestrator', a.name, 'bid.request', {'jobs': [j['id'] for j in waiting], 'limit_s': self.timeout})
        futures = {self.pool.submit(a.run, self.timeout, 'bid', self.farm, waiting): a for a in agents}
        done, _ = wait_futures(futures, timeout=self.timeout)
        bids = []
        for future, a in futures.items():
            if future not in done:
                a.failed = True
                self.send(a.name, 'Planner', 'bid.timeout', {'treated_as': 'decline', 'limit_s': self.timeout})
                continue
            try:
                result = future.result()
            except Exception as exc:
                a.failed = True
                self.send(a.name, 'Planner', 'bid.error', {'error': str(exc)[:120]})
                continue
            a.failed = bool(a.fault)
            bids += result
            offers = [b for b in result if not b.get('decline')]
            ghosts = [b for b in offers if b['tractor'] != a.tid]
            if offers:
                self.send(a.name, 'Planner', 'bid', {'offers': [(b['tractor'], b['job'], f"T+{b['eta_min']}m") for b in offers]})
            else:
                reasons = sorted({b['reason'] for b in result})
                self.send(a.name, 'Planner', 'decline', {'reasons': reasons[:2]})
            for g in ghosts:
                self.farm.log('alert', f"{a.name} agent submitted a bid for {g['tractor']}, a tractor that is not in the fleet.")
        return bids

    def send(self, sender, recipient, verb, payload=None):
        payload = payload or {}
        text = json.dumps(payload, separators=(',', ':'), default=str)
        self._sequence += 1
        self.sent += 1
        self.bytes_sent += len(text.encode('utf-8'))
        self.messages.appendleft({'id':self._sequence, 'time':f'T+{self.farm.minute:03d}m',
                                  'sender':sender,'recipient':recipient,'verb':verb,
                                  'detail':text[:180]})

    def incident(self, event, tractor, summary):
        # Event-driven route: Monitor -> Orchestrator -> Recovery -> Memory.
        state = self.monitor.classify(event, tractor)
        self.send('Monitor','Orchestrator','health.event', {'tractor':tractor['id'],'kind':event,'health':state['health']})
        self.send('Orchestrator','Recovery','evaluate',{'tractor':tractor['id'],'release_safe':state['release_safe']})
        decision = self.recovery.decide(state)
        self.send('Recovery','Orchestrator','decision', {'tractor':tractor['id'], **decision})
        job = self.farm.job_by_id(tractor['job']) if tractor.get('job') else None
        if job:
            # Save exactly how far the interrupted job got, before anything is released.
            self.send('Orchestrator','Memory','checkpoint.save',{'job':job['id'],'done':job['done'],'total':job['total']})
            self.memory.checkpoint(self.farm.minute, getattr(self.farm, 'run', None), tractor['id'], job)
            self.send('Memory','Orchestrator','checkpoint.saved',{'job':job['id'],'resume_at':job['done']})
        self.memory.remember(self.farm.minute, event, tractor['id'], summary)
        self.send('Orchestrator','Memory','remember', {'tractor':tractor['id'],'event':event})
        return decision

    def replan(self):
        farm = self.farm
        if not self.planner.propose(farm):
            self.skipped_idle += 1  # no chatter if nothing meaningful changed
            return []
        started = time.monotonic()
        waiting = self.planner.waiting(farm)
        self.send('Orchestrator','Planner','plan.request',{'minute':farm.minute,'waiting_jobs':len(waiting)})
        bids = self._collect_bids(waiting)
        proposals, proposed_by, rounds, refused = None, 'Planner', 0, []
        if farm.minute < self.breaker_until:
            self.send('Orchestrator','Planner','breaker.open',{'until':f'T+{self.breaker_until:03d}m'})
        else:
            feedback = {}
            for rounds in (1, 2):
                award = self._ask(self.planner, 'award', farm, bids, feedback)
                if not isinstance(award, list):
                    continue
                self.send('Planner','Guard','assignment.proposals',{'round':rounds,'pairs':[(p['tractor'],p['job']) for p in award]})
                violations = self.guard.verify(farm, award)
                if not violations:
                    proposals = award
                    self.send('Guard','Orchestrator','verify.pass',{'round':rounds,'pairs':len(award)})
                    break
                self.blocked_plans += 1
                refused += violations
                self.send('Guard','Planner','verify.fail',{'round':rounds,'violations':[v['detail'] for v in violations[:2]]})
                farm.log('alert', f"Guard blocked the Planner's proposal (round {rounds}): {violations[0]['detail']}. Nothing was applied.")
                feedback = {'violations': violations,
                            'bad_tractors': sorted({v['tractor'] for v in violations if v['rule'] == 'unknown_tractor'})}
        if proposals is None:
            if farm.minute >= self.breaker_until:
                self.breaker_until = farm.minute + BREAKER_MINUTES
                farm.log('alert', f"Planner agent failed twice. Deterministic fallback planner engaged; the Planner is bypassed until T+{self.breaker_until:03d}m.")
            self.fallback_rounds += 1
            proposed_by = 'Fallback planner'
            proposals = self.planner.propose(farm)
            self.send('Orchestrator','Guard','fallback.proposals',{'pairs':[(p['tractor'],p['job']) for p in proposals]})
            violations = self.guard.verify(farm, proposals)
            if violations:  # should never happen; the Guard still refuses to apply it
                self.blocked_plans += 1
                farm.log('alert', f"Guard blocked the fallback plan too: {violations[0]['detail']}. Work stays queued.")
                return []
            self.send('Guard','Orchestrator','verify.pass',{'round':'fallback','pairs':len(proposals)})
        committed = []
        for p in proposals:
            tractor = self.farm.tractor(p['tractor'])
            job = self.farm.job_by_id(p['job'])
            ok, reason = self.guard.commit(self.farm, tractor, job)
            self.send('Guard','Orchestrator','tool.assign.result', {'job':job['id'],'tractor':tractor['id'],'ok':ok})
            if ok:
                committed.append(p)
                self.farm.log('plan', f"Planner → Guard assigned {tractor['name']} to {job['name']}: implement compatible, battery {tractor['battery']:.0f}%, expected finish T+{p['eta_min']}m, deadline T+{job['deadline']}m.")
            else:
                self.farm.log('alert', f'Guard rejected assignment {job["id"]} → {tractor["id"]}: {reason}.')
        self.last_round = {'minute': farm.minute, 'bids': sum(1 for b in bids if not b.get('decline')),
                           'declines': sum(1 for b in bids if b.get('decline')), 'rounds': rounds,
                           'proposed_by': proposed_by, 'committed': len(committed),
                           'ms': round((time.monotonic() - started) * 1000)}
        if committed:
            self.last_explanation = self.explain(committed, bids, refused, proposed_by)
            self.send('Communicator','Operator','explain',{'summary':self.last_explanation[:150]})
        return committed

    def explain(self, committed, bids, refused, proposed_by):
        """Communicator: the new plan in plain language, built only from facts on the board."""
        farm, parts = self.farm, []
        for p in committed:
            t, j = farm.tractor(p['tractor']), farm.job_by_id(p['job'])
            others = sorted({f"{farm.tractor(b['tractor'])['name']} ({b['reason']})" for b in bids
                             if b.get('decline') and b['job'] == j['id'] and farm.tractor(b['tractor'])
                             and 'busy' not in b['reason']})
            parts.append(f"{t['name']} takes {j['name']}: it has the {j['implement']} implement and {t['battery']:.0f}% battery, "
                         f"finishing about T+{p['eta_min']}m against a T+{j['deadline']}m deadline"
                         + (f", resuming from the saved checkpoint at {j['done']}/{j['total']} units" if j['done'] else '')
                         + (f"; not chosen: {', '.join(others[:2])}" if others else '') + '.')
        text = f"Plan update at T+{farm.minute:03d}m. " + ' '.join(parts)
        if getattr(farm, 'reserve', 0):
            text += f" Every new assignment keeps at least {farm.reserve}% battery in reserve."
        if refused:
            text += f" The Guard first blocked {len(refused)} invalid proposal{'s' if len(refused) > 1 else ''} ({refused[0]['detail']}), so nothing unsafe was applied."
        if proposed_by != 'Planner':
            text += ' This plan came from the deterministic fallback planner because the Planner agent failed.'
        return text

    def reset_protocol(self):
        """Called on a demo reset: clear injected faults, the breaker and round history."""
        for a in [self.planner, *self.tractor_agents.values()]:
            a.fault, a.failed = None, False
        self.breaker_until, self.last_round, self.last_explanation = -1, None, ''
        self.blocked_plans = self.fallback_rounds = 0

    def set_fault(self, target, on=None, mode=None):
        """Demo control: break or restore the Planner agent or one Tractor agent."""
        agent = self.planner if target in ('Planner', 'planner', None, '') else self.tractor_agents.get(target)
        if agent is None:
            raise ValueError('Choose Planner or a tractor id for fault injection.')
        on = (agent.fault is None) if on is None else bool(on)
        if on:
            agent.fault = mode or ('flaky' if agent is self.planner else 'garbage')
            what = {'flaky': 'returns invalid plans, then stops answering', 'garbage': 'returns false bids',
                    'timeout': 'stops answering'}.get(agent.fault, agent.fault)
            self.send('Monitor','Orchestrator','fault.injected',{'agent':agent.name,'mode':agent.fault})
            self.farm.log('alert', f"AI FAULT INJECTED: {agent.name} agent {what}.")
            return f'{agent.name} agent broken ({agent.fault}).'
        agent.fault, agent.failed = None, False
        if agent is self.planner:
            self.breaker_until = -1
        self.send('Monitor','Orchestrator','fault.cleared',{'agent':agent.name})
        self.farm.log('success', f"{agent.name} agent restored.")
        return f'{agent.name} agent restored.'

    def request_llm(self):
        self.send('Orchestrator','Advisor','analyze.request', {'snapshot':'compact fleet state','purpose':'explain tradeoffs'})
        snap = self.farm.snapshot()
        def done(ok, msg):
            with self.farm.lock:
                self.send('Advisor','Orchestrator','analyze.result', {'success':ok,'summary':msg[:150]})
                self.farm.log('success' if ok else 'alert', 'LLM Advisor: ' + msg[:220])
        return self.advisor.ask_async(snap, done)

    def snapshot(self):
        agents = [self.planner, *self.tractor_agents.values()]
        return {'roles':['Orchestrator','Monitor','Tractor agents','Planner','Guard','Recovery','Memory','Communicator','Advisor'],
                'messages':list(self.messages)[:22],
                'communication':{'directed_messages':self.sent,'payload_bytes':self.bytes_sent,
                                 'idle_cycles_without_messages':self.skipped_idle},
                'orchestration':{'blocked_plans':self.blocked_plans,'fallback_rounds':self.fallback_rounds,
                                 'fallback_active':self.farm.minute < self.breaker_until,
                                 'agent_timeout_s':self.timeout,'last_round':self.last_round,
                                 'faults':{a.name:a.fault for a in agents if a.fault},
                                 'failed_agents':[a.name for a in agents if a.failed]},
                'last_explanation':self.last_explanation,
                'last_checkpoint':self.memory.last_checkpoint(),
                'memory_count':len(self.memory.facts), 'recent_memory':self.memory.recall(limit=3),
                'llm':self.advisor.status(), 'integration':'AgOpenWeb adapter not yet connected; simulation backend active'}
