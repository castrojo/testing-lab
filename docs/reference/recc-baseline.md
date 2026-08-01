# RECC baseline and pilot contract

This is a lab-only contract for measuring C/C++ compilation inside a
BuildStream element. It does not change Dakota, Cosmic, Bluefin Server, or any
other production BuildStream lane.

## Current state and evidence boundary

The prototype pins the freedesktop-sdk 25.08.14 junction used by the factory
and stages the compiler from `components/gcc.bst`. RECC comes from the same
junction's `components/buildbox.bst`. The overlay validates the provider path
at run time, so a missing or changed junction fails closed before build work.

## Fixture

`bst-prototype/elements/recc-baseline.bst` compiles two deterministic C++
translation units and links `/usr/bin/recc-baseline`. The operator-only
`recc-baseline-pipeline` targets this element, not `hello.bst`.

The project option `recc` selects the launcher and mode:

| Option | Compiler action | Expected purpose |
| --- | --- | --- |
| `buildstream-only` | `/usr/bin/g++` | Outer BuildStream baseline; no RECC |
| `passthrough` | `recc /usr/bin/g++` + `RECC_PASSTHROUGH=1` | Wrapper overhead/control |
| `cache-only` | `recc /usr/bin/g++` + `RECC_CACHE_ONLY=1` | Remote hit, or local compile on a miss |
| `upload-local-build` | cache-only + `RECC_CACHE_UPLOAD_LOCAL_BUILD=1` | Compile misses locally and upload results |
| `remote-execution` | blocked | Nested runner capability is not proven |

The fixture deliberately uses two compile actions and one link action. Linking
stays local unless a later pilot explicitly enables `RECC_LINK`.

The pilot sandbox stages `/bin/sh`, `/usr/bin/g++`, `/usr/bin/recc`, and the
matching C++ sysroot through BuildStream dependencies. It never mounts a host
`/usr`; the `bst2` image's host `recc` is not visible inside the sandbox.

## Workflow run contract

Use the isolated operator entrypoint:

```bash
just run-recc-baseline
just run-recc-baseline mode=cache-only cache-policy=both \
  recc-provider=freedesktop-sdk.bst:components/buildbox.bst
```

The workflow defaults to the pinned provider and accepts `mode`, `run-id`,
`cache-policy`, and `recc-provider`.
`cache-policy` is `cold`, `warm`, or `both`. Each selected phase starts with a
clean workflow-scoped BuildStream cache. The remote RECC endpoint and its
action cache are not cleared, so a `both` run can preserve remote warm-hit
evidence while avoiding a local BuildStream artifact hit.

`buildstream-only` runs without the overlay. Every supported RECC mode
(`passthrough`, `cache-only`, and `upload-local-build`) requires a non-empty
provider and invokes the overlay with `--pilot-cache-only`.
`remote-execution` remains blocked until an upstream runner candidate proves
that it consumes BuildStream's `remoteApisSocketPath` and stages the pod-local
LocalCAS socket.

Keep the source revision, BuildStream image, compiler/sysroot, BuildStream
config, RECC config, node, and worker set fixed for a comparison. Do not run
the pilot through a production lane.

## Evidence

For each selected phase, record:

- wall-clock duration and BuildStream fetch/build/push durations;
- BuildStream element key, final state, and whether the result came from its
  artifact cache;
- RECC compile action count, action-cache hits/misses, local fallbacks, and
  compile/link duration;
- BuildBarn worker action count, execution time, and scheduler queue time from
  explicit Prometheus counter/histogram deltas;
- CAS/action-cache blob requests and bytes uploaded/downloaded from explicit
  BuildBarn blobstore metrics;
- output digest and `recc-baseline` stdout.

The workflow captures dedicated RECC log sections, BuildStream show metadata,
fixture stdout, and before/after BuildBarn Prometheus federation snapshots.
Collector failure is surfaced as a failed workflow; missing metric fields
remain explicit `unavailable_fields` entries and are never represented as zero
or a successful warm hit. The workflow emits compact `metadata` and `evidence`
output parameters.

RECC StatsD counters are interpreted conservatively: cache hit/miss counters
are the action count, local execution counters report fallbacks, and execution
timings are used only when their StatsD unit is milliseconds. Counter samples
are never treated as compiler durations.

RECC's metrics file is written under BuildStream's ephemeral `%{build-root}`,
then printed into the element build log and removed before artifact creation.
The workflow sets BuildStream's supported `logdir` to `/work/buildstream-logs`
and extracts RECC-marked lines from those per-element logs into the workflow
evidence. This keeps the StatsD/log handoff outside the deterministic artifact;
cache-only and upload-local-build phases fail closed when that handoff produces
no RECC evidence.

The acceptance comparison is mode-specific: `cache-only` and
`upload-local-build` must prove stable action keys and a warm action-cache hit.
A successful outer BuildStream build alone is not RECC evidence.

The first lab measurements are recorded in
`docs/research/2026-07-31-recc-run-results.md`. They include real
BuildStream/BuildBarn timings and CAS deltas; RECC action-level fields remain
unavailable until the sandbox metrics-file handoff is proven.

## Evidence boundaries

- `buildstream-only` establishes the deterministic fixture and outer artifact
  baseline.
- `passthrough` isolates wrapper/launcher overhead without cache or remote
  execution.
- `cache-only` establishes lookup behavior and miss handling.
- `upload-local-build` establishes local-miss result upload behavior.
- `remote-execution` is blocked until nested socket support is verified.
- The first phase is **cold-local**: the workflow-local BuildStream cache is
  cleared while the shared remote RECC endpoint is preserved. A second phase
  is **warm-remote** evidence, not a true remote-cold result.
- The generic `bst-qa` workflow is a QA lane, not this prototype's measurement
  harness.
- The dedicated operator workflow is intentionally separate from `bst-qa` and
  all production lanes. It must not be referenced by those lanes or gain a
  runtime switch into them.

Do not enable `recc_optimisations`, `RECC_LINK`, or production project includes
until these measurements are captured and reviewed.
