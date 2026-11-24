#!/bin/bash
set -euo pipefail

service=ugv-hmi.service
package=/opt/ugv-gpio17-hold/package
target=$package/owner/web_hmi/pixhawk_telemetry.py
socket=/run/ugv-pixhawk-control/backend.sock
source_file=$(dirname -- "$0")/pixhawk_telemetry.py

test "$(id -u)" = 0
test -f /run/ugv-disarm-policy/inhibit
test "$(systemctl show "$service" -p WorkingDirectory --value)" = "$package/owner"
profile=$(systemctl show "$service" -p ExecStart --value)
case "$profile" in
    *--externalnav-tx*|*--bench-web-release*|*--isolated-brake-cycle*|*--web-arm*)
        echo 'Unexpected control profile' >&2; exit 1 ;;
esac
[[ "$profile" == *--externalnav-observe-only*--brake-hold-only* ]]
[[ "$profile" == *"--web-disarm-socket $socket"* ]]
printf '%s  %s\n' 2a3e89f48243b74b7b20b74dd1a2fe800b7b6c707b4b31495c7d2ae888f6f710 "$target" | sha256sum -c

backup=$(mktemp -d /var/backups/ugv-mavlink.XXXXXX)
cp -a -- "$target" "$backup/"
printf 'Backup: %s\n' "$backup"
systemctl stop "$service"
test "$(systemctl show "$service" -p MainPID --value)" = 0
test "$(systemctl show "$service" -p ActiveState --value)" = inactive
if [[ -e "$socket" ]]; then
    test -S "$socket"
    test -r /proc/net/unix
    if ! awk -v path="$socket" '$8 == path { found=1 } END { exit found }' /proc/net/unix; then
        echo "Socket is still in use or cannot be checked: $socket" >&2
        exit 1
    fi
    rm -- "$socket"
fi
install -m 0644 -- "$source_file" "$target"
systemctl start "$service"
