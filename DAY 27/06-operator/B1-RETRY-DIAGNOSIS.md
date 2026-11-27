# B1 diagnosis — 2026-11-27

The manual attempt differs from the earlier authentication failure. The helper started the temporary Owner at 14:55:26 UTC. Its parse_args failed at hold_runner.py:70 with AttributeError: Namespace has no attribute stage3_trace. The minimal overlay omitted those full-profile CLI options while retaining two validation blocks for them. The failure occurred before the Owner initialization completed. The helper subsequently returned 1 at bench-deploy.sh:96 when wait_owner never obtained readiness. Its EXIT trap restored the stock profile, active at 14:55:52 UTC. The temporary configuration and matching cloned files show that B1 installation began; readiness never passed.

The patch removes only those two full-profile validation blocks from the minimal overlay. It retains all B1 observe-only interlocks. Four offline parser tests reproduced the original exception, accepted the pinned B1 configuration after the fix, verified the stock argument contract, and rejected six incompatible flag cases. The updated staging package and regenerated hashes were checked locally. No retry or service change was made during diagnosis.

The restored production baseline and rollback snapshot matched. One Owner with fresh heartbeat, HOLD, DISARMED, TX=0, inhibit and STOP latch was confirmed via system metadata and read-only telemetry. Host paths and raw logs are omitted.
