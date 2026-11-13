# UGV №1 — working HMI checkpoint, 2026-11-13

The user confirmed that live ESC voltage is visible on the tablet after restarting
8083. This checkpoint preserves the deployed sources; it does not change services.

## Restore layout

| Component | Repository source | Installation directory |
| --- | --- | --- |
| 8082, sole passive Pixhawk reader | `web_hmi/` | `/home/ugvs2/ugv_optical_navigation/web_hmi/` |
| 8083, authenticated HMI and HTTP bridge | `hmi_auth/web_hmi/`, `hmi_auth/tools/` | `/home/ugvs2/ugv-hmi-auth-password8-release/` |
| Systemd and Nginx templates, installed ownership helper | `deploy/ugv1/` | Review and install under `/etc/systemd/system/`, `/etc/nginx/conf.d/`, `/usr/local/libexec/` |

Install each component separately; do not overlay the two web_hmi directories.
Create separate virtual environments from their requirements files. 8082 uses
`/home/ugvs2/.venv`; 8083 uses `/home/ugvs2/.venv-ugv-hmi-auth`.
The authoritative service templates for this checkpoint are in `deploy/ugv1/`;
older generic deployment examples in component documentation are historical.
Nginx points to loopback 8083. TLS material is provisioned separately.

Only 8082 opens the fixed Pixhawk serial device using the ownership helper.
8083 keeps `UGV_HMI_TELEMETRY=0`; its protected GET /api/telemetry reads the fixed
http://127.0.0.1:8082/api/telemetry with a 0.7 second timeout and no proxies or redirects.
Fresh valid BATTERY_STATUS / SYS_STATUS voltage takes priority. Otherwise it uses
positive fresh ESC #3 voltage matching the current autopilot, already in volts.
The UI displays «Вхід ESC №3», packet age, «Застаріло» at 3 seconds, and
«Немає даних» when unavailable. Upstream failure cannot retain a fresh old voltage.
Existing session and permission checks remain in force.

Provision private state outside Git at `/home/ugvs2/.local/state/ugv-hmi`.
Restore its private DB and auth.key only from an authorized private backup;
never commit them or recreate production accounts through tests. This checkpoint
contains no account state, passwords, private keys, TLS credentials, or virtualenvs.
Test passwords in test fixtures are synthetic and isolated from production.

## Integrity and local history

Base remote: git@github.com:nikitaolefear/ugv_optical_navigation.git
Base main: 1b602761c346dafdfa9f108254cb26fe93864653.
Original local Git has multiple empty objects, including HEAD. It was not repaired.
A clean clone of that verified remote was used for this branch.
Both original working trees and complete Git metadata (including refs, reflogs and
recoverable local objects) remain in the private backup outside this repository.
Previously tracked backup artifacts are removed from this checkpoint tree; the
existing remote history is retained without rewriting.

`UGV1_SOURCE_MANIFEST.json` records SHA256 and original paths for copied release
files. The bridge's isolated tests and JS test were carried over from the prepared
working copy; they were not installed as production modules.

## Isolated verification

From repository root, using the existing local virtualenvs:

    /home/ugvs2/.venv/bin/python -B -c "import pathlib,unittest; r=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(['web_hmi.'+p.stem for p in sorted(pathlib.Path('web_hmi').glob('test_*.py')) if p.stem != 'test_support'])); raise SystemExit(not r.wasSuccessful())"

From `hmi_auth/`:

    /home/ugvs2/.venv-ugv-hmi-auth/bin/python -B -c "import pathlib,unittest; r=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(['web_hmi.'+p.stem for p in sorted(pathlib.Path('web_hmi').glob('test_*.py')) if p.stem != 'test_support'])); raise SystemExit(not r.wasSuccessful())"
    node test_voltage_ui.cjs

Tests use temporary state and mocked or in-memory telemetry. Do not point test
state directories at the private production database. No serial reader, service
restart or Pixhawk parameter changes are required for checkpoint verification.

Checkpoint validation completed: 94 tests for 8082 and 220 tests for 8083 passed;
JS voltage display assertions passed. All 75 copied release files were compared
to their live originals. The generic auth.env.example is the sole intentional
configuration adaptation (local reader disabled on 8083); executable release
sources match byte for byte. A staged-content scan of all 142 tracked files found
no private credential signatures or runtime/backup files. Remaining password
literals were reviewed as test-only fixtures. Original helper integration patches
retain their literal diff context lines, including whitespace; other staged files
pass git diff --check.
