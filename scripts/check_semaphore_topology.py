#!/usr/bin/env python3
"""Enforce the lab semaphore topology invariants.

Two rules, both learned from a live starvation incident on ghost:

1. ConfigMap-backed semaphores are declared on the leaf template that consumes
   the scarce resource, never at ``spec.synchronization``. A workflow-level
   semaphore is held for the entire run, so a parent would hold a slot its own
   children are queued for — a self-deadlock.

2. Any ``dag``/``steps`` template that can start two or more semaphore-holding
   children at once (via ``withItems``/``withParam``/``withSequence``, or via
   several sibling tasks) must declare its own ``parallelism``.
   ``spec.parallelism`` is NOT inherited across ``templateRef`` (only a
   spec-level ``workflowTemplateRef`` inherits it), so without a template-level
   cap a single fan-out can hold every slot at once.

Usage: check_semaphore_topology.py [path ...]   (default: argo/)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

DEFAULT_ROOTS = ["argo"]


def semaphore_keys(sync: object) -> list[str]:
    """Return the ConfigMap semaphore keys referenced by a synchronization block."""
    if not isinstance(sync, dict):
        return []
    entries = sync.get("semaphores") or []
    if isinstance(sync.get("semaphore"), dict):
        entries = [*entries, sync["semaphore"]]
    keys = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("configMapKeyRef") or {}
        key = ref.get("key")
        if key:
            keys.append(key)
    return keys


FANOUT_FIELDS = ("withItems", "withParam", "withSequence")


def template_refs(template: dict) -> list[dict]:
    """Return one record per ``templateRef`` task in a ``dag``/``steps`` template.

    Each record carries the task ``name``, its ``target`` (``name/template``),
    whether it ``loops`` into several concurrent children, and the sibling task
    names it ``depends`` on. Steps groups run sequentially, so every step in
    group *n* implicitly depends on every step in group *n-1*.
    """
    records: list[dict] = []
    tasks: list[tuple[dict, list[str]]] = []

    dag = template.get("dag")
    if isinstance(dag, dict):
        for task in dag.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            depends = task.get("dependencies") or []
            if not isinstance(depends, list):
                depends = []
            expr = task.get("depends")
            if isinstance(expr, str):
                depends = [*depends, *re.findall(r"[A-Za-z0-9_-]+", expr)]
            tasks.append((task, depends))

    steps = template.get("steps")
    if isinstance(steps, list):
        previous: list[str] = []
        for group in steps:
            if not isinstance(group, list):
                continue
            current = []
            for step in group:
                if not isinstance(step, dict):
                    continue
                tasks.append((step, list(previous)))
                current.append(step.get("name", ""))
            previous = current

    for task, depends in tasks:
        ref = task.get("templateRef")
        if not isinstance(ref, dict) or not ref.get("name") or not ref.get("template"):
            continue
        records.append(
            {
                "name": task.get("name", ""),
                "target": f"{ref['name']}/{ref['template']}",
                "loops": any(task.get(field) is not None for field in FANOUT_FIELDS),
                "depends": depends,
            }
        )
    return records


def _reaches(name: str, targets: set[str], by_name: dict[str, dict]) -> bool:
    """True when ``name`` transitively depends on another task in ``targets``."""
    seen: set[str] = set()
    stack = list(by_name.get(name, {}).get("depends", []))
    while stack:
        dep = stack.pop()
        if dep in seen or dep == name:
            continue
        seen.add(dep)
        if dep in targets:
            return True
        stack.extend(by_name.get(dep, {}).get("depends", []))
    return False


def load_docs(paths: list[Path]):
    for path in paths:
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError as exc:  # pragma: no cover - surfaced to the user
            yield path, None, exc
            continue
        for doc in docs:
            if isinstance(doc, dict):
                yield path, doc, None


def collect(paths: list[Path]) -> tuple[dict, list[tuple], list[str]]:
    """Return (holders, consumers, errors) across every manifest in ``paths``."""
    holders: dict[str, set[str]] = {}
    consumers: list[tuple] = []
    errors: list[str] = []

    for path, doc, exc in load_docs(paths):
        if exc is not None:
            errors.append(f"{path}: unparseable YAML: {exc}")
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            continue
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")

        for key in semaphore_keys(spec.get("synchronization")):
            errors.append(
                f"{path}: {name} declares semaphore '{key}' at spec.synchronization. "
                "Workflow-level semaphores are held for the whole run and starve "
                "the children that need the same key; move it to the leaf template."
            )

        templates = spec.get("templates")
        if not isinstance(templates, list):
            continue
        for template in templates:
            if not isinstance(template, dict):
                continue
            tname = template.get("name", "<unnamed>")
            for key in semaphore_keys(template.get("synchronization")):
                holders.setdefault(key, set()).add(f"{name}/{tname}")
            refs = template_refs(template)
            if refs:
                consumers.append((path, name, tname, template.get("parallelism"), refs))

    return holders, consumers, errors


def main(argv: list[str]) -> int:
    roots = [Path(p) for p in (argv[1:] or DEFAULT_ROOTS)]
    paths = sorted(
        {p for root in roots for p in (root.rglob("*.yaml") if root.is_dir() else [root])}
    )
    holders, consumers, errors = collect(paths)

    # target template -> semaphore keys it holds
    keys_by_target: dict[str, set[str]] = {}
    for key, owners in holders.items():
        for owner in owners:
            keys_by_target.setdefault(owner, set()).add(key)

    for path, wf_name, tname, parallelism, refs in consumers:
        if parallelism is not None:
            continue
        by_name = {ref["name"]: ref for ref in refs if ref["name"]}
        # How many children of this template can hold the same key at once?
        # Tasks chained behind another holder of the same key are serialized by
        # the DAG and do not contend. A looping task is unbounded on its own.
        contended = []
        for key in sorted({k for ref in refs for k in keys_by_target.get(ref["target"], ())}):
            holders_for_key = [
                ref for ref in refs if key in keys_by_target.get(ref["target"], ())
            ]
            names_for_key = {ref["name"] for ref in holders_for_key if ref["name"]}
            concurrent = 0
            for ref in holders_for_key:
                if ref["loops"]:
                    concurrent += 2
                elif _reaches(ref["name"], names_for_key, by_name):
                    continue
                else:
                    concurrent += 1
            if concurrent > 1:
                contended.append(key)
        if contended:
            errors.append(
                f"{path}: template '{wf_name}/{tname}' can start several children "
                f"contending for semaphore(s) {', '.join(contended)} but declares "
                "no 'parallelism'. spec.parallelism is not inherited through "
                "templateRef, so this fan-out can hold every slot at once."
            )

    if errors:
        print("Semaphore topology check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  ✗ {err}", file=sys.stderr)
        return 1

    print(
        f"✓ semaphore topology: {len(holders)} key(s), "
        f"{len(consumers)} fan-out template(s) checked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
