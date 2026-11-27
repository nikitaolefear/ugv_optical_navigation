# B2-LOG operator procedure

**Blocking input.** Obtain the numeric LOG_DISARMED index from a trusted, separately authorized read-only parameter inventory for this installed firmware and active scripting configuration. No verified current index was available in the retained artifacts. Do not guess it or use a named PARAM_REQUEST_READ to verify a write. Rover emits PARAM_SET echoes with param_index=-1 (wire 65535) and named read replies can also use that index. The isolated helper reads by index and accepts only an exact name/index/type from 1/1. A wrong or moved index fails the pre-write baseline read. No parameter inventory was requested during this task. [Rover parameter handlers](https://raw.githubusercontent.com/ArduPilot/ardupilot/Rover-4.7.2/libraries/GCS_MAVLink/GCS_Param.cpp#L244-L388)

## Deployment and rollback

After separate deployment approval and confirmation of a valid index, set shell variable LOG_DISARMED_INDEX and execute `sudo bash "$PACKAGE/deploy.sh" activate --approved "$LOG_DISARMED_INDEX"`. The helper pins the B2 reference and modified baseline files, requires safety and verifies the SHA256 manifest, renders a transient B2-LOG drop-in and runs the actual Owner parser with the full command. It preserves the baseline payload and creates a dedicated persistent journal directory. The Owner starts with authorization absent. Readiness requires the standard B2 checks and a B2-LOG IPC status with device/session identity. Automatic startup failure rollback restores the existing B2. No B2-LOG deployment was performed for this task.

To return to B2, run `sudo bash "$PACKAGE/deploy.sh" rollback --approved`. It first revokes the marker and refuses while the visible diagnostic queue or DataFlash exchange is active. It removes only its own drop-in, restarts the original B2 and preserves the journal, including an unfinished transaction. Prefer to verify restoration of the parameter before returning to B2. If rollback is needed with an unfinished record, report LOG_DISARMED as unverified and retain the journal for explicit recovery. Neither rollback nor startup writes a parameter.

The runtime pathname of the Unix socket is taken from the B2 command. A stale socket may be removed only after stopping the unit, MainPID=0 and confirmation from the kernel that no listener or connection owns that pathname. A symlink or other file type stops the helper. EADDRINUSE was previously observed on bind after a stale socket survived shutdown.

## Authorization and recovery

Use the installed log_client.py with the same UID/GID, Owner UID and socket arguments as the B2 operator client. Set PYTHONPATH to the isolated payload. The only actions are status, begin, restore and recover. Run status and record identity.hardware, pi_boot, fc_boot and owner_session, the unfinished transaction ID and queue status. Hardware is the digest of the configured USB by-id name verified against the open Owner descriptor. fc_boot is a local generation bound to the current link and observed boot time continuity, with a new value after reconnect or clock rollback. It is not a Pixhawk UUID. Treat ambiguous identity as blocking.

After physical isolation and explicit approval, create a root-owned mode 0644 marker in the log-profile runtime directory. Write exactly one line, ending in a newline:
```text
B2-LOG <hardware> <pi_boot> <fc_boot> <owner_session> begin -
```
The command is `log_client.py begin` with the standard IPC arguments. Require the journal result enabled. Keep the marker through the result, then revoke it. No implicit retry or restoring write occurs on startup. A timed-out in-flight exchange marks the transport faulted and prevents further transactions in that Owner process. Do not restart a live Owner without separate approval. Its later restart requires a new marker and current-session proof.

For routine restore, use marker action restore with the transaction ID and run `log_client.py restore --transaction "$TX_ID"`. After a crash or session change, read status on the current authorized B2-LOG, require matching hardware and identify the unfinished record. Obtain separate recovery approval. Set the same marker format with current identity and suffix `recover <transaction>`. Run `log_client.py recover --transaction "$TX_ID"`. Require phase restored and no unfinished journal, then revoke the marker. The fresh indexed read is mandatory even when no restorative write is needed. If an unexpected value or transport fault is reported, stop and retain the record. Re-authorizing a different Owner session never replays an old write.

## Time and DataFlash

Use the transaction and DataFlash proof plan in PROCEDURE.md. On the isolated stand, cap the enabled window at 60 seconds and read at most the existing Owner prefix limit. Schedule the independent restore before enable and stop acquisition early enough to verify restoration. If a guard fails, leave the journal pending for controlled recovery. Never turn an ambiguous readback into successful rollback. Two after-catalogs and the earlier catalog must finish with LOG_REQUEST_END. Capture a fresh Observer and bracketing TIMESYNC. Compare both XKFS cores in the anchored boot-relative interval. Ensure the selected prefix includes the interval or report INCONCLUSIVE. No SRC command is used.
