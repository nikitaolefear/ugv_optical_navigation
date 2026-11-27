"""Client for the candidate IPC. The CLI is offline-only pending hardware gates."""
import argparse
from pathlib import Path
import uuid

from disarm_control.stage3 import Refused


class Stage3Client:
    def __init__(self, channel, actor):
        self.channel = channel
        self.actor = actor
        self.token = None
        self.sequence = 0

    def _call(self, op, **fields):
        payload = dict(fields)
        if op != 'S3_OPEN':
            payload['token'] = self.token
        result = self.channel.call(op, self.actor, uuid.uuid4().hex, payload=payload)
        if result.get('ok') is not True or type(result.get('command')) is not dict:
            raise Refused(result.get('error', 'INVALID_REPLY'))
        return result['command']

    def open(self):
        reply = self._call('S3_OPEN')
        self.token = reply['token']
        return reply

    def call(self, operation, **fields):
        if operation == 'S3_RENEW':
            self.sequence += 1
            fields['seq'] = self.sequence
        return self._call(operation, **fields)


def exercise(client, clock, preflight, observe, *, switch_approved=False):
    """Three phases over authorized IPC; callbacks supply owner-side evidence."""
    baseline = client.open()['baseline']
    report = {'phases': [], 'rollback': 'FAIL', 'status': 'INCONCLUSIVE'}
    try:
        preflight(client, baseline)
        client.call('S3_START')
        t0 = clock.monotonic()
        for phase, start, end in [('A', 0, 30), ('B', 30, 90), ('C', 90, 180)]:
            switched = False
            while clock.monotonic() < t0 + end:
                snapshot = client.call('S3_RENEW', phase=phase)
                if snapshot['stopped']:
                    raise Refused('OWNER_STOPPED')
                if phase == 'C' and baseline['source'] != 2 and not switched:
                    if not switch_approved:
                        raise Refused('SRC2_NOT_APPROVED')
                    client.call('S3_SRC2')
                    switched = True
                observe(phase, snapshot)
                clock.sleep(min(.1, max(0, t0 + end - clock.monotonic())))
            report['phases'].append(phase)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status='FAIL', error=str(exc))
    finally:
        try:
            receipt = client.call('S3_STOP')
            for _ in range(20):
                if receipt['tx_barrier'] and receipt['driver_empty']:
                    break
                clock.sleep(.05)
                receipt = client.call('S3_STATUS')
            else:
                raise Refused('TX_STOP_UNCONFIRMED')
            client.call('S3_RESTORE')
            for _ in range(100):
                receipt = client.call('S3_STATUS')
                if receipt['restore_failed']:
                    raise Refused('READBACK_RESTORE_FAILED')
                if receipt['restored']:
                    report['rollback'] = 'PASS'
                    break
                clock.sleep(.05)
            else:
                raise Refused('RESTORE_TIMEOUT')
        except (Exception, KeyboardInterrupt) as exc:
            report.update(status='FAIL', rollback_error=str(exc))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'preflight', 'retest'))
    args = parser.parse_args()
    if args.action != 'check':
        parser.exit(2, 'LIVE_DISABLED: independently proven SRC and recording integration required\n')
    print(f'Candidate only: {Path(__file__).resolve()}')
