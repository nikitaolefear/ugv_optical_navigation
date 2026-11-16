# UGV №1 HMI checkpoint — 16.11.2026

Branch: `checkpoint/ugv1-hmi-ui-2026-11-16`.
Parent: `19abc112684604f1bac6dbdb9c698c0e76bdfb21`.
The inherited `stop_latch_test/PHYSICAL_PROTOCOL_2026-11-13.md` is preserved.

## Installed release and UI

Runtime source: `/home/ugvs2/ugv-hmi-auth-password8-release`.
`SOURCE_MANIFEST_20261116.json` records matching file hashes. API, auth,
telemetry bridge, runtime and tools outside `web_hmi/` were compared too.
The main page has no EKF section or MAVLink freshness row. Connection,
Rover mode, ARM/DISARM, yaw, voltage, source «Вхід ESC №3» and voltage age remain.
The existing fixed-loopback bridge selects ESC #3 without changing
`/api/telemetry`, opening serial or sending MAVLink requests/commands.

## Background technical log

Implementation: `web_hmi/technical_telemetry_log.py`.
Gunicorn starts/stops its background thread through `gunicorn_conf.py`.
Its only local module dependencies are tracked `auth.private_file` and
`telemetry_bridge.TelemetryBridge`. There is no additional external logger script.
It polls existing 8082 HTTP at most once/second, without browser traffic.
FRESH samples are logged at most once/30 seconds; prolonged unavailability is
logged only when state/reason/autopilot source changes. A process restart emits
an initial state again. Logger errors are reported at most once/minute.

Private production file:
`/home/ugvs2/.local/state/ugv-hmi/telemetry-technical.jsonl`.
UTF-8 JSONL, UTC time, indicator, source, state, value/reason and available age.
Limit: 1 MiB plus three rotated backups, mode 0600. Operator audit is separate.
Indicators: ALT, BATTERY_STATUS, ESTIMATOR_STATUS, EKF_STATUS_REPORT.
ALT has `source=null`, `unit=null`, `value=null`, `UNKNOWN/NOT_RECEIVED`,
`detail=ALT_NOT_COLLECTED_BY_CURRENT_API`; current 8082 does not collect altitude.
No altitude source/units or measurement is invented. BATTERY_STATUS values
retain explicit `voltages_mv`/`voltages_ext_mv` keys. EKF values contain flags.
No JSONL files, private auth DB/state, keys, passwords or backups are included.

## Configuration for reproduction

Keep `UGV_HMI_TELEMETRY=0` for 8083, `UGV_HMI_TRUST_PROXY=1`, the loopback bind
`127.0.0.1:8083`, one Gunicorn worker, no preload/reloader and umask 0077.
The running service matches `../deploy/ugv1/ugv-hmi-auth.service.example`.
The release's old `auth.env.example` says TELEMETRY=1; it was deliberately not
copied. The checkpoint example keeps 0, matching the running service.

To reproduce the source tree, copy `hmi_auth/web_hmi/` and `hmi_auth/tools/`
into a separate release directory and use existing venv dependencies in
`web_hmi/requirements_production.txt`. Adapt WorkingDirectory/ExecStart paths
if the release directory differs. Private auth state and TLS files must be
provisioned separately; they cannot be restored from this checkpoint.
No service was restarted or changed during checkpoint creation.

## Verification on isolated state

Run all Python modules from `hmi_auth/`:

```python
from pathlib import Path
import unittest
names = ['web_hmi.' + p.stem for p in sorted(Path('web_hmi').glob('test_*.py'))
         if p.stem != 'test_support']
result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(names))
raise SystemExit(not result.wasSuccessful())
```

Browser regression (requires Chromium, temporary test login only):

```sh
python -B tools/browser_telemetry_check.py /tmp/ugv-hmi-tablet-check
```

The browser check uses synthetic FRESH EKF/MAVLink data and four successive
mode/voltage changes, then STALE/recovery. It checks absent diagnostics,
preserved voltage source/age, 3-second expiry, missing voltage and no horizontal
overflow at 800×1100 and 1280×800. It never connects to the rover or live auth state.
The logger tests cover prolonged absence, transitions, sampling/rate limits,
rotation/private modes, fresh EKF flags/age and snapshot immutability.
Results: **222 Python tests passed** in 78.753 seconds. Chromium regression
passed at **800×1100 and 1280×800**. `git diff --cached --check` passed.
