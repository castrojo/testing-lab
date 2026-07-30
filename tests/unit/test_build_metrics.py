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
        assert workload["spec"]["template"]["metadata"]["annotations"][
            "lab.projectbluefin.io/config-version"
        ] == "metrics-v1"


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

        expected = {
            "lab_build_workflow_completed_total",
            "lab_build_workflow_duration_seconds",
        }
        if pipeline in {"cosmic", "dakota"}:
            expected |= {
                "lab_build_workflow_wall_time_seconds",
                "lab_build_workflow_throughput_total",
            }
        assert {metric["name"] for metric in metrics} == expected
        for metric in metrics:
            labels = {label["key"]: label["value"] for label in metric["labels"]}
            assert labels["pipeline"] == pipeline
            assert labels["status"] == "{{workflow.status}}"
            assert set(labels) <= {"pipeline", "execution_mode", "status"}


def test_dakota_and_cosmic_emit_bounded_variant_duration_metrics():
    expected = {
        "cosmic-build-pipeline.yaml": {"cosmic"},
        "dakota-build-pipeline.yaml": {"dakota", "dakota-nvidia"},
    }

    for filename, variants in expected.items():
        workflow = yaml.safe_load(
            (ROOT / "argo/workflow-templates" / filename).read_text(encoding="utf-8")
        )
        template_metrics = {
            template["name"]: template.get("metrics", {}).get("prometheus", [])
            for template in workflow["spec"]["templates"]
        }
        emitted = {}
        for template_name, metrics in template_metrics.items():
            for metric in metrics:
                if metric["name"] == "lab_build_variant_duration_seconds":
                    labels = {label["key"]: label["value"] for label in metric["labels"]}
                    emitted[labels["variant"]] = metric

        assert set(emitted) == variants
        for metric in emitted.values():
            assert metric["histogram"]["value"] == "{{duration}}"
            assert metric["labels"][0] == {
                "key": "pipeline",
                "value": "cosmic" if "cosmic" in filename else "dakota",
            }
            labels = {label["key"]: label["value"] for label in metric["labels"]}
            assert set(labels) <= {"pipeline", "variant", "execution_mode", "status"}
            assert labels["status"] == "{{status}}"


def test_dakota_and_cosmic_expose_queue_and_execution_durations():
    for filename in ("cosmic-build-pipeline.yaml", "dakota-build-pipeline.yaml"):
        workflow = yaml.safe_load(
            (ROOT / "argo/workflow-templates" / filename).read_text(encoding="utf-8")
        )
        metrics = [
            metric
            for template in workflow["spec"]["templates"]
            for metric in template.get("metrics", {}).get("prometheus", [])
        ]
        by_name = {metric["name"]: metric for metric in metrics}

        assert by_name["lab_build_workflow_queue_time_seconds"]["histogram"][
            "value"
        ] == "{{duration}}"
        assert by_name["lab_build_workflow_execution_time_seconds"]["histogram"][
            "value"
        ] == "{{duration}}"
        for name in (
            "lab_build_workflow_queue_time_seconds",
            "lab_build_workflow_execution_time_seconds",
        ):
            assert by_name[name]["labels"][0]["key"] == "pipeline"
            assert by_name[name]["labels"][1] == {
                "key": "status",
                "value": "{{status}}",
            }
