# B3-SW software USB evidence

**B3-HW — DEFERRED / RESOURCE CONSTRAINT.** No new equipment or dependencies. A software PASS covers the Linux host and never establishes the independently measured last bit on USB. An eventual B4 is separate and remains subject to isolated traction, separate approval and its own acceptance criteria.

## Passive capture A

After separate approval, verify one Owner, HOLD, DISARMED, inhibit, STOP latch, fresh heartbeat and 999/999 microseconds, ExternalNav disabled and sent=0. Run from the verified package. Record the current tty from the existing Owner check:

```bash
sudo modprobe usbmon
tty_name=$(sudo /usr/local/libexec/ugv-pixhawk-owners | python3 -c '
import json,os,sys
s=json.load(sys.stdin)
assert s["complete"] and len(s["owners"])==1 and not s["error"]
print(os.path.basename(s["identity"]["target"]))') || exit
private=$(mktemp -d)
sudo python3 ./usb_capture.py --tty "$tty_name" --seconds 15 --max-bytes 8388608 --output "$private/b3-passive.jsonl"
sudo chown "$(id -u):$(id -g)" "$private/b3-passive.jsonl"
"$HOME/.venv/bin/python" ./decode.py --expect-passive "$private/b3-passive.jsonl"
```

Decoding uses the existing pymavlink virtual environment. The collector is standard-library only for 64-bit Linux on this Pi. It maps the current tty through sysfs CDC union and Bulk OUT descriptors, monitors mapping continuity and opens only the resulting per-bus usbmon character device. It caps output and duration, uses an exclusive mode 0600 file in a private directory, checks MON_IOCG_STATS and captures GETX full payloads. The first binary API run remains to be validated against this kernel under authorization. No live module loading, capture or serial activity was performed during preparation.

A has no TX lease. Collect 15 seconds. PASS requires attributable complete OUT traffic and zero ExternalNav frames during that window, plus confirmed Owner TX=0 and all interlocks before and after. Any decoded ExternalNav frame or violated safety condition is FAIL. Missing OUT traffic, capture drops, unpaired/short URB, incomplete payload or drift beyond clock bounds is INCONCLUSIVE. For interruption send SIGINT (Ctrl+C) or SIGTERM. The collector closes its own fd and writes a bounded failure footer when possible. Missing footer also invalidates the run. Protect the raw capture as private. Rollback is closure of this collector only. If this session loaded usbmon and there are no other users, remove it with `sudo modprobe -r usbmon`. No Owner overlay or service action is needed for A.

## Active scenarios B–D

The archived Stage3 lease and sole-Owner trace contract are the starting point. owner_evidence.py defines an offline adapter for full frame hash, usec, ID, source/sequence, syscall count and an acceptance check against completed USB frames. The installed B2-LOG does not wire this lease/trace to its active transport. Its existing IPC remains read-only or LOG_DISARMED-only. The archived generic Stage3 candidate also includes operations outside B3 and must not be enabled wholesale. Before active readiness, narrowly wire only lease open/renew/revoke and the fixed bench ExternalNav frame path, with UID and boot/session authorization, a five-second hard cap, no raw-tx API and no parameter, SRC, arm or brake operations. Verify the actual send-time PWM/heartbeat and existing guards, gate-close serialization, barrier/short-write handling, parser, profile rollback and conflicts with B2-LOG on the combined profile. Commands for active deployment and active scenarios remain unavailable until that wiring is done.

All B–D use the same guards as A plus separately authorized TX in a pinned profile, one Owner, 10 s baseline, at most 5 s TX, 5 s capture after the last confirmed completion or barrier (whichever is later). Run the scenarios separately.

| Scenario | Trigger and limits |
| --- | --- |
| B manual revoke | Timestamp the revoke request on Pi monotonic. Verify gate closed, successful write accounting and barrier. |
| C client termination | Timestamp exit from the local supervisor; terminate the test client only. Stop via normal revocation or the idle expiry fallback. |
| D lease expiry | Stop renewals. Use the last accepted renew as origin and verify idle expiry and barrier. |

Proposed host stop budget 850 ms from the trigger, including uncertainty. This carries forward the earlier 750 ms lease-idle allowance plus 100 ms for host scheduling and residual proof. It is an acceptance threshold, not a hardware measurement and is not increased after seeing results. Confirm its applicability to B and C before authorizing active tests. The actual gate, syscall exit, barrier, queue state and latest full URB completion remain distinct. The verifier requires a five-second observation tail and rejects new Owner writes after gate. An agreed deadline exceeded, unsafe state, successful late frames or unreconciled writes is FAIL; missing provenance, capture loss and clock ambiguity are INCONCLUSIVE. Abort by revoking the narrow lease and preserving isolation. Missing safe closure does not justify assuming TX=0. Rollback of a future active profile must be checked with the installed service and guard verification before live use.

## Clock and USB boundary

Store usbmon realtime timestamps and bracket them with Pi realtime/monotonic pairs. Apply the bracket and drift bounds and reject discontinuities. For each successful bulk OUT completion join its submission by URB ID, actual length and status; require complete bytes, unambiguous order and MAVLink CRC. Decode MAVLink frame IDs, sequence and sender timestamps across submissions. Join to the Owner trace on full bytes hash and protocol fields. A host completion interval is not the physical last-bit timestamp. Capture truncation, losses and absent framing deliberately fail closed. A fresh nonce-correlated TIMESYNC can align Pi and FC boot time with known error. Include DataFlash TimeUS and CLK/Observer evidence only if already available without changing LOG_DISARMED. No claim of FC parse or EKF aiding is derived from these host observations.

[Linux usbmon ABI](https://docs.kernel.org/usb/usbmon.html) [MAVLink framing](https://mavlink.io/en/guide/serialization.html)
