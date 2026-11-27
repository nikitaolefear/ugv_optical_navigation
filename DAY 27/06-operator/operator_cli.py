"""Future operator client. Uses the existing checked owner IPC."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time
import uuid

from disarm_control.ipc import OwnerClient, SocketPolicy


def request(client, kind, *, extra=None, sleep=time.sleep, timeout=40):
    client('S3_BENCH_REQUEST', dict(kind=kind, **(extra or {})))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        poll = client('S3_BENCH_RESULT', {})
        if not poll['busy']:
            result = poll['result']
            if not result or result.get('reason', 'COMPLETE') != 'COMPLETE':
                raise RuntimeError('operator request incomplete')
            return result
        sleep(.15)
    raise TimeoutError('operator request timed out; inspect owner before retrying')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status','catalog','prefix','log_disarmed','timesync','scr_enable','scr_heap_size','memory'])
    parser.add_argument('--socket', required=True)
    parser.add_argument('--owner-uid', type=int, required=True)
    parser.add_argument('--socket-gid', type=int, required=True)
    parser.add_argument('--id', type=int)
    parser.add_argument('--size', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    port = OwnerClient(args.socket, policy=SocketPolicy(args.owner_uid, args.socket_gid, 0o710, 0o660))
    def call(operation, payload):
        result = port.call(operation, {'id': 1, 'username': 'stage3-bench'}, uuid.uuid4().hex, payload=payload)
        if result.get('ok') is not True:
            raise RuntimeError(result.get('error','operator request refused'))
        return result['command']
    if args.action == 'status':
        result = call('S3_BENCH_STATUS', {})
    elif args.action == 'prefix':
        if args.id is None or args.size is None or args.output is None:
            parser.error('prefix requires --id, --size, --output')
        result = request(call,'prefix',extra={'id':args.id,'size':args.size})
        data = bytearray()
        while len(data) < result['bytes_received']:
            chunk = call('S3_BENCH_CHUNK', {'offset':len(data)})
            if chunk['offset'] != len(data):
                raise RuntimeError('wrong prefix offset')
            block = base64.b64decode(chunk['base64'], validate=True)
            if not block or len(data)+len(block) > result['bytes_received']:
                raise RuntimeError('invalid prefix length')
            data.extend(block)
        if hashlib.sha256(data).hexdigest() != result['sha256']:
            raise RuntimeError('prefix SHA256 mismatch')
        with args.output.open('xb') as handle:
            handle.write(data)
    else:
        result = request(call,args.action)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
