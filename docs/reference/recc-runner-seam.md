# RECC runner seam

This is a lab-only preparation seam for the isolated RECC pilot. It does not
enable nested REAPI in a production BuildStream lane, and it does not change
the outer BuildStream RE contract.

The sidecar, socket, ConfigMap, and admission-gate descriptions below are
checked-in repository/configuration evidence. The Kubernetes/Argo API timed out
during the current live-verification attempt, so this document does not claim
live worker, runner, endpoint, or workflow health.

## What is wired

The checked-in BuildBarn worker manifest declares:

- a `buildbox-casd` sidecar using the existing, pinned `bst2` image contract,
  invoked as `buildbox-casd` through the image `PATH` (the install prefix of
  that digest is not pinned anywhere in this repo, so an absolute path would
  be an unverified guess — this matches the lab's historical casd manifest);
- a worker-local CAS, Action Cache, and Execution proxy to the BuildBarn
  frontend;
- an 8 GiB LRU cache under `/worker/recc-casd`, inside the existing node-local
  worker storage;
- a pod-local UNIX socket at `/run/buildbarn/recc/casd.sock`, shared only by
  the sidecar and runner through an `emptyDir` volume;
- sidecar readiness/liveness probes for that socket.

The outer BuildStream REAPI endpoint, USB4 admission gates, worker cache,
concurrency, and existing worker/runner resource limits and security context
are unchanged.

## Health boundaries

There are two independent health boundaries, and they must stay independent
until a runner actually consumes the nested socket:

1. **Outer remote execution** — the `worker` and `runner` containers. The
   workflow admission gates in `dakota-build-pipeline.yaml`,
   `cosmic-build-pipeline.yaml`, `bluefin-server-build-pipeline.yaml`, and
   `bst-cache-warm.yaml` select those two containers *by name* from
   `containerStatuses`, so adding or removing a preparation sidecar cannot
   reject a healthy worker. Never gate on the container-count-dependent
   `containerStatuses[*].ready` list.
2. **Nested RECC preparation** — the `recc-casd` sidecar and its socket. Its
   readiness/liveness probes cover `/run/buildbarn/recc/casd.sock` and nothing
   else. `runner.jsonnet` deliberately does **not** set
   `readinessCheckingPathnames` for that socket: nothing consumes it yet, so
   coupling outer BuildBarn admission to it would take both workers out of
   service for an unused capability. The socket is not exposed through a Service, host mount,
`hostPID`, or `hostIPC`.
The sidecar adds only a bounded 100m/128Mi request and 1 CPU/2Gi limit.
The sidecar image is the lab registry's
`bst2@sha256:ce272e0ae4b59251680b8a70645740640ced17689e21268ddc037614c755f734`
digest, verified to contain `buildbox-casd` 1.3.53 and `buildbox-run`.

## Shared RECC config contract

`manifests/buildstream-remote-cache-config.yaml` publishes the
`recc-environment.conf` key for every BuildStream pipeline to mount. It
contains the lab's shared RECC server, CAS, action-cache, project-root, and
cache-policy values. It intentionally contains no wrapper-prefix setting.
Toolchain-specific integrations must add
`RECC_REMOTE_PLATFORM_chrootRootDigest` from their compiler/sysroot dependency
tree; the lab cannot safely invent that digest.

The contract is only an environment map. A consumer still has to install the
RECC wrapper and merge the map into its BuildStream project. The current
`bb_runner` does not support nested socket pass-through, so consumers must not
enable `sandbox.remote-apis-socket` yet.

## Concrete blocker

The checked-in pinned `bb_runner` installer/image contract exposes only
BuildBarn's native chroot runner. Its `ApplicationConfiguration` has no LocalCAS
client or BuildBox runner command, and the checked-in source/configuration does
not consume the `remoteApisSocketPath` platform property. BuildBox's
`buildbox-run` can honor that property only when it stages through LocalCAS;
adding an arbitrary `remoteApisSocketPath` or LocalCAS field to `runner.jsonnet`
would therefore fail configuration or silently do nothing. This is not a live
health observation.

The next implementation step is a separately built and pinned runner image
that implements BuildBarn's `Runner` gRPC service by invoking a
LocalCAS-capable `buildbox-run`, or an upstream `bb_runner` release that
provides the same capability. Until then, keep
`sandbox.remote-apis-socket` disabled in all lab projects. The prototype's
cache-only mode is only an operator experiment after a provider is explicitly
verified; the generic `bst-qa` workflow is not a prototype measurement
harness.

`scripts/apply_recc_overlay.py` enforces this: it refuses the `dakota`,
`cosmic`, `bluefin-server`, and `bst-qa` adapters unless `--runner-capability`
is passed to assert a proven runner, and it refuses the operator-only
`--pilot-cache-only` flag for those lanes.

The dedicated `recc-baseline-pipeline` is the operator-only measurement
workflow. It validates the pinned provider before applying the cache pilot
overlay and keeps `remote-execution` blocked until both an upstream runner
candidate and a nested-socket canary prove support. It must not substitute
outer-only execution or a production cache-only fallback.

RECC admission is currently **rolled back**. The mandatory admission probe and
the shared-overlay invocation were removed from the `dakota`, `cosmic`,
`bluefin-server`, `bst-qa`, and `bst-cache-warm` lanes because they gated on a
`remoteApisSocketPath:` field that, per the concrete blocker above, the pinned
`bb_runner` cannot provide. That made every BST lane fail closed permanently
rather than transiently, taking the whole build grid offline.

Those lanes now build with outer BuildStream remote execution only, which is
the documented rollback path. The USB4 link gate, the name-based
worker/runner readiness gate, and the distributed-only (`build-mode: re`)
requirement are all retained.

`scripts/apply_recc_overlay.py` still refuses the production lanes without
`--runner-capability`; it is simply no longer invoked by them. Re-enable the
overlay invocation and the admission probe together, in the same change that
deploys a runner which honors `remoteApisSocketPath`.

## Rollback

Remove the `recc-casd` container and the `nested-reapi` volume mounts. Nothing
else references the socket, so no workflow or downstream repository change is
required and the outer BuildStream worker continues to use the existing
`bb_runner` path.
