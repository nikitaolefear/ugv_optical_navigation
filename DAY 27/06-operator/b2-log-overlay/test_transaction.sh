#!/bin/bash
# Offline only. No actual systemctl, Owner or serial.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source ./transaction.sh
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
unit=synthetic
boot=synthetic
drop=$work/ftp.conf
marker=$work/marker
candidate=$work/candidate
Trace=$work/trace
printf '%s\n' BASE_B2 > "$work/B2.conf"
printf '%s\n' CANDIDATE > "$candidate"
stop_owner() { printf 'stop\n' >> "$Trace"; }
systemctl() {
    printf '%s\n' "$*" >> "$Trace"
    [[ ! ( $1 == start && -f "$drop" && $fail == start ) ]]
}
wait_profile() {
    printf 'verify %s\n' "$1" >> "$Trace"
    if [[ $1 == B2 ]]; then
        [[ ! -e "$drop" && ! -e "$marker" && $(cat "$work/B2.conf") == BASE_B2 ]]
    else
        [[ $fail != readiness && -f "$drop" && ! -e "$marker" ]]
    fi
}
for fail in start readiness; do
    : > "$Trace"
    # A separate shell has independent errexit semantics. Invoke without "if".
    export fail unit boot drop marker candidate Trace work
    export -f log_activate log_restore stop_owner systemctl wait_profile
    rc=0
    set +e
    bash -e -c log_activate
    rc=$?
    set -e
    [[ $rc != 0 && ! -e "$drop" && ! -e "$marker" ]]
    [[ $(tail -1 "$Trace") == 'verify B2' ]]
    echo "PASS automatic rollback after $fail failure"
done
fail=none
log_activate
[[ -f "$drop" && ! -e "$marker" ]]
log_restore
[[ $(tail -1 "$Trace") == 'verify B2' ]]
echo 'PASS explicit rollback to pinned B2'
