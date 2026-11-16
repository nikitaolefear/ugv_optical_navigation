import unittest
from concurrent.futures import ThreadPoolExecutor
from web_hmi.test_support import TestEnvironment
from web_hmi.simulator import Simulator


class HMITest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sim = Simulator(lambda: self.now)
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        self.client = self.env.client(self.sim)
        self.sequence = 0

    def send(self, action, **extra):
        self.sequence += 1
        body = dict(action=action, revision=self.sim.snapshot()['revision'], request_id=str(self.sequence))
        body.update(extra)
        return self.client.post('/api/command', json=body)

    def test_both_missions_complete_without_turn(self):
        for mission, start, end, speed in [(1, 0, 10, .5), (2, 10, 0, -.5)]:
            self.send('reset')
            self.assertEqual(self.send('select', mission=mission).status_code, 200)
            self.assertEqual(self.sim.snapshot()['position_m'], start)
            self.send('start', confirmed=True)
            self.assertEqual(self.sim.snapshot()['speed_m_s'], speed)
            self.now += 20
            state = self.client.get('/api/status').json
            self.assertEqual((state['state'], state['position_m'], state['heading_deg']), ('COMPLETE', end, 0))
            self.assertEqual(state['speed_m_s'], 0)

    def test_pause_resume_complete_both_missions(self):
        for mission in (1, 2):
            self.send('reset')
            self.send('select', mission=mission)
            self.send('start', confirmed=True)
            self.now += 4
            paused = self.send('pause').json
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['progress'], 20)
            self.assertEqual(paused['speed_m_s'], 0)
            self.now += 100
            frozen = self.sim.snapshot()
            for key in ('mission', 'direction', 'progress', 'target', 'position_m', 'target_position_m'):
                self.assertEqual(frozen[key], paused[key])
            self.assertEqual(self.send('resume').status_code, 200)
            self.now += 2
            self.assertEqual(self.sim.snapshot()['progress'], 30)
            self.now += 14
            self.assertEqual(self.sim.snapshot()['state'], 'COMPLETE')

    def test_pause_stop_resume_forbidden(self):
        self.send('start', confirmed=True)
        self.send('pause')
        self.assertEqual(self.send('stop', revision=-100).json['state'], 'STOPPED')
        self.assertEqual(self.send('resume').status_code, 409)
        self.assertEqual(self.send('start', confirmed=True).status_code, 409)

    def test_protective_states_forbid_resume(self):
        for action, state in [('hold', 'HOLD'), ('fault', 'FAULT'), ('estop', 'E_STOP')]:
            self.send('reset')
            self.send('start', confirmed=True)
            self.send('pause')
            result = self.send(action, revision=-100).json
            self.assertEqual(result['state'], state)
            self.assertFalse(result['resume_allowed'])
            self.assertEqual(self.send('resume').status_code, 409)
            self.assertEqual(self.send('start', confirmed=True).status_code, 409)
            if action != 'hold':
                self.assertEqual(self.send('stop').json['state'], state)
                self.assertEqual(self.send('hold').json['state'], state)

    def test_paused_page_reload(self):
        self.send('start', confirmed=True)
        self.now = 5
        paused = self.send('pause').json
        self.now = 100
        other = self.env.client(self.sim)
        self.assertEqual(other.get('/').status_code, 200)
        self.assertEqual(other.get('/api/status').json, paused)

    def test_double_resume(self):
        self.send('start', confirmed=True)
        self.now = 4
        self.send('pause')
        body = dict(action='resume', revision=self.sim.snapshot()['revision'], request_id='resume-once')
        self.assertEqual(self.client.post('/api/command', json=body).status_code, 200)
        self.now = 6
        repeated = self.client.post('/api/command', json=body).json
        self.assertEqual(repeated['progress'], 30)
        self.assertEqual(self.send('resume').status_code, 409)
        self.assertEqual(sum('RESUME:' in e['message'] for e in self.sim.events), 1)

    def test_concurrent_resume_only_once(self):
        self.send('start', confirmed=True)
        self.now = 4
        self.send('pause')
        revision = self.sim.snapshot()['revision']
        clients = [self.env.client(self.sim) for _ in range(8)]
        def resume(i):
            with clients[i] as client:
                return client.post('/api/command', json=dict(action='resume', revision=revision, request_id=f'resume-{i}')).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(resume, range(8)))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        self.assertEqual(self.sim.snapshot()['progress'], 20)

    def test_resume_requires_pause(self):
        self.assertEqual(self.send('resume').status_code, 409)
        self.assertEqual(self.send('pause').status_code, 409)
        self.send('start', confirmed=True)
        self.assertEqual(self.send('resume').status_code, 409)
        self.now = 20
        self.assertEqual(self.send('resume').status_code, 409)

    def test_precheck_is_demo_only(self):
        policy = self.sim.snapshot()['resume_precheck']
        self.assertEqual(policy['mode'], 'DEMO_ONLY')
        self.assertFalse(policy['real_resume_supported'])
        self.assertEqual(len(policy['real_requirements']), 6)

    def test_reset_never_starts(self):
        for action in ('start', 'pause', 'hold', 'stop', 'fault', 'estop'):
            self.send('start', confirmed=True)
            self.send(action, confirmed=True)
            state = self.send('reset').json
            self.assertEqual((state['state'], state['progress'], state['speed_m_s']), ('READY', 0, 0))
            self.now += 100
            self.assertEqual(self.sim.snapshot()['state'], 'READY')
            self.assertEqual(self.send('resume').status_code, 409)

    def test_invalid_transitions(self):
        self.assertEqual(self.send('start').status_code, 409)
        self.assertEqual(self.send('select', mission=True).status_code, 409)
        self.assertEqual(self.send('select', mission=3).status_code, 409)
        self.assertEqual(self.send('unknown').status_code, 409)
        self.send('start', confirmed=True)
        for action in ('start', 'select'):
            self.assertEqual(self.send(action, confirmed=True, mission=2).status_code, 409)
        self.send('fault')
        self.assertEqual(self.send('start', confirmed=True).status_code, 409)

    def test_repeated_request_does_not_restart(self):
        body = dict(action='start', confirmed=True, revision=0, request_id='same')
        self.assertEqual(self.client.post('/api/command', json=body).status_code, 200)
        self.now = 5
        self.assertEqual(self.client.post('/api/command', json=body).json['position_m'], 2.5)
        self.send('stop')
        self.assertEqual(self.client.post('/api/command', json=body).json['state'], 'STOPPED')
        body['action'] = 'reset'
        self.assertEqual(self.client.post('/api/command', json=body).status_code, 409)

    def test_stale_commands_rejected(self):
        self.send('select', mission=2)
        self.assertEqual(self.send('start', confirmed=True, revision=0).status_code, 409)
        self.assertEqual(self.sim.snapshot()['state'], 'READY')

    def test_page_reload_and_second_client(self):
        self.send('start', confirmed=True)
        self.now = 7
        revision = self.sim.snapshot()['revision']
        other = self.env.client(self.sim)
        for client in (self.client, other):
            self.assertEqual(client.get('/').status_code, 200)
            state = client.get('/api/status').json
            self.assertEqual((state['state'], state['position_m'], state['revision']), ('MOVING', 3.5, revision))
            self.assertFalse(state['movement_allowed'])
            self.assertIsNone(state['armed'])
            self.assertEqual(state['physical_telemetry'], 'UNAVAILABLE')

    def test_malformed_and_get_cannot_command(self):
        for body in ([], None, 'start', {}):
            self.assertIn(self.client.post('/api/command', json=body).status_code, (400, 409))
        self.assertEqual(self.client.get('/api/command').status_code, 405)
        self.assertEqual(self.sim.snapshot()['state'], 'READY')

    def test_clock_fault_and_recovery(self):
        self.send('start', confirmed=True)
        self.now = -1
        self.assertEqual(self.sim.snapshot()['state'], 'FAULT')
        self.assertEqual(self.sim.snapshot()['speed_m_s'], 0)
        self.assertEqual(self.send('reset').json['state'], 'READY')

    def test_concurrent_start_only_once(self):
        clients = [self.env.client(self.sim) for _ in range(8)]
        def start(i):
            with clients[i] as client:
                return client.post('/api/command', json=dict(action='start', confirmed=True, revision=0, request_id=f'parallel-{i}')).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, range(8)))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)

    def test_static_assets(self):
        for path in ('/', '/static/app.js', '/static/style.css'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            response.close()


if __name__ == '__main__':
    unittest.main()
