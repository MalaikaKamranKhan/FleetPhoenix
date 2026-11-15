"""Read-only GPS feed from FleetPhoenix to AgOpenWeb.

AgOpenWeb shows one vehicle's guidance view. With its internal simulator switched off it
accepts standard NMEA GPS sentences ($GPGGA position + $GPVTG course/speed) on UDP 9999.
This bridge sends those sentences for ONE FleetPhoenix tractor so AgOpenWeb draws it
moving over the "Sonoran Fleet Plot" field. It never sends commands to AgOpenWeb and never
changes FleetPhoenix's plan; it only reports simulated positions.

Settings (environment variables):
  FLEET_AGOPENWEB       "off" disables the feed (default: on when AgOpenWeb is detected)
  FLEET_AGOPENWEB_URL   where to detect AgOpenWeb (default http://127.0.0.1:5174)
  FLEET_AGOPENWEB_UDP   host:port for GPS sentences (default 127.0.0.1:9999)
"""
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone
from urllib import request

# StartFix of the "Sonoran Fleet Plot" field (local metres are measured from here).
LAT0, LON0 = 32.45132, -111.24680675
M_PER_DEG_LAT = 111320.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))
BOUNDARY = (-391, 375, -175, 181)          # east_min, east_max, north_min, north_max
FIELDS = {                                # three strips inside the boundary, west to east
    'West Terrace': (-380, -135, -160, 170),
    'North Ridge': (-120, 120, -160, 170),
    'East Orchard': (135, 365, -160, 170),
}
DEPOT_NORTH = -169                         # parking line along the south edge
SIM_SPEEDUP = 60                           # 1 real second = 1 simulated minute
TRANSIT_MPS = 150.0                        # on-screen travel speed when a tractor drives to work
HZ = 4


def to_latlon(east, north):
    return LAT0 + north / M_PER_DEG_LAT, LON0 + east / M_PER_DEG_LON


def nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}'


def _dm(value, width):
    v = abs(value)
    d = int(v)
    return f'{d:0{width}d}{(v - d) * 60:08.5f}'


def sentences(east, north, heading_deg, speed_kmh, when=None):
    """One GGA (position) + VTG (course and speed) pair, checksummed, CRLF-terminated."""
    lat, lon = to_latlon(east, north)
    stamp = (when or datetime.now(timezone.utc)).strftime('%H%M%S.%f')[:9]
    gga = nmea(f"GPGGA,{stamp},{_dm(lat, 2)},{'N' if lat >= 0 else 'S'},{_dm(lon, 3)},{'E' if lon >= 0 else 'W'},"
               '4,10,0.9,700.0,M,0.0,M,,')
    vtg = nmea(f'GPVTG,{heading_deg % 360:.1f},T,,M,{speed_kmh / 1.852:.2f},N,{speed_kmh:.2f},K,A')
    return f'{gga}\r\n{vtg}\r\n'


def depot(tractor_index):
    return -300 + tractor_index * 150.0, DEPOT_NORTH


class AgOpenWebBridge:
    def __init__(self, engine, send=None, detect=None, env=None):
        env = os.environ if env is None else env
        self.engine = engine
        self.enabled = env.get('FLEET_AGOPENWEB', 'on').lower() not in ('off', '0', 'false', 'no')
        self.url = env.get('FLEET_AGOPENWEB_URL', 'http://127.0.0.1:5174')
        host, _, port = env.get('FLEET_AGOPENWEB_UDP', '127.0.0.1:9999').rpartition(':')
        self.target = (host or '127.0.0.1', int(port or 9999))
        self.send = send or self._udp_send
        self.detect = detect or self._detect
        self.follow = 'auto'                 # 'auto' (follow the work, starting on J1) or a tractor id
        self.feeding = None                  # tractor id currently on the feed
        self.followed_job = None             # job being shown in auto mode
        self.positions = {}                  # tractor id -> (east, north) shown on screen
        self.frac = {}                       # tractor id -> fraction along its current pass
        self.detected = False
        self.fixes_sent = 0
        self.last_detect = 0.0
        self.last_step = None
        self.signal_lost = False
        self._sock = None
        self._thread = None

    # ---- transport ----
    def _udp_send(self, payload):
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.sendto(payload.encode('ascii'), self.target)

    def _detect(self):
        try:
            with request.urlopen(self.url, timeout=1.0) as resp:
                return b'AgOpenWeb' in resp.read(4096)
        except Exception:
            return False

    def reset(self):
        """Called on a demo reset: forget the old run's positions; keep the follow setting."""
        self.positions.clear()
        self.frac.clear()
        self.feeding = self.follow if self.follow != 'auto' else None
        self.followed_job = None
        self.signal_lost = False

    # ---- which tractor ----
    def set_follow(self, value):
        value = str(value or 'auto')
        if value != 'auto' and not self.engine.tractor(value):
            raise ValueError('Follow "auto" or a tractor id such as T1.')
        self.follow = value
        self.feeding = value if value != 'auto' else None
        self.followed_job = None
        return value

    def _choose(self):
        """Auto mode follows the WORK: start on whoever plows North Ridge (J1); if another
        tractor takes over the job on screen, switch to it; when the job is finished, stay
        with the same tractor on its next job. A stopped tractor with no successor stays shown."""
        e = self.engine
        if self.follow != 'auto':
            return self.follow
        if self.followed_job:
            j = e.job_by_id(self.followed_job)
            if j and j['assigned']:
                return j['assigned']
            if j and j['done'] >= j['total']:
                # Finished: show the job that was waiting for it (plow -> plant), else the same field.
                nxt = next((x for x in e.jobs if x['depends'] == j['id'] and x['assigned']), None) or \
                      next((x for x in e.jobs if x['field'] == j['field'] and x['assigned']), None)
                if nxt:
                    self.followed_job = nxt['id']
                    return nxt['assigned']
        cur = e.tractor(self.feeding) if self.feeding else None
        if cur:
            if cur['job']:
                self.followed_job = cur['job']
            return cur['id']
        j1 = e.job_by_id('J1')
        start = j1['assigned'] if j1 and j1['assigned'] else None
        if not start:
            working = [t for t in e.tractors if t['status'] == 'working']
            start = (working or e.tractors)[0]['id']
        self.followed_job = e.tractor(start)['job']
        return start

    # ---- geometry ----
    def _target(self, t, index, job, frac):
        if t['status'] == 'working' and job and t['field'] in FIELDS:
            e0, e1, n0, n1 = FIELDS[t['field']]
            passes = max(1, job['total'])
            row = min(job['done'], passes - 1)
            east = e0 + (row + 0.5) * (e1 - e0) / passes
            along = n0 + frac * (n1 - n0) if row % 2 == 0 else n1 - frac * (n1 - n0)
            return east, along
        # Not working: stay where it is (a breakdown stops it in place), else park at the depot.
        return self.positions.get(t['id'], depot(index))

    def step(self, now=None):
        """Advance one frame. Returns the NMEA payload sent, or None when nothing was sent."""
        now = time.monotonic() if now is None else now
        dt = 1.0 / HZ if self.last_step is None else max(0.0, min(1.0, now - self.last_step))
        self.last_step = now
        if now - self.last_detect > 5 or not self.last_detect:
            self.last_detect = now
            self.detected = self.enabled and bool(self.detect())
        e = self.engine
        with e.lock:
            tid = self._choose()
            self.feeding = tid
            frames = []
            for i, t in enumerate(e.tractors):
                job = e.job_by_id(t['job']) if t['job'] else None
                since = now - getattr(e, 'last_tick_at', now)
                frac = self.frac.get(t['id'], 0.0) if e.paused else max(0.0, min(1.0, since))
                self.frac[t['id']] = frac
                frames.append((t['id'], t['status'], self._target(t, i, job, frac)))
        heading, speed = 0.0, 0.0
        for t_id, status, (tx, ty) in frames:
            px, py = self.positions.get(t_id, (tx, ty))
            dist = math.hypot(tx - px, ty - py)
            working_pass = status == 'working' and dist <= (BOUNDARY[3] - BOUNDARY[2])
            limit = dist if working_pass else TRANSIT_MPS * dt
            if dist > limit > 0:
                tx, ty = px + (tx - px) * limit / dist, py + (ty - py) * limit / dist
            self.positions[t_id] = (tx, ty)
            if t_id == tid:
                moved = math.hypot(tx - px, ty - py)
                if moved > 0.01:
                    heading = math.degrees(math.atan2(tx - px, ty - py))
                    speed = moved / max(dt, 1e-3) / SIM_SPEEDUP * 3.6   # real-world equivalent km/h
        status = next(s for t_id, s, _ in frames if t_id == tid)
        self.signal_lost = status == 'offline'
        if not self.detected or self.signal_lost:      # a silent tractor sends no GPS, honestly
            return None
        x, y = self.positions[tid]
        payload = sentences(x, y, heading, speed)
        try:
            self.send(payload)
            self.fixes_sent += 1
        except OSError:
            return None
        return payload

    # ---- lifecycle ----
    def start(self):
        if self._thread or not self.enabled:
            return
        def loop():
            while True:
                try:
                    self.step()
                except Exception as exc:  # never let the feed take the farm down
                    print('AgOpenWeb feed error:', repr(exc))
                time.sleep(1.0 / HZ)
        self._thread = threading.Thread(target=loop, name='agopenweb-feed', daemon=True)
        self._thread.start()

    def status(self):
        t = self.engine.tractor(self.feeding) if self.feeding else None
        if not self.enabled:
            state = 'off'
        elif not self.detected:
            state = 'waiting'
        elif self.signal_lost:
            state = 'signal_lost'
        else:
            state = 'sending'
        return {'state': state, 'follow': self.follow, 'feeding': t['name'] if t else None,
                'feeding_id': self.feeding, 'fixes_sent': self.fixes_sent, 'url': self.url}
