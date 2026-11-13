# GPIO17 STOP_LATCH — isolated test, no vehicle control

Base: checkpoint `8dcc268c7da187829e2424a01301715e08f71e92`.
Branch: `test/ugv1-stop-latch-gpio17`.
This is an independent loopback-only test server on **8084**, not a production
HMI change. Services 8082/8083, their serial reader and private auth state are
untouched. No Pixhawk, MAVLink, serial, relay, motor or drive commands exist here.
TEST_RUNNING is only a state label; there is no movement implementation.

## Input and state

Physical evidence: `~/bumper17.log` records two pressed/released cycles on
2026-11-13 17:08:40–17:08:45 UTC. This historical log is not used as a live input.
The test reads `/usr/bin/pinctrl get 17` every approximately 100 ms, as the existing
bumper17.sh does. Only `17: ip pu | lo/hi` is accepted (BCM17, input, pull-up).
It does not configure GPIO or reserve the line. Shorter-than-poll pulses can be
missed; this experiment tests software latching, not a hardware stop guarantee.

`contact`: UNKNOWN / PRESSED / RELEASED, independent of `stop_latch`.
Startup and input errors/staleness latch STOP and require a fresh released input
plus explicit RESET. First press latches STOP, cancels TEST_RUNNING/PAUSED and
sets STOPPED. Release changes only contact; STOP_LATCH remains active.
RESET while pressed/unknown returns HTTP 409 and records RESET_REJECTED.
A permitted RESET changes to IDLE without starting. START/RESUME are server-rejected
while latched, even after release. RESUME also requires PAUSED; reset never resumes
a canceled run. The lock serializes GPIO updates and all test actions.
No GPIO samples for 1.5 seconds fail closed on the next state/action request.

All transitions and accepted/rejected actions are appended to a local JSONL journal
with UTC time, sequence, contact, latch and test state. The last 500 entries are
visible on the page and loaded again after a server restart. Boot always latches,
regardless of previous journal state. The full journal stays on Pi, outside Git.

## Tests

    cd /home/ugvs2/ugv1-checkpoints/ugv1-stop-latch-gpio17/stop_latch_test
    /home/ugvs2/.venv-ugv-hmi-auth/bin/python -B -m unittest -v test_stop_latch

Tests include two cycles, RESET while pressed via the server (409), START/RESUME
rejection, separate RESET/START, automatic reset probe, timestamped journal,
restart, GPIO errors, stale reader, invalid GPIO mode, journal failure and CSRF.
They use temporary journals and mocks; no hardware is opened.

## Physical check by one person

1. On Pi, in a dedicated shell as ugvs2:

       cd /home/ugvs2/ugv1-checkpoints/ugv1-stop-latch-gpio17/stop_latch_test
       umask 077
       /home/ugvs2/.venv-ugv-hmi-auth/bin/python -B server.py --journal "$HOME/stop-latch-test-$(date -u +%Y%m%dT%H%M%SZ).jsonl"

2. In an SSH client **on the tablet**, forward local port 8084 to Pi's
   `127.0.0.1:8084` (SSH destination `ugvs2@192.168.110.59`). Equivalent CLI:

       ssh -N -L 8084:127.0.0.1:8084 ugvs2@192.168.110.59

   Open `http://127.0.0.1:8084` on the tablet. This isolated test page is reached
   through your authenticated SSH tunnel; it does not use production passwords,
   production cookies or production account DB. Do not publish its port on LAN.
   POST requires a per-process CSRF token and same-origin requests.

3. With bumper released, expect contact RELEASED and latch ACTIVE at boot. Press
   RESET: expect IDLE, latch cleared, no automatic START. Optionally START TEST:
   expect TEST_RUNNING, still movement_allowed=false.
4. Click «Спробувати RESET при наступному натисканні». Walk to bumper, press and
   hold for at least 1 second, then release. Return to the tablet.
5. Expect contact RELEASED **and latch ACTIVE**. In the journal find, in order:
   CONTACT_PRESSED (PRESSED, latch=true, STOPPED), RESET_REJECTED
   (AUTO_RESET_PROBE, PRESSED, latch=true), CONTACT_RELEASED (latch=true).
   The automatic probe invokes the same RESET gate as the UI; it cannot clear STOP.
6. Click START TEST and RESUME TEST before RESET: both must be rejected; journal
   shows START_REJECTED and RESUME_REJECTED. Click RESET: latch clears, IDLE.
   Wait: it must remain IDLE. Only a separate START TEST changes to TEST_RUNNING.
7. Repeat steps 4–6 for the second cycle. To test normal RESUME without a latch,
   use START TEST → PAUSE TEST → RESUME TEST before pressing the bumper.
8. Stop only this test with Ctrl+C. Keep the JSONL journal as physical evidence.
   Never stop/restart production services or invoke production /api/command for
   this procedure. The original bumper17.log remains unchanged by this test.

Prepared code and automated results do not establish that this new physical
STOP_LATCH procedure has been performed; record its outcome after the tablet test.
