# Dakota distributed benchmark — 2026-07-22

## Scope and controls

Five fresh Dakota commits were exercised sequentially through the distributed Argo/BuildStream path in namespace `argo`. Before the matrix, the live `buildstream-remote-cache` ConfigMap was reconciled to upstream-only source caches with `source-caches.override-project-caches: true`; the deployed Dakota WorkflowTemplate remained stale, so the matrix used inline workflows with the corrected project cache stanza. No further cluster configuration changes were made during the matrix.

Each iteration used:

- repository: `https://github.com/projectbluefin/dakota.git`
- target: `oci/bluefin.bst`
- `build-mode=re` and the `bst-build` semaphore
- BuildBarn remote execution through the configured frontend
- image `<ghost-ip>:30500/bst2:64eb0b4930d57a92710822898fb73af6cc1ae35d`
- upstream-only artifact/source caches; project source-cache override enabled
- privileged execution with `/dev/fuse` and `/var/lib/dakota/buildstream-cache`
- 8 CPU / 16Gi request, 16 CPU / 32Gi limit
- actual command: `bst --config /tmp/buildstream.conf --no-interactive --max-jobs 8 --fetchers 4 build oci/bluefin.bst`

## Five full-build results

| Iteration | Workflow | Commit | StartedAt | FinishedAt | Duration | Phase | Result / evidence |
|---:|---|---|---|---|---:|---|---|
| 1 | `dakota-benchmark-inline-kxjgs` | `39264b8e9341fcad6e57a70c3b79e6dda1d3155e` | 2026-07-22 01:45:33 -04:00 | 2026-07-22 03:45:42 -04:00 | 2h0m9s | Failed | Cold full build timed out, exit 143 |
| 2 | `dakota-benchmark-inline-p6vfq` | `56b6bae297e73943f786bba83a19903bbd16e7c6` | 2026-07-22 07:47:49 -04:00 | 2026-07-22 08:08:02 -04:00 | 20m13s | Failed/terminated | Full build terminated during repeated artifact/source pull stall |
| 3 | `dakota-full-build-iter3-294kn` | `54e1d7f3ce46445670760aa7377d4fcb0dd780a8` | 2026-07-22 08:22:15 -04:00 | 2026-07-22 08:49:59 -04:00 | 27m44s | Failed/terminated | Full build remained in repeated source fetch for default OCI layers; exit 143 |
| 4 | `dakota-full-build-iter4-24pdw` | `8d20693fda8348c7ea197da614fae23f0b196f08` | 2026-07-22 08:51:56 -04:00 | 2026-07-22 08:52:49 -04:00 | 53s | Failed | Remote build waited 2m57s, then `rustc -vV` failed with permission denied; exit 255 |
| 5 | `dakota-full-build-iter5-b8kzt` | `6928fe3ac0bf94d52560d32b2e77d5f3e8e6e81e` | 2026-07-22 08:53:32 -04:00 | 2026-07-22 08:54:19 -04:00 | 47s | Failed | Same remote-runner `rustc -vV` permission failure; exit 255 |

All five rows are actual full-build attempts. None produced a successful image build.

## Full-build failure evidence

Iterations 4 and 5 reached remote execution successfully:

```text
Uploading input root                         SUCCESS
Waiting for the remote build to complete     SUCCESS (00:02:57)
make: TMPDIR value /worker/build/226ed6bcbdcb68fc/tmp: No such file or directory
make: using default temporary directory '/tmp'
error: could not execute process `rustc -vV` (never executed)
Caused by: Permission denied (os error 13)
make: *** [Makefile:44: manpages] Error 101
```

Iterations 1–3 failed earlier in cold acquisition/fetch behavior. Iteration 4 and 5 logs also reported:

```text
Failed to initialize remote https://cache.projectbluefin.io:11001 ... HTTP 502
Failed to initialize remote grpc://frontend.buildbarn.svc.cluster.local:8980:
  Configured remote does not implement the Remote Asset Fetch service
```

## E2E

The independent Dakota container E2E passed:

- workflow: `dakota-container-qa-c98sd`
- image: `<ghost-ip>:30500/dakota:testing`
- phase: `Succeeded`
- duration: 20s
- all three workflow nodes succeeded

This confirms the published container/image smoke path independently of distributed compilation.

## Telemetry and data-driven conclusions

The observed worker/node telemetry showed low node utilization during the failed smoke path rather than CPU or memory saturation. The limiting factors were cache/backend and remote-runner correctness:

1. **Remote runner filesystem/execution is the primary blocker.** The remote action accepts and starts, but the input-root environment lacks a usable per-action `TMPDIR`, and `rustc` cannot execute from the labeled input root. Fix the BuildBarn runner sandbox/input-root permissions and create the action temporary directory before tuning concurrency.
2. **Source/artifact cache initialization is unreliable.** `cache.projectbluefin.io` returned HTTP 502, while the BuildBarn frontend did not implement Remote Asset Fetch. Keep source caches upstream-only until the Remote Asset service supports the BuildStream URN/API used by this client.
3. **Hardware was not maximized by this benchmark.** Low observed node utilization during failures means raising `--max-jobs`, workers, or semaphore capacity would not help yet; the build is failing before sustained compilation. Optimize hardware utilization only after a full build reaches stable remote execution.
4. **Keep the distributed gate and sequential benchmark.** Retain `build-mode=re`, fresh USB4 admission, Ready workers on both nodes, and one benchmark at a time until the runner and cache failures are fixed.

## Limitations

The five requested full-build iterations are complete as measurements, but there were zero successful full builds. The report therefore supports failure diagnosis and prioritization, not a valid successful-build throughput median.

## Additional controlled-run lessons

The later controlled runs confirmed that local success and container success are
not equivalent to distributed recovery. `dakota-full-local-push-fzwsl` produced a
published image, and `dakota-container-qa-c98sd` passed, but the distributed gate
remained red because no full remote BuildStream action completed. Future agents
must record these as separate lanes and must not use the local image digest as
evidence for the distributed build.

The first graph-validation rejection was caused by missing workflow resource
requests/limits and cluster admission/quota policy. Validate workflow resources
before attributing a rejected submission to Dakota or BuildBarn.

## Remediation status after the benchmark

The runner configuration was corrected and reconciled to use native BuildBarn directories,
root/chroot execution, per-action `/tmp` and `/var/tmp` symlinks, required input-root
device nodes, and one action slot per runner. The earlier `setTmpdirEnvironmentVariable`
setting was removed because it exported a worker-side path that was invalid after chroot.
The virtual/FUSE directory experiment was abandoned after `operation not permitted`, and
stale worker mounts were lazily unmounted before worker recreation.

These changes removed the previously observed runner-configuration failure mode but did
not make the distributed gate green: direct isolation still shows the full SDK input root
stalling during CAS materialization, with inconsistent shard availability and
`context canceled` input-fetch errors. Source caches therefore remain upstream-only and
read-only; BuildBarn is retained for artifact/RE writes and remote execution. Keep the
Dakota gate sequential and distributed-only, and do not raise jobs, workers, or semaphore
capacity until full-root materialization completes reliably.

## Controlled retry after runner reconciliation

Workflow `dakota-distributed-controlled-2nvcb` admitted successfully with fresh USB4
validation and two Ready workers. It progressed beyond the former Rust/TMPDIR failure:
input roots uploaded and remote actions reached the BuildBarn execution phase. The
coordinated worker restart then caused both active workers to disappear while actions
were executing (`Worker ... disappeared while task was executing`), so the first retry
ended with exit 255. Its automatic retry started afterward and was still fetching/pulling
artifacts at the time of this note. This is evidence that the runner correction changed
the failure stage, not evidence of a green build. The gate remains red until a retry
finishes, publishes the image, and the registry digest plus container E2E are verified.
