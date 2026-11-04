#!/bin/bash
# Exercise slave.mk's docker-image-load recipe against archives whose embedded
# image tag is not what the current build expects.
#
# A cached docker-*.gz (SONIC_DPKG_CACHE_METHOD=rwcache) may have been saved by
# another build: with another SONIC_IMAGE_VERSION, or with/without the docker in
# SONIC_PACKAGES_LOCAL (INSTALL_DEBUG_TOOLS). The archive then carries a tag
# such as docker-macsec:<other-version> or docker-macsec:latest, and
# `make target/docker-macsec.gz-load` must still produce
# docker-macsec-<user>:<usertag> (kamelnetworks/sonic-buildimage#113).
#
# The recipe bodies (docker-get-tag, docker-image-load, MOD_LOCK, MOD_UNLOCK)
# are extracted verbatim from slave.mk / Makefile.cache into a scratch
# Makefile, so the test runs the real recipe without a configured build tree.
# Needs: bash, make, flock, docker (any image name used is unique to this run).
#
# Usage: tests/test_docker_image_load.sh [repo-root]
set -euo pipefail

REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/docker-image-load-test.XXXXXX")
NAME="docker-loadtest-$$"
USERNAME_=tester
USERTAG=t$$
TARGET_REF="$NAME-$USERNAME_:$USERTAG"
CUR_VERSION="202611-master-kamel.54-62be4035b"
OLD_VERSION="verify_66-t810-current-20261101-kamel.53-02ce58b09"
failed=0

cleanup() {
    local repo
    for repo in "$NAME" "$NAME-$USERNAME_"; do
        docker images --format '{{.Repository}}:{{.Tag}}' "$repo" 2>/dev/null \
            | grep -v '<none>' | xargs -r docker rmi -f >/dev/null 2>&1 || true
        docker images -q "$repo" 2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1 || true
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

extract() { # file, define-name
    awk -v d="^define $2\$" '$0 ~ d, /^endef$/' "$1"
}

mkdir -p "$WORK/target" "$WORK/lock" "$WORK/scripts"
[ -e "$REPO/scripts/docker_image_load.sh" ] && cp "$REPO/scripts/docker_image_load.sh" "$WORK/scripts/"
{
    echo '.ONESHELL:'
    echo 'SHELL = /bin/bash'
    echo '.SHELLFLAGS += -e'
    echo 'TARGET_PATH = target'
    echo 'DOCKER_LOCKDIR = lock'
    echo 'DOCKER_LOCKFILE_SUFFIX := access'
    echo 'DOCKER_LOCKFILE_TIMEOUT := 60'
    echo 'LOG = &>> $@.log'
    extract "$REPO/Makefile.cache" MOD_LOCK
    extract "$REPO/Makefile.cache" MOD_UNLOCK
    extract "$REPO/slave.mk" docker-get-tag
    extract "$REPO/slave.mk" docker-image-load
    printf '%s\n' '$(TARGET_PATH)/%.gz-load : $(TARGET_PATH)/%.gz' '	$(call docker-image-load,$*)'
} > "$WORK/Makefile"
for d in docker-get-tag docker-image-load MOD_LOCK MOD_UNLOCK; do
    grep -q "^define $d\$" "$WORK/Makefile" || { echo "could not extract $d"; exit 2; }
done

# A one-file image that needs no network.
printf 'x' > "$WORK/f"
tar -C "$WORK" -cf - f | docker import - "$NAME:base" >/dev/null
BASE_ID=$(docker image inspect -f '{{.Id}}' "$NAME:base")

# make_archive <tag|ID>: save the base image under that tag (or untagged) as target/$NAME.gz
make_archive() {
    if [ "$1" = ID ]; then
        docker save "$BASE_ID" | gzip > "$WORK/target/$NAME.gz"
    else
        docker tag "$NAME:base" "$NAME:$1"
        docker save "$NAME:$1" | gzip > "$WORK/target/$NAME.gz"
        docker rmi "$NAME:$1" >/dev/null
    fi
}

# run_case <description> <archive tag|ID> <native y|n> <in-packages-local y|n> <expect foreign tag gone y|n>
run_case() {
    local desc=$1 tag=$2 native=$3 local_=$4 gone=$5 pkgs="" rc=0
    [ "$local_" = y ] && pkgs="$NAME.gz"
    make_archive "$tag"
    rm -f "$WORK/target/$NAME.gz-load.log"
    docker rmi -f "$TARGET_REF" >/dev/null 2>&1 || true
    make -C "$WORK" --no-print-directory \
        DOCKER_USERNAME="$USERNAME_" DOCKER_USERTAG="$USERTAG" \
        SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD="$native" \
        SONIC_PACKAGES_LOCAL="$pkgs" SONIC_IMAGE_VERSION="$CUR_VERSION" \
        "target/$NAME.gz-load" >"$WORK/make.out" 2>&1 || rc=$?
    local ok=y why=""
    if [ $rc -ne 0 ]; then
        ok=n; why="make exited $rc"
    elif [ "$(docker image inspect -f '{{.Id}}' "$TARGET_REF" 2>/dev/null)" != "$BASE_ID" ]; then
        ok=n; why="$TARGET_REF does not point at the loaded image"
    elif [ "$tag" != ID ] && [ "$gone" = y ] && docker image inspect "$NAME:$tag" >/dev/null 2>&1; then
        ok=n; why="$NAME:$tag was not removed after loading"
    elif [ "$tag" != ID ] && [ "$gone" = n ] && ! docker image inspect "$NAME:$tag" >/dev/null 2>&1; then
        ok=n; why="$NAME:$tag was removed although native dockerd is off"
    fi
    if [ "$ok" = y ]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc ($why)"
        sed 's/^/    /' "$WORK/make.out" "$WORK/target/$NAME.gz-load.log" 2>/dev/null | tail -20
        failed=1
    fi
    [ "$tag" != ID ] && docker rmi -f "$NAME:$tag" >/dev/null 2>&1 || true
    docker rmi -f "$TARGET_REF" >/dev/null 2>&1 || true
}

# The two shapes seen in CI: the cached archive was saved by another build.
run_case "archive saved with another build's version tag (rwcache, other branch)" "$OLD_VERSION" y y y
run_case "archive saved as :latest, now in SONIC_PACKAGES_LOCAL (debug-tools/final mismatch)" latest y y y
run_case "archive saved with a version tag, now not in SONIC_PACKAGES_LOCAL" "$OLD_VERSION" y n y
# Unchanged behaviour.
run_case "archive saved with the expected version tag" "$CUR_VERSION" y y y
run_case "archive saved as :latest, not in SONIC_PACKAGES_LOCAL" latest y n y
run_case "non-native dockerd keeps the :latest tag" latest n n n
run_case "archive without any tag (saved by image ID)" ID y y n

exit $failed
