#!/usr/bin/python3 -I
"""Install root-owned; no arguments, stdin, environment configuration or commands.

Only stats the fixed Pixhawk and /proc descriptors. Never opens the tty.
"""
import errno
import json
import os
import re
import signal
import stat
import sys

PORT = '/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if02'
SCHEMA = 1


class CheckError(Exception):
    def __init__(self, code, path, message, number=None):
        super().__init__(message)
        self.code, self.path, self.number = code, path, number


def device_identity():
    for path in ('/dev', '/dev/serial', '/dev/serial/by-id'):
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise CheckError('UNTRUSTED_DEVICE_PATH', path, 'Expected root-owned non-writable directory')
    link = os.lstat(PORT)
    if not stat.S_ISLNK(link.st_mode) or link.st_uid != 0:
        raise CheckError('UNTRUSTED_DEVICE_PATH', PORT, 'Expected root-owned device symlink')
    target = os.path.realpath(PORT)
    if re.fullmatch(r'/dev/ttyACM[0-9]+', target) is None:
        raise CheckError('UNEXPECTED_DEVICE', PORT, 'Target must be /dev/ttyACM<number>')
    info = os.lstat(target)
    if not stat.S_ISCHR(info.st_mode) or info.st_uid != 0:
        raise CheckError('UNEXPECTED_DEVICE', target, 'Expected root-owned character device')
    return dict(target=target, dev=info.st_dev, ino=info.st_ino, rdev=info.st_rdev)


def scan_owners(identity):
    owners = set()
    with os.scandir('/proc') as processes:
        for process in processes:
            if not process.name.isdigit():
                continue
            path = process.path + '/fd'
            try:
                descriptors = os.listdir(path)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ESRCH):
                    continue
                raise
            for descriptor in descriptors:
                try:
                    info = os.stat(path + '/' + descriptor)
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ESRCH):
                        continue  # descriptor or process disappeared
                    raise
                if stat.S_ISCHR(info.st_mode) and info.st_rdev == identity['rdev']:
                    owners.add(int(process.name))
    return sorted(owners)


def collect():
    """No partial owner list is ever reported as a completed check."""
    try:
        before = device_identity()
        owners = scan_owners(before)
        if device_identity() != before:
            raise CheckError('DEVICE_CHANGED', PORT, 'Device identity changed during scan')
        return dict(schema=SCHEMA, port=PORT, complete=True, identity=before, owners=owners, error=None)
    except (OSError, CheckError) as exc:
        error = dict(code=exc.code if isinstance(exc, CheckError) else type(exc).__name__,
                     path=exc.path if isinstance(exc, CheckError) else exc.filename,
                     errno=exc.number if isinstance(exc, CheckError) else exc.errno,
                     message=str(exc))
        return dict(schema=SCHEMA, port=PORT, complete=False, identity=None, owners=[], error=error)


def main():
    try:
        if len(sys.argv) != 1:
            raise CheckError('ARGUMENTS_FORBIDDEN', None, 'This command accepts no arguments')
        if os.geteuid() != 0:
            raise CheckError('ROOT_REQUIRED', None, 'Run only through the fixed sudoers rule', errno.EPERM)
        def timeout(signum, frame):
            raise CheckError('SCAN_TIMEOUT', '/proc', 'Owner scan exceeded 2 seconds', errno.ETIMEDOUT)
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(2)
        try:
            result = collect()
        finally:
            signal.alarm(0)
    except CheckError as exc:
        result = dict(schema=SCHEMA, port=PORT, complete=False, identity=None, owners=[],
                      error=dict(code=exc.code, path=exc.path, errno=exc.number, message=str(exc)))
    print(json.dumps(result, sort_keys=True))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
