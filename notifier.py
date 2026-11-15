"""Owner SMS alerts: text the farm owner when a tractor breaks down.

Real texts go through Twilio's Messages API and need four environment variables
(set them in the terminal that starts the app; never commit them):

    TWILIO_ACCOUNT_SID   your Twilio account SID
    TWILIO_AUTH_TOKEN    your Twilio auth token
    TWILIO_FROM_NUMBER   a Twilio number that can send SMS, e.g. +15205550100
    FLEET_OWNER_PHONE    the owner's phone, e.g. +15205550123

Without all four (or with numbers not in +E.164 format) alerts run in dry-run mode:
the exact text is shown in the dashboard's event log, but nothing is sent.
Sending happens on a background thread, so a slow network never freezes the farm.
Only the Python standard library is used.
"""
import base64
import json
import os
import re
import threading
from collections import deque
from urllib import error, parse, request

E164 = re.compile(r'^\+[1-9]\d{7,14}$')
TWILIO_URL = 'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'


def mask(number):
    """Show only the last four digits of a phone number."""
    return ('•••' + number[-4:]) if number else 'not set'


class OwnerNotifier:
    def __init__(self, env=None, transport=None):
        env = os.environ if env is None else env
        self.sid = env.get('TWILIO_ACCOUNT_SID', '').strip()
        self.token = env.get('TWILIO_AUTH_TOKEN', '').strip()
        self.sender = env.get('TWILIO_FROM_NUMBER', '').strip()
        self.owner = env.get('FLEET_OWNER_PHONE', '').strip()
        missing = [k for k, v in (('TWILIO_ACCOUNT_SID', self.sid), ('TWILIO_AUTH_TOKEN', self.token),
                                  ('TWILIO_FROM_NUMBER', self.sender), ('FLEET_OWNER_PHONE', self.owner)) if not v]
        bad = [k for k, v in (('TWILIO_FROM_NUMBER', self.sender), ('FLEET_OWNER_PHONE', self.owner))
               if v and not E164.match(v)]
        if missing:
            self.mode, self.reason = 'dry-run', 'Set ' + ', '.join(missing) + ' to send real texts.'
        elif bad:
            self.mode, self.reason = 'dry-run', ', '.join(bad) + ' must look like +15205550123.'
        else:
            self.mode, self.reason = 'twilio', ''
        self.transport = transport or self._twilio_post
        self.recent = deque(maxlen=10)
        self.last_thread = None

    def notify(self, body, on_result=None):
        """Queue one alert. on_result(record) is called once the outcome is known."""
        record = {'to': mask(self.owner), 'body': body[:320],
                  'status': 'dry-run' if self.mode == 'dry-run' else 'sending'}
        self.recent.appendleft(record)
        if self.mode == 'dry-run':
            if on_result:
                on_result(record)
            return record
        self.last_thread = threading.Thread(target=self._send, args=(record, on_result),
                                            name='owner-sms', daemon=True)
        self.last_thread.start()
        return record

    def _send(self, record, on_result):
        try:
            record['sid'] = self.transport(self.owner, self.sender, record['body'])
            record['status'] = 'sent'
        except Exception as exc:  # never let an SMS problem affect the farm
            record['status'], record['error'] = 'failed', str(exc)[:160]
        if on_result:
            on_result(record)

    def _twilio_post(self, to, sender, body):
        url = TWILIO_URL.format(sid=parse.quote(self.sid, safe=''))
        data = parse.urlencode({'To': to, 'From': sender, 'Body': body}).encode()
        auth = base64.b64encode(f'{self.sid}:{self.token}'.encode()).decode()
        req = request.Request(url, data=data, method='POST', headers={
            'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with request.urlopen(req, timeout=8) as resp:
                return json.load(resp).get('sid')
        except error.HTTPError as exc:
            try:
                detail = json.load(exc).get('message', '')
            except (ValueError, AttributeError):
                detail = ''
            raise RuntimeError(f'Twilio returned {exc.code}' + (f': {detail}' if detail else '')) from None
        except error.URLError as exc:
            raise RuntimeError(f'could not reach Twilio ({exc.reason})') from None

    def status(self):
        return {'mode': self.mode, 'to': mask(self.owner), 'reason': self.reason,
                'recent': [dict(r) for r in self.recent]}
