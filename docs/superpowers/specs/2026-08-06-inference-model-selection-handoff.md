# Model selection handoff — lab inference

**Date:** 2026-08-06
**Status:** handoff, no decision made
**Purpose:** hand the next session a confirmed hardware envelope and the
evidence needed to choose a model and a runtime. Nothing here is implemented.

---

## 1. Confirmed hardware envelope

Both nodes are identical and both are now usable for inference. This was not
true before 2026-08-06 — ghost's GPU existed but was invisible to the
scheduler, and both nodes were capped at 31 GiB of GPU-addressable memory.

| | ghost (control-plane) | exo-0 (worker) |
|---|---|---|
| SoC | Ryzen AI Max+ 395 "Strix Halo" | same |
| GPU | Radeon 8060S, gfx1151, RDNA 3.5 | same |
| `amd.com/gpu` | 1 | 1 |
| **GPU-addressable (GTT)** | **48 GiB** | **48 GiB** |
| System RAM | 62.6 GiB | 62.1 GiB |
| CPU threads | 32 | 32 |

Interconnect: point-to-point Thunderbolt/USB4, `10.99.0.0/30`.
Measured ~1.0–2.0 GB/s at **70–100 µs** TCP latency.

**Budget realistically, not to the ceiling.** The 48 GiB is a ceiling shared
with everything else on the node. ghost carries the k3s control plane,
KubeVirt, and KubeStellar; exo-0 carries buildbarn workers. Assume roughly
**40 GiB usable per node** for a resident model plus KV cache, and expect
`bst-build` to contend for system RAM.

## 2. The single most important finding

**On this hardware, MoE beats dense by an order of magnitude.**

| Model | Memory | Throughput |
|---|---|---|
| Qwen3-32B (dense) | ~34 GB | **6.4 tok/s** |
| Qwen3-30B-A3B (MoE) | ~17 GB | **78 tok/s** |

Nearly the same parameter count. Half the memory. **12× the speed.**

Strix Halo is bandwidth-bound, not compute-bound. A dense model must stream
every weight per token; an MoE model streams only the active expert. The 6.4
tok/s figure was identical on HIP and Vulkan backends, which confirms the
bottleneck is memory bandwidth and not the driver stack.

**Do not evaluate dense models above ~14B on this cluster.** Start the search
at MoE architectures.

## 3. Multi-node: pipeline parallel or nothing

**Tensor parallelism across the Thunderbolt link is ruled out.** A 64-layer
model does 2 all-reduces per layer = 128 collectives per token. At 70–100 µs
each, that is **9–13 ms of pure network stall per token** — worse than the
model's own compute. Break-even needs ≈300 µs per collective; we are 100×
short. vLLM's own documentation describes the `NET/Socket` fallback path as
"not efficient". Bandwidth is *not* the problem here; latency is.

**Pipeline parallelism is ~128× cheaper** — one hidden-state hop per token,
about 10 KiB. If a model must span both nodes, use
`--tensor-parallel-size 1 --pipeline-parallel-size 2`.

Caveat: PP gives **zero single-request latency benefit**. It buys capacity and
multi-request throughput. For a single interactive user, a model that fits on
one node will always feel faster.

**Recommendation: prefer a model that fits in ~40 GiB on one node.** Treat
two-node serving as a capability to prove later, not a design requirement.

## 4. Runtime choice

| | llama.cpp | vLLM (ROCm) |
|---|---|---|
| gfx1151 support | mature | official, but needs **ROCm ≥ 7.0.2** |
| GGUF | native, best-in-class | out-of-tree plugin, "highly experimental" |
| Continuous batching | limited | yes — the reason to pick it |
| Multi-node | RPC (layer-split/PP) | PP |
| Cold start | fast | +90 s on gfx1151 (missing tuned MoE configs) |
| Known gfx1151 breakage | few | FP8 MoE broken; AITER crashes on RDNA |

**If the lab is a single-user assistant → llama.cpp.** Simpler, best GGUF
support, no ROCm version gate, fewer gfx1151 landmines.

**If it must serve concurrent agents/CI → vLLM**, accepting the ROCm floor and
the open upstream issues.

The existing `manifests/llm-d.yaml` already assumes vLLM
(`vllm/vllm-openai-rocm:latest`) at `replicas: 0`. It is a starting point, not
a decision — and it has an `emptyDir` HF cache, which means a full model
re-download on every restart. **That must be replaced with persistent storage
before anything is put into service.**

Note: `rocm/vllm` and `rocm/vllm-dev` were deprecated 2026-01-20;
`vllm/vllm-openai-rocm` is the supported image, though the deprecated ones
carry useful per-arch `gfx1151` tags.

**Unsloth publishes no ROCm image and no serving container** — `unsloth/unsloth`
is CUDA-12.8, training/Jupyter only. Use their GGUF *weights* with llama.cpp;
do not try to run their image.

## 5. Quantization on gfx1151

| Format | vLLM ROCm | Notes |
|---|---|---|
| FP8 W8A8 | ✅ | but **FP8 MoE is broken on gfx1151** (upstream PR #46186 open) |
| GGUF | ⚠️ | moved out-of-tree to `vllm-gguf-plugin`, experimental |
| AWQ / GPTQ | docs say ❌ | docs reflect CDNA; `rdna_hybrid_w4a16.py` has explicit gfx1151 tuning and AWQ works empirically |

Because FP8 MoE is broken and MoE is the recommended architecture, the likely
landing spot is **GGUF on llama.cpp** or **AWQ on vLLM**.

## 6. KV cache sizing

For a 64-layer / 8-KV-head / 128-head-dim model (Qwen3-32B shape):

| Precision | Per 1k tokens |
|---|---|
| fp16 | 256 MiB |
| fp8 | 128 MiB |

10 GiB at fp16 = 40,960 tokens, exactly that model's native context. Budget KV
cache explicitly; it is not a rounding error at long context.

## 7. Open questions for the next session

1. **Does fp8 KV cache work on gfx1151?** FP8 MoE is known broken; KV is
   untested. Halves cache memory if it works. **Test empirically first** — it
   changes the context budget.
2. **Single-user or multi-tenant?** This is the actual decision that picks the
   runtime. Everything else follows.
3. **Is 48 GiB the right GTT ceiling?** Chosen to leave ~14 GiB for k3s and
   builds. Tunable via `GTT_SIZE_MIB` / `TTM_PAGES_LIMIT` in
   `manifests/amdgpu-kargs.yaml`. Requires a reboot.
4. **Model residency vs. build preemption.** `bst-build` (1500000) already
   outranks `llm-d-preempt` (1000000), so builds win — but nothing implements
   *graceful* drain. Today a build would hard-evict a loaded model and force a
   full reload. This is the main unsolved design problem.
5. **Where do weights live?** `emptyDir` today. Needs a host path or PVC, ideally
   on ghost's `/var/mnt/ghost-data`.

## 8. Explicitly do not

- Do not raise the BIOS UMA carve-out. Measured: it steals system RAM and
  *shrinks* GTT. See `docs/skills/cluster-tooling/SKILL.md`.
- Do not set `ttm.page_pool_size` — it pre-allocates and permanently removes
  memory from the OS.
- Do not set `amd_iommu=off` on this cluster despite the ~5–12% inference gain;
  it risks KubeVirt VFIO passthrough.
- Do not plan on tensor parallelism over Thunderbolt (§3).

---

## Related

- `manifests/amdgpu-kargs.yaml` — GTT ceiling, tuning knobs, measured BIOS table
- `manifests/amdgpu-device-plugin.yaml` — NFD-driven GPU advertisement
- `manifests/llm-d.yaml` — existing paused vLLM Deployment
- `docs/skills/cluster-tooling/SKILL.md` § "AMD GPU topology"
- `docs/reference/agent-cheatsheet.md` § "AMD ROCm GPU readiness"
