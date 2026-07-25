#!/usr/bin/env python3
"""Offline structural validator for rendered catalog app manifests.

Validates the YAML produced by scripts/catalog_install_lsio.py before it is
applied or committed. All checks are structural and stdlib-only: no cluster
access, no kubeconform, no external YAML parser.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ALLOWLISTED_REGISTRIES = {
    "ghcr.io",
    "quay.io",
    "registry.fedoraproject.org",
    "registry.k8s.io",
    "cgr.dev",
    "192.168.1.102",
    "192.168.1.102:30500",
    "192.168.1.102:30501",
    "localhost",
}

REQUIRED_CONTAINER_RESOURCES = {"cpu", "memory", "ephemeral-storage"}
NAMESPACED_KINDS = {"Deployment", "Service", "PersistentVolumeClaim", "ConfigMap", "Ingress", "HTTPRoute"}


class YamlParseError(Exception):
    pass


# ── Minimal YAML parser for the subset emitted by catalog_install_lsio.py ─────


def _indent_level(line: str) -> int:
    return len(line) - len(line.lstrip())


def _strip_comment(line: str) -> str:
    # Simple comment strip: ignore # unless inside quotes. Our output does not
    # put # in values, so this is safe.
    return line.split("#", 1)[0].rstrip()


def _unquote_scalar(text: str) -> str | int | float | bool | None:
    text = text.strip()
    if not text or text == "null" or text == "~":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "yes":
        return True
    if text == "no":
        return False
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if re.match(r"^-?\d+$", text):
        return int(text)
    if re.match(r"^-?\d+\.\d+$", text):
        return float(text)
    return text


def _parse_scalar(text: str) -> tuple:
    """Parse a scalar and return (value, leftover_text)."""
    text = text.strip()
    if not text:
        return None, ""
    # Quoted string
    if text[0] in ('"', "'"):
        quote = text[0]
        end = 1
        while end < len(text):
            if text[end] == quote and text[end - 1] != "\\":
                return text[1:end], text[end + 1:].strip()
            end += 1
        return text[1:], ""
    # Plain scalar: take until structural chars or comment at line level
    # In our output plain scalars never contain unquoted # or : at top level.
    return _unquote_scalar(text), ""


def _parse_block(lines: list[str], base_indent: int):
    """Parse a YAML block starting at the current position.

    Returns (value, remaining_lines).
    """
    if not lines:
        return None, lines

    first = _strip_comment(lines[0])
    stripped = first.lstrip()
    indent = _indent_level(first)

    if indent < base_indent:
        return None, lines

    if stripped.startswith("- "):
        return _parse_list(lines, base_indent)

    if ":" in stripped:
        return _parse_mapping(lines, base_indent)

    # Scalar at this level
    value, _ = _parse_scalar(stripped)
    return value, lines[1:]


def _parse_list(lines: list[str], base_indent: int):
    items = []
    while lines:
        line = _strip_comment(lines[0])
        if not line.strip():
            lines = lines[1:]
            continue
        indent = _indent_level(line)
        stripped = line.lstrip()
        if indent < base_indent or not stripped.startswith("- "):
            break
        content = stripped[2:].strip()
        if content:
            # If the content looks like a mapping entry, parse it as a mapping
            # nested at indent + 2, with following lines belonging to that mapping.
            if ":" in content:
                item_value, rest = _parse_mapping(
                    [" " * (indent + 2) + content] + lines[1:],
                    indent + 2,
                )
                items.append(item_value)
                lines = rest
            else:
                value, _ = _parse_scalar(content)
                items.append(value)
                lines = lines[1:]
        else:
            # Nested value on following lines
            nested, rest = _parse_block(lines[1:], indent + 2)
            items.append(nested)
            lines = rest
    return items, lines


def _parse_inline_mapping(text: str):
    """Parse a simple inline mapping like 'name: value, other: 2'."""
    result = {}
    parts = re.split(r", (?=\w+:\s)", text)
    for part in parts:
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        result[key.strip()], _ = _parse_scalar(val)
    return result, ""


def _parse_mapping(lines: list[str], base_indent: int):
    result = {}
    while lines:
        line = _strip_comment(lines[0])
        if not line.strip():
            lines = lines[1:]
            continue
        indent = _indent_level(line)
        stripped = line.lstrip()
        if indent < base_indent:
            break
        if indent > base_indent:
            # This should not happen at mapping level; nested already consumed
            raise YamlParseError(f"Unexpected indentation at line: {line!r}")
        if ":" not in stripped:
            break
        key, rest = stripped.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest:
            # Inline value or inline mapping
            if rest.startswith("{") and rest.endswith("}"):
                result[key] = {}
            elif rest.startswith("[") and rest.endswith("]"):
                result[key] = []
            else:
                # Could be inline mapping like labels:
                value, leftover = _parse_scalar(rest)
                if leftover:
                    value, _ = _parse_inline_mapping(rest)
                result[key] = value
            lines = lines[1:]
        else:
            # Nested block value
            nested, rest = _parse_block(lines[1:], base_indent + 2)
            result[key] = nested
            lines = rest
    return result, lines


def load_yaml_docs(text: str) -> list[dict]:
    """Parse a multi-document YAML string into a list of Python dicts."""
    docs = []
    for raw_doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        raw_doc = raw_doc.strip()
        if not raw_doc:
            continue
        lines = raw_doc.splitlines()
        # Remove empty leading/trailing lines
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        value, leftover = _parse_block(lines, 0)
        if leftover:
            raise YamlParseError(f"Unparsed YAML lines remain: {leftover[:3]}")
        if not isinstance(value, dict):
            raise YamlParseError(f"Document root is not a mapping: {type(value)}")
        docs.append(value)
    return docs


# ── Validator logic ──────────────────────────────────────────────────────────


def _registry_of(image: str) -> str:
    parts = image.split("/")
    if len(parts) <= 1:
        return ""
    registry = parts[0]
    # Bare name like linuxserver/jellyfin has no dot or colon -> implicit docker.io
    if not re.search(r"[.:]", registry):
        return ""
    return registry


def _errors_for_container(container: dict, path: str) -> list[str]:
    errors = []
    name = container.get("name", "<unnamed>")
    res = container.get("resources") or {}
    requests = res.get("requests") or {}
    limits = res.get("limits") or {}
    for key in REQUIRED_CONTAINER_RESOURCES:
        if key not in requests:
            errors.append(f"{path}.containers[{name}].resources.requests.{key} missing")
        if key not in limits:
            errors.append(f"{path}.containers[{name}].resources.limits.{key} missing")
    return errors


def _errors_for_volumes(volumes: list, path: str) -> list[str]:
    errors = []
    for vol in volumes or []:
        if not isinstance(vol, dict):
            continue
        if "hostPath" in vol:
            errors.append(f"{path}.volumes: hostPath volume '{vol.get('name')}' is not allowed")
    return errors


def validate_manifest(doc: dict) -> list[str]:
    """Return a list of validation errors for a single manifest document."""
    errors = []
    kind = doc.get("kind")
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    path = f"{kind}/{name}"

    if not doc.get("apiVersion"):
        errors.append(f"{path}: apiVersion is required")
    if not kind:
        errors.append(f"{path}: kind is required")
    if not name or name == "<unnamed>":
        errors.append(f"{path}: metadata.name is required")

    if kind in NAMESPACED_KINDS and not (doc.get("metadata") or {}).get("namespace"):
        errors.append(f"{path}: namespace is required for {kind}")

    if kind == "PersistentVolumeClaim":
        scn = (doc.get("spec") or {}).get("storageClassName")
        if scn != "local-path":
            errors.append(f"{path}: PVC storageClassName must be local-path, got {scn!r}")

    if kind == "Deployment":
        spec = doc.get("spec") or {}
        template = spec.get("template") or {}
        pod_spec = template.get("spec") or {}
        containers = pod_spec.get("containers") or []
        if not containers:
            errors.append(f"{path}: Deployment must have at least one container")
        for i, container in enumerate(containers):
            cpath = f"{path}.containers[{i}]"
            errors.extend(_errors_for_container(container, cpath))
        errors.extend(_errors_for_volumes(pod_spec.get("volumes"), path))

    # Image allowlist check for both top-level containers (Pods) and Deployment
    # pod template containers.
    containers_to_check = []
    if kind == "Deployment":
        pod_spec = (doc.get("spec") or {}).get("template", {}).get("spec", {})
        containers_to_check = pod_spec.get("containers") or []
    else:
        containers_to_check = (doc.get("spec") or {}).get("containers") or []

    for container in containers_to_check:
        image = container.get("image", "")
        registry = _registry_of(image)
        if registry and registry not in ALLOWLISTED_REGISTRIES:
            errors.append(
                f"{path}.containers[{container.get('name', '?')}].image uses uncached registry: {registry}"
            )

    return errors


def validate_manifests(docs: list[dict]) -> list[str]:
    """Validate a list of parsed manifest documents."""
    errors = []
    if not docs:
        errors.append("No documents found")
        return errors
    for doc in docs:
        errors.extend(validate_manifest(doc))
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate rendered catalog manifests.")
    parser.add_argument("manifest", help="Path to rendered manifest.yaml")
    parser.add_argument("--json", action="store_true", help="Output errors as JSON")
    args = parser.parse_args()

    text = Path(args.manifest).read_text()
    try:
        docs = load_yaml_docs(text)
    except YamlParseError as exc:
        errors = [f"YAML parse error: {exc}"]
    else:
        errors = validate_manifests(docs)

    if args.json:
        print(json.dumps({"valid": not errors, "errors": errors}))
    else:
        for err in errors:
            print(err, file=sys.stderr)
        if errors:
            print(f"Validation failed: {len(errors)} error(s)")
        else:
            print("Validation passed")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
