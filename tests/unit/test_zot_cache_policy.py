from pathlib import Path

import yaml


def sync_registries():
    manifest = next(
        document
        for document in yaml.safe_load_all(Path("manifests/zot-cache.yaml").read_text())
        if document["kind"] == "ConfigMap"
    )
    config = yaml.safe_load(manifest["data"]["config.json"])
    return config["extensions"]["sync"]["registries"]


def test_zot_sync_is_on_demand_and_bounded():
    registries = sync_registries()

    assert registries
    for registry in registries:
        assert registry["onDemand"] is True
        assert registry["maxRetries"] == 1
        assert registry["retryDelay"] == "2m"


def test_zot_sync_prefixes_are_scoped_to_observed_repositories():
    for registry in sync_registries():
        assert registry["content"]
        assert all(item["prefix"] not in {"*", "**"} for item in registry["content"])
