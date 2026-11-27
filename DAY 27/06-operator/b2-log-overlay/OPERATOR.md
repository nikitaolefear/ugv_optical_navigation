# B2-LOG operator procedure

**Discovery starts without a parameter index.** The profile enables no mutation until the Owner verifies the index and a separately approved boot/session marker grants the specific transaction action. Rover named reads and PARAM_SET echoes can share wire index 65535. Discovery uses one bounded PARAM_REQUEST_LIST and then an indexed read with exact source, name, type, index and count checks. [Rover parameter handlers](https://raw.githubusercontent.com/ArduPilot/ardupilot/Rover-4.7.2/libraries/GCS_MAVLink/GCS_Param.cpp#L244-L388)

## Deployment and rollback

After separate deployment approval, execute `sudo bash "$PACKAGE/deploy.sh" activate --approved`. The helper pins the B2 reference and modified baseline files, requires safety and verifies the SHA256 manifest, renders a transient B2-LOG drop-in and runs the actual Owner parser with the full command. It preserves the baseline payload and creates a dedicated persistent journal directory. The Owner starts with authorization absent. Readiness requires the standard B2 checks and a B2-LOG IPC status with device/session identity. Automatic startup failure rollback restores the existing B2. No B2-LOG deployment was performed for this task.

To return to B2, run `sudo bash "$PACKAGE/deploy.sh" rollback --approved`. It first revokes the marker and refuses while the visible diagnostic queue or DataFlash exchange is active. It removes only its own drop-in, restarts the original B2 and preserves the journal, including an unfinished transaction. Prefer to verify restoration of the parameter before returning to B2. If rollback is needed with an unfinished record, report LOG_DISARMED as unverified and retain the journal for explicit recovery. Neither rollback nor startup writes a parameter.

The runtime pathname of the Unix socket is taken from the B2 command. A stale socket may be removed only after stopping the unit, MainPID=0 and confirmation from the kernel that no listener or connection owns that pathname. A symlink or other file type stops the helper. EADDRINUSE was previously observed on bind after a stale socket survived shutdown.

## Authorization and recovery

On the Pi after an approved deployment:

```bash
source /etc/ugv-disarm/identities.env
p=$(cat /run/ugv-stage3-operator/log-profile/payload)
py=$(awk -F= '/^ExecStart=// {split($2,a," "); print a[1]}' /run/ugv-stage3-operator/B2.conf)
log_op() {
  sudo -u "#$UGV_GATEWAY_UID" -g "#$UGV_OWNER_RPC_GID" \
    env PYTHONPATH="$p" "$py" -B "$p/log_client.py" "$@" \
    --socket /run/ugv-pixhawk-control/backend.sock --owner-uid "$(id -u)" \
    --socket-gid "$UGV_OWNER_RPC_GID"
}
mark_log() (
  set -euo pipefail
  local auth action="$1" tx="${2:--}"
  auth=$(log_op status | python3 -c '
import json,re,sys
s=json.load(sys.stdin); action, tx=sys.argv[1:]
assert s["profile"]=="B2-LOG" and s["active"] is None and s["queued"]==0
assert not s["transport_fault"] and s["dataflash"]=="IDLE"
assert action=="discover" or s["discovery"] is not None
assert (action in ("discover","begin") and tx=="-") or (
    action in ("restore","recover") and re.fullmatch("[a-f0-9]{32}",tx))
if action in ("restore","recover"):
    assert s["unfinished"] and s["unfinished"]["transaction"]==tx
i=s["identity"]
fields=[i[k] for k in ("hardware","pi_boot","fc_boot","owner_session")]
assert all(re.fullmatch("[a-f0-9]{32,64}",v) for v in fields)
print(" ".join(["B2-LOG",*fields,action,tx]))
' "$action" "$tx")
  sudo bash -eu -c '
    d=/run/ugv-stage3-operator/log-profile
    t=$(mktemp "$d/.authorization.XXXXXX")
    trap '''rm -f -- "$t"''' EXIT
    printf "%s\n" "$1" > "$t"
    chmod 0644 "$t"
    mv -T -- "$t" "$d/authorization"
  ' _ "$auth"
)
revoke_log() { sudo rm -f -- /run/ugv-stage3-operator/log-profile/authorization; }
```

After confirming isolation and the operator's approval for the read-only inventory, call `mark_log discover`, then `log_op discover`. A single inventory request accepts only 1/1, validates all slots, rejects conflicting duplicates, and ignores sentinel 65535. It stops on full coverage, deadline or failure. Limits are 60 seconds, 4096 slots, 12288 frames and zero retries. No unrelated parameter values are retained. Metadata hashes live only in memory during discovery. After full coverage and a quiet period the Owner issues a separate indexed read. Proof requires equal count, type, name, index and value. Incomplete inventory or failed proof leaves the index unavailable. Run `revoke_log` after completion or to cancel. Inspect `log_op status` for the discovered target and transport fault.

Boot/session evidence is in memory and is invalid after a new Owner session, a detected clock rollback or reconnect. Hardware is the digest of the configured USB by-id name verified against the existing Owner descriptor. The local FC epoch is a continuity marker, not a global Pixhawk UUID. Fresh indexed readback also requires the same parameter count. No implicit retry or restoring write occurs on startup.

Only after separate authorization for the LOG_DISARMED transaction, call `mark_log begin`, then `log_op begin`. No Owner restart is needed for this transition. Require phase enabled and save its transaction ID; then revoke the marker. Transport timeout prevents further requests in that Owner and requires explicit follow-up. Never assume it left LOG_DISARMED unchanged.

For routine restore, run `mark_log restore "$TX_ID"` and `log_op restore --transaction "$TX_ID"`. After a crash or session change, read status on the current authorized B2-LOG, require matching hardware and identify the unfinished record. Obtain separate recovery approval. Repeat discovery for the current session, run `mark_log recover "$TX_ID"` and `log_op recover --transaction "$TX_ID"`. Require phase restored and no unfinished journal, then revoke the marker. The fresh indexed read is mandatory even when no restorative write is needed. If an unexpected value or transport fault is reported, stop and retain the record. Re-authorizing a different Owner session never replays an old write.

## Time and DataFlash

Use the transaction and DataFlash proof plan in PROCEDURE.md. On the isolated stand, cap the enabled window at 60 seconds and read at most the existing Owner prefix limit. Schedule the independent restore before enable and stop acquisition early enough to verify restoration. If a guard fails, leave the journal pending for controlled recovery. Never turn an ambiguous readback into successful rollback. Two after-catalogs and the earlier catalog must finish with LOG_REQUEST_END. Capture a fresh Observer and bracketing TIMESYNC. Compare both XKFS cores in the anchored boot-relative interval. Ensure the selected prefix includes the interval or report INCONCLUSIVE. No SRC command is used.
