# RECC nested-runner upstream gate

## Result

The lab's pinned `bb_runner` path remains unsuitable for nested RECC. The
BuildBarn `ApplicationConfiguration` exposes readiness path checks and native
chroot execution, but the current upstream source does not contain
`remoteApisSocketPath`, `remote_apis_socket`, or LocalCAS socket handoff
support.

The inspected upstream revision was:

```text
buildbarn/bb-remote-execution
236bcd95eb8fd2136807f8f6b47093639311a9f1
```

This is source evidence, not a live cluster claim. Adding a speculative field
to `runner.jsonnet` would either fail schema validation or silently leave the
pod-local `recc-casd` socket unused.

The latest upstream tag (`20260722T162832Z-236bcd9`) is the same revision
already pinned by the worker DaemonSet. Its `cmd/bb_runner/main.go` constructs
the native local/chroot runner from `ApplicationConfiguration`; the source
contains no `remoteApisSocketPath`, `remote_apis_socket`, LocalCAS handoff, or
BuildBox runner invocation. Consequently it cannot satisfy either open runner
requirement:

- #515 needs proc-backed `/dev/stdin` to be visible inside the action root.
- #513/#533 need BuildStream's `remoteApisSocketPath` to reach the pod-local
  `buildbox-casd` socket.

No upstream-only candidate was found that satisfies both requirements. The
candidate gate is therefore blocked, not a reason to weaken the existing
fail-closed admission checks.

## Gate for adopting an upstream runner

An upstream candidate is eligible only when all of these are proven:

1. The image is pinned by immutable digest and its configuration schema
   documents the nested socket/LocalCAS capability.
2. A minimal BuildStream action sends `remoteApisSocketPath` and the runner
   stages the pod-local `emptyDir` socket into the action sandbox.
3. RECC connects through that socket and emits dedicated evidence while the
   sandbox cannot reach a host-mounted or cluster-wide socket.
4. BuildBarn worker metrics show the nested action and the output remains
   deterministic across a repeated run.

Until that canary passes, production BuildStream lanes remain fail-closed and
the isolated pilot's `remote-execution` mode remains blocked. A custom runner
implementation is out of scope.
