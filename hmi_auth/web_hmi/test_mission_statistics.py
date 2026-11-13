from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
import tempfile
import unittest
from unittest.mock import patch

from web_hmi.auth import AuthStore
from web_hmi.app import create_app
from web_hmi.mission_statistics import MissionStatistics, PERIODS, TZ, metrics, window
from web_hmi.simulator import Simulator
from web_hmi.test_support import TestEnvironment, PASSWORD, CONFIG


class StatisticsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,11,13,12,30,tzinfo=TZ).timestamp()
        self.env = TestEnvironment(clock=lambda:self.now)
        self.addCleanup(self.env.close)
        self.store = self.env.store
        self.store.add_user('operator1',PASSWORD,'operator')
        self.store.add_user('operator2',PASSWORD,'operator')
        self.actors = {row['username']: row for row in self.store.users()}
        self.stats = MissionStatistics(self.store)

    def report(self, period='all_time', **kwargs):
        return self.stats.report(period, **kwargs)['sources']['DEMO']['summary']

    def mission(self, result='COMPLETED', owner='operator1', domain='DEMO'):
        identifier = self.stats.start(self.actors[owner], 1, domain=domain)
        if result:
            self.stats.finish(identifier,result)
        return identifier

    def test_calendar_periods_kyiv_and_iso_monday(self):
        for period in PERIODS:
            start,end = window(period,self.now)
            if period == 'all_time':
                self.assertIsNone(start)
                continue
            local = datetime.fromtimestamp(start,TZ)
            self.assertLessEqual(start,self.now)
            self.assertGreater(end,self.now)
            self.assertEqual(local.minute,0)
            if period == 'hour': self.assertEqual(local.hour,12)
            if period == 'week': self.assertEqual(local.weekday(),0)
            if period == 'month': self.assertEqual(local.day,1)
            if period == 'year': self.assertEqual((local.month,local.day),(1,1))
        with self.assertRaises(ValueError): window('injection', self.now)

    def test_dst_days_have_23_or_25_hours_and_repeated_hours_are_separate(self):
        for date, hours in [((2026,3,29),23),((2026,10,25),25)]:
            now = datetime(*date,12,tzinfo=TZ).timestamp()
            start,end = window('day',now)
            self.assertEqual(end-start,hours*3600)
        first = datetime(2026,10,25,3,30,tzinfo=TZ,fold=0).timestamp()
        second = datetime(2026,10,25,3,30,tzinfo=TZ,fold=1).timestamp()
        self.assertEqual(window('hour',second)[0]-window('hour',first)[0],3600)
        self.now=first
        self.mission()
        self.now=second
        self.mission()
        self.assertEqual(self.stats.report('hour')['sources']['DEMO']['summary']['started'],1)
        self.assertEqual(self.stats.report('day')['sources']['DEMO']['summary']['started'],2)

    def test_all_six_windows_include_only_start_buckets(self):
        dates = [(2024,1,1,10,0),(2026,1,2,10,0),(2026,11,1,10,0),
                 (2026,11,10,10,0),(2026,11,13,11,0),(2026,11,13,12,0)]
        for date in dates:
            self.now=datetime(*date,tzinfo=TZ).timestamp()
            self.mission()
        self.now=datetime(2026,11,13,12,30,tzinfo=TZ).timestamp()
        for period, count in [('hour',1),('day',2),('week',3),('month',4),('year',5),('all_time',6)]:
            with self.subTest(period=period):
                self.assertEqual(self.report(period)['started'],count)

    def test_hour_boundary_is_start_inclusive_end_exclusive(self):
        start,end=window('hour',self.now)
        self.now=start-0.001
        self.mission()
        self.now=start
        self.mission()
        self.now=end
        self.mission()
        self.assertEqual(self.report('hour')['started'],1)
        self.assertEqual(self.report('day')['started'],3)

    def test_start_attribution_across_day_month_and_year(self):
        self.now=datetime(2026,12,31,23,59,tzinfo=TZ).timestamp()
        identifier=self.mission(result=None)
        self.now=datetime(2027,1,1,0,1,tzinfo=TZ).timestamp()
        self.stats.finish(identifier,'COMPLETED')
        for period in ('hour','day','month','year'):
            self.assertEqual(self.report(period)['completed'],1)
            self.assertEqual(self.report(period)['started'],0)
        self.assertEqual(self.report('all_time')['completed'],1)

    def test_totals_per_operator_and_inactive_historical_owner(self):
        self.mission(owner='operator1')
        self.mission('STOPPED',owner='operator2')
        self.store.change_user('operator2',active=False)
        data=self.stats.report('all_time',by_operator=True)['sources']['DEMO']
        self.assertEqual(data['summary']['started'],2)
        self.assertEqual({row['username'] for row in data['by_operator']},{'operator1','operator2'})
        own=self.report(operator_id=self.actors['operator1']['id'])
        self.assertEqual((own['started'],own['completed']),(1,1))
        self.assertEqual(self.report(operator_id=self.actors['operator2']['id'])['cancelled'],1)

    def test_outcomes_and_unknown_results_do_not_become_success(self):
        for outcome in ('COMPLETED','STOPPED','RESET','HOLD','FAULT','E_STOP','INTERRUPTED'):
            self.mission(outcome)
        self.mission(None)
        result=self.report()
        self.assertEqual([result[k] for k in ('started','completed','cancelled','failed','interrupted','active')],[8,1,3,2,1,1])
        self.assertEqual(result['success_rate_pct'],round(100/7,2))
        self.assertIsNone(metrics()['success_rate_pct'])

    def test_finish_is_idempotent_including_concurrent_calls(self):
        identifier=self.mission(result=None)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes=list(pool.map(lambda _:self.stats.finish(identifier,'COMPLETED'),range(4)))
        self.assertEqual(outcomes.count(True),1)
        self.assertEqual(self.report()['completed'],1)
        with self.assertRaises(RuntimeError): self.stats.finish(identifier,'FAULT')

    def test_demo_and_real_are_separate_and_real_unavailable_by_default(self):
        self.mission()
        data=self.stats.report('all_time')
        self.assertFalse(data['sources']['REAL']['available'])
        self.assertIsNone(data['sources']['REAL']['summary'])
        with self.assertRaises(RuntimeError): self.mission(domain='REAL')
        # A fake future trusted source, not a vehicle command or current production adapter.
        future=MissionStatistics(self.store,real_connected=True)
        identifier=future.start(self.actors['operator2'],2,domain='REAL')
        future.finish(identifier,'COMPLETED')
        data=future.report('all_time',by_operator=True)['sources']
        self.assertEqual(data['DEMO']['summary']['completed'],1)
        self.assertEqual(data['REAL']['summary']['completed'],1)
        self.assertEqual(data['REAL']['by_operator'][0]['username'],'operator2')
        self.assertFalse(self.stats.report('all_time')['sources']['REAL']['available'])

    def test_all_time_survives_audit_and_detail_retention(self):
        self.mission()
        self.now+=500*86400
        self.store.audit_rows=1
        self.store.audit(None,'hmi.test','OK')
        self.mission('STOPPED')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM mission_runs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM mission_counts WHERE hour!=-1').fetchone()[0],1)
        result=self.report()
        self.assertEqual((result['started'],result['completed'],result['cancelled']),(2,1,1))

    def test_persistence_and_restart_recovery_are_idempotent(self):
        self.mission(result=None)
        reopened=MissionStatistics(AuthStore(self.env.tmp.name,clock=lambda:self.now))
        self.assertEqual(reopened.recover_demo(),1)
        self.assertEqual(reopened.recover_demo(),0)
        result=reopened.report('all_time')['sources']['DEMO']['summary']
        self.assertEqual((result['active'],result['interrupted'],result['completed']),(0,1,0))
        self.assertEqual(result['success_rate_pct'],0.0)

    def test_auth_only_database_migration_preserves_users_hashes_and_sessions(self):
        with tempfile.TemporaryDirectory(prefix='ugv-stat-migration-') as directory:
            auth=AuthStore(directory,clock=lambda:self.now)
            auth.add_user('firstadmin',PASSWORD,'admin',first=True)
            anonymous=auth.anonymous_session()
            token,status=auth.login('firstadmin',PASSWORD,'127.0.0.1',anonymous)
            self.assertEqual(status,200)
            with auth.transaction() as db:
                before=[tuple(row) for row in db.execute('SELECT * FROM users')]
            migrated=MissionStatistics(auth)
            MissionStatistics(auth)  # schema is idempotent
            self.assertEqual(auth.session(token)['username'],'firstadmin')
            with auth.transaction() as db:
                self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM users')])
            self.assertEqual(migrated.report('all_time')['sources']['DEMO']['summary']['started'],0)

    def test_invalid_domain_route_unknown_owner_or_outcome_fail_closed(self):
        for domain in ('REAL','demo',"DEMO' OR 1=1"):
            with self.assertRaises(RuntimeError): self.stats.start(self.actors['operator1'],1,domain=domain)
        for actor,route in [(None,1),({'id':999999},1),(self.actors['operator1'],3)]:
            with self.assertRaises(RuntimeError): self.stats.start_demo(actor,route)
        with self.assertRaises(ValueError): self.stats.finish('arbitrary','INVALID')
        self.assertEqual(self.report()['started'],0)


class StatisticsAPITest(unittest.TestCase):
    def setUp(self):
        self.now=1700000000.0
        self.mono=0.0
        self.env=TestEnvironment(clock=lambda:self.now)
        self.addCleanup(self.env.close)
        for name in ('operator1','operator2'): self.env.store.add_user(name,PASSWORD,'operator')
        self.sim=Simulator(lambda:self.mono)
        self.app=self.env.app(self.sim)
        self.stats=self.app.extensions['mission_statistics']
        self.client=self.app.test_client()
        self.sequence=0
        self.csrf=self.login(self.client,'operator1')

    def login(self,client,name):
        page=client.get('/login')
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
        self.assertEqual(client.post('/login',data=dict(username=name,password=PASSWORD,csrf_token=csrf)).status_code,303)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)

    def command(self,action,client=None,csrf=None,**kwargs):
        self.sequence+=1
        body=dict(action=action,request_id=str(self.sequence),revision=self.sim.snapshot()['revision'],confirmed=True)
        body.update(kwargs)
        return (client or self.client).post('/api/command',json=body,headers={'X-CSRF-Token':csrf or self.csrf})

    def totals(self):
        return self.client.get('/api/statistics?period=all_time').json['sources']['DEMO']['summary']

    def test_new_endpoints_are_authenticated_and_admin_is_forbidden(self):
        anonymous=self.app.test_client()
        for path in ('/api/statistics','/api/admin/statistics'):
            response=anonymous.get(path)
            self.assertEqual(response.status_code,401)
            self.assertTrue(response.is_json)
        self.assertEqual(anonymous.get('/statistics').status_code,303)
        self.assertEqual(self.client.get('/api/admin/statistics').status_code,403)
        self.assertEqual(self.client.get('/statistics').status_code,200)

    def test_operator_cannot_choose_another_operator_or_global_scope(self):
        owner=next(row for row in self.env.store.users() if row['username']=='operator2')
        for query in ('operator_id='+str(owner['id']),'scope=all'):
            self.assertEqual(self.client.get('/api/statistics?'+query).status_code,403)
        self.command('start')
        data=self.client.get('/api/statistics').json
        self.assertEqual(data['scope'],'self')
        self.assertEqual(data['sources']['DEMO']['by_operator'],[])
        self.assertNotIn('operator2',json.dumps(data))

    def test_admin_global_and_operator_filter(self):
        self.command('start')
        self.command('stop')
        other=self.app.test_client()
        other_csrf=self.login(other,'operator2')
        self.command('reset',other,other_csrf)
        self.command('start',other,other_csrf)
        self.mono+=20
        self.client.get('/api/statistics')
        admin=self.app.test_client()
        self.login(admin,'testadmin')
        data=admin.get('/api/admin/statistics?period=all_time').json
        self.assertEqual(data['sources']['DEMO']['summary']['started'],2)
        self.assertEqual({row['username'] for row in data['sources']['DEMO']['by_operator']},{'operator1','operator2'})
        self.assertEqual(self.totals()['started'],1)
        owner=next(row for row in self.env.store.users() if row['username']=='operator2')
        data=admin.get('/api/admin/statistics?period=all_time&operator_id='+str(owner['id'])).json
        self.assertEqual(data['sources']['DEMO']['summary']['completed'],1)
        self.assertEqual(len(data['sources']['DEMO']['by_operator']),1)

    def test_pause_resume_replay_and_polling_count_one_mission(self):
        response=self.command('start')
        self.assertEqual(response.status_code,200)
        self.mono+=4
        self.command('pause')
        self.mono+=100
        self.command('resume')
        for _ in range(3): self.client.get('/api/status')
        self.mono+=16
        self.assertEqual(self.totals()['completed'],1)
        self.assertEqual(self.totals()['started'],1)
        self.assertEqual(self.totals()['active'],0)

    def test_double_start_request_and_concurrent_statistics_do_not_duplicate(self):
        body=dict(action='start',request_id='same',revision=0,confirmed=True)
        for _ in range(2):
            self.assertEqual(self.client.post('/api/command',json=body,headers={'X-CSRF-Token':self.csrf}).status_code,200)
        self.mono+=20
        # Sim lock serializes all observers, SQLite finish is separately idempotent.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.sim.snapshot(),range(4)))
        self.assertEqual((self.totals()['started'],self.totals()['completed']),(1,1))

    def test_reset_stop_before_start_and_rejected_start_do_not_count(self):
        self.command('reset')
        self.command('stop')
        self.assertEqual(self.command('start').status_code,409)
        self.assertEqual(self.totals()['started'],0)

    def test_cancellation_and_protective_outcomes_count_once(self):
        for action,key in [('stop','cancelled'),('hold','cancelled'),('reset','cancelled'),('fault','failed'),('estop','failed')]:
            self.command('reset')
            self.command('start')
            before=self.totals()[key]
            self.command(action)
            self.command('stop')
            self.assertEqual(self.totals()[key],before+1)
        self.assertEqual(self.totals()['started'],5)
        self.assertEqual(self.totals()['active'],0)

    def test_closing_actor_does_not_steal_start_attribution(self):
        self.command('start',operator_id=999999,domain='REAL')
        other=self.app.test_client()
        csrf=self.login(other,'operator2')
        self.command('stop',other,csrf)
        self.assertEqual(self.totals()['cancelled'],1)
        data=other.get('/api/statistics?period=all_time').json
        self.assertEqual(data['sources']['DEMO']['summary']['started'],0)
        self.assertFalse(data['sources']['REAL']['available'])

    def test_invalid_period_id_and_unknown_admin_operator(self):
        for query in ('period=last_hundred_years','period=day%27','operator_id=-1','operator_id=abc'):
            self.assertEqual(self.client.get('/api/statistics?'+query).status_code,400)
        admin=self.app.test_client()
        self.login(admin,'testadmin')
        self.assertEqual(admin.get('/api/admin/statistics?operator_id=999999').status_code,404)

    def test_no_csrf_or_revoked_session_cannot_start_or_read_statistics(self):
        self.assertEqual(self.client.post('/api/command',json=dict(action='start')).status_code,403)
        self.assertEqual(self.totals()['started'],0)
        self.env.store.change_user('operator1',revoke=True)
        self.assertEqual(self.client.get('/api/statistics').status_code,401)

    def test_failed_start_accounting_prevents_simulator_movement(self):
        with patch.object(self.stats,'start_demo',side_effect=RuntimeError('storage unavailable')),self.assertLogs(self.app.logger,level='ERROR'):
            self.assertEqual(self.command('start').status_code,503)
        self.assertEqual(self.sim.snapshot()['state'],'READY')
        self.assertEqual(self.totals()['started'],0)

    def test_failed_finish_is_retried_without_double_counting(self):
        self.command('start')
        self.mono+=20
        with patch.object(self.stats,'finish',side_effect=RuntimeError('storage unavailable')),self.assertLogs(self.app.logger,level='ERROR'):
            self.assertEqual(self.client.get('/api/statistics').status_code,503)
        self.assertEqual(self.totals()['completed'],1)
        self.assertEqual(self.totals()['completed'],1)

    def test_clock_fault_is_failure_and_page_reload_is_not_process_restart(self):
        self.command('start')
        self.env.app(self.sim)  # another authenticated client factory must not recover active missions
        self.assertEqual(self.totals()['active'],1)
        self.mono=-1
        self.assertEqual(self.totals()['failed'],1)
        self.assertEqual(self.totals()['interrupted'],0)


    def test_second_engine_cannot_interrupt_a_live_mission(self):
        from web_hmi.runtime import start_hmi, stop_telemetry
        other=self.env.app(Simulator(lambda:self.mono))
        start_hmi(self.app)
        try:
            self.command('start')
            with self.assertRaises(BlockingIOError):
                start_hmi(other)
            self.assertEqual(self.totals()['active'],1)
            self.assertEqual(self.totals()['interrupted'],0)
        finally:
            stop_telemetry(self.app)
        self.assertEqual(self.totals()['interrupted'],1)

    def test_new_engine_recovers_only_after_exclusive_lifecycle_lock(self):
        from web_hmi.runtime import start_hmi, stop_telemetry
        # Simulate lost RAM without running a second live engine.
        self.command('start')
        replacement=self.env.app(Simulator(lambda:self.mono))
        start_hmi(replacement)
        try:
            result=replacement.extensions['mission_statistics'].report('all_time')['sources']['DEMO']['summary']
            self.assertEqual((result['active'],result['interrupted']),(0,1))
        finally:
            stop_telemetry(replacement)


    def test_graceful_shutdown_observes_completed_demo_before_recovering(self):
        from web_hmi.runtime import start_hmi, stop_telemetry
        start_hmi(self.app)
        self.command('start')
        self.mono+=20
        stop_telemetry(self.app)
        result=self.stats.report('all_time')['sources']['DEMO']['summary']
        self.assertEqual((result['completed'],result['interrupted'],result['active']),(1,0,0))


    def test_retry_keeps_the_first_terminal_timestamp_across_hour_boundary(self):
        from web_hmi.mission_statistics import window
        boundary=window('hour',self.now)[1]
        self.now=boundary-1
        self.client=self.app.test_client()
        self.csrf=self.login(self.client,'operator1')
        self.command('start')
        self.mono+=20
        self.now=boundary+1
        with patch.object(self.stats,'finish',side_effect=RuntimeError('storage unavailable')), self.assertLogs(self.app.logger,level='ERROR'):
            self.assertEqual(self.client.get('/api/statistics').status_code,503)
        self.now=boundary+3601
        self.sim.snapshot()  # retry after storage recovers; do not re-date the END
        with self.env.store.transaction() as db:
            self.assertEqual(db.execute('SELECT ended FROM mission_runs').fetchone()[0],boundary+1)
            self.assertEqual(db.execute('SELECT completed FROM mission_counts WHERE hour=?',(int(boundary),)).fetchone()[0],1)
        current=self.stats.report('hour')['sources']['DEMO']['summary']
        self.assertEqual((current['started'],current['completed'],current['active']),(0,0,0))
        self.assertEqual(self.stats.report('all_time')['sources']['DEMO']['summary']['completed'],1)
