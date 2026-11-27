"""Offline acceptance checks for the archived sole-Owner TX trace contract."""
import hashlib

from pymavlink.dialects.v20 import ardupilotmega as mav
from decode import EXTERNAL_NAV, correlate, offset_bounds

IDLE_NS = 750_000_000
BUDGET_NS = dict(revoke=250_000_000, client_exit=350_000_000, expiry=850_000_000)
GATE_NS = dict(revoke=150_000_000, client_exit=250_000_000, expiry=825_000_000)
TAIL_NS = 5_000_000_000
ACTIVE_NS = 5_000_000_000


def frame_fields(packet, *, enter_ns, exit_ns, written):
    """Call offline on already recorded bytes or at the future narrow trace hook."""
    msg = mav.MAVLink(None).decode(bytearray(packet))
    if msg.get_msgId() not in EXTERNAL_NAV or not 0 < enter_ns <= exit_ns:
        raise ValueError("unexpected Owner evidence")
    return dict(event="tx",id=msg.get_msgId(),seq=msg.get_seq(),sys=msg.get_srcSystem(),
                comp=msg.get_srcComponent(),usec=getattr(msg,"usec",getattr(msg,"time_usec",None)),
                sha256=hashlib.sha256(packet).hexdigest(),
                enter_ns=enter_ns,exit_ns=exit_ns,written=written,size=len(packet))


def verify_stop(rows, frames, trace, times):
    """Verify a bounded host-only claim. Missing inputs must be reported inconclusive."""
    began, trigger, gate, barrier = (times[k] for k in
                                     ("lease_start_ns","trigger_ns","gate_ns","barrier_ns"))
    if not began <= trigger <= gate <= barrier or barrier-began > ACTIVE_NS+max(BUDGET_NS.values()):
        raise ValueError("invalid stop evidence")
    scenario = times["scenario"]
    if scenario == "expiry":
        if trigger != times["last_renew_ns"]:
            raise ValueError("expiry trigger mismatch")
    elif scenario not in ("revoke","client_exit"):
        raise ValueError("unknown stop scenario")
    if not frames or not correlate(frames,trace):
        raise ValueError("no complete authorized test frames")
    valid_tx = [r for r in trace if r["event"] == "tx"]
    if any(r["written"] != r["size"] or not began <= r["enter_ns"] <= r["exit_ns"]
           or r["enter_ns"] >= gate or r["enter_ns"] >= began+ACTIVE_NS
           or r["exit_ns"] > barrier for r in valid_tx):
        raise ValueError("late or incomplete Owner TX")
    last = max(f["completion_ns"][1] for f in frames)
    if gate-trigger > GATE_NS[scenario] or max(last,barrier)-trigger > BUDGET_NS[scenario]:
        raise ValueError("host stop budget exceeded")
    a,b = rows[0]["start"],rows[-1]["end"]
    bound = offset_bounds(a,b)
    if b[1]-bound[1]-max(last,barrier) < TAIL_NS:
        raise ValueError("post-stop tail too short")
    if times.get("safety_lost") is not False or times.get("trace_drops") != 0:
        raise ValueError("unsafe or incomplete trace")
    return last


if __name__ == "__main__":
    import argparse
    import json
    from decode import reconstruct

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("capture")
    cli.add_argument("trace")
    cli.add_argument("timing")
    args = cli.parse_args()

    def lines(path):
        with open(path) as stream:
            return [json.loads(line) for line in stream]

    with open(args.timing) as stream:
        timing = json.load(stream)
    rows = lines(args.capture)
    result = verify_stop(rows, reconstruct(rows), lines(args.trace), timing)
    print(json.dumps(dict(last_host_completion_upper_ns=result)))
