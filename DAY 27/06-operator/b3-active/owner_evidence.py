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


class Inconclusive(ValueError):
    pass


class Failed(ValueError):
    pass


def verify_stop(rows, frames, trace, times, external=None):
    scenario = times.get("scenario")
    if scenario not in BUDGET_NS or not times.get("reason"):
        raise Inconclusive("missing stop evidence")
    if times["reason"] != scenario:
        raise Failed("unexpected actual stop reason")
    try:
        began, gate, barrier, last_syscall = (times[k] for k in
            ("lease_start_ns","gate_ns","barrier_ns","last_syscall_ns"))
        lease, peer = times["lease"], times["peer"]
        if scenario == "expiry":
            trigger = times["last_renew_ns"]
            if times["trigger_ns"] != trigger:
                raise Inconclusive("expiry trigger mismatch")
        else:
            if not external or external["lease"] != lease or external["peer"] != peer:
                raise Inconclusive("independent same-client trigger absent")
            trigger = external["before_ns"]
            if scenario == "revoke":
                ipc = times["ipc"]
                if (external["event"] != "client_revoke"
                        or not trigger <= ipc["received_ns"] <= ipc["lock_wait_ns"]
                            <= ipc["lock_acquired_ns"] <= gate <= external["after_ns"]):
                    raise Inconclusive("invalid client/IPC chronology")
            elif (external["event"] != "supervisor_pidfd_exit"
                    or external["signal_sent"] is not True
                    or not trigger <= external["exit_observed_ns"]):
                raise Inconclusive("invalid supervisor exit proof")
        if not began <= trigger <= gate <= barrier or last_syscall > barrier:
            raise Inconclusive("invalid stop chronology")
    except (KeyError, TypeError) as error:
        raise Inconclusive("missing or invalid timestamps") from error
    if times.get("safety_lost") is True:
        raise Failed("safety lost")
    if times.get("safety_lost") is not False or times.get("trace_drops") != 0:
        raise Inconclusive("incomplete trace")
    if any(r.get("event") == "tx" and
           (type(r.get("written")) is not int or r["written"] != r.get("size"))
           for r in trace):
        raise Failed("failed or partial Owner TX")
    if not frames:
        raise Inconclusive("no complete authorized test frames")
    try:
        correlate(frames,trace)
    except ValueError as error:
        raise Inconclusive(str(error)) from error
    valid_tx = [r for r in trace if r["event"] == "tx"]
    if any(r.get("lease") != lease for r in valid_tx):
        raise Inconclusive("trace lease mismatch")
    if any(r["written"] != r["size"] or not began <= r["enter_ns"] <= r["exit_ns"]
           or r["enter_ns"] >= gate or r["enter_ns"] >= began+ACTIVE_NS
           or r["exit_ns"] > barrier for r in valid_tx):
        raise Failed("late or incomplete Owner TX")
    if not valid_tx or max(r["exit_ns"] for r in valid_tx) != last_syscall:
        raise Inconclusive("last syscall unproven")
    last = max(f["completion_ns"][1] for f in frames)
    if gate-trigger > GATE_NS[scenario] or max(last,barrier)-trigger > BUDGET_NS[scenario]:
        raise Failed("host stop budget exceeded")
    try:
        a,b = rows[0]["start"],rows[-1]["end"]
        bound = offset_bounds(a,b)
        if a[1]-bound[0] > began-10_000_000_000:
            raise Inconclusive("passive baseline too short")
        if b[1]-bound[1]-max(last,barrier) < TAIL_NS:
            raise Inconclusive("post-stop tail too short")
    except (KeyError, TypeError) as error:
        raise Inconclusive("capture window unproven") from error
    return last


if __name__ == "__main__":
    import argparse
    import json
    from decode import reconstruct

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("capture")
    cli.add_argument("trace")
    cli.add_argument("timing")
    cli.add_argument("--external-trigger")
    args = cli.parse_args()

    def lines(path):
        with open(path) as stream:
            return [json.loads(line) for line in stream]

    with open(args.timing) as stream:
        timing = json.load(stream)
    external = None
    if args.external_trigger:
        with open(args.external_trigger) as stream:
            external = json.load(stream)
    try:
        rows = lines(args.capture)
        trace = lines(args.trace)
        if any(row.get("event") == "tx" and row.get("written") != row.get("size") for row in trace):
            raise Failed("failed or partial Owner TX")
        result = verify_stop(rows, reconstruct(rows), trace, timing, external)
    except Failed as error:
        print(json.dumps(dict(result="FAIL", reason=str(error))))
        raise SystemExit(1)
    except (Inconclusive, ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps(dict(result="INCONCLUSIVE", reason=str(error))))
        raise SystemExit(2)
    print(json.dumps(dict(result="PASS", last_host_completion_upper_ns=result)))
