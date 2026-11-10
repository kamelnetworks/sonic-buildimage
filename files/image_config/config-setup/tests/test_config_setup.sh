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
FAKE_DIR="$(mktemp -d)"
trap 'rm -rf "${SOURCE_SCRIPT}" "${CONFIG_CALL_LOG}" "${FAKE_DIR}"' EXIT

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

# CONFIG_DB of the fake switch, as the JSON sonic-cfggen --print-data prints.
FAKE_CONFIG_DB="${FAKE_DIR}/configdb.json"
echo '{}' > "$FAKE_CONFIG_DB"

# "sonic-cfggen -n asicN -d --print-data" prints ${FAKE_DIR}/configdb_asicN.json.
sonic-cfggen() {
    local namespace=""
    while [[ $# -gt 0 ]]; do
        [[ "$1" == "-n" ]] && namespace="$2"
        shift
    done
    if [[ -n "$namespace" ]]; then
        cat "${FAKE_DIR}/configdb_${namespace}.json"
    else
        cat "$FAKE_CONFIG_DB"
    fi
}

# db_migrator.py -o migrate as far as these tests are concerned: drop
# valid_speeds from PORT (sonic-utilities patch 0004) and set the schema
# version.
fake_db_migrator() {
    echo "db_migrator.py -o migrate" >> "$CONFIG_CALL_LOG"
    jq 'if .PORT then .PORT |= map_values(del(.valid_speeds)) else . end
        | .VERSIONS.DATABASE.VERSION = "version_202605_01"' \
        "$FAKE_CONFIG_DB" > "${FAKE_CONFIG_DB}.new"
    mv "${FAKE_CONFIG_DB}.new" "$FAKE_CONFIG_DB"
}

# Stub of the sonic-utilities "config" command. CONFIG_RELOAD_VALID=0 makes
# "config reload" fail the way it does when config_db.json fails YANG
# validation, unless -f is given and no file name is given (the only form
# in which -f skips the validation). CONFIG_RELOAD_VALID=file fails it only
# when the file has a PORT valid_speeds field (#136); there a successful
# reload loads the file into the fake CONFIG_DB and then runs db_migrator on
# it, as the real command does (migrate_db_to_lastest at the end of reload()
# in sonic-utilities config/main.py), and "config save" writes the fake
# CONFIG_DB back to CONFIG_DB_JSON. CONFIG_RELOAD_VALID=never fails every
# reload, -f or not.
config() {
    printf '%s\n' "$*" >> "$CONFIG_CALL_LOG"
    if [[ "$1" == "save" ]]; then
        [[ "${CONFIG_RELOAD_VALID}" == "file" ]] && cp "$FAKE_CONFIG_DB" "$CONFIG_DB_JSON"
        return 0
    fi
    [[ "$1" == "reload" ]] || return 0
    [[ "${CONFIG_RELOAD_VALID}" == "1" ]] && return 0
    if [[ "${CONFIG_RELOAD_VALID}" == "never" ]]; then
        echo "Aborted!"
        return 1
    fi
    local force=0 filename=""
    local arg
    for arg in "${@:2}"; do
        case "$arg" in
            -f|--force) force=1 ;;
            -*) ;;
            *) filename="$arg" ;;
        esac
    done
    if [[ "${CONFIG_RELOAD_VALID}" == "file" ]]; then
        if [[ $force == 1 && -z "$filename" ]] || \
           ! jq -e '[.PORT[]? | has("valid_speeds")] | any' "${filename:-$CONFIG_DB_JSON}" > /dev/null; then
            cp "${filename:-$CONFIG_DB_JSON}" "$FAKE_CONFIG_DB"
            fake_db_migrator
            return 0
        fi
        echo "All Keys are not parsed in PORT ... 'valid_speeds' ... fails YANG validation!"
        echo "Aborted!"
        return 1
    fi
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

# Image-upgrade migration (do_config_migration) with a config_db.json from the
# old image. CONFIG_DB_JSON points at a temporary file; /etc/sonic/old_config
# doesn't exist here, so nothing is copied.
CONFIG_DB_JSON="${FAKE_DIR}/config_db.json"
WARM_BOOT=false
NUM_ASIC=1

CONFIG_DB_PATH="${FAKE_DIR}/"

# The real do_db_migration runs /usr/local/bin/db_migrator.py and sets
# CONFIG_DB_INITIALIZED with sonic-db-cli.
do_db_migration() {
    fake_db_migrator
}

run_config_migration() {
    : > "$CONFIG_CALL_LOG"
    set +e
    ( do_config_migration > /dev/null )
    RC=$?
    set -e
}

# #136 with the #96 fallback: the old file has valid_speeds, so it is loaded
# with -f. "config reload" itself already migrates CONFIG_DB, so by the time
# config-setup runs do_db_migration there is nothing left to change; the save
# must still happen because CONFIG_DB no longer matches the file. Then the
# next "config reload" by hand passes without any manual step.
CONFIG_RELOAD_VALID=file
echo '{
    "PORT": {
        "Ethernet0": {"lanes": "1,2,3,4", "speed": "100000", "valid_speeds": "100000,40000", "admin_status": "up"},
        "Ethernet4": {"lanes": "5,6,7,8", "speed": "40000", "valid_speeds": "100000,40000"}
    },
    "VERSIONS": {"DATABASE": {"VERSION": "version_202605_01"}}
}' > "$CONFIG_DB_JSON"
run_config_migration
assert_eq "valid_speeds upgrade: exit status" 0 "$RC"
assert_eq "valid_speeds upgrade: calls" \
    $'reload -y -n\nreload -y -n -f\ndb_migrator.py -o migrate\ndb_migrator.py -o migrate\nsave -y' \
    "$(cat "$CONFIG_CALL_LOG")"
assert_eq "valid_speeds upgrade: field left in saved file" false \
    "$(jq '[.PORT[] | has("valid_speeds")] | any' "$CONFIG_DB_JSON")"
assert_eq "valid_speeds upgrade: other fields kept" '"up"' \
    "$(jq '.PORT.Ethernet0.admin_status' "$CONFIG_DB_JSON")"
set +e; config reload -y > /dev/null; RC=$?; set -e
assert_eq "valid_speeds upgrade: later config reload" 0 "$RC"

# A config the migrator doesn't change is not saved, so the upgrade leaves
# the operator's file as it was, even if its key order and formatting differ
# from what "config save" would write.
echo '{"VERSIONS": {"DATABASE": {"VERSION": "version_202605_01"}},
"PORT": {"Ethernet4": {"speed": "40000", "lanes": "5,6,7,8"},
"Ethernet0": {"speed": "100000", "lanes": "1,2,3,4", "admin_status": "up"}}}' > "$CONFIG_DB_JSON"
cp "$CONFIG_DB_JSON" "${FAKE_DIR}/clean.json"
run_config_migration
assert_eq "clean upgrade: exit status" 0 "$RC"
assert_eq "clean upgrade: calls" $'reload -y -n\ndb_migrator.py -o migrate\ndb_migrator.py -o migrate' \
    "$(cat "$CONFIG_CALL_LOG")"
assert_eq "clean upgrade: file unchanged" "" "$(cmp "$CONFIG_DB_JSON" "${FAKE_DIR}/clean.json" 2>&1)"

# A migration that changes the config (here: the schema version) is saved
# even without valid_speeds.
echo '{"PORT": {"Ethernet0": {"speed": "100000"}},
"VERSIONS": {"DATABASE": {"VERSION": "version_202505_01"}}}' > "$CONFIG_DB_JSON"
run_config_migration
assert_eq "version upgrade: calls" $'reload -y -n\ndb_migrator.py -o migrate\ndb_migrator.py -o migrate\nsave -y' \
    "$(cat "$CONFIG_CALL_LOG")"
assert_eq "version upgrade: saved version" '"version_202605_01"' "$(jq '.VERSIONS.DATABASE.VERSION' "$CONFIG_DB_JSON")"

# If the old file can't be loaded at all, CONFIG_DB doesn't hold the
# operator's config, so it must not be saved over the file.
CONFIG_RELOAD_VALID=never
echo '{"PORT": {"Ethernet0": {"valid_speeds": "100000"}}}' > "$CONFIG_DB_JSON"
cp "$CONFIG_DB_JSON" "${FAKE_DIR}/unloadable.json"
echo '{}' > "$FAKE_CONFIG_DB"
run_config_migration
assert_eq "failed load: calls" $'reload -y -n\nreload -y -n -f\ndb_migrator.py -o migrate' "$(cat "$CONFIG_CALL_LOG")"
assert_eq "failed load: file unchanged" "" "$(cmp "$CONFIG_DB_JSON" "${FAKE_DIR}/unloadable.json" 2>&1)"
CONFIG_RELOAD_VALID=file

# Multi-ASIC: a difference in one ASIC namespace alone counts.
NUM_ASIC=2
echo '{"PORT": {}}' > "$CONFIG_DB_JSON"
echo '{"PORT": {}}' > "$FAKE_CONFIG_DB"
for ns in 0 1; do
    echo '{"PORT": {"Ethernet0": {"speed": "100000"}}}' > "${FAKE_DIR}/config_db${ns}.json"
    cp "${FAKE_DIR}/config_db${ns}.json" "${FAKE_DIR}/configdb_asic${ns}.json"
done
set +e; config_db_matches_files; RC=$?; set -e
assert_eq "multi-asic, all equal: matches" 0 "$RC"
echo '{"PORT": {"Ethernet0": {"speed": "100000", "valid_speeds": "100000"}}}' > "${FAKE_DIR}/config_db1.json"
set +e; config_db_matches_files; RC=$?; set -e
assert_eq "multi-asic, asic1 differs: matches" 1 "$RC"
NUM_ASIC=1

# Warm reboot into a new image: CONFIG_DB comes from the redis dump, not from
# the file, so it is migrated but neither reloaded nor saved.
WARM_BOOT=true
echo '{"PORT": {"Ethernet0": {"valid_speeds": "100000"}}}' > "$FAKE_CONFIG_DB"
load_cold_booted_namespaces() {
    :
}
run_config_migration
assert_eq "warm upgrade: exit status" 0 "$RC"
assert_eq "warm upgrade: calls" "db_migrator.py -o migrate" "$(cat "$CONFIG_CALL_LOG")"
WARM_BOOT=false

echo "PASS: config-setup tests"
