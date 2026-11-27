# Active B3-SW re-audit tests

20/20 candidate tests passed. Includes real Unix IPC and credentials, RLock, non-vehicle PTY os.write, real MAVLink decoding, unreaped zombie pidfd, individually false safety predicates, injected callback exceptions and failures, trace, rollback predicate, peer/process binding, premature renew, manual revoke, expiry and termination, bad scenario, missing or impossible trigger, 700 ms hidden delay, stale pose, session change and partial write. Original false PASS and callback escape reproduced on archived code offline.

Previous B2-LOG tests 34/34, decoder 11/11, archived Owner/lease 25/25 and rollback mocks 3/3. The PTY harness exercises extracted production predicates and IPC, not a full combined HMI service boot. No live deployment, traffic, capture, parameter queries or changes.
