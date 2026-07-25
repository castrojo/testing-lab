# ADR 0006 — Elastic BuildStream / BuildBarn build grid

Status: Accepted
Date: 2026-07-25

## Context

BuildBarn is the lab's remote-execution backend for BuildStream. It is
composed of:

- `frontend` (Deployment, 2 replicas spread across nodes)
- `scheduler` (Deployment, 1 replica)
- `storage` (StatefulSet, 2 replicas with podAntiAffinity, `local-path` PVCs)
- `worker` (DaemonSet — one pod per node)
- `bb-remote-asset` (Deployment, 1 replica)

The `worker` DaemonSet is the elastic piece: when a node joins the cluster,
Kubernetes is responsible for scheduling a worker pod onto it. The worker's
runner registers with the scheduler and starts accepting BuildStream actions.
That makes the "add a second PC, builds get faster with zero config" story
possible.

Today the worker pod's runner container requests **8 CPU and 16 GiB memory**,
with the worker container adding another 0.5 CPU and 1 GiB. That 8.5 CPU /
17 GiB admission footprint is larger than many homelab nodes can offer,
especially a newly added second PC. As a result the DaemonSet fails to admit
on small nodes and the grid does not expand automatically.

The worker uses a node-local cache on a `hostPath` volume
(`/var/lib/buildbarn/worker`). The pod is already privileged, runs as
`spc_t`, and uses an unconfined seccomp profile, so spreading it to a new
node does not require new SELinux policy or additional hostPath directories.
The kubelet creates the directory via `type: DirectoryOrCreate`.

## Decision

**Keep the BuildBarn worker as a DaemonSet. Lower the runner container's
resource requests so a newly joined homelab node can admit a worker
automatically. Leave limits unchanged to preserve burst performance on large
nodes.**

### Runner requests before

```yaml
requests:
  cpu: "8"
  memory: 16Gi
```

### Runner requests after

```yaml
requests:
  cpu: "2"
  memory: 4Gi
```

Worker requests remain `500m` CPU / `1Gi` memory. The combined pod request is
now **2.5 CPU / 5 GiB**, which fits the documented minimum node spec of 16 GiB
RAM and leaves headroom for system pods and the large `bst-build` coordinator
pods.

## Design evaluation

Three options were considered:

| Option | Fit for the lab | Verdict |
|---|---|---|
| (a) DaemonSet + right-sized requests | Native k8s mechanism; already in use; new node = new worker automatically; no new controllers | **Chosen** |
| (b) Deployment + Cluster Proportional Autoscaler | Adds a non-CNCF-graduated controller and does not guarantee a worker on every node; loses the simple "one per node" mental model | Rejected |
| (c) Deployment + HPA on queue depth | Needs custom metrics from `bb-scheduler` and a `prometheus-adapter` (or equivalent custom metrics API). The cluster only runs a lightweight Prometheus scraper (`manifests/prometheus-lightweight.yaml`) with no adapter. | Rejected |

Option (a) is the only one that requires no new in-cluster controllers and
matches the user policy of scheduler-driven, no-pinning placement.

## Consequences

- A second PC that joins the shared k3s cluster will get a BuildBarn worker
  pod automatically if the node has enough allocatable CPU and memory to
  satisfy the reduced requests.
- The existing `hostPath` worker cache continues to work on new nodes;
  `/var/lib/buildbarn/worker` is created on first pod start.
- No additional SELinux work is required: the worker already runs as
  `spc_t` with a privileged, unconfined security context.
- Limits remain high so the same worker bursts on large nodes (e.g., `ghost`).
- Actual per-node concurrency is still bounded by the runner configuration in
  `manifests/buildbarn-config.yaml` (`concurrency: 12`). Very small nodes may
  become memory- or CPU-saturated before reaching that concurrency; that is
  acceptable because the grid now scales by node count, and the scheduler
  handles admission honestly.
- The `workflow-controller` PriorityClass on the worker is unchanged. Large
  `bst-build` pods (PriorityClass `bst-build`, higher) can still preempt a
  worker if a node is overcommitted.

## Connection to the onboarding mission

The "Add your second PC" mission (`missions/add-your-second-pc.json`) ends
with the elastic-grid payoff. The payoff step now tells the user to look for
a new `worker` pod on the new node automatically:

```bash
kubectl get pods -n buildbarn -o wide --field-selector spec.nodeName=<new-node>
```

No manual worker deployment or node label is required.

## Verification recipe

After a node joins the shared cluster:

1. Confirm the node is Ready:

   ```bash
   kubectl get nodes -o wide
   ```

2. Confirm a BuildBarn worker pod landed on the new node:

   ```bash
   kubectl get pods -n buildbarn -o wide --field-selector spec.nodeName=<new-node>
   ```

   Expected: a pod named `worker-<hash>` in `Running` state.

3. Submit a distributed build pipeline and watch actions spread:

   ```bash
   kubectl get pods -n buildbarn -o wide
   argo submit --from workflowtemplate/dakota-build-pipeline -n argo --wait --log
   ```

4. Check that the runner is registered with the scheduler and has available
   action slots:

   ```bash
   kubectl logs -n buildbarn daemonset/worker -c worker --tail=50 | grep -i "registered\|slots\|concurrency"
   ```

A successful expansion means the build wall time drops or the parallel action
count increases compared to a single-node run, with no manual worker
configuration.
