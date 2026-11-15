"""Core engine invariants. Run: python test_engine.py"""
import unittest
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

if __name__ == '__main__':
    unittest.main(verbosity=2)
