#!/bin/sh
set -eu

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

usage() {
    cat <<'USAGE'
Usage: install.sh [--uv PATH] [--python PATH] [--index-url URL]
                  [--destdir DIRECTORY] [PACKAGE]

Install sanex as a root systemd service. PACKAGE defaults to "sanex" and may be
an exact requirement or a local wheel path. --destdir stages an installation
without invoking systemd and is intended for packaging and automated checks.
USAGE
}

fail() {
    printf 'install.sh: %s\n' "$1" >&2
    exit 1
}

validate_directory_chain() {
    directory=$(dirname -- "$1")
    while :; do
        [ "$(stat -c %u "$directory")" -eq 0 ] \
            || fail "directory is not owned by root: $directory"
        mode=$(stat -c %a "$directory")
        [ $((0$mode & 022)) -eq 0 ] \
            || fail "directory is writable by group or others: $directory"
        [ "$directory" = / ] && break
        directory=$(dirname -- "$directory")
    done
}

resolve_executable() {
    value=$1
    case "$value" in
        */*) candidate=$value ;;
        *) candidate=$(command -v "$value" 2>/dev/null || true) ;;
    esac
    [ -n "$candidate" ] || fail "executable not found: $value"
    candidate=$(readlink -f "$candidate")
    [ -x "$candidate" ] || fail "not executable: $candidate"
    if [ "$staging" -eq 1 ]; then
        printf '%s\n' "$candidate"
        return
    fi
    [ "$(stat -c %u "$candidate")" -eq 0 ] || fail "not owned by root: $candidate"
    mode=$(stat -c %a "$candidate")
    [ $((0$mode & 022)) -eq 0 ] || fail "writable by group or others: $candidate"
    validate_directory_chain "$candidate"
    printf '%s\n' "$candidate"
}

uv_command=uv
python_command=/usr/bin/python3
index_url=https://pypi.org/simple
package=sanex
package_set=0
destdir=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uv)
            [ "$#" -ge 2 ] || fail "--uv requires a path"
            uv_command=$2
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || fail "--python requires a path"
            python_command=$2
            shift 2
            ;;
        --index-url)
            [ "$#" -ge 2 ] || fail "--index-url requires a URL"
            index_url=$2
            shift 2
            ;;
        --destdir)
            [ "$#" -ge 2 ] || fail "--destdir requires a directory"
            destdir=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            fail "unknown option: $1"
            ;;
        *)
            [ "$package_set" -eq 0 ] || fail "only one PACKAGE may be specified"
            package=$1
            package_set=1
            shift
            ;;
    esac
done

staging=0
if [ -n "$destdir" ]; then
    case "$destdir" in
        /*) ;;
        *) fail "--destdir must be an absolute path" ;;
    esac
    [ "$destdir" != / ] || fail "--destdir must not be /"
    staging=1
    mkdir -p -- "$destdir"
elif [ "$(id -u)" -ne 0 ]; then
    fail "must run as root"
fi
case "$index_url" in
    https://*) ;;
    *) fail "index URL must use HTTPS" ;;
esac
case "${index_url#https://}" in
    ''|*@*|*\#*) fail "index URL must not contain credentials or a fragment" ;;
esac
case "$index_url" in
    *[[:space:]]*) fail "index URL must not contain whitespace" ;;
esac
case "$package" in
    -*) fail "PACKAGE must not start with a hyphen" ;;
esac

uv_path=$(resolve_executable "$uv_command")
python_path=$(resolve_executable "$python_command")
"$python_path" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
    || fail "Python 3.12 or newer is required"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
asset_base_url=https://raw.githubusercontent.com/sanecmp/sanex/main
asset_tmp_dir=
unit_source=$script_dir/systemd/sanex.service
path_source=$script_dir/systemd/sanex.path
logrotate_source=$script_dir/systemd/sanex.logrotate
icon_source=$script_dir/icons/hicolor/scalable/status/sanex-symbolic.svg
warning_icon_source=$script_dir/icons/hicolor/scalable/status/sanex-warning-symbolic.svg
autostart_source=$script_dir/xdg/sanex-indicator.desktop

prepare_asset_tmp_dir() {
    if [ -z "$asset_tmp_dir" ]; then
        asset_tmp_dir=$(mktemp -d)
    fi
}

if [ ! -f "$unit_source" ] || [ ! -f "$path_source" ] || [ ! -f "$logrotate_source" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl is required for remote installation"
    prepare_asset_tmp_dir
    curl -fsSL "$asset_base_url/systemd/sanex.service" -o "$asset_tmp_dir/sanex.service" \
        || fail "unable to download sanex.service"
    curl -fsSL "$asset_base_url/systemd/sanex.path" -o "$asset_tmp_dir/sanex.path" \
        || fail "unable to download sanex.path"
    curl -fsSL "$asset_base_url/systemd/sanex.logrotate" -o "$asset_tmp_dir/sanex.logrotate" \
        || fail "unable to download sanex.logrotate"
    unit_source=$asset_tmp_dir/sanex.service
    path_source=$asset_tmp_dir/sanex.path
    logrotate_source=$asset_tmp_dir/sanex.logrotate
fi
if [ ! -f "$icon_source" ] || [ ! -f "$warning_icon_source" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl is required for remote installation"
    prepare_asset_tmp_dir
    curl -fsSL "$asset_base_url/icons/hicolor/scalable/status/sanex-symbolic.svg" \
        -o "$asset_tmp_dir/sanex-symbolic.svg" \
        || fail "unable to download sanex-symbolic.svg"
    curl -fsSL "$asset_base_url/icons/hicolor/scalable/status/sanex-warning-symbolic.svg" \
        -o "$asset_tmp_dir/sanex-warning-symbolic.svg" \
        || fail "unable to download sanex-warning-symbolic.svg"
    icon_source=$asset_tmp_dir/sanex-symbolic.svg
    warning_icon_source=$asset_tmp_dir/sanex-warning-symbolic.svg
fi
if [ ! -f "$autostart_source" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl is required for remote installation"
    prepare_asset_tmp_dir
    curl -fsSL "$asset_base_url/xdg/sanex-indicator.desktop" \
        -o "$asset_tmp_dir/sanex-indicator.desktop" \
        || fail "unable to download sanex-indicator.desktop"
    autostart_source=$asset_tmp_dir/sanex-indicator.desktop
fi

config_dir=$destdir/opt/sanex/config
state_dir=$destdir/opt/sanex/state
bundle_dir=$destdir/opt/sanex/bundle
bin_dir=$destdir/opt/sanex/bin
log_dir=$destdir/var/log/sanex
icon_dir=$destdir/usr/share/icons/hicolor/scalable/status
autostart_target=$destdir/etc/xdg/autostart/sanex-indicator.desktop
unit_target=$destdir/etc/systemd/system/sanex.service
path_target=$destdir/etc/systemd/system/sanex.path
logrotate_target=$destdir/etc/logrotate.d/sanex

owner_options=
if [ "$staging" -eq 0 ]; then
    owner_options='-o root -g root'
fi

install -d $owner_options -m 0700 \
    "$config_dir" "$config_dir/pki" "$state_dir" "$state_dir/accounts" "$log_dir"
install -d $owner_options -m 0755 \
    "$bundle_dir" "$bin_dir" "$(dirname -- "$unit_target")" \
    "$(dirname -- "$logrotate_target")" "$(dirname -- "$autostart_target")" \
    "$icon_dir"

was_active=0
if [ "$staging" -eq 0 ] && systemctl is-active --quiet sanex.service; then
    was_active=1
    systemctl stop sanex.service
fi

restart_previous_service() {
    if [ "$was_active" -eq 1 ]; then
        systemctl start sanex.service || true
    fi
}

cleanup() {
    restart_previous_service
    if [ -n "$asset_tmp_dir" ]; then
        rm -rf "$asset_tmp_dir"
    fi
}
trap cleanup EXIT

for variable in $(env | sed -n 's/^\(UV_[A-Za-z0-9_]*\)=.*/\1/p'); do
    unset "$variable"
done

UV_TOOL_DIR=$bundle_dir \
UV_TOOL_BIN_DIR=$bin_dir \
"$uv_path" tool install \
    --force \
    --no-cache \
    --no-config \
    --no-sources \
    --no-managed-python \
    --no-python-downloads \
    --no-progress \
    --color never \
    --index-strategy first-index \
    --default-index "$index_url" \
    --python "$python_path" \
    "$package"

[ -x "$bin_dir/sanex" ] || fail "uv did not install the sanex entry point"
for entrypoint in sanex-indicator sanex-window-agent; do
    [ -x "$bin_dir/$entrypoint" ] \
        || fail "uv did not install the $entrypoint entry point"
done

install_tmp=$(mktemp "$config_dir/.install.json.XXXXXX")
"$python_path" - "$uv_path" "$python_path" "$install_tmp" <<'PY'
import json
import sys
from pathlib import Path

_, uv_path, python_path, output_path = sys.argv
Path(output_path).write_text(
    json.dumps(
        {"uv": uv_path, "python": python_path},
        separators=(",", ":"),
    )
    + "\n"
)
PY
install $owner_options -m 0600 "$install_tmp" "$config_dir/install.json"
rm -f "$install_tmp"

install $owner_options -m 0644 "$unit_source" "$unit_target"
install $owner_options -m 0644 "$path_source" "$path_target"
install $owner_options -m 0644 "$logrotate_source" "$logrotate_target"
install $owner_options -m 0644 "$icon_source" "$icon_dir/sanex-symbolic.svg"
install $owner_options -m 0644 \
    "$warning_icon_source" "$icon_dir/sanex-warning-symbolic.svg"
install $owner_options -m 0644 "$autostart_source" "$autostart_target"
if [ "$staging" -eq 0 ]; then
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
    fi
    systemctl daemon-reload
    systemctl enable sanex.service
    systemctl enable --now sanex.path
fi

if [ "$staging" -eq 0 ] && [ -f "$config_dir/config.json" ]; then
    systemctl start sanex.service
    was_active=0
fi
trap - EXIT
if [ -n "$asset_tmp_dir" ]; then
    rm -rf "$asset_tmp_dir"
fi

if [ "$staging" -eq 1 ]; then
    printf 'sanex staged successfully in %s.\n' "$destdir"
else
    printf '%s\n' 'sanex installed successfully.'
fi
if [ ! -f "$config_dir/config.json" ]; then
    printf '%s\n' 'Register this computer in sanea. The service will start automatically.'
fi
