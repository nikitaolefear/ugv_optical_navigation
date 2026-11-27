"""Future operator CLI. Uses only the checked Owner Unix socket."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from disarm_control.ftp_backup import MAX_FILES, MAX_TIME, MAX_TOTAL
from disarm_control.ipc import OwnerClient, SocketPolicy
from disarm_control.scripts_ftp import ardupilot_crc, confined_path


def sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, contents):
    with path.open('xb') as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())


def backup(call, output):
    parent = output.parent
    info = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('Use a private operator-owned parent on the persistent Pi disk')
    if shutil.disk_usage(parent).free < MAX_TOTAL + 1024 * 1024:
        raise OSError('Insufficient local free space')
    output.mkdir(mode=0o700)
    output = output.resolve()
    write_private(output / 'INCOMPLETE', b'Backup pending verification\n')
    sync_dir(parent)
    sync_dir(output)
    files = output / 'scripts'
    files.mkdir(mode=0o700)
    call('S3_FTP_START', {'budget': MAX_TOTAL})
    completed = False
    try:
        end = time.monotonic() + MAX_TIME + 5
        while time.monotonic() < end:
            result = call('S3_FTP_RESULT', {})
            if result['status'] != 'BUSY':
                break
            time.sleep(.2)
        else:
            raise TimeoutError('Owner FTP operation timed out')
        if result['status'] != 'COMPLETE' or result['unresolved'] or not 0 <= result['count'] <= MAX_FILES:
            raise RuntimeError(f'Backup incomplete: {result}')
        manifest = []
        total = 0
        for index in range(result['count']):
            info = call('S3_FTP_FILE', {'index': index})
            parts = tuple(info['parts'])
            confined_path(parts)
            size = info['size']
            total += size
            if not parts or not 0 <= size <= MAX_TOTAL or total > MAX_TOTAL:
                raise ValueError('Invalid file metadata')
            data = bytearray()
            while len(data) < size:
                chunk = call('S3_FTP_CHUNK', {'index': index, 'offset': len(data)})
                block = base64.b64decode(chunk['base64'], validate=True)
                if chunk['offset'] != len(data) or not block or len(data) + len(block) > size:
                    raise ValueError('Invalid chunk')
                data.extend(block)
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('SHA256 mismatch')
            if info['crc32'] is not None and ardupilot_crc(data) != info['crc32']:
                raise ValueError('CRC32 mismatch')
            path = files.joinpath(*parts)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(path, data)
            manifest.append(info)
        for directory, _, _ in os.walk(files, topdown=False):
            sync_dir(directory)
        write_private(output / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
        write_private(output / 'SHA256SUMS', ''.join(
            f"{f['sha256']}  scripts/{'/'.join(f['parts'])}\n" for f in manifest).encode())
        completed = True
    finally:
        try:
            call('S3_FTP_RELEASE', {})
        except (OSError, RuntimeError):
            if completed:
                raise
    if completed:
        (output / 'INCOMPLETE').unlink()
        sync_dir(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'backup'))
    parser.add_argument('--socket', required=True)
    parser.add_argument('--owner-uid', required=True, type=int)
    parser.add_argument('--socket-gid', required=True, type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    client = OwnerClient(args.socket, policy=SocketPolicy(args.owner_uid, args.socket_gid, 0o710, 0o660))

    def call(op, payload):
        reply = client.call(op, {'id': 1, 'username': 'stage3-ftp'}, uuid.uuid4().hex, payload=payload)
        if reply.get('ok') is not True:
            raise RuntimeError(reply.get('error', 'Owner refused'))
        return reply['command']

    if args.action == 'status':
        print(json.dumps(call('S3_BENCH_STATUS', {})))
    elif args.output is None:
        parser.error('backup needs an output path')
    else:
        backup(call, args.output)


if __name__ == '__main__':
    main()
