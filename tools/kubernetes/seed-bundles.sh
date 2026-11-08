#!/usr/bin/env bash
set -euo pipefail
# From a clone of upstream SONiC, import local migration commits before publication.
# Usage: seed-bundles.sh /path/to/bundle-directory
bundles=$(realpath "${1:?Supply the directory containing the migration Git bundles}")
cd "$(git rev-parse --show-toplevel)"
git fetch "$bundles/sonic-buildimage.bundle" kamel/202611-master
if git show-ref --verify --quiet refs/heads/kamel/202611-master; then
    git switch kamel/202611-master
    git merge --ff-only FETCH_HEAD
else
    git switch -c kamel/202611-master FETCH_HEAD
fi
for component in sonic-linux-kernel sonic-host-services sonic-swss; do
    if ! git -C "src/$component" rev-parse --show-prefix >/dev/null 2>&1 || [[ ! -e src/$component/.git ]]; then
        git clone --filter=blob:none "https://github.com/sonic-net/$component" "src/$component"
    fi
    git -C "src/$component" fetch "$bundles/$component.bundle" kamel/202611-master
    expected=$(git rev-parse "HEAD:src/$component")
    git -C "src/$component" checkout --detach "$expected"
done
git submodule update --init --recursive --jobs "${FETCH_JOBS:-8}"
