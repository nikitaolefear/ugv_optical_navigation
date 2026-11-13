"""Unprivileged, opt-in adapter. Import never executes the privileged helper."""
import json
import os
import re
import stat
import subprocess

try:
    from .pixhawk_telemetry import PORT, PortFailure, LOG, open_passive, port_stage
except ImportError:
    from pixhawk_telemetry import PORT, PortFailure, LOG, open_passive, port_stage

COMMAND = ('/usr/bin/sudo', '-n', '--', '/usr/local/libexec/ugv-pixhawk-owners')


def identity(info, target):
    return dict(target=target, dev=info.st_dev, ino=info.st_ino, rdev=info.st_rdev)


def validate(result):
    if result.stderr.strip() or len(result.stdout) > 65536:
        raise ValueError('Helper stderr or oversized output')
    body = json.loads(result.stdout)
    if not isinstance(body, dict) or type(body.get('schema')) is not int or body.get('schema') != 1 or body.get('port') != PORT:
        raise ValueError('Wrong helper schema/device')
    if body.get('complete') is not True or result.returncode != 0 or body.get('error') is not None:
        raise ValueError('Owner check incomplete: ' + repr(body.get('error')))
    owners, device = body.get('owners'), body.get('identity')
    if not isinstance(owners, list) or any(type(pid) is not int or pid <= 0 for pid in owners):
        raise ValueError('Invalid owner PIDs')
    if owners != sorted(set(owners)):
        raise ValueError('Duplicate/unsorted owner PIDs')
    if not isinstance(device, dict) or set(device) != {'target', 'dev', 'ino', 'rdev'}:
        raise ValueError('Invalid device identity')
    if not isinstance(device['target'], str) or re.fullmatch(r'/dev/ttyACM[0-9]+', device['target']) is None:
        raise ValueError('Invalid device target')
    if any(type(device[k]) is not int or device[k] < 0 for k in ('dev', 'ino', 'rdev')):
        raise ValueError('Invalid device identity numbers')
    return body


class OwnerHelper:
    """One instance per open attempt: binds both snapshots to the same device."""
    def __init__(self):
        self.before = None

    def __call__(self, port, stage, fd=None):
        if port != PORT or stage not in ('fuser_before', 'fuser_after'):
            raise PortFailure('PORT_CHECK_FAILED', stage, port, 'Only fixed Pixhawk is allowed')
        result = None
        try:
            result = subprocess.run(COMMAND, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=3, shell=False, close_fds=True, cwd='/', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            LOG.info('owner_helper stage=%s hmi_pid=%s hmi_uid=%s path=%r rc=%s stdout=%r stderr=%r',
                     stage, os.getpid(), os.getuid(), port, result.returncode, result.stdout, result.stderr)
            body = validate(result)
            device = body['identity']
            # Prefer a real known owner over an identity mismatch in classification.
            foreign = set(body['owners']) - {os.getpid()}
            if foreign or (fd is None and body['owners']):
                raise PortFailure('PORT_BUSY', stage, port, 'Confirmed device owner',
                                  owners=body['owners'], result=result)
            with port_stage('tty_identity', port):
                info = os.stat(PORT) if fd is None else os.fstat(fd)
                local_identity = identity(info, os.path.realpath(PORT))
            if not stat.S_ISCHR(info.st_mode) or local_identity != device:
                raise ValueError('Helper identity does not match local tty/descriptor')
            if fd is None:
                self.before = device
            elif self.before != device:
                raise ValueError('Device changed between ownership checks')
            # Compatibility with current checked_owners(), including own-PID check.
            return subprocess.CompletedProcess(COMMAND, 0 if body['owners'] else 1,
                stdout=' '.join(map(str, body['owners'])).encode(), stderr=b'')
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
            raise PortFailure('PORT_CHECK_FAILED', stage, port, str(exc), result=result) from exc


def open_with_owner_helper():
    # Requires the supplied integration patch; no fallback if unavailable.
    return open_passive(owner_check=OwnerHelper())
