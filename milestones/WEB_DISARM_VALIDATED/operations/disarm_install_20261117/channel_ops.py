#!/usr/bin/python3
"""Default passive check. --activate only after operator approval. Never sends RPC/MAVLink."""
import argparse
from datetime import datetime,timezone
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import time
import urllib.request

s=importlib.util.spec_from_file_location('cutover',Path(__file__).with_name('cutover.py'))
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
s=importlib.util.spec_from_file_location('stock_activation',Path(__file__).resolve().parent.parent/'home_externalnav_ready/activate_stock_tx.py')
stock=importlib.util.module_from_spec(s);s.loader.exec_module(stock)
GW=Path('/run/systemd/system/ugv-command-gateway.service.d/90-disarm-enabled.conf')
FE=Path('/run/systemd/system/ugv-hmi-auth.service.d/90-disarm-enabled.conf')
ENV=Path('/run/ugv-disarm-policy/frontend-enabled.env')
CANCEL=Path('/run/ugv-disarm-policy/activation-cancelled')


def trusted_policy():
    p=m.POLICY.parent;i=p.lstat()
    assert not p.is_symlink() and i.st_uid==0 and not i.st_mode&0o022


def block():
    trusted_policy()
    fd=os.open(m.POLICY,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o444)
    os.fchown(fd,0,0);os.fchmod(fd,0o444);os.close(fd)
    cancel_fd=os.open(CANCEL,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o444)
    os.fchown(cancel_fd,0,0);os.fchmod(cancel_fd,0o444);os.close(cancel_fd)
    # Marker first; channel closure next. Never restart owner for emergency block.
    m.run('systemctl','stop','ugv-command-gateway.service')
    for p in (GW,FE,ENV):
        if p.exists():
            assert not p.is_symlink() and p.stat().st_uid==0
            p.unlink()
    m.run('systemctl','daemon-reload')
    m.run('systemctl','restart','ugv-hmi-auth.service')
    print('DISARM_BLOCKED: inhibit ON, gateway stopped, frontend OFF; owner not restarted')


def check():
    trusted_policy();assert m.POLICY.exists()
    for u in m.UNITS:assert m.prop(u,'ActiveState')=='active'
    assert m.owners()==[int(m.prop('ugv-hmi.service','MainPID'))]
    assert '--enable-disarm' not in m.prop('ugv-command-gateway.service','ExecStart')
    assert '--enable-bench-hold' not in m.prop('ugv-hmi.service','ExecStart')
    assert not any(p.exists() for p in (GW,FE,ENV))
    env=dict(x.split('=',1) for x in Path('/etc/ugv-disarm/identities.env').read_text().splitlines() if x and not x.startswith('#'))
    assert env['UGV_HMI_ENABLE_DISARM']=='0' and env['UGV_HMI_TELEMETRY']=='0'
    stock.verified_package()
    assert m.prop('ugv-hmi.service','WorkingDirectory')==str(stock.PACKAGE/'owner')
    stock.inspector()(stock.LEDGER)
    ledger=m.digest_db(stock.LEDGER)
    with sqlite3.connect('file:/var/lib/ugv-hmi/auth.sqlite3?mode=ro',uri=True) as c:
        assert [r[0] for r in c.execute("SELECT role FROM role_permissions WHERE permission='pixhawk.disarm' ORDER BY role")]==['admin','operator','superadmin']
    context=ssl.create_default_context(cafile='/home/ugvs2/ugv-ca.crt')
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    with op.open('https://192.168.110.59/login',timeout=4) as r:
        assert r.status==200
        cookie=r.headers.get('Set-Cookie','')
        assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
        assert r.headers.get('Strict-Transport-Security')=='max-age=31536000'
    safety=m.safety_check()
    assert m.get(8084,'/api/state')[1]['legacy_control']=='DISABLED'
    print('CHANNEL_PREPARATION_PASS: OFF, inhibit ON, fresh safety, idle ledger retained, RBAC, HTTPS+CA verified')
    return ledger,safety


def cleanup_backend():
    assert m.prop('ugv-hmi.service','MainPID')=='0' and m.owners()==[]
    p=Path('/run/ugv-pixhawk-control/backend.sock')
    if not p.exists():return
    info=p.lstat();assert (info.st_uid,info.st_gid,info.st_mode&0o777)==(1000,983,0o660)
    with socket.socket(socket.AF_UNIX) as c:
        try:c.connect(str(p))
        except OSError as e:assert e.errno==errno.ECONNREFUSED
        else:raise AssertionError('Backend still listening')
    assert p.lstat().st_ino==info.st_ino;p.unlink()


def rootfile(p,text):
    p.parent.mkdir(parents=True,exist_ok=True)
    assert not p.parent.is_symlink() and p.parent.stat().st_uid==0
    p.parent.chmod(0o755)
    assert not p.exists() and not p.is_symlink()
    p.write_text(text);p.chmod(0o644)


def activate():
    ledger,safety=check()
    assert not CANCEL.exists(), 'Prior block cancels activation; inspect before any reapproval'
    backup=Path('/var/backups/ugv-disarm')/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-activation')
    backup.mkdir(mode=0o700)
    report={'status':'PREPARING','backup':str(backup),'rpc_requests':0,'mavlink_commands':0,'pre_safety':safety}
    def save(): (backup/'ACTIVATION.json').write_text(json.dumps(report,indent=2)+'\n')
    for u in m.UNITS:(backup/(u+'.txt')).write_text(m.run('systemctl','cat',u))
    for name,p in (('auth.sqlite3',m.STATE/'auth.sqlite3'),('disarm.sqlite3',Path('/var/lib/ugv-pixhawk/disarm.sqlite3'))):
        with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as src:
            with sqlite3.connect(backup/name) as dst:src.backup(dst)
    save()
    try:
        # Keep marker present throughout shutdown and runtime configuration.
        m.run('systemctl','stop','ugv-hmi-auth.service','ugv-command-gateway.service')
        stock.inspector()(stock.LEDGER)
        ledger=m.digest_db(stock.LEDGER)
        m.safety_check();stock.rc_safe()
        m.run('systemctl','stop','ugv-hmi.service')
        assert m.digest_db(stock.LEDGER)==ledger, 'Ledger changed during shutdown'
        cleanup_backend()
        command=next(x for x in Path('/etc/systemd/system/ugv-command-gateway.service').read_text().splitlines() if x.startswith('ExecStart='))
        assert '--enable-disarm' not in command
        rootfile(GW,'[Service]\nExecStart=\n'+command+' --enable-disarm\n')
        # Base identities.env remains OFF. Last runtime EnvironmentFile overrides only frontend flag.
        rootfile(ENV,'UGV_HMI_ENABLE_DISARM=1\nUGV_HMI_TELEMETRY=0\n')
        rootfile(FE,'[Service]\nEnvironmentFile='+str(ENV)+'\n')
        m.run('systemctl','daemon-reload')
        assert m.POLICY.exists() and m.owners()==[]
        # Both latched processes must start afresh without marker. Gateway/frontend are stopped.
        assert not CANCEL.exists(), 'Activation cancelled'
        m.POLICY.unlink()
        assert not CANCEL.exists(), 'Activation cancelled'
        m.run('systemctl','start','ugv-hmi.service')
        deadline=time.monotonic()+20
        while True:
            try:safety=m.safety_check();break
            except (OSError,AssertionError,ValueError):
                if time.monotonic()>deadline:raise
                time.sleep(.5)
        assert not CANCEL.exists(), 'Activation cancelled'
        assert m.digest_db(stock.LEDGER)==ledger, 'Ledger changed at owner restart'
        m.run('systemctl','start','ugv-command-gateway.service')
        m.run('systemctl','start','ugv-hmi-auth.service')
        deadline=time.monotonic()+15
        while True:
            try:
                assert m.get(8083,'/login')[0]==200
                pid=m.prop('ugv-hmi-auth.service','MainPID')
                data=Path('/proc/'+pid+'/environ').read_bytes().split(b'\0')
                assert b'UGV_HMI_ENABLE_DISARM=1' in data and b'UGV_HMI_TELEMETRY=0' in data
                break
            except (OSError,AssertionError):
                if time.monotonic()>deadline:raise
                time.sleep(.5)
        for u in m.UNITS:
            assert m.prop(u,'ActiveState')=='active', 'Service not active: '+u
        for path,uid,gid in (('/run/ugv-pixhawk-control/backend.sock',1000,983),('/run/ugv-command/ingress.sock',997,984)):
            info=Path(path).lstat()
            assert (info.st_uid,info.st_gid,info.st_mode&0o777)==(uid,gid,0o660)
        safety=m.safety_check()
        assert not m.POLICY.exists() and not CANCEL.exists()
        assert '--enable-disarm' in m.prop('ugv-command-gateway.service','ExecStart')
        assert m.owners()==[int(m.prop('ugv-hmi.service','MainPID'))]
        assert m.digest_db(Path('/var/lib/ugv-pixhawk/disarm.sqlite3'))==ledger
        report.update(status='CHANNEL_ENABLED_NO_COMMAND_SENT',safety=safety)
        save();print(json.dumps(report,indent=2))
    except BaseException as e:
        report.update(status='ACTIVATION_FAILED',error=type(e).__name__+': '+str(e));save()
        block()
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group();group.add_argument('--activate',action='store_true');group.add_argument('--block',action='store_true')
    p.add_argument('--operator-approved',action='store_true')
    a=p.parse_args();assert os.geteuid()==0
    os.umask(0o077)
    if a.block:block();return # Emergency marker must not wait on activation lock.
    with open('/run/ugv-disarm-cutover.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.activate:
            assert a.operator_approved,'Activation requires explicit operator approval'
            activate()
        else:check()

if __name__=='__main__':main()
