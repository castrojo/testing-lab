#!/usr/bin/env bash
set -euo pipefail

CHUNKAH_VERSION="v0.6.0"
CHUNKAH_REF="${CHUNKAH_REF:-quay.io/coreos/chunkah:${CHUNKAH_VERSION}@sha256:ff8b8b466a942ec6000445d4001fc661e2fc5a952ad9ee29b4de9ab09d1d1708}"
MAX_LAYERS=128

usage() {
    echo "usage: $0 SOURCE_OCI OUTPUT_OCI FAKECAP_MANIFEST" >&2
    exit 2
}

fail() {
    echo "rechunk_bst_image: $*" >&2
    exit 1
}

[[ $# -eq 3 ]] || usage

SOURCE=$1
OUTPUT=$2
MANIFEST=$3
PODMAN=${PODMAN:-podman}
SKOPEO=${SKOPEO:-skopeo}
JQ=${JQ:-jq}

for command in "$PODMAN" "$SKOPEO" "$JQ" mount umount; do
    command -v "$command" >/dev/null || fail "required command not found: $command"
done

[[ -f "$SOURCE/oci-layout" && -f "$SOURCE/index.json" ]] ||
    fail "source is not an OCI layout: $SOURCE"
[[ -f "$MANIFEST" ]] || fail "fakecap manifest not found: $MANIFEST"
[[ ! -e "$OUTPUT" ]] || fail "output already exists: $OUTPUT"

SOURCE=$(cd "$(dirname "$SOURCE")" && pwd -P)/$(basename "$SOURCE")
OUTPUT_PARENT=$(cd "$(dirname "$OUTPUT")" && pwd -P)
OUTPUT_NAME=$(basename "$OUTPUT")
MANIFEST=$(cd "$(dirname "$MANIFEST")" && pwd -P)/$(basename "$MANIFEST")
FAKECAP_RESTORE=${FAKECAP_RESTORE:-"$(dirname "$MANIFEST")/fakecap/fakecap-restore"}
[[ -x "$FAKECAP_RESTORE" ]] ||
    fail "fakecap restore helper is not executable: $FAKECAP_RESTORE"

ROOT=()
if [[ $(id -u) -ne 0 ]]; then
    command -v sudo >/dev/null || fail "root privileges are required"
    ROOT=(sudo)
fi

WORK_ROOT=${RECHUNK_WORKDIR:-"${OUTPUT_PARENT}/.rechunk-${OUTPUT_NAME}"}
[[ ! -e "$WORK_ROOT" ]] || fail "work directory already exists: $WORK_ROOT"
UPPER="${WORK_ROOT}/upper"
OVERLAY_WORK="${WORK_ROOT}/work"
MERGED="${WORK_ROOT}/rootfs"
MOUNTED=false
SUCCESS=false
SOURCE_IMAGE=

cleanup() {
    if [[ "$MOUNTED" == true ]]; then
        "${ROOT[@]}" umount "$MERGED" 2>/dev/null || true
    fi
    if [[ -n "$SOURCE_IMAGE" ]]; then
        "${ROOT[@]}" "$PODMAN" image unmount "$SOURCE_IMAGE" >/dev/null 2>&1 || true
    fi
    "${ROOT[@]}" rm -rf "$WORK_ROOT"
    if [[ "$SUCCESS" != true ]]; then
        "${ROOT[@]}" rm -rf "$OUTPUT"
    fi
}
trap cleanup EXIT

STARTED_AT=$(date +%s)
INPUT_DIGEST=$("$SKOPEO" inspect --format '{{.Digest}}' "oci:${SOURCE}")
SOURCE_INSPECT=$("$SKOPEO" inspect "oci:${SOURCE}")
SOURCE_BOOTC=$("$JQ" -er '.Labels["containers.bootc"]' <<<"$SOURCE_INSPECT") ||
    fail "source OCI layout is missing containers.bootc"

SOURCE_IMAGE=$("${ROOT[@]}" "$PODMAN" pull -q "oci:${SOURCE}" | tail -n 1)
[[ -n "$SOURCE_IMAGE" ]] || fail "podman did not return an image ID"
SOURCE_CONFIG=$("${ROOT[@]}" "$PODMAN" inspect "$SOURCE_IMAGE")
LOWER=$("${ROOT[@]}" "$PODMAN" image mount "$SOURCE_IMAGE")
[[ -d "$LOWER" ]] || fail "podman did not mount the source image"

"${ROOT[@]}" mkdir -p "$UPPER" "$OVERLAY_WORK" "$MERGED"
"${ROOT[@]}" mount -t overlay overlay \
    -o "lowerdir=${LOWER},upperdir=${UPPER},workdir=${OVERLAY_WORK}" "$MERGED"
MOUNTED=true

echo "==> Applying fakecap xattrs" >&2
"${ROOT[@]}" "$FAKECAP_RESTORE" "$MANIFEST" "$MERGED"

for attempt in 1 2 3; do
    if "${ROOT[@]}" "$PODMAN" pull "$CHUNKAH_REF" >/dev/null; then
        break
    fi
    [[ $attempt -lt 3 ]] || fail "unable to pull ${CHUNKAH_REF}"
    sleep 10
done

echo "==> Rechunking OCI layout with chunkah ${CHUNKAH_VERSION}" >&2
"${ROOT[@]}" "$PODMAN" run --rm \
    --pull never \
    --security-opt label=type:unconfined_t \
    -v "${MERGED}:/chunkah:ro" \
    -v "${OUTPUT_PARENT}:/output:rw" \
    -e "CHUNKAH_ROOTFS=/chunkah" \
    -e "CHUNKAH_CONFIG_STR=${SOURCE_CONFIG}" \
    "$CHUNKAH_REF" build \
    --compressed \
    --max-layers "$MAX_LAYERS" \
    --prune /sysroot/ \
    --label ostree.commit- \
    --label ostree.final-diffid- \
    --output "oci:/output/${OUTPUT_NAME}"

[[ -f "$OUTPUT/oci-layout" && -f "$OUTPUT/index.json" ]] ||
    fail "chunkah did not produce an OCI layout"

OUTPUT_DIGEST=$("$SKOPEO" inspect --format '{{.Digest}}' "oci:${OUTPUT}")
OUTPUT_INSPECT=$("$SKOPEO" inspect "oci:${OUTPUT}")
OUTPUT_BOOTC=$("$JQ" -er '.Labels["containers.bootc"]' <<<"$OUTPUT_INSPECT") ||
    fail "output OCI layout is missing containers.bootc"
[[ "$OUTPUT_BOOTC" == "$SOURCE_BOOTC" ]] ||
    fail "containers.bootc changed during rechunk"

# shellcheck disable=SC2016 # jq variables, not shell variables
if "$JQ" -e '
    (.Labels // {}) as $labels
    | ($labels | has("ostree.commit")) or ($labels | has("ostree.final-diffid"))
' >/dev/null <<<"$OUTPUT_INSPECT"; then
    fail "output retained stale ostree labels"
fi

OUTPUT_MANIFEST=$("$SKOPEO" inspect --raw "oci:${OUTPUT}")
LAYER_COUNT=$("$JQ" -er '.layers | length' <<<"$OUTPUT_MANIFEST")
COMPRESSED_SIZE=$("$JQ" -er '[.layers[].size] | add // 0' <<<"$OUTPUT_MANIFEST")
(( LAYER_COUNT <= MAX_LAYERS )) ||
    fail "output has ${LAYER_COUNT} layers; maximum is ${MAX_LAYERS}"

DURATION=$(( $(date +%s) - STARTED_AT ))
SUCCESS=true
# shellcheck disable=SC2016 # jq variables, not shell variables
"$JQ" -n \
    --arg input_digest "$INPUT_DIGEST" \
    --arg output_digest "$OUTPUT_DIGEST" \
    --arg tool_version "chunkah ${CHUNKAH_VERSION}" \
    --argjson compressed_size_bytes "$COMPRESSED_SIZE" \
    --argjson layer_count "$LAYER_COUNT" \
    --argjson duration_seconds "$DURATION" \
    '{
        input_digest: $input_digest,
        output_digest: $output_digest,
        compressed_size_bytes: $compressed_size_bytes,
        layer_count: $layer_count,
        duration_seconds: $duration_seconds,
        tool_version: $tool_version
    }'
