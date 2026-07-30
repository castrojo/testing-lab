"""Regression coverage for argo-quota resource admission requirements."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow_templates(document):
    spec = document.get("spec") or {}
    return (spec.get("templates") or []) + (
        (spec.get("workflowSpec") or {}).get("templates") or []
    )


def _executable_containers(template):
    for key in ("script", "container"):
        container = template.get(key)
        if isinstance(container, dict):
            yield f"{key}:{template.get('name')}", container
    for container in template.get("initContainers") or []:
        yield f"init:{container.get('name')}:{template.get('name')}", container


def test_all_argo_workflow_containers_have_quota_resources():
    missing = []
    for path in sorted((ROOT / "argo").rglob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            for template in _workflow_templates(document):
                if not isinstance(template, dict):
                    continue
                for name, container in _executable_containers(template):
                    resources = container.get("resources") or {}
                    if not all(
                        isinstance(resources.get(kind), dict)
                        for kind in ("requests", "limits")
                    ):
                        missing.append(f"{path.relative_to(ROOT)}:{name}")

    assert not missing, "containers missing argo-quota resources:\n" + "\n".join(missing)
