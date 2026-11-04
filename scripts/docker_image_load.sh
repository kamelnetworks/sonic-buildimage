#!/bin/bash
# Load a saved docker image archive and tag the image it contains.
#
# Usage: docker_image_load.sh <archive.gz> <target-ref> <native-dockerd y|n> [expected-ref]
#
# Used by docker-image-load in slave.mk. The reference inside the archive is
# whatever docker-image-save used when the archive was produced, which is not
# necessarily what the current build computes with docker-get-tag: an archive
# restored from the package cache (SONIC_DPKG_CACHE_METHOD=rwcache) may have
# been saved by another build, with another SONIC_IMAGE_VERSION (another branch
# or CI run), or with the docker in/out of SONIC_PACKAGES_LOCAL
# (INSTALL_DEBUG_TOOLS), i.e. tagged :latest instead of :<version> or the other
# way round. Bazel-built archives are tagged :latest as well. So the reference
# is taken from what `docker load` reports, and <expected-ref> is only used to
# log the difference.
#
# With native dockerd (a daemon shared between builds) the loaded reference is
# removed again after tagging, as before, so parallel builds don't fight over
# <name>:latest.
set -euo pipefail

archive=$1
target=$2
native=${3:-}
expected=${4:-}

output=$(docker load -i "$archive" 2>&1) || { printf '%s\n' "$output"; exit 1; }
printf '%s\n' "$output"

# docker load prints "Loaded image: name:tag" (one line per tag), or
# "Loaded image ID: sha256:..." for an archive saved without a tag.
mapfile -t refs < <(printf '%s\n' "$output" | sed -n 's/^Loaded image: //p')
if [ ${#refs[@]} -gt 0 ]; then
    source=${refs[0]}
else
    source=$(printf '%s\n' "$output" | sed -n 's/^Loaded image ID: //p' | head -n1)
fi
if [ -z "$source" ]; then
    echo "ERROR: cannot tell which image was loaded from $archive" >&2
    exit 1
fi
if [ -n "$expected" ] && [ "$source" != "$expected" ]; then
    echo "Archive $archive contains $source, not $expected (saved by another build); using $source"
fi

echo "Tagging docker image $source as $target"
docker tag "$source" "$target"

if [ "$native" = y ]; then
    for ref in ${refs[@]+"${refs[@]}"}; do
        [ "$ref" = "$target" ] && continue
        echo "Removing docker image $ref"
        docker rmi -f "$ref"
    done
fi
