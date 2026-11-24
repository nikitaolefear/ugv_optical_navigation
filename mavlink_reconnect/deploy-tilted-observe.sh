#!/bin/bash
set -euo pipefail

service=ugv-hmi.service
owner=/opt/ugv-tilted-observe/20261117/package/owner
target=$owner/web_hmi/pixhawk_telemetry.py
socket=/run/ugv-pixhawk-control/backend.sock
backup=/var/backups/ugv-pixhawk-telemetry-before-reconnect.py
stock=2a3e89f48243b74b7b20b74dd1a2fe800b7b6c707b4b31495c7d2ae888f6f710
reconnect=9c01b0de35a31883306c36b9cfe5f2d8303b58041bf5cf63703cfaaf04af5e35

check_hash() {
    printf '%s  %s\n' "$1" "$2" | sha256sum -c
}

test "$(id -u)" = 0
test -f /run/ugv-disarm-policy/inhibit
test "$(systemctl show "$service" -p WorkingDirectory --value)" = "$owner"
profile=$(systemctl show "$service" -p ExecStart --value)
case "$profile" in
    *--externalnav-tx*|*--functional-brake*|*--bench-web-release*|*--enable-bench-hold*|*--web-arm*)
        echo 'Unexpected owner profile' >&2; exit 1 ;;
esac
[[ "$profile" == *--externalnav-observe-only* ]]
[[ "$profile" == *"--web-disarm-socket $socket"* ]]

case "${1:-}" in
    apply)
        source_file=/opt/ugv-gpio17-hold/package/owner/web_hmi/pixhawk_telemetry.py
        check_hash "$reconnect" "$source_file"
        check_hash "$stock" "$target"
        if [[ -e "$backup" ]]; then
            check_hash "$stock" "$backup"
        else
            install -m 0600 -- "$target" "$backup"
        fi ;;
    rollback)
        source_file=$backup
        check_hash "$stock" "$source_file"
        check_hash "$reconnect" "$target" ;;
    *) echo "Usage: $0 apply|rollback" >&2; exit 2 ;;
esac

systemctl stop "$service"
test "$(systemctl show "$service" -p MainPID --value)" = 0
test "$(systemctl show "$service" -p ActiveState --value)" = inactive
if [[ -e "$socket" || -L "$socket" ]]; then
    test -S "$socket"
    test -r /proc/net/unix
    if ! awk -v path="$socket" '$8 == path { found=1 } END { exit found }' /proc/net/unix; then
        echo "Owner socket is still in use or cannot be checked: $socket" >&2
        exit 1
    fi
    rm -- "$socket"
fi
install -m 0644 -- "$source_file" "$target"
systemctl start "$service"
