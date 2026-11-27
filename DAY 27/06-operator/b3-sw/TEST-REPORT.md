# B3-SW offline checks

- 11 package tests PASS. Covered single, split and coalesced frames, bad CRC, truncation, loss, endpoint filtering, failed and short completions, out-of-order completion, empty stream, clock step, quota, trace mismatch, stop cases and unsafe trace rejection.
- Current B2-LOG regression 34/34 PASS and rollback mocks 3/3 PASS.
- Archived adapter and Owner integration regression 25/25 PASS. Cases included no write before authorization, revoke, killed client, lease expiry, heartbeat safety and rollback. These exercise archived behavior, not a combined active overlay. PWM interruption is modeled in evidence checks; live send-time PWM integration is pending.
- Current sysfs mapping verified read-only via the collector helper. Binary usbmon syscall compatibility and no-loss capture still need the authorized passive run. No live usbmon, new serial, deployment or TX.
