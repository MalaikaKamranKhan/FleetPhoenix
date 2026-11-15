"""Live scenario suite behind the dashboard's "Run tests" button.

Each scenario runs on a fresh, throwaway FleetEngine (its own temp folder, so the live
demo and its memory are untouched), injects events through the same engine.action()
calls the judge phone uses, and checks the outcome. A pass means the real system did it.
"""
import tempfile
import time


class Watch:
    """Drives one fresh engine and watches invariants after every step."""
    def __init__(self, engine_cls, tmp):
        self.e = engine_cls(runtime_dir=tmp)
        self.e.agents.timeout = 0.1            # keep fault scenarios fast
        self.max_violations = 0
        self.progress_lost = 0
        self.finished = {}
        self.last = {j['id']: j['done'] for j in self.e.jobs}
        self.check()

    def check(self):
        e = self.e
        self.max_violations = max(self.max_violations, e.snapshot()['metrics']['violations'])
        for j in e.jobs:
            if j['done'] < self.last.get(j['id'], 0):
                self.progress_lost += self.last[j['id']] - j['done']
            self.last[j['id']] = j['done']
            if j['done'] >= j['total'] and j['id'] not in self.finished:
                self.finished[j['id']] = e.minute

    def act(self, op, **payload):
        result = self.e.action(op, payload)
        self.check()
        return result

    def tick(self, n=1):
        for _ in range(n):
            self.e.tick()
            self.check()

    def run(self, until, limit=400):
        """Tick until until(engine) is true; recharge tractors that ran flat, as a crew would."""
        for _ in range(limit):
            if until(self.e):
                return True
            for t in self.e.tractors:
                if t['status'] == 'charging':
                    self.act('recharge', tractor=t['id'])
            self.tick()
        return until(self.e)

    def worker(self, job_id):
        return next(t for t in self.e.tractors if t['job'] == job_id)

    def job(self, job_id):
        return self.e.job_by_id(job_id)

    def all_done(self):
        return all(j['done'] >= j['total'] for j in self.e.jobs)

    def clean(self):
        return self.max_violations == 0 and self.progress_lost == 0

    def metrics(self, full_day=False, **extra):
        e = self.e
        base = {'violations': self.max_violations, 'units_lost': self.progress_lost}
        if full_day:
            met = sum(1 for jid, m in self.finished.items() if m <= e.job_by_id(jid)['deadline'])
            base = {'jobs_done': f"{len(self.finished)}/{len(e.jobs)}", 'deadlines_met': f"{met}/{len(e.jobs)}", **base}
        return {**base, **extra}


def normal_day(w):
    w.act('start')
    w.run(lambda e: all(j['done'] >= j['total'] for j in e.jobs))
    return w.all_done() and w.clean(), w.metrics(full_day=True, sim_minutes=w.e.minute)


def breakdown_mid_job(w):
    w.act('start')
    w.tick(7)
    t = w.worker('J1')
    done = w.job('J1')['done']
    w.act('fail', tractor=t['id'])
    j = w.job('J1')
    new_owner = j['assigned']
    moved = new_owner not in (None, t['id'])
    kept = j['done'] == done
    w.run(lambda e: all(j['done'] >= j['total'] for j in e.jobs))
    return moved and kept and w.all_done() and w.clean(), \
        w.metrics(full_day=True, broken=t['id'], new_owner=new_owner, units_kept=done)


def two_breakdowns(w):
    w.act('start')
    w.tick(7)
    first = w.worker('J1')
    w.act('fail', tractor=first['id'])
    w.tick(1)
    second = next(t for t in w.e.tractors if t['status'] == 'working')
    w.act('fail', tractor=second['id'])
    leases = [j['assigned'] for j in w.e.jobs if j['assigned']]
    no_double = len(leases) == len(set(leases))
    w.act('restore', tractor=first['id'])
    w.act('restore', tractor=second['id'])
    w.run(lambda e: all(j['done'] >= j['total'] for j in e.jobs))
    return no_double and w.all_done() and w.clean(), w.metrics(full_day=True, broken=f"{first['id']}, {second['id']}")


def lost_signal(w):
    w.act('start')
    w.tick(4)
    t = w.worker('J1')
    done = w.job('J1')['done']
    w.act('disconnect', tractor=t['id'])
    w.tick(4)
    held = w.job('J1')['assigned'] == t['id'] and w.job('J1')['done'] == done
    w.act('confirm_stop', tractor=t['id'])
    released = w.job('J1')['assigned'] not in (None, t['id']) and w.job('J1')['done'] == done
    return held and released and w.clean(), w.metrics(held_while_unknown=held, released_after_confirm=released)


def broken_ai(w):
    w.act('fault', agent='Planner')            # invalid plan, then a timeout
    w.act('start')
    w.tick(7)
    t = w.worker('J1')
    w.act('fail', tractor=t['id'])
    o = w.e.agents
    fallback = o.last_round['proposed_by'] == 'Fallback planner'
    moved = w.job('J1')['assigned'] not in (None, t['id'])
    return fallback and moved and o.blocked_plans >= 1 and w.clean(), \
        w.metrics(bad_plans_blocked=o.blocked_plans, fallback_used=fallback)


def ghost_bid(w):
    w.act('fault', agent='T1')                 # T1's agent also bids for "T9"
    w.act('start')
    w.tick(7)
    t = w.worker('J1')
    w.act('fail', tractor=t['id'])
    ghost_seen = any('T9' in ev['message'] for ev in w.e.events)
    planner_recovered = w.e.agents.last_round['proposed_by'] == 'Planner'
    real = w.job('J1')['assigned'] in {x['id'] for x in w.e.tractors} - {t['id']}
    return ghost_seen and planner_recovered and real and w.clean(), \
        w.metrics(ghost_blocked=ghost_seen, next_round_passed=planner_recovered)


def battery_reserve(w):
    w.act('set_reserve', reserve=30)
    w.act('drain', tractor='T2')
    t2 = w.e.tractor('T2')
    t2['battery'], t2['status'] = 60.0, 'idle'   # back with 60%: 21% would be left after J2
    w.e.plan()
    w.check()
    held_back = w.job('J2')['assigned'] is None
    explained = '30% reserve' in w.e.snapshot()['explanation']
    return held_back and explained and w.clean(), w.metrics(reserve_pct=30, unsafe_assignment_refused=held_back)


def surprise_job_no_double_booking(w):
    w.act('create_job', name='Urgent harvest', field='West Terrace', implement='harvest',
          units=8, deadline=30, priority=3)
    new = w.e.jobs[-1]
    leases = [j['assigned'] for j in w.e.jobs if j['assigned']]
    waits = new['assigned'] is None               # the only harvester is busy
    return waits and len(leases) == len(set(leases)) and w.clean(), \
        w.metrics(new_job=new['id'], queued_not_double_booked=waits)


def dependency_order(w):
    w.act('start')
    early = w.job('J4')['assigned'] is None
    w.run(lambda e: e.job_by_id('J1')['done'] >= e.job_by_id('J1')['total'] and e.job_by_id('J4')['assigned'])
    started_after = w.job('J4')['assigned'] is not None and w.finished.get('J1') is not None
    return early and started_after and w.clean(), w.metrics(planting_waited_for_plowing=early and started_after)


def checkpoint_guard(w):
    w.act('start')
    w.tick(7)
    t = w.worker('J1')
    w.act('fail', tractor=t['id'])
    j = w.job('J1')
    saved = j['done']
    holder = w.e.tractor(j['assigned'])
    probe = dict(j, done=0, assigned=None)       # a planner that "forgot" the progress
    w.e.jobs[w.e.jobs.index(j)] = probe
    holder_job, holder['job'] = holder['job'], None
    rules = [v['rule'] for v in w.e.agents.guard.verify(w.e, [{'tractor': holder['id'], 'job': 'J1'}])]
    w.e.jobs[w.e.jobs.index(probe)] = j            # restore the real state
    holder['job'] = holder_job
    blocked = 'progress_lost' in rules
    return blocked and w.clean(), w.metrics(checkpoint_units=saved, restart_below_checkpoint_blocked=blocked)


SCENARIOS = [
    ('S1', 'Normal day: 5 jobs, 4 tractors', normal_day),
    ('S2', 'Tractor breaks mid-job', breakdown_mid_job),
    ('S3', 'Two tractors break 1 minute apart', two_breakdowns),
    ('S4', 'Lost signal: job held until safe stop', lost_signal),
    ('S5', 'Planner AI broken: block, retry, fallback', broken_ai),
    ('S6', 'Agent bids for a tractor that does not exist', ghost_bid),
    ('S7', 'Battery reserve refuses an unsafe assignment', battery_reserve),
    ('S8', 'Surprise job while its only tractor is busy', surprise_job_no_double_booking),
    ('S9', 'Dependent job waits for its prerequisite', dependency_order),
    ('S10', 'Guard blocks a restart below the checkpoint', checkpoint_guard),
]


def run_suite(engine_cls):
    started = time.monotonic()
    results = []
    for sid, name, fn in SCENARIOS:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            w = None
            try:
                w = Watch(engine_cls, tmp)
                passed, metrics = fn(w)
            except Exception as exc:  # a crash is a failure, shown on the board
                passed, metrics = False, {'error': f'{type(exc).__name__}: {exc}'[:160]}
            finally:
                if w:
                    w.e.agents.pool.shutdown(wait=False)
        results.append({'id': sid, 'name': name, 'passed': bool(passed), 'metrics': metrics,
                        'ms': round((time.monotonic() - t0) * 1000)})
    return {'results': results, 'passed': sum(r['passed'] for r in results), 'total': len(results),
            'ms': round((time.monotonic() - started) * 1000)}
