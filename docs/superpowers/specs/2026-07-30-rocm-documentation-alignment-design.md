# ROCm Documentation Alignment

## Goal

Align all relevant repository documentation with the current lab ROCm model
without changing manifests, tests, or live cluster state.

## Canonical model

> **Superseded in part (2026-08-06).** Three claims below no longer hold. See
> `docs/skills/cluster-tooling/SKILL.md` § "AMD GPU topology" for the current
> model. Retained for historical context.
>
> - ~~`exo-0` is the AMD GPU node.~~ **Both** nodes are AMD GPU nodes. ghost's
>   iGPU was present all along but invisible to the scheduler.
> - ~~The DaemonSet targets `lab.projectbluefin.io/amd-gpu=true`.~~ It now
>   selects on the NFD label `feature.node.kubernetes.io/pci-0380_1002.present`.
>   The hand-applied label is retired.
> - ~~`llm-d-modelserver` is pinned to `exo-0`.~~ Node pinning is no longer
>   required for GPU reasons; both nodes qualify.

- `exo-0` is the AMD GPU node.
- The ArgoCD-managed `amdgpu-device-plugin` DaemonSet targets nodes labeled
  `lab.projectbluefin.io/amd-gpu=true`.
- The node advertises the extended resource `amd.com/gpu`.
- `llm-d-modelserver` is the desired local inference workload. It requests one
  `amd.com/gpu`, is pinned to `exo-0` by the manifest, and exposes NodePort
  `30800`.
- The vLLM pod uses privileged mode and hostPath mounts for ROCm device access.
  Documentation must call out the corresponding PodSecurity requirement rather
  than describing the workload as baseline-compliant.
- KubeVirt GPU passthrough, multi-GPU/MIG, Intel QSV, and NVIDIA media
  transcoding remain separate or deferred paths.

## Scope

Update every relevant user-facing Markdown document and README, including:

- the agent cheatsheet and bootstrap guide;
- homelab contracts and GPU lane wording;
- cluster-tooling and GitOps/image-policy skill documentation;
- README architecture and namespace descriptions;
- operational failure-mode references.

The refresh will remove stale claims that the cluster has no AMD GPU, correct
the node and GitOps ownership details, replace manual manifest-apply guidance
with GitOps-safe inspection/reconciliation guidance, and scope deferred
language to paths that are actually deferred.

## Non-goals

- Do not change `manifests/`, tests, workflow behavior, or PodSecurity labels.
- Do not claim that vLLM inference is healthy until the deployment reaches
  `Available` and its endpoint has been verified.
- Do not expand the NVIDIA transcoding test lane into an AMD implementation.

## Validation

After the edits:

1. Search documentation for stale “no AMD GPU”, “AMD ROCm deferred”, baseline
   compliance, unpinned-node, and manual-apply claims.
2. Confirm every remaining deferred statement names the deferred path.
3. Render/read the changed Markdown and verify links and command ownership.
4. Run the repository's existing documentation validation selector if it covers
   the changed files.
