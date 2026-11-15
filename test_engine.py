"""Core engine invariants. Run: python test_engine.py"""
import base64
import io
import json
import os
import unittest

# Tests must never text anyone, even on a laptop where Twilio is configured.
for _key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER', 'FLEET_OWNER_PHONE'):
    os.environ.pop(_key, None)
from unittest import mock
from urllib.parse import parse_qs
from app import FleetEngine
from notifier import OwnerNotifier


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

class OwnerAlertTests(unittest.TestCase):
    """No real texts are ever sent here: either dry-run mode or a fake transport."""
    ENV = {'TWILIO_ACCOUNT_SID': 'ACtest', 'TWILIO_AUTH_TOKEN': 'secret',
           'TWILIO_FROM_NUMBER': '+15205550100', 'FLEET_OWNER_PHONE': '+15205550123'}

    def break_j1_worker(self, e):
        e.action('start', {})
        for _ in range(6):
            e.tick()
        t = next(t for t in e.tractors if t['job'] == 'J1')
        e.action('fail', {'tractor': t['id']})
        if e.notifier.last_thread:
            e.notifier.last_thread.join(5)
        return t

    def test_without_credentials_alert_is_logged_not_sent(self):
        e = FleetEngine(notifier=OwnerNotifier(env={}))
        t = self.break_j1_worker(e)
        text = next(ev['message'] for ev in e.events if 'OWNER ALERT' in ev['message'])
        self.assertIn('dry run, no SMS sent', text)
        self.assertIn(f"{t['name']} broke down at North Ridge", text)
        self.assertIn('Prepare North Ridge saved at 6/24 units', text)
        self.assertIn('has taken over', text)
        self.assertEqual(e.snapshot()['owner_alerts']['mode'], 'dry-run')

    def test_with_credentials_text_goes_to_the_owner(self):
        sent = []
        n = OwnerNotifier(env=self.ENV, transport=lambda to, frm, body: sent.append((to, frm, body)) or 'SM123')
        e = FleetEngine(notifier=n)
        t = self.break_j1_worker(e)
        self.assertEqual(len(sent), 1)
        to, frm, body = sent[0]
        self.assertEqual((to, frm), ('+15205550123', '+15205550100'))
        self.assertTrue(body.startswith(f"FleetPhoenix alert: {t['name']} broke down"))
        self.assertTrue(any('OWNER ALERT texted to •••0123' in ev['message'] for ev in e.events))
        self.assertNotIn('+15205550123', json.dumps(e.snapshot()))   # full number never exposed

    def test_failed_send_is_reported_and_farm_keeps_running(self):
        def boom(to, frm, body):
            raise RuntimeError('Twilio returned 401: Authenticate')
        e = FleetEngine(notifier=OwnerNotifier(env=self.ENV, transport=boom))
        self.break_j1_worker(e)
        self.assertTrue(any('not delivered: Twilio returned 401' in ev['message'] for ev in e.events))
        self.assertIsNotNone(e.job_by_id('J1')['assigned'])
        self.assertEqual(e.snapshot()['metrics']['violations'], 0)

    def test_bad_phone_number_falls_back_to_dry_run(self):
        n = OwnerNotifier(env=dict(self.ENV, FLEET_OWNER_PHONE='520-555-0123'))
        self.assertEqual(n.mode, 'dry-run')
        self.assertIn('FLEET_OWNER_PHONE must look like', n.reason)

    def test_request_matches_twilio_messages_api(self):
        captured = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout):
            captured.update(url=req.full_url, data=req.data, auth=req.get_header('Authorization'), method=req.get_method())
            return Resp(b'{"sid": "SM999"}')
        n = OwnerNotifier(env=self.ENV)
        with mock.patch('notifier.request.urlopen', fake_urlopen):
            sid = n._twilio_post('+15205550123', '+15205550100', 'hello')
        self.assertEqual(sid, 'SM999')
        self.assertEqual(captured['url'], 'https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(parse_qs(captured['data'].decode()), {'To': ['+15205550123'], 'From': ['+15205550100'], 'Body': ['hello']})
        self.assertEqual(captured['auth'], 'Basic ' + base64.b64encode(b'ACtest:secret').decode())


if __name__ == '__main__':
    unittest.main(verbosity=2)
