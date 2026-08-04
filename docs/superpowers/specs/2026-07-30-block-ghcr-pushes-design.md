---
name: block-ghcr-pushes
description: Prevent Argo workflows from pushing artifacts to ghcr.io while preserving GHCR pulls.
---

# Block GHCR Pushes

## Goal

Prevent every workflow in the cluster from pushing container images or OCI
artifacts to `ghcr.io`. Existing GHCR pulls, image references, and local Zot
publishing remain supported.

## Scope

- Keep GHCR references used as workflow inputs, runner images, polling targets,
  and pull-only sources.
- Remove the hard-coded GHCR publication destination from the Dakota publish
  workflow.
- Guard parameterized image-push workflows so a `ghcr.io` registry is rejected
  before authentication or upload.
- Disable the KDE and GNOME screenshot publication steps explicitly, because
  they have no non-GHCR destination parameter. They must report that GHCR
  publication is disabled rather than silently claim success.
- Update the Git-tracked Argo templates only; ArgoCD remains responsible for
  reconciling the cluster.

## Design

Generic build workflows retain their existing registry parameters and local
Zot behavior. Each push-producing template validates the registry host before
the first push command. A normalized host comparison rejects `ghcr.io` and
subdomains, while allowing the configured local registry.

The Dakota publication template is no longer a valid GHCR publisher. Its
destination is removed or replaced with an explicit disabled path, and the
workflow exits with a clear status if submitted. Existing pull and digest
verification behavior is unchanged.

The KDE and GNOME workflows skip their screenshot upload block and emit a
stable diagnostic. Test execution and result collection continue; only the
optional screenshot artifact publication is disabled.

## Safety and Errors

- Validation happens before registry login or push.
- A forbidden GHCR destination produces a non-zero workflow error for generic
  build/publish workflows.
- Screenshot publication is an explicit skip, not a silent success.
- No running workflow is mutated in place. New submissions use reconciled
  templates; existing workflows finish under their submitted specification.

## Verification

1. Search tracked workflow sources for every push verb and verify no path can
   target `ghcr.io`.
2. Lint the changed workflow templates with the repository's `just` entrypoint.
3. Confirm ArgoCD reconciles the updated templates.
4. Inspect the live templates and verify GHCR remains only in pull/input
   contexts, except for explicit rejection/disabled diagnostics.
