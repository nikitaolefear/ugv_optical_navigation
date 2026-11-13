# Automated results, 2026-11-13

On Raspberry Pi as ugvs2:

- `python -B -m unittest -v test_stop_latch`: 11 tests, OK.
- `node --check app.js`: OK.
- `git diff --cached --check`: OK.
- GPIO reader is mocked in tests; no physical GPIO operation is needed by tests.
- Existing 8082 / 8083 processes unchanged: PID 957 / 3301, both active.
- The server has not been started and the new physical STOP_LATCH experiment has
  not yet been performed. Follow README.md and retain its JSONL journal.

Verified: distinct contact/latch state; press latches/cancels test running;
release retains latch; RESET/PRESSED gets HTTP 409; START/RESUME blocked while
latched even after release; permitted RESET leaves IDLE until separate START;
reset probe logs rejection with PRESSED contact; two cycles; mode validation;
input failures/stale samples and restart latch; journal failure fails closed;
CSRF and no API for injecting contact state.

The only physical evidence so far remains the two earlier contact cycles in
~/bumper17.log, not a completed STOP_LATCH validation.
