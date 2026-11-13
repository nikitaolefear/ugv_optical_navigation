"""Read-only status by default; explicit stop targets only the verified 8083 master.
This tool never starts services and never reads the private database or auth key.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import time

SOURCE=Path('/home/ugvs2/ugv-hmi-auth-password8-release')
STATE='/home/ugvs2/.local/state/ugv-hmi'


def listeners():
    result={}
    for table in ('tcp','tcp6'):
        for line in Path('/proc/net/'+table).read_text().splitlines()[1:]:
            c=line.split();port=int(c[1].rsplit(':',1)[1],16)
            if port in (8082,8083) and c[3]=='0A':
                if port==8083 and c[1]!='0100007F:1F93':
                    raise RuntimeError('8083 is not exclusively IPv4 loopback')
                result['socket:['+c[9]+']']=port
    return result


def identify():
    sockets=listeners();owners={};production=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            ports={sockets[value] for fd in (proc/'fd').iterdir() if (value:=os.readlink(fd)) in sockets}
            if not ports:continue
            status=(proc/'status').read_text().splitlines()
            ppid=int(next(s.split()[1] for s in status if s.startswith('PPid:')))
            uid=int(next(s.split()[1] for s in status if s.startswith('Uid:')))
            if uid!=os.getuid():
                if 8083 in ports:raise RuntimeError('Unexpected 8083 process owner')
                continue
            env=dict(v.split(b'=',1) for v in (proc/'environ').read_bytes().split(b'\0') if b'=' in v)
            state=env.get(b'UGV_HMI_STATE_DIR')
            if 8082 in ports and state==STATE.encode():production.append(int(proc.name))
            if 8083 not in ports:continue
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if (8082 in ports or (proc/'cwd').resolve()!=SOURCE or
                    b'/home/ugvs2/.venv-ugv-hmi-auth/bin/gunicorn' not in args or
                    b'127.0.0.1:8083' not in args or
                    env.get(b'UGV_HMI_TELEMETRY')!=b'0' or
                    env.get(b'UGV_HMI_TRUST_PROXY')!=b'1' or
                    env.get(b'UGV_HMI_PUBLIC_ORIGIN')!=b'https://192.168.110.59' or state!=STATE.encode()):
                raise RuntimeError('8083 identity/configuration mismatch; no signal sent')
            owners[int(proc.name)]=ppid
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError:
            # Refuse deployment if relevant socket ownership cannot be established.
            continue
    if not sockets:return None,[]
    master=[pid for pid,ppid in owners.items() if ppid not in owners]
    has_test=8083 in sockets.values()
    if has_test and (not owners or len(master)!=1):
        raise RuntimeError('No unique verified 8083 master')
    if production:
        raise RuntimeError('8082 shares this state; migration is forbidden while it is running')
    return (master[0] if has_test else None),sorted(owners)


def process_alive(pid):
    try:
        return not any(line.startswith('State:') and 'Z' in line.split()[1]
                       for line in Path('/proc',str(pid),'status').read_text().splitlines())
    except FileNotFoundError:
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','stop'),default='status',nargs='?')
    args=parser.parse_args()
    if os.getuid()==0:raise RuntimeError('Run as ugvs2, not root')
    master,owners=identify()
    print(json.dumps({'master':master,'owners':owners,'port':8083}))
    if args.action=='stop' and master is not None:
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise RuntimeError('pidfd support required; no signal sent')
        pidfd=os.pidfd_open(master)
        try:
            verified,_=identify()
            if verified!=master:
                raise RuntimeError('Master identity changed; no signal sent')
            signal.pidfd_send_signal(pidfd,signal.SIGTERM)
        finally:
            os.close(pidfd)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if 8083 not in listeners().values() and not any(process_alive(pid) for pid in owners):
                print('8083 stopped');return
            time.sleep(.2)
        raise RuntimeError('8083 still listening; do not migrate or send additional signals')

if __name__=='__main__':
    try:main()
    except (OSError,RuntimeError):
        raise SystemExit('Cannot verify/stop only the test HMI; no further action permitted.')
