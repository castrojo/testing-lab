---
name: image-policy
description: >
  Registry and base-image choices for lab images under ArgoCD.
---

## Governing principle: CNCF tooling over distribution tooling

**Always prefer industry-standard CNCF/OCI tooling over distribution tooling.
This is the project's greatest competitive advantage — treat it as such.**

The lab composes capability from **purpose-built, signed, org-published OCI
images**. It does not build capability by layering distro packages into a
general-purpose base box. Concretely:

| Need | Use (CNCF / OCI) | Not (distro) |
|---|---|---|
| Registry ops | `ghcr.io/projectbluefin/skopeo`, `oras`, `crane` | `dnf install skopeo` |
| General CI utilities | `ghcr.io/projectbluefin/lab-runner:latest` | `dnf install jq git curl` |

**Why this is the advantage, not just a preference:**

- **Portable.** An OCI image runs identically on any node, any arch, any distro.
  A `dnf` recipe is welded to one distro's release cycle, package names, and
  patent politics.
- **Reproducible and attributable.** A digest is exact and permanent. "Whatever
  the mirror served that day" is neither, so failures cannot be bisected and
  results cannot be cited.
- **Signed and governed.** Org-published images pass through the registry
  allowlist and can be verified with `cosign`. A package repo contacted at
  runtime is invisible to `scripts/check_gitops_policy.py`.
- **Composable.** Multi-stage `COPY --from` lets you take exactly one binary
  from a purpose-built image. Package managers force you to take a dependency
  tree and its opinions.
- **Air-gap capable.** Every dependency is a pinned artifact that can be
  mirrored into zot ahead of time.

**Do not use RPM or `dnf`. Not at runtime, not in a Containerfile, not "just
this once" in a builder stage.** There is no acceptable-use section here on
purpose. Removing this legacy tooling is the point of the project, so this
document does not teach you when to reach for it.

**When the org does not already publish what you need, the answer is to add an
image to [`fsdk-containers`](https://github.com/projectbluefin/fsdk-containers)
— not to fall back to a package manager.** That keeps the capability signed,
SBOM'd, CVE-patched by FSDK, and reusable by the next workload. A `dnf` line
solves it once, for you, invisibly, and leaves the debt in the repo.

If a dependency ships an upstream release artifact (a static binary or tarball),
fetch that artifact directly at build time and verify its checksum. That is
upstream-native and reproducible; it is not distribution tooling.

## Image Policy

**Preference order (enforced by `just lint` registry allowlist):**

1. **[`fsdk-containers`](https://github.com/projectbluefin/fsdk-containers)** — if the image you need is missing, propose making one for the need.

**Verified contents (2026-08-21) — do not assume, these are commonly mis-stated:**

| Image | Contains | Does NOT contain |
|---|---|---|
| `lab-runner` | bash, curl, git, jq, python3.13, kubectl | skopeo, oras, tar, yq, PyYAML, podman |
| `skopeo` | skopeo 1.23.0 at `/usr/bin/skopeo` | any shell (distroless, no entrypoint) |
| `bluefin` | full ffmpeg (libx265, libsvtav1, ffv1, prores_ks), Mesa RADV, git, curl, tar, xz, python3 | — |

`bluefin` is an ostree/bootc image: `/opt` → `/var/opt`, `/usr/local` → `/var/usrlocal` and `/root` → `/var/roothome`, and `/var` is empty at build time. Install into `/usr/lib`, and set `HOME` to a real in-image path or anything writing to it fails with `FileExistsError`/`NotDir`.

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

