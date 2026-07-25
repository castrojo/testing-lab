"""Unit tests for the offline catalog manifest validator.

Covers the minimal YAML parser and the structural validation rules:
required fields, container resources, local-path storage, image allowlist,
and the hostPath ban.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "scripts"))

import catalog_validate as validator

FIXTURES = Path(__file__).resolve().parent / "fixtures"


GOOD_MANIFEST = """\
---
apiVersion: v1
kind: Namespace
metadata:
  name: catalog-good
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: good-config
  namespace: catalog-good
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: good
  namespace: catalog-good
spec:
  replicas: 1
  selector:
    matchLabels:
      app: good
  template:
    metadata:
      labels:
        app: good
    spec:
      containers:
        - name: good
          image: linuxserver/good:latest
          ports:
            - containerPort: 8080
          env:
            - name: TZ
              value: UTC
          volumeMounts:
            - name: config
              mountPath: /config
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
              ephemeral-storage: 100Mi
            limits:
              cpu: 500m
              memory: 512Mi
              ephemeral-storage: 1Gi
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: good-config
"""


def _load_good_manifest():
    return validator.load_yaml_docs(GOOD_MANIFEST)


class TestYamlParser:
    """Smoke tests for the minimal YAML parser used by the validator."""

    def test_loads_multi_doc_mapping(self):
        docs = _load_good_manifest()
        assert len(docs) == 3
        assert docs[0]["kind"] == "Namespace"
        assert docs[1]["kind"] == "PersistentVolumeClaim"
        assert docs[2]["kind"] == "Deployment"

    def test_loads_nested_list_of_mappings(self):
        deployment = _load_good_manifest()[2]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "good"
        assert container["image"] == "linuxserver/good:latest"
        assert container["resources"]["limits"]["memory"] == "512Mi"

    def test_loads_quoted_strings(self):
        docs = _load_good_manifest()
        assert docs[0]["metadata"]["name"] == "catalog-good"


class TestValidationRules:
    """Structural validation rules."""

    def test_good_manifest_passes(self):
        docs = _load_good_manifest()
        errors = validator.validate_manifests(docs)
        assert errors == []

    def test_missing_resources_rejected(self):
        docs = _load_good_manifest()
        container = docs[2]["spec"]["template"]["spec"]["containers"][0]
        del container["resources"]["limits"]["ephemeral-storage"]
        errors = validator.validate_manifests(docs)
        assert any("ephemeral-storage" in e and "limits" in e for e in errors)

    def test_hostpath_volume_rejected(self):
        docs = _load_good_manifest()
        docs[2]["spec"]["template"]["spec"]["volumes"].append({
            "name": "host-data",
            "hostPath": {"path": "/mnt/data"},
        })
        errors = validator.validate_manifests(docs)
        assert any("hostPath" in e for e in errors)

    def test_non_local_path_pvc_rejected(self):
        docs = _load_good_manifest()
        docs[1]["spec"]["storageClassName"] = "standard"
        errors = validator.validate_manifests(docs)
        assert any("storageClassName" in e for e in errors)

    def test_uncached_registry_rejected(self):
        docs = _load_good_manifest()
        docs[2]["spec"]["template"]["spec"]["containers"][0]["image"] = "docker.io/library/nginx:latest"
        errors = validator.validate_manifests(docs)
        assert any("docker.io" in e for e in errors)

    def test_bare_image_allowed(self):
        docs = _load_good_manifest()
        docs[2]["spec"]["template"]["spec"]["containers"][0]["image"] = "linuxserver/good:latest"
        errors = validator.validate_manifests(docs)
        assert errors == []

    def test_ghcr_image_allowed(self):
        docs = _load_good_manifest()
        docs[2]["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/projectbluefin/lab-runner:latest"
        errors = validator.validate_manifests(docs)
        assert errors == []

    def test_namespace_required_for_namespaced_kinds(self):
        docs = _load_good_manifest()
        del docs[2]["metadata"]["namespace"]
        errors = validator.validate_manifests(docs)
        assert any("namespace is required" in e for e in errors)


class TestGoldenFixtures:
    """Validate the rendered golden fixtures used by the install translator."""

    @pytest.mark.parametrize("app_name", ["jellyfin", "sonarr"])
    def test_golden_fixture_passes(self, app_name):
        path = FIXTURES / f"expected-{app_name}" / "manifest.yaml"
        docs = validator.load_yaml_docs(path.read_text())
        errors = validator.validate_manifests(docs)
        assert errors == []
