"""Regression checks for node-local Local Path Provisioner storage mappings."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "manifests/local-path-config.yaml"


def test_local_path_uses_each_nodes_data_mount_without_a_default():
    manifest = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    config = json.loads(manifest["data"]["config.json"])
    node_paths = {entry["node"]: entry["paths"] for entry in config["nodePathMap"]}

    assert node_paths == {
        "ghost": ["/var/mnt/ghost-data/local-path"],
        "exo-0": ["/var/mnt/exo0-data/local-path"],
    }
    assert "DEFAULT_PATH_FOR_NON_LISTED_NODES" not in node_paths
