#!/usr/bin/env bash

check_image_storage() {
    local config=${SONIC_WORKSPACE_CONFIG:-/etc/sonic-build-workspace.conf}
    local SONIC_WORKSPACE_MODE= SONIC_WORKSPACE_BUDGET_GIB=
    if [[ ! -r $config ]]; then
        echo "Full image build requires workspace metadata: $config" >&2
        return 1
    fi
    # Provisioned by the pod manifest, not inherited through sudo.
    source "$config"
    if [[ $SONIC_WORKSPACE_MODE != persistent ]]; then
        echo 'Full image build requires the persistent PVC builder; temporary workspaces support component TARGETS only.' >&2
        return 1
    fi
    if [[ ! $SONIC_WORKSPACE_BUDGET_GIB =~ ^[1-9][0-9]{0,6}$ ]]; then
        echo 'SONIC_WORKSPACE_BUDGET_GIB must be a positive integer (GiB).' >&2
        return 1
    fi
    # The manifest budgets one filesystem shared by source, cache and Docker.
    # Reject checkouts elsewhere instead of applying /work metadata to them.
    local source_device work_device docker_device
    source_device=$(stat -c %d .) || return 1
    work_device=$(stat -c %d /work) || return 1
    docker_device=$(stat -c %d /var/lib/docker) || return 1
    if [[ $source_device != "$work_device" || $source_device != "$docker_device" ]]; then
        echo 'Full image checkout, /work and /var/lib/docker must share the configured workspace filesystem.' >&2
        return 1
    fi
    local used_kib free_kib remaining_kib
    read -r used_kib free_kib < <(LC_ALL=C df -Pk . | awk 'NR == 2 {print $3, $4}')
    if [[ ! $used_kib =~ ^[0-9]{1,15}$ || ! $free_kib =~ ^[0-9]{1,15}$ ]]; then
        echo 'Cannot determine workspace filesystem usage.' >&2
        return 1
    fi
    remaining_kib=$(( SONIC_WORKSPACE_BUDGET_GIB * 1024 * 1024 - 10#$used_kib ))
    if (( 10#$free_kib < remaining_kib )); then
        remaining_kib=$((10#$free_kib))
    fi
    if (( remaining_kib < 150 * 1024 * 1024 )); then
        echo 'Full image build requires at least 150 GiB remaining within both the workspace budget and filesystem. Use component TARGETS or enlarge the PVC and budget.' >&2
        return 1
    fi
}
