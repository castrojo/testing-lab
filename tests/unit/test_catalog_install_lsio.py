"""Golden-file tests for the linuxserver.io install translator.

The translator maps LSIO config (env_vars, volumes, ports) to Kubernetes
manifests with lab conventions. These tests pin the output for representative
apps so refactors stay deterministic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# scripts/ is at repo root; import the translator directly.
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "scripts"))

import catalog_install_lsio as translator

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG_FIXTURE = FIXTURES / "linuxserver-catalog.json"


def _load_catalog():
    with open(CATALOG_FIXTURE) as f:
        return json.load(f)


def _expected_yaml(app_name: str) -> str:
    return (FIXTURES / f"expected-{app_name}" / "manifest.yaml").read_text()


class TestCatalogInstallLsio:
    """Golden-file assertions for jellyfin and sonarr rendering."""

    @pytest.mark.parametrize("app_name", ["jellyfin", "sonarr"])
    def test_render_matches_golden(self, app_name, tmp_path):
        catalog = _load_catalog()
        entry = translator.find_app(catalog, app_name)
        resources = translator.render_manifests(app_name, entry, f"catalog-{app_name}")
        rendered = translator.to_yaml(resources)

        assert rendered == _expected_yaml(app_name)

    def test_jellyfin_resources(self):
        catalog = _load_catalog()
        entry = translator.find_app(catalog, "jellyfin")
        resources = translator.render_manifests("jellyfin", entry, "catalog-jellyfin")
        kinds = [r["kind"] for r in resources]

        assert kinds == ["Namespace", "PersistentVolumeClaim", "PersistentVolumeClaim", "PersistentVolumeClaim", "Deployment", "Service"]

        deployment = resources[4]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "linuxserver/jellyfin:latest"
        assert len(container["ports"]) == 4
        assert {p["protocol"] for p in container["ports"]} == {"TCP", "UDP"}
        assert len(container["volumeMounts"]) == 3
        assert any(m["mountPath"] == "/config" for m in container["volumeMounts"])

        pvcs = [r for r in resources if r["kind"] == "PersistentVolumeClaim"]
        sizes = {r["metadata"]["name"]: r["spec"]["resources"]["requests"]["storage"] for r in pvcs}
        assert sizes["jellyfin-config"] == "5Gi"
        assert sizes["jellyfin-data-tvshows"] == "100Gi"
        assert sizes["jellyfin-data-movies"] == "100Gi"

    def test_sonarr_security_context(self):
        catalog = _load_catalog()
        entry = translator.find_app(catalog, "sonarr")
        resources = translator.render_manifests("sonarr", entry, "catalog-sonarr")
        deployment = [r for r in resources if r["kind"] == "Deployment"][0]
        ctx = deployment["spec"]["template"]["spec"].get("securityContext")

        assert ctx is not None
        assert ctx["runAsUser"] == 1000
        assert ctx["runAsGroup"] == 1000
        assert ctx["fsGroup"] == 1000
        assert ctx["runAsNonRoot"] is True
        assert ctx["readOnlyRootFilesystem"] is True

    def test_pvc_size_heuristic(self):
        assert translator.pvc_size("/config") == "5Gi"
        assert translator.pvc_size("/data/movies") == "100Gi"
        assert translator.pvc_size("/transcode") == "50Gi"
        assert translator.pvc_size("/etc/something") == "1Gi"

    def test_parse_port(self):
        assert translator.parse_port("8096") == (8096, "tcp")
        assert translator.parse_port("7359/udp") == (7359, "udp")
        assert translator.parse_port("8096/tcp") == (8096, "tcp")
        assert translator.parse_port("not-a-port") == (None, None)

    def test_pvc_name_sanitizes_path(self):
        assert translator.pvc_name("jellyfin", "/config") == "jellyfin-config"
        assert translator.pvc_name("jellyfin", "/data/tvshows") == "jellyfin-data-tvshows"
