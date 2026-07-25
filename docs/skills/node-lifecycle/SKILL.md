---
name: node-lifecycle
description: >
  Add, drain, and remove capacity in the Bluefin Server home cluster:
  k3s node join (shared-cluster expansion), WEC cluster join, and the
  elastic BuildStream grid checks. Agent-executable counterpart of the
  GUI onboarding flow. Use when adding a second PC, removing a node, or
  verifying builds scale with new capacity.
metadata:
  context7-sources:
    - /k3s-io/k3s
    - /kubestellar/kubestellar
---

# Node & Cluster Lifecycle — lab Skill

## When to Use

- User adds a PC to the home cluster ("second PC" killer feature)
- Registering an entire separate cluster as a WEC
- Draining/removing a node or WEC
- Verifying the BST build grid expanded with new capacity

## When NOT to Use

- Flatcar-specific onboarding → `flatcar-node-onboarding/SKILL.md`
- KubeStellar install/BindingPolicy mechanics → `kubestellar/SKILL.md`

## Decision: node join vs WEC join (ADR-0003)

- **Default: shared-cluster node join.** A new PC at the same site joins
  the existing k3s cluster as an agent. One scheduling domain, live
  migration possible, zero extra control-plane overhead.
- **WEC join** is for a *separate cluster* (another site, a machine that
  must stay autonomous offline). KubeStellar federates clusters, not PCs.

## Node join (shared cluster)

On the server (ghost): get the join token from the k3s server config
(never commit it). On the new machine running Bluefin Server, install k3s
agent as a persistent systemd service:

```bash
# On the new node
curl -sfL https://get.k3s.io | \
  K3S_URL="https://<server-lan-ip>:6443" \
  K3S_TOKEN="<token>" \
  INSTALL_K3S_BIN_DIR="/var/usrlocal/bin" \
  sh -s -
```

This creates the `k3s-agent` systemd unit; it starts automatically and
rejoins after reboot. Use `/etc/rancher/k3s/config.yaml` plus
`systemctl enable --now k3s-agent` if you prefer explicit configuration.

Verify from any kubectl:

```bash
kubectl get nodes -o wide          # new node Ready
kubectl get pods -A -o wide | grep <node>   # scheduler placing pods
```

Post-join checklist:

1. **No pinning** — do not add node selectors/affinities for the new node;
   scheduler-driven placement picks it up (user policy).
2. **Storage labels** — if the node carries a data disk for a hostPath
   workload (e.g. zot-cache), apply the capability label
   (`lab.projectbluefin.io/zot-cache-data=true` style). Never hostPath onto
   root disks.
3. **local-path config** — add the node's data-disk path to the
   local-path-provisioner nodePathMap (manifests/, via PR) so PVCs land on
   the data disk, not the root disk.

## WEC join (separate cluster)

From a workflow/agent with hub access:

```bash
argo submit --from workflowtemplate/register-wec -n argo --wait --log \
  -p wec-name=<cluster-name>
```

For remote clusters the join runs *on the remote side* (egress-only):
`clusteradm join --hub-token ... --hub-apiserver <reachable-endpoint>
--singleton`, then accept on the hub. External reachability of its1 is a
prerequisite (Gateway API TLSRoute or LoadBalancer — ADR-0004); in-LAN
clusters can use the node IP.

Zero-touch option: bootstrap tokens + `ManagedClusterAutoApproval` feature
gate with `--cluster-auto-approval-users=` lets a Bluefin Server image
join with no admin action.

## Drain / remove

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl delete node <node>
# WEC removal:
kubectl delete managedcluster <wec>    # after evacuating BindingPolicies
```

Check before draining: PVCs with local-path volumes on that node (data does
not move — reprovision or restore), KubeVirt VMIs (migrate or stop), BST
workers (grid shrinks; builds slow but continue).

## Elastic BST grid check (the payoff)

After adding capacity, verify builds actually got faster:

```bash
kubectl get pods -n buildbarn -o wide       # workers spread onto new node
argo submit --from workflowtemplate/dakota-build-pipeline -n argo ...
# Compare wall time and parallel job count against the previous run
```

BuildBarn workers scale with cluster capacity; bb-remote-asset:8984 +
frontend:8980 CAS pattern is grid-ready (see BuildStream source cache
memory/ADR). Cross-WEC grid joins via Tailscale CAS mesh.

## GUI flow

The Console hosts this as a guided mission ("Add your second PC") wrapping
the same steps: token display, agent config, verification queries. The
mission source lives at
[`missions/add-your-second-pc.json`](../../missions/add-your-second-pc.json)
in `kc-mission-v1` format. Because the Console v0.3.34 cannot yet load
custom missions from an in-cluster ConfigMap or CRD, import it via
**Missions > Local Files > Import**. Keep the mission and this skill in sync
— the mission is the GUI counterpart; this file is the agent-executable truth.
