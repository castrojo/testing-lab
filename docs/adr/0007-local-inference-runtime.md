# ADR 0007 — Local inference runtime on Strix Halo

Status: Accepted
Date: 2026-08-06

## Context

The lab has a repo-managed local inference deployment (`manifests/llm-d.yaml`)
pinned to `exo-0`, a Framework Desktop with an AMD Ryzen AI Max+ 395 "Strix Halo"
APU (`gfx1151`, Radeon 8060S iGPU). Until now it was a placeholder: vLLM on ROCm
serving `unsloth/Llama-3.2-3B-Instruct` at 4096 context, an `emptyDir` model
cache, and `replicas: 0` — so it served nothing, while the agent cheatsheet
claimed it was enabled with one replica.

The requirement is a **single-user, quality-first assistant left running for
hours**. Throughput is secondary to answer quality.

### The hardware constraint that drives everything

`exo-0` has **64 GB** of unified LPDDR5X, not the 128 GB that every published
Strix Halo tuning guide assumes. Measured allocatable capacity is 65090060Ki
(~62.1 GiB), and the node reports allocatable == capacity, i.e. no kubelet
system-reserved headroom.

`manifests/amdgpu-kargs.yaml` raises the GPU-addressable ceiling to **48 GiB**
(`amdgpu.gttsize=49152` plus a matching `ttm.pages_limit`). That ceiling is
~77% of system RAM and cannot meaningfully rise: the BIOS UMA carve-out must stay
at its 512 MiB minimum, because raising it is a hard steal from system RAM
(measured: a 48 GiB carve-out dropped `MemTotal` to 15.4 GiB *and* collapsed GTT
to 7.9 GiB).

Critically, **GTT is system RAM**, shared with k3s, BuildBarn workers, KubeVirt
VMs and the KubeStellar control plane. The 48 GiB ceiling is a maximum mapping
limit, not a spending budget.

### Measured behaviour of this hardware

Benchmarking on Strix Halo, corroborated by published measurements from
`apellegr/Strix-Halo-Models` across 38 models:

| Model | Kind | Quant | Size | Generation |
|---|---|---|---|---|
| Qwen3 Coder 30B-A3B | MoE, 3B active | Q4_K_M | 17 GB | **69.6 tok/s** |
| Qwen 2.5 Coder 32B | dense | Q4_K_M | 19 GB | **11.0 tok/s** |
| Llama 3.3 70B | dense | Q6_K | 54 GB | 3.8 tok/s |

The machine is **memory-bandwidth bound**: host-to-device bandwidth is
**~85 GB/s**, against 205–236 GB/s GPU-internal. Decode speed is therefore set by
the parameters *activated per token*, not by total parameter count. A 30B MoE
with 3B active runs roughly 6× faster than a dense 32B of comparable size, and
the published summary states MoE achieves 3–14× higher generation throughput
than similarly-sized dense models.

## Decisions

### 1. llama.cpp, not vLLM

vLLM's advantage is continuous batching across concurrent tenants. For a single
user that buys nothing and costs KV-cache memory that could be spent on context
or quality. llama.cpp gives the full GGUF quantization ladder, KV-cache
quantization, and is the far better-trodden path on `gfx1151`.

### 2. Vulkan, not ROCm/HIP

Two independent sources conclude that on `gfx1151` the reliable, well-supported
llama.cpp GPU path is the **Vulkan** backend (Mesa RADV), and that ROCm is not
required for inference. The community ROCm toolboxes additionally have to carry a
patch for llama.cpp issue #25992 (ROCm host-buffer selection on integrated GPUs
causing inference failures).

This also resolves a supply-chain constraint: `ghcr.io/ggml-org/llama.cpp`
publishes **no ROCm tags at all** (only `vulkan`, `cuda`, `musa`, `intel`), while
the patched ROCm toolboxes live on Docker Hub, which is not in the lab registry
allowlist. Vulkan keeps us on an allowlisted registry with an upstream-maintained
image.

The image is **pinned by digest**, not `latest`, so rollback is reproducible.

### 3. Qwen3-30B-A3B-Instruct-2507 at Q6_K, not Q8_0

Published quantization guidance:

| Quant | Quality loss | Recommended for |
|---|---|---|
| Q8_0 | Minimal | Small models, max quality |
| **Q6_K** | **Very low** | **Models up to 32B** |
| Q5_K_M | Low | up to 70B |
| Q4_K_M | Acceptable | 70B |
| Q3_K_M | **Noticeable** | 100B+ only |

Q6_K (25.1 GB) sits in its recommended band for a 30B model at "very low" loss.
Q8_0 (32.5 GB) costs 7.4 GB more to move from "very low" to "minimal" — a
negligible quality gain that spends the KV-cache headroom a long-context
assistant actually needs on a 64 GB node.

### 4. Rejected: a larger MoE at lower precision

Qwen3-Next-80B-A3B has the **same ~3B active parameters** as Qwen3-30B-A3B, so it
would decode at the same speed, and at Q3 it fits the same envelope
(UD-IQ3_XXS 33.1 GB, UD-Q3_K_XL 35.6 GB) with 2.7× the total parameters. The
bandwidth argument above genuinely points this way.

It is rejected for now because the same quantization table rates Q3_K_M as
"noticeable" loss and reserves it for 100B+ models, and because Qwen3-Next's
Gated-DeltaNet linear attention is only newly supported in llama.cpp — the CUDA
optimisation PR is still open — making it a poor bet on the Vulkan path.

**Re-evaluation trigger:** revisit if a ~30–40 GB MoE ships a native MXFP4 GGUF
(rated "low" loss, and the format behind GPT-OSS 120B's 51 tok/s), or once
Qwen3-Next's Vulkan path is proven. GPT-OSS 120B itself needs 59 GB and does not
fit this node.

### 5. Tensor parallelism across the USB4 link is rejected

Splitting a model across `ghost` and `exo-0` over the point-to-point USB4 link
requires ~128 collective operations per token. At 70–100 µs link latency that is
**9–13 ms of stall per token**, worse than the single-node decode it would try to
beat.

This is confirmed by measurement, not just arithmetic: `Foxlight-Foundation/Skulk`
measured llama.cpp RPC pooling across a Strix Halo pair as "a capacity feature,
not a speedup" — decode runs **~15% slower** pooled than on a single node that
fits the model, and a Thunderbolt link "helps load time, not decode".

Multi-node pooling therefore remains justified only for models that do not fit
one node. Since we deliberately choose a model that fits, it stays off.

### 6. GPU lockup timeout is raised to 20s

The amdgpu default (~10s) is tuned for interactive desktop use. Under sustained
inference on `gfx1151` it trips a **spurious GPU reset** ("device lost", ring
timeout) that kills the inference backend mid-run even though the GPU is merely
busy. Since the stated workload is hours-long sessions, this is the most likely
practical failure mode. `manifests/amdgpu-kargs.yaml` now sets
`amdgpu.lockup_timeout=20000`, which avoids the false positive without masking a
genuine hang.

**Not adopted:** `amd_iommu=off`, despite being measured 5–12% faster. It
disables IOMMU device isolation host-wide, and these nodes run KubeVirt VMs that
depend on it. Inference here is throughput-tolerant, so the security property
wins. Also not adopted: `ttm.page_pool_size`, which some guides recommend but
which pre-allocates and permanently removes memory from the OS — unacceptable on
a shared node.

### 7. Unified memory is a node-level safety problem, not a pod-level one

`amd.com/gpu: 1` grants device access but reserves **no** GTT capacity, and
amdgpu GTT pins system RAM that the kernel OOM-killer cannot reclaim. On a UMA
node this means memory pressure can *deadlock the node* rather than evict a pod.

Consequences encoded in the deployment:

- The memory **request** is sized to reserve scheduler space for the model, so
  nothing else fills the node underneath it.
- The memory **limit** is deliberately generous rather than snug, because a tight
  limit risks an OOM kill during model load, and GPU-backed pages may not be
  charged to the pod cgroup at all.
- Node-level protection (kubelet system-reserved / eviction thresholds on
  `exo-0`) is required and tracked separately; it is not something a pod spec can
  express.

### 8. Rollout is `Recreate` and phased

The node exposes exactly one `amd.com/gpu` and the cache PVC is RWO. A default
rolling update would create the replacement pod before deleting the old one, the
replacement could never obtain the GPU, and `maxUnavailable: 0` would keep the
old pod forever — a permanent deadlock. `strategy.type: Recreate` is mandatory,
and the resulting downtime during updates is accepted.

The model cache moves from `emptyDir` to a 100Gi `local-path` PVC, so a restart
no longer re-downloads ~25 GB. `local-path` is RWO and `WaitForFirstConsumer`;
placement is deterministic because the Deployment pins to `exo-0`.

The Deployment ships at `replicas: 0` and is enabled in a **separate** GitOps
change once the kargs reboot, the PVC bind and node headroom are confirmed.

## Consequences

- Local inference is a genuinely useful assistant rather than a 3B placeholder,
  at roughly 8× the parameter count of what was configured before.
- The pod drops from `privileged: true` with four hostPaths to unprivileged with
  a single `/dev/dri` mount, because the Vulkan backend needs only the render
  node (`/dev/dri/renderD128` is mode 0666 on these nodes).
- `llm-d` still trips two `scripts/check_gitops_policy.py` rules — the `/dev/dri`
  hostPath and the `kubernetes.io/hostname` nodeSelector. Both are inherent to
  pinning a GPU workload to the one node with the GPU, and are accepted
  exceptions rather than defects.
- The lab gains a real OpenAI-compatible endpoint. It has no authentication and
  permissive CORS, so it must stay on the trusted LAN.
- Updating the model server incurs downtime by design.

## Verification

```bash
# Kernel args live (after the reboot that the kargs DaemonSet stages)
kubectl get node exo-0 -o jsonpath='{.metadata.annotations.lab\.projectbluefin\.io/amdgpu-kargs}{"\n"}'   # applied

# Storage and pod
kubectl -n llm-d get pvc llm-d-model-cache      # Bound
kubectl -n llm-d get pods -o wide               # Running on exo-0

# GPU actually in use — expect Vulkan0, and all layers offloaded
kubectl -n llm-d logs -l app.kubernetes.io/name=llm-d-modelserver --tail=200 \
  | grep -Ei "vulkan|offload"

# Serving, with measured decode speed
curl -s http://<ghost-ip>:30800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Explain Kubernetes in one sentence."}],"max_tokens":128}' \
  | jq '.timings.predicted_per_second'
```

A healthy result is a completion returned with all layers offloaded to Vulkan, a
sustained multi-hour session with **zero** amdgpu resets, and no node memory
pressure or evictions on `exo-0`.

## References

- [`kyuz0/amd-strix-halo-toolboxes`](https://github.com/kyuz0/amd-strix-halo-toolboxes) — Strix Halo llama.cpp images, tested `models.ini` presets, host tuning
- [`defilantech/LLMKube`](https://github.com/defilantech/LLMKube) — Kubernetes + Strix Halo onboarding, kernel args, UMA OOM failure mode
- [`Foxlight-Foundation/Skulk`](https://github.com/Foxlight-Foundation/Skulk) — Vulkan-over-ROCm rationale, measured multi-node pooling results
- [`apellegr/Strix-Halo-Models`](https://github.com/apellegr/Strix-Halo-Models) — 38-model benchmark set, quantization guide, GPU stability runbook
- ADR 0005 — storage and backup (`local-path` semantics)
