# Lab RECC project overlay

`scripts/apply_recc_overlay.py` prepares an isolated BuildStream checkout for
the lab RECC pilot. It is intentionally a lab-owned checkout transformation:
it does not edit `.github/workflows`, upstream repositories, or production
lanes, and it is not a measurement workflow.

The helper has explicit adapters for `dakota`, `cosmic`, `bluefin-server`, and
`bst-prototype`/`bst-qa`. It adds the upstream-style wrapper element, the RECC
project include, GCC/Clang `digest-environment` includes, and the `recc`
project option pinned to the resolved policy. It refuses unknown layouts,
conflicting existing overlay files, inline dependency lists, and endpoints
containing credentials.

## Mandatory, unambiguous mode selection

Every invocation must state exactly one mode, and there is no default:

| Adapter | no mode flag (what the templates use) | `--runner-capability` | `--pilot-cache-only` |
| --- | --- | --- | --- |
| `dakota`, `cosmic`, `bluefin-server`, `bst-qa` | **refused** — lane fails closed | `recc=remote-execution` + `sandbox.remote-apis-socket` | **refused** (operator-only flag) |
| `bst-prototype` | **refused** | `recc=remote-execution` + nested socket | `recc=cache-only` (requires an explicit provider) |

`--runner-capability` is an assertion that the deployed BuildBarn runner honors
BuildStream's `remoteApisSocketPath`. No such runner is verified in this
checkout, so **every mandatory-RECC lane refuses**, before the checkout is
touched and before the expensive build work. That refusal is the intended
behavior: a lane that cannot run nested RECC must fail, not quietly build with
outer remote execution only or with a cache-only fallback.

`--pilot-cache-only` is an **operator-only** flag for the documented prototype
baseline in `docs/reference/recc-baseline.md`. Only the isolated
`recc-baseline-pipeline` may pass it; mandatory-RECC adapters refuse it
outright.

Running a lane with outer BuildStream remote execution alone is only reachable
through a deliberate GitOps rollback (revert the overlay invocation and the
admission check, let ArgoCD reconcile). It is not a normal operating path.

The generic `bst-qa` workflow is not the prototype measurement harness. It is a
mandatory QA lane and must keep the no-mode invocation above; it cannot be used
to claim RECC cold/warm timings, action-cache results, or remote worker actions.
The dedicated `recc-baseline-pipeline` is the operator-only measurement
workflow and must validate its provider before checkout/build work.

## Prototype provider status

The `bst-prototype` adapter intentionally has no adapter-level default because
the workflow validates the provider in the actual checkout. The prototype
checkout now pins the freedesktop-sdk junction and its
`components/buildbox.bst` provider, and the workflow defaults to that exact
element reference. Omitting or overriding the provider still fails closed when
the referenced junction or element is absent.

## The `recc` binary must come from a pinned element

The generated wrapper (`files/recc-wrapper/recc-wrapper`) execs `recc`. A
BuildStream sandbox does not inherit the `bst2` image's host binaries, so the
overlay requires an operator-verified element that stages `recc` and declares
it as the wrapper element's `runtime-depends`:

- SDK-based adapters default to `freedesktop-sdk.bst:components/buildbox.bst`,
  which freedesktop-sdk builds with `-DRECC=ON`;
- any adapter can override it with `--recc-provider ELEMENT`;
- `bst-prototype` resolves the provider through its pinned junction; `bst-qa`
  remains providerless and mandatory/fail-closed.

The overlay verifies that the named element (or its junction) exists under the
project's `element-path` and refuses with a diagnostic otherwise. The wrapper
script itself also fails closed at build time if `recc` is not on `PATH`.

```bash
python3 scripts/apply_recc_overlay.py /src \
  --project-kind dakota \
  --runner-capability
```

Diagnostics contain only the validated endpoint, policy/capability state, the
resolved `recc` provider, and SHA-256 checksums. `changed_files` reports files
written during this invocation; `file_checksums` reports the stable checksums of
all managed files, including on an idempotent second invocation.

`include/recc.yml` prepends `/usr/recc/bin` to `PATH` and keeps the standard
`/usr/bin`, `/bin`, `/usr/sbin`, and `/sbin` entries, so overlaying a project
never hides its existing tooling.

A documented pilot experiment looks like:

```bash
# Illustrative only: this provider must first exist in /src.
python3 scripts/apply_recc_overlay.py /src \
  --project-kind bst-prototype \
  --pilot-cache-only \
  --recc-provider freedesktop-sdk.bst:components/buildbox.bst \
  --json
```

The prototype has a pinned compiler dependency and the overlay attaches both
the wrapper and GCC `digest-environment` include to `recc-baseline.bst`. This
supplies `RECC_REMOTE_PLATFORM_chrootRootDigest` without inventing a digest.
Provider references are resolved and checked to remain inside the checkout
before any overlay files are written.

Adapters only patch compiler dependencies declared in the checkout's own
`build-depends` lists. Compiler elements hidden inside an upstream junction are
not rewritten; changing those would require an upstream checkout or patch
queue, which is outside this lab-only overlay.

## Workflow wiring

The Dakota, Cosmic, Bluefin Server, and `bst-qa` BuildStream pods mount the
byte-identical helper and shared endpoint from the `buildstream-remote-cache`
ConfigMap and **each invokes it with no mode flags**, after checkout and before
any `bst show`/`bst build`. Warmup paths reuse the same `build-core` template,
so they inherit the same invocation.

Because no currently pinned runner honors `remoteApisSocketPath`, the
production invocation refuses today and the lane fails. Two admission layers
make that failure early and explicit:

1. the gate step in each build pipeline (and in `bst-cache-warm.yaml`) probes
   the deployed `buildbarn-config` `runner.jsonnet` for a real
   `remoteApisSocketPath:` field and rejects the run before the build work;
2. the overlay invocation itself refuses in the build container.

Both clear only after a capable runner is independently proven and the
templates are updated to assert `--runner-capability`; neither capability nor
live health should be inferred from the presence of the sidecar or a
successful outer BuildStream build.

The dedicated operator workflow is present in this checkout but requires
ArgoCD reconciliation before it is live. Repository configuration is not
treated as live health: verify the deployed template, worker pods, Prometheus
endpoint, and BuildBarn admission state before submitting a run.
