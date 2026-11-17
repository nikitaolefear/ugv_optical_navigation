#!/usr/bin/python3
"""Explicit --apply cutover; default --check. DISARM OFF + root inhibit mandatory."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

ROOT = Path('/opt/ugv-disarm')
SOURCE = Path('/home/ugvs2/.local/state/ugv-hmi')
STATE = Path('/var/lib/ugv-hmi')
POLICY = Path('/run/ugv-disarm-policy/inhibit')
UNITS = ('ugv-hmi.service','ugv-command-gateway.service','ugv-hmi-auth.service')
COMMIT = 'ca8512a6a41eec42a73809fdf7ca8e797f81e7a5'


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if p.returncode:
        raise RuntimeError('Command failed: '+args[0]+' '+args[1]+'; '+p.stderr[:700])
    return p.stdout.strip()


def prop(unit, name):
    return run('systemctl','show',unit,'-p',name,'--value')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def get(port,path,method='GET'):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',method=method,
                                    headers={'Host':'192.168.110.59' if port==8083 else '127.0.0.1'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=3) as r:
            data=r.read()
            return r.status,json.loads(data) if 'json' in r.headers.get('Content-Type','') else data
    except urllib.error.HTTPError as e:
        data=e.read()
        return e.code,json.loads(data) if 'json' in e.headers.get('Content-Type','') else data


def owners():
    d=json.loads(run('/usr/local/libexec/ugv-pixhawk-owners'))
    assert d['complete'] is True and d['error'] is None, 'Incomplete serial-owner audit'
    return d['owners']


def digest_db(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)], 'SQLite integrity failure'
        assert not db.execute('PRAGMA foreign_key_check').fetchall(), 'Foreign key failure'
        digest=hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode())
            digest.update(b'\n')
        counts={r[0]:db.execute('SELECT count(*) FROM "'+r[0].replace('"','""')+'"').fetchone()[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'logical_sha256':digest.hexdigest(),'table_counts':counts}


def clone_state(target,uid,gid):
    target.mkdir(mode=0o700,exist_ok=False)
    assert len((SOURCE/'auth.key').read_bytes())==32
    before=digest_db(SOURCE/'auth.sqlite3')
    with sqlite3.connect((SOURCE/'auth.sqlite3').as_uri()+'?mode=ro',uri=True) as src:
        with sqlite3.connect(target/'auth.sqlite3') as dst:
            src.backup(dst)
    # Only auth-owned artifacts. Owner journal stays at the current path.
    for p in SOURCE.iterdir():
        if p.name=='auth.key' or p.name.startswith('telemetry-technical.jsonl'):
            assert p.is_file() and not p.is_symlink()
            shutil.copyfile(p,target/p.name)
    after=digest_db(target/'auth.sqlite3')
    # In rehearsal the live DB may change between snapshots; check mode only
    # compares backup integrity. For actual migration both writers are stopped.
    for p in target.iterdir():
        os.chown(p,uid,gid)
        p.chmod(0o600)
    os.chown(target,uid,gid)
    target.chmod(0o700)
    assert (SOURCE/'auth.key').read_bytes()==(target/'auth.key').read_bytes()
    return before,after


def preflight():
    assert os.geteuid()==0, 'Run via sudo in operator terminal'
    d=json.loads((ROOT/'PROVISIONING.json').read_text())
    assert d['status']=='PROVISIONED_INACTIVE' and d['commit']==COMMIT
    assert [d[k] for k in ('frontend_uid','gateway_uid','owner_uid','frontend_gid','backend_gid')]==[999,997,1000,984,983]
    assert json.loads((ROOT/'ISOLATION_CHECK.json').read_text())['status']=='PASS'
    for p in (ROOT,ROOT/'package',ROOT/'pending-systemd',Path('/etc/ugv-disarm'),POLICY.parent,POLICY):
        info=p.lstat()
        assert not p.is_symlink() and info.st_uid==0 and not info.st_mode & 0o022, 'Untrusted path: '+str(p)
    manifest=Path('/home/ugvs2/ugv-disarm-installation/20261116T191832586517Z/inactive-release')/COMMIT/'INSTALLED_SHA256.json'
    expected=json.loads(manifest.read_text())
    for name,digest in expected.items():
        p=ROOT/'package'/Path(name).relative_to('hmi_disarm_prepared')
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest, 'Release changed: '+name
        assert p.stat().st_uid==0 and not p.stat().st_mode&0o022
    env=dict(line.split('=',1) for line in Path('/etc/ugv-disarm/identities.env').read_text().splitlines() if line and not line.startswith('#'))
    assert env['UGV_HMI_ENABLE_DISARM']=='0' and env['UGV_HMI_TELEMETRY']=='0'
    assert [env[k] for k in ('UGV_FRONTEND_UID','UGV_GATEWAY_UID','UGV_OWNER_UID','UGV_FRONTEND_RPC_GID','UGV_OWNER_RPC_GID')]==['999','997','1000','984','983']
    assert env['UGV_DISARM_GATEWAY_UID']=='997' and env['UGV_DISARM_FRONTEND_GID']=='984'
    for name in UNITS:
        text=(ROOT/'pending-systemd'/name).read_text()
        execs=[line for line in text.splitlines() if line.startswith('ExecStart=')]
        assert len(execs)==1 and '--enable-disarm' not in execs[0] and '--enable-bench-hold' not in execs[0]
    run('systemd-analyze','verify',*[str(ROOT/'pending-systemd'/u) for u in UNITS])
    assert prop('ugv-hmi.service','ActiveState')=='active'
    assert prop('ugv-hmi-auth.service','ActiveState')=='active'
    assert prop('ugv-hmi.service','DropInPaths')=='/etc/systemd/system/ugv-hmi.service.d/hold-bench.conf'
    assert not prop('ugv-hmi-auth.service','DropInPaths')
    assert not prop('ugv-command-gateway.service','FragmentPath'), 'Gateway already installed'
    assert prop('ugv-hmi-auth.service','WorkingDirectory')=='/home/ugvs2/ugv-hmi-auth-password8-release'
    current=prop('ugv-hmi.service','ExecStart')
    assert '--enable-bench-hold' in current and '--web-disarm' not in current
    assert owners()==[int(prop('ugv-hmi.service','MainPID'))]
    info=STATE.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid==999 and info.st_gid==984 and stat.S_IMODE(info.st_mode)==0o700
    assert list(STATE.iterdir())==[], 'Destination auth state is not empty'
    info=Path('/var/lib/ugv-pixhawk').lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid==1000 and stat.S_IMODE(info.st_mode)==0o700
    assert list(Path('/var/lib/ugv-pixhawk').iterdir())==[], 'Existing ledger requires explicit migration'
    for p in ('/run/ugv-pixhawk-control/backend.sock','/run/ugv-command/ingress.sock'):
        assert not os.path.lexists(p), 'Existing socket: '+p
    assert (SOURCE/'auth.sqlite3').is_file() and not (SOURCE/'auth.sqlite3').is_symlink()
    assert (SOURCE/'auth.key').is_file() and not (SOURCE/'auth.key').is_symlink()
    c=sqlite3.connect((SOURCE/'auth.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        assert c.execute('SELECT version FROM access_meta').fetchall()==[(1,)]
        assert c.execute('SELECT count(*) FROM users').fetchone()[0]>0
    finally:
        c.close()
    safety_check()
    return d


def safety_check():
    code,tele=get(8082,'/api/telemetry')
    assert code==200 and tele['connection']=='CONNECTED'
    assert tele['armed']=={'state':'FRESH','value':'DISARMED'}
    assert tele['messages']['HEARTBEAT']['state']=='FRESH'
    code,safety=get(8084,'/api/state')
    assert code==200 and safety['stop_latch'] is True and safety['movement_allowed'] is False
    assert safety['hold_phase']=='IDLE'
    return {'armed':tele['armed'],'heartbeat':tele['messages']['HEARTBEAT'],
            'stop_latch':True,'movement_allowed':False,'hold_phase':'IDLE'}


def rehearse(d):
    target=Path(tempfile.mkdtemp(prefix='ugv-hmi-rehearsal-',dir='/var/lib'))
    target.rmdir()
    try:
        _,snapshot=clone_state(target,d['frontend_uid'],d['frontend_gid'])
        code='''import sys,os,json
sys.path[:0]=['/opt/ugv-disarm/package/auth','/opt/ugv-disarm/package']
from web_hmi.pixhawk_telemetry import PixhawkTelemetry
from disarm_control.ipc import OwnerClient
def forbidden(*a,**k):raise AssertionError('Hardware/RPC forbidden in rehearsal')
PixhawkTelemetry.start=forbidden
OwnerClient.call=forbidden
from web_hmi.app import create_app
from flask import g
app=create_app()
assert app.config['DISARM_ENABLED'] is False
with app.test_client() as client:
 r=client.get('/login',base_url='https://192.168.110.59')
 assert r.status_code==200
 assert 'Secure' in r.headers['Set-Cookie'] and 'HttpOnly' in r.headers['Set-Cookie']
 assert client.get('/',base_url='https://192.168.110.59').status_code==303
 assert client.get('/static/disarm.js',base_url='https://192.168.110.59').status_code==303
with app.test_request_context('/',base_url='https://192.168.110.59'):
 g.user={'id':1,'username':'rehearsal','role':'fixture','permissions':['status.view','pixhawk.disarm']}
 g.session={'csrf':'fixture'}
 html=app.view_functions['index']()
 assert 'id="pixhawk-disarm" type="button" disabled' in html
print('AUTH_REHEARSAL_PASS: existing DB/key, login, cookie policy, disabled DISARM template; no login credentials, RPC or serial')
'''
        cmd=['systemd-run','--quiet','--wait','--pipe','--collect','--unit=ugv-hmi-auth-rehearsal']
        for p in ('User=ugv-hmi','Group=ugv-rpc-frontend','NoNewPrivileges=yes','PrivateDevices=yes',
                  'PrivateTmp=yes','ProtectHome=yes','ProtectSystem=strict','ReadWritePaths='+str(target),
                  'CapabilityBoundingSet=','RestrictAddressFamilies=AF_UNIX'):
            cmd+=['--property',p]
        for v in ('UGV_HMI_STATE_DIR='+str(target),'UGV_HMI_ENABLE_DISARM=0','UGV_HMI_TELEMETRY=0',
                  'UGV_HMI_PUBLIC_ORIGIN=https://192.168.110.59','UGV_DISARM_GATEWAY_UID=997','UGV_DISARM_FRONTEND_GID=984',
                  'PYTHONDONTWRITEBYTECODE=1'):
            cmd+=['--setenv',v]
        print(run(*cmd,'/opt/ugv-disarm/venv-auth/bin/python','-I','-c',code))
        return {'status':'PASS','source_snapshot':snapshot,'hardware_access':False,'production_rpc_requests':0}
    finally:
        shutil.rmtree(target)


def install_grant(report):
    operations=ROOT/'operations'
    if operations.exists() or operations.is_symlink():
        info=operations.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
    else:
        operations.mkdir(mode=0o700)
    # Explicit chmod is required: mkdir(mode=...) is filtered by umask0077.
    os.chown(operations,0,984)
    operations.chmod(0o710)
    source=Path(__file__).with_name('grant_disarm_roles.py')
    target=operations/source.name
    assert not target.is_symlink()
    shutil.copyfile(source,target)
    os.chown(target,0,984)
    target.chmod(0o640)
    run('runuser','-u','ugv-hmi','--','/usr/bin/test','-r',str(target))
    report['rbac_grant']=json.loads(run('runuser','-u','ugv-hmi','--',
        '/opt/ugv-disarm/venv-auth/bin/python','-I',str(target),
        '--state-dir','/var/lib/ugv-hmi'))
    report['auth_migration']['rbac_changed']=bool(report['rbac_grant']['changed_roles'])


def complete(backup,report,save):
    for p in backup.rglob('*'):
        if not p.is_symlink():p.chmod(0o700 if p.is_dir() else 0o600)
    # Remove active legacy ExecStart override, retaining its exact copy in backup.
    drop=Path('/etc/systemd/system/ugv-hmi.service.d/hold-bench.conf')
    drop.unlink()
    for name in UNITS:
        shutil.copyfile(ROOT/'pending-systemd'/name,Path('/etc/systemd/system')/name)
        os.chmod(Path('/etc/systemd/system')/name,0o644)
    run('systemctl','daemon-reload')
    assert not prop('ugv-hmi.service','DropInPaths')
    assert '--enable-bench-hold' not in prop('ugv-hmi.service','ExecStart')
    assert '--enable-disarm' not in prop('ugv-command-gateway.service','ExecStart')
    assert POLICY.exists()
    run('systemctl','start','ugv-hmi.service')
    # Bounded readiness, never another start attempt and no command retries.
    deadline=time.monotonic()+20
    while True:
        try:
            safety=safety_check()
            assert get(8084,'/api/state')[1]['legacy_control']=='DISABLED'
            break
        except (AssertionError,OSError,ValueError):
            if time.monotonic()>deadline:raise
            time.sleep(.5)
    assert owners()==[int(prop('ugv-hmi.service','MainPID'))]
    run('systemctl','start','ugv-command-gateway.service')
    run('systemctl','start','ugv-hmi-auth.service')
    deadline=time.monotonic()+15
    while True:
        try:
            assert get(8083,'/login')[0]==200
            assert get(8083,'/static/disarm.js')[0]==303
            break
        except (AssertionError,OSError,ValueError):
            if time.monotonic()>deadline:raise
            time.sleep(.5)
    for name,user in zip(UNITS,('ugvs2','ugv-command','ugv-hmi')):
        assert prop(name,'ActiveState')=='active' and prop(name,'User')==user
    for name,uid,gids in (('ugv-hmi-auth.service',999,{984}),('ugv-command-gateway.service',997,{983,984})):
        status=dict(line.split(':',1) for line in Path('/proc/'+prop(name,'MainPID')+'/status').read_text().splitlines() if ':' in line)
        assert set(map(int,status['Uid'].split()))=={uid}
        assert set(map(int,status['Groups'].split()))==gids
    for p,uid,gid in (('/run/ugv-pixhawk-control/backend.sock',1000,983),('/run/ugv-command/ingress.sock',997,984)):
        info=Path(p).lstat()
        assert stat.S_ISSOCK(info.st_mode) and info.st_uid==uid and info.st_gid==gid and stat.S_IMODE(info.st_mode)==0o660
    rpc_code="""import sys
sys.path.insert(0,'/opt/ugv-disarm/package')
from disarm_control.ipc import OwnerClient,SocketPolicy
r=OwnerClient('/run/ugv-command/ingress.sock',policy=SocketPolicy(997,984,0o710,0o660)).call('STATUS',{'id':2147483647,'username':'integration_check'},'00000000-0000-4000-8000-000000000001')
assert r['ok'] is False and r['error']=='REQUEST_NOT_FOUND'
print('LIVE_STATUS_RELAY_OK')
"""
    print(run('runuser','-u','ugv-hmi','--','/opt/ugv-disarm/venv-owner/bin/python','-I','-c',rpc_code))
    # Body deliberately has no action. Only after verifying the new process/code.
    code,response=get(8084,'/api/test-action',method='POST')
    assert code==403 and response['error']=='LEGACY_CONTROL_DISABLED'
    assert owners()==[int(prop('ugv-hmi.service','MainPID'))]
    assert POLICY.exists()
    ledger=Path('/var/lib/ugv-pixhawk/disarm.sqlite3')
    ld=digest_db(ledger)
    assert ld['table_counts']['commands']==0 and ld['table_counts']['delivery_evidence']==0
    run('systemctl','enable','ugv-command-gateway.service')
    report.update(status='CUTOVER_PASS_DISARM_OFF',safety=safety,
                  serial_owner=int(prop('ugv-hmi.service','MainPID')),legacy_control='DISABLED',
                  ledger=ld,production_disarm_requests=0,production_status_requests=1,
                  authenticated_user_login_tested=False,physical_test_permitted=False)
    save()
    print(json.dumps(report,indent=2))


def apply(d):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=Path('/var/backups/ugv-disarm')/stamp
    backup.mkdir(parents=True,mode=0o700)
    backup.parent.chmod(0o700)
    backup.chmod(0o700)
    report={'status':'IN_PROGRESS','backup':str(backup),'commit':COMMIT,'frontend_disarm':False,'inhibit':True}
    def save():
        (backup/'CUTOVER.json').write_text(json.dumps(report,indent=2)+'\n')
        (backup/'CUTOVER.json').chmod(0o600)
    changed=False
    source_locks=[]
    try:
        save()
        # Preserve actual system config before any mutation; no old auth DB restoration.
        for name in ('ugv-hmi.service','ugv-hmi-auth.service'):
            shutil.copy2('/etc/systemd/system/'+name,backup/name)
            (backup/(name+'.effective.txt')).write_text(run('systemctl','cat',name))
        shutil.copytree('/etc/systemd/system/ugv-hmi.service.d',backup/'ugv-hmi.service.d')
        shutil.copy2('/etc/nginx/conf.d/ugv-hmi.conf',backup/'nginx-ugv-hmi.conf')
        changed=True
        run('systemctl','stop','ugv-hmi-auth.service')
        assert prop('ugv-hmi-auth.service','MainPID')=='0'
        run('systemctl','stop','ugv-hmi.service')
        assert prop('ugv-hmi.service','MainPID')=='0' and owners()==[]
        for name in ('access-control.lock','hmi-engine.lock'):
            fd=os.open(SOURCE/name,os.O_RDWR|os.O_NOFOLLOW)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            source_locks.append(fd)
        # All writers now stopped; preserve full current state plus a SQLite API snapshot.
        shutil.copytree(SOURCE,backup/'original-state',symlinks=False)
        before,snapshot=clone_state(backup/'auth-snapshot',0,0)
        assert before==snapshot==digest_db(SOURCE/'auth.sqlite3'), 'Source changed while stopped'
        # Existing destination is a provisioned empty directory. Keep its identity.
        temporary=STATE.with_name('ugv-hmi-migration-'+stamp)
        _,copied=clone_state(temporary,d['frontend_uid'],d['frontend_gid'])
        assert copied==snapshot and (temporary/'auth.key').read_bytes()==(backup/'auth-snapshot/auth.key').read_bytes()
        for p in temporary.iterdir():
            os.rename(p,STATE/p.name)
        temporary.rmdir()
        assert digest_db(STATE/'auth.sqlite3')==snapshot
        report['auth_migration']={'integrity':'ok','logical_copy_equal':True,'key_equal':True,'snapshot':snapshot,
                                  'source_preserved':True,'rbac_changed':False,'sessions_preserved_at_copy':True}
        install_grant(report)
        save()
        complete(backup,report,save)
    except BaseException as e:
        report.update(status='FAILED_CLOSED',error=type(e).__name__+': '+str(e))
        save()
        if changed:
            # Never restart the old writable owner or replace current audit/ledger.
            subprocess.run(['systemctl','stop','ugv-hmi-auth.service','ugv-command-gateway.service','ugv-hmi.service'],capture_output=True,timeout=45)
        print('FAILED_CLOSED; preserve current state and backup: '+str(backup),file=sys.stderr)
        raise
    finally:
        for fd in source_locks:os.close(fd)


def resume_preflight(backup):
    assert os.geteuid()==0
    backup=Path(backup).absolute()
    assert backup.parent==Path('/var/backups/ugv-disarm') and not backup.is_symlink()
    for p in (backup,backup.parent):
        info=p.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o077
    report=json.loads((backup/'CUTOVER.json').read_text())
    assert report['status']=='FAILED_CLOSED' and report['commit']==COMMIT
    assert 'grant_disarm_roles.py' in report['error'] and '[Errno 13]' in report['error']
    assert report['auth_migration']['logical_copy_equal'] is True
    assert report['auth_migration']['rbac_changed'] is False and 'rbac_grant' not in report
    snapshot=report['auth_migration']['snapshot']
    assert digest_db(STATE/'auth.sqlite3')==snapshot==digest_db(backup/'auth-snapshot/auth.sqlite3')
    assert digest_db(SOURCE/'auth.sqlite3')==snapshot, 'Old auth writer changed source: refuse automatic recovery'
    assert len((STATE/'auth.key').read_bytes())==32
    assert (STATE/'auth.key').read_bytes()==(backup/'auth-snapshot/auth.key').read_bytes()
    info=STATE.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid==999 and info.st_gid==984 and stat.S_IMODE(info.st_mode)==0o700
    for name in UNITS:
        assert prop(name,'MainPID')=='0' and prop(name,'ActiveState') in ('inactive','failed')
    assert owners()==[]
    assert prop('ugv-hmi.service','DropInPaths')=='/etc/systemd/system/ugv-hmi.service.d/hold-bench.conf'
    assert '--enable-bench-hold' in prop('ugv-hmi.service','ExecStart')
    assert not prop('ugv-command-gateway.service','FragmentPath')
    assert not list(Path('/var/lib/ugv-pixhawk').iterdir())
    for path in ('/run/ugv-pixhawk-control/backend.sock','/run/ugv-command/ingress.sock'):
        assert not os.path.lexists(path)
    for p in (POLICY.parent,POLICY,ROOT,ROOT/'pending-systemd'):
        info=p.lstat()
        assert not p.is_symlink() and info.st_uid==0 and not info.st_mode&0o022
    env=dict(line.split('=',1) for line in Path('/etc/ugv-disarm/identities.env').read_text().splitlines() if line and not line.startswith('#'))
    assert env['UGV_HMI_ENABLE_DISARM']=='0' and env['UGV_HMI_TELEMETRY']=='0'
    assert [env[k] for k in ('UGV_FRONTEND_UID','UGV_GATEWAY_UID','UGV_OWNER_UID','UGV_FRONTEND_RPC_GID','UGV_OWNER_RPC_GID')]==['999','997','1000','984','983']
    manifest=Path('/home/ugvs2/ugv-disarm-installation/20261116T191832586517Z/inactive-release')/COMMIT/'INSTALLED_SHA256.json'
    for name,digest in json.loads(manifest.read_text()).items():
        p=ROOT/'package'/Path(name).relative_to('hmi_disarm_prepared')
        assert p.stat().st_uid==0 and not p.stat().st_mode&0o022
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest
    for name in UNITS:
        p=ROOT/'pending-systemd'/name
        assert p.stat().st_uid==0 and not p.stat().st_mode&0o022
        for line in p.read_text().splitlines():
            if line.startswith('ExecStart='):
                assert '--enable-disarm' not in line and '--enable-bench-hold' not in line
    run('systemd-analyze','verify',*[str(ROOT/'pending-systemd'/u) for u in UNITS])
    assert not list(backup.glob('RECOVERY_*.json')), 'Recovery already attempted: preserve updated state for inspection'
    print('RESUME_PREFLIGHT_PASS: exact migrated snapshot; stopped services; no serial owner; no migration required')
    return backup,report


def resume(backup,report):
    # The original CUTOVER.json and DB snapshot remain immutable evidence.
    report=json.loads(json.dumps(report))
    report['status']='RECOVERY_IN_PROGRESS'
    report.pop('error',None)
    report['migration_repeated']=False
    target=backup/('RECOVERY_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    def save():
        target.write_text(json.dumps(report,indent=2)+'\n')
        target.chmod(0o600)
    save()
    try:
        install_grant(report)
        save()
        complete(backup,report,save)
    except BaseException as e:
        report.update(status='FAILED_CLOSED',error=type(e).__name__+': '+str(e))
        save()
        subprocess.run(['systemctl','stop','ugv-hmi-auth.service','ugv-command-gateway.service','ugv-hmi.service'],capture_output=True,timeout=45)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--rehearse',action='store_true')
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--resume-check',metavar='BACKUP')
    mode.add_argument('--resume-from',metavar='BACKUP')
    args=parser.parse_args()
    os.umask(0o077)
    if os.geteuid()!=0:sys.exit('Run using sudo')
    with open('/run/ugv-disarm-cutover.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.resume_check or args.resume_from:
            backup,report=resume_preflight(args.resume_check or args.resume_from)
            if args.resume_from:resume(backup,report)
            return
        d=preflight()
        print('PREFLIGHT_PASS: live services unchanged; DISARM OFF, inhibit ON')
        if args.rehearse or args.apply:
            result=rehearse(d)
            out=ROOT/'AUTH_REHEARSAL.json'
            out.write_text(json.dumps(result,indent=2)+'\n')
            out.chmod(0o600)
        if args.apply:
            # Recheck after rehearsal before stopping writers.
            apply(preflight())

if __name__=='__main__':
    main()
