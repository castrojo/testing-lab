import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
IMAGE_VERSIONS = ROOT / "image-versions.kde.yaml"
RENOVATE_CONFIG = ROOT / "renovate.json"

# Regex from projectbluefin/renovate-config custom manager for
# image-versions*.yaml files.
IMAGE_PIN_RE = re.compile(
    r"image:\s*(?P<image>\S+)\s*"
    r"tag:\s*(?P<tag>\S+)\s*"
    r"digest:\s*(?P<digest>sha256:[a-f0-9]{64})",
    re.IGNORECASE,
)


def test_renovate_config_is_valid_json_and_extends_shared_config():
    config = json.loads(RENOVATE_CONFIG.read_text(encoding="utf-8"))
    assert "$schema" in config
    assert "local>projectbluefin/renovate-config" in config["extends"]
    assert "kubernetes" in config
    file_match = config["kubernetes"]["fileMatch"]
    assert any("kde-" in pattern for pattern in file_match)


def test_kde_image_versions_is_valid_yaml_with_pins():
    data = yaml.safe_load(IMAGE_VERSIONS.read_text(encoding="utf-8"))
    assert "images" in data
    assert len(data["images"]) >= 1
    for entry in data["images"]:
        assert "name" in entry
        assert "image" in entry
        assert "tag" in entry
        assert "digest" in entry
        assert entry["digest"].startswith("sha256:")


def test_kde_image_versions_matches_renovate_regex():
    content = IMAGE_VERSIONS.read_text(encoding="utf-8")
    matches = list(IMAGE_PIN_RE.finditer(content))
    assert matches, "no image/tag/digest pins matched the Renovate regex"
    for match in matches:
        assert match.group("image").startswith("ghcr.io/")
        assert match.group("tag")
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", match.group("digest"))


def test_kde_source_tracker_files_are_not_directly_edited():
    """This agent owns Renovate coverage, not the source tracker itself."""
    source_tracker_wt = ROOT / "argo/workflow-templates/kde-source-tracker.yaml"
    source_tracker_cm = ROOT / "manifests/kde-source-tracker.yaml"
    assert source_tracker_wt.exists()
    assert source_tracker_cm.exists()
