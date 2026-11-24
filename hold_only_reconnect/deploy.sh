#!/bin/bash
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
service=ugv-hmi.service
bundle=/opt/ugv-hold-only-reconnect/package
prior=/opt/ugv-tilted-observe/20261117/package/owner
dropin=/run/systemd/system/$service.d/zzzzzz-ugv-hold-only-reconnect.conf
socket=/run/ugv-pixhawk-control/backend.sock

test "$(id -u)" = 0
test -f /run/ugv-disarm-policy/inhibit

stop_owner() {
    systemctl stop "$service"
    test "$(systemctl show "$service" -p MainPID --value)" = 0
    test "$(systemctl show "$service" -p ActiveState --value)" = inactive
    if [[ -e "$socket" || -L "$socket" ]]; then
        test -S "$socket"
        test ! -L "$socket"
        test -r /proc/net/unix
        awk -v p="$socket" '$8==p {busy=1} END {exit busy}' /proc/net/unix
        rm -- "$socket"
    fi
}

case "${1:-}" in
apply-hold-only)
    test "$(systemctl show "$service" -p WorkingDirectory --value)" = "$prior"
    test ! -e "$dropin"
    test ! -L "$dropin"
    paths=$(systemctl show "$service" -p DropInPaths --value)
    case "$paths" in */run/systemd/*) echo 'Unexpected runtime override' >&2; exit 1;; esac
    (cd "$here" && sha256sum -c profile.sha256)
    (cd "$here/package" && sha256sum -c "$here/package.sha256")
    if [[ ! -e "$bundle" ]]; then
        stage=$(mktemp -d /opt/ugv-hold-only-reconnect.XXXXXX)
        trap 'rm -rf -- "$stage"' EXIT
        cp -R -- "$here/package" "$stage/package"
        find "$stage/package" -type l -print -quit | grep -q . && exit 1
        chmod -R go-w "$stage/package"
        (cd "$stage/package" && sha256sum -c "$here/package.sha256")
        chmod 0755 "$stage"
        mv -T -- "$stage" "${bundle%/package}"
        trap - EXIT
    fi
    (cd "$bundle" && sha256sum -c "$here/package.sha256")
    boot=$(cat /proc/sys/kernel/random/boot_id)
    [[ "$boot" =~ ^[0-9a-f-]{36}$ ]]
    backup=$(mktemp -d /var/backups/ugv-hold-only-reconnect.XXXXXX)
    systemctl cat "$service" > "$backup/previous-unit.txt"
    systemctl show "$service" -p ExecStart -p WorkingDirectory -p DropInPaths > "$backup/previous-effective.txt"
    sed "s/__CURRENT_PI_BOOT_ID__/$boot/" "$here/hold-only.conf.in" > "$backup/applied.conf"
    printf 'Explicit HOLD-only: APPLY request only, ExternalNav TX OFF. Backup: %s\n' "$backup"
    stop_owner
    install -d -m 0755 -- "$(dirname "$dropin")"
    install -m 0644 -- "$backup/applied.conf" "$dropin"
    systemctl daemon-reload
    test "$(systemctl show "$service" -p WorkingDirectory --value)" = "$bundle/owner"
    profile=$(systemctl show "$service" -p ExecStart --value)
    [[ "$profile" == *--brake-hold-only* && "$profile" == *--externalnav-observe-only* ]]
    case "$profile" in *--externalnav-tx*|*--web-arm*|*--bench-web-release*) exit 1;; esac
    systemctl start "$service"
    ;;
rollback)
    backup=${2:?Specify the backup path printed by apply-hold-only}
    cmp -- "$backup/applied.conf" "$dropin"
    stop_owner
    rm -- "$dropin"
    systemctl daemon-reload
    test "$(systemctl show "$service" -p WorkingDirectory --value)" = "$prior"
    systemctl cat "$service" | cmp -- "$backup/previous-unit.txt" -
    systemctl start "$service"
    ;;
*) echo "Usage: $0 apply-hold-only | rollback BACKUP" >&2; exit 2;;
esac
