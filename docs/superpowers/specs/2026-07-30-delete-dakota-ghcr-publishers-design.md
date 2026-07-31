---
name: delete-dakota-ghcr-publishers
description: Delete the scheduled and reusable Dakota GHCR publication entry points.
---

# Delete Dakota GHCR Publishers

## Goal

Remove both Dakota publication entry points so the cluster cannot schedule or
manually submit the obsolete GHCR publication workflow.

## Scope

- Delete `manifests/nightly-dakota-publish.yaml`.
- Delete `argo/workflow-templates/dakota-publish-pipeline.yaml`.
- Remove documentation and test references that describe either resource as
  available or suspended.
- Preserve Dakota build, QA, local Zot, and pull-only workflows.
- Let ArgoCD prune the deleted resources; do not delete them out of band.

## Design

The GitOps source becomes authoritative: deleting both tracked manifests causes
ArgoCD to remove the CronWorkflow and WorkflowTemplate from the `argo`
namespace. No replacement schedule or manual publication path is added.

Documentation will describe Dakota build and QA flows without a publication
lane. Tests that validate the deleted templates or schedule are removed or
rewritten to assert the remaining workflows do not reference them.

## Safety and Errors

- Existing running publication workflows are not mutated; inspect and
  terminate any active instance before reconciliation if one exists.
- ArgoCD must report the managed application as Synced and Healthy after prune.
- Live checks must confirm both resource names return NotFound.
- No direct `kubectl delete` or `kubectl apply` is used.

## Verification

1. Search tracked sources for references to both deleted resource names.
2. Run targeted unit tests and `just lint`.
3. Merge the GitOps change and run `just argocd-sync`.
4. Confirm both resources are absent and the ArgoCD applications are
   Synced/Healthy.
