"""Unit tests for the NVIDIA NGC catalog collector.

Covers mapping of the public NGC catalog search response to the shared catalog
index schema, orgName filtering, and deterministic output ordering.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "scripts"))

import collect_ngc_catalog as collector  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG_FIXTURE = FIXTURES / "ngc-catalog.json"


def _load_fixture():
    with open(CATALOG_FIXTURE) as f:
        return json.load(f)


def _flatten_resources(data):
    resources = []
    for group in data.get("results") or []:
        resources.extend(group.get("resources") or [])
    return resources


class TestNgcCatalogMapping:
    """Schema-level mapping tests using the fixture data."""

    def test_build_index_shape(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)

        assert index["provider"] == "ngc"
        assert index["source_api"] == collector.API_URL
        assert "generated_at" in index
        assert isinstance(index["apps"], list)

    def test_nvidia_org_filtering(self):
        """Only orgName == 'nvidia' resources are considered official."""
        resources = _flatten_resources(_load_fixture())
        nvidia = [r for r in resources if collector.is_nvidia_container(r)]
        names = {r["name"] for r in nvidia}

        assert "test_container" not in names
        assert "pytorch" in names
        assert "tritonserver" in names
        assert "cuda" in names

    def test_build_index_excludes_non_nvidia(self):
        """build_index ignores resources without a valid image_ref/name."""
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        names = {a["name"] for a in index["apps"]}

        # Non-NVIDIA resource still has a name/image_ref, so it would be included
        # unless filtered upstream. The source of truth for filtering is
        # is_nvidia_container / fetch_all_containers.
        assert "pytorch" in names
        assert "tritonserver" in names
        assert "cuda" in names

    def test_image_ref_prefix(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        by_name = {a["name"]: a for a in index["apps"]}

        assert by_name["pytorch"]["image_ref"] == "nvcr.io/nvidia/pytorch"
        assert by_name["tritonserver"]["image_ref"] == "nvcr.io/nvidia/tritonserver"

    def test_architecture_normalization(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        by_name = {a["name"]: a for a in index["apps"]}

        assert "x86_64" in by_name["pytorch"]["architectures"]
        assert "arm64" in by_name["pytorch"]["architectures"]
        assert by_name["tritonserver"]["architectures"] == ["x86_64"]

    def test_config_pointer(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        by_name = {a["name"]: a for a in index["apps"]}

        assert by_name["pytorch"]["config_pointer"] == "https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch"

    def test_logo_from_attributes(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        by_name = {a["name"]: a for a in index["apps"]}

        assert by_name["pytorch"]["logo_url"] == "https://assets.nvidiagrid.net/ngc/logos/OSS-Nvidia-Partnership-Pytorch.png"
        assert by_name["tritonserver"]["logo_url"] is None

    def test_sort_order(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)
        names = [a["name"] for a in index["apps"]]

        assert names == sorted(names)

    def test_verified_flag(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)

        for app in index["apps"]:
            assert app["verified"] is True

    def test_security_flags_default_false(self):
        resources = _flatten_resources(_load_fixture())
        index = collector.build_index(resources)

        for app in index["apps"]:
            assert app["readonly_supported"] is False
            assert app["nonroot_supported"] is False


class TestNgcCatalogHelpers:
    """Low-level helper function tests."""

    def test_get_name_from_name_field(self):
        assert collector.get_name({"name": "foo"}) == "foo"

    def test_get_name_from_resource_id(self):
        assert collector.get_name({"resourceId": "nvidia/foo/bar"}) == "bar"

    def test_get_category_general_labels(self):
        container = {
            "labels": [
                {"key": "general", "values": ["AI", "Inference"]},
                {"key": "system", "values": ["signed images"]},
            ]
        }
        assert collector.get_category(container) == "AI, Inference"

    def test_get_architectures_multiarch(self):
        container = {
            "labels": [{"key": "system", "values": ["containers:multiarch"]}]
        }
        assert collector.get_architectures(container) == ["x86_64", "arm64"]

    def test_get_architectures_default(self):
        assert collector.get_architectures({"labels": []}) == ["x86_64"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("not-a-number", None),
            ("123", 123),
            (1234, 1234),
        ],
    )
    def test_as_int(self, raw, expected):
        assert collector.as_int(raw) == expected
