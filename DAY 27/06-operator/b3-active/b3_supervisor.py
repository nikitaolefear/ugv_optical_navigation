"""Independent local timing for a human-approved test-client termination."""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import time


def terminate(active):
    if (active.get('event') != 'active' or not re.fullmatch('[a-f0-9]{32}', active['lease'])
            or len(active['peer']) != 3):
        raise ValueError('invalid active test client evidence')
    uid,pid,generation=active['peer']
    if uid != os.getuid() or pid <= 1:
        raise PermissionError('must run as the same unprivileged test client UID')
    fd=os.pidfd_open(pid)
    try:
        process=Path('/proc') / str(pid)
        if process.stat().st_uid != uid:
            raise PermissionError('peer UID changed')
        info=(process/'stat').read_text().rsplit(')',1)[1].split()
        argv=(process/'cmdline').read_bytes().split(b'\0')
        if (int(info[19])!=generation or info[0]=='Z'
                or not any(a.endswith(b'/b3_client.py') for a in argv)
                or b'client_exit' not in argv or active['lease'].encode() not in argv):
            raise ValueError('peer does not identify authorized test client')
        poll=select.poll()
        poll.register(fd,select.POLLIN)
        if poll.poll(0):
            raise ValueError('test client exited before supervisor action')
        before=time.monotonic_ns()
        signal.pidfd_send_signal(fd,signal.SIGTERM)
        if not poll.poll(1000):
            raise TimeoutError('test client did not exit')
        return dict(event='supervisor_pidfd_exit',lease=active['lease'],peer=active['peer'],
                    before_ns=before,exit_observed_ns=time.monotonic_ns(),signal_sent=True)
    finally:
        os.close(fd)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('active_event_json')
    a=p.parse_args()
    print(json.dumps(terminate(json.loads(a.active_event_json))))
