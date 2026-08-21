# 4K AI upscale on Vulkan/ncnn — 2026-08-21

Maximum-fidelity 1080p→4K upscale of a single 507 s music video, run on the lab's
AMD Strix Halo (gfx1151) nodes. The goal was to find the real throughput and the
real quality ceiling of a Vulkan-only inference stack, and to decide whether the
two-node USB4 architecture was worth building.

**Headline results**

| Question | Answer |
|---|---:|
| End-to-end throughput | **0.377 s/frame** (2.65 fps) sustained |
| Full 507 s video | **1 h 19 m** render, 2 h 01 m including master encode |
| Break-even needed to fit 48 h | 27.3 s/frame → **59x margin** |
| Best restoration branch | **none** — the bare model won |
| Two-node distribution | **abandoned** — pod network measured 235 KB/s |

## Stack

Vulkan/ncnn throughout. No ROCm, HIP, CUDA or PyTorch, per
[ADR-0007](../adr/0007-local-inference-runtime.md) and the C1 constraint.

| Component | Pin |
|---|---|
| VapourSynth | R79 |
| `vapoursynth-mlrt-ncnn` | 16.0 (ncnn `20260526`) |
| `vapoursynth-bestsource` | 21.0 |
| `vapoursynth-deblock` | 9.0 |
| `vapoursynth-mvtools` | 29 |
| vszip | commit `beb7a0ab` (Zig 0.16.0) |
| Model | `2xLiveActionV1_SPAN`, native x2 |
| Image | `images/video-upscale/Containerfile`, both stages from org `bluefin` |

**Model license: `2xLiveActionV1_SPAN` is CC-BY-NC-SA-4.0 — non-commercial.**
Acceptable for an internal lab benchmark; it may not be used for anything
commercial, and the output inherits the restriction.

The image contains **no package manager invocation at all**, per
[image-policy.md](../skills/gitops-argocd/image-policy.md). ffmpeg comes from the
org `bluefin` image; Zig comes from a checksum-verified upstream tarball.

### Currency audit

The audit removed more than it kept. Everything in the "obvious" 2026 answer to
this problem is abandonware:

| Rejected | Last activity | Cause |
|---|---|---|
| `realesrgan-ncnn-vulkan` | 2022-04-24 | stale |
| BasicVSR++ / MMagic | 2023-12-18 | stale, **and** MMCV deform-conv is CUDA-only |
| DPIR · SCUNet · SwinIR · Real-CUGAN · Waifu2x | 2020–2024 | stale |
| FlashVSR v1.1 | current | NVIDIA block-sparse attention |

Consequence: nearly all of the 812 MB vs-mlrt model zoo fails a currency check.
Two earlier revisions of this plan were built on abandoned software that looked
authoritative. **Run a currency audit before adopting a dependency, not after.**

## Source

`input.mp4`, measured — not assumed:

| Property | Value |
|---|---|
| Resolution | **1920x804** (2.388:1), no black bars |
| Codec | H.264 High L4.0, yuv420p 8-bit, progressive |
| Colour | BT.709, **limited** range |
| Rate | 25 fps CFR, **12,675 frames**, 507.05 s |
| Bitrate | **1.26 Mbps** (median frame 3.7 kB, p5 1.0 kB) |
| Scene cuts | 79 |
| Stream 2 | 1280x720 PNG cover art — dropped |

Two findings drove every downstream decision:

1. **Output is 3840x1608, not 3840x2160.** The model is native x2 and
   1920x804 x 2 lands exactly on 3840x1608. No resampling, no letterboxing.
2. **80% of pixels fall in 42 code values**, so 8-bit output would band.
   **10-bit is mandatory** and was verified on the master.

At 1.26 Mbps the source, not the GPU, is the binding constraint. The honest
ceiling is "cleaner and larger", not "restored".

## Throughput

50 frames per cell, `vspipe -> ffmpeg` FFV1 4K 10-bit, single gfx1151.

| Clip | Content | Branch 1 (model only) | Branch 2 (Deblock+MosquitoNR) |
|---|---|---:|---:|
| A | near-black, 0.30 Mbps | 0.461 s/f | 0.472 s/f |
| B | fine detail, grain | 0.474 s/f | 0.448 s/f |
| C | dissolve + heavy bokeh | 0.475 s/f | 0.463 s/f |
| D | high bitrate, heavy motion | 0.471 s/f | 0.445 s/f |

**Throughput is flat across content and across branches** — it is set by the
model, not by the footage or the restoration filters.

Isolating the encoder (`vspipe` to `/dev/null`) gives **0.402 s/frame**, so the
4K FFV1 encode costs only ~0.06 s/frame (~13%) and restoration is
free to within noise (0.402 vs 0.403). The pipeline is inference-bound.

## Quality

### Round-trip SSIM cannot pick the branch

Master downscaled back to 1920x804 (Lanczos) and compared to source:

| Clip | Branch 1 | Branch 2 |
|---|---:|---:|
| A | **0.996984** | 0.996954 |
| B | **0.995077** | 0.994273 |
| C | **0.995627** | 0.994435 |
| D | **0.995537** | 0.994573 |

Branch 1 wins everywhere — but this metric is **inverted for this purpose**.
Round-trip SSIM rewards fidelity to a source whose defining feature is
compression damage, so it penalises exactly the artefact removal that
restoration exists to perform. It is reported here as a drift/hallucination
detector, and it shows no drift. **It is not evidence that restoration is
harmful.** That required looking at pixels.

### Deblocking is a losing trade on this source

Sweep on the worst-case clips, inspected as 900x600 1:1 crops:

| Setting | Flat shadow (clip A) | Hair detail (clip D) |
|---|---|---|
| off | blocking visible | sharpest |
| `quant=24, strength=8` | unchanged | unchanged |
| `quant=40, strength=16` | slightly reduced | **visibly softened** |
| `quant=56, strength=24` | reduced | **visibly softened** |

The conservative setting is visually indistinguishable from off, and every
setting strong enough to touch the blocking also smears genuine hair texture.
There is no window where deblocking helps this source.

**Decision: render with the bare model (branch 1).** It was equal or better on
every axis measured — SSIM, visual detail, and complexity.

Branches 3 (ArtCNN chroma) and 4 (4xUltraSharpV2 adversarial control) were
**not run**: branch 3's upstream wiring was never verified, and branch 4's
weights were never vendored. They are unfinished, not evaluated.

A `vszip.Deband` variant was attempted and failed on an incorrect call
signature; it was dropped rather than guessed at.

## The two-node architecture was abandoned

The plan called for splitting the render across both GPUs with an HTTP chunk
store on the pod network, specifically so bulk traffic would ride USB4.
Measurement killed it.

| Path | Endpoints | Throughput |
|---|---|---:|
| Loopback control (same pod) | `127.0.0.1` | 8.76 GB/s |
| **Raw USB4, host netns** | `10.99.0.1` -> `10.99.0.2` | **1.097 GB/s** |
| Ethernet, host netns | `192.168.1.170` | 294 MB/s |
| **Cross-node pod -> pod** | `10.42.0.249` -> `10.42.1.54` | **235 KB/s** |

The USB4 link is healthy and is 3.7x faster than Ethernet. The **pod** path is
~4,600x slower than the link beneath it. The loopback control rules out the test
server; pod→host-netns was also slow, so the fault is on pod-netns egress.

Routing is not the cause — rule 5209 correctly steers pod-CIDR traffic onto
`thunderbolt0` even with a pod source address, and MTU is a uniform 1500:

```
5209:	from all to 10.42.1.0/24 lookup 40 proto static
10.42.1.54 from 10.42.0.249 via 10.99.0.2 dev thunderbolt0 table 40
```

Moving 65–85 GB of chunks at 235 KB/s would take ~4 days against ~1.6 h to
render everything on one node, so the benchmark ran **single-node on `ghost`**.
Filed as **issue #662**.

> **Trap:** `ip route get <peer-pod-ip>` only tells the truth in the host netns.
> Run inside a pod it always answers `via <cni0 gateway> dev eth0`, which looks
> like a failure and proves nothing either way.

## Full run

Branch 1, 12,675 frames, four chunks, single gfx1151 on `ghost`.

| Chunk | Frames | Wall |
|---|---:|---:|
| 0 | 3,169 | 1,195 s |
| 1 | 3,169 | 1,192 s |
| 2 | 3,169 | 1,195 s |
| 3 | 3,168 | 1,197 s |
| **Render total** | **12,675** | **4,779 s (1 h 19 m)** |

**0.377 s/frame (2.65 fps)** sustained — *faster* than the 50-frame calibration
figure of 0.46 s/frame, because per-invocation startup no longer dominates.
Chunk times agree within 0.4% across wildly different content, confirming the
pipeline is inference-bound rather than content-bound.

Every chunk passed its frame-count gate exactly. Total pod wall clock was
2 h 01 m; the extra ~41 min is the ProRes master encode plus four full
`ffprobe -count_frames` decodes over 27.7 GiB of FFV1.

Against the 48 h budget the plan was sized for, the run needed **2.8%** of it.
The deadline arithmetic gate that revision 4 treated as the project's main risk
turned out to have a 59x margin.

### Output

| | |
|---|---|
| Intermediate | 27.7 GiB FFV1 4K 10-bit (4 chunks) |
| Master | **23.1 GiB** ProRes 422 HQ, `/work/final/master.mov` |

Final gate, all assertions met:

```
index=0  codec_name=prores  width=3840  height=1608  pix_fmt=yuv422p10le
         color_space=bt709  color_range=tv  r_frame_rate=25/1
         nb_read_frames=12675
index=1  codec_name=aac  sample_rate=44100
```

3840x1608 native aspect with no letterboxing, 10-bit as required by the banding
finding, colour tags intact, frame count exact, and exactly two streams — the
1280x720 cover art was dropped by mapping `0:v:0` and `1:a:0` explicitly.


## What this does not measure

No 4K ground truth exists, so "is it better" cannot be scored directly. Round-trip
SSIM detects drift, not improvement. The visual conclusions above rest on paired
1:1 crops, which is the honest instrument for this question.

The comparison is against Lanczos, not against a current commercial upscaler.

## Reproducing

```bash
podman build -t video-upscale:v4 -f images/video-upscale/Containerfile images/video-upscale/
# stage input.mp4 into the upscale-scratch PVC, then per chunk:
SRC=/work/input.mp4 BRANCH=1 vspipe -c y4m --start "$START" --end "$END" \
    /usr/lib/upscale/upscale.vpy - \
  | ffmpeg -f yuv4mpegpipe -i - -c:v ffv1 -level 3 -g 1 \
      -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv \
      "/work/final/chunk-${IDX}.mkv"
```

`vspipe --end` is **inclusive**.

## Rights

The output is a derivative of a commercial copyrighted music video. Local
experimentation is one thing; publishing is a separate question for the
rightsholder. The pipeline is content-agnostic.
