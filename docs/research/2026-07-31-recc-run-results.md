# RECC pilot run results

These are real Argo runs against the lab's BuildBarn-backed `bst2` image and
the pinned freedesktop-sdk 25.08.14 junction. The fixture output was
deterministic in every successful run:

```text
recc-baseline-v1
sha256-like CAS digest: 2e62c57b753a02f97d6663ccbdf3573feb171300f155877bbc3ae22fbf081598/77
```

| Run | Mode | Phase | Wall time | BuildBarn CAS GETs | CAS bytes read | Result |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `recc-baseline-pipeline-45x6n` | buildstream-only | cold-local | 481 s | 3,156 | 305,180,671 | success |
| `recc-baseline-pipeline-ndmw8` | cache-only | cold-local | 411 s | 6,147 | 2,279,113,223 | success |
| `recc-baseline-pipeline-ndmw8` | cache-only | warm-remote | 428 s | included above | included above | success |
| `recc-baseline-pipeline-dn799` | cache-only | cold-local | 419 s | 18,495 | 338,260,335 | success |

The cache-only run produced a different BuildStream element key from the
control while preserving the same output digest, confirming that the RECC
configuration participates in the action key without changing the fixture
result.

## Evidence limitation

The outer BuildStream and BuildBarn evidence is available. These historical
runs predate the supported handoff now checked in for issue #532: the RECC
metrics file is written under the ephemeral BuildStream build root, printed
into the element log, and removed before artifact creation. The workflow
collects that log through BuildStream's configured `/work/buildstream-logs`
directory and fails closed if a RECC mode produces no valid StatsD metric
lines. A fresh
cache-only run is required before claiming action-cache hits/misses, local
fallbacks, or compiler timing from live evidence.

Nested RECC remote execution was not measured. The pinned upstream `bb_runner`
source still lacks `remoteApisSocketPath`/LocalCAS socket handoff support, so
production lanes remain fail-closed and the custom-runner path remains out of
scope.
