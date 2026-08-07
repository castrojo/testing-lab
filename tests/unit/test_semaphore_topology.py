"""Regression tests for the ghost-container-qa semaphore topology.

Live incident: a single QA pipeline held five of the six `ghost-container-qa`
slots because `spec.parallelism` is not inherited when a caller reaches a
template through `templateRef` (only a spec-level `workflowTemplateRef`
inherits it). `pr-poller`'s inline `pr-pipeline` and `image-poller` both use
`templateRef`, so every `test-lane` in a fan-out started at once and starved
every other PR.
"""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/check_semaphore_topology.py"

_spec = importlib.util.spec_from_file_location("check_semaphore_topology", CHECKER)
topology = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(topology)

FANOUT_PIPELINES = (
    "argo/workflow-templates/bluefin-qa-pipeline.yaml",
    "argo/workflow-templates/dakota-qa-pipeline.yaml",
    "argo/workflow-templates/cosmic-qa-pipeline.yaml",
)


def _templates(relpath):
    doc = yaml.safe_load((ROOT / relpath).read_text(encoding="utf-8"))
    return {t["name"]: t for t in doc["spec"]["templates"]}


def test_ghost_container_qa_is_declared_only_on_the_leaf_runner():
    """The semaphore lives on the template that consumes the runner, nowhere else."""
    owners = set()
    for path in (ROOT / "argo").rglob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict):
                continue
            spec = doc.get("spec") or {}
            assert "ghost-container-qa" not in topology.semaphore_keys(
                spec.get("synchronization")
            ), f"{path}: workflow-level ghost-container-qa starves its own children"
            for template in spec.get("templates") or []:
                if "ghost-container-qa" in topology.semaphore_keys(
                    template.get("synchronization")
                ):
                    owners.add(f"{doc['metadata']['name']}/{template['name']}")
    assert owners == {"run-container-tests/run-container-tests"}


def test_fanout_pipelines_cap_their_own_semaphore_consumption():
    """Each pipeline template caps its lanes below the ghost-container-qa limit."""
    limit = int(
        yaml.safe_load(
            (ROOT / "manifests/workflow-semaphores.yaml").read_text(encoding="utf-8")
        )["data"]["ghost-container-qa"]
    )
    for relpath in FANOUT_PIPELINES:
        pipeline = _templates(relpath)["pipeline"]
        parallelism = pipeline.get("parallelism")
        assert parallelism is not None, f"{relpath}: pipeline template needs parallelism"
        assert parallelism < limit, f"{relpath}: parallelism must stay under {limit}"


def test_repository_passes_the_topology_check():
    holders, consumers, errors = topology.collect(
        sorted((ROOT / "argo").rglob("*.yaml"))
    )
    assert errors == []
    assert holders["ghost-container-qa"] == {"run-container-tests/run-container-tests"}


def test_checker_rejects_an_uncapped_fanout(tmp_path):
    """The guard must actually catch the shape that caused the incident."""
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "WorkflowTemplate",
        "metadata": {"name": "leaky"},
        "spec": {
            "templates": [
                {
                    "name": "runner",
                    "synchronization": {
                        "semaphores": [
                            {
                                "configMapKeyRef": {
                                    "name": "workflow-semaphores",
                                    "key": "ghost-container-qa",
                                }
                            }
                        ]
                    },
                },
                {
                    "name": "pipeline",
                    "dag": {
                        "tasks": [
                            {
                                "name": "test-lane",
                                "withItems": ["smoke", "common"],
                                "templateRef": {
                                    "name": "leaky",
                                    "template": "runner",
                                },
                            }
                        ]
                    },
                },
            ]
        },
    }
    path = tmp_path / "leaky.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert topology.main(["check", str(tmp_path)]) == 1

    manifest["spec"]["templates"][1]["parallelism"] = 2
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert topology.main(["check", str(tmp_path)]) == 0


def test_checker_rejects_workflow_level_semaphores(tmp_path):
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "WorkflowTemplate",
        "metadata": {"name": "greedy-parent"},
        "spec": {
            "synchronization": {
                "semaphores": [
                    {
                        "configMapKeyRef": {
                            "name": "workflow-semaphores",
                            "key": "ghost-container-qa",
                        }
                    }
                ]
            },
            "templates": [{"name": "noop", "container": {"image": "ghcr.io/x/y"}}],
        },
    }
    (tmp_path / "greedy.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert topology.main(["check", str(tmp_path)]) == 1


def test_checker_allows_dag_serialized_holders(tmp_path):
    """Lanes chained with `depends` are serialized and need no parallelism cap."""
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "WorkflowTemplate",
        "metadata": {"name": "chained"},
        "spec": {
            "templates": [
                {
                    "name": "runner",
                    "synchronization": {
                        "semaphores": [
                            {
                                "configMapKeyRef": {
                                    "name": "workflow-semaphores",
                                    "key": "ghost-container-qa",
                                }
                            }
                        ]
                    },
                },
                {
                    "name": "pipeline",
                    "dag": {
                        "tasks": [
                            {
                                "name": "first",
                                "templateRef": {
                                    "name": "chained",
                                    "template": "runner",
                                },
                            },
                            {
                                "name": "second",
                                "depends": "first.Succeeded",
                                "templateRef": {
                                    "name": "chained",
                                    "template": "runner",
                                },
                            },
                        ]
                    },
                },
            ]
        },
    }
    (tmp_path / "chained.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert topology.main(["check", str(tmp_path)]) == 0
