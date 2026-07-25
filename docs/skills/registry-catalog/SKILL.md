---
name: registry-catalog
description: >
  Working with the transparent registry catalog feature: the linuxserver.io
  catalog index, deploy-time install translation, validation, GitOps PR flow,
  and adding future providers (NGC/AMD/DockerHub). Use when editing
  docs/data/catalog/, scripts/collect_lsio_catalog.py,
  scripts/catalog_install_lsio.py, scripts/catalog_validate.py, or
  argo/workflow-templates/catalog-install-lsio.yaml.
metadata:
  context7-sources:
    - /argoproj/argo-workflows
    - /kubernetes/website
---

# Registry catalog — lab skill

## When to Use

- Adding or updating the linuxserver.io catalog poller
- Changing the provider index schema in `docs/reference/catalog-index-schema.md`
- Editing the install translator (`scripts/catalog_install_lsio.py`)
- Editing the offline validator (`scripts/catalog_validate.py`)
- Editing the install WorkflowTemplate (`argo/workflow-templates/catalog-install-lsio.yaml`)
- Adding a new catalog provider (NGC, AMD, DockerHub, etc.)
- Debugging a failed catalog install workflow

## When NOT to Use

- General Argo workflow authoring → `argo-workflows` skill
- ArgoCD sync issues → `gitops-argocd` skill
- Dashboard page work → `astro-dashboard-pages` skill

## Architecture

The catalog is intentionally thin. The repo holds metadata and pointers only;
it does **not** contain per-app Kubernetes manifests or vendored compose files.

```text
docs/data/catalog/linuxserver.json
  | poller regenerates weekly (manifests/catalog-lsio-poller.yaml)
  v
argo/workflow-templates/catalog-install-lsio.yaml
  | install time: fetch LSIO API config, translate, validate, apply/commit
  v
manifests/catalog-apps/<app>/manifest.yaml   (gitops mode PR)
```

### Index file

`docs/data/catalog/<provider>.json` follows the schema in
`docs/reference/catalog-index-schema.md`:

- `provider`, `generated_at`, `source_api`
- `apps[]`: `name`, `description`, `category`, `logo_url`, `image_ref`,
  `monthly_pulls`, `stars`, `architectures[]`, `config_pointer`,
  `readonly_supported`, `nonroot_supported`, `verified`

`config_pointer` is the upstream URL to fetch deploy-time configuration from.
For linuxserver.io it is the `application_setup` README anchor.

### Provider tiers

- **linuxserver.io** is the reference rich-API tier. The poller uses
  `https://api.linuxserver.io/api/v1/images?include_config=true`.
- Future providers (NGC, AMD, DockerHub) reuse the same index schema but may
  leave many fields `null`/`false`. They only need to populate `name`,
  `description`, `image_ref`, and `config_pointer`.

## Install-time translation

`scripts/catalog_install_lsio.py` reads the upstream LSIO config at install
time and renders Kubernetes manifests using one generic mapping:

| LSIO field | Kubernetes output |
|---|---|
| `config.env_vars` | Container `env:` (PUID/PGID/TZ + app-specific) |
| `config.volumes` | One `PersistentVolumeClaim` per volume + mount |
| `config.ports` | Container `ports:` + ClusterIP `Service` ports |
| `config.readonly_supported` / `nonroot_supported` | Pod `securityContext` |

Lab conventions applied:

- `storageClassName: local-path` on every PVC.
- PVC size heuristic based on mount path (`/config` 5Gi, media paths 100Gi,
  `/transcode` 50Gi, default 1Gi).
- Images emitted as bare `linuxserver/<app>:latest` so the cluster's
  zot-docker mirror resolves them (avoids the registry allowlist lint).
- PUID/PGID become env vars and, when the image supports it, a
  `securityContext` with `runAsUser`, `runAsGroup`, `fsGroup`, `runAsNonRoot`,
  and `readOnlyRootFilesystem`.
- A `ClusterIP` Service is emitted for in-cluster reachability. External
  ingress is deferred to Gateway API per ADR-0004.

## Modes

The install WorkflowTemplate takes a `mode` parameter:

- `gitops` (default):
  1. Render manifests.
  2. Run offline structural validation.
  3. Commit `manifests/catalog-apps/<app>/manifest.yaml` to branch
     `bot/catalog-install-<app>-<ts>`.
  4. Open a PR via `curl` + `python3` using the GitHub REST API.

- `imperative`:
  1. Render manifests.
  2. Run offline structural validation.
  3. `kubectl apply --dry-run=server -f` gate.
  4. `kubectl apply -f` the manifests.
  5. Push a capture branch (no PR opened).

## Validation contract

`scripts/catalog_validate.py` runs offline before apply or commit:

- Required fields: `apiVersion`, `kind`, `metadata.name`.
- Every container has `resources.requests` and `resources.limits` for `cpu`,
  `memory`, and `ephemeral-storage`.
- No `hostPath` volumes.
- Namespaced resources have `metadata.namespace`.
- PVC `storageClassName` is `local-path`.
- Container images are bare (implicit docker.io) or from the allowlisted
  registries.

## Failure modes learned live

- `lab-runner` has **no `gh` CLI and no `tar`**. Open PRs with `curl` + the
  GitHub REST API; do not rely on `gh pr create`.
- The `argo` namespace has a ResourceQuota that rejects pods without explicit
  `resources.requests` and `resources.limits`, including `ephemeral-storage`.
- `generated_at` timestamp-only changes in `docs/data/catalog/linuxserver.json`
  are skipped by the poller using `git diff -I '"generated_at"'`.
- Heredocs (`<<'EOF'`) inside YAML `script:` block scalars break the Argo
  linter. Build JSON payloads with inline `python3 -c` or write helper scripts
  to files instead.
- LSIO images must use the bare docker.io form in rendered manifests;
  `lscr.io/...` is not in the registry allowlist and fails CI lint.

## Commands

```bash
# Regenerate the catalog index locally
python3 scripts/collect_lsio_catalog.py

# Render an app for inspection
python3 scripts/catalog_install_lsio.py jellyfin --output-dir /tmp/rendered-jellyfin

# Validate rendered manifests
python3 scripts/catalog_validate.py /tmp/rendered-jellyfin/manifest.yaml

# Lint
just lint

# Run unit tests
python3 -m pytest tests/unit/test_catalog_install_lsio.py tests/unit/test_catalog_validate.py -v

# Submit a gitops install (example)
argo submit --from workflowtemplate/catalog-install-lsio \
  -p app=jellyfin \
  -p mode=gitops \
  -n argo --watch
```

## Adding a new provider

1. Add the provider section to `docs/reference/catalog-index-schema.md`.
2. Create `scripts/collect_<provider>_catalog.py` following the poller pattern.
3. Generate `docs/data/catalog/<provider>.json`.
4. Optionally extend `scripts/catalog_install_lsio.py` or add a provider-specific
   translator if the config shape differs from LSIO.
5. Update `manifests/catalog-lsio-poller.yaml` or add a new CronWorkflow.
6. Add golden-file tests under `tests/unit/`.
