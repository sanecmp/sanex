#!/bin/sh
set -eu

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

fail() {
    printf "uninstall.sh: %s\n" "$1" >&2
    exit 1
}

stop_unit() {
    load_state=$(systemctl show --property=LoadState --value "$1")

    if [ "$load_state" != not-found ]; then
        systemctl disable --now "$1"
    fi
}

stop_indicators() {
    # Match only installed entry points, not development tools or other Python processes.
    indicator_path="/opt/sanex/(bin|bundle/sanecmp-sanex/bin)/sanex-indicator"
    indicator_pattern="^(([^[:space:]]*/)?python[0-9.]*[[:space:]]+)?$indicator_path([[:space:]]|$)"
    signal_status=0
    pkill --signal TERM --full "$indicator_pattern" || signal_status=$?
    [ "$signal_status" -le 1 ] || fail "unable to stop sanex indicators"
    attempt=0

    while :; do
        process_status=0
        pgrep --full "$indicator_pattern" >/dev/null || process_status=$?

        case "$process_status" in
            0) ;;
            1) return 0 ;;
            *) fail "unable to check for running sanex indicators" ;;
        esac

        [ "$attempt" -lt 10 ] || fail "sanex indicators did not stop; no application data was removed"
        sleep 1
        attempt=$((attempt + 1))
    done
}

[ "$#" -eq 0 ] || fail "usage: uninstall.sh (no arguments)"
[ "$(id -u)" -eq 0 ] || fail "must run as root"

for command in systemctl pgrep pkill; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

cat <<'NOTICE'
This will stop and permanently remove the local sanex installation:
  /opt/sanex (application, registration, private keys, counters and events)
  /var/log/sanex (technical logs)
  /run/sanex (runtime files)
  /etc/systemd/system/sanex.service
  /etc/systemd/system/sanex.path
  /etc/systemd/system/multi-user.target.wants/sanex.service
  /etc/systemd/system/multi-user.target.wants/sanex.path
  /etc/xdg/autostart/sanex-indicator.desktop
  /etc/logrotate.d/sanex
  /usr/share/icons/hicolor/scalable/status/sanex-symbolic.svg
  /usr/share/icons/hicolor/scalable/status/sanex-warning-symbolic.svg

Running sanex indicators will be stopped without ending user sessions.
Back up any data you want to keep. This does not contact or change sanea.
NOTICE
printf "Type sanex to confirm permanent removal: " >&2
confirmation=

# Read from the terminal even when the script itself arrives through a pipe.
if ! IFS= read -r confirmation < /dev/tty || [ "$confirmation" != sanex ]; then
    printf "Uninstallation cancelled; nothing was changed.\n" >&2
    exit 0
fi

# Stop the path watcher first so it cannot reactivate the service.
stop_unit sanex.path
stop_unit sanex.service
rm -f -- /etc/xdg/autostart/sanex-indicator.desktop
stop_indicators

rm -f -- \
    /etc/systemd/system/sanex.service \
    /etc/systemd/system/sanex.path \
    /etc/systemd/system/multi-user.target.wants/sanex.service \
    /etc/systemd/system/multi-user.target.wants/sanex.path \
    /etc/logrotate.d/sanex \
    /usr/share/icons/hicolor/scalable/status/sanex-symbolic.svg \
    /usr/share/icons/hicolor/scalable/status/sanex-warning-symbolic.svg
systemctl daemon-reload
rm -rf -- /opt/sanex /var/log/sanex /run/sanex

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    if ! gtk-update-icon-cache -f -t /usr/share/icons/hicolor; then
        printf "Unable to refresh the icon cache; refresh it manually.\n" >&2
    fi
fi

printf "sanex uninstalled successfully.\n" >&2
