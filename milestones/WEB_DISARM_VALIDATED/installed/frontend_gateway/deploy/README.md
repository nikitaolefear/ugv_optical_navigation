# Prepared configuration — do not apply during development

Three service UIDs are required: frontend `ugv-hmi`, gateway `ugv-command`,
owner `ugv-pixhawk` (or current `ugv-operator` as an explicitly accepted transition).
No accounts, groups or services have been created by this preparation.

- Frontend gets only `ugv-rpc-frontend`, no dialout/gpio/sudo. Its code/venv
  must move from private `/home/ugv-operator` into root-owned read-only `/opt`.
  Its auth DB/key stay separate and private (0700 state, 0600 files).
- Gateway primary group `ugv-rpc-frontend` makes its RuntimeDirectory 0710
  and ingress socket 0660 match that exact GID. It also joins only
  `ugv-rpc-owner` to connect to the backend. It cannot read owner state 0700.
- Root provisions `/run/ugv-pixhawk-control` as owner UID:`ugv-rpc-owner`
  0710; owner must be allowed to chgrp its own newly bound socket to that GID.
  Its backend socket is 0660, exact peer allowlist only the gateway UID.
- Root provisions `/run/ugv-disarm-policy` root:root 0755, not writable by
  services. A marker `/run/ugv-disarm-policy/inhibit` blocks new requests.
  Missing/untrusted policy directory or stat failure also blocks. Once blocked,
  each process latches OFF until restart; deleting the marker alone cannot enable.

The frontend is explicitly configured with gateway UID and frontend RPC GID.
The gateway CLI requires all three distinct UIDs, distinct RPC GIDs and explicit
`--enable-disarm`. The template intentionally omits this enabling flag.
Owner startup requires the backend UID/GID explicitly:

```text
--web-disarm-socket /run/ugv-pixhawk-control/backend.sock
--web-disarm-db /var/lib/ugv-pixhawk/disarm.sqlite3
--web-disarm-client-uid <gateway UID>
--web-disarm-client-gid <owner RPC GID>
--inhibit-file /run/ugv-disarm-policy/inhibit
```

Do not add `--enable-bench-hold` for the normal production profile. The prepared
8084 server is read-only regardless of that maintenance flag. Keep auth local
telemetry OFF; only owner opens serial. Never pass FD via SCM_RIGHTS.

The current owner uses a sudo-based read-only ownership helper. Do not apply
gateway NoNewPrivileges/PrivateDevices settings to owner: they would break
that helper and serial/GPIO. Owner migration needs separate reviewed policy.

These are templates, not a tested cross-UID installation. Test actual DAC,
SO_PEERCRED and systemd restrictions in a disposable VM before approved
deployment. Programmatic same-UID Unix-socket fixtures test code, not OS isolation.

For runtime disable, root first creates the inhibit marker through an approved
operations procedure. Owner checks it before command request/tick; gateway
checks before DISARM and while returning status. This blocks subsequent sends
after the check, not packets already written. Then disable frontend flag and
gateway enable option in the approved window. Telemetry remains read-only.

Persistent commands/identity ledger remains outside release directories and
must never be deleted/replaced to change profiles. The new table is additive;
the historical `identities` table is not used or cleared. Old strict-mode
records are exposed as UNKNOWN. A downgrade must leave DISARM OFF; the new
owner changes state labels/adds evidence, so old writable code must not be
started against that DB to resume commands. Preserve the latest ledger/audit.
