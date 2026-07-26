import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/rechunk_bst_image.sh"
WORK_BASE = ROOT / "tests/.rechunk-test-work"


def write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def rechunk_case():
    work = WORK_BASE / str(os.getpid())
    shutil.rmtree(work, ignore_errors=True)
    (work / "bin").mkdir(parents=True)
    source = work / "source"
    source.mkdir()
    (source / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')
    (source / "index.json").write_text('{"schemaVersion":2,"manifests":[]}\n')
    manifest = work / "files/fakecap-manifest.tsv"
    manifest.parent.mkdir()
    manifest.write_text("/usr/bin/example\tbluefin/example.bst\tweekly\n")
    fakecap = manifest.parent / "fakecap/fakecap-restore"
    fakecap.parent.mkdir()
    log = work / "commands.log"

    write_executable(
        work / "bin/sudo",
        'echo "sudo $*" >> "$MOCK_LOG"\nexec "$@"\n',
    )
    write_executable(
        work / "bin/mount",
        'echo "mount $*" >> "$MOCK_LOG"\n',
    )
    write_executable(
        work / "bin/umount",
        'echo "umount $*" >> "$MOCK_LOG"\n',
    )
    write_executable(
        fakecap,
        'echo "fakecap $*" >> "$MOCK_LOG"\n',
    )
    write_executable(
        work / "bin/podman",
        r'''
echo "podman $*" >> "$MOCK_LOG"
case "${1:-} ${2:-}" in
    "pull -q") echo "sha256:source-image" ;;
    "inspect sha256:source-image")
        printf '[{"Config":{"Labels":{"containers.bootc":"1","ostree.commit":"old"}}}]\n'
        ;;
    "image mount")
        mkdir -p "$MOCK_LOWER"
        echo "$MOCK_LOWER"
        ;;
    "run --rm")
        mkdir -p "$MOCK_OUTPUT"
        printf '{"imageLayoutVersion":"1.0.0"}\n' > "$MOCK_OUTPUT/oci-layout"
        printf '{"schemaVersion":2,"manifests":[]}\n' > "$MOCK_OUTPUT/index.json"
        ;;
esac
''',
    )
    write_executable(
        work / "bin/skopeo",
        r'''
echo "skopeo $*" >> "$MOCK_LOG"
ref="${*: -1}"
if [[ "$*" == *"--raw"* ]]; then
    printf '%s\n' "$MOCK_OUTPUT_MANIFEST"
elif [[ "$*" == *"--format"* ]]; then
    if [[ "$ref" == *"/source" ]]; then echo "sha256:input"; else echo "sha256:output"; fi
elif [[ "$ref" == *"/source" ]]; then
    printf '{"Labels":{"containers.bootc":"1","ostree.commit":"old","ostree.final-diffid":"old"}}\n'
else
    printf '%s\n' "$MOCK_OUTPUT_INSPECT"
fi
''',
    )

    env = os.environ | {
        "PATH": f"{work / 'bin'}:{os.environ['PATH']}",
        "MOCK_LOG": str(log),
        "MOCK_LOWER": str(work / "lower"),
        "MOCK_OUTPUT": str(work / "output"),
        "MOCK_OUTPUT_MANIFEST": '{"layers":[{"size":40},{"size":50}]}',
        "MOCK_OUTPUT_INSPECT": '{"Labels":{"containers.bootc":"1"}}',
        "FAKECAP_RESTORE": str(fakecap),
        "RECHUNK_WORKDIR": str(work / "overlay"),
    }

    try:
        yield work, source, manifest, log, env
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if WORK_BASE.exists() and not any(WORK_BASE.iterdir()):
            WORK_BASE.rmdir()


def run_rechunk(case, **env_overrides):
    work, source, manifest, _, env = case
    return subprocess.run(
        [SCRIPT, source, work / "output", manifest],
        cwd=ROOT,
        env=env | env_overrides,
        text=True,
        capture_output=True,
        check=False,
    )


def test_rechunks_layout_and_emits_metrics(rechunk_case):
    result = run_rechunk(rechunk_case)

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)
    assert metrics["input_digest"] == "sha256:input"
    assert metrics["output_digest"] == "sha256:output"
    assert metrics["compressed_size_bytes"] == 90
    assert metrics["layer_count"] == 2
    assert metrics["tool_version"] == "chunkah v0.6.0"
    assert isinstance(metrics["duration_seconds"], int)

    commands = rechunk_case[3].read_text(encoding="utf-8")
    assert "fakecap-manifest.tsv" in commands
    assert "quay.io/coreos/chunkah:v0.6.0@sha256:" in commands
    assert "--compressed --max-layers 128 --prune /sysroot/" in commands
    assert "--label ostree.commit- --label ostree.final-diffid-" in commands
    assert "--output oci:/output/output" in commands
    assert "CHUNKAH_CONFIG_STR=" in commands
    assert not (rechunk_case[0] / "overlay").exists()


def test_rejects_more_than_128_layers(rechunk_case):
    layers = {"layers": [{"size": 1}] * 129}
    result = run_rechunk(
        rechunk_case,
        MOCK_OUTPUT_MANIFEST=json.dumps(layers, separators=(",", ":")),
    )

    assert result.returncode == 1
    assert "maximum is 128" in result.stderr
    assert not (rechunk_case[0] / "output").exists()


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"Labels": {}}, "missing containers.bootc"),
        (
            {"Labels": {"containers.bootc": "1", "ostree.commit": "stale"}},
            "retained stale ostree labels",
        ),
    ],
)
def test_rejects_invalid_output_labels(rechunk_case, labels, message):
    result = run_rechunk(
        rechunk_case,
        MOCK_OUTPUT_INSPECT=json.dumps(labels, separators=(",", ":")),
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert not (rechunk_case[0] / "output").exists()
