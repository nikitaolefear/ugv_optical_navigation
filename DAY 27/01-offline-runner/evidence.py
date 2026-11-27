"""Strict offline checks for an exported Stage 3 trace and its DataFlash BIN."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


def analyze(trace, messages, *, tail_ok=True):
    phases = {r['phase']: r for r in trace if r['event'] == 'phase'}
    results = [r for r in trace if r['event'] == 'result']
    if len(results) == 1 and (results[0]['status'] == 'FAIL' or results[0]['rollback'] != 'PASS'):
        return {'status': 'FAIL', 'reason': 'RUN_OR_ROLLBACK_FAILED'}
    if set(phases) != {'A', 'B', 'C'} or len(results) != 1 or results[0]['phases'] != ['A', 'B', 'C']:
        return {'status': 'INCONCLUSIVE', 'reason': 'INCOMPLETE_RUN'}
    a, b, c = (phases[p] for p in 'ABC')
    if (a['end'] - a['begin'], b['end'] - b['begin'], c['end'] - c['begin']) != (30, 60, 90) or a['end'] != b['begin'] or b['end'] != c['begin']:
        return {'status': 'INCONCLUSIVE', 'reason': 'PHASE_BOUNDARY_MISMATCH'}
    if not tail_ok:
        return {'status': 'INCONCLUSIVE', 'reason': 'BIN_PARTIAL_OR_PARSER_GAP'}
    catalog = [r for r in trace if r['event'] == 'dataflash_proof']
    if (len(catalog) != 1 or catalog[0]['id'] != c['log_id']
            or catalog[0]['size_after'] <= catalog[0]['size_before']
            or catalog[0].get('prefix_parsed') is not True):
        return {'status': 'INCONCLUSIVE', 'reason': 'DATAFLASH_IDENTITY_UNPROVEN'}
    t0, t1 = a['begin'], c['end']
    anchors = sorted((r for r in trace if r['event'] == 'clock_anchor'), key=lambda r: r['pi_monotonic'])
    if (len(anchors) < 2 or anchors[0]['pi_monotonic'] > t0
            or anchors[-1]['pi_monotonic'] < t1
            or len({r['boot_id'] for r in anchors}) != 1
            or any(not 0 <= r['uncertainty_s'] <= .1 for r in anchors)):
        return {'status': 'INCONCLUSIVE', 'reason': 'CLOCK_ANCHORS_MISSING'}
    offsets = [r['pixhawk_boot_us'] / 1e6 - r['pi_monotonic'] for r in anchors]
    utc_offsets = [r['pi_utc'] - r['pi_monotonic'] for r in anchors]
    if (max(offsets) - min(offsets) > .1 or max(utc_offsets) - min(utc_offsets) > .1
            or any(x['pixhawk_boot_us'] >= y['pixhawk_boot_us'] for x, y in zip(anchors, anchors[1:]))):
        return {'status': 'INCONCLUSIVE', 'reason': 'CLOCK_DISCONTINUITY'}
    offset = (offsets[0] + offsets[-1]) / 2
    by_type = defaultdict(list)
    for row in messages:
        if 'TimeUS' in row:
            by_type[row['mavpackettype']].append(row)

    def interval(rows, begin, end):
        return [r for r in rows if begin + offset <= r['TimeUS'] / 1e6 < end + offset]

    def covers(rows, begin, end):
        stamps = sorted(r['TimeUS'] / 1e6 - offset for r in rows)
        return (bool(stamps) and abs(stamps[0] - begin) <= .5
                and abs(stamps[-1] - end) <= .5
                and all(y - x <= .5 for x, y in zip(stamps, stamps[1:])))

    baseline = [r for r in trace if r['event'] == 'sample' and r['phase'] == 'A']
    if not baseline or baseline[0]['source'] not in (1, 2, 3):
        return {'status': 'INCONCLUSIVE', 'reason': 'INITIAL_SRC_UNKNOWN'}
    initial = baseline[0]['source']
    for msg_type in ('XKFS', 'XKF1', 'XKF2', 'XKF3', 'XKF4'):
        for phase in 'ABC':
            begin, end = phases[phase]['begin'], phases[phase]['end']
            rows = interval(by_type[msg_type], begin, end)
            if any(not covers([r for r in rows if r['C'] == core], begin, end) for core in (0, 1)):
                return {'status': 'INCONCLUSIVE', 'reason': f'{msg_type}_{phase}_COVERAGE_GAP'}
            if msg_type == 'XKFS':
                for r in rows:
                    after_grace = r['TimeUS'] / 1e6 >= begin + offset + 5
                    if phase in 'AB' and r['SS'] != initial - 1:
                        return {'status': 'FAIL', 'reason': 'INITIAL_SRC_CHANGED'}
                    if phase == 'C' and after_grace and r['SS'] != 1:
                        return {'status': 'FAIL', 'reason': 'SRC2_NOT_ACTIVE_IN_DATAFLASH'}
    if any(r['event'] == 'tx' and r['write_ok'] is True
           and t0 <= r['pi_monotonic'] < b['begin'] for r in trace):
        return {'status': 'FAIL', 'reason': 'TX_IN_PHASE_A'}
    tx = [r for r in trace if r['event'] == 'tx' and r['message'] == 'VISION_POSITION_ESTIMATE'
          and r['write_ok'] is True and b['begin'] <= r['pi_monotonic'] < t1]
    if not tx or len(tx) != len({r['usec'] for r in tx}):
        return {'status': 'INCONCLUSIVE', 'reason': 'TX_PACKET_TRACE_MISSING_OR_AMBIGUOUS'}
    received = interval(by_type['VISP'], b['begin'], t1)
    counts = Counter(r['RTimeUS'] for r in received)
    matched = sum(counts[r['usec']] == 1 for r in tx)
    if not matched:
        return {'status': 'INCONCLUSIVE', 'reason': 'NO_UNAMBIGUOUS_VPE_VISP_JOIN'}
    for phase in ('B', 'C'):
        begin, end = phases[phase]['begin'], phases[phase]['end']
        tx_phase = sorted((r for r in tx if begin <= r['pi_monotonic'] < end),
                          key=lambda r: r['pi_monotonic'])
        stamps = [begin, *(r['pi_monotonic'] for r in tx_phase), end]
        if (len(tx_phase) / (end - begin) < 8 or
                any(y - x > .5 for x, y in zip(stamps, stamps[1:]))):
            return {'status': 'INCONCLUSIVE', 'reason': f'TX_TRACE_{phase}_GAP'}
    settled = c['begin'] + 30
    samples = [r for r in trace if r['event'] == 'sample' and r['phase'] == 'C'
               and settled <= r['pi_monotonic'] < t1]
    stamps = [settled, *sorted(r['pi_monotonic'] for r in samples), t1]
    if (not samples or any(y - x > .5 for x, y in zip(stamps, stamps[1:]))
            or any(r['flags'] is None for r in samples)):
        return {'status': 'INCONCLUSIVE', 'reason': 'MISSING_SETTLED_RX'}
    if not all(type(r['flags']) is int and r['flags'] & 16 and not r['flags'] & (128 | 1024)
               and r['local_age'] is not None and 0 <= r['local_age'] < 1 for r in samples):
        return {'status': 'FAIL', 'reason': 'XY_NOT_VALID', 'matched': matched}
    c_matches = [r for r in tx if settled <= r['pi_monotonic'] < t1
                 and counts[r['usec']] == 1]
    if not c_matches or not all(r['aiding'] is True and r.get('aiding_ref') for r in samples):
        return {'status': 'INCONCLUSIVE', 'reason': 'EXTERNAL_NAV_AIDING_UNPROVEN', 'matched': matched}
    return {'status': 'PASS', 'reason': 'ALIGNED_SRC2_AIDING_AND_VALID_XY', 'matched': matched,
            'tx': len(tx), 'visp': len(received)}


def read_bin(path):
    from pymavlink import DFReader

    r = DFReader.DFReader_binary(str(path))
    messages = []
    contiguous = True
    keep = {'XKFS', 'XKF1', 'XKF2', 'XKF3', 'XKF4', 'VISP', 'VISV', 'MSG', 'EV'}
    while True:
        start = r.offset
        m = r.recv_msg()
        if m is None:
            break
        contiguous &= r.offset - start == m.fmt.len
        if m.get_type() in keep:
            messages.append(m.to_dict())
    return messages, contiguous and start == path.stat().st_size


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('bin', type=Path)
    args = parser.parse_args()
    trace = [json.loads(line) for line in args.trace.read_text().splitlines()]
    messages, tail_ok = read_bin(args.bin)
    with args.bin.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    print(json.dumps(dict(analyze(trace, messages, tail_ok=tail_ok), sha256=digest,
                          bytes=args.bin.stat().st_size, tail_ok=tail_ok), indent=2))
