---
name: systemd-container-tests
description: >
  Native systemd E2E tests in a privileged disposable Kubernetes Pod (no VM).
---

### 11. Native-systemd E2E Testing

`run-systemd-container-tests` validates native systemd behavior inside a
scheduler-managed Kubernetes target Pod, without KubeVirt, a disk artifact, or
nested Podman. It creates a privileged disposable target Pod with systemd as
PID 1; qecore and Behave run inside that target, never under Argo emissary
PID 1.

A full desktop smoke suite is still blocked by GNOME session handoff inside the
container target, so do not claim that all desktop suites pass there. System-level
and headless-qecore suites are the current working target; desktop GNOME Shell
suites remain under investigation.

The `homebrew` lane below is the one deliberate, targeted exception: it needs a
real session (Ptyxis typing, two `@chairlift_ui` scenarios), so it is a probe of
that unproven boundary rather than a claim that the boundary is solved. Its
provisioning — Homebrew, the systemd user manager, the reconciled runtime
directory — is validated statically (`argo lint`, unit assertions, `bash -n`
over every extracted runner/heredoc block) and by running the
`brew-preinstall` settle block against a stub `systemctl`. That covers the
lane's own branch logic and nothing else: no real systemd, logind, Homebrew, or
cluster has executed it. Read a session-handoff failure there as the known
desktop limitation, not as a regression in that provisioning.

#### Core Containerized Testing Rules:
1. **Native systemd boundary:** Create the target with an Argo `resource`
   template and set its owner reference to the Workflow. The runner waits for
   `systemctl is-system-running`, `dbus`, and `systemd-logind` before invoking
   qecore, and deletes the target in an EXIT trap.
2. **Bounded privileged runtime:** Keep privilege confined to the target Pod.
   Request 2 CPU, 4 Gi memory, and 20 Gi ephemeral storage, with limits of
   4 CPU, 8 Gi memory, and 40 Gi ephemeral storage. Do not add a node pin,
   VMI, raw-disk build, or containerDisk step:
   ```yaml
   securityContext:
     privileged: true
     runAsUser: 0
     allowPrivilegeEscalation: true
   ```
3. **Resolver repair:** The memory-backed `/run` emptyDir volume breaks the
   image's `/etc/resolv.conf` symlink (it typically points below `/run`). The
   runner must copy its own Kubernetes-provided `/etc/resolv.conf` into the
   target before any `git clone` or `pip install`, and replace the dangling
   symlink with that file.
4. **Autologin test user:** The ephemeral `bluefin-test` account must have a
   non-expired shadow `lastchg` value for GDM PAM autologin to succeed. A zero
   or expired value forces a password change and blocks login before qecore
   starts. Set `lastchg` to the current day (or a recent value) when preparing
   the target image.
5. **Pip bootstrapping:** Minimal bootc target images do not always include
   `pip`. Bootstrap it with `python3 -m ensurepip --default-pip`, then install
   qecore, dogtail, and Behave inside the disposable target.
6. **Both suite allowlists:** `SUITE` is validated twice — once in the runner
   script before the target Pod is touched, and again inside the heredoc that
   writes `/workspace/run-behave.sh`. A suite added to only the first passes
   validation, boots the target, and then exits 2 from inside it. Add every new
   suite to both `case` statements.
7. **Per-suite provisioning stays behind a suite guard:** anything one suite
   needs and the others do not (Homebrew, a systemd user manager) belongs
   inside `if [[ "${SUITE}" == "<suite>" ]]`, so no other suite pays the boot
   or setup cost. The `TARGET_SETUP` heredoc is quoted, so it inherits nothing
   — pass `SUITE` into it explicitly via `env`.
8. **One session, two lookups:** `systemctl --user` reaches the user manager
   over local transport at `${XDG_RUNTIME_DIR}/systemd/private` and ignores
   `DBUS_SESSION_BUS_ADDRESS`; qecore/dogtail reach the session and a11y bus
   through the absolute `DBUS_SESSION_BUS_ADDRESS`/`AT_SPI_BUS_ADDRESS` and
   ignore `XDG_RUNTIME_DIR`. Derive all three from one directory. Moving one
   without the other gives either an unreachable manager or a dead a11y bus.
9. **Provisioning steps capture, checks decide:** under `set -euo pipefail` a
   bare `systemctl start` aborts the script before the check that would explain
   it. Capture the exit code (`|| RC=$?`), let the binary/socket check be
   authoritative, and print a named error plus `systemctl status` and `loginctl
   show-user <user>` before `exit 1`. The same applies to command substitution:
   `RUNTIME_DIR=$(loginctl show-user … || true)` so an empty value reaches the
   friendly diagnostic instead of killing the shell.
10. **Settle a session unit's state before a suite starts it:** units pulled in
    by `graphical-session.target` with `Restart=on-failure` and
    `StartLimitBurst` may already have run — or still be running — by the time
    Behave starts them explicitly. `ActiveState` alone cannot tell those apart,
    so read `SubState` too, immediately before behave and with the lane's
    reconciled `XDG_RUNTIME_DIR` (not a guess). Read the pair in **one**
    `systemctl show --property=ActiveState --property=SubState` call: two calls
    can straddle a transition and hand the branch a pair the unit never held.
    Use the `key=value` output, not `--value` — a multi-property `--value` read
    prints in systemd's own property order, not the requested one, so parsing
    it positionally is a silent mis-assignment waiting to happen.
    - `activating` + `start`: a **healthy run in flight**. Wait for it, capped
      (`BREW_PREINSTALL_WAIT_SECONDS`), polling `ActiveState`/`SubState`, and
      log progress on an interval — a bounded wait that prints nothing for a
      quarter of an hour is indistinguishable from a dead runner in the Argo
      log view. Killing it discards work the deadline already budgets for and
      hands the suite a half-applied result.
    - `activating` + `auto-restart` (`auto-restart-queued` on systemd ≥ 254):
      the `RestartSec` gap after a failure. `is-failed` reports nothing and
      `reset-failed` cancels nothing here, and the queued restart would race
      the suite's own start — do not wait it out, diagnose and clear it.
    - `failed`, or a run that outlasts the wait: dump `systemctl --user
      status` before anything clears it.
    - `inactive`/`active` are the settled states, but `inactive` is also what
      a unit that is not installed, or that systemd skipped on a failed
      `Condition*`, looks like — and starting either exits 0. Log `LoadState`,
      `ConditionResult` **and `ConditionTimestamp`**: `ConditionResult=no` on
      its own means either of two opposite things, and only an empty
      `ConditionTimestamp` marks the difference (see the verified finding
      below). Print the verdict, not just the raw properties.
    - Any property read can come back empty (unreachable manager, unknown
      property). Default to a non-settled sentinel such as `unknown` so an
      empty read falls through to the diagnostic branch instead of being
      mistaken for a clean slate — and so `set -u` does not abort the lane.

    Then `stop` the unit — which cancels a queued auto-restart and clears a
    latched `RemainAfterExit=true` success — and only then `reset-failed` to
    clear the start-limit counter. Capture both exit codes and warn by name;
    `|| true` throws away the reason the next failure will be blamed on. If the
    `stop` fails, **re-read the state** before warning: the pre-stop sample
    cannot say what survived, and the three survivors are not equally bad. A
    still-`activating` run that is not in the auto-restart gap means a live
    install is executing and the suite's start will run concurrently against
    the same prefix — say that specifically, not just "a queued auto-restart
    may race". Never reset *after* the suite's start: that would hide real
    failures. Be honest about what the suite's start then re-covers: it re-runs
    the unit, but if the unit is idempotent or content-addressed that re-run
    may legitimately do nothing, so it proves the start path, not the work.
11. **Trap TERM and INT, not just EXIT:** `activeDeadlineSeconds` expiry and
    `argo terminate` reach the runner as signals, and an untrapped SIGTERM
    kills bash without running the EXIT trap — the privileged target Pod then
    waits for owner-reference GC while holding its whole CPU/memory
    reservation. Put the delete in a function, `trap` it on `EXIT`, and add
    `trap 'cleanup; exit 143' TERM` / `trap 'cleanup; exit 130' INT` so the
    handler exits instead of resuming. `kubectl delete --ignore-not-found`
    makes the double delete harmless. A trapped signal is handled once the
    current foreground command returns, so the deletion is immediate when the
    signal reaches the runner's process group (which also ends the in-flight
    `kubectl exec`) and otherwise happens as soon as that exec returns.

#### The `homebrew` lane

`suite=homebrew` is the only suite that needs a provisioned Homebrew prefix and
a real systemd **user** manager (`brew-preinstall.service` is a user unit). The
runner adds both in `TARGET_SETUP`, guarded on the suite:

```bash
systemctl unmask --runtime brew-setup.service
BREW_SETUP_RC=0
systemctl start brew-setup.service || BREW_SETUP_RC=$?   # never fatal on its own
if [[ ! -x /var/home/linuxbrew/.linuxbrew/bin/brew ]]; then          # the real gate
  echo "brew-setup.service left no brew at … (start exit ${BREW_SETUP_RC})" >&2
  systemctl status --no-pager --full brew-setup.service >&2 || true
  exit 1
fi

LINGER_RC=0
loginctl enable-linger bluefin-test || LINGER_RC=$?      # creates the user object …
USER_MANAGER_RC=0
systemctl start user@1000.service || USER_MANAGER_RC=$?  # … and its manager
RUNTIME_DIR=$(loginctl show-user bluefin-test --property=RuntimePath --value 2>/dev/null || true)
if [[ -z "${RUNTIME_DIR}" ]]; then
  report_user_manager_failure "logind assigned no runtime directory to bluefin-test \
(enable-linger exit ${LINGER_RC}, user@1000.service start exit ${USER_MANAGER_RC})"
  exit 1
fi
DBUS_SOCKET_RC=0
runuser -u bluefin-test -- env XDG_RUNTIME_DIR="${RUNTIME_DIR}" \
  systemctl --user start dbus.socket || DBUS_SOCKET_RC=$?
# Both sockets are named guards, never a bare `[[ -S … ]]`: under `set -e` a
# bare check aborts the script with no line saying which socket was missing.
if [[ ! -S "${RUNTIME_DIR}/systemd/private" ]]; then                 # manager reachable
  report_user_manager_failure "user@1000.service exposed no control socket at \
${RUNTIME_DIR}/systemd/private (start exit ${USER_MANAGER_RC})"
  exit 1
fi
if [[ ! -S "${RUNTIME_DIR}/bus" ]]; then                             # session bus live
  report_user_manager_failure "the bluefin-test user manager exposed no session bus at \
${RUNTIME_DIR}/bus (dbus.socket start exit ${DBUS_SOCKET_RC})"
  exit 1
fi
printf '%s\n' "${RUNTIME_DIR}" >/workspace/qa-runtime-dir
```

Every capture above exists so the *authoritative* check gets to run and report.
`systemctl start` under bare `set -e` aborts the script before the binary or
socket check can say anything, which turns a legible "no brew at …" into an
unexplained exit. Each real check therefore prints a named error plus
`systemctl status` (`brew-setup.service` or `user@1000.service`) and `loginctl
show-user bluefin-test` before exiting 1.

The runner then reads `/workspace/qa-runtime-dir` — the directory that was
already proven to carry both sockets — and exports `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS`, and `AT_SPI_BUS_ADDRESS` from it for this suite
only. Do not query `RuntimePath` a second time from the runner: a second answer
is unvalidated and can differ from the one that was asserted. Do not hard-code
`/run/user/1000` either: logind owns that path, and pinning it while leaving the
bus addresses under `/home/bluefin-test/run` is exactly the split rule 8
forbids. Every other suite keeps the runner-created `/home/bluefin-test/run`
triple unchanged.

`run-behave.sh` also settles `brew-preinstall.service` immediately before
Behave's explicit start, with the same reconciled `XDG_RUNTIME_DIR` (rule 10):
sample `ActiveState` *and* `SubState` in one `systemctl show`, wait out an
in-flight `activating (start)` run for up to `BREW_PREINSTALL_WAIT_SECONDS`
(900s, polled every 10s, progress logged every 60s), dump `status` for
`failed`, the `auto-restart` gap, or a run that outlasts that wait, report
`LoadState` plus a `ConditionResult`/`ConditionTimestamp` verdict for a settled
`inactive`/`active`, then `stop` — cancelling a queued auto-restart and any
latched `RemainAfterExit=true` success — and `reset-failed`, warning by name if
either step fails. A failed `stop` re-reads the unit and names what survived:
a live install still running (the suite's start would then run brew
concurrently against the same prefix), a queued auto-restart, or a latched
`active`. Behave's start then executes the unit rather than returning from the
latch, but because `brew-preinstall` is content-addressed that re-run is a fast
no-op after a successful install (unchanged Brewfile hash); it re-covers the
unit's start path, not the install. After a *failed* run nothing was recorded,
so the re-run does the real work and reports the real error. The runner deletes
the target Pod from a `cleanup_target` function trapped on `EXIT`, `TERM`, and
`INT` (rule 11). The wall-clock budget for this lane is documented on
`activeDeadlineSeconds` (7200s) in the template and in
[`docs/reference/WORKFLOWS.md`](../../reference/WORKFLOWS.md); it pays for the
network cask install once, the in-flight wait cap is deliberately that same
900s, and the restart attempts the session can burn before the lane ever
samples the unit are absorbed by the headroom rather than by a budget line.
The template's `phase:`/`headroom:` comment lines are parsed by
`tests/unit/test_container_only_qa_workflows.py`, which re-derives the totals
and the stated minutes — edit the numbers, not the prose.

The suite itself verifies this contract in `before_all` and fails the run — it
never skips. See `projectbluefin/testsuite`'s `tests/homebrew/README.md`.
Submission contract: [`docs/reference/WORKFLOWS.md`](../../reference/WORKFLOWS.md).

#### Verified session findings

- A privileged target Pod running systemd as PID 1 is viable for native-systemd
  E2E tests.
- `/run` must be an `emptyDir` for systemd, but that invalidates the
  `/etc/resolv.conf` symlink. Copy the runner's live resolver into the target and
  overwrite the broken symlink before network-dependent setup.
- **Do not pre-start `gnome-ponytail-daemon`** in the target. `qecore` starts and
  manages the daemon itself; an existing instance will collide with the session
  it tries to create.
- The disposable `bluefin-test` user needs a valid, non-expired shadow `lastchg`
  entry. Without it, GDM autologin fails and qecore cannot reach the desktop.
- `qecore` does **not** propagate arbitrary suite-level environment variables to
  its spawned user script. Persist any inputs the suite needs (image references,
  branch names, secrets paths) in a file the target can read, and have the test
  runner or environment read that file instead of relying on env propagation.
- The reason for that: `qecore-headless` reads `gnome-session`'s
  `/proc/<pid>/environ` after starting GDM and **replaces its own environment
  with it** (keeping only a short list — `PYTHONPATH`, `TERM`, `LOGGING`,
  `GNOME_ACCESSIBILITY`, …), then launches the user script with that
  environment. So `runuser … env …` values shape qecore's pre-session run, not
  behave's. Keep the runner's `XDG_RUNTIME_DIR` equal to the runtime directory
  logind assigned, so the pre-session and in-session values are identical
  instead of silently diverging.
- `brew-setup.service` ships **enabled** in the bootc image and carries
  `ConditionPathExists=!/etc/.linuxbrew`, so it normally runs at target boot and
  a later `systemctl start` is *skipped* while still exiting 0. Assert
  `/var/home/linuxbrew/.linuxbrew/bin/brew`, never the unit's return code.
- `systemctl unmask --runtime <unit>` exits 0 when the unit is not masked — and
  even when it does not exist — so it is safe under `set -euo pipefail`. It only
  clears runtime masks, which is what `systemd.mask=` on the kernel cmdline
  creates in the QEMU lane.
- `loginctl show-user <user>` fails with `No such process` until that user has a
  session or lingering enabled. Run `loginctl enable-linger` first, then read
  `--property=RuntimePath`; `runuser` alone opens no PAM/logind session, so
  without it there is no user manager and no `/run/user/<uid>` at all.
- `brew-preinstall.service` is `Type=oneshot` with `RemainAfterExit=true`,
  `WantedBy=graphical-session.target`, `Restart=on-failure`, `RestartSec=30`,
  `StartLimitIntervalSec=600`, `StartLimitBurst=3`, `ConditionUser=!@system`,
  and `ConditionPathExists=/var/home/linuxbrew/.linuxbrew/bin/brew`. The
  session qecore starts pulls it in on its own, so by the time the suite starts
  it explicitly it may be `failed` (suite reports the start-limit, not the
  cause), `activating (auto-restart)` during the 30s `RestartSec` gap (a queued
  restart job that `reset-failed` does not cancel and that then races the
  suite's start), `activating (start)` with the install still running, or
  `active` from a successful run that `RemainAfterExit` keeps latched (the
  suite's start becomes a no-op). `inactive` is ambiguous too: a skipped
  `Condition*` or an uninstalled unit looks identical to "never ran", and both
  start with exit 0. Read `ActiveState` *and* `SubState`, wait out an in-flight
  run, stop the rest, then `reset-failed` — see rule 10.
- **`ConditionResult=no` does not mean the condition failed.** Verified on
  systemd 260.2: `systemctl show <unit> --property=ConditionResult
  --property=ConditionTimestamp` reports `ConditionResult=no` with an **empty**
  `ConditionTimestamp` for any unit whose conditions have never been evaluated
  — every loaded-but-never-started unit on the host, and also every
  `LoadState=not-found` unit, for which `systemctl show` still exits 0 and
  answers `inactive`/`dead`/`no`. A unit that really was condition-skipped
  carries a **populated** `ConditionTimestamp` (e.g. `brew-setup.service` on a
  host whose prefix already exists). So `ConditionTimestamp`, not
  `ConditionResult`, is what separates "never evaluated" from "evaluated and
  failed"; log both and print the verdict.
- **`systemctl show --property=A --property=B --value` does not answer in the
  order asked.** Verified on systemd 260.2: requesting
  `ConditionResult,ConditionTimestamp,LoadState` with `--value` prints the
  `LoadState` value first, because systemctl uses its own property order.
  Positional parsing of a multi-property `--value` read therefore assigns the
  wrong variables silently. Read the `key=value` form and match on the key —
  which is also the only way to sample two properties **atomically**, and
  sampling `ActiveState` and `SubState` in separate calls can pair states the
  unit never simultaneously held.
- `/usr/libexec/brew-preinstall` is **content-addressed**: it hashes
  `/usr/share/ublue-os/homebrew/preinstall.d/*.Brewfile` and compares that with
  `~/.local/share/ublue-os/brew-preinstall-state.json` before touching brew, so
  a second run after a successful one exits immediately on the unchanged hash.
  Restarting the unit therefore costs one network cask install per *lane*, not
  per start — which is why the wait cap and the deadline budget both spend 900s
  on it exactly once, and why a post-stop re-run proves the unit starts, not
  that the install happened.
- Wall clock: `run-tests` carries `activeDeadlineSeconds: 7200`. The homebrew
  lane's own budget (pull + boot + pip + brew prefix + session + one network
  cask install + 15 Ptyxis/dogtail scenarios) is ~88 minutes with no
  contention, leaving ~32 minutes of headroom. Two costs sit in that headroom
  rather than in a budget line: an in-flight run that fails after being waited
  out and is then redone by the suite (a second 900s install), and the
  `Restart=on-failure` attempts the session burns *before* `run-behave.sh` ever
  samples the unit — bounded by `StartLimitBurst=3` within
  `StartLimitIntervalSec=600`, and overlapping the lane's earlier phases
  anyway. Even a run that pays both lands ~7 minutes inside the deadline. Every
  other suite finishes far inside that, and the deadline is a hang guard rather
  than an expectation. The template writes those numbers as machine-checked
  `phase:`/`headroom:` comment lines.

