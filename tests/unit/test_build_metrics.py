import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_documents(path):
    return list(yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8")))


def test_prometheus_uses_bounded_local_path_storage_and_retention():
    documents = load_documents("manifests/prometheus-lightweight.yaml")
    pvc = next(doc for doc in documents if doc["kind"] == "PersistentVolumeClaim")
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert pvc["spec"]["storageClassName"] == "local-path"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "50Gi"
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    assert "--storage.tsdb.retention.time=30d" in container["args"]
    assert "--storage.tsdb.retention.size=45GB" in container["args"]
    assert deployment["spec"]["template"]["spec"]["volumes"][1] == {
        "name": "storage",
        "persistentVolumeClaim": {"claimName": "prometheus-lightweight-data"},
    }


def test_prometheus_scrapes_required_build_and_node_targets():
    config_map = next(
        doc
        for doc in load_documents("manifests/prometheus-lightweight.yaml")
        if doc["kind"] == "ConfigMap"
    )
    config = yaml.safe_load(config_map["data"]["prometheus.yml"])
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}

    assert {
        "kubernetes-nodes",
        "kubernetes-cadvisor",
        "argo-workflow-controller",
        "zot",
        "buildbarn",
    }.issubset(jobs)
    assert jobs["zot"]["metrics_path"] == "/metrics"
    assert jobs["zot"]["kubernetes_sd_configs"][0]["namespaces"]["names"] == [
        "local-registry"
    ]
    assert jobs["argo-workflow-controller"]["kubernetes_sd_configs"][0][
        "namespaces"
    ]["names"] == ["argo"]
    assert jobs["kubernetes-cadvisor"]["sample_limit"] == 50000
    assert jobs["zot"]["sample_limit"] == 2000
    assert jobs["github-traffic-hooks"]["sample_limit"] == 500
    assert jobs["github-traffic-hooks"]["relabel_configs"][1]["regex"] == "github-api"


def test_zot_metrics_are_enabled_on_both_registries():
    for path, workload_kind in (
        ("manifests/zot-cache.yaml", "DaemonSet"),
        ("manifests/zot-writable.yaml", "Deployment"),
    ):
        documents = load_documents(path)
        config_map = next(doc for doc in documents if doc["kind"] == "ConfigMap")
        workload = next(doc for doc in documents if doc["kind"] == workload_kind)
        config = json.loads(config_map["data"]["config.json"])

        assert config["extensions"]["metrics"] == {
            "enable": True,
            "prometheus": {"path": "/metrics"},
        }
        config_version = workload["spec"]["template"]["metadata"]["annotations"].get(
            "lab.projectbluefin.io/config-version"
        )
        assert isinstance(config_version, str) and config_version.strip()


def test_safe_build_workflows_emit_only_low_cardinality_metrics():
    pipelines = {
        "cosmic-build-pipeline.yaml": "cosmic",
        "bluefin-server-build-pipeline.yaml": "bluefin-server",
        "bst-qa-pipeline.yaml": "bst-qa",
        "dakota-build-pipeline.yaml": "dakota",
    }

    for filename, pipeline in pipelines.items():
        workflow = yaml.safe_load(
            (ROOT / "argo/workflow-templates" / filename).read_text(encoding="utf-8")
        )
        metrics = workflow["spec"]["metrics"]["prometheus"]

        assert {metric["name"] for metric in metrics} == {
            "lab_build_workflow_completed_total",
            "lab_build_workflow_duration_seconds",
        }
        for metric in metrics:
            assert metric["labels"] == [
                {"key": "pipeline", "value": pipeline},
                {"key": "status", "value": "{{workflow.status}}"},
            ]
