"""Unit tests for the NVIDIA NGC catalog collector.

Covers mapping of the NGC public catalog response to the shared catalog index
schema, container filtering, and the deterministic sort order of the output.
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


class TestNgccatalogMapping:
    """Schema-level mapping tests using the fixture data."""

    def test_build_index_shape(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])

        assert index["provider"] == "ngc"
        assert index["source_api"] == collector.API_URL
        assert "generated_at" in index
        assert isinstance(index["apps"], list)

    def test_container_filtering(self):
        """Non-container resources are excluded from the index."""
        data = _load_fixture()
        index = collector.build_index(data["containers"])
        names = {a["name"] for a in index["apps"]}

        # The model entry should be dropped.
        assert "nim-llama-3-8b-instruct" not in names
        # Container entries should be kept.
        assert "ollama" in names
        assert "tritonserver" in names
        assert "pytorch" in names
        assert "tensorrt-llm" in names

    def test_image_ref_normalized(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])
        by_name = {a["name"]: a for a in index["apps"]}

        assert by_name["ollama"]["image_ref"] == "nvcr.io/nvidia/ollama"
        assert by_name["tritonserver"]["image_ref"] == "nvcr.io/nvidia/tritonserver"
        assert by_name["pytorch"]["image_ref"] == "nvcr.io/nvidia/pytorch"
        assert by_name["tensorrt-llm"]["image_ref"] == "nvcr.io/nvidia/tensorrt-llm"

    def test_architecture_normalization(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])
        by_name = {a["name"]: a for a in index["apps"]}

        assert "x86_64" in by_name["ollama"]["architectures"]
        assert "arm64" in by_name["ollama"]["architectures"]
        assert by_name["tritonserver"]["architectures"] == ["x86_64"]
        assert by_name["tensorrt-llm"]["architectures"] == ["x86_64"]

    def test_config_pointer_fallback(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])
        by_name = {a["name"]: a for a in index["apps"]}

        assert by_name["ollama"]["config_pointer"] == "https://catalog.ngc.nvidia.com/orgs/nvidia/containers/ollama"

    def test_sort_order(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])
        names = [a["name"] for a in index["apps"]]

        assert names == sorted(names)

    def test_verified_flag(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])

        for app in index["apps"]:
            assert app["verified"] is True

    def test_security_flags_default_false(self):
        data = _load_fixture()
        index = collector.build_index(data["containers"])

        for app in index["apps"]:
            assert app["readonly_supported"] is False
            assert app["nonroot_supported"] is False


class TestNgccatalogHelpers:
    """Low-level helper function tests."""

    def test_get_name_prefers_name(self):
        assert collector.get_name({"name": "foo", "displayName": "Foo Bar"}) == "foo"

    def test_get_name_falls_back(self):
        assert collector.get_name({"displayName": "Foo Bar"}) == "Foo Bar"

    def test_get_description_prefers_long(self):
        assert collector.get_description({"description": "Long", "shortDescription": "Short"}) == "Long"

    def test_get_architectures_string(self):
        assert collector.get_architectures({"architecture": "amd64, arm64"}) == ["x86_64", "arm64"]

    def test_is_container_rejects_model(self):
        assert collector.is_container({"resourceType": "model", "imagePath": "x"}) is False

    def test_is_container_accepts_container(self):
        assert collector.is_container({"imagePath": "nvcr.io/nvidia/foo:1"}) is True

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
