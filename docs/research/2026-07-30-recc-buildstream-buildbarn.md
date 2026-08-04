# RECC with BuildStream and BuildBarn

## Executive summary

RECC is a compiler launcher for the Remote Execution API (REAPI). BuildStream
uses REAPI at element granularity; RECC adds a second, finer-grained layer
inside a BuildStream element by caching and distributing individual C/C++
compile (and optionally link) actions.

The GNOME presentation explicitly describes this model: BuildStream caches
whole builds, while RECC caches individual files and can run as a nested
remote-execution client inside a BuildStream sandbox. The accompanying
Mastodon post says the integration is already used by `gnome-build-meta` and
points to BuildStream issue #1751 and GNOME MR !4704.

The lab now has an isolated operator-only RECC cache pilot. Production
BuildStream, QA, and warmup lanes remain fail-closed: the pinned BuildBarn
runner does not implement the `remoteApisSocketPath`/LocalCAS handoff required
for nested RECC. The pilot pins a freedesktop-sdk compiler/provider junction,
uses a privileged `bst2` script container for bubblewrap, and records
BuildStream/BuildBarn evidence. See
`docs/research/2026-07-31-recc-run-results.md` for measured runs and their
explicit RECC-metric limitation.

## What RECC does

For a compiler command, RECC:

1. hashes the command, inputs, environment, and declared outputs;
2. checks the REAPI action cache;
3. downloads a cached object file when available;
4. otherwise executes locally or submits the compile action to a remote worker;
5. stores the result in the remote cache.

This complements rather than replaces BuildStream:

- **BuildStream:** distributes complete element builds and caches element
  artifacts.
- **RECC:** distributes individual C/C++ compiler invocations inside one
  element.
- **Linking:** remains local unless explicitly enabled with `RECC_LINK`; large
  link steps may therefore remain the critical path.
- **Rust and other languages:** receive no benefit unless an equivalent
  compiler wrapper exists.

Sources:

- GNOME `State of GNOME OS` slide 24:
  <https://events.gnome.org/event/306/contributions/1637/attachments/897/1597/presentation.pdf>
- RECC documentation:
  <https://buildgrid.gitlab.io/recc/>
- BuildStream issue #1751:
  <https://github.com/apache/buildstream/issues/1751>

## What GNOME actually shipped

GNOME MR !4704 adds a complete BuildStream integration:

- `include/recc.yml` adds `/usr/recc/bin` to `PATH`, points RECC at the local
  `unix:/tmp/casd.sock`, enables nested remote execution, and defines explicit
  modes:
  `passthrough`, `cache-only`, `remote-execution`, and
  `upload-local-build`.
- `elements/buildsystems/recc-wrapper.bst` installs the wrapper and links
  compiler names such as `gcc`, `g++`, `clang`, and `clang++` to it.
- `include/gcc-for-recc.yml` and `include/clang-for-recc.yml` use
  BuildStream's `digest-environment` to export the compiler dependency tree
  digest as `RECC_REMOTE_PLATFORM_chrootRootDigest`.
- `project.conf` exposes the `recc` option and defaults it to
  `remote-execution`.
- A later GNOME optimization commit enables `RECC_USE_LOCALCAS` and
  `RECC_USE_JOBSERVER` behind a `recc_optimisations` option.

The wrapper refuses to run unless
`RECC_REMOTE_PLATFORM_chrootRootDigest` is present. This prevents remote
compilation from using a worker with the wrong compiler/sysroot.

Sources:

- MR metadata and benchmark:
  <https://gitlab.gnome.org/api/v4/projects/GNOME%2Fgnome-build-meta/merge_requests/4704>
- MR file changes:
  <https://gitlab.gnome.org/api/v4/projects/GNOME%2Fgnome-build-meta/merge_requests/4704/changes>
- Current GNOME `include/recc.yml`:
  <https://gitlab.gnome.org/GNOME/gnome-build-meta/-/blob/master/include/recc.yml>
- Current GNOME `recc-wrapper.bst`:
  <https://gitlab.gnome.org/GNOME/gnome-build-meta/-/blob/master/elements/buildsystems/recc-wrapper.bst>

GNOME's published comparison is important for rollout planning:

| Mode | Build time |
| --- | ---: |
| No RECC | 1h31m |
| RECC, cold cache | 2h50m |
| RECC, warm cache | 50m |

RECC can therefore make a warm incremental build faster while making a cold
build substantially slower. GNOME specifically calls out RECC overhead,
`RECC_USE_LOCALCAS`, unconverted freedesktop-sdk elements, and Rust as current
limitations.

## Current lab state

The active `bst2` contract contains:

- `bst --version`: 2.7.0
- `recc --version`: 1.3.53
- `buildbox-casd`: present
- BuildBarn frontend: `grpc://frontend.buildbarn.svc.cluster.local:8980`

The shared ConfigMap contains:

```yaml
RECC_SERVER: "frontend.buildbarn.svc.cluster.local:8980"
RECC_CAS_SERVER: "frontend.buildbarn.svc.cluster.local:8980"
RECC_ACTION_CACHE_SERVER: "frontend.buildbarn.svc.cluster.local:8980"
RECC_ACTION_UNCACHEABLE: "0"
RECC_PROJECT_ROOT: "/workspace"
RECC_VERBOSE: "1"
```

The operator pilot adds the checkout-local wrapper, GCC
`digest-environment`, pinned GCC/RECC provider, and a deterministic two-compile
fixture. The production lanes mount the same preparation contract but refuse
before build work until a capable runner is proven. The pilot's first
successful runs measured outer BuildStream and BuildBarn timings/CAS deltas;
RECC action-level fields remain unavailable because the sandbox metrics-file
handoff has not been proven.

## BuildBarn blocker

BuildStream 2.6 introduced the exact two features needed for nested RECC:

- `remote-apis-socket`: expose a worker-local REAPI socket inside the sandbox.
- `digest-environment`: export a dependency tree digest to the sandbox.

BuildStream's BuildBox sandbox sends `remoteApisSocketPath` as an action
platform property. BuildBox's own runner supports that property when LocalCAS
is enabled and passes the socket to the staged sandbox.

The lab's current BuildBarn `bb_runner` configuration only enables
`chrootIntoInputRoot`; its worker platform advertises only `ISA=x86-64` and
`OSFamily=linux`. The current upstream `bb-remote-execution` source contains no
`remoteApisSocketPath` implementation. Therefore simply adding the GNOME
project config will not work reliably: the BuildBarn runner must first gain a
worker-local LocalCAS socket and honor the nested socket platform property.

Relevant sources:

- BuildStream 2.6 release notes:
  <https://raw.githubusercontent.com/apache/buildstream/master/NEWS>
- BuildStream sandbox implementation in the active image:
  `/usr/lib/python3.13/site-packages/buildstream/sandbox/_sandboxbuildboxrun.py`
- BuildBox nested socket implementation:
  <https://gitlab.com/BuildGrid/buildbox/-/blob/master/common/buildboxcommon_runner.cpp>
- Lab BuildBarn worker configuration:
  `manifests/buildbarn-config.yaml`
  and `manifests/buildbarn-worker.yaml`

## Recommended adoption path

### Phase 1: prove the cache path without remote compile — completed

The lab built the isolated `recc-baseline.bst` fixture with
`recc=cache-only`. This validated:

- compiler wrapper installation;
- stable output digest;
- BuildStream and BuildBarn evidence capture.

The run did not produce trustworthy RECC action-cache hit/miss fields, so the
results do not claim RECC cache effectiveness.

### Phase 2: add nested REAPI support to the worker

Choose one of these implementation paths:

1. **Preferred:** run a BuildBox LocalCAS-capable runner for BuildStream actions,
   with a worker-local `buildbox-casd` socket backed by the lab BuildBarn
   frontend.
2. **Alternative:** extend or replace `bb_runner` so it recognizes
   `remoteApisSocketPath`, stages the socket, and exposes it at the requested
   path.
3. **Fallback for experiments only:** run RECC against the BuildBarn frontend
   over a network path from inside the sandbox. This weakens the sandbox
   boundary and is not the desired production design.

The worker must advertise and honor the nested socket capability before the
BuildStream project enables `remote-apis-socket`.

### Phase 3: consume upstream GNOME integration

Once the worker path works, use the upstream GNOME files rather than copying
them into the lab:

- update the GNOME junction/ref used by the Dakota/Cosmic projects;
- enable `recc=remote-execution`;
- enable `recc_optimisations` only after baseline measurements;
- make sure the BuildStream image includes `recc`, `buildbox-casd`, and the
  wrapper runtime dependency.

The lab workflow should append only the outer project remote-execution config;
the nested RECC policy belongs in the upstream BuildStream project.

### Phase 4: benchmark before broad rollout — partially completed

Measure separately:

1. BuildStream only, cold cache.
2. BuildStream only, warm cache.
3. RECC cache-only, cold and warm.
4. RECC nested remote execution, cold and warm.
5. RECC with and without `RECC_USE_LOCALCAS`.

The control and cache-only runs collected total wall time, deterministic output
digest, and BuildBarn CAS deltas. RECC action-level metrics remained
`unavailable`, so no rollout decision can be based on RECC hit/miss or
compiler-time improvement yet. Nested remote execution was not measured.

## Bottom line

RECC is the right mechanism for the specific problem of large BuildStream
elements whose internal C/C++ compilation is not parallel enough at element
granularity. It is not a fix for the current one-slot workflow semaphore or
upstream source-fetch latency. The lab already has the client binary, but the
nested worker socket path is missing; that infrastructure seam must be solved
before enabling the upstream GNOME integration.
