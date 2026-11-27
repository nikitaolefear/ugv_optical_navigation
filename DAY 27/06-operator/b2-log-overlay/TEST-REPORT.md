# B2-LOG test report

- 29 Python tests passed, including the prior 22 cases and 7 discovery cases. The original packet test additionally verifies rejection of a PARAM_SET echo with index 65535.
- The real pymavlink codec, a temporary Unix socket and SO_PEERCRED were exercised. The Owner tick, lifecycle, link change, playback of echo, asynchronous worker and durable recovery used simulated inputs. Separate processes were terminated before and after simulated write to verify recovery. No Pixhawk connection was opened.
- Full B2 and rendered B2-LOG parser arguments were accepted in isolated imports of the installed baseline. B1 rejects the additional profile options and ordinary B1/B2 IPC excludes LOG operations.
- Termination of expired work, loss of guard at tick, status queue and blocked unauthorized recovery were checked. Arming, SRC switches and ExternalNav TX are excluded.
- Simulated systemctl tests passed for automatic rollback after startup failure, after readiness failure and manual return to B2. No live deployment or parameter changes were attempted.
- Deployment does not require a supplied index. Discovery tests cover unique inventory, duplicates, sentinel index, wrong source, timeout, incomplete inventory, cancellation, heartbeat loss, reset and session loss, mismatches in indexed readback, and separate transaction authorization. Live discovery was not run.
