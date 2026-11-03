#!/bin/bash
#
# Unit tests for the functions in config-setup. Run: bash test_config_setup.sh
#
# Only the function definitions are sourced; everything after
# "# read SONiC immutable variables" needs a running switch.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${SCRIPT_DIR}/config-setup"
SOURCE_SCRIPT="$(mktemp)"
CONFIG_CALL_LOG="$(mktemp)"
trap 'rm -f "${SOURCE_SCRIPT}" "${CONFIG_CALL_LOG}"' EXIT

sed '/^# read SONiC immutable variables$/,$d' "$SCRIPT" > "$SOURCE_SCRIPT"
# shellcheck source=/dev/null
source "$SOURCE_SCRIPT"

assert_eq() {
    local description="$1"
    local expected="$2"
    local actual="$3"
    if [[ "$expected" != "$actual" ]]; then
        echo "FAIL: ${description}: expected '${expected}', got '${actual}'" >&2
        exit 1
    fi
}

logger() {
    :
}

# Stub of the sonic-utilities "config" command. CONFIG_RELOAD_VALID=0 makes
# "config reload" fail the way it does when config_db.json fails YANG
# validation, unless -f is given and no file name is given (the only form
# in which -f skips the validation).
config() {
    printf '%s\n' "$*" >> "$CONFIG_CALL_LOG"
    [[ "$1" == "reload" ]] || return 0
    [[ "${CONFIG_RELOAD_VALID}" == "1" ]] && return 0
    local force=0 filename=""
    local arg
    for arg in "${@:2}"; do
        case "$arg" in
            -f|--force) force=1 ;;
            -*) ;;
            *) filename="$arg" ;;
        esac
    done
    if [[ $force == 1 && -z "$filename" ]]; then
        return 0
    fi
    echo "/etc/sonic/config_db.json fails YANG validation! Error: Data Loading Failed"
    echo "Aborted!"
    return 1
}

run_reload_configdb() {
    : > "$CONFIG_CALL_LOG"
    set +e
    reload_configdb "$@" > /dev/null
    RC=$?
    set -e
}

# A valid config is loaded with validation, in one call.
CONFIG_RELOAD_VALID=1
run_reload_configdb
assert_eq "valid config: exit status" 0 "$RC"
assert_eq "valid config: calls" "reload -y -n" "$(cat "$CONFIG_CALL_LOG")"

# The image-upgrade migration path (no file name): a config that fails YANG
# validation is loaded anyway with -f instead of leaving the switch unconfigured.
CONFIG_RELOAD_VALID=0
run_reload_configdb
assert_eq "invalid config, migration: exit status" 0 "$RC"
assert_eq "invalid config, migration: calls" $'reload -y -n\nreload -y -n -f' "$(cat "$CONFIG_CALL_LOG")"

# With an explicit file (factory / ZTP config) -f does not skip validation,
# so the failure is reported without a retry.
CONFIG_RELOAD_VALID=0
run_reload_configdb /etc/sonic/config_db.json
assert_eq "invalid config, explicit file: exit status" 1 "$RC"
assert_eq "invalid config, explicit file: calls" "reload /etc/sonic/config_db.json -y -n" "$(cat "$CONFIG_CALL_LOG")"

echo "PASS: config-setup tests"
