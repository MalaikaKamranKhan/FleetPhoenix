"""Core engine invariants. Run: python test_engine.py"""
import json
import tempfile
import unittest
from agent_mesh import TractorAgent
from app import FleetEngine


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.e = FleetEngine()

    def assert_constraints(self):
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_initial_plan_respects_implements(self):
        s = self.e.snapshot()
        self.assertGreaterEqual(s['metrics']['active'], 3)
        self.assert_constraints()

    def test_breakdown_keeps_progress_and_reassigns(self):
        self.e.action('start', {})
        for _ in range(7):
            self.e.tick()
        a = next(t for t in self.e.tractors if t['job'] == 'J1')
        p = self.e.job_by_id('J1')['done']
        self.e.action('fail', {'tractor': a['id']})
        j = self.e.job_by_id('J1')
        self.assertEqual(j['done'], p)
        self.assertIsNotNone(j['assigned'])
        self.assertNotEqual(j['assigned'], a['id'])
        self.assert_constraints()

    def test_connectivity_reserves_job_until_confirmation(self):
        self.e.action('start', {})
        for _ in range(4):
            self.e.tick()
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        p = self.e.job_by_id('J1')['done']
        self.e.action('disconnect', {'tractor': t['id']})
        for _ in range(4):
            self.e.tick()
        self.assertEqual(self.e.job_by_id('J1')['done'], p)
        self.assertEqual(self.e.job_by_id('J1')['assigned'], t['id'])
        self.assertEqual(self.e.snapshot()['metrics']['held'], 1)
        self.assert_constraints()
        self.e.action('confirm_stop', {'tractor': t['id']})
        self.assertEqual(self.e.job_by_id('J1')['done'], p)
        self.assertNotEqual(self.e.job_by_id('J1')['assigned'], t['id'])
        self.assert_constraints()

    def test_disconnect_reconnect_resumes_same_job(self):
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        self.e.action('disconnect', {'tractor': t['id']})
        self.e.action('restore', {'tractor': t['id']})
        self.assertEqual(t['job'], 'J1')
        self.assertEqual(t['status'], 'working')
        self.assert_constraints()

    def test_new_job_constraints(self):
        self.e.action('create_job', {'name': 'Urgent orchard work', 'field': 'North Ridge',
                                    'implement': 'harvest', 'units': 8,
                                    'deadline': 30, 'priority': 3})
        self.assertEqual(len(self.e.jobs), 6)
        self.assert_constraints()

    def test_job_dependencies_block_start(self):
        self.assertIsNone(self.e.job_by_id('J4')['assigned'])
        self.assert_constraints()



class AgentMeshTests(unittest.TestCase):
    def setUp(self):
        self.e = FleetEngine()

    def test_agent_activity_and_constraint_guard(self):
        snap = self.e.snapshot()
        self.assertIn('Guard', snap['agent_mesh']['roles'])
        self.assertGreater(snap['agent_mesh']['communication']['directed_messages'], 0)
        self.assertEqual(snap['metrics']['violations'], 0)

    def test_delayed_heartbeat_holds_then_restores_lease(self):
        self.e.action('start', {})
        for _ in range(2): self.e.tick()
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        jid = t['job']; done = self.e.job_by_id(jid)['done']
        self.e.action('delay', {'tractor':t['id']})
        for _ in range(5): self.e.tick()
        self.assertEqual(t['status'], 'offline')
        self.assertEqual(self.e.job_by_id(jid)['done'], done)
        self.assertEqual(self.e.job_by_id(jid)['assigned'], t['id'])
        self.e.tick()
        self.assertEqual(t['status'], 'working')
        self.assertEqual(self.e.job_by_id(jid)['assigned'], t['id'])
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_memory_records_failure_and_recovery_messages(self):
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        self.e.action('disconnect', {'tractor':t['id']})
        incident = self.e.agents.memory.recall(t['id'], 1)[0]
        self.assertEqual(incident['event'], 'disconnect')
        self.assertTrue(any(m['sender']=='Recovery' for m in self.e.agents.messages))
        self.assertEqual(self.e.snapshot()['metrics']['held'], 1)

    def test_planner_skips_idle_cycles_without_chatter(self):
        old = self.e.agents.sent
        self.e.plan()
        self.assertEqual(self.e.agents.sent, old)
        self.assertGreater(self.e.agents.skipped_idle, 0)

class OrchestrationProtocolTests(unittest.TestCase):
    """bid -> award -> verify -> retry -> fallback -> commit."""
    def setUp(self):
        self.e = FleetEngine()
        self.e.agents.timeout = 0.1          # keep fault tests fast

    def break_j1_worker(self):
        self.e.action('start', {})
        for _ in range(7):
            self.e.tick()
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        done = self.e.job_by_id('J1')['done']
        self.e.action('fail', {'tractor': t['id']})
        return t, done

    def verbs(self):
        return [m['verb'] for m in self.e.agents.messages]

    def test_tractor_agents_bid_and_planner_awards(self):
        t, done = self.break_j1_worker()
        j = self.e.job_by_id('J1')
        self.assertEqual(j['done'], done)
        self.assertNotIn(j['assigned'], (None, t['id']))
        r = self.e.agents.last_round
        self.assertEqual(r['proposed_by'], 'Planner')
        self.assertGreater(r['bids'], 0)
        self.assertIn('bid.request', self.verbs())
        self.assertIn('verify.pass', self.verbs())
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_broken_planner_is_blocked_then_fallback_keeps_fleet_running(self):
        self.e.action('fault', {'agent': 'Planner'})     # flaky: garbage, then timeout
        t, done = self.break_j1_worker()
        j = self.e.job_by_id('J1')
        self.assertEqual(j['done'], done)
        self.assertNotIn(j['assigned'], (None, t['id']))
        o = self.e.snapshot()['agent_mesh']['orchestration']
        self.assertGreaterEqual(o['blocked_plans'], 1)
        self.assertTrue(o['fallback_active'])
        self.assertEqual(self.e.agents.last_round['proposed_by'], 'Fallback planner')
        self.assertIn('fallback planner', self.e.snapshot()['explanation'])
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_open_breaker_skips_the_planner(self):
        self.e.action('fault', {'agent': 'Planner', 'mode': 'timeout'})
        self.e.action('create_job', {'name': 'Extra plowing', 'field': 'North Ridge', 'implement': 'plow',
                                     'units': 5, 'deadline': 90, 'priority': 3})
        self.assertNotIn('breaker.open', self.verbs())     # first failure trips the breaker
        self.assertEqual(self.e.agents.last_round['proposed_by'], 'Fallback planner')
        holder = self.e.tractor(self.e.job_by_id('J6')['assigned'])
        self.e.action('drain', {'tractor': holder['id']})  # releases J6
        self.e.action('recharge', {'tractor': holder['id']})  # replans while the breaker is open
        self.assertIn('breaker.open', self.verbs())
        self.assertEqual(self.e.job_by_id('J6')['assigned'], holder['id'])
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_ghost_bid_for_missing_tractor_is_rejected_then_next_round_passes(self):
        idle = [t['id'] for t in self.e.tractors]
        self.e.action('fault', {'agent': idle[0]})       # that agent also bids for "T9"
        t, _ = self.break_j1_worker()
        self.assertTrue(any('T9' in e['message'] for e in self.e.events))
        self.assertGreaterEqual(self.e.agents.blocked_plans, 1)
        self.assertEqual(self.e.agents.last_round['proposed_by'], 'Planner')
        self.assertIn(self.e.job_by_id('J1')['assigned'], {x['id'] for x in self.e.tractors} - {t['id']})
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_explanation_names_tractor_job_and_deadline(self):
        self.break_j1_worker()
        text = self.e.snapshot()['explanation']
        j = self.e.job_by_id('J1')
        self.assertIn(self.e.tractor(j['assigned'])['name'], text)
        self.assertIn(f"T+{j['deadline']}m", text)

    def test_reset_clears_injected_faults(self):
        self.e.action('fault', {'agent': 'Planner'})
        self.e.action('reset', {})
        o = self.e.snapshot()['agent_mesh']['orchestration']
        self.assertEqual(o['faults'], {})
        self.assertFalse(o['fallback_active'])

    def test_unknown_fault_target_is_refused(self):
        with self.assertRaises(ValueError):
            self.e.action('fault', {'agent': 'T9'})


class ReserveAndCheckpointTests(unittest.TestCase):
    """Ideas from FarmOS: a remembered battery reserve and saved progress checkpoints."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = FleetEngine(runtime_dir=self.tmp.name)

    def tearDown(self):
        self.e.agents.pool.shutdown(wait=False)
        self.tmp.cleanup()

    def half_charged_t2(self):
        """T2 drops J2, then comes back with 60% battery: enough for J2 alone, not with a big reserve."""
        self.e.action('drain', {'tractor': 'T2'})
        t2 = self.e.tractor('T2')
        t2['battery'], t2['status'] = 60.0, 'idle'
        return t2

    def test_reserve_blocks_assignment_that_would_breach_it(self):
        self.e.action('set_reserve', {'reserve': 30})
        self.half_charged_t2()
        self.e.plan()
        self.assertIsNone(self.e.job_by_id('J2')['assigned'])   # 60 - 39 = 21% left < 30% reserve
        self.assertIn('30% reserve', self.e.snapshot()['explanation'])
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_tractor_agent_declines_with_reserve_reason(self):
        self.e.action('set_reserve', {'reserve': 30})
        t2 = self.half_charged_t2()
        bids = TractorAgent('T2').bid(self.e, [self.e.job_by_id('J2')])
        self.assertTrue(bids[0]['decline'])
        self.assertIn('30% reserve', bids[0]['reason'])
        self.assertEqual(t2['status'], 'idle')

    def test_without_reserve_the_same_tractor_is_assigned(self):
        self.half_charged_t2()
        self.e.plan()
        self.assertEqual(self.e.job_by_id('J2')['assigned'], 'T2')

    def test_reserve_is_remembered_across_reset_and_restart(self):
        self.e.action('set_reserve', {'reserve': 25})
        self.e.action('reset', {})
        self.assertEqual(self.e.reserve, 25)
        again = FleetEngine(runtime_dir=self.tmp.name)
        self.assertEqual(again.reserve, 25)
        self.assertEqual(again.snapshot()['reserve'], 25)
        again.agents.pool.shutdown(wait=False)

    def test_invalid_reserve_is_refused(self):
        with self.assertRaises(ValueError):
            self.e.action('set_reserve', {'reserve': 99})
        with self.assertRaises(ValueError):
            self.e.action('set_reserve', {'reserve': 'lots'})
        self.assertEqual(self.e.reserve, 0)

    def break_j1_worker(self):
        self.e.action('start', {})
        for _ in range(7):
            self.e.tick()
        t = next(t for t in self.e.tractors if t['job'] == 'J1')
        done = self.e.job_by_id('J1')['done']
        self.e.action('fail', {'tractor': t['id']})
        return t, done

    def test_breakdown_saves_checkpoint_and_resume_is_explained(self):
        t, done = self.break_j1_worker()
        cp = self.e.snapshot()['agent_mesh']['last_checkpoint']
        self.assertEqual((cp['job'], cp['done'], cp['tractor']), ('J1', done, t['id']))
        self.assertEqual(self.e.agents.memory.saved_progress(self.e.run, 'J1'), done)
        self.assertIn(f'resuming from the saved checkpoint at {done}/24 units', self.e.snapshot()['explanation'])

    def test_guard_blocks_restart_below_the_checkpoint(self):
        t, done = self.break_j1_worker()
        j = self.e.job_by_id('J1')
        holder = self.e.tractor(j['assigned'])
        holder['job'], j['assigned'], j['done'] = None, None, 0     # a planner that "forgot" the progress
        rules = [v['rule'] for v in self.e.agents.guard.verify(self.e, [{'tractor': holder['id'], 'job': 'J1'}])]
        self.assertIn('progress_lost', rules)

    def test_checkpoints_do_not_leak_into_a_new_run(self):
        self.break_j1_worker()
        self.e.action('reset', {})
        self.assertIsNone(self.e.agents.memory.saved_progress(self.e.run, 'J1'))
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)


class AutoRecoveryTests(unittest.TestCase):
    """A judge's sabotage must never dead-end the demo, and reserved work is never released early."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = FleetEngine(runtime_dir=self.tmp.name)
        self.e.action('start', {})
        for _ in range(4):
            self.e.tick()

    def tearDown(self):
        self.e.agents.pool.shutdown(wait=False)
        self.tmp.cleanup()

    def run_minutes(self, n):
        for _ in range(n):
            self.e.tick()

    def complete(self, limit=400):
        for _ in range(limit):
            if all(j['done'] >= j['total'] for j in self.e.jobs):
                return True
            self.e.tick()
        return all(j['done'] >= j['total'] for j in self.e.jobs)

    def test_lost_signal_returns_on_its_own_without_releasing_the_job(self):
        t = self.e.tractor('T2')
        done = self.e.job_by_id('J2')['done']
        self.e.action('disconnect', {'tractor': 'T2'})
        self.run_minutes(4)
        self.assertEqual(t['status'], 'offline')                       # still unknown: held, not released
        self.assertEqual(self.e.job_by_id('J2')['assigned'], 'T2')
        self.assertEqual(self.e.job_by_id('J2')['done'], done)
        self.run_minutes(FleetEngine.SIGNAL_BACK_MIN)
        self.assertEqual(t['status'], 'working')                       # same tractor, same job
        self.assertEqual(t['job'], 'J2')
        self.assertTrue(self.complete())
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_sole_implement_breakdown_is_repaired_and_day_completes(self):
        self.e.action('fail', {'tractor': 'T2'})                       # the only irrigator
        self.assertIsNone(self.e.job_by_id('J2')['assigned'])
        self.run_minutes(FleetEngine.FIELD_TECH_MIN + 1)
        self.assertNotEqual(self.e.tractor('T2')['status'], 'failed')
        self.assertTrue(self.complete())
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_drained_battery_recharges_and_day_completes(self):
        self.e.action('drain', {'tractor': 'T3'})                      # the only harvester
        self.run_minutes(FleetEngine.CHARGE_MIN + 1)
        t = self.e.tractor('T3')
        self.assertNotEqual(t['status'], 'charging')
        self.assertGreaterEqual(t['battery'], 90)
        self.assertTrue(self.complete())

    def test_confirmed_stop_is_repaired_by_the_field_tech(self):
        self.e.action('disconnect', {'tractor': 'T4'})
        self.e.action('confirm_stop', {'tractor': 'T4'})
        self.assertEqual(self.e.tractor('T4')['status'], 'quarantined')
        self.run_minutes(FleetEngine.FIELD_TECH_MIN + 1)
        self.assertIn(self.e.tractor('T4')['status'], ('idle', 'working'))
        self.assertTrue(self.complete())

    def test_manual_repair_still_works_and_beats_the_timer(self):
        self.e.action('fail', {'tractor': 'T2'})
        self.e.action('restore', {'tractor': 'T2'})                    # judge presses Repair at once
        self.assertIn(self.e.tractor('T2')['status'], ('idle', 'working'))  # replanned straight back to work
        self.run_minutes(FleetEngine.FIELD_TECH_MIN + 2)               # stale timer must not fire twice
        self.assertIn(self.e.tractor('T2')['status'], ('idle', 'working'))
        self.assertTrue(self.complete())


class AgOpenWebFeedTests(unittest.TestCase):
    """The GPS feed is tested with a fake UDP sender; nothing goes on the network."""
    def setUp(self):
        from agopenweb_bridge import AgOpenWebBridge
        self.tmp = tempfile.TemporaryDirectory()
        self.e = FleetEngine(runtime_dir=self.tmp.name)
        self.sent = []
        self.b = AgOpenWebBridge(self.e, send=self.sent.append, detect=lambda: True, env={})
        self.e.bridge = self.b

    def tearDown(self):
        self.e.agents.pool.shutdown(wait=False)
        self.tmp.cleanup()

    def frames(self, n, start=100.0):
        out = None
        for k in range(n):
            out = self.b.step(start + k * 0.25)
        return out

    def test_sentences_are_valid_nmea_inside_the_plot(self):
        from agopenweb_bridge import BOUNDARY, LAT0, LON0, M_PER_DEG_LAT, M_PER_DEG_LON
        payload = self.frames(2)
        gga, vtg = payload.strip().split('\r\n')
        for line in (gga, vtg):
            body, checksum = line[1:].split('*')
            calc = 0
            for ch in body:
                calc ^= ord(ch)
            self.assertEqual(checksum, f'{calc:02X}')
        f = gga.split(',')
        lat = int(f[2][:2]) + float(f[2][2:]) / 60
        lon = -(int(f[4][:3]) + float(f[4][3:]) / 60)
        east, north = (lon - LON0) * M_PER_DEG_LON, (lat - LAT0) * M_PER_DEG_LAT
        self.assertTrue(BOUNDARY[0] <= east <= BOUNDARY[1] and BOUNDARY[2] <= north <= BOUNDARY[3])
        self.assertEqual((f[3], f[5], f[6]), ('N', 'W', '4'))
        self.assertTrue(vtg.startswith('$GPVTG,'))

    def test_auto_follow_switches_to_the_tractor_that_takes_over(self):
        self.e.action('start', {})
        for _ in range(6):
            self.e.tick()
        first = self.e.job_by_id('J1')['assigned']
        self.frames(2)
        self.assertEqual(self.b.feeding, first)
        self.e.action('fail', {'tractor': first})
        self.frames(1, start=200)
        self.assertEqual(self.b.feeding, self.e.job_by_id('J1')['assigned'])
        self.assertNotEqual(self.b.feeding, first)

    def test_auto_follow_stays_with_the_work_after_the_first_job(self):
        self.e.action('start', {})
        self.frames(1)
        first = self.b.feeding
        for k in range(30):                      # J1 (24 units) finishes; its tractor moves on to J4
            self.e.tick()
            self.b.step(150 + k)
        self.assertEqual(self.e.job_by_id('J1')['done'], 24)
        planter = self.e.job_by_id('J4')['assigned']            # plowing done -> planting starts
        self.assertIsNotNone(planter)
        self.assertEqual(self.b.feeding, planter)
        self.assertNotEqual(first, None)
        self.e.action('fail', {'tractor': planter})              # break the planter mid-job
        self.b.step(400)
        holder = self.e.job_by_id('J4')['assigned']
        self.assertEqual(self.b.feeding, holder or planter)      # successor, or the stopped tractor

    def test_reset_starts_the_feed_fresh(self):
        self.e.action('start', {})
        for _ in range(6):
            self.e.tick()
        self.frames(1)
        self.e.action('fail', {'tractor': self.b.feeding})
        self.frames(1, start=200)
        self.e.action('reset', {})
        self.assertEqual(self.b.positions, {})
        self.frames(1, start=300)
        self.assertEqual(self.b.feeding, self.e.job_by_id('J1')['assigned'])

    def test_silent_tractor_sends_no_gps(self):
        self.b.set_follow('T2')
        self.assertIsNotNone(self.frames(1))
        self.e.action('disconnect', {'tractor': 'T2'})
        before = len(self.sent)
        self.assertIsNone(self.frames(4, start=300))
        self.assertEqual(len(self.sent), before)
        self.assertEqual(self.e.snapshot()['agopenweb']['state'], 'signal_lost')

    def test_waits_quietly_when_agopenweb_is_not_running(self):
        from agopenweb_bridge import AgOpenWebBridge
        b = AgOpenWebBridge(self.e, send=self.sent.append, detect=lambda: False, env={})
        self.assertIsNone(b.step(1.0))
        self.assertEqual(b.status()['state'], 'waiting')
        self.assertEqual(self.sent, [])

    def test_follow_choice_is_validated_and_logged(self):
        msg = self.e.action('agopenweb_follow', {'tractor': 'T3'})
        self.assertIn('Titan 03', msg)
        with self.assertRaises(ValueError):
            self.e.action('agopenweb_follow', {'tractor': 'T9'})

    def test_setup_script_writes_an_isolated_profile(self):
        from agopenweb_setup import write_profile
        root = write_profile(self.tmp.name)
        self.assertFalse(json.loads((root / 'appsettings.json').read_text())['simulatorEnabled'])
        self.assertIn('-391,181,0', (root / 'Fields' / 'FleetPhoenix Farm' / 'Boundary.txt').read_text())


class LiveTestBoardTests(unittest.TestCase):
    def test_every_scenario_on_the_board_passes(self):
        from scenarios import SCENARIOS, run_suite
        r = run_suite(FleetEngine)
        failed = [(x['id'], x['metrics']) for x in r['results'] if not x['passed']]
        self.assertEqual(failed, [])
        self.assertEqual(r['total'], len(SCENARIOS))

    def test_board_run_does_not_touch_the_live_farm(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = FleetEngine(runtime_dir=tmp)
            before = [(j['id'], j['done'], j['assigned']) for j in e.jobs]
            from scenarios import SCENARIOS
            n = len(SCENARIOS)
            msg = e.action('run_tests', {})
            self.assertIn(f'{n} of {n}', msg)
            self.assertEqual([(j['id'], j['done'], j['assigned']) for j in e.jobs], before)
            self.assertEqual(e.snapshot()['tests']['passed'], n)
            e.agents.pool.shutdown(wait=False)


class JobListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = FleetEngine(runtime_dir=self.tmp.name)

    def tearDown(self):
        self.e.agents.pool.shutdown(wait=False)
        self.tmp.cleanup()

    def submit(self, csv):
        return self.e.action('submit_jobs', {'csv': csv})

    def test_valid_list_adds_jobs_with_dependency_and_plans_them(self):
        from scenarios import JOB_LIST
        msg = self.submit(JOB_LIST)
        self.assertIn('3 jobs added: J6, J7, J8', msg)
        self.assertEqual(self.e.job_by_id('J7')['depends'], 'J6')
        self.assertIsNotNone(self.e.job_by_id('J6')['assigned'])      # an idle plow tractor takes it
        self.assertIsNone(self.e.job_by_id('J7')['assigned'])         # waits for the plowing
        self.assertTrue(any(m['sender'] == 'Intake' for m in self.e.agents.messages))
        self.assertEqual(self.e.snapshot()['metrics']['violations'], 0)

    def test_any_bad_line_rejects_the_whole_list(self):
        from scenarios import BAD_JOB_LIST
        with self.assertRaises(ValueError) as ctx:
            self.submit(BAD_JOB_LIST)
        self.assertIn('Line 2 (Plow West Terrace): missing deadline', str(ctx.exception))
        self.assertIn('unknown implement "spray"', str(ctx.exception))
        self.assertEqual(len(self.e.jobs), 5)

    def test_unknown_dependency_and_bad_header_are_flagged(self):
        with self.assertRaises(ValueError) as ctx:
            self.submit('name,field,implement,units,deadline,priority,depends\nX,North Ridge,seed,5,90,2,J99')
        self.assertIn('depends on "J99"', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.submit('name,field,units\nX,North Ridge,5')
        self.assertIn('missing: implement, deadline, priority', str(ctx.exception))
        self.assertEqual(len(self.e.jobs), 5)

    def test_names_are_case_insensitive_and_duplicates_refused(self):
        self.submit('name,field,implement,units,deadline,priority\nA,north ridge,SEED,5,90,2')
        self.assertEqual(self.e.job_by_id('J6')['field'], 'North Ridge')
        self.assertEqual(self.e.job_by_id('J6')['implement'], 'seed')
        with self.assertRaises(ValueError):
            self.submit('name,field,implement,units,deadline,priority\nA,North Ridge,seed,5,90,2')


if __name__ == '__main__':
    unittest.main(verbosity=2)
