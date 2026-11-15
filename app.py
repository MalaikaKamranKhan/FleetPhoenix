"""FleetPhoenix - self-contained, simulation-only Track 2 hackathon prototype.
Requires Python 3.10+ and qrcode[pil] only to render the judge QR code.
No connection to real tractors, farm machinery, actuators, or navigation systems.
"""
import io
import json
import os
import secrets
import socket
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from agent_mesh import AgentMesh

BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get('FLEET_PORT', '5000'))
HOST_IP = os.environ.get('FLEET_HOST_IP', '').strip()
ACCESS_TOKEN = secrets.token_urlsafe(16)


def local_ip():
    if HOST_IP:
        return HOST_IP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))  # No network packet is sent.
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


class FleetEngine:
    IMPLEMENTS = {'plow', 'seed', 'harvest', 'irrigate'}
    FIELDS = {'North Ridge', 'East Orchard', 'West Terrace'}

    def __init__(self):
        self.lock = threading.RLock()
        self.agents = AgentMesh(self, BASE / 'runtime' / 'agent_memory.jsonl')
        self.reset()

    def log(self, kind, message):
        self.serial += 1
        self.events.insert(0, {
            'id': self.serial, 'time': f'T+{self.minute:03d}m',
            'kind': kind, 'message': message,
        })
        self.events = self.events[:65]

    def reset(self):
        with self.lock:
            self.minute = 0
            self.paused = True
            self.serial = 0
            self.events = []
            self.recoveries = 0
            self.tractors = [
                {'id': 'T1', 'name': 'Titan 01', 'implements': ['plow', 'seed'], 'battery': 88.0, 'speed': 1, 'status': 'idle', 'job': None, 'field': 'North Ridge'},
                {'id': 'T2', 'name': 'Titan 02', 'implements': ['irrigate'], 'battery': 83.0, 'speed': 1, 'status': 'idle', 'job': None, 'field': 'East Orchard'},
                {'id': 'T3', 'name': 'Titan 03', 'implements': ['harvest'], 'battery': 93.0, 'speed': 1, 'status': 'idle', 'job': None, 'field': 'West Terrace'},
                {'id': 'T4', 'name': 'Titan 04', 'implements': ['plow', 'seed'], 'battery': 96.0, 'speed': 1, 'status': 'idle', 'job': None, 'field': 'North Ridge'},
            ]
            self.jobs = [
                self.job('J1', 'Prepare North Ridge', 'North Ridge', 'plow', 24, 60, 3),
                self.job('J2', 'Irrigate East Orchard', 'East Orchard', 'irrigate', 27, 56, 3),
                self.job('J3', 'Harvest West Terrace', 'West Terrace', 'harvest', 29, 74, 3),
                self.job('J4', 'Plant North Ridge', 'North Ridge', 'seed', 20, 104, 2, 'J1'),
                self.job('J5', 'Inspect West Irrigation', 'West Terrace', 'irrigate', 14, 118, 2, 'J3'),
            ]
            self.log('system', 'Demo scenario initialized: five field jobs, four tractors, two dependent tasks.')
            self.plan()

    @staticmethod
    def job(id_, name, field, implement, units, deadline, priority, depends=None):
        return {'id': id_, 'name': name, 'field': field, 'implement': implement,
                'total': units, 'done': 0, 'deadline': deadline, 'priority': priority,
                'depends': depends, 'assigned': None}

    def tractor(self, tid):
        return next((t for t in self.tractors if t['id'] == tid), None)

    def job_by_id(self, jid):
        return next((j for j in self.jobs if j['id'] == jid), None)

    def ready(self, job):
        dep = self.job_by_id(job['depends']) if job['depends'] else None
        return dep is None or dep['done'] == dep['total']

    @staticmethod
    def battery_cost(job):
        return (job['total'] - job['done']) * 1.15 + 8

    def eligible(self, tractor, job):
        return (tractor['status'] == 'idle' and tractor['job'] is None
                and job['implement'] in tractor['implements']
                and tractor['battery'] >= self.battery_cost(job))

    def plan(self):
        # Agentic handoff: Planner proposes; Guard alone commits simulated work.
        return self.agents.replan()

    def tick(self):
        with self.lock:
            if self.paused:
                return
            self.minute += 1
            for t in self.tractors:
                if t['status'] == 'offline' and t.get('delay_until') is not None and self.minute >= t['delay_until']:
                    # Simulated heartbeat returns; previously-held lease is still valid.
                    t.pop('delay_until', None)
                    t['status'] = 'working' if t['job'] else 'idle'
                    self.agents.send('Monitor','Orchestrator','heartbeat.restored', {'tractor':t['id'],'lease':t['job']})
                    self.log('success', f"Late telemetry arrived from {t['name']}; restored its original reserved job without creating a duplicate lease.")
            completed = []
            for t in self.tractors:
                if t['status'] != 'working' or not t['job']:
                    continue
                j = self.job_by_id(t['job'])
                if not j or j['assigned'] != t['id']:
                    self.log('alert', f"Assignment mismatch detected for {t['name']}; operations held.")
                    t['status'] = 'quarantined'
                    continue
                if t['battery'] <= 8:
                    t['status'] = 'charging'
                    j['assigned'] = None
                    t['job'] = None
                    self.log('alert', f"{t['name']} reached battery reserve: {j['name']} held at {j['done']}/{j['total']} work units.")
                    continue
                j['done'] = min(j['total'], j['done'] + t['speed'])
                t['battery'] = max(0.0, round(t['battery'] - 1.15, 2))
                if j['done'] >= j['total']:
                    completed.append((j, t))
            for j, t in completed:
                self.log('success', f"Completed {j['name']} ({j['total']}/{j['total']}); {t['name']} is available.")
                j['assigned'] = None
                t['job'] = None
                t['status'] = 'idle'
            self.plan()

    def action(self, op, payload):
        with self.lock:
            tid = str(payload.get('tractor', ''))
            t = self.tractor(tid)
            if op == 'reset':
                self.reset()
                return 'Demo reset.'
            if op == 'start':
                self.paused = False
                self.log('system', 'Simulation started. One simulated minute passes per real-world second.')
                return 'Simulation running.'
            if op == 'pause':
                self.paused = True
                self.log('system', 'Simulation paused.')
                return 'Simulation paused.'
            if op == 'fail':
                if not t:
                    raise ValueError('Choose a valid tractor.')
                if t['status'] in ('offline', 'quarantined'):
                    raise ValueError('Unknown location/state: verify safe stop before releasing work.')
                if t['status'] == 'failed':
                    raise ValueError('This tractor has already failed.')
                decision = self.agents.incident('fail', t, f'Confirmed mechanical failure of {t["id"]}; original assignment must preserve progress.')
                if not decision['release']:
                    raise ValueError('Recovery policy refused unsafe release.')
                old = self.job_by_id(t['job']) if t['job'] else None
                if old:
                    old['assigned'] = None
                t['job'] = None
                t['status'] = 'failed'
                self.log('alert', f"CONFIRMED BREAKDOWN: {t['name']} stopped. " +
                         (f"{old['name']} preserved at {old['done']}/{old['total']} units." if old else 'No job was interrupted.'))
                assigned = self.plan()
                if old:
                    self.recoveries += 1
                    if not assigned or old['assigned'] is None:
                        self.log('alert', f"No currently eligible replacement for {old['name']}; unfinished work remains safely queued.")
                return f'{t["name"]} failed; schedule reviewed.'
            if op == 'delay':
                if not t or t['status'] not in ('working','idle'):
                    raise ValueError('Select an active or idle tractor.')
                self.agents.incident('delay', t, 'Simulated delivery delay; maintain the exclusive lease until heartbeat returns.')
                t['status'] = 'offline'
                t['delay_until'] = self.minute + 6
                self.log('alert', f"DELAY INJECTED for {t['name']}: telemetry temporarily unavailable for six simulated minutes. Job lease stays RESERVED.")
                return 'Six-minute simulated telemetry delay injected; assignment stays locked.'
            if op == 'disconnect':
                if not t or t['status'] not in ('working', 'idle'):
                    raise ValueError('Select an active or idle tractor.')
                self.agents.incident('disconnect', t, 'Connection unknown; do not infer machine stopped or release work.')
                t['status'] = 'offline'
                self.log('alert', f"SIGNAL LOST: {t['name']} status is UNKNOWN. " +
                         (f"Holding {t['job']} exclusively; no duplicate assignment permitted." if t['job'] else 'No job assigned.'))
                return 'Signal lost. Assignment reserved pending verification.'
            if op == 'confirm_stop':
                if not t or t['status'] != 'offline':
                    raise ValueError('A disconnected tractor is required.')
                decision = self.agents.incident('confirm_stop', t, 'Operator verified safe stop; release remaining work only.')
                if not decision['release']:
                    raise ValueError('Safe stop not verified.')
                t.pop('delay_until', None)
                old = self.job_by_id(t['job']) if t['job'] else None
                if old:
                    old['assigned'] = None
                t['job'] = None
                t['status'] = 'quarantined'
                self.log('alert', f"OPERATOR CONFIRMED SAFE STOP for {t['name']}. " +
                         (f"Released unfinished {old['name']} at {old['done']}/{old['total']} units for replanning." if old else 'No work required release.'))
                self.plan()
                if old:
                    self.recoveries += 1
                return 'Safe stop confirmed; reserved work may now be reassigned.'
            if op == 'restore':
                if not t or t['status'] not in ('offline', 'quarantined', 'failed'):
                    raise ValueError('The tractor is not disconnected or failed.')
                self.agents.incident('restore', t, 'Operator restored tractor or connectivity.')
                t.pop('delay_until', None)
                previously = t['status']
                t['status'] = 'working' if t['job'] else 'idle'
                self.log('success', f"{t['name']} restored from {previously}. " +
                         ('Its reserved assignment resumes.' if t['job'] else 'Available for scheduling.'))
                self.plan()
                return f'{t["name"]} restored.'
            if op == 'drain':
                if not t or t['status'] not in ('working', 'idle'):
                    raise ValueError('Select an active tractor.')
                self.agents.incident('drain', t, 'Simulated low battery; preserve completed progress and replan.')
                old = self.job_by_id(t['job']) if t['job'] else None
                if old:
                    old['assigned'] = None
                t['job'] = None
                t['battery'] = 7.0
                t['status'] = 'charging'
                self.log('alert', f"LOW BATTERY: {t['name']} returned to charge. " +
                         (f"{old['name']} preserved at {old['done']}/{old['total']} units." if old else 'No task interrupted.'))
                self.plan()
                return 'Energy shortage simulated; schedule replanned.'
            if op == 'recharge':
                if not t or t['status'] != 'charging':
                    raise ValueError('Select a charging tractor.')
                self.agents.incident('recharge', t, 'Battery reserve restored for scheduling.')
                t['battery'] = 95.0
                t['status'] = 'idle'
                self.log('success', f"{t['name']} charged to 95% and returned to service.")
                self.plan()
                return f'{t["name"]} recharged.'
            if op == 'ask_advisor':
                ok, message = self.agents.request_llm()
                if not ok:
                    raise ValueError(message)
                return message
            if op == 'create_job':
                name = str(payload.get('name', '')).strip()[:56]
                field = str(payload.get('field', ''))
                implement = str(payload.get('implement', ''))
                if not name or field not in self.FIELDS or implement not in self.IMPLEMENTS:
                    raise ValueError('Provide a name, valid field and implement.')
                try:
                    units = int(payload.get('units', 15))
                    deadline = int(payload.get('deadline', 90))
                    priority = int(payload.get('priority', 2))
                except (TypeError, ValueError):
                    raise ValueError('Invalid numeric values.')
                if not 1 <= units <= 60 or not 10 <= deadline <= 240 or not 1 <= priority <= 3:
                    raise ValueError('Work 1–60, deadline 10–240, priority 1–3.')
                jid = f'J{max(int(j["id"][1:]) for j in self.jobs) + 1}'
                new = self.job(jid, name, field, implement, units, deadline, priority)
                self.jobs.append(new)
                self.log('system', f"OPERATOR SUBMITTED {jid}: {name} ({field}, {implement}, {units} units, deadline T+{deadline}m).")
                self.agents.send('Operator','Orchestrator','job.submitted', {'job':jid,'implement':implement,'deadline':deadline})
                self.plan()
                return f'{jid} added to farm job queue.'
            raise ValueError('Unknown command.')

    def snapshot(self):
        with self.lock:
            jobs = [dict(j, ready=self.ready(j), status=('completed' if j['done'] >= j['total'] else
                'reserved' if j['assigned'] and self.tractor(j['assigned'])['status'] == 'offline' else
                'running' if j['assigned'] else 'blocked' if not self.ready(j) else 'queued'))
                for j in self.jobs]
            errors = []
            assigned = [j['assigned'] for j in jobs if j['assigned']]
            if len(assigned) != len(set(assigned)):
                errors.append('A tractor is assigned to more than one job.')
            for j in jobs:
                if j['assigned']:
                    t = self.tractor(j['assigned'])
                    if not t or t['job'] != j['id'] or j['implement'] not in t['implements']:
                        errors.append(f"Invalid assignment for {j['id']}.")
            total = sum(j['total'] for j in jobs)
            done = sum(j['done'] for j in jobs)
            blocked = [j for j in jobs if j['status'] == 'queued' and
                       not any(self.eligible(t, j) for t in self.tractors)]
            return {
                'minute': self.minute, 'paused': self.paused, 'tractors': [dict(t) for t in self.tractors],
                'jobs': jobs, 'events': list(self.events[:24]), 'recoveries': self.recoveries,
                'metrics': {'completed_units': done, 'total_units': total,
                            'percent': round(100 * done / total) if total else 0,
                            'jobs_done': sum(j['status'] == 'completed' for j in jobs),
                            'jobs_total': len(jobs), 'active': sum(t['status'] == 'working' for t in self.tractors),
                            'held': sum(t['status'] == 'offline' and bool(t['job']) for t in self.tractors),
                            'violations': len(errors), 'errors': errors, 'unscheduled': len(blocked)},
                'explanation': self.explanation(jobs, blocked), 'agent_mesh': self.agents.snapshot(),
            }

    def explanation(self, jobs, blocked):
        offline = [t for t in self.tractors if t['status'] == 'offline']
        if offline:
            busy = [t for t in offline if t['job']]
            if busy:
                return (f"Connection lost to {', '.join(t['name'] for t in busy)}. Their positions and progress are unknown. "
                        'Their jobs remain exclusively reserved until connectivity returns or the operator verifies a safe stop.')
            return 'A tractor is offline. No active assignment is duplicated; the fleet continues with confirmed-available equipment.'
        if blocked:
            names = ', '.join(j['name'] for j in blocked[:2])
            return f'No eligible idle tractor currently has the equipment and battery needed for {names}. The work remains queued.'
        if self.recoveries:
            return 'Recovery is active: completed work is retained, unfinished work is reassigned only to compatible tractors, and the event log records why.'
        return 'Initial plan respects implement compatibility, available energy, task dependencies, and job deadlines.'


engine = FleetEngine()


def qr_image(url):
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#101d1c', back_color='white')
    buff = io.BytesIO()
    img.save(buff, format='PNG')
    return buff.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet request logs, but retain actionable server errors.
        if not fmt.startswith('"GET /api/state'):
            super().log_message(fmt, *args)

    def send_bytes(self, body, mimetype='text/html; charset=utf-8', status=200):
        self.send_response(status)
        self.send_header('Content-Type', mimetype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data).encode(), 'application/json; charset=utf-8', status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/state':
            return self.send_json(engine.snapshot())
        if path in ('/api/trace', '/api/info', '/qr.png') and self.client_address[0] not in ('127.0.0.1', '::1'):
            return self.send_bytes(b'Open this dashboard on the host laptop at http://localhost:5000', 'text/plain', 403)
        if path == '/api/trace':
            return self.send_json({'events':engine.snapshot()['events'],
                                   'agent_messages':engine.agents.snapshot()['messages'],
                                   'incident_memory':engine.agents.memory.recall(limit=35)})
        if path == '/api/info':
            controller_url = f'http://{local_ip()}:{PORT}/judge?t={ACCESS_TOKEN}'
            return self.send_json({'url': controller_url, 'local': f'http://localhost:{PORT}',
                                   'lan': f'http://{local_ip()}:{PORT}'})
        if path == '/qr.png':
            im = qr_image(f'http://{local_ip()}:{PORT}/judge?t={ACCESS_TOKEN}')
            if im is None:
                return self.send_bytes(b'Install qrcode[pil] to render QR.', 'text/plain', 503)
            return self.send_bytes(im, 'image/png')
        if path in ('/', '/judge'):
            filename = 'dashboard.html' if path == '/' else 'judge.html'
            content = (BASE / 'templates' / filename).read_bytes()
            if path == '/judge':
                token = parse_qs(parsed.query).get('t', [''])[0]
                if token != ACCESS_TOKEN:
                    return self.send_bytes(b'This controller link requires a valid event token.', 'text/plain', 403)
            return self.send_bytes(content)
        static = {
            '/static/style.css': ('style.css', 'text/css; charset=utf-8'),
            '/static/shared.js': ('shared.js', 'text/javascript; charset=utf-8'),
            '/static/dashboard.js': ('dashboard.js', 'text/javascript; charset=utf-8'),
            '/static/judge.js': ('judge.js', 'text/javascript; charset=utf-8'),
        }
        if path in static:
            filename, content_type = static[path]
            return self.send_bytes((BASE / 'static' / filename).read_bytes(), content_type)
        return self.send_bytes(b'Not found', 'text/plain', 404)

    def do_POST(self):
        if urlparse(self.path).path != '/api/action':
            return self.send_json({'error': 'Not found'}, 404)
        if self.headers.get('X-Fleet-Token') != ACCESS_TOKEN:
            return self.send_json({'error': 'Unauthorized controller request'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 10000 or length <= 0:
                return self.send_json({'error': 'Invalid request size'}, 400)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Expected JSON object.')
            result = engine.action(str(payload.get('action', '')), payload)
            return self.send_json({'ok': True, 'message': result, 'state': engine.snapshot()})
        except (json.JSONDecodeError, ValueError) as exc:
            return self.send_json({'error': str(exc)}, 400)
        except Exception as exc:
            print('Server error:', repr(exc))
            return self.send_json({'error': 'Unexpected server error'}, 500)


def clock():
    while True:
        time.sleep(1.0)
        engine.tick()


def main():
    threading.Thread(target=clock, name='simulation-clock', daemon=True).start()
    print('\n' + '=' * 65)
    print('  FLEETPHOENIX — AGRICULTURAL RESILIENCE SIMULATOR')
    print('=' * 65)
    print(f'  Laptop display : http://localhost:{PORT}')
    print(f'  On local Wi-Fi : http://{local_ip()}:{PORT}')
    print(f'  Judge link     : http://{local_ip()}:{PORT}/judge?t={ACCESS_TOKEN}')
    print('  Start simulation from the phone controller or laptop toolbar.')
    print('  Both devices must be on the same reachable Wi-Fi network.')
    print('  Ctrl+C to stop. This is a SIMULATION — never real tractor control.\n')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
