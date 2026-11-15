"""Core engine invariants. Run: python test_engine.py"""
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


class LiveTestBoardTests(unittest.TestCase):
    def test_every_scenario_on_the_board_passes(self):
        from scenarios import run_suite
        r = run_suite(FleetEngine)
        failed = [(x['id'], x['metrics']) for x in r['results'] if not x['passed']]
        self.assertEqual(failed, [])
        self.assertEqual(r['total'], 10)

    def test_board_run_does_not_touch_the_live_farm(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = FleetEngine(runtime_dir=tmp)
            before = [(j['id'], j['done'], j['assigned']) for j in e.jobs]
            msg = e.action('run_tests', {})
            self.assertIn('10 of 10', msg)
            self.assertEqual([(j['id'], j['done'], j['assigned']) for j in e.jobs], before)
            self.assertEqual(e.snapshot()['tests']['passed'], 10)
            e.agents.pool.shutdown(wait=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
