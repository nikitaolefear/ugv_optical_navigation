# Active B3-SW — independent Cursor audit handoff

Baseline evidence/checkpoint 92a42827c2078d111c3cd37f4d9246edac7444c3. Candidate implementation commit 19c4e8060e7f555f252367cc16066398570737b6. This audit handoff is a follow-up commit. No hardware test or deployment of this candidate has occurred.

## File inventory

- active-delta.patch covers disarm_control/b3_lease.py and changes to disarm_control/ipc.py and owner/stop_latch_test/hold_runner.py relative to the B2-LOG overlay. integration.patch is the combined patch relative to pinned B2. BASE_SHA256 pins the changed B2 files.
- deploy.sh, profile.py and transaction.sh generate and settle the combined runtime profile. log_client.py is unchanged from B2-LOG. b3_client.py adds fixed IPC operations.
- owner_evidence.py defines host-only trace/USB validation. test_active.py and test_transaction.sh cover the candidate and rollback. OPERATOR.md, TEST-REPORT.md and SHA256SUMS capture procedure and versions.

## Lease and syscall

Default closed. Root-owned marker binds hardware, Pi boot, FC link epoch, Owner generation, scenario and one-use nonce. IPC uses existing SO_PEERCRED and process generation. Request, renew and revoke are fixed; no client packet data. Idle limit 750 ms, hard limit 5 s, no retry after short write or failure, 10 Hz ceiling. Pose is read from the existing qualified route pose, retaining the physical-alignment record requirement and a 200 ms age limit. The sole Owner can send only its existing ODOMETRY layout, with its existing encoder identity.

Owner tick takes operator lock and the lease lock. Priority/disarm work precedes B3. The check covers current session and marker, client liveness, heartbeat no older than 1 s, HOLD, DISARMED, inhibit, STOP latch, fresh 999/999 PWM from this epoch, physical isolation, unchanged alignment and no conflicting LOG/DataFlash transaction or transport fault. After encoding and the alignment check, the code rechecks lease/guards and pose age. It then makes one os.write on the nonblocking Owner fd under the lease lock. IPC revoke serializes with the send. A write already holding the lock may complete; barrier evidence follows it. Partial/error latched as fault. A bounded trace stores time, protocol identity, pose usec, SHA256, syscall length/result, lease and Owner session. Export while inactive before a later case overwrites the rows.

## Audit questions and known gaps

1. Review lock ordering and whether existing B2-LOG paths, STATUS I/O, marker/asset reads and nonblocking writes can stall the Owner tick. The alignment gate reads assets and may add latency. Clock elapsed check runs again after that gate, but blocking OS I/O has no measured bound.
2. Check atomicity boundary for a root out-of-band marker removal or hardware state change between the last read and syscall. The mutex orders IPC revoke with write; it cannot serialize a physical event or external filesystem operation. Validate the correct stop trigger with separate client/supervisor timestamps, including priority, signal delivery and reaping.
3. Review source/boot identity and linkage with connected FD, heartbeat epoch, Pixhawk reset and same-boot alignment. Existing alignment file existence was observed; its suitability under the combined candidate has not been qualified.
4. Confirm the dormant path cannot expose SRC, PARAM_SET, brake or ARM from B3, and combined trace status reflects TX. Verify HMI, camera, Observer, DataFlash and policy coexistence in an independent combined-profile harness before any active run.
5. Verify active buffers and kernel queue evidence, trace export completeness, and successful completion attribution. Current barrier orders local syscalls. It does not drain the device queue or establish wire timing. An independent host capture is required and hardware is deferred.
6. Review boot and process logs, paused supervisor and clock steps. Separate draft budgets are manual 150/250 ms gate/host, client exit 250/350 ms, expiry 825/850 ms from last renew. Live thresholds must be approved before data collection. Five-second captured tail required after the later of barrier and host completion.
7. Review profile rollback with no live B3 authorization, no diagnostic conflict and retained journal. Restart invalidates Indexed Discovery and any old markers. No previously observed discovery may be reused.

## Test evidence

Candidate 9/9, B2-LOG 34/34, passive decoder 11/11, archived adapter/Owner 25/25, shell rollback 3/3 PASS. The simulated syscall/revoke race and guard loss after packing were exercised. These do not replace an independent review of the real callbacks and IPC or authorize live TX. See OPERATOR.md for exact staged deployment and rollback. Safety decisions and physically isolated bench remain mandatory.
