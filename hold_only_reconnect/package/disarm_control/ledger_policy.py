"""Read-only restart admission for the existing DISARM ledger; never repairs data."""
from collections import Counter
import hashlib
import math
from pathlib import Path
import sqlite3

# _retire() retains SENT and clears engine.active for these reasons.
TERMINAL_SENT_REASONS = (
    'DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF', 'OBSERVATION_TIMEOUT',
    'FEATURE_DISABLED', 'CLOCK_FAULT', 'AUTOPILOT_CHANGED', 'LINK_UNAVAILABLE',
    'HEARTBEAT_STALE', 'UNTRUSTED_HEARTBEAT',
)
TERMINAL_REJECTED_REASONS = TERMINAL_SENT_REASONS + ('FEATURE_DISABLED_NO_SEND',)
TERMINAL_UNKNOWN_REASONS = TERMINAL_SENT_REASONS + ('SEND_UNCERTAIN_NO_RETRY', 'OWNER_RESTARTED')


class LedgerNotIdle(RuntimeError):
    pass


def number(v):
    return type(v) in (int, float) and math.isfinite(v)


def inspect_ledger(path):
    """Consistent query-only snapshot, including all tables in logical digest.

    No status RPC is used: status may update engine state. Terminal uncertainty
    is never interpreted as command execution, and no UUID is replayed.
    """
    with sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if [tuple(r) for r in db.execute('PRAGMA integrity_check')] != [('ok',)]:
            raise LedgerNotIdle('DISARM ledger integrity failure')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise LedgerNotIdle('DISARM ledger foreign-key failure')
        rows = db.execute('SELECT * FROM commands ORDER BY id').fetchall()
        evidence = {r['id']: r for r in db.execute('SELECT * FROM delivery_evidence')}
        if set(evidence) != {r['id'] for r in rows}:
            raise LedgerNotIdle('Missing/orphan delivery evidence; manual review required')
        counts = Counter()
        uncertain = 0
        for r in rows:
            ident, state, reason = r['id'], r['state'], r['reason']
            e = evidence[ident]
            counts[state] += 1
            if (not number(r['created']) or not number(r['updated']) or r['updated'] < r['created']
                    or type(r['actor_id']) is not int or r['actor_id'] <= 0):
                raise LedgerNotIdle('Invalid command metadata: '+str(ident))
            if (not isinstance(e['owner_epoch'], str) or not e['owner_epoch']
                    or type(e['target_system']) is not int or not 1 <= e['target_system'] <= 255
                    or type(e['target_component']) is not int or e['target_component'] != 1):
                raise LedgerNotIdle('Invalid delivery identity: '+str(ident))
            write = e['write_state']
            if write == 'FULL_WRITE':
                if (not number(e['sent_at']) or not r['created'] <= e['sent_at'] <= r['updated']
                        or type(e['sent_serial']) is not int or e['sent_serial'] < 0):
                    raise LedgerNotIdle('Invalid full-write evidence: '+str(ident))
            elif write in ('NOT_ATTEMPTED', 'ATTEMPTED'):
                if e['sent_at'] is not None or e['sent_serial'] is not None:
                    raise LedgerNotIdle('Inconsistent unsent/uncertain evidence: '+str(ident))
            else:
                raise LedgerNotIdle('Unknown write state: '+str(ident))
            if state in ('QUEUED', 'SEND_ATTEMPT', 'WAITING'):
                raise LedgerNotIdle('DISARM transaction pending '+str(ident)+': '+state)
            if state == 'SENT':
                if reason not in TERMINAL_SENT_REASONS or write != 'FULL_WRITE':
                    raise LedgerNotIdle('SENT still observing or unreviewed: '+str(ident))
            elif state == 'REJECTED':
                if reason not in TERMINAL_REJECTED_REASONS or write != 'NOT_ATTEMPTED':
                    raise LedgerNotIdle('Unreviewed rejection: '+str(ident))
            elif state == 'UNKNOWN':
                if reason not in TERMINAL_UNKNOWN_REASONS:
                    raise LedgerNotIdle('Unreviewed uncertainty: '+str(ident))
                if reason == 'SEND_UNCERTAIN_NO_RETRY' and write != 'ATTEMPTED':
                    raise LedgerNotIdle('Inconsistent uncertain write: '+str(ident))
                uncertain += 1
            else:
                raise LedgerNotIdle('Unsupported historical state: '+str(ident)+' '+str(state))
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode('utf-8')+b'\n')
        tables = {r[0]: db.execute('SELECT count(*) FROM "'+r[0].replace('"','""')+'"').fetchone()[0]
                  for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return dict(status='LEDGER_IDLE', command_counts=dict(counts), terminal_uncertain=uncertain,
                    logical_sha256=digest.hexdigest(), table_counts=tables)
