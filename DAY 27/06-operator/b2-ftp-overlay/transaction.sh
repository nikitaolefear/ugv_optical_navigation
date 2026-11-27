# Only used by the future deploy helper. Tests substitute these callbacks.
ftp_restore() {
    stop_owner &&
        rm -f -- "$drop" "$marker" &&
        systemctl daemon-reload &&
        systemctl start "$unit" &&
        wait_profile B2
}

ftp_activate() (
    set -Eeuo pipefail
    recover() {
        local rc=$?
        trap - EXIT INT TERM
        if ((rc)); then
            if ftp_restore; then
                echo 'B2 restored after B2-FTP activation failure' >&2
            else
                echo 'B2 restore failed; leave traction isolated and inspect Owner' >&2
            fi
        fi
        exit "$rc"
    }
    trap recover EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    stop_owner
    install -m 0644 -- "$candidate" "$drop"
    printf 'B2-FTP ISOLATED %s\n' "$boot" > "$marker"
    chmod 0644 "$marker"
    systemctl daemon-reload
    systemctl start "$unit"
    wait_profile B2-FTP
)
