---
name: test-authoring
description: >
  Writing, debugging, and running behave/qecore/dogtail GNOME GUI tests.
  All Bluefin image tests live in projectbluefin/testsuite. Use when adding
  test scenarios, fixing AT-SPI failures, debugging Shell.Eval interactions,
  or working with the qecore-headless session.
---

# Test Authoring

## Single source of truth

**`projectbluefin/testsuite`** is the canonical test repo for all Bluefin image tests.
Tests run in two places:

- **GitHub Actions** (`e2e.yml`) — QEMU-based, triggers on every PR and image publish
- **KubeVirt lab** (`run-gnome-tests` WorkflowTemplate) — clones `testsuite` main (or a branch), runs against a real VM

Do NOT add Bluefin image tests here in `lab`. Add them in `testsuite`.
See [`docs/reference/desktop-coverage.md`](../../reference/desktop-coverage.md)
for the desktop epic's slice decomposition and runner ownership boundary.

Tests that belong in `lab/tests/` are lab infrastructure tests only:
`homelab_access`, `homelab_backup`, `homelab_storage`, `homelab_substrate`,
`service_catalog`, `flatcar`.

## When to Use

- Debugging a `run-gnome-tests` workflow failure (lab execution path)
- Fixing AT-SPI / `findChild` / `Shell.Eval` issues in the testsuite
- Debugging `qecore-headless` startup failures
- Working with GNOME Shell 50 top-bar interactions

## When NOT to Use

- Adding new Bluefin image scenarios → go to `projectbluefin/testsuite`
- Argo Workflows template YAML → [`argo-workflows/SKILL.md`](../argo-workflows/SKILL.md)
- VM boot failures before tests start → [`kubevirt-vms/SKILL.md`](../kubevirt-vms/SKILL.md)
- Detailed dogtail patterns and GNOME Shell 50 caveats → [`dogtail-patterns.md`](dogtail-patterns.md)

## Core Process

### 1. Test directory layout (testsuite)

```
tests/                            (in projectbluefin/testsuite)
├── smoke/features/               GNOME Shell, desktop identity, app launch
├── common/features/              Flatpak model, portals, polkit, immutable OS
├── developer/features/           Homebrew, Podman, Ptyxis
├── software/features/            Bazaar, Flatpak CLI
├── lifecycle/features/           bootc upgrade/rollback/migration
├── hardware/features/            udev rules, peripherals
├── security/features/            image provenance, SELinux
└── vanilla-gnome/features/       baseline GNOME parity
```

Add `.feature` files and step implementations in the appropriate suite directory.
Tag new/unstable scenarios `@wip` until they pass reliably in CI.

### Suite roles (gating vs informational)

A lane's release-verdict QA gate counts only gating suites. All other tracked
suites are informational: displayed, tracked, and linked, but never blocking.

Gating suites:

- `smoke` — boot and core identity
- `system` — bootc/atomic contract (`bootc status`, read-only `/usr`, staged upgrades, rollback)
- `flatcar` — for the flatcar lane only

Informational suites:

- `developer` — Homebrew, Podman, Ptyxis
- `software` — Bazaar, Flatpak CLI
- `common` — Flatpak model, portals, polkit, immutable OS parity

When adding a scenario, prefer expanding `system/` over an informational suite.
The bootc contract is the lab's north star.

### 2. qecore-headless session startup (required incantation)

```bash
qecore-headless --session-type wayland --session-desktop gnome <test-script>
```

Both flags are required. `wayland` only — Xorg is not available. `gnome` session desktop
matches the GNOME Shell environment Bluefin boots into.

### 2a. Aurora/KDE capability probes

Aurora uses Plasma Login Manager rather than SDDM. When probing an Aurora image,
check `plasma-login-manager` and `plasmalogin.service`; the Wayland session entry
is `/usr/share/wayland-sessions/plasma.desktop`, whose `Exec` launches
`startplasma-wayland`. Do not assume the `plasmawayland.desktop` filename or an
SDDM configuration path.

### 3. AT-SPI tree traversal — findChildren vs findChild

```python
# ✅ No-raise presence check (returns empty list, not exception)
nodes = app.findChildren(pred)
if nodes:
    nodes[0].click()

# ✅ Fast failure without the default long retry loop
node = app.findChild(pred, retry=False)

# ✗ INVALID in this repo's dogtail stack
app.findChild(pred, requireResult=True)   # requireResult kwarg doesn't exist here
app.findChild(pred, requireResult=False)  # same — will TypeError
```

`findChild(pred, requireResult=...)` is invalid. Use `findChildren(pred)` for
no-raise checks or `findChild(pred, retry=False)` for fast failure.

### 4. GNOME Shell 50 — top-bar limitations

On Bluefin (GNOME Shell 50.1), the clock and system-status area are **not reliably
actionable via AT-SPI**. The AT-SPI tree normally exposes only `Activities` and
`Show Apps` in the top bar.

**Use Shell.Eval for top-bar interactions:**

```python
# Enable unsafe mode first
global.context.unsafe_mode = True  # required for top-bar AT-SPI

# Or drive via gdbus Shell.Eval
import subprocess
result = subprocess.run([
    'gdbus', 'call', '--session',
    '--dest', 'org.gnome.Shell',
    '--object-path', '/org/gnome/Shell',
    '--method', 'org.gnome.Shell.Eval',
    'Main.panel.statusArea.dateMenu.menu.toggle()'
], capture_output=True, text=True)
```

Clock, quick-settings, and calendar interactions **must** use Shell.Eval.

### 5. bootc system assertions (system/ suite)

The `system/` suite is the most important. It validates the bootc contract:

Boot/update runs should retain a compact JSON record from the ephemeral VM:
`bootc_status`, deployment image/digest, forward update success, rollback
success, and post-update boot success. Consume it through
`scripts/collect_boot_update_telemetry.py`; a missing digest is `null`, never a
tag-derived substitute.

```gherkin
Scenario: bootc status shows a valid image
  When I run "bootc status --format json"
  Then the output contains a valid image reference
  And the transport is "registry"

Scenario: /usr is read-only
  When I run "touch /usr/test-file"
  Then the command fails with permission denied

Scenario: bootc upgrade is staged not immediate
  When I run "bootc upgrade"
  Then the output contains "Queued for next boot"
  And the current boot is unchanged
```

Prioritize system/ tests over cosmetic UI checks. The lab's north star is proving
the bootc contract holds in real user workflows.

### 6. Unsafe mode for top-bar interactions

```python
# In your environment setup or conftest
from dogtail.utils import run
run('gdbus call --session --dest org.gnome.Shell '
    '--object-path /org/gnome/Shell '
    '--method org.gnome.Shell.Eval '
    '"global.context.unsafe_mode = true"')
```

Must be called before any AT-SPI interaction with the top bar.

### 7. Debugging test failures in the workflow

Tests run inside `run-gnome-tests` — a Fedora pod SSHing into the VM. Artifacts land in `/tmp/results/` inside the pod.

```bash
# Get workflow logs
just logs
# or
argo logs -n argo <workflow-name>

# Get specific step logs
argo logs -n argo <workflow-name> --node-name run-gnome-tests

# SSH directly if VM IP is known (from workflow outputs)
ssh -i /path/to/id_ed25519 bluefin-test@<pod-ip>
```

Common failure table from /docs/ops/RUNBOOK.md:

| Symptom | Root cause | Fix |
|---|---|---|
| `TypeError` with `requireResult` | Stale dogtail pattern | Use `findChildren()` or `findChild(retry=False)` |
| Clock/quick-settings miss targets | GNOME Shell 50 AT-SPI gap | Use Shell.Eval |
| `outputs.result` has debug text | Script wrote to stdout | Move debug to `>&2` |
| Test hangs on `qecore-headless` | Missing Wayland session flag | Add `--session-type wayland --session-desktop gnome` |
| Behave exits with 127 "command not found" | `behave` missing from VM pip install block for non-system suites | Add `behave` to pip install list inside `run-gnome-tests.yaml` |

### 9. qecore `context.failed_setup` — check `is not None`, not `hasattr`

qecore's `TestSandbox` initializes `context.failed_setup = None` during setup.
Using `hasattr(context, 'failed_setup')` in `before_scenario` will **always** be
`True` — every scenario will be skipped with "Suite setup failed: None" even when
setup succeeded.

```python
# ✗ WRONG — fires even when setup succeeded (qecore sets to None)
def before_scenario(context, scenario):
    if hasattr(context, 'failed_setup'):
        scenario.skip(f"Suite setup failed: {context.failed_setup}")

# ✅ CORRECT — only fires when setup recorded an actual traceback
def before_scenario(context, scenario):
    if getattr(context, 'failed_setup', None) is not None:
        scenario.skip(f"Suite setup failed: {context.failed_setup}")
```

In `before_all`, set `context.failed_setup` to the traceback string (not `True` or `1`):

```python
def before_all(context):
    try:
        context.sandbox = TestSandbox("ptyxis", context=context)
        # ... rest of setup
    except Exception:
        context.failed_setup = traceback.format_exc()  # non-None string on failure
        return
```

### 10. Optional dependencies — decouple from the critical path

When a suite depends on an optional app (e.g. Podman Desktop), failure to find it
must NOT block all other tests in the suite.

```python
# In before_all — optional dependency pattern
try:
    context.podman_desktop = context.sandbox.get_flatpak(
        flatpak_id="io.podman_desktop.PodmanDesktop"
    )
except Exception as e:
    print(f"Warning: optional dependency not found: {e}")
    context.podman_desktop = None  # mark absent, not a fatal error

# In before_scenario — skip only tagged scenarios
def before_scenario(context, scenario):
    if getattr(context, 'podman_desktop', None) is None \
            and 'podman_desktop' in scenario.tags:
        scenario.skip("Podman Desktop not found")
        return
```

Tag scenarios that require the optional app with `@podman_desktop` (or equivalent).
Never put optional app initialization in the main try/except — it will make the entire
suite appear to have a setup failure.

When choosing between a new UI test and a new bootc contract test — prefer the
contract test. Bias toward:

- `bootc status` / `bootc upgrade` / `bootc switch` behavior
- `/usr` read-only, `/var` writable
- `composefs` / fs-verity integrity
- `uupd` orchestration
- OCI layer signature verification

See `/docs/reference/WORKFLOWS.md` for the full WorkflowTemplate reference.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll use `findChild(pred, requireResult=False)` — it's cleaner." | `requireResult` kwarg doesn't exist in this repo's dogtail. Use `findChildren()`. |
| "The top-bar items are in the AT-SPI tree, I can click them directly." | GNOME Shell 50 doesn't expose clock/system-status reliably. Use Shell.Eval. |
| "The system/ tests are slow — I'll focus on smoke tests." | The bootc contract is the lab's north star. System tests are the highest-value suite. |
| "I'll add `@wip` and clean it up later." | `@wip` scenarios are skipped in nightly runs. Fix before merging or they rot. |
| "`grep -c` returning 0 means zero matches, that's fine." | `grep -c` exits 1 when count=0. Combined with `\|\| echo 0` in a pipeline, this emits `0\n0\n` (double output), breaking exact-match steps. Use `\|\| true` instead. |
| "Key combos take effect immediately — no sleep needed." | AT-SPI operations need time to reflect UI state after a key combo. Add `sleep(1)` before checking widget state (e.g., tab count after `<Shift><Ctrl><T>`). |
| "I need to keep empty stubs like before_scenario in environment.py." | Behave runs perfectly fine without empty hooks. Prune them to keep files minimal and highly readable. |

## Red Flags

- `findChild(pred, requireResult=...)` — will TypeError
- Clicking the clock or system-status area without Shell.Eval on GNOME Shell 50
- New UI scenarios added while zero `system/` bootc contract coverage exists
- Test that only passes in smoke/developer suites but never validates bootc behavior
- `hasattr(context, 'failed_setup')` in `before_scenario` — qecore sets this to `None` by default, so `hasattr` always returns True; use `getattr(...) is not None`
- Optional dependency (e.g. Podman Desktop) initialized inside the main try/except — causes ALL tests to appear as setup failures when the optional app is absent
- `grep -c ... || echo 0` in a bash step — `grep -c` exits 1 on zero matches; `|| echo 0` fires and doubles the output; use `|| true` instead
- No `sleep()` between a keyboard shortcut (e.g. `<Shift><Ctrl><T>`) and the AT-SPI check that follows — race condition; add `sleep(1)` before checking widget state
- Omission of `behave` from the VM pip install block for non-system suites inside `run-gnome-tests.yaml` — results in exit code 127 ("command not found") at suite run-time
- Empty behave environment hooks (like `before_scenario`, `after_scenario`) that do nothing in `environment.py` — dead boilerplate; delete them so the file is clean

- **Native-systemd E2E tests** — see [systemd-container-tests.md](systemd-container-tests.md).
## Verification

Before marking a test change done:

- [ ] New scenario tagged appropriately (remove `@wip` when stable)
- [ ] All AT-SPI traversal uses `findChildren()` or `findChild(retry=False)` — no `requireResult`
- [ ] Top-bar interactions use Shell.Eval (no direct AT-SPI click on clock/system-status)
- [ ] Step definition file is in `tests/<suite>/features/steps/`
- [ ] `qecore-headless` invoked with `--session-type wayland --session-desktop gnome`
- [ ] `before_scenario` uses `getattr(context, 'failed_setup', None) is not None` (not `hasattr`)
- [ ] Optional dependencies (Flatpaks, etc.) are outside the main try/except with their own skip tag
