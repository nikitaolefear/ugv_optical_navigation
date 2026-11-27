"""Render a B2-FTP systemd drop-in from the installed B2 reference."""
import shlex
import sys
from pathlib import Path
import re


IDENTITIES = {'--web-disarm-client-uid': 'UGV_GATEWAY_UID',
              '--web-disarm-client-gid': 'UGV_OWNER_RPC_GID'}


def resolve_identities(argv, identities):
    resolved = list(argv)
    for flag, variable in IDENTITIES.items():
        if resolved.count(flag) != 1:
            raise ValueError('Ambiguous Owner credentials')
        index = resolved.index(flag) + 1
        if resolved[index] == '${' + variable + '}':
            resolved[index] = identities[variable]
        if not re.fullmatch('[0-9]+', resolved[index]):
            raise ValueError('Invalid numeric Owner credential')
    return resolved


def render(text, base, overlay, marker):
    lines = text.splitlines()
    prefix = 'ExecStart='
    starts = [line[len(prefix):] for line in lines if line.startswith(prefix) and line != prefix]
    if (len(starts) != 1 or lines.count(prefix) != 1
            or f'WorkingDirectory={base}/owner' not in lines
            or f'Environment=PYTHONPATH={base}/owner:{base}/optical:{base}' not in lines):
        raise ValueError('Unrecognized B2 reference')
    argv = shlex.split(starts[0])
    if (argv[1:4] != ['-u', '-m', 'stop_latch_test.hold_runner']
            or '--stage3-ftp-approval-file' in argv or '%' in starts[0]
            or argv[argv.index('--stage3-operator-mode') + 1] != 'B2'):
        raise ValueError('Unexpected B2 command')
    safe_argv = resolve_identities(argv, dict.fromkeys(IDENTITIES.values(), '1'))
    if any('$' in item for item in safe_argv):
        raise ValueError('Unexpected systemd substitution')
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+', p) for p in (base, overlay, marker)):
        raise ValueError('Unsafe systemd path')
    return (f'[Service]\nWorkingDirectory={overlay}/owner\n'
            f'Environment=PYTHONPATH={overlay}/owner:{overlay}/optical:{overlay}\n'
            f'{prefix}\n{prefix}{starts[0]} --stage3-ftp-approval-file {marker}\n')


if __name__ == '__main__':
    reference, base, overlay, marker = sys.argv[1:]
    print(render(Path(reference).read_text(), base, overlay, marker), end='')
