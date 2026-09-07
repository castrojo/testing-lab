# bluefin-test-suite Justfile
# GitOps policy:
#   - WorkflowTemplate changes go via git push to main; ArgoCD auto-syncs.
#   - Do NOT kubectl apply templates directly.
#   - Workflow submission and monitoring: use these just targets (argo/kubectl CLI).
#   - These recipes are the canonical interface for all routine lifecycle operations.
#   - Agents use these recipes or call argo/kubectl directly. No MCP required.
#   - ssh jorge@ghost is permitted for OS-level tasks only (k3s restart, systemd, brew).
#   - No recipe SSHes to ghost; do NOT add workstation SSH hops.
#   - Cluster bootstrap (setup-ssh-secret, setup-argocd) runs once from workstation.

image     := env_var_or_default("BLUEFIN_IMAGE", "ghcr.io/projectbluefin/bluefin:testing")
image_tag := env_var_or_default("BLUEFIN_IMAGE_TAG", "testing")
argo_ns   := "argo"

# List all available recipes
default:
    @just --list

# ── Bootstrap (run once) ─────────────────────────────────────────────────────

# Create bluefin-test-ssh-key secret in argo namespace (idempotent)
# The secret is read by bib-disk-configure via secretKeyRef — no pubkey env var needed.
setup-ssh-secret:
    #!/usr/bin/env bash
    set -euo pipefail
    if kubectl get secret bluefin-test-ssh-key -n {{ argo_ns }} &>/dev/null; then
        echo "✓ bluefin-test-ssh-key already exists"
        kubectl get secret bluefin-test-ssh-key -n {{ argo_ns }} \
            -o jsonpath="{.data.id_ed25519\.pub}" | base64 -d | ssh-keygen -lf - \
            && echo "(fingerprint above)"
        exit 0
    fi
    ssh_key=$(mktemp)
    ssh-keygen -t ed25519 -f "${ssh_key}" -N "" -C "bluefin-test-suite@ghost" >/dev/null
    kubectl create secret generic bluefin-test-ssh-key \
        --from-file=id_ed25519="${ssh_key}" \
        --from-file=id_ed25519.pub="${ssh_key}.pub" \
        -n {{ argo_ns }}
    shred -u "${ssh_key}" "${ssh_key}.pub"
    echo "✓ SSH secret created"

# Deploy the ArgoCD Application that auto-syncs argo/workflow-templates from git (run once)
# After this, template changes take effect on git push — no kubectl apply needed.
setup-argocd:
    kubectl apply -f argocd/application.yaml -n argocd
    @echo "✓ ArgoCD Application deployed — syncs argo/workflow-templates from main automatically"

# Recreate the ARC GitHub App secret in the arc-runners namespace (idempotent).
# Interactive by default; pass vars for non-interactive recovery after ARC reinstall.
# Usage: just setup-arc-github-secret
# Usage: just setup-arc-github-secret pem=/path/to/key.pem app_id=123 installation_id=456
setup-arc-github-secret pem="" app_id="" installation_id="" namespace="arc-runners" app_slug="bluefin-ghost-arc":
    #!/usr/bin/env bash
    set -euo pipefail
    args=()
    [[ -n "{{ pem }}" ]]           && args+=(--pem-path "{{ pem }}")
    [[ -n "{{ app_id }}" ]]        && args+=(--app-id "{{ app_id }}")
    [[ -n "{{ installation_id }}" ]] && args+=(--installation-id "{{ installation_id }}")
    [[ "{{ namespace }}" != "arc-runners" ]] && args+=(--namespace "{{ namespace }}")
    [[ "{{ app_slug }}" != "bluefin-ghost-arc" ]] && args+=(--app-slug "{{ app_slug }}")
    exec bash scripts/setup-arc-github-secret.sh "${args[@]}"

# ── Template management (GitOps — prefer git push over manual sync) ──────────

# Force ArgoCD to sync now instead of waiting for the next poll interval
argocd-sync:
    argocd app sync testing-lab testing-lab-infra --timeout 120
    argocd app wait testing-lab --health --timeout 120
    argocd app wait testing-lab-infra --health --timeout 120

# Show ArgoCD sync status for the test suite
argocd-status:
    argocd app get testing-lab
    argocd app get testing-lab-infra

# ── Test execution ───────────────────────────────────────────────────────────

# Run smoke tests against latest (or BLUEFIN_IMAGE_TAG)
run-tests:
    argo submit argo/bluefin-smoke-test.yaml \
        -p image="{{ image }}" \
        -p image-tag="{{ image_tag }}" \
        -n {{ argo_ns }} \
        --watch

# Run smoke tests against a specific tag
# Usage: just run-tests-tag lts-testing
run-tests-tag tag:
    #!/usr/bin/env bash
    set -euo pipefail
    image="ghcr.io/projectbluefin/bluefin"
    image_tag="{{ tag }}"
    variant="bluefin"
    if [[ "{{ tag }}" == lts-* ]]; then
        image="ghcr.io/projectbluefin/bluefin-lts"
        image_tag="${image_tag#lts-}"
        variant="bluefin-lts"
    fi
    argo submit argo/bluefin-smoke-test.yaml \
        -p image="${image}" \
        -p image-tag="${image_tag}" \
        -p variant="${variant}" \
        -n {{ argo_ns }} \
        --watch

# Run smoke tests for testing and lts-testing images in parallel.
run-tests-matrix:
    argo submit argo/bluefin-test-matrix.yaml \
        -n {{ argo_ns }} \
        --watch

# Run migration validation (bootc switch: ublue-os/bluefin → projectbluefin/bluefin)
# Usage: just run-migration-test
run-migration-test tag=image_tag:
    argo submit --from workflowtemplate/bluefin-migration-test \
        -p image-tag="{{ tag }}" \
        -n {{ argo_ns }} \
        --watch

# One-time: write SSH banner on ghost.
setup-ghost-ssh-banner:
    argo submit --from workflowtemplate/setup-ghost-ssh-banner \
        -n {{ argo_ns }} \
        --wait --log


# —— [REMOVED] titan VM recipes ——
# run-titan-smoke, run-titan-system, run-titan-developer, run-titan-software,
# setup-titan-fixtures, run-titan-disk-cleanup
# Titan persistent VMs are no longer GitOps-managed.

# Run Flatcar smoke tests
run-flatcar-smoke:
    argo submit argo/flatcar-smoke-test.yaml \
        -n {{ argo_ns }} \
        --watch

# Run the pinned KDE Linux native OVMF lane.
run-kde-linux:
    argo submit argo/kde-linux-qa.yaml \
        -n {{ argo_ns }} \
        --watch

# Run the mandatory red-path proof for the Aurora/KDE gate.
run-aurora-kde-sabotage:
    argo submit argo/aurora-kde-sabotage.yaml \
        -n {{ argo_ns }} \
        --watch

# Evaluate the rolling KDE soak window. A qualified result still needs human
# approval before the suite is promoted to CI gating.
evaluate-kde-soak:
    python3 scripts/evaluate_kde_soak.py docs/results/aurora-testing-smoke.json

# ── Observation ─────────────────────────────────────────────────────────────

# List all test workflows
list-workflows:
    argo list -n {{ argo_ns }}

# Tail logs from the most recent workflow
logs:
    argo logs -n {{ argo_ns }} @latest

# Report rolling uplink, WAN-estimate, workload, and Zot cache traffic.
# Usage: just traffic-report
# Usage: just traffic-report window=1h interface=enp191s0 limit=10
traffic-report *args:
    #!/usr/bin/env bash
    set -euo pipefail
    translated=()
    for arg in {{ args }}; do
        case "${arg}" in
            window=*) translated+=(--window "${arg#window=}") ;;
            prometheus-url=*) translated+=(--prometheus-url "${arg#prometheus-url=}") ;;
            prometheus_url=*) translated+=(--prometheus-url "${arg#prometheus_url=}") ;;
            interface=*) translated+=(--interface "${arg#interface=}") ;;
            limit=*) translated+=(--limit "${arg#limit=}") ;;
            *) translated+=("${arg}") ;;
        esac
    done
    exec python3 scripts/traffic_report.py "${translated[@]}"

# List VMs in all test namespaces
list-vms:
    @echo "=== bluefin-test ===" && kubectl get vm -n bluefin-test 2>/dev/null || true
    @echo "=== bluefin-lts-test ===" && kubectl get vm -n bluefin-lts-test 2>/dev/null || true
    @echo "=== flatcar-test ===" && kubectl get vm -n flatcar-test 2>/dev/null || true

# ── Cleanup ──────────────────────────────────────────────────────────────────

# Delete orphaned VMs in test namespaces (safe — never touches knuckle-test)
delete-vms:
    kubectl delete vm --all -n bluefin-test --ignore-not-found
    kubectl delete vm --all -n bluefin-lts-test --ignore-not-found
    kubectl delete vm --all -n flatcar-test --ignore-not-found

# Delete all test workflows
delete-workflows:
    argo delete --all -n {{ argo_ns }} || true

# Full teardown of in-flight resources
teardown:
    just delete-vms
    just delete-workflows

# ── In-cluster homelab substrate ─────────────────────────────────────────────

# Run in-cluster homelab substrate lifecycle tests
run-homelab-substrate:
    argo submit --from workflowtemplate/homelab-substrate \
      -n {{ argo_ns }} --wait --log

# Run in-cluster homelab storage persistence tests
run-homelab-storage:
    argo submit --from workflowtemplate/homelab-storage \
      -n {{ argo_ns }} --wait --log

# Run in-cluster homelab access probe (includes HTTPS exposure lane #58)
run-homelab-access:
    argo submit --from workflowtemplate/homelab-access-probe \
      -n {{ argo_ns }} --wait --log

# Run on-demand K8sGPT cluster analysis
# Usage: just run-k8sgpt
# Usage: just run-k8sgpt argo "Pod,Deployment"
run-k8sgpt namespace="" filters="Pod,Deployment,Service,Ingress,Node":
    argo submit --from workflowtemplate/k8sgpt-on-demand \
      -p namespace="{{ namespace }}" \
      -p filters="{{ filters }}" \
      -n {{ argo_ns }} --wait --log

# Verify the GitOps-managed KubeStellar platform and final smoke acceptance gate
run-kubestellar-verify wec-name="ghost":
    argo submit --from workflowtemplate/kubestellar-platform-verify \
      -p wec-name="{{ wec-name }}" \
      -n {{ argo_ns }} --wait --log

# Run first PVC/local-path restore drill (#60 #74 #84)
run-homelab-restore:
    argo submit --from workflowtemplate/homelab-restore-drill \
      -n {{ argo_ns }} --wait --log

# ── Service-catalog workload validation (#51) ────────────────────────────────

# Run service-catalog pipeline for a given lane (default: media)
# Usage: just run-service-catalog-smoke
# Usage: just run-service-catalog-smoke lane=non-media
# Usage: just run-service-catalog-smoke lane=media image-tag=lts branch=feat/my-branch
run-service-catalog-smoke lane="media" image-tag="latest" branch="main":
    argo submit --from workflowtemplate/service-catalog-pipeline \
      -p lane={{ lane }} \
      -p image-tag={{ image-tag }} \
      -p branch={{ branch }} \
      -n {{ argo_ns }} --wait --log

# ── Ghost maintenance ─────────────────────────────────────────────────────────

# Patch ghost OTel collector config to remove noisy process scraper (#117)
run-otel-patch:
    argo submit --from workflowtemplate/ghost-otel-patch \
      -n {{ argo_ns }} --wait --log

# Clear stale podman containers-storage lock files on ghost (run when no BIB workflows active)
run-ghost-cleanup:
    argo submit --from workflowtemplate/ghost-cleanup \
      -n {{ argo_ns }} --wait --log

# Set Strix Halo performance kernel args on ghost via rpm-ostree (reboot required after)
run-kernel-args:
    argo submit --from workflowtemplate/ghost-kernel-args \
      -n {{ argo_ns }} --wait --log

# ── Dakota BST builds ────────────────────────────────────────────────────────

# Show the MergeRaptor-owned lab Check Run for a PR head commit.
# Usage: just lab-check-status <repo> <pr_number>
lab-check-status repo pr_number:
    #!/usr/bin/env bash
    set -euo pipefail
    REPO="projectbluefin/{{ repo }}"
    SHA=$(gh pr view {{ pr_number }} --repo "${REPO}" --json headRefOid --jq .headRefOid)
    gh api --method GET "repos/${REPO}/commits/${SHA}/check-runs" \
        -f per_page=100 \
        --jq '.check_runs[]
          | select(.name == "testing-lab / {{ repo }}" and .app.slug == "mergeraptor")
          | {
              id,
              status,
              conclusion,
              started_at,
              completed_at,
              details_url,
              title: .output.title,
              summary: .output.summary
            }'

# Run Dakota BST pipeline (default bluefin variant only; NVIDIA disabled)
# Usage: just run-bst-build
# Usage: just run-bst-build testing https://github.com/projectbluefin/dakota.git
run-bst-build ref="testing" repo="https://github.com/projectbluefin/dakota.git":
    argo submit --from workflowtemplate/dakota-build-pipeline \
      -p ref={{ ref }} \
      -p repo={{ repo }} \
      -p build-mode=re \
      -n {{ argo_ns }} --watch

# Re-run the Dakota poller for the current testing SHA without bypassing BST admission.
force-dakota-poll:
    argo submit --from cronworkflow/dakota-commit-poller \
      -p force=true \
      -n {{ argo_ns }} --watch

# Compatibility alias for older docs/callers.
run-dakota-validate ref="testing" repo="https://github.com/projectbluefin/dakota.git":
    just run-bst-build {{ ref }} {{ repo }}

# Compatibility alias for older docs/callers.
run-dakota-build ref="testing" repo="https://github.com/projectbluefin/dakota.git":
    just run-bst-build {{ ref }} {{ repo }}

# Full Dakota QA pipeline: container-only suite fan-out against the published Dakota image.
run-dakota-qa branch="main" variant="dakota":
    argo submit --from workflowtemplate/dakota-qa-pipeline \
      -p variant={{ variant }} \
      -p branch={{ branch }} \
      -n {{ argo_ns }} --watch

# Legacy Dakota containerized smoke lane: run behave suites directly inside the OCI
# image with explicit image/variant overrides.
run-dakota-container-qa image-tag="testing" variant="dakota":
    argo submit --from workflowtemplate/dakota-container-qa-pipeline \
      -p image=192.168.1.102:30500/{{ variant }} \
      -p image-tag={{ image-tag }} \
      -p variant={{ variant }} \
      -n {{ argo_ns }} --watch

# Validate canonical Dakota build/publish history records.
validate-dakota-history:
    python3 scripts/publish_dakota_run.py validate-history

# Compare the latest Dakota build/publish runs with the preceding window.
# Usage: just report-dakota-history 20
report-dakota-history window="20":
    python3 scripts/publish_dakota_run.py report --window {{ window }}

# Promote one immutable Zot candidate to :testing after its lane passes QA.
# Usage: just run-zot-promotion dakota-testing dakota candidate-<sha> sha256:<digest>
run-zot-promotion lane repository candidate_tag expected_digest:
    argo submit --from workflowtemplate/zot-candidate-lifecycle \
      -p lane={{ lane }} \
      -p repository={{ repository }} \
      -p candidate-tag={{ candidate_tag }} \
      -p expected-digest={{ expected_digest }} \
      -n {{ argo_ns }} --watch

# Run the in-cluster BuildStream build pipeline for bluefin-server
# Usage: just run-bluefin-server-build
run-bluefin-server-build ref="main" repo="https://github.com/projectbluefin/server.git":
    argo submit --from workflowtemplate/bluefin-server-build-pipeline \
      -p ref={{ ref }} \
      -p repo={{ repo }} \
      -n {{ argo_ns }} --watch

# Run the isolated operator-only RECC baseline.
# Usage: just run-recc-baseline mode=cache-only cache-policy=both recc-provider=components/buildbox.bst
run-recc-baseline *args:
    #!/usr/bin/env bash
    set -euo pipefail
    MODE="buildstream-only"
    RUN_ID=""
    CACHE_POLICY="cold"
    RECC_PROVIDER="freedesktop-sdk.bst:components/buildbox.bst"
    for arg in {{ args }}; do
      case "${arg}" in
        mode=*) MODE="${arg#mode=}" ;;
        run-id=*) RUN_ID="${arg#run-id=}" ;;
        cache-policy=*) CACHE_POLICY="${arg#cache-policy=}" ;;
        recc-provider=*) RECC_PROVIDER="${arg#recc-provider=}" ;;
        *)
          echo "Unsupported run-recc-baseline argument: ${arg}" >&2
          exit 2
          ;;
      esac
    done
    exec argo submit --from workflowtemplate/recc-baseline-pipeline \
      -p mode="${MODE}" \
      -p run-id="${RUN_ID}" \
      -p cache-policy="${CACHE_POLICY}" \
      -p recc-provider="${RECC_PROVIDER}" \
      -n {{ argo_ns }} --watch

# ── Validation ───────────────────────────────────────────────────────────────

# Validate the reusable BuildStream OCI rechunk transform.
test-rechunk:
    bash -n scripts/rechunk_bst_image.sh
    python -m pytest -q tests/unit/test_rechunk_bst_image.py

# Apply bootstrap WorkflowTemplates to the cluster (run once during initial setup)
apply-bootstrap:
    kubectl apply -f argo/bootstrap/ -n {{ argo_ns }}
    @echo "✓ Bootstrap templates applied — run individual templates with: argo submit --from workflowtemplate/<name> -n argo --wait --log"

# Lint all Argo YAML manifests.
# WorkflowTemplates are linted together (--offline) so cross-file templateRef
# references (e.g. dakota-commit-poller → dakota-build-pipeline) resolve without needing
# the Argo server to have the new templates already synced.
# Standalone Workflow files (argo/*.yaml) reference server-side templates and
# are linted individually against the live server.
lint:
    @echo "Linting argo/workflow-templates/ (offline, cross-file refs)..."
    @argo lint --offline argo/workflow-templates/
    @echo "✔ workflow-templates: no linting errors found!"
    @echo "Linting argo/bootstrap/ (offline)..."
    @argo lint --offline argo/bootstrap/
    @echo "✔ bootstrap: no linting errors found!"
    @for f in argo/*.yaml; do \
        echo "Linting $f..."; \
        argo lint --offline argo/workflow-templates/ "$f" || exit 1; \
    done
    @echo "Checking semaphore topology..."
    @python3 scripts/check_semaphore_topology.py argo/
    @echo "✓ All manifests valid"

# Run the Python checks over the trees declared in .python-scope — the single
# source of truth also consumed by .github/workflows/lint.yaml (syntax, blocking)
# and .github/workflows/ci.yml (ruff, advisory). Keeps local == CI.
check-python:
    #!/usr/bin/env bash
    set -euo pipefail
    mapfile -t TREES < <(grep -vE '^[[:space:]]*(#|$)' .python-scope)
    echo "Python check scope (.python-scope): ${TREES[*]}"
    echo "Validating Python syntax..."
    find "${TREES[@]}" -name '*.py' -print0 | xargs -0 python3 -m py_compile
    echo "✔ syntax OK"
    echo "Linting with ruff (advisory)..."
    python3 -m ruff check "${TREES[@]}" || echo "WARNING: ruff reported findings (advisory, not a gate)"
