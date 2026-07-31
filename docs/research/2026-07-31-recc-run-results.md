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

The outer BuildStream and BuildBarn evidence is available. RECC-specific
action-cache hits/misses, fallbacks, and compiler timings remain explicitly
`unavailable` in the emitted evidence. RECC was configured with verbose/StatsD
metrics, but the nested BuildStream sandbox did not expose its metrics file
through the install-root handoff; the workflow therefore does not claim a
RECC hit or miss from these runs.

Nested RECC remote execution was not measured. The pinned upstream `bb_runner`
source still lacks `remoteApisSocketPath`/LocalCAS socket handoff support, so
production lanes remain fail-closed and the custom-runner path remains out of
scope.
