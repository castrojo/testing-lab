from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "manifests/registry-mirror-config.yaml"


def _config_map():
    return next(
        document
        for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if document["kind"] == "ConfigMap"
    )


def test_registry_mirrors_have_no_public_fallback():
    config_map = _config_map()
    hosts = config_map["data"]

    for registry, namespace in (
        ("ghcr.io", "ghcr"),
        ("docker.io", "docker"),
        ("quay.io", "quay"),
        ("registry.fedoraproject.org", "fedora"),
        ("registry.k8s.io", "k8s"),
        ("cgr.dev", "cgr"),
        ("public.ecr.aws", "ecr"),
        ("lscr.io", "lscr"),
    ):
        content = hosts[f"{registry}.hosts.toml"]
        assert "server =" not in content
        assert f'host."http://192.168.1.102:30501/v2/{namespace}"' in content
        assert 'capabilities = ["pull", "resolve"]' in content
        assert "override_path = true" in content
