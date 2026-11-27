"""Offline preflight proof checks. Never opens the link or requests a live catalog."""


def prove_prefix(before, after, identifier, prefix, *, session_ended):
    if session_ended is not True:
        raise ValueError('missing LOG_REQUEST_END evidence')
    if identifier not in after or after[identifier] <= before.get(identifier, 0):
        raise ValueError('no growth on specified log ID')
    if not {'FMT', 'XKFS'} <= {row['mavpackettype'] for row in prefix}:
        raise ValueError('prefix lacks FMT/XKFS')
    if {r['C'] for r in prefix if r['mavpackettype'] == 'XKFS'} != {0, 1}:
        raise ValueError('prefix lacks independent core samples')
    return dict(event='dataflash_proof', id=identifier, size_before=before.get(identifier, 0),
                size_after=after[identifier], prefix_parsed=True)


def join_vpe_visp(owner_trace, dataflash, *, t0, t1):
    """Join exact one-to-one timestamps; report misses without asserting fusion."""
    from collections import Counter
    sent = [row['usec'] for row in owner_trace if row.get('direction') == 'TX'
            and row.get('message_id') == 102 and type(row.get('usec')) is int
            and t0 <= row['monotonic'] < t1]
    seen = Counter(row['RTimeUS'] for row in dataflash if row.get('mavpackettype') == 'VISP')
    counts = Counter(sent)
    return dict(tx=len(sent), matched=sum(seen[u] == 1 and counts[u] == 1 for u in sent),
                unmatched=sum(seen[u] == 0 for u in sent),
                ambiguous=sum(seen[u] > 1 or counts[u] > 1 for u in sent))
