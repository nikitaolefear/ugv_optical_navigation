"""Operator IPC only. Each active case needs a distinct pre-approved nonce."""
import argparse
import json
import time
import os
from pathlib import Path
import uuid

from disarm_control.ipc import OwnerClient, SocketPolicy


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('status','trace','run'))
    p.add_argument('--socket', required=True)
    p.add_argument('--owner-uid', required=True,type=int)
    p.add_argument('--socket-gid', required=True,type=int)
    p.add_argument('--scenario', choices=('revoke','client_exit','expiry'))
    p.add_argument('--nonce')
    p.add_argument('--await-approval', action='store_true')
    args=p.parse_args()
    if args.action=='run' and not (args.scenario and args.nonce):
        p.error('run requires scenario and separately authorized nonce')
    client=OwnerClient(args.socket,policy=SocketPolicy(args.owner_uid,args.socket_gid,0o710,0o660))

    def call(action,payload):
        r=client.call('S3_B3_'+action,{'id':1,'username':'stage3-b3'},uuid.uuid4().hex,payload=payload)
        if r.get('ok') is not True:
            raise RuntimeError(r.get('error','B3 IPC refused'))
        return r['command']

    if args.action=='status':
        print(json.dumps(call('STATUS',{})))
        return
    if args.action=='trace':
        status=call('STATUS',{})
        if status['active']:
            raise RuntimeError('wait for gate close')
        for index in range(status['count']):
            print(json.dumps(call('ROW',{'index':index})))
        return
    generation = int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
    peer = [os.getuid(), os.getpid(), generation]
    if args.await_approval:
        print(json.dumps(dict(event='await_approval', lease=args.nonce, peer=peer)),flush=True)
        marker=Path('/run/ugv-stage3-operator/log-profile/b3-authorization')
        tail=f" {peer[1]} {peer[2]} {args.scenario} {args.nonce}\n"
        timeout=time.monotonic()+30
        while True:
            try:
                if marker.read_text().endswith(tail):
                    break
            except FileNotFoundError:
                pass
            if time.monotonic()>=timeout:
                raise TimeoutError('approval not observed')
            time.sleep(.05)
    call('OPEN',{'scenario':args.scenario,'nonce':args.nonce})
    wait_deadline=time.monotonic()+.5
    while call('STATUS',{})['start_ns'] is None:
        if time.monotonic()>=wait_deadline:
            raise TimeoutError('lease never active')
        time.sleep(.02)
    print(json.dumps(dict(event='active',peer=peer,lease=args.nonce)),flush=True)
    deadline=time.monotonic()+1
    while time.monotonic()<deadline:
        time.sleep(.15)
        call('RENEW',{})
    if args.scenario=='revoke':
        start=time.monotonic_ns()
        call('REVOKE',{})
        print(json.dumps(dict(event='client_revoke',before_ns=start,
                              after_ns=time.monotonic_ns(),lease=args.nonce,peer=peer)),flush=True)
    elif args.scenario=='expiry':
        time.sleep(1.1)  # Keep peer alive while deliberately not renewing.
    else:
        # Remain alive if the supervisor misses its window. Expiry is then visible.
        time.sleep(2)


if __name__=='__main__':
    main()
