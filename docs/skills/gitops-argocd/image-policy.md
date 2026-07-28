---
name: image-policy
description: >
  Registry and base-image choices for lab images under ArgoCD.
---

## Image Policy

**Preference order (enforced by `just lint` registry allowlist):**

1. **`ghcr.io/projectbluefin/lab-runner:latest` (or other organization-owned containers)** — always preferred for pollers, GC, and CronWorkflows; includes prebuilt CLI utilities (kubectl, skopeo, oras, curl, jq) to eliminate external runtime package manager dependencies and guarantee offline/air-gapped resilience.
2. `cgr.dev/chainguard/*` — default choice for general/system infra and tooling images
3. For anything else: **ask the user** — do not assume distros
4. Fedora images are allowed when appropriate for Fedora/CoreOS-specific tooling
5. Banned: `registry.access.redhat.com` (UBI), `bitnami/*`, `docker.io/*` (except `docker.io/rocm/k8s-device-plugin` with `# registry-lint-ignore`)
2. For anything else: **ask the user** — do not assume distros
3. Fedora images are allowed when appropriate for Fedora/CoreOS-specific tooling
4. Banned: `registry.access.redhat.com` (UBI), `bitnami/*`, `docker.io/*` (except `docker.io/rocm/k8s-device-plugin` with `# registry-lint-ignore`)

**Critical Chainguard tag facts:**
- `cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795` ✅ (has apk, nsenter, full tooling)
- `cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795-dev` ❌ DOES NOT EXIST
- `cgr.dev/chainguard/kubectl:latest-dev` ✅ (has bash; `:latest` is distroless — no shell)
- `cgr.dev/chainguard/kubectl:latest` ❌ no bash — use `latest-dev` for steps that need shell

**Zot pull-through cache — 6 upstreams (as of 2026):**

| Upstream | NodePort path prefix |
|---|---|
| `ghcr.io` | `:30501/ghcr` |
| `docker.io` | `:30501/docker` |
| `quay.io` | `:30501/quay` |
| `registry.fedoraproject.org` | `:30501/fedora` |
| `registry.k8s.io` | `:30501/k8s` |
| `cgr.dev` | `:30501/cgr` |

All images in `argo/` and `manifests/` must use a registry from the allowlist in `.github/workflows/lint.yaml`.

## KDE lane integration pins

KDE Linux source advancement (MR !534 → master on invent.kde.org) is owned by
the `kde-source-tracker` CronWorkflow. Renovate covers only the integration
pins the KDE lane shares with the rest of the lab:

- `image-versions.kde.yaml` holds Renovate-managed workflow images such as
  `ghcr.io/projectbluefin/lab-runner`.
- `renovate.json` scopes the `kubernetes` manager to `kde-*.yaml` files and
  excludes `bst2` (versioned by the KDE build agent) and any future
  `invent.kde.org` image references.

Do not add the moving KDE source image or BuildStream tooling versions to
`image-versions.kde.yaml`; those stay with the source tracker and build agent.

