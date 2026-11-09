#!/bin/bash
# Print a stable sha256 fingerprint of the remote metadata of one or more artifact URLs.
#
# platform/broadcom/sai-*.dep fold this into the DPKG cache key of the vendor SAI debs
# (SONIC_ONLINE_DEBS), because the URL contents may be replaced in place: a changed
# blob must give a new key. Only the headers that identify the blob are hashed:
# content-length, content-md5, last-modified and etag. Everything else the server
# sends (Date, x-azure-ref, x-ms-request-id, x-cache, ...) differs on every request
# and would make the key change on every build, which rebuilt syncd and the syncd /
# gbsyncd dockers on every CI run (#56).
#
# Header names are matched case-insensitively (HTTP/2 responses are lowercase) and
# emitted lowercased, so the fingerprint does not depend on the protocol version.
#
# REMOTE_ARTIFACT_HEADERS=<file>: fingerprint saved `wget --server-response` output
# instead of fetching (unit tests).
set -o pipefail

fetch_headers() {
    if [ -n "${REMOTE_ARTIFACT_HEADERS:-}" ]; then
        cat -- "$REMOTE_ARTIFACT_HEADERS"
    else
        SKIP_BUILD_HOOK=y wget --spider --server-response "$@" 2>&1
    fi
}

fetch_headers "$@" | tr -d '\r' | awk '
    {
        line = $0
        sub(/^[[:space:]]+/, "", line)
        name = line; sub(/:.*$/, "", name); name = tolower(name)
        if (name ~ /^(content-length|content-md5|last-modified|etag)$/) {
            value = line; sub(/^[^:]*:[[:space:]]*/, "", value); sub(/[[:space:]]+$/, "", value)
            print name ": " value
        }
    }' | sha256sum | awk '{print $1}'
