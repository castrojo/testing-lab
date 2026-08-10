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
directory — is validated statically and by mocked branch runs, and it has never
been executed against the cluster. Read a session-handoff failure there as the
known desktop limitation, not as a regression in that provisioning.

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
10. **Reset failed/start-limit state before a suite starts a session unit:**
    units pulled in by `graphical-session.target` with `Restart=on-failure` and
    `StartLimitBurst` can already be failed and rate-limited by the time Behave
    starts them explicitly, and the suite then sees "start request repeated too
    quickly" instead of the real error. Report the pre-existing failure, then
    `systemctl --user reset-failed <unit> || true` in `run-behave.sh` — with the
    lane's reconciled `XDG_RUNTIME_DIR`, not a guess — immediately before
    behave. Never reset *after* the suite's start: that would hide real
    failures.

#### The `homebrew` lane

`suite=homebrew` is the only suite that needs a provisioned Homebrew prefix and
a real systemd **user** manager (`brew-preinstall.service` is a user unit). The
runner adds both in `TARGET_SETUP`, guarded on the suite:

```bash
systemctl unmask --runtime brew-setup.service
BREW_SETUP_RC=0
systemctl start brew-setup.service || BREW_SETUP_RC=$?  # never fatal on its own
test -x /var/home/linuxbrew/.linuxbrew/bin/brew          # the real gate

loginctl enable-linger bluefin-test               # creates the user object …
systemctl start user@1000.service                 # … and its manager
RUNTIME_DIR=$(loginctl show-user bluefin-test --property=RuntimePath --value 2>/dev/null || true)
runuser -u bluefin-test -- env XDG_RUNTIME_DIR="${RUNTIME_DIR}" \
  systemctl --user start dbus.socket
test -S "${RUNTIME_DIR}/systemd/private"          # manager reachable
test -S "${RUNTIME_DIR}/bus"                      # session bus live
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

`run-behave.sh` also clears `brew-preinstall.service`'s failed/start-limit state
with the same reconciled `XDG_RUNTIME_DIR`, immediately before Behave's explicit
start (rule 9). The wall-clock budget for this lane is documented on
`activeDeadlineSeconds` (7200s) in the template and in
[`docs/reference/WORKFLOWS.md`](../../reference/WORKFLOWS.md).

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
- `brew-preinstall.service` is `WantedBy=graphical-session.target` with
  `Restart=on-failure`, `RestartSec=30`, `StartLimitIntervalSec=600`, and
  `StartLimitBurst=3`. The session qecore starts pulls it in on its own, so by
  the time the suite starts it explicitly it may already be `failed` and
  rate-limited — and the suite would report the start-limit, not the cause.
- Wall clock: `run-tests` carries `activeDeadlineSeconds: 7200`. The homebrew
  lane's own budget (pull + boot + pip + brew prefix + session + network cask
  install + 15 Ptyxis/dogtail scenarios) is ~89 minutes with no contention;
  every other suite finishes far inside that, and the deadline is a hang guard
  rather than an expectation.

