from pathlib import Path

import yaml


def test_zot_sync_is_on_demand_and_bounded():
    manifest = next(
        document
        for document in yaml.safe_load_all(Path("manifests/zot-cache.yaml").read_text())
        if document["kind"] == "ConfigMap"
    )
    config = yaml.safe_load(manifest["data"]["config.json"])
    registries = config["extensions"]["sync"]["registries"]

    assert registries
    for registry in registries:
        assert registry["onDemand"] is True
        assert registry["maxRetries"] == 1
        assert registry["retryDelay"] == "2m"
        assert all(item["prefix"] != "**" for item in registry["content"])
