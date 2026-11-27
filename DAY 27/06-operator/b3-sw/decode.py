"""Fail-closed reconstruction of a single completed USB CDC bulk OUT stream."""
import argparse
import hashlib
import json

from pymavlink.dialects.v20 import ardupilotmega as mav

EXTERNAL_NAV = {101, 102, 103, 104, 11011, 331}


def offset_bounds(begin, end):
    def bound(sample):
        start, wall, finish = sample
        if not 0 <= start <= finish or finish-start > 2_000_000:
            raise ValueError("clock bracket")
        return wall-finish, wall-start
    if begin[2] >= end[0]:
        raise ValueError("clock ordering")
    a,b=bound(begin),bound(end)
    # Includes calibration jitter and limited drift over the bounded window.
    drift = 1_000_000 + (end[2]-begin[0])//10000
    if max(a[0],b[0])-min(a[1],b[1]) > drift:
        raise ValueError("clock step or drift")
    return min(a[0],b[0])-drift,max(a[1],b[1])+drift


def reconstruct(rows):
    if len(rows) < 2:
        raise ValueError("missing capture envelope")
    start, end = rows[0], rows[-1]
    if end.get("dropped") != 0 or end.get("undrained") or end.get("interrupted") or end.get("failure"):
        raise ValueError("capture incomplete")
    bounds = offset_bounds(start["start"],end["end"])
    key = start["mapping"]
    pending = {}
    completion_order = []
    expected_order = []
    parts = []
    last_wall = 0
    for item in rows[1:-1]:
        if (item["bus"],item["dev"],item["ep"],item["kind"]) != (
                key["bus"],key["device"],key["endpoint"],3):
            continue
        wall = item["wall_ns"]
        if wall < last_wall or not start["start"][1] <= wall <= end["end"][1]:
            raise ValueError("ambiguous event clock")
        last_wall = wall
        tag = item["urb"]
        if item["event"] == "S":
            payload = bytes.fromhex(item["payload"])
            if tag in pending or not 0 < len(payload) == item["length"] == item["captured"] or item["no_data"]:
                raise ValueError("missing, truncated or ambiguous submission")
            pending[tag] = (payload,wall)
            expected_order.append((tag,wall))
        elif item["event"] == "C":
            if tag not in pending:
                raise ValueError("completion without submission")
            payload, sent_at = pending.pop(tag)
            if item["status"] or item["length"] != len(payload):
                raise ValueError("failed or partial completion")
            completion_order.append((tag,sent_at))
            parts.append((payload,sent_at,wall))
        else:
            raise ValueError("unexpected URB event")
    if not parts:
        raise ValueError("no complete OUT traffic in capture")
    if pending or expected_order != completion_order:
        raise ValueError("missing or ambiguous completion order")
    return frames(parts,bounds)


def frames(parts,bounds):
    decoder = mav.MAVLink(None)
    stream = bytearray()
    results = []
    first_submit = None
    for payload, submit, complete in parts:
        if not stream:
            first_submit = submit
        stream.extend(payload)
        while len(stream) >= 2:
            if stream[0] not in (0xfe,0xfd):
                raise ValueError("stream does not start with a MAVLink frame")
            v2 = stream[0] == 0xfd
            if v2 and len(stream)<3:
                break
            if v2 and stream[2] & ~1:
                raise ValueError("unsupported MAVLink incompatibility flags")
            size = stream[1] + (12 if v2 else 8) + (13 if v2 and stream[2]&1 else 0)
            if len(stream)<size:
                break
            frame=bytes(stream[:size]); del stream[:size]
            try:
                msg=decoder.decode(bytearray(frame))
            except mav.MAVError as error:
                raise ValueError("MAVLink CRC or framing rejected") from error
            mid=msg.get_msgId()
            if mid < 0 or msg.get_type() == 'UNKNOWN':
                raise ValueError("unknown MAVLink CRC extra")
            if mid in EXTERNAL_NAV:
                results.append(dict(id=mid, seq=msg.get_seq(), sys=msg.get_srcSystem(),
                                    comp=msg.get_srcComponent(), usec=getattr(msg,"usec",getattr(msg,"time_usec",None)),
                                    sha256=hashlib.sha256(frame).hexdigest(),
                                    submit_ns=[first_submit-bounds[1],first_submit-bounds[0]],
                                    completion_ns=[complete-bounds[1],complete-bounds[0]]))
            first_submit=submit
    if stream:
        raise ValueError("truncated MAVLink tail")
    return results


def correlate(frames, trace):
    # Wire sequence alone wraps. Match full frame hash and other fields exactly.
    key = lambda row: tuple(row[k] for k in ("id","seq","sys","comp","usec","sha256"))
    successes = [r for r in trace if r["event"] == "tx" and r["written"] == r["size"]]
    if [key(f) for f in frames] != [key(t) for t in successes]:
        raise ValueError("trace and capture frame sequences disagree")
    for frame, owner in zip(frames,successes):
        if frame["submit_ns"][1] < owner["enter_ns"] or frame["completion_ns"][1] < owner["enter_ns"]:
            raise ValueError("causal mismatch")
    return len(successes)


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture")
    p.add_argument("--trace")
    p.add_argument("--expect-passive", action="store_true")
    a=p.parse_args()
    def load(path):
        with open(path) as stream:
            return [json.loads(line) for line in stream]
    rows=reconstruct(load(a.capture))
    if a.trace:
        correlate(rows,load(a.trace))
    if a.expect_passive and rows:
        raise SystemExit("FAIL: ExternalNav frame during passive window")
    print(json.dumps(dict(externalnav_frames=len(rows),last=rows[-1] if rows else None),indent=2))
