#!/usr/bin/env bash
set -euo pipefail

# Run as builder in the Ubuntu pod. Each platform needs its own checkout and daemon.
if [[ $(id -u) == 0 ]]; then
    echo 'Run this script as builder, for example: sudo -u builder tools/kubernetes/build.sh' >&2
    exit 1
fi
cd "$(git rev-parse --show-toplevel)"
export BLDENV=trixie
platform=${PLATFORM:-marvell-teralynx}
mkdir -p /work/logs /work/cache
# Do not overwrite an existing local configuration.
if [[ ! -e rules/config.user ]]; then
    cat > rules/config.user <<'CONFIG'
DEFAULT_CONTAINER_REGISTRY = docker.io
INCLUDE_FIPS = n
SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD = y
SONIC_CONFIG_BUILD_JOBS = 2
SONIC_CONFIG_MAKE_JOBS = 4
SONIC_DPKG_CACHE_METHOD = rwcache
SONIC_DPKG_CACHE_SOURCE = /work/cache
SONIC_BUILD_MEMORY = 28g
CONFIG
fi

git submodule update --init --recursive --jobs "${FETCH_JOBS:-8}"
make NOBOOKWORM=1 init
make NOBOOKWORM=1 configure PLATFORM="$platform" "$@"
if [[ -n ${TARGETS:-} ]]; then
    # TARGETS is a whitespace-separated list of make targets, never shell code.
    read -r -a targets <<< "$TARGETS"
else
    # A full image requires the persistent workspace, not the preflight emptyDir.
    free_kib=$(df -Pk . | awk 'NR == 2 {print $4}')
    if (( free_kib < 150 * 1024 * 1024 )); then
        echo 'Full image build requires at least 150 GiB free. Use the PVC builder or specify component TARGETS.' >&2
        exit 1
    fi
    targets=("target/sonic-${platform}.bin")
fi
make NOBOOKWORM=1 "${targets[@]}" "$@"
