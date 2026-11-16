"""Isolated access-control integration, migration and concurrency regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from .auth import AuthStore, AccessDenied, AccountError
from .access_control import PERMISSIONS
from .access_migration import migrate_access
from . import admin_cli
from .test_support import TestEnvironment, PASSWORD


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        self.store = self.env.store
        self.store.add_user('chief', PASSWORD, 'superadmin')
        self.store.add_user('worker', PASSWORD, 'operator')
        self.store.add_user('viewer', PASSWORD, 'observer')
        self.app = self.env.app()
        self.chief, self.chief_csrf = self.login('chief')
        self.admin, self.admin_csrf = self.login('testadmin')

    def login(self, username):
        client = self.app.test_client()
        page = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        response = client.post('/login', data=dict(username=username, password=PASSWORD, csrf_token=csrf))
        self.assertEqual(response.status_code, 303)
        page = client.get('/access' if username in ('chief','testadmin') else '/')
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
        return client, csrf

    def id(self, username):
        return next(u['id'] for u in self.store.users() if u['username']==username)

    def post(self, url, body, *, chief=False, client=None, csrf=None):
        return (client or (self.chief if chief else self.admin)).post(url, json=body,
            headers={'X-CSRF-Token': csrf or (self.chief_csrf if chief else self.admin_csrf)})

    def user(self, name, action, *, chief=False, **fields):
        return self.post('/api/admin/users/'+str(self.id(name)), dict(action=action, **fields), chief=chief)

    def role(self, name, action, *, chief=False, **fields):
        return self.post('/api/admin/roles/'+name, dict(action=action, **fields), chief=chief)

    def test_superadmin_manages_admin_and_lower_roles(self):
        self.assertEqual(self.user('testadmin', 'block', chief=True).status_code, 200)
        self.assertEqual(self.admin.get('/api/status').status_code, 401)
        self.assertEqual(self.user('testadmin', 'unblock', chief=True).status_code, 200)
        self.assertEqual(self.user('testadmin', 'role', role='observer', chief=True).status_code, 200)
        self.assertEqual(self.user('worker', 'password', password='replacement-test-password', chief=True).status_code, 200)
        self.assertEqual(self.user('viewer', 'delete', chief=True).status_code, 200)
        self.assertNotIn('viewer', [u['username'] for u in self.store.users()])
        self.assertTrue(any(r['action']=='user.delete' and r['actor']=='chief' for r in self.store.logs()))

    def test_admin_create_modify_delete_only_lower_users(self):
        response = self.post('/api/admin/users', dict(username='newperson', password=PASSWORD, role='operator'))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.user('newperson', 'role', role='observer').status_code, 200)
        self.assertEqual(self.user('newperson', 'block').status_code, 200)
        self.assertEqual(self.user('newperson', 'unblock').status_code, 200)
        self.assertEqual(self.user('newperson', 'password', password='new-test-password').status_code, 200)
        self.assertEqual(self.user('newperson', 'delete').status_code, 200)
        for target in ('testadmin','chief'):
            for action, fields in [('block',{}), ('unblock',{}), ('delete',{}),
                                   ('password',{'password':'replacement-test-password'}), ('role',{'role':'observer'})]:
                self.assertEqual(self.user(target, action, **fields).status_code, 403)
        for role in ('admin','superadmin'):
            self.assertEqual(self.user('worker', 'role', role=role).status_code, 403)
            self.assertEqual(self.post('/api/admin/users', dict(username='otheruser', password=PASSWORD, role=role)).status_code, 403)

    def test_admin_cannot_modify_own_or_higher_role_or_raise_rank(self):
        for name in ('admin','superadmin'):
            self.assertEqual(self.role(name,'edit',rank=1,permissions=[]).status_code, 403)
            self.assertEqual(self.role(name,'delete').status_code, 403)
        for rank in (300,1000):
            self.assertEqual(self.post('/api/admin/roles', dict(name='custom',rank=rank,permissions=[])).status_code,403)
            self.assertEqual(self.role('observer','edit',rank=rank,permissions=[]).status_code,403)
        self.assertEqual(self.post('/api/admin/roles', dict(name='custom',rank=150,permissions=['status.view'])).status_code,201)
        self.assertEqual(self.role('custom','edit',rank=160,permissions=['audit.view']).status_code,200)
        self.assertEqual(self.role('custom','delete').status_code,200)
        self.assertEqual(self.role('operator','delete').status_code,400)
        self.assertIn('призначена',self.role('observer','delete').json['error'])

    def test_no_grant_of_unowned_or_unknown_permissions(self):
        # Reduce admin capabilities as superadmin, then reauthenticate that admin.
        limited = ['status.view','users.manage','roles.manage']
        self.assertEqual(self.role('admin','edit',chief=True,rank=300,permissions=limited).status_code,200)
        self.assertEqual(self.admin.get('/api/status').status_code,401)
        self.admin,self.admin_csrf=self.login('testadmin')
        for permissions in (['demo.control'],['NOT_SUPPORTED'],[1],['status.view','status.view']):
            self.assertIn(self.post('/api/admin/roles',dict(name='custom',rank=50,permissions=permissions)).status_code,(400,403))
        self.assertEqual(self.user('worker','role',role='operator').status_code,403)
        self.assertEqual(self.post('/api/admin/users',dict(username='newperson',password=PASSWORD,role='operator')).status_code,403)
        self.assertEqual(self.role('observer','edit',rank=100,permissions=['demo.control']).status_code,403)
        self.assertEqual(self.post('/api/admin/roles',dict(name='custom',rank=50,permissions=['status.view'])).status_code,201)

    def test_observer_server_denies_commands_and_management(self):
        viewer,csrf=self.login('viewer')
        for url in ('/api/status','/api/telemetry','/api/audit','/api/statistics'):
            self.assertEqual(viewer.get(url).status_code,200)
        for url,body in [('/api/command',{'action':'reset','revision':0,'request_id':'test'}),
                         ('/api/admin/users',dict(username='baduser',password=PASSWORD,role='superadmin')),
                         ('/api/admin/roles',dict(name='evil',rank=999,permissions=[]))]:
            self.assertEqual(self.post(url,body,client=viewer,csrf=csrf).status_code,403)
        self.assertEqual(viewer.get('/access').status_code,403)
        for url in ('/api/admin/audit','/api/admin/statistics','/api/admin/users','/api/admin/roles'):
            self.assertEqual(viewer.get(url).status_code,403)
        self.assertIn('Режим перегляду', viewer.get('/').text)
        self.assertEqual(self.app.extensions['simulator'].snapshot()['revision'],0)

    def test_each_permission_is_checked_by_endpoint_not_rank(self):
        permissions=['status.view','audit.view','statistics.view','demo.control']
        self.assertEqual(self.role('observer','edit',chief=True,rank=100,permissions=permissions).status_code,200)
        viewer,csrf=self.login('viewer')
        self.assertEqual(viewer.get('/api/telemetry').status_code,403)
        self.assertEqual(self.post('/api/command',{'action':'reset','revision':0,'request_id':'r1'},client=viewer,csrf=csrf).status_code,200)
        self.assertEqual(self.role('observer','edit',chief=True,rank=100,permissions=[]).status_code,200)
        self.assertEqual(viewer.get('/api/status').status_code,401)
        # Login can succeed even with no permissions; every data route must still deny.
        anonymous=self.app.test_client()
        page=anonymous.get('/login'); csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text)[1]
        self.assertEqual(anonymous.post('/login',data=dict(username='viewer',password=PASSWORD,csrf_token=csrf)).status_code,303)
        for url in ('/','/api/status','/api/telemetry','/audit','/api/audit','/statistics','/api/statistics'):
            self.assertEqual(anonymous.get(url).status_code,403)

    def test_last_superadmin_protected_for_http_and_cli(self):
        for action,fields in [('block',{}),('delete',{}),('role',{'role':'admin'})]:
            self.assertEqual(self.user('chief',action,chief=True,**fields).status_code,403)
        with self.assertRaises(AccessDenied): self.store.change_user('chief',active=False)
        with self.assertRaises(AccessDenied): self.store.change_user('chief',role='admin')
        self.assertEqual(self.role('superadmin','edit',chief=True,rank=999,permissions=[]).status_code,403)
        self.store.add_user('otherchief',PASSWORD,'superadmin')
        self.assertEqual(self.user('otherchief','delete',chief=True).status_code,200)
        self.assertEqual(self.user('chief','delete',chief=True).status_code,403)

    def test_password_role_activity_and_role_permissions_revoke_sessions(self):
        for action,fields in [('password',{'password':PASSWORD}),('role',{'role':'operator'}),('block',{})]:
            worker,csrf=self.login('worker')
            self.assertEqual(self.user('worker',action,chief=True,**fields).status_code,200)
            self.assertEqual(worker.get('/api/status').status_code,401)
            self.assertEqual(self.user('worker','unblock',chief=True).status_code,200)
        worker,csrf=self.login('worker')
        self.assertEqual(self.role('operator','edit',chief=True,rank=201,permissions=['status.view']).status_code,200)
        self.assertEqual(worker.get('/api/status').status_code,401)

    def test_csrf_and_origin_required_on_every_new_mutation(self):
        operations=[('/api/admin/users',dict(username='created',password=PASSWORD,role='observer')),
                    ('/api/admin/users/'+str(self.id('worker')),dict(action='block')),
                    ('/api/admin/roles',dict(name='created',rank=50,permissions=[])),
                    ('/api/admin/roles/observer',dict(action='edit',rank=101,permissions=[]))]
        for url,body in operations:
            self.assertEqual(self.admin.post(url,json=body).status_code,403)
            self.assertEqual(self.admin.post(url,json=body,headers={'X-CSRF-Token':self.admin_csrf,'Origin':'https://other.example'}).status_code,403)
        self.assertTrue(next(u for u in self.store.users() if u['username']=='worker')['active'])

    def test_no_secrets_in_api_or_audit_and_all_changes_have_actor_target(self):
        secret='only-test-NEVER-PRINT-secret'
        self.assertEqual(self.user('worker','password',password=secret).status_code,200)
        self.assertEqual(self.user('chief','block').status_code,403)
        self.assertEqual(self.role('observer','edit',rank=101,permissions=['status.view']).status_code,200)
        content=''.join(self.admin.get(path).text for path in ('/api/session','/api/admin/users','/api/admin/roles','/api/admin/audit'))
        audit=self.store.logs()
        for row in audit:
            if row['action'] in ('user.password','user.block','role.edit'):
                self.assertEqual(row['actor'],'testadmin')
                self.assertRegex(row['detail'],r'^(user_id:|role:)')
        raw=self.admin.get_cookie('ugv_session').value
        for value in (secret,raw,self.admin_csrf,self.store.key.hex(),'scrypt:', 'password_hash'):
            self.assertNotIn(value,content+json.dumps(audit))
        self.assertEqual(set(self.admin.get('/api/session').json),{'user'})
        self.assertEqual(self.admin.get('/').headers['Referrer-Policy'],'same-origin')

    def test_deleted_user_retains_mission_and_audit_identity(self):
        worker,csrf=self.login('worker')
        uid=self.id('worker')
        stats=self.app.extensions['mission_statistics']
        run=stats.start(dict(id=uid),1,domain='DEMO')
        stats.finish(run,'COMPLETED')
        self.assertEqual(self.user('worker','delete',chief=True).status_code,200)
        self.assertEqual(worker.get('/api/status').status_code,401)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT operator_name FROM mission_runs WHERE id=?',(run,)).fetchone()[0],'worker')
        report=stats.report('all_time',operator_id=uid,by_operator=True)
        self.assertEqual(report['sources']['DEMO']['summary']['completed'],1)
        self.assertTrue(any(r['actor']=='worker' for r in self.store.logs()))
        self.assertEqual(self.post('/api/admin/users',dict(username='worker',password=PASSWORD,role='operator'),chief=True).status_code,400)

    def test_concurrent_last_superadmin_demotion_preserves_one(self):
        self.store.add_user('otherchief',PASSWORD,'superadmin')
        other,csrf=self.login('otherchief')
        tokens=[self.chief.get_cookie('ugv_session').value,other.get_cookie('ugv_session').value]
        targets=[self.id('otherchief'),self.id('chief')]
        def demote(i):
            try:
                self.store.manage_user(tokens[i],'role',{'role':'admin'},targets[i])
                return 'OK'
            except AccessDenied: return 'DENIED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(demote,[0,1])),['OK','DENIED'])
        self.assertEqual(sum(u['role']=='superadmin' and u['active'] for u in self.store.users()),1)

    def test_concurrent_role_promotion_cannot_bypass_admin_target_check(self):
        token=self.admin.get_cookie('ugv_session').value
        uid=self.id('worker')
        started=threading.Event()
        def block():
            started.set()
            try:
                self.store.manage_user(token,'block',{},uid)
                return 'OK'
            except AccessDenied: return 'DENIED'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.transaction() as db:
                db.execute("UPDATE roles SET rank=500 WHERE name='operator'")
                future=pool.submit(block)
                self.assertTrue(started.wait(2))
            self.assertEqual(future.result(timeout=8),'DENIED')
        self.assertTrue(next(u for u in self.store.users() if u['id']==uid)['active'])

    def test_request_releases_access_lock_even_with_preserved_flask_context(self):
        with self.app.test_client() as client:
            client.set_cookie('ugv_session',self.admin.get_cookie('ugv_session').value)
            self.assertEqual(client.get('/api/status').status_code,200)
            self.assertFalse(getattr(self.store._local,'access_locked',False))

    def test_demo_intent_remains_pending_when_final_audit_fails(self):
        with patch.object(self.store,'finish_event',side_effect=sqlite3.OperationalError('fixture')), self.assertLogs(self.app.logger,level='ERROR'):
            response=self.post('/api/command',dict(action='reset',revision=0,request_id='pending-check'))
        self.assertEqual(response.status_code,503)
        self.assertTrue(any(r['action']=='demo.reset' and r['result']=='PENDING' for r in self.store.logs()))

    def test_revocation_waits_for_authorized_command_then_blocks_next_request(self):
        worker,csrf=self.login('worker')
        entered=threading.Event(); finish=threading.Event(); revoked=threading.Event()
        real_command=self.app.extensions['simulator'].command
        def delayed(body,actor):
            entered.set()
            if not finish.wait(5): raise RuntimeError('fixture timeout')
            return real_command(body,actor=actor)
        def command():
            return self.post('/api/command',dict(action='reset',revision=0,request_id='race'),client=worker,csrf=csrf).status_code
        def revoke():
            entered.wait(5)
            other_store=AuthStore(self.env.tmp.name)
            other_store.change_user('worker',role='observer')
            revoked.set()
        with patch.object(self.app.extensions['simulator'],'command',side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool:
            request=pool.submit(command)
            self.assertTrue(entered.wait(5))
            change=pool.submit(revoke)
            self.assertFalse(revoked.wait(.1))
            finish.set()
            self.assertEqual(request.result(timeout=8),200)
            change.result(timeout=8)
        self.assertEqual(worker.get('/api/status').status_code,401)

    def test_explicit_superadmin_grant_refuses_inactive_user_without_password_change(self):
        self.store.change_user('viewer',active=False)
        with self.store.transaction() as db:
            before=tuple(db.execute("SELECT role,active,password_hash FROM users WHERE username='viewer'").fetchone())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(admin_cli.main(['--state-dir',self.env.tmp.name,'grant-superadmin','viewer']),2)
        with self.store.transaction() as db:
            after=tuple(db.execute("SELECT role,active,password_hash FROM users WHERE username='viewer'").fetchone())
        self.assertEqual(after,before)

    def test_superadmin_can_edit_own_role_without_losing_management_or_top_rank(self):
        self.assertEqual(self.role('superadmin','edit',chief=True,rank=200,permissions=list(PERMISSIONS)).status_code,403)
        self.assertEqual(self.role('superadmin','edit',chief=True,rank=900,
                         permissions=['users.manage','roles.manage']).status_code,200)
        self.assertEqual(self.chief.get('/api/status').status_code,401)
        self.chief,self.chief_csrf=self.login('chief')
        self.assertEqual(self.chief.get('/api/status').status_code,403)
        self.assertEqual(self.chief.get('/access').status_code,200)
        self.assertEqual(self.post('/api/admin/roles',dict(name='too_high',rank=950,permissions=[]),chief=True).status_code,403)

    def test_unclassified_api_is_denied_even_for_superadmin(self):
        app=self.env.app()
        @app.get('/api/future-access')
        def future():return {'should_not':'expose'}
        client=app.test_client()
        client.set_cookie('ugv_session',self.chief.get_cookie('ugv_session').value)
        self.assertEqual(client.get('/api/future-access').status_code,403)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ugv-access-legacy-')
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        # Exact legacy users CHECK and related tables; no dependency on an external checkout.
        from .auth import password_hash
        key = self.path/'auth.key'; key.write_bytes(os.urandom(32)); key.chmod(0o600)
        db_path = self.path/'auth.sqlite3'; db_path.touch(mode=0o600)
        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('operator','admin')),
              active INTEGER NOT NULL CHECK(active IN (0,1)), created REAL NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id),
              csrf TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL);
            CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
              actor_id INTEGER, actor TEXT, role TEXT, action TEXT NOT NULL, result TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE INDEX audit_actor ON audit(actor_id,id);
            CREATE TABLE login_limits(key TEXT PRIMARY KEY, started REAL NOT NULL, attempts INTEGER NOT NULL);
        """)
        hashed=password_hash(PASSWORD)
        db.executemany('INSERT INTO users VALUES(?,?,?,?,?,?)',[
            (1,'mykyta',hashed,'admin',1,1700000000), (2,'oldworker',hashed,'operator',1,1700000000)])
        db.execute("INSERT INTO sessions VALUES('legacy-token-hash',1,'legacy-csrf',1700000000,1700000000)")
        db.execute("INSERT INTO audit(time,actor_id,actor,role,action,result,detail) VALUES(1700000000,2,'oldworker','operator','login','OK','')")
        db.execute("INSERT INTO login_limits VALUES('legacy-limit',1700000000,1)")
        db.commit(); db.close()
        # Use the existing statistics implementation to populate a real pre-migration fixture.
        # Only this disposable fixture bypasses constructor initialization to keep users legacy.
        store=AuthStore(self.path, read_only=True)
        store.read_only=False
        from .mission_statistics import MissionStatistics
        stats=MissionStatistics(store)
        run=stats.start(dict(id=2),1,domain='DEMO'); stats.finish(run,'COMPLETED')
        self.before=self.rows()
        self.key=(self.path/'auth.key').read_bytes()

    def rows(self):
        db=sqlite3.connect(self.path/'auth.sqlite3')
        try:
            return {r[0]: db.execute('SELECT * FROM '+r[0]).fetchall()
                    for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        finally: db.close()

    def test_explicit_migration_preserves_all_rows_hashes_keys_and_fks_repeat_safe(self):
        before_bytes=(self.path/'auth.sqlite3').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'migrate-access'):
            AuthStore(self.path)
        self.assertEqual((self.path/'auth.sqlite3').read_bytes(),before_bytes)
        self.assertTrue(migrate_access(self.path))
        after=self.rows()
        for table,rows in self.before.items():
            if table=='users': rows=[(*row,0) for row in rows]
            self.assertEqual(after[table],rows,table)
        self.assertEqual((self.path/'auth.key').read_bytes(),self.key)
        db=sqlite3.connect(self.path/'auth.sqlite3')
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok'); db.close()
        self.assertFalse(migrate_access(self.path))
        self.assertEqual(self.rows(),after)
        store=AuthStore(self.path)
        with store.transaction() as db:
            original_hash=db.execute("SELECT password_hash FROM users WHERE username='mykyta'").fetchone()[0]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(admin_cli.main(['--state-dir',str(self.path),'grant-superadmin','mykyta']),0)
        with store.transaction() as db:
            row=db.execute("SELECT role,password_hash FROM users WHERE username='mykyta'").fetchone()
            self.assertEqual(tuple(row),('superadmin',original_hash))

    def test_running_engine_blocks_migration(self):
        lock=self.path/'hmi-engine.lock'; lock.touch(mode=0o600)
        with lock.open('rb') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError,'Stop'):
                migrate_access(self.path)
        self.assertEqual(self.rows(),self.before)

    def test_failed_migration_rolls_back_all_ddl_and_data(self):
        from .access_control import initialize_roles
        def fail_after_ddl(db):
            initialize_roles(db)
            raise RuntimeError('fixture')
        with patch('web_hmi.access_migration.initialize_roles',side_effect=fail_after_ddl):
            with self.assertRaises(RuntimeError): migrate_access(self.path)
        self.assertEqual(self.rows(),self.before)
        self.assertTrue(migrate_access(self.path))
