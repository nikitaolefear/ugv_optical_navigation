#!/bin/bash
# Future authorized operation. Never run for offline evaluation.
set -Eeuo pipefail
[[ $EUID == 0 && $# -ge 2 && $2 == --approved ]]
if [[ $1 == activate ]]; then
    [[ $# == 3 && $3 =~ ^[0-9]{1,5}$ && $((10#$3)) -le 32767 ]]
else
    [[ $# == 2 && $1 == rollback ]]
fi
bundle=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
(cd "$bundle" && sha256sum --quiet -c SHA256SUMS)
source "$bundle/transaction.sh"
unit=ugv-hmi.service
state=/run/ugv-stage3-operator
extra=$state/log-profile
marker=$extra/authorization
persistent_root=/var/lib
journal=$persistent_root/stage3-log-disarmed
installed=$extra/payload
dropdir=/run/systemd/system/$unit.d
reference=$dropdir/zzzz-stage3-operator.conf
drop=$dropdir/zzzzz-stage3-log.conf
exec 9>/run/lock/ugv-mission-stage2.lock
flock -n 9
[[ -f "$reference" && ! -L "$reference" ]]
cmp -s "$reference" "$state/B2.conf"
base=$(cat "$state/payload")
[[ "$base" =~ ^/opt/[A-Za-z0-9_.-]+$ && -d "$base" && ! -L "$base" ]]
(cd "$base" && sha256sum --quiet -c "$bundle/BASE_SHA256")
boot=$(cat /proc/sys/kernel/random/boot_id)
mapfile -t meta < <(python3 - "$reference" "$bundle" <<'PY'
import shlex, sys
args = shlex.split(next(s.removeprefix('ExecStart=') for s in open(sys.argv[1])
                        if s.startswith('ExecStart=') and s.strip() != 'ExecStart='))
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from profile import IDENTITIES, resolve_identities

values = {}
for line in Path('/etc/ugv-disarm/identities.env').read_text().splitlines():
    key, _, value = line.strip().partition('=')
    if key in IDENTITIES.values():
        if key in values:
            raise ValueError('Duplicate Owner credential')
        values[key] = value.strip()
args = resolve_identities(args, values)
print(args[0])
for key in ('--web-disarm-socket', '--web-disarm-client-uid', '--web-disarm-client-gid'):
    print(args[args.index(key) + 1])
PY
)
[[ ${#meta[@]} == 4 ]]
py=${meta[0]} socket=${meta[1]} client_uid=${meta[2]} client_gid=${meta[3]}
[[ $py == /* && -x "$py" && $socket == /run/* && $client_uid =~ ^[0-9]+$ && $client_gid =~ ^[0-9]+$ ]]
owner_name=$(systemctl show "$unit" -p User --value)
owner_uid=$(id -u "${owner_name:-root}")

stop_owner() {
    systemctl stop "$unit" || return
    [[ $(systemctl show "$unit" -p MainPID --value) == 0 ]] || return 1
    if [[ -e "$socket" || -L "$socket" ]]; then
        [[ -S "$socket" && ! -L "$socket" ]] || return 1
        awk -v name="$socket" '$8==name {live=1} END {exit live}' /proc/net/unix || return 1
        rm -- "$socket" || return
    fi
}

checked_profile() {
    local expected=$1
    systemctl is-active --quiet "$unit" &&
        setpriv --reuid "$client_uid" --regid "$client_gid" --clear-groups \
            env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$base" \
            "$py" "$base/operator_cli.py" status --socket "$socket" --owner-uid "$owner_uid" --socket-gid "$client_gid" |
        python3 -c 'import json,sys
s=json.load(sys.stdin)
ok=( s["readonly"] is True and s["tx_enabled"] is False
    and 0 <= s["heartbeat_age"] < 1.5
    and all(s[k] is True for k in ("hold","disarmed","inhibit","stop_latch","hold_only"))
    and s["mode"]=="B2")
sys.exit(not ok)' "$expected" &&
        curl -fsS --noproxy '*' --max-time 1 http://127.0.0.1:8084/api/externalnav |
        python3 -c 'import json,sys; v=json.load(sys.stdin); sys.exit(not (v["sent"]==0 and v["tx_enabled"] is False))' &&
        { [[ $expected == B2 ]] || log_status | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(not(s["profile"]=="B2-LOG" and len(s["identity"])==4))'; }
}

log_status() {
    local path
    path=$(cat "$installed") || return
    [[ "$path" =~ ^/opt/stage3-b2-log\.[[:alnum:]]+$ ]] || return 1
    setpriv --reuid "$client_uid" --regid "$client_gid" --clear-groups \
        env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$path" "$py" "$path/log_client.py" status \
        --socket "$socket" --owner-uid "$owner_uid" --socket-gid "$client_gid"
}

wait_profile() {
    for _ in {1..25}; do
        if checked_profile "$1" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

if [[ $1 == rollback ]]; then
    [[ -f "$drop" && ! -L "$drop" && -f "$extra/candidate.conf" ]]
    cmp -s "$drop" "$extra/candidate.conf"
    rm -f -- "$marker"
    if systemctl is-active --quiet "$unit"; then
        log_status | python3 -c 'import json,sys;s=json.load(sys.stdin);sys.exit(not(s["active"] is None and s["queued"]==0 and s["dataflash"]=="IDLE"))'
    fi
    log_restore
    exit
fi

[[ ! -e "$drop" && ! -L "$drop" && ! -e "$marker" && ! -L "$marker" ]]
[[ -f "$state/isolation" && ! -L "$state/isolation" &&
   $(cat "$state/isolation") == "B2 ISOLATED $boot" &&
   $(stat -c '%u:%a' "$state/isolation") == 0:644 ]]
wait_profile B2
[[ ! -e "$dropdir/zzzzz-stage3-ftp.conf" && ! -L "$journal" ]]
if [[ ! -d "$journal" ]]; then
    install -d -o "$owner_uid" -m 0700 "$journal"
fi
[[ $(stat -c '%u:%a' "$journal") == "$owner_uid:700" ]]
[[ ! -L "$extra" ]]
install -d -o root -g root -m 0755 "$extra"
payload=$(mktemp -d /opt/stage3-b2-log.XXXXXX)
chmod 0755 "$payload"
# The baseline stays in place. Only overlay files are materialized.
cp -as "$base/." "$payload/"
for file in disarm_control/{ipc,log_queue,log_disarmed,owner_port}.py owner/stop_latch_test/hold_runner.py; do
    install -m 0644 -- "$bundle/overlay/$file" "$payload/$file"
done
install -m 0644 -- "$bundle/log_client.py" "$payload/log_client.py"
candidate=$extra/candidate.conf
python3 "$bundle/profile.py" "$reference" "$base" "$payload" "$marker" "$journal" "$3" > "$candidate"
printf '%s\n' "$payload" > "$installed"
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$payload/owner:$payload/optical:$payload" \
    "$py" - "$candidate" "$owner_uid" "$client_uid" "$client_gid" "$bundle" <<'PY'
import os, shlex, sys
from stop_latch_test.hold_runner import parse_args

sys.path.insert(0, sys.argv[5])
from profile import IDENTITIES, resolve_identities

argv = shlex.split(open(sys.argv[1]).read().splitlines()[-1].removeprefix('ExecStart='))
argv = resolve_identities(argv, dict(zip(IDENTITIES.values(), sys.argv[3:5])))
# Run parser with the same effective uid test as the service.
uid = int(sys.argv[2])
if os.getuid() != uid:
    os.setuid(uid)
args = parse_args(argv[4:])
assert args.stage3_operator_mode == 'B2' and args.stage3_log_approval_file
PY
log_activate
printf 'B2-LOG active; operator IPC client at %s/log_client.py\n' "$payload"
