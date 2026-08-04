# Bootstrap WorkflowTemplates

These WorkflowTemplates set up the cluster infrastructure. They are **not** managed
by ArgoCD — run them **once** during initial cluster setup, then leave them in place
as runnable idempotent runbooks.

## Why not GitOps-managed?

ArgoCD with `prune: true` would delete a resource the moment it's removed from git.
Bootstrap templates exist for long-term reference and re-execution (e.g., re-running
them during cluster setup). Host-level kernel and SSH changes are not bootstrap
WorkflowTemplates; use the maintainer-approved host procedures for those operations.
Bootstrap templates belong in git as documentation and runbooks, not in the ArgoCD
sync path.

## Applying

Apply once to the cluster so they're runnable:

```bash
kubectl apply -f argo/bootstrap/ -n argo
```

## Templates

| File | Template name | Purpose | Run when |
|---|---|---|---|
| `install-kubevirt.yaml` | `install-kubevirt` | Install KubeVirt (CNCF Incubating) | New cluster |
| `install-cdi.yaml` | `install-cdi` | Install CDI (disk import support) | New cluster |
| `install-kubevirt-manager.yaml` | `install-kubevirt-manager` | Web UI at NodePort :30180 | Optional |
| `install-test-vms.yaml` | `install-test-vms` | Apply initial test VM manifests | After KubeVirt ready |
| `setup-otel.yaml` | `setup-otel` | Deploy observability stack | Optional |

KubeStellar installation is owned by the `kubestellar-applications` ArgoCD
Application. Its reusable `register-wec` and `kubestellar-smoke-test`
WorkflowTemplates live in `argo/workflow-templates/` and are reconciled by
ArgoCD.

## Running a template

```bash
argo submit --from workflowtemplate/<name> -n argo --wait --log
```

## Full setup sequence

See [/docs/ops/bootstrap.md](/docs/ops/bootstrap.md) for the complete ordered guide.
