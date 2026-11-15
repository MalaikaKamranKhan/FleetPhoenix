"""Explicit multi-agent orchestration layer for FleetPhoenix.

Agents communicate through a small directed message log. The orchestration layer is
simulation-only; actuator commands are deliberately absent. A deterministic
constraint guard is the only component authorized to commit simulated jobs.
"""
from __future__ import annotations
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error


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

    def remember(self, minute, kind, tractor, summary):
        fact = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'sim_minute': minute, 'event': kind, 'tractor': tractor,
                'summary': summary[:350]}
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


class PlannerAgent:
    name = 'Planner'
    @staticmethod
    def score(farm, tractor, job):
        travel = 0 if tractor['field'] == job['field'] else 5
        remaining = job['total'] - job['done']
        finish = farm.minute + travel + remaining / tractor['speed']
        tardy = max(0, finish - job['deadline'])
        return (round(tardy * 35 + travel * 2 + remaining / tractor['speed'] +
                      (100 - tractor['battery']) * .04, 2), travel, finish)

    def propose(self, farm):
        # One proposal per newly available job; machine selection is deterministic.
        waiting = sorted((j for j in farm.jobs if j['done'] < j['total'] and
                          j['assigned'] is None and farm.ready(j)),
                         key=lambda j: (-j['priority'], j['deadline'], j['id']))
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
        self.messages = deque(maxlen=75)
        self.sent = 0
        self.bytes_sent = 0
        self.skipped_idle = 0
        self._sequence = 0

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
        self.memory.remember(self.farm.minute, event, tractor['id'], summary)
        self.send('Orchestrator','Memory','remember', {'tractor':tractor['id'],'event':event})
        return decision

    def replan(self):
        proposals = self.planner.propose(self.farm)
        if not proposals:
            self.skipped_idle += 1  # no chatter if nothing meaningful changed
            return []
        self.send('Orchestrator','Planner','plan.request',{'minute':self.farm.minute,'candidate_count':len(proposals)})
        self.send('Planner','Guard','assignment.proposals',{'pairs':[(p['tractor'],p['job']) for p in proposals]})
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
        return committed

    def request_llm(self):
        self.send('Orchestrator','Advisor','analyze.request', {'snapshot':'compact fleet state','purpose':'explain tradeoffs'})
        snap = self.farm.snapshot()
        def done(ok, msg):
            with self.farm.lock:
                self.send('Advisor','Orchestrator','analyze.result', {'success':ok,'summary':msg[:150]})
                self.farm.log('success' if ok else 'alert', 'LLM Advisor: ' + msg[:220])
        return self.advisor.ask_async(snap, done)

    def snapshot(self):
        return {'roles':['Orchestrator','Monitor','Planner','Guard','Recovery','Memory','Advisor'],
                'messages':list(self.messages)[:22],
                'communication':{'directed_messages':self.sent,'payload_bytes':self.bytes_sent,
                                 'idle_cycles_without_messages':self.skipped_idle},
                'memory_count':len(self.memory.facts), 'recent_memory':self.memory.recall(limit=3),
                'llm':self.advisor.status(), 'integration':'AgOpenWeb adapter not yet connected; simulation backend active'}
