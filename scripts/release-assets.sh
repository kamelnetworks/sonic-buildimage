#!/usr/bin/env bash
# Source from the build workflow. Stage assets in ./assets and record every
# remote filename in uploaded-assets.txt. The optional limit allows small tests.
prepare_release_asset() {
    local source=$1 name=$2 limit=${3:-2147483648}
    local size part_bytes part
    size=$(stat -c %s "$source")
    mkdir -p assets
    if (( size < limit )); then
        ln -f "$source" "assets/$name"
        printf '%s\n' "$name" >> uploaded-assets.txt
        return
    fi

    # Leave headroom below GitHub's 2 GiB per-file cap. Use byte splitting,
    # since installers and debug containers already contain compressed data.
    part_bytes=$((1900 * 1024 * 1024))
    (( part_bytes < limit )) || part_bytes=$((limit - 1))
    echo "Splitting $name ($size bytes) into $part_bytes-byte release assets"
    split --bytes="$part_bytes" --numeric-suffixes=0 --suffix-length=3 \
        "$source" "assets/$name.part-"
    : > "assets/$name.parts"
    for part in assets/"$name".part-[0-9][0-9][0-9]; do
        printf '%s\n' "${part#assets/}" >> "assets/$name.parts"
        printf '%s\n' "${part#assets/}" >> uploaded-assets.txt
    done
    # The original checksum verifies that reassembly preserves every byte.
    printf '%s  %s\n' "$(sha256sum "$source" | cut -d ' ' -f 1)" "$name" \
        > "assets/$name.sha256"
    printf '%s\n' "$name.parts" "$name.sha256" >> uploaded-assets.txt
}
