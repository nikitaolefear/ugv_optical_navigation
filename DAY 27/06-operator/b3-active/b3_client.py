"""Operator IPC only. Each active case needs a distinct pre-approved nonce."""
import argparse
import json
import time
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
    call('OPEN',{'scenario':args.scenario,'nonce':args.nonce})
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        time.sleep(.15)
        call('RENEW',{})
    if args.scenario=='revoke':
        print(json.dumps({'client_revoke_ns':time.monotonic_ns()}),flush=True)
        call('REVOKE',{})
    elif args.scenario=='expiry':
        time.sleep(1.1)  # Keep peer alive while deliberately not renewing.
    else:
        raise SystemExit('Supervisor did not terminate test client in time')


if __name__=='__main__':
    main()
