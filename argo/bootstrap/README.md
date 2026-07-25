# Bootstrap WorkflowTemplates

These WorkflowTemplates set up the cluster infrastructure. They are **not** managed
by ArgoCD — run them **once** during initial cluster setup, then leave them in place
as runnable idempotent runbooks.

## Why not GitOps-managed?

ArgoCD with `prune: true` would delete a resource the moment it's removed from git.
Bootstrap templates exist for long-term reference and re-execution (e.g., re-running
`ghost-kernel-args` after an OS update, or `setup-ghost-ssh-banner` after reimaging).
They belong in git as documentation and runbooks, not in the ArgoCD sync path.

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
| `install-kubestellar.yaml` | `install-kubestellar` | KubeStellar core via ArgoCD app (ADR-0003) | Multi-cluster setup |
| `register-wec.yaml` | `register-wec` | Register a cluster as a WEC with its1 (OCM join) | After install-kubestellar |
| `kubestellar-smoke-test.yaml` | `kubestellar-smoke-test` | BindingPolicy downsync + status upsync check | After register-wec |
| `install-test-vms.yaml` | `install-test-vms` | Apply initial test VM manifests | After KubeVirt ready |
| `ghost-kernel-args.yaml` | `ghost-kernel-args` | Strix Halo performance kernel args | New node / after OS update |
| `setup-ghost-ssh-banner.yaml` | `setup-ghost-ssh-banner` | API-only SSH warning banner | New node |
| `setup-otel.yaml` | `setup-otel` | Deploy observability stack | Optional |

## Running a template

```bash
argo submit --from workflowtemplate/<name> -n argo --wait --log
```

Example — re-run kernel arg tuning after a Bluefin update:
```bash
argo submit --from workflowtemplate/ghost-kernel-args -n argo --wait --log
# Schedule reboot after completion
```

## Full setup sequence

See [/docs/ops/bootstrap.md](/docs/ops/bootstrap.md) for the complete ordered guide.
