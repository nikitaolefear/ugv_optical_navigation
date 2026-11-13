"""Synthetic, private temp databases only: no access to the deployed auth DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import os
import math
import tempfile
import unittest

from web_hmi.auth import AuthStore
from web_hmi.mission_statistics import MissionStatistics, TZ, COUNTERS, OUTCOMES, PERIODS
from web_hmi.test_support import PASSWORD


def stamp(y,m,d,h=0,minute=0,*,fold=0):
    return datetime(y,m,d,h,minute,tzinfo=TZ,fold=fold).timestamp()


# Reproduce v1 tables/START attribution, including runs whose ledger was pruned.
# This fixture does not load or mutate the previous candidate or any live state.
def seed_v1(store, actors, records):
    with store.transaction() as db:
        db.execute('CREATE TABLE mission_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT INTO mission_meta VALUES('version','1')")
        db.execute('CREATE TABLE mission_runs(id TEXT PRIMARY KEY,domain TEXT NOT NULL,operator_id INTEGER NOT NULL,'
                   'operator_name TEXT NOT NULL,operator_role TEXT NOT NULL,route INTEGER NOT NULL,started REAL NOT NULL,'
                   'ended REAL,outcome TEXT NOT NULL)')
        db.execute('CREATE TABLE mission_counts(domain TEXT NOT NULL,operator_id INTEGER NOT NULL,hour INTEGER NOT NULL,'
                   'started INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,cancelled INTEGER NOT NULL DEFAULT 0,'
                   'failed INTEGER NOT NULL DEFAULT 0,interrupted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(domain,operator_id,hour))')
        for domain in {r.get('domain','DEMO') for r in records}:
            db.execute('INSERT INTO mission_meta VALUES(?,?)',('coverage_'+domain,str(min(r['start'] for r in records))))
        for index,r in enumerate(records):
            actor=actors[r.get('owner','firstadmin')]
            domain,outcome=r.get('domain','DEMO'),r.get('outcome','COMPLETED')
            for hour in (-1,int(r['start']//3600)*3600):
                db.execute('INSERT INTO mission_counts(domain,operator_id,hour,started) VALUES(?,?,?,1) '
                           'ON CONFLICT(domain,operator_id,hour) DO UPDATE SET started=started+1',(domain,actor['id'],hour))
                if outcome!='ACTIVE':
                    column=OUTCOMES[outcome]
                    db.execute('UPDATE mission_counts SET '+column+'='+column+'+1 WHERE domain=? AND operator_id=? AND hour=?',
                               (domain,actor['id'],hour))
            if r.get('retain',True):
                db.execute('INSERT INTO mission_runs VALUES(?,?,?,?,?,?,?,?,?)',
                           ('legacy-'+str(index),domain,actor['id'],actor['username'],actor['role'],1,r['start'],r.get('end'),outcome))


class BoundaryV2Test(unittest.TestCase):
    def test_all_terminal_outcomes_cross_hour_day_week_month_year_and_dst(self):
        cases=[('hour',stamp(2026,11,13,12,59),stamp(2026,11,13,13,1)),
               ('day',stamp(2026,11,13,23,59),stamp(2026,11,14,0,1)),
               ('week',stamp(2026,11,15,23,59),stamp(2026,11,16,0,1)),
               ('month',stamp(2026,11,30,23,59),stamp(2026,12,1,0,1)),
               ('year',stamp(2026,12,31,23,59),stamp(2027,1,1,0,1)),
               ('hour',stamp(2026,3,29,2,59),stamp(2026,3,29,4,1)),
               ('hour',stamp(2026,10,25,3,59,fold=0),stamp(2026,10,25,3,1,fold=1))]
        for period,start,end in cases:
            with self.subTest(period=period,start=start,end=end),tempfile.TemporaryDirectory(prefix='ugv-v2-boundary-') as path:
                clock=[start]
                store=AuthStore(path,clock=lambda:clock[0])
                store.add_user('firstadmin',PASSWORD,'admin',first=True)
                actor=store.users()[0]
                stats=MissionStatistics(store)
                endings=('COMPLETED','STOPPED','FAULT','INTERRUPTED')
                ids=[stats.start_demo(actor,1) for _ in endings]
                clock[0]=end
                current=stats.report(period)['sources']['DEMO']['summary']
                self.assertEqual((current['started'],current['active']),(0,4))
                for run_id,outcome in zip(ids,endings): stats.finish(run_id,outcome)
                current=stats.report(period)['sources']['DEMO']['summary']
                self.assertEqual([current[k] for k in COUNTERS],[0,1,1,1,1])
                self.assertEqual(current['active'],0)
                self.assertEqual(current['success_rate_pct'],25.0)
                for run_id,outcome in zip(ids,endings): self.assertFalse(stats.finish(run_id,outcome))
                all_time=stats.report('all_time')['sources']['DEMO']['summary']
                self.assertEqual([all_time[k] for k in COUNTERS],[4,1,1,1,1])
                self.assertEqual(all_time['active'],0)

    def test_active_is_current_for_every_period_and_operator(self):
        with tempfile.TemporaryDirectory(prefix='ugv-v2-active-') as path:
            clock=[stamp(2024,12,31,23,59)]
            store=AuthStore(path,clock=lambda:clock[0])
            store.add_user('firstadmin',PASSWORD,'admin',first=True)
            store.add_user('operator2',PASSWORD,'operator')
            stats=MissionStatistics(store)
            actor=store.users()[0]
            stats.start_demo(actor,2)
            clock[0]=stamp(2026,11,13,12)
            with store.transaction() as db:
                stats._prune(db)
            for period in PERIODS:
                report=stats.report(period,by_operator=True)['sources']['DEMO']
                self.assertEqual(report['summary']['active'],1)
                self.assertEqual(report['by_operator'][0]['active'],1)
                if period!='all_time': self.assertEqual(report['summary']['started'],0)
                other=stats.report(period,operator_id=store.users()[1]['id'])['sources']['DEMO']['summary']
                self.assertEqual(other['active'],0)


class MigrationV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ugv-v2-migration-')
        self.addCleanup(self.tmp.cleanup)
        self.now=stamp(2027,1,1,0,10)
        self.store=AuthStore(self.tmp.name,clock=lambda:self.now)
        self.store.add_user('firstadmin',PASSWORD,'admin',first=True)
        self.store.add_user('operator2',PASSWORD,'operator')
        self.actors={row['username']:row for row in self.store.users()}

    def totals(self):
        with self.store.transaction() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM mission_counts WHERE hour=-1 ORDER BY domain,operator_id')]

    def version(self):
        with self.store.transaction() as db:
            return db.execute("SELECT value FROM mission_meta WHERE key='version'").fetchone()[0]

    def test_complete_ledger_rebuilds_result_hours_preserves_all_time_and_sessions(self):
        records=[dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,1)),
                 dict(start=stamp(2026,12,31,23,50),end=stamp(2027,1,1,0,2),outcome='FAULT',owner='operator2')]
        seed_v1(self.store,self.actors,records)
        old=self.totals()
        anonymous=self.store.anonymous_session()
        token,_=self.store.login('firstadmin',PASSWORD,'127.0.0.1',anonymous)
        with self.store.transaction() as db:
            users=[tuple(r) for r in db.execute('SELECT * FROM users')]
        stats=MissionStatistics(self.store)
        self.assertEqual(self.version(),'2')
        self.assertEqual(old,self.totals())
        report=stats.report('year',by_operator=True)['sources']['DEMO']
        self.assertEqual([report['summary'][k] for k in COUNTERS],[0,1,0,1,0])
        self.assertTrue(report['summary']['results_complete'])
        self.assertEqual(report['summary']['success_rate_pct'],50.0)
        self.assertEqual(self.store.session(token)['username'],'firstadmin')
        with self.store.transaction() as db: self.assertEqual(users,[tuple(r) for r in db.execute('SELECT * FROM users')])
        MissionStatistics(self.store)
        self.assertEqual(old,self.totals())
        self.assertEqual(stats.report('year')['sources']['DEMO']['summary']['completed'],1)

    def test_pruned_history_is_unallocated_not_assigned_to_start_hour(self):
        seed_v1(self.store,self.actors,[
            dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,1)),
            dict(start=stamp(2027,1,1,0,2),outcome='STOPPED',retain=False),
            dict(start=stamp(2027,1,1,0,3),outcome='COMPLETED',retain=False)])
        old=self.totals()
        stats=MissionStatistics(self.store)
        report=stats.report('day')['sources']['DEMO']
        self.assertEqual(report['summary']['started'],2)
        self.assertEqual(report['summary']['completed'],1)  # only the retained END evidence
        self.assertEqual(report['summary']['cancelled'],0)
        self.assertFalse(report['summary']['results_complete'])
        self.assertIsNone(report['summary']['success_rate_pct'])
        self.assertEqual(report['history_without_end_time'],dict(completed=1,cancelled=1,failed=0,interrupted=0))
        all_time=stats.report('all_time')['sources']['DEMO']['summary']
        self.assertTrue(all_time['results_complete'])
        self.assertEqual((all_time['started'],all_time['completed'],all_time['cancelled']),(3,2,1))
        self.assertEqual(all_time['success_rate_pct'],round(200/3,2))
        self.assertEqual(old,self.totals())
        MissionStatistics(self.store)
        self.assertEqual(old,self.totals())
        self.now=stamp(2027,1,1,1,1)
        current=stats.report('hour')['sources']['DEMO']['summary']
        self.assertTrue(current['results_complete'])
        self.assertEqual(current['completed'],0)
        self.assertIsNone(current['success_rate_pct'])

    def test_partial_history_is_scoped_to_owner(self):
        seed_v1(self.store,self.actors,[
            dict(start=stamp(2027,1,1,0,1),retain=False),
            dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,2),owner='operator2')])
        stats=MissionStatistics(self.store)
        own=stats.report('day',operator_id=self.actors['operator2']['id'])['sources']['DEMO']
        self.assertTrue(own['summary']['results_complete'])
        self.assertEqual(sum(own['history_without_end_time'].values()),0)
        self.assertEqual(own['summary']['completed'],1)
        aggregate=stats.report('day',by_operator=True)['sources']['DEMO']
        self.assertFalse(aggregate['summary']['results_complete'])
        owners={r['username']:r for r in aggregate['by_operator']}
        self.assertTrue(owners['operator2']['results_complete'])
        self.assertFalse(owners['firstadmin']['results_complete'])

    def test_missing_end_is_not_filled_from_start_and_active_remains_current(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2026,12,31,23,59),end=None),
                                          dict(start=stamp(2026,1,1),outcome='ACTIVE')])
        stats=MissionStatistics(self.store)
        for period in PERIODS:
            data=stats.report(period)['sources']['DEMO']
            self.assertEqual(data['summary']['active'],1)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT ended FROM mission_runs WHERE id='legacy-0'").fetchone()[0])
        data=stats.report('day')['sources']['DEMO']
        self.assertEqual(data['summary']['completed'],0)
        self.assertFalse(data['summary']['results_complete'])
        stats.finish('legacy-1','INTERRUPTED')
        data=stats.report('day')['sources']['DEMO']['summary']
        self.assertEqual((data['started'],data['interrupted'],data['active']),(0,1,0))

    def test_demo_real_migration_and_new_finish_never_double_count_all_time(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,1)),
            dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,1),domain='REAL'),
            dict(start=stamp(2026,12,31,23,59),outcome='ACTIVE')])
        stats=MissionStatistics(self.store,real_connected=True)
        self.assertEqual(stats.report('day')['sources']['REAL']['summary']['completed'],1)
        self.assertEqual(stats.report('day')['sources']['DEMO']['summary']['completed'],1)
        stats.finish('legacy-2','COMPLETED')
        self.assertFalse(stats.finish('legacy-2','COMPLETED'))
        for _ in range(3): MissionStatistics(self.store,real_connected=True)
        demo=stats.report('all_time')['sources']['DEMO']['summary']
        real=stats.report('all_time')['sources']['REAL']['summary']
        self.assertEqual((demo['started'],demo['completed']),(2,2))
        self.assertEqual((real['started'],real['completed']),(1,1))

    def test_inconsistent_history_rolls_back_schema_and_every_counter(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2027,1,1,0,1),retain=False),
            dict(start=stamp(2027,1,1,0,2),end=stamp(2027,1,1,0,3),owner='operator2')])
        with self.store.transaction() as db:
            db.execute('UPDATE mission_counts SET started=0 WHERE operator_id=? AND hour=-1',(self.actors['operator2']['id'],))
            before=[tuple(r) for r in db.execute('SELECT * FROM mission_counts ORDER BY domain,operator_id,hour')]
        with self.assertRaises(RuntimeError): MissionStatistics(self.store)
        self.assertEqual(self.version(),'1')
        with self.store.transaction() as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM mission_counts ORDER BY domain,operator_id,hour')])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='mission_history_gaps'").fetchone())

    def test_retained_ledger_exceeding_all_time_fails_closed(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2026,12,31,23,59),end=stamp(2027,1,1,0,1))])
        with self.store.transaction() as db:
            db.execute('UPDATE mission_counts SET completed=0,cancelled=1 WHERE hour=-1')
        old=self.totals()
        with self.assertRaises(RuntimeError): MissionStatistics(self.store)
        self.assertEqual(self.version(),'1')
        self.assertEqual(old,self.totals())


    def test_live_v1_engine_prevents_migration_without_changing_data(self):
        from web_hmi.auth import private_file
        seed_v1(self.store,self.actors,[dict(start=stamp(2027,1,1,0,1),retain=False)])
        old=self.totals()
        path=self.store.directory/'hmi-engine.lock'
        private_file(path)
        fd=os.open(path,os.O_RDWR)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError,'Stop the existing HMI'):
                MissionStatistics(self.store)
        finally:
            os.close(fd)
        self.assertEqual(self.version(),'1')
        self.assertEqual(old,self.totals())

    def test_concurrent_initializers_migrate_once(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2027,1,1,0,1),retain=False)])
        old=self.totals()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:MissionStatistics(self.store),range(2)))
        self.assertEqual(self.version(),'2')
        self.assertEqual(old,self.totals())
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT sum(completed) FROM mission_history_gaps').fetchone()[0],1)


    def test_unknown_or_missing_nonempty_schema_version_is_not_guessed(self):
        seed_v1(self.store,self.actors,[dict(start=stamp(2027,1,1,0,1),retain=False)])
        old=self.totals()
        with self.store.transaction() as db:
            db.execute("UPDATE mission_meta SET value='99' WHERE key='version'")
        with self.assertRaises(RuntimeError): MissionStatistics(self.store)
        self.assertEqual(self.version(),'99')
        with self.store.transaction() as db:
            db.execute("DELETE FROM mission_meta WHERE key='version'")
        with self.assertRaises(RuntimeError): MissionStatistics(self.store)
        self.assertEqual(self.totals(),old)
