#!/usr/bin/env bash
set -euo pipefail

# Non-FIPS slave images can link the FIPS Go version even though backports
# installed a newer version. Repair that dangling alias without replacing the
# installed compiler or disturbing a valid (possibly FIPS) toolchain link.
if [[ -L /usr/local/go && ! -e /usr/local/go ]]; then
    go_root=$(env -u GOROOT GOTOOLCHAIN=local /usr/bin/go env GOROOT)
    test -x "$go_root/bin/go"
    ln -sfnT "$go_root" /usr/local/go
    printf 'Repaired /usr/local/go -> %s\n' "$go_root"
fi
