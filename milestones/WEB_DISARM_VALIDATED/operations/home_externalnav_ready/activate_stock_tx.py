#!/usr/bin/python3
"""Operator-only stock ExternalNav TX activation with fresh HOME evidence.
--check is read-only. --enable requires explicit physical confirmations.
--disable removes the boot-bound alignment grant first; no drive command exists.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode=True
PACKAGE=Path('/opt/ugv-tilted-observe/20261117/package')
RELEASE=PACKAGE.parent
TILT_DROP=Path('/etc/systemd/system/ugv-hmi.service.d/tilted-observe.conf')
TX_DROP=TILT_DROP.with_name('zz-externalnav-tx.conf')
GRANT=Path('/etc/ugv-externalnav/alignment-verified.json')
LEDGER=Path('/var/lib/ugv-pixhawk/disarm.sqlite3')
SOCKET=Path('/run/ugv-pixhawk-control/backend.sock')
BOOT=Path('/proc/sys/kernel/random/boot_id')
BACKUPS=Path('/var/backups/ugv-disarm')
OWNER='ugv-hmi.service'
GATEWAY='ugv-command-gateway.service'
AUTH='ugv-hmi-auth.service'
ASSETS=('camera_calibration_fisheye.npz','rover_config.json','marker_map.json')


def require(ok,message):
    if not ok:raise RuntimeError(message)


def number(x):return type(x) in (int,float) and math.isfinite(x)


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(*args):
    p=subprocess.run(args,text=True,capture_output=True,timeout=40,
                     env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
    require(p.returncode==0,'Command failed: '+' '.join(args[:2])+': '+p.stderr[-300:])
    return p.stdout.strip()


def prop(service,key):return run('systemctl','show',service,'-p',key,'--value')


def get(path):
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open('http://127.0.0.1:8084'+path,timeout=3) as r:return json.load(r)


def immutable(path):
    info=path.lstat()
    require(not path.is_symlink() and info.st_uid==0 and not info.st_mode&0o022,
            'Expected root-owned non-writable path: '+str(path))
    return info


def verified_package():
    immutable(RELEASE);immutable(PACKAGE);immutable(RELEASE/'SHA256SUMS')
    pins={}
    for line in (RELEASE/'SHA256SUMS').read_text().splitlines():
        value,relative=line.split('  ',1)
        if not relative.startswith('payload/'):continue
        rel=relative[len('payload/'):]
        require(not Path(rel).is_absolute() and '..' not in Path(rel).parts,'Unsafe package path')
        p=PACKAGE
        for part in Path(rel).parts:p=p/part;immutable(p)
        require(p.is_file() and sha(p)==value,'Installed package changed: '+rel)
        require(rel not in pins,'Duplicate installed pin');pins[rel]=value
    actual={p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob('*') if p.is_file()}
    require(actual==set(pins),'Installed package file set changed')
    require(all('optical/'+x in pins for x in ASSETS),'Missing optical asset pins')
    return {name:pins['optical/'+name] for name in ASSETS}


def inspector():
    spec=importlib.util.spec_from_file_location('stock_ledger_policy',PACKAGE/'disarm_control/ledger_policy.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m.inspect_ledger


def owners():
    d=json.loads(run('/usr/local/libexec/ugv-pixhawk-owners'))
    require(d.get('complete') is True and d.get('error') is None,'Incomplete serial-owner scan')
    return d['owners']


def sole_owner():
    pid=int(prop(OWNER,'MainPID'));require(pid>0 and owners()==[pid],'Expected sole running serial-owner')
    return pid


def disarmed():
    s=get('/api/state');a=s.get('heartbeat_age_s')
    require(s.get('pixhawk_disarmed') is True and s.get('heartbeat_reason') is None
            and number(a) and 0<=a<=1.5,'Fresh DISARMED required')
    require(s.get('legacy_control')=='DISABLED','Legacy control must remain closed')
    return s


def rc_safe():
    # Existing cached RC only: no parameter requests or control messages.
    d=get('/api/owner-diagnostics');r=d.get('rc');a=r.get('age_s') if isinstance(r,dict) else None
    c=r.get('channels',{}) if isinstance(r,dict) else {}
    require(number(a) and 0<=a<=1.5 and number(c.get('5')) and c['5']<1200
            and number(c.get('3')) and 1450<=c['3']<=1550,
            'Fresh cached RC5 DISARM and RC3 neutral required before TX')
    return dict(rc5=c['5'],rc3=c['3'],age_s=a)


def matrix_rotation(T):
    require(type(T) is list and len(T)==4 and all(type(r) is list and len(r)==4 for r in T)
            and all(number(v) for row in T for v in row),'Finite 4x4 camera measurement required')
    require(all(abs(a-b)<1e-9 for a,b in zip(T[3],[0,0,0,1])),'Invalid homogeneous transform')
    R=[row[:3] for row in T[:3]]
    for i in range(3):
        for j in range(3):require(abs(sum(R[k][i]*R[k][j] for k in range(3))-(i==j))<1e-6,'Invalid camera rotation')
    det=(R[0][0]*(R[1][1]*R[2][2]-R[1][2]*R[2][1])
         -R[0][1]*(R[1][0]*R[2][2]-R[1][2]*R[2][0])
         +R[0][2]*(R[1][0]*R[2][1]-R[1][1]*R[2][0]))
    require(abs(det-1)<1e-6,'Improper camera rotation')
    return R


def euler(roll,pitch,yaw):
    r,p,y=map(math.radians,(roll,pitch,yaw));cr,sr,cp,sp,cy,sy=math.cos(r),math.sin(r),math.cos(p),math.sin(p),math.cos(y),math.sin(y)
    return [[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr],
            [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr],[-sp,cp*sr,cp*cr]]


def stock_home(nav):
    """Reconstruct exact current stock planar worker from its direct camera PnP."""
    m=nav.get('optical_measurement');now=time.monotonic()
    require(nav.get('geometry_mode')=='LEGACY_PLANAR_Z0_YAW_ONLY' and nav.get('pose_fresh') is True
            and nav.get('marker')==23 and nav.get('fault') is False and nav.get('camera_thread_alive') is True,
            'Fresh stock ID23 observation required')
    require(type(m) is dict and m.get('source_marker')==23 and m.get('frame')=='C_OPTICAL_RIGHT_DOWN_FORWARD'
            and m.get('fresh') is True and m.get('nominal_marker_side_m')==.283,'Direct source23 optical measurement required')
    stamp=m.get('acquired_at');require(number(stamp) and 0<=now-stamp<=.2,'Camera frame stale')
    T=m.get('T_Coptical_M');R=matrix_rotation(T);t=[T[i][3] for i in range(3)]
    marker=next(x for x in json.loads((PACKAGE/'optical/marker_map.json').read_text())['markers'] if x['id']==23)
    o=marker['orientation'];W=euler(o['roll'],o['pitch'],o['yaw'])
    # World camera R = R_W_M * transpose(R_C_M).
    C=[[sum(W[i][k]*R[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
    pos=[marker['position'][axis]-sum(C[i][j]*t[j] for j in range(3)) for i,axis in enumerate(('x','y','z'))]
    require(math.hypot(C[0][2],C[1][2])>1e-10,'Degenerate camera heading')
    camera_yaw=math.degrees(math.atan2(C[1][2],C[0][2]))
    # Import only pinned pure planar math, never a camera or transport module.
    spec=importlib.util.spec_from_file_location('stock_planar',PACKAGE/'optical/pose_transform_2d.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    x,y,yaw=mod.camera_to_rover_pose(pos[0],pos[1],camera_yaw)
    return dict(x_m=x,y_m=y,yaw_flu_deg=yaw,acquired_at=stamp)


def read_evidence(path,assets):
    require(path is not None and path.is_file() and not path.is_symlink(),'Reviewed HOME evidence required')
    e=json.loads(path.read_text())
    require(e.get('schema_version')==1 and e.get('review_status')=='REVIEWED_STOCK_HOME',
            'Evidence must record reviewed stock HOME')
    require(e.get('boot_id')==BOOT.read_text().strip() and e.get('package_root')==str(PACKAGE),
            'Evidence belongs to another boot/package')
    require(e.get('sha256')==assets,'Evidence optical assets differ')
    require(e.get('operator_alignment_confirmed') is True and e.get('secondary_marker_reviewed') is True,
            'Operator alignment and secondary mapped marker review required')
    expected=e.get('expected_home');bounds=e.get('home_tolerances')
    require(expected==dict(x_m=0.,y_m=0.,yaw_flu_deg=0.),'Stock HOME must remain its original zero reference')
    require(type(bounds) is dict and number(bounds.get('xy_m')) and 0<bounds['xy_m']<=.15
            and number(bounds.get('yaw_deg')) and 0<bounds['yaw_deg']<=5.,'Explicit reviewed HOME bounds required (caps .15m/5deg)')
    return e


def within_home(h,e):
    return (math.hypot(h['x_m'],h['y_m'])<=e['home_tolerances']['xy_m']
            and abs(h['yaw_flu_deg'])<=e['home_tolerances']['yaw_deg'])


def fresh_home(e,duration=5.):
    seen=set();samples=[];until=time.monotonic()+duration
    while time.monotonic()<until:
        disarmed();n=get('/api/externalnav')
        require(n.get('tx_enabled') is False and n.get('alignment_verified') is False and n.get('sent')==0,
                'Expected inactive TX before activation')
        h=stock_home(n)
        require(within_home(h,e),'Fresh HOME outside reviewed bounds')
        if h['acquired_at'] not in seen:samples.append(h);seen.add(h['acquired_at'])
        time.sleep(.1)
    require(len(samples)>=10,'Insufficient distinct HOME frames')
    return dict(samples=len(samples),last=samples[-1])


def config_text():
    immutable(TILT_DROP)
    lines=TILT_DROP.read_text().splitlines()
    commands=[x for x in lines if x.startswith('ExecStart=') and x!='ExecStart=']
    require(len(commands)==1,'Expected one prior owner command');line=commands[0]
    require(line.count('--externalnav-observe-only')==1 and '--externalnav-tx' not in line
            and '--externalnav-bench-profile' not in line and '--diagnostic-params' not in line
            and '--enable-bench-hold' not in line,'Expected pure stock observe-only command')
    require('--web-disarm-db '+str(LEDGER) in line and '--web-disarm-socket '+str(SOCKET) in line,
            'Existing DISARM arguments must remain')
    return '[Service]\nExecStart=\n'+line.replace('--externalnav-observe-only','--externalnav-tx')+'\n'


def preflight(path):
    assets=verified_package()
    require(not TX_DROP.exists() and not TX_DROP.is_symlink() and not GRANT.exists() and not GRANT.is_symlink(),
            'Existing TX drop/grant requires explicit review, no overwrite')
    require(prop(OWNER,'WorkingDirectory')==str(PACKAGE/'owner'),'Unexpected owner package')
    require(prop(OWNER,'DropInPaths').split()==[str(TILT_DROP.with_name('externalnav-observe.conf')),str(TILT_DROP)],
            'Unexpected owner drop-ins; review all overrides before activation')
    for u in (OWNER,GATEWAY,AUTH):require(prop(u,'ActiveState')=='active','Inactive service: '+u)
    command=prop(OWNER,'ExecStart')
    require('--externalnav-observe-only' in command and '--externalnav-tx' not in command
            and '--externalnav-bench-profile' not in command,'Expected stock observe-only owner')
    pid=sole_owner();disarmed();rc=rc_safe();e=read_evidence(path,assets);home=fresh_home(e)
    ledger=inspector()(LEDGER)
    return dict(assets=assets,evidence=e,home=home,ledger=ledger,owner_pid=pid,
                auth_pid=prop(AUTH,'MainPID'),tx_drop=config_text(),rc=rc)


def dump(path):
    with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        return ''.join(x+'\n' for x in db.iterdump())


def rootdir(path,mode):
    require(not path.is_symlink(),'Symlink directory forbidden');path.mkdir(mode=mode,exist_ok=True)
    info=path.stat();require(info.st_uid==0 and stat.S_ISDIR(info.st_mode),'Root-owned directory required');path.chmod(mode)


def atomic(path,data):
    require(not path.is_symlink() and not path.exists(),'Refuse existing atomic target')
    tmp=path.with_name(path.name+'.new-'+str(os.getpid()))
    with open(tmp,'xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.chown(tmp,0,0);tmp.chmod(0o644)
    try:os.link(tmp,path,follow_symlinks=False)  # Atomic no-overwrite, including concurrent deny.
    finally:tmp.unlink(missing_ok=True)


def remove_grant():
    if not GRANT.exists():require(not GRANT.is_symlink(),'Unexpected dangling alignment symlink');return None
    immutable(GRANT.parent);immutable(GRANT)
    moved=GRANT.with_name('alignment-disabled-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    require(not moved.exists(),'Disable backup exists');os.replace(GRANT,moved)
    return str(moved)


def clean_socket():
    require(prop(OWNER,'MainPID')=='0' and owners()==[],'Owner remains; socket cleanup forbidden')
    try:info=SOCKET.lstat()
    except FileNotFoundError:return
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid==1000 and info.st_gid==983,'Unexpected backend socket')
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(1)
        try:c.connect(str(SOCKET))
        except OSError as e:require(e.errno==errno.ECONNREFUSED,'Backend socket not proven stale')
        else:raise RuntimeError('Backend listener remains')
    require(SOCKET.lstat().st_ino==info.st_ino,'Socket changed');SOCKET.unlink()


def wait_owner(tx):
    end=time.monotonic()+25
    while time.monotonic()<end:
        try:
            disarmed();sole_owner();n=get('/api/externalnav')
            require(n.get('tx_enabled') is tx and n.get('alignment_verified') is tx and n.get('fault') is False
                    and n.get('camera_thread_alive') is True,'Waiting for expected owner navigation mode')
            if tx:require(n.get('sent',0)>0 and n.get('pose_fresh') is True,'Waiting for actual ExternalNav packets')
            return n
        except (OSError,ValueError,RuntimeError):time.sleep(.2)
    raise RuntimeError('Owner readiness timeout')


def enable(ctx):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=BACKUPS/(stamp+'-stock-externalnav-tx')
    rootdir(backup.parent,0o700);backup.mkdir(mode=0o700)
    report=dict(status='PREPARING',backup=str(backup),parameters_changed=False,ledger_restored=False)
    def save():(backup/'RESULT.json').write_text(json.dumps(report,indent=2)+'\n')
    for u,name in ((OWNER,'owner'),(GATEWAY,'gateway'),(AUTH,'auth')):
        (backup/(name+'.unit.txt')).write_text(run('systemctl','cat',u))
    (backup/'reviewed-evidence.json').write_text(json.dumps(ctx['evidence'],indent=2)+'\n')
    save();gateway_stopped=owner_stopped=False
    try:
        gateway_stopped=True;run('systemctl','stop',GATEWAY)
        require(prop(GATEWAY,'ActiveState')=='inactive','Gateway failed to quiesce')
        quiet=inspector()(LEDGER);disarmed();rc_safe()
        # Re-evaluate the stationary HOME immediately before stopping owner.
        nav=get('/api/externalnav');h=stock_home(nav)
        require(within_home(h,ctx['evidence']),'HOME changed after preflight')
        owner_stopped=True;run('systemctl','stop',OWNER);clean_socket()
        stopped=inspector()(LEDGER);require(stopped['logical_sha256']==quiet['logical_sha256'],'Ledger changed after quiescence')
        before=dump(LEDGER)
        with closing(sqlite3.connect(LEDGER.absolute().as_uri()+'?mode=ro',uri=True)) as src:
            with closing(sqlite3.connect(backup/'disarm.sqlite3')) as dst:src.backup(dst)
        require(dump(backup/'disarm.sqlite3')==before,'Backup differs')
        (backup/'ledger.logical.sql').write_text(before)
        rootdir(GRANT.parent,0o755)
        grant=dict(camera_mount_verified=True,mapped_markers_verified=True,boot_id=BOOT.read_text().strip(),sha256=ctx['assets'],
                   reviewed_evidence_sha256=hashlib.sha256(json.dumps(ctx['evidence'],sort_keys=True).encode()).hexdigest(),
                   verified_at_utc=datetime.now(timezone.utc).isoformat())
        atomic(TX_DROP,ctx['tx_drop'].encode())
        atomic(GRANT,(json.dumps(grant,indent=2)+'\n').encode())
        run('systemctl','daemon-reload');run('systemctl','start',OWNER)
        nav=wait_owner(True)
        require(dump(LEDGER)==before,'Ledger/audit/replay history changed at restart')
        report['ledger_preserved_at_restart']=True
        run('systemctl','start',GATEWAY)
        require(prop(AUTH,'MainPID')==ctx['auth_pid'],'Auth unexpectedly restarted')
        for u in (OWNER,GATEWAY,AUTH):require(prop(u,'ActiveState')=='active','Service inactive')
        report.update(status='STOCK_EXTERNALNAV_TX_ACTIVE',externalnav=nav,owner_pid=sole_owner());save()
        print(json.dumps(report,indent=2))
    except BaseException as error:
        # Removing the grant is always first: no new optical write is authorized.
        remove_grant()
        report.update(status='ROLLBACK_STARTED',error=str(error));save()
        try:
            if owner_stopped:
                run('systemctl','stop',GATEWAY,OWNER)
                if TX_DROP.exists():immutable(TX_DROP);TX_DROP.unlink()
                run('systemctl','daemon-reload');clean_socket();run('systemctl','start',OWNER)
                wait_owner(False)
            if gateway_stopped:run('systemctl','start',GATEWAY)
            report['status']='ROLLED_BACK_TO_STOCK_OBSERVE_ONLY'
        except BaseException as rollback_error:report.update(status='ROLLBACK_REQUIRES_OPERATOR',rollback_error=str(rollback_error))
        save();print(json.dumps(report,indent=2));raise


def block_grant():
    # Revoke immediately even if another helper currently holds the cutover lock.
    rootdir(GRANT.parent,0o755);moved=None
    if GRANT.exists():
        immutable(GRANT);require(GRANT.is_file(),'Alignment grant must be a regular file')
        backup=GRANT.with_name('alignment-disabled-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        os.link(GRANT,backup,follow_symlinks=False);moved=str(backup)
    else:require(not GRANT.is_symlink(),'Unexpected alignment symlink')
    tmp=GRANT.with_name('alignment-deny.new-'+str(os.getpid()))
    with open(tmp,'xb') as f:f.write(b'{"camera_mount_verified":false,"mapped_markers_verified":false,"disabled":true}\n');f.flush();os.fsync(f.fileno())
    os.chown(tmp,0,0);tmp.chmod(0o644);os.replace(tmp,GRANT)
    return moved


def disable():
    moved=block_grant()  # Atomic denial FIRST; a concurrent enable cannot replace it.
    if TX_DROP.exists():immutable(TX_DROP);TX_DROP.unlink();run('systemctl','daemon-reload')
    # Existing --tx process remains guarded OFF; next restart is observe-only.
    time.sleep(.3);a=get('/api/externalnav');time.sleep(.4);b=get('/api/externalnav')
    require(b.get('alignment_verified') is False and b.get('sent')==a.get('sent'),'TX did not stop after grant removal')
    print(json.dumps(dict(status='EXTERNALNAV_GUARD_BLOCKED',alignment_backup=moved,sent=b.get('sent'),
                         current_process_tx_flag=b.get('tx_enabled'),restart_profile='OBSERVE_ONLY',services_restarted=False),indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__);m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--check',action='store_true');m.add_argument('--enable',action='store_true');m.add_argument('--disable',action='store_true')
    p.add_argument('--home-evidence',type=Path)
    p.add_argument('--physical-map-restored',action='store_true');p.add_argument('--camera-mount-restored',action='store_true')
    p.add_argument('--elrs-disarm-neutral',action='store_true');a=p.parse_args()
    require(os.geteuid()==0,'Operator sudo required for fixed owner helper and system changes')
    if a.disable:
        disable();return
    if a.check:
        ctx=preflight(a.home_evidence)
        print(json.dumps(dict(status='STOCK_TX_PREFLIGHT_PASS',home=ctx['home'],ledger=ctx['ledger'],services_changed=False),indent=2));return
    require(a.physical_map_restored and a.camera_mount_restored and a.elrs_disarm_neutral,
            'Explicit physical map/camera/ELRS DISARM-neutral confirmations required')
    os.umask(0o077)
    with open('/run/ugv-disarm-cutover.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);enable(preflight(a.home_evidence))


if __name__=='__main__':main()
