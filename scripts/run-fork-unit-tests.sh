#!/bin/bash
# Discover and run the hardware-free unit tests of this tree, without a build.
#
# Convention (see .github/workflows/README.md, "Fork unit tests"):
#   device/<vendor>/<platform>/tests/test_*.py
#   platform/**/tests/test_*.py
#   tests/test_*.py
#   files/**/tests/test_*.sh
# Only the files directly in a tests/ directory are picked up. Each file runs
# in its own process (python -m pytest for .py, bash for .sh), so tests that
# stub modules in sys.modules, or share a file name with another platform's
# test, can't affect each other. Python tests get on PYTHONPATH:
#   - the code under test: the directory that contains tests/ (or, if that is
#     a package, the first directory above it that isn't one);
#   - the pinned submodules and in-tree packages listed in PYPATH.
# PyPI dependencies are in scripts/fork-unit-tests-requirements.txt.
# Exits non-zero if any file fails or collects no tests.
#
# Usage: scripts/run-fork-unit-tests.sh [--list] [--no-submodules]
#   --list           print the discovered test files and exit
#   --no-submodules  don't run 'git submodule update' (already checked out)

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
ROOT=$PWD

# Pinned submodules the Python tests import, fetched shallow at the commit
# this tree pins. Add one here (and to PYPATH) when a new test needs it.
SUBMODULES=(
    src/sonic-sairedis
    src/sonic-platform-common
    src/sonic-platform-daemons
    # tests/test_xgs_module_order.py applies platform/broadcom/saibcm-modules.patch
    # to this submodule's debian/ files; nothing imports it.
    platform/broadcom/saibcm-modules
    src/sonic-utilities
)
PYPATH=(
    src/sonic-platform-common
    src/sonic-py-common
    src/sonic-platform-daemons/sonic-xcvrd
)

# tests/ directories (relative to the repository root) that are not run.
# Each entry says why; remove it once the reason is gone.
EXCLUDE=(
    # Upstream suites that import swsscommon (or other packages that only a
    # SONiC build provides); upstream's CI runs them in a full build.
    platform/aspeed/sonic-platform-modules-nvidia-bmc/ast2700/tests
    platform/mellanox/integration-scripts/tests
    platform/mellanox/mlnx-platform-api/tests
    platform/mellanox/platform-utils/tests
    platform/nvidia-bluefield/platform-api/tests
    # Red on 202611-master: the AS5712-54X pd-plugin.json still has lambda fan
    # formulas (#26). Re-enable when #92 / #112 drops the AS5712 patches.
    platform/pddf/platform-api-pddf-base/tests
)

PYTHON=${PYTHON:-python3}
list_only=0
init_submodules=1
for arg in "$@"; do
    case "$arg" in
        --list) list_only=1 ;;
        --no-submodules) init_submodules=0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

is_excluded() {
    local d
    for d in "${EXCLUDE[@]}"; do
        [[ "$1" == "$d" ]] && return 0
    done
    return 1
}

discover_dirs() {
    {
        find device -mindepth 3 -maxdepth 3 -type d -name tests
        find platform -type d -name tests
        [[ -d tests ]] && echo tests
        find files -type d -name tests
    } 2>/dev/null | sort -u
}

tests=()
skipped=()
while read -r dir; do
    [[ -z "$dir" ]] && continue
    if is_excluded "$dir"; then
        skipped+=("$dir")
        continue
    fi
    case "$dir" in
        files/*) pattern='test_*.sh' ;;
        *) pattern='test_*.py' ;;
    esac
    while read -r f; do tests+=("$f"); done \
        < <(find "$dir" -maxdepth 1 -type f -name "$pattern" | sort)
done < <(discover_dirs)

echo "Discovered ${#tests[@]} test files (${#skipped[@]} directories excluded):"
printf '  %s\n' "${tests[@]}"
if (( ${#skipped[@]} )); then
    printf '  excluded: %s\n' "${skipped[@]}"
fi
(( list_only )) && exit 0
if (( ${#tests[@]} == 0 )); then
    echo "No tests found" >&2
    exit 1
fi

if (( init_submodules )); then
    git submodule update --init --depth 1 "${SUBMODULES[@]}" || exit 1
fi
for p in "${PYPATH[@]}"; do
    if [[ ! -d "$p" || -z "$(ls -A "$p")" ]]; then
        echo "missing $p (pinned submodule not checked out?)" >&2
        exit 1
    fi
done
BASE_PYTHONPATH=$(IFS=:; echo "${PYPATH[*]/#/$ROOT/}")${PYTHONPATH:+:$PYTHONPATH}
export PYTHONDONTWRITEBYTECODE=1

echo "$("$PYTHON" --version 2>&1)"
for s in "${SUBMODULES[@]}"; do
    echo "  $s @ $(git -C "$s" rev-parse --short=12 HEAD 2>/dev/null || echo '?')"
done

# Directory holding the code a tests/ directory tests.
code_dir() {
    local d
    d=$(dirname "$1")
    while [[ -f "$d/__init__.py" && "$d" != . ]]; do
        d=$(dirname "$d")
    done
    echo "$ROOT/$d"
}

results=()
failed=0
for t in "${tests[@]}"; do
    echo
    echo "::group::$t"
    if [[ $t == *.py ]]; then
        PYTHONPATH="$(code_dir "$(dirname "$t")"):$BASE_PYTHONPATH" \
            "$PYTHON" -m pytest -p no:cacheprovider -o addopts= -v "$t"
        rc=$?   # 5 = no tests collected, a failure here too
    else
        bash "$t"
        rc=$?
    fi
    echo "::endgroup::"
    if (( rc == 0 )); then
        results+=("PASS  $t")
    else
        results+=("FAIL  $t (exit $rc)")
        echo "::error file=$t::$t failed (exit $rc)"
        failed=$((failed + 1))
    fi
done

passed=$(( ${#tests[@]} - failed ))
echo
echo "Summary: $passed/${#tests[@]} test files passed"
printf '  %s\n' "${results[@]}"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        echo "### Fork unit tests: $passed/${#tests[@]} files passed"
        echo
        printf -- '- `%s`\n' "${results[@]}"
        if (( ${#skipped[@]} )); then
            echo
            echo "Excluded (see EXCLUDE in scripts/run-fork-unit-tests.sh):"
            printf -- '- `%s`\n' "${skipped[@]}"
        fi
    } >> "$GITHUB_STEP_SUMMARY"
fi
(( failed == 0 ))
