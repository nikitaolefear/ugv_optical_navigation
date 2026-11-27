# B3-SW active candidate tests

- 9 candidate Python tests pass, including full rendered startup parser, closed default, UID/session-derived guard inputs, expiry, peer termination, explicit revoke, serialized concurrent send, stale pose, guard after pack, alignment, I/O failure and partial writes.
- B2-LOG regression 34/34, passive decoder 11/11, archived lease/Owner 25/25 and shell rollback mocks 3/3 passed. These established baseline behavior and do not prove all combined service runtime paths.
- Mock guard tests model heartbeat, PWM and transport failure as loss of the precise predicate passed into the lease. Real callback execution, IPC/client process timing, USB host stop bound and module timing are pending independent audit and authorized bench qualification.
- No live commands, usbmon loads, service restarts or Pixhawk writes.
