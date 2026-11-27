"""Operator client for the fixed B2-LOG IPC."""
import argparse
import json
import time
import uuid

from disarm_control.ipc import OwnerClient, SocketPolicy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status','discover','begin','restore','recover'))
    parser.add_argument('--socket', required=True)
    parser.add_argument('--owner-uid', type=int, required=True)
    parser.add_argument('--socket-gid', type=int, required=True)
    parser.add_argument('--transaction')
    args = parser.parse_args()
    if bool(args.transaction) != (args.action in ('restore','recover')):
        parser.error('transaction ID required only for restore/recover')
    client = OwnerClient(args.socket, policy=SocketPolicy(args.owner_uid,args.socket_gid,0o710,0o660))

    def call(op, payload):
        result = client.call(op, {'id':1,'username':'stage3-log'}, uuid.uuid4().hex,payload=payload)
        if result.get('ok') is not True:
            raise RuntimeError(result.get('error','request refused'))
        return result['command']

    if args.action == 'status':
        print(json.dumps(call('S3_LOG_STATUS', {})))
        return
    call('S3_LOG_'+args.action.upper(), {'transaction':args.transaction} if args.transaction else {})
    deadline=time.monotonic()+(68 if args.action == "discover" else 12)
    while time.monotonic()<deadline:
        result=call('S3_LOG_RESULT',{})
        if not result['active']:
            print(json.dumps(result['result']))
            if 'error' in result['result']:
                raise SystemExit(1)
            return
        time.sleep(.15)
    raise TimeoutError('Inspect persistent journal before any further request')


if __name__ == '__main__':
    main()
