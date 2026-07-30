# Dogtail Testing Guide

Authoritative reference for writing, submitting, and debugging dogtail/qecore/behave tests in
this repo. Read this before adding a scenario, a step, or a new suite. Pair it with
[`/AGENTS.md`](/AGENTS.md), [`/docs/ops/RUNBOOK.md`](/docs/ops/RUNBOOK.md), and the example suites under
[`tests/`](/tests/).

> **TL;DR for agents:** Tests run inside a real GNOME Wayland session on a KubeVirt VM. The
> Argo runner SSHs in, sets up `qecore-headless`, and executes `behave` (+ optional `pytest`)
> over the AT-SPI bus using `dogtail`. You write feature files + step defs locally, push to
> `main` (or a PR branch), and submit a `just` target. Never SSH to ghost or `kubectl apply`
> workflows.

## Upstream sources of truth

This repo follows the upstream conventions; when in doubt, the upstream docs win.

| Upstream                       | Why you read it                                        |
|--------------------------------|--------------------------------------------------------|
| <https://gitlab.com/dogtail/dogtail>     | dogtail API, Wayland support, GTK4 notes.   |
| <https://gitlab.com/dogtail/qecore>      | `TestSandbox`, `Application`, `Flatpak`, `common_steps`. |
| <https://dogtail.gitlab.io/qecore/>      | Generated API docs (sandbox / application / flatpak). |
| <https://gitlab.com/dogtail/qecore/-/blob/master/templates/environment.py> | Canonical `environment.py` (copy this, adapt). |
| <https://lazka.github.io/pgi-docs/#Atspi-2.0> | Full AT-SPI2 API reference for predicate work. |
| <https://gitlab.gnome.org/ofourdan/gnome-ponytail-daemon> | Wayland input bridge.            |
| <https://fedoramagazine.org/automation-through-accessibility/> | Wayland automation overview.        |

> **Versioning:** Upstream dogtail is currently the **2.x** line on PyPI (the 1.x branch is
> legacy-maintained at `dogtail-1.x`). Upstream qecore tracks its own `X.Y` series. Any
> historical "dogtail 4.16" mentions in repo comments refer to the qecore release pinned at
> the time and predate the dogtail 2.x rename — treat them as qecore-version markers, not
> dogtail versions. The runner installs the latest releases of both from PyPI on every fresh
> VM and skips on persistent titans (see §3).

---

## 1. The Stack

| Layer                     | Role                                                                        |
|---------------------------|-----------------------------------------------------------------------------|
| **KubeVirt VM**           | Real Bluefin (`latest`/`lts`) boot on ghost. Wayland + GNOME Shell 50.      |
| **gnome-ponytail-daemon** | Bridges AT-SPI coordinates → Wayland surface coordinates (input injection). |
| **qecore-headless**       | Boots Wayland/GNOME session, sets `DBUS_SESSION_BUS_ADDRESS`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, activates ponytail-daemon. |
| **qecore.TestSandbox**    | Per-test bookkeeping: app handles, journal scoping, screenshots, retries.   |
| **dogtail 4.16**          | AT-SPI tree traversal (`tree.root.application(...)`, `findChild(ren)`).     |
| **behave**                | BDD runner. Feature files in Gherkin; step defs in Python.                  |
| **pytest**                | Adjacent suite for command/file/journal assertions (no GUI needed).         |
| **Shell.Eval (gdbus)**    | Escape hatch for GNOME 50 gaps in AT-SPI / `uinput`.                        |

### Why this combination

GNOME Shell 50 / Mutter on Bluefin has two well-known gaps:

1. **Top bar (clock, quick settings, dateMenu) toggles** are exposed to AT-SPI with
   `INT_MIN` coordinates. Clicking them via dogtail silently misses.
2. **`uinput` Super key (`KEY_LEFTMETA`)** is not routed by Mutter from python-uinput devices.

For both, drive Shell via `Shell.Eval` (see §6.4). Everything else uses normal dogtail.

---

## 2. Repo Layout

```
tests/
  shared/
    wait_for_shell.py        ← AT-SPI + Shell.Eval readiness poller (run before behave)
  smoke/
    features/
      environment.py         ← before_all/before_scenario/after_* qecore hooks
      gnome_shell.feature    ← Gherkin scenarios
      steps/steps.py         ← Custom @step defs
    test_*.py                ← Pytest suite (non-GUI: image contract, flatpak health, etc.)
  developer/
    features/                ← Ptyxis, Podman Desktop, micro
    conftest.py              ← Pytest fixtures (AT-SPI app names confirmed here)
    test_*.py
  software/
    features/                ← Flatpak / Bazaar UI flows
    test_*.py
  flatcar/                   ← Separate stack, do not mix
```

**Convention:** each suite directory contains both `features/` (behave) and `test_*.py`
(pytest). The Argo runner detects and runs whichever are present.

---

## 3. How a Test Run Executes (mental model)

1. **You push** to `main` or a PR branch.
2. **You submit** a workflow (e.g. `just run-titan-smoke` or `just run-tests`).
3. **Argo** starts the `run-gnome-tests` pod on ghost.
4. The pod's `git-sync` initContainer clones `lab @ <branch>` into a shared volume.
5. The runner container waits for SSH on the target VM, then over SSH:
   - Verifies/installs `qecore`, `behave`, `dogtail`, `pytest`, `python-uinput`,
     `gnome-ponytail-daemon` (+ `python3-gnome-ponytail-daemon`).
   - Ensures `/dev/uinput` is `chmod 0666` and SELinux-labelled.
6. **rsyncs** `tests/<suite>/` and `tests/shared/wait_for_shell.py` to `/tmp/bluefin-tests/` on the VM.
7. Runs:
   ```
   qecore-headless --session-type wayland --session-desktop gnome \
     "bash -lc 'python3 .../wait_for_shell.py && exec behave .../features/ --format json.pretty --outfile /tmp/results/results.json'"
   ```
8. Repeats for `pytest` if `test_*.py` exist.
9. SCPs `/tmp/results/` back, prints summary, exits non-zero on any failure.

**Important:** the readiness probe (`wait_for_shell.py`) runs inside the same
`qecore-headless` session as `behave`, so `unsafe_mode` and AT-SPI state are guaranteed.

---

## 4. Submitting a Test Run

### Fast path — persistent titan VMs (preferred for iterating on tests)

| Command                    | What it runs                                | Approx. duration |
|----------------------------|---------------------------------------------|------------------|
| `just run-titan-smoke`     | `smoke` suite against `titan-bluefin` + `titan-lts` | ~5 min   |
| `just run-titan-developer` | `developer` suite against both titans        | ~7 min          |
| `just run-titan-software`  | `software` suite against both titans         | ~7 min          |

Titans skip provisioning entirely. Dependency install is idempotent and skipped on
re-runs.

### Full pipeline (fresh VM, provisioning)

| Command                          | Notes                                                         |
|----------------------------------|---------------------------------------------------------------|
| `just run-tests`                 | Smoke against `latest`.                                       |
| `just run-tests-tag lts`         | Smoke against a specific tag.                                 |
| `just run-tests-matrix`          | `latest` + `lts` in parallel.                                 |
| `just run-developer-tests [tag]` | Smoke + developer on a fresh VM.                              |
| `just run-software-tests [tag]`  | Smoke + developer + software on a fresh VM.                   |

### Testing a PR branch without merging

Set `BLUEFIN_TEST_BRANCH` so the runner's `git-sync` initContainer clones your branch:

```bash
BLUEFIN_TEST_BRANCH=fix/clock-toggle-flake just run-titan-smoke
```

Or override per-submit:

```bash
argo submit --from workflowtemplate/bluefin-titan-smoke \
  -p vm-ip-latest=... -p vm-ip-lts=... -p branch=fix/clock-toggle-flake \
  -p suite=smoke -n argo --watch
```

### Filtering by behave tag

```bash
argo submit --from workflowtemplate/bluefin-titan-smoke \
  -p vm-ip-latest=... -p vm-ip-lts=... \
  -p behave-tags="--tags @regression" -n argo --watch
```

Common tags in this repo: `@smoke_suite`, `@top_bar`, `@activities`, `@quick_settings`,
`@calendar`, `@regression`, `@bluefin_<issue#>`, `@developer_suite`, `@podman_desktop`,
`@ptyxis`, `@stability`.

### Watching results

- Argo UI: <http://<lab-ip>:32746>
- Logs: `just logs` or Argo MCP `logs_workflow`
- Artifacts (`results.json`, `pytest-results.xml`, `atspi_tree.txt`) are echoed into the
  pod's stderr — use `argo logs <workflow-name> -n argo` to retrieve them.

---

## 5. Writing a New Feature

### 5.1 Anatomy

```gherkin
@smoke_suite
Feature: GNOME Shell smoke tests
  One-paragraph summary of intent.

  # Group with comments. The runner emits these to logs verbatim.
  @top_bar
  Scenario: Clock toggle button is visible in panel
    * GNOME Shell is accessible via AT-SPI
    * Clock toggle is visible in top bar
```

**Rules:**
- Start every GUI scenario with `* GNOME Shell is accessible via AT-SPI`. This is a *retrying*
  AT-SPI handshake (up to 30 s) that prevents the first real step from racing against a not-yet
  exposed shell.
- Use `*` (or `Given/When/Then`) — behave treats them the same. The repo uses `*` for
  brevity.
- Tag every scenario with at least one *suite* tag (`@smoke_suite`, `@developer_suite`, …)
  and one *area* tag (`@top_bar`, `@activities`, `@podman_desktop`, …). For regressions
  also add `@regression @<repo>_<issue#>`.

### 5.2 Reuse `qecore.common_steps` first

`environment.py` imports `from qecore.common_steps import *`. That gives you, free of charge:

| Step                                                    | Use                                                      |
|---------------------------------------------------------|----------------------------------------------------------|
| `Start application "{app}" via "{method}"`              | `command`, `menu`, `shortcut`.                           |
| `Close application "{app}" via "{method}"`              | `shortcut` is most reliable on Wayland.                  |
| `Application "{app}" is running` / `is no longer running` | Polls AT-SPI app registration.                         |
| `Wait until "{name}" "{role}" appears in "{app}"`       | Retrying child wait.                                     |
| `Item "{name}" "{role}" is "{state}" in "{app}"`        | `showing`, `enabled`, `visible`, `checked`, `focused`.   |
| `Left/Middle/Right click "{name}" "{role}" in "{app}"`  | Coordinates resolved via ponytail-daemon.                |
| `Key combo "{combo}"` / `Press key "{key}"`             | Uses uinput (caveats in §6.3).                           |
| `Type text "{text}"`                                    | Uses uinput.                                             |
| `Run and save command output: "{cmd}"`                  | Result lands in `context.command_stdout`.                |
| `Last command output "{op}" "{value}"`                  | `is`, `contains`, `starts with`, …                       |

**Do not redefine these.** Behave raises `AmbiguousStep` on a literal that shadows an existing
wildcard. If you need a different shape, give it a *distinct* name (e.g.
`GNOME Shell is accessible via AT-SPI` instead of redefining `Application "..." is running`).

### 5.3 When to add a custom step

Add one only when:

1. The behaviour is **GNOME 50 specific** and needs a `Shell.Eval` driver
   (clock/system menu/dateMenu/overview).
2. You're asserting on **dogtail tree shape** that common_steps can't express (e.g. "panel
   contains at least one toggle whose name matches `\d{1,2}:\d{2}`").
3. You need a **bespoke retry** because the default 20 s is too slow or too fast.
4. The assertion is **non-AT-SPI** (journal grep, coredump count, command exit code with
   trailing whitespace normalisation).

Put it in `tests/<suite>/features/steps/steps.py`. The directory is auto-discovered by behave
because the `features/` dir name is fixed.

---

## 6. Writing Step Definitions

### 6.1 Skeleton

```python
from behave import step
from dogtail.tree import root
from qecore.common_steps import *  # noqa: F401,F403  — keep, registers common steps

@step('Panel is present in AT-SPI tree')
def panel_is_present(context) -> None:
    # Use tree.root.application() for a live AT-SPI query — NOT context.sandbox.shell
    # (qecore caches sandbox.shell after the first lookup; the cached node never detects
    # a crashed or restarted shell).
    shell = root.application("gnome-shell")
    panels = shell.findChildren(lambda n: n.roleName == "panel")
    if not panels:
        children = [(c.roleName, c.name) for c in shell.children[:15]]
        raise AssertionError(
            f"Panel (role='panel') not found.\nTop-level children: {children}"
        )
    context.panel = panels[0]              # stash for downstream steps
```

### 6.2 dogtail API rules (very important)

These rules reflect current upstream (dogtail 2.x) behaviour and the failure modes seen on
this stack:

- **No `requireResult=` kwarg.** `findChild(pred, requireResult=True)` raises `TypeError` at
  the logging decorator on the version pinned in the runner. Use:
  - `node.findChildren(predicate)` → returns list, never raises.
  - `node.findChild(predicate, retry=False)` → fast fail without 20 s wait.
- **`searchCutoffCount`, `searchBackoffDuration`** are deprecated no-ops. Don't set them.
- **`dogtail.config.searchShowingOnly = True`** is set in `before_all` for the smoke suite.
  All searches are already filtered to visible nodes — do not add a redundant `.showing`
  check inside the predicate.
- **`tree.root.application(name)`** raises `SearchError` if absent. Wrap with try/except or
  use the `_find_application` helpers (see `smoke/steps.py`) when an app may register under
  more than one a11y name.
- **`node.text`** is a property on text/entry nodes; `node.children`, `node.name`,
  `node.roleName` are universal.
- **GTK4 shadows.** Upstream dogtail expects GTK4 apps to have window shadows disabled for
  accurate coordinate handling. The runner does not patch `~/.config/gtk-4.0/gtk.css` today;
  if you write a click-driven test against a GTK4 app and see consistent off-by-N coordinate
  misses, that's the cause — file an issue to drop the upstream-recommended CSS into the
  golden disk before adding a workaround:
  ```css
  window, .popover, .tooltip { box-shadow: none; }
  ```
- **Accessibility bus.** `qecore-headless` ensures
  `gsettings set org.gnome.desktop.interface toolkit-accessibility true` and calls
  `dogtail.utils.enableA11y()` itself. Never re-enable manually.

### 6.3 Input injection caveats

- `uinput` must be unlocked (`modprobe uinput`, `chmod 0666 /dev/uinput`). The runner does
  this on every run; you don't need to.
- **uinput typing is unreliable on these VMs** for the GNOME 50 overview search bar — use
  `Main.overview.searchEntry.set_text("...")` via `Shell.Eval` instead (§6.4).
- `Press key "Return"` (uinput) **does** work for triggering activations once the entry
  has focus. Use it to launch the first search result.
- `keyCombo("<Alt>F4")` is the most reliable way to close a window on Wayland.

### 6.4 Shell.Eval escape hatch

`Shell.Eval` runs arbitrary JS inside `gnome-shell`. It requires `global.context.unsafe_mode
= true` (set by `wait_for_shell.py`).

Stable patterns proven on Bluefin GNOME 50:

```python
def _shell_eval(js: str) -> str:
    import subprocess
    r = subprocess.run(
        ['gdbus', 'call', '--session',
         '--dest', 'org.gnome.Shell',
         '--object-path', '/org/gnome/Shell',
         '--method', 'org.gnome.Shell.Eval', js],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout
```

| Action                          | JS                                                                     |
|---------------------------------|------------------------------------------------------------------------|
| Open Activities overview        | `Main.overview.show()`                                                 |
| Close Activities overview       | `Main.overview.hide()`                                                 |
| Toggle quick settings           | `Main.panel.statusArea.quickSettings.menu.toggle()`                    |
| Force-close quick settings      | `Main.panel.statusArea.quickSettings.menu.close(0)`                    |
| Query quick settings open       | `Main.panel.statusArea.quickSettings.menu.isOpen.toString()`           |
| Toggle date menu                | `Main.panel.statusArea.dateMenu.menu.toggle()`                         |
| Set overview search text        | `Main.overview.searchEntry.set_text("Files")` (fires `::changed`)      |

**`Shell.Eval` parsing trap.** The gdbus response wraps every result as
`(true, '<inner>')` — the *outer* `true` is the DBus call-success flag, not your JS result.
Always extract the inner value:

```python
import re
m = re.search(r"\((?:true|false),\s*'(.*)'\)", out.strip())
return m.group(1) if m else ""
```

`'true' in out.lower()` is a false-positive trap because the wrapper always contains
`true`.

### 6.5 Top-bar AT-SPI nuances

- The top bar exposes `Activities` and `Show Apps` as named items. **Clock**, **System**, and
  **dateMenu** toggles are present *as nodes* but their geometries are `INT_MIN` — clicking
  them via dogtail misses. Use `Shell.Eval` for the action, then `findChildren` for the
  assertion.
- The **clock's accessible-name** is the formatted time string (e.g. `7:14 PM` or
  `Sunday 25 May, 7:14 PM`), *not* the literal `clock`. Match with
  `re.compile(r'\d{1,2}:\d{2}|clock', re.IGNORECASE)`.
- The **system menu's** accessible-name is `System` on GNOME 47/48 and `System menu` on later
  builds. Accept both: `{"System", "System menu", "System Menu"}`.
- **No lax fallbacks.** `next(iter(toggles), None)` produced silent false passes when the
  real target was missing (issue #5). If your match fails, raise with the full inventory of
  candidates for triage:

  ```python
  raise AssertionError(
      f"Clock toggle (time-pattern in accessible-name) not found.\n"
      f"All panel toggles: {[(t.name, t.roleName) for t in toggles]}"
  )
  ```

### 6.6 App-window steps

> **Upstream rule (qecore):** *Do not define `gnome-shell` as an application.* Use
> `context.sandbox.shell` — qecore already exposes the shell's accessibility tree there with
> retry semantics. Calling `get_application(name="gnome-shell", …)` will fight qecore's own
> lifecycle handling and produce flaky setup failures.

```python
context.ptyxis = context.sandbox.get_application(
    name="ptyxis",                              # short handle used in step text
    a11y_app_name="ptyxis",                     # name on the AT-SPI bus
    desktop_file_name="org.gnome.Ptyxis.desktop",  # what `Start application` launches
)
context.ptyxis.exit_shortcut = "<Alt>F4"        # used by `Close application "X" via "shortcut"`
```

The `a11y_app_name` is the AT-SPI registration. Confirmed names in this repo:

| App            | `a11y_app_name`                       |
|----------------|---------------------------------------|
| Ptyxis         | `ptyxis`                              |
| Files          | `org.gnome.Nautilus` (also `Files`, `nautilus`) |
| Settings       | `org.gnome.Settings` (also `gnome-control-center`, `Settings`) |
| Podman Desktop | `Podman Desktop` (Flatpak)            |

When in doubt, run a scenario that calls `Dump gnome-shell AT-SPI tree to results` — the
output is written to `/tmp/results/atspi_tree.txt`, retrieved by SCP, and printed to the
pod's stderr.

### 6.7 Flatpak / Podman Desktop

```python
context.podman_desktop = context.sandbox.get_flatpak(
    flatpak_id="io.podman_desktop.PodmanDesktop",
)
```

Tenet: **system-scoped Flatpak only.** Never `flatpak install --user` in tests or workflows.

### 6.8 Hooks (`environment.py`)

Required structure per suite:

```python
def before_all(context):
    dogtail_config.searchShowingOnly = True            # smoke suite
    # (verify unsafe_mode if you'll Shell.Eval — wait_for_shell.py already sets it,
    #  but assert here for safety)
    context.sandbox = TestSandbox("gnome-shell", context=context)
    context.sandbox.attach_faf = False                 # disable failure-attached-files
    context.sandbox.production = False                 # enables verbose logging

def before_scenario(context, scenario):
    # qecore version drift: command_stdout (4.16) vs last_command_output (older)
    context.command_stdout = ""
    context.last_command_output = ""
    context.sandbox.before_scenario(context, scenario)

def after_scenario(context, scenario):
    context.sandbox.after_scenario(context, scenario)

def after_step(context, step):
    # behave's JSON serialiser drops error_message for exceptions with empty str();
    # print the full traceback ourselves so it lands in Argo pod logs.
    if step.status.name in ("error", "failed") and step.exception is not None:
        traceback.print_exception(type(step.exception), step.exception,
                                  step.exception.__traceback__, file=sys.stderr)
```

Do not `sys.exit(1)` from `before_all` — set `context.failed_setup` and let scenarios fail
loudly. `before_scenario` may `sys.exit(1)` to abort the whole run when sandbox setup is
unrecoverable (e.g. AT-SPI gone).

---

## 7. Patterns That Work

### 7.1 Retry with explicit ceiling

```python
for _ in range(8):                                    # 4 s total at 0.5 s steps
    if condition_met():
        return
    sleep(0.5)
raise AssertionError("condition not met after 4 s; <diagnostic context>")
```

Always include diagnostic context in the final error. No bare `assert False`.

### 7.2 Journal-scoped assertions

The runner exports `TEST_JOURNAL_SINCE=$(date --iso-8601=seconds)` *before* tests start.
Use it to scope regressions:

```gherkin
* Run and save command output: "sh -c 'journalctl --no-pager -b --since=\"${TEST_JOURNAL_SINCE:-1 minute ago}\" -p err..emerg --lines=50 2>/dev/null | grep -c gnome-shell; true'"
* Last command output stripped "is" "0"
```

`Last command output stripped "is"` is a custom step (see `smoke/steps.py`) because
`grep -c` always appends `\n` and the upstream `Last command output "is"` does not strip.

### 7.3 Stash on `context`

```python
context.panel = panels[0]                # ok
context.clock_toggle = clock             # ok
context.active_application = app         # ok — see Close active application window
```

Anything attached to `context` is visible to later `@step` defs in the same scenario.

### 7.4 Bail with diagnostics, not silence

Print the entire candidate set on mismatch (toggles, role inventories, app names).
The cluster has no interactive debugger; the only artefact is the log.

### 7.5 Proper testing discipline (read this before adding tests)

This is the *quality bar* every new scenario must clear. A flaky or low-signal test is worse
than no test — it costs cluster time and trains agents to ignore red builds.

1. **One behaviour per scenario.** A scenario is "given X, when Y, then Z". Don't chain
   unrelated assertions. If you need to verify five panel items, write five scenarios sharing
   a tag — behave runs them with isolated `before_scenario`/`after_scenario` teardown so a
   failure in one doesn't poison the next.
2. **Deterministic by construction.** Every wait is bounded with a clear ceiling and a
   diagnostic message on timeout (see §7.1). No bare `sleep(N)` "just in case".
   `time.sleep` is only acceptable as the *step* between retry iterations, never as the sole
   synchronisation.
3. **Assert on observable behaviour, not implementation.** Prefer `findChildren(roleName=
   "panel")` over hard-coded child indices. Prefer `Main.panel.statusArea.dateMenu.menu.isOpen`
   over scraping CSS class names from Shell.Eval. Tests should survive cosmetic GNOME
   refreshes.
4. **Fail loudly, fail informatively.** Every assertion must produce enough context in its
   error message to triage from logs alone (full toggle inventory, raw Shell.Eval output,
   AT-SPI children of the parent). Argo pod logs are the *only* post-mortem surface.
5. **No silent skips.** `behave` will mark a scenario "skipped" if its `before_scenario`
   fails silently — don't catch broad exceptions in hooks without `print`ing them and
   re-raising or `sys.exit(1)`.
6. **Regressions cite an issue.** Every `@regression` scenario carries a `@<repo>_<issue#>`
   tag and a comment referencing the upstream bug. This is how the project tracks coverage
   regressions over time and prevents test drift after a fix lands.
7. **Tests must boot from a cold image.** Don't depend on state left by a previous scenario,
   a previous run, or manual VM tweaks. Titan VMs are persistent for *speed*, not for
   carrying state — the same scenario must pass on a fresh fully-provisioned VM
   (`just run-tests`).
8. **Prove the negative.** When testing that an error dialog *doesn't* appear, write the
   detection with the same predicate you'd use to find it — don't just sleep and hope.
   Example: `assert not app.findChildren(lambda n: n.roleName == "dialog" and "error" in n.name.lower())`.
9. **Verify on titan first, matrix second.** Iterating on a scenario? Use
   `just run-titan-<suite>` (5 min). Only once green, re-run on a fresh VM with
   `just run-tests` / `just run-tests-matrix` to catch first-boot races.
10. **Use `STABILITY=N`** (upstream qecore env var, §7.6) to prove a new scenario passes 10×
    in a row before merging anything tagged `@regression`. Flaky regressions erode trust
    immediately.
11. **Mirror the Bluefin tenet.** The repo's north star is *Bluefin as an image-based, atomic
    OS*. UI coverage exists to prove the bootc / read-only `/usr` / staged-update contract
    holds in real user workflows — not to be a generic GNOME QA suite. Prefer scenarios that
    cross-check image integrity (e.g. "GNOME Shell extensions load with `/usr` read-only")
    over cosmetic widget checks.
12. **Don't test what `common_steps` already covers.** If `Item "..." "..." is "showing" in
    "..."` does the job, use it — don't reinvent the predicate in a custom step.
13. **No `--user` flatpak.** System scope only, always (tenet).
14. **Tests are code.** They get reviewed, linted, and refactored. A 200-line step def is a
    smell — split into helpers, keep `@step` bodies short and readable.

### 7.6 Upstream qecore environment variables

These are recognised by `TestSandbox.__init__` and `qecore-headless`. Pass them in front of
`behave` inside your one-off invocation (or via the workflow's `env:` block):

| Variable                       | Effect                                                                    |
|--------------------------------|---------------------------------------------------------------------------|
| `AUTORETRY=N`                  | Re-run each failed scenario up to N times before marking it failed.       |
| `STABILITY=N`                  | Run every scenario N times; fail if any iteration fails. Use for flake hunts. |
| `LOGGING=yes`                  | qecore debug logging to console.                                          |
| `RICH_TRACEBACK=true`          | Rich tracebacks for assertion failures.                                   |
| `BACKTRACE=yes`                | Generate coredump backtraces and attach to HTML report.                   |
| `QECORE_EMBED_ALL=yes`         | Force-embed all attachments (videos, screenshots) even on pass.           |
| `QECORE_ENABLE_SCREENCAST=yes` | Record screencasts of each scenario.                                      |
| `QECORE_NO_CACHE=yes`          | Delete qecore cache on run start (forces fresh app registration).         |
| `PRODUCTION=no`                | Disable HTML report embeds — useful when iterating locally.               |

`STABILITY=10` is the de-facto pre-merge check for any new `@regression`. Submit via:

```bash
argo submit --from workflowtemplate/bluefin-titan-smoke \
  -p vm-ip-latest=... -p vm-ip-lts=... \
  -p behave-tags="--tags @my_new_regression" \
  -p extra-env="STABILITY=10" -n argo --watch
```

(If the workflow template doesn't expose the env var you need, add a passthrough rather
than editing the runner ad-hoc — keep the GitOps contract.)

### 7.7 Useful `qecore-headless` flags

| Flag                          | Use                                                                  |
|-------------------------------|----------------------------------------------------------------------|
| `--session-type wayland`      | Required on Bluefin; do not omit.                                    |
| `--session-desktop gnome`     | Required; selects the GNOME session over GNOME Classic.              |
| `--keep N`                    | Keep GDM alive for N scenarios then restart — fast iteration loop.   |
| `--keep-max`                  | Keep GDM alive as long as possible; restart only on failure.         |
| `--debug`                     | Enables dogtail's own debug log to stdout. Use when triaging.        |
| `--force`                     | Fails fast if the resulting session doesn't match the requested type. |
| `--restart`                   | Restart a running GDM before starting the suite.                     |
| `--virtual-monitors N`        | Experimental multi-monitor; do not use in CI suites yet.             |

The runner always passes `--session-type wayland --session-desktop gnome`. Do not change
these unless you're authoring a Xorg-specific scenario (we don't run those today).

### 7.8 `TestSandbox` attributes worth knowing

Set after `context.sandbox = TestSandbox(...)` in `before_all`. Full list at
<https://dogtail.gitlab.io/qecore/_modules/sandbox.html#TestSandbox.__init__>.

| Attribute                       | Default | When to flip                                                         |
|---------------------------------|---------|----------------------------------------------------------------------|
| `record_video`                  | `True`  | Leave on — videos attach to failures only.                           |
| `attach_video`                  | `True`  | Leave on.                                                            |
| `attach_video_on_pass`          | `False` | Flip when investigating flakes.                                      |
| `attach_journal`                | `True`  | Leave on.                                                            |
| `attach_screenshot`             | `True`  | Leave on.                                                            |
| `attach_coredump` / `_on_pass`  | `False` | Flip when investigating crashes (`@regression @bluefin_<n>` style).  |
| `opt_in_tree_on_fail`           | `False` | Flip when triaging "node not found" — embeds the AT-SPI tree.        |
| `set_keyring`                   | `True`  | Leave on — prevents keyring popups breaking tests.                   |
| `workspace_return`              | `False` | Flip if a scenario leaves apps on workspace ≠ 1.                     |
| `production`                    | `True`  | Set to `False` in this repo (verbose logs needed in Argo pod logs).  |
| `attach_faf`                    | `True`  | Set to `False` (no FAF infrastructure).                              |
| `package_list`                  | `{"gnome-shell","mutter",component}` | Extend to record extra RPM versions in the report. |

The smoke suite's `before_all` already sets `attach_faf=False, production=False` — copy that
shape for new suites.

---

## 8. Antipatterns (do not do)

| Antipattern                                                    | Why                                                       |
|----------------------------------------------------------------|-----------------------------------------------------------|
| `findChild(pred, requireResult=True)`                          | Raises `TypeError` on the current pinned dogtail; use `findChildren` or `findChild(..., retry=False)`. |
| `get_application(name="gnome-shell", ...)`                     | Upstream-forbidden. Use `context.sandbox.shell`.          |
| `if 'true' in shell_eval(js).lower()`                          | Outer DBus tuple always contains `true`; extract inner.   |
| Click clock/system/dateMenu via dogtail/ponytail               | INT_MIN coords; silently misses on GNOME 50.              |
| Press Super (`KEY_LEFTMETA`) via uinput                        | Mutter does not route it; use `Main.overview.show()`.     |
| Type into overview search via uinput                           | Unreliable; use `searchEntry.set_text(...)`.              |
| `next(iter(toggles), None)` as clock fallback                  | Silent false pass; see issue #5.                          |
| `Application "gnome-shell" is running`                         | `is_open()` doesn't apply to compositors; use the custom step. |
| Redefine a common_steps step name                              | `AmbiguousStep` at parse time.                            |
| `flatpak install --user` anywhere                              | Tenet violation. System-scoped only.                      |
| SSH to ghost / `kubectl apply workflowtemplate/...`            | GitOps rule. Push to `main`; ArgoCD syncs.                |
| Inline Python inside bash inside YAML with `:` or `'`          | YAML parse breakage. Use `jsonpath` or a separate script. |
| Speculative `time.sleep(10)` between every step                | Hides races. Use bounded retry with diagnostics.          |

---

## 9. Debugging a Failure

1. **Read the runner log first.** Search Argo logs for
   `=== BEHAVE RESULTS JSON ===` (full per-scenario report).
2. **Look for `STEP_ERROR`** — `after_step` prints a full traceback for any errored step.
3. **Inspect `/tmp/results/atspi_tree.txt`** — first smoke scenario writes it. Tells you
   which nodes exist and what their `roleName` / `name` actually are on this build.
4. **Re-run with one scenario** via `behave-tags`:
   ```bash
   argo submit --from workflowtemplate/bluefin-titan-smoke \
     -p vm-ip-latest=... -p vm-ip-lts=... \
     -p behave-tags="--tags @bluefin_4612" -n argo --watch
   ```
5. **In-cluster sanity** on a one-shot AT-SPI predicate (no workstation SSH — use a probe pod):
   ```bash
   # 1. Spin up a probe pod with the SSH key and wait for it to be Ready
   kubectl run probe -n argo --image=quay.io/fedora/fedora:latest \
     --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"ghost"},"volumes":[{"name":"k","secret":{"secretName":"bluefin-test-ssh-key","defaultMode":384}}],"containers":[{"name":"probe","image":"quay.io/fedora/fedora:latest","command":["sleep","600"],"volumeMounts":[{"name":"k","mountPath":"/root/.ssh","readOnly":true}]}]}}'
   kubectl wait -n argo pod/probe --for=condition=Ready --timeout=60s

   # 2. Run the AT-SPI check from inside the probe pod
   VM_IP=$(kubectl get vmi titan-bluefin -n bluefin-test -o jsonpath='{.status.interfaces[0].ipAddress}')
   kubectl exec -n argo probe -- bash -c "
     dnf install -y --quiet openssh-clients python3-pip 2>&1 | tail -2
     pip install dogtail -q
     ssh -o StrictHostKeyChecking=no -i /root/.ssh/id_ed25519 bluefin-test@${VM_IP} \
       'qecore-headless --session-type wayland --session-desktop gnome \
        \"python3 -c \\\"from dogtail.tree import root; shell=root.application(\\\\\\\"gnome-shell\\\\\\\"); print([(c.roleName,c.name) for c in shell.children[:20]])\\\"\"'
   "

   # 3. Clean up
   kubectl delete pod -n argo probe
   ```
6. **Common root causes**, in order: stale dogtail kwarg, GNOME 50 INT_MIN toggle,
   missing `unsafe_mode`, app a11y name drift, qecore version variable rename
   (`command_stdout` vs `last_command_output`).

---

## 10. Adding a New Suite

1. Create `tests/<suite>/features/{environment.py, <name>.feature, steps/steps.py}`.
2. Copy the `environment.py` skeleton from `tests/developer/features/environment.py` (apps)
   or `tests/smoke/features/environment.py` (shell).
3. Add a top-of-feature tag (`@<suite>_suite`) and per-area tags.
4. Submit via:
   ```bash
   argo submit --from workflowtemplate/bluefin-titan-smoke \
     -p vm-ip-latest=... -p vm-ip-lts=... -p suite=<suite> -n argo --watch
   ```
5. Once green on titans, add a `just run-titan-<suite>` recipe and/or include the suite in
   `bluefin-qa-pipeline` via the `suites=` param.
6. If the suite needs an extra RPM/Flatpak in the VM, update the `verify_test_dependencies`
   block in `argo/workflow-templates/run-gnome-tests.yaml` so the install is idempotent.

---

## 11. Checklist Before You Push

**Quality bar (§7.5):**
- [ ] One behaviour per scenario; no chained unrelated assertions.
- [ ] Every wait has a bounded ceiling and a diagnostic message on timeout.
- [ ] Assertions target observable behaviour (roles, names, `isOpen`), not implementation
      details (child indices, CSS classes).
- [ ] `@regression` scenarios cite a real issue with `@<repo>_<issue#>`.
- [ ] Scenario passes from a cold image, not just on a re-run.
- [ ] New `@regression` proven stable via `STABILITY=10` (§7.6).

**Mechanics:**
- [ ] Every GUI scenario starts with `* GNOME Shell is accessible via AT-SPI`.
- [ ] Every scenario has at least one suite tag + one area tag.
- [ ] No `requireResult=`, no `searchCutoffCount`, no `searchBackoffDuration`.
- [ ] No `get_application(name="gnome-shell", ...)` — use `context.sandbox.shell`.
- [ ] No clicking clock / system / dateMenu via dogtail — `Shell.Eval` only.
- [ ] No `uinput` Super key. No `uinput` typing into overview search.
- [ ] Custom steps print full diagnostic context on assertion failure.
- [ ] Shell.Eval steps parse the inner GVariant string (not the outer `true`).
- [ ] System-scoped Flatpak only. No `--user`.

**Validation:**
- [ ] `just lint` passes.
- [ ] Validated on titan VMs (`just run-titan-<suite>`).
- [ ] Validated on a fresh VM (`just run-tests` or matrix) before requesting review.

---

## 12. Reference Material

- **Upstream qecore**: <https://gitlab.com/dogtail/qecore>
  — `templates/environment.py` is the canonical hook layout.
- **Upstream dogtail**: <https://gitlab.com/dogtail/dogtail> (currently 2.x; legacy at
  `dogtail-1.x`).
- **qecore API docs**: <https://dogtail.gitlab.io/qecore/> — sandbox / application / flatpak.
- **AT-SPI2 API reference**: <https://lazka.github.io/pgi-docs/#Atspi-2.0>.
- **Wayland automation overview**:
  <https://fedoramagazine.org/automation-through-accessibility/>.
- **gnome-ponytail-daemon**: <https://gitlab.gnome.org/ofourdan/gnome-ponytail-daemon>.
- **Example patterns lifted into this repo**:
  <https://github.com/modehnal/GNOMETerminalAutomation> (steps + environment shape).
- **Shell.Eval API surface** (GJS): `Main.overview`, `Main.panel.statusArea.*.menu`,
  `Main.panel.statusArea.quickSettings`, `Main.panel.statusArea.dateMenu`.
- **In-repo source of truth**:
  - [`tests/smoke/features/`](https://github.com/projectbluefin/testsuite/tree/main/tests/smoke/features/) — canonical GNOME shell patterns.
  - [`tests/developer/features/`](https://github.com/projectbluefin/testsuite/tree/main/tests/developer/features/) — canonical app-window
    patterns (Ptyxis, Podman Desktop Flatpak).
  - [`tests/shared/wait_for_shell.py`](/tests/shared/wait_for_shell.py) — readiness probe.
  - [`argo/workflow-templates/run-gnome-tests.yaml`](/argo/workflow-templates/run-gnome-tests.yaml)
    — exact runner contract.
- **Operational reference**: [`/docs/ops/RUNBOOK.md`](/docs/ops/RUNBOOK.md), [`/AGENTS.md`](/AGENTS.md).


---

## Historical Lessons

Date-stamped debugging history is recorded in the sections below.

---

### 2026-05-26 — Headless session management and GNOME 50 extension assertions

**qecore-headless is a long-running process, not a one-shot runner.**
After the test suite finishes, `qecore-headless` keeps running to manage the GNOME session.
The next run will hit "Attempting to start another instance / Exiting the duplicate" unless the
prior process is explicitly stopped first. Lock file removal alone is insufficient — the process
must be terminated.

**Do NOT use `pgrep -f` inside an SSH heredoc to find the headless process.**
`pgrep -f <pattern>` searches full process cmdlines. When a heredoc is passed as `bash -c '...'`,
the bash process's cmdline *is* the entire heredoc. Any pattern matching text in the heredoc
(including the binary path `qecore-headless`) will self-match and `xargs kill` will kill the
current shell, causing SSH to exit with code 255 (connection reset by remote).

**Use `/proc/*/exe` inspection instead:**
```bash
for _pid in $(ls -la /proc/[0-9]*/exe 2>/dev/null \
               | awk '/qecore/{match($0, /\/proc\/([0-9]+)\//, m); print m[1]}'); do
  kill "$_pid" 2>/dev/null || true
done
```
This checks the executable file, not the cmdline. The running bash process has exe=`/usr/bin/bash`,
so only actual qecore-headless binaries match. No self-match possible.

**AT-SPI stale objects persist across consecutive headless runs.**
When gnome-session survives after the headless process is killed (which it does — killing
qecore-headless does not restart gnome-session), the AT-SPI accessible object tree retains
nodes from the previous test run. These cause `atspi_error: object does not exist` failures.

Mitigations applied:
1. In `before_scenario`, call `Main.overview.hide()` via Shell.Eval to force-close the Overview
   and evict its stale search-result nodes before each scenario.
2. In any step that traverses the AT-SPI tree (especially overview search), catch
   `"does not exist"` / `"atspi_error"` exceptions and retry (up to ~6×, 2 s delay).

**Headless sessions have no monitor output — test extension *state*, not visual actors.**
`qecore-headless` sessions run without a physical or virtual display. Extensions that depend on
a monitor to create visual actors (Dash to Dock dock actors, App Indicators panel statusArea
actors) will never create those actors. Assertions that check dock actor count or panel children
will always fail.

Correct assertion pattern for headless extension coverage:
```python
# Check extension is active (state == 1 == ENABLED), not that it rendered
ext_state = _shell_eval_json(
    "JSON.stringify((() => {"
    "const ext = Main.extensionManager.lookup('dash-to-dock@micxgx.gmail.com');"
    "return {installed: !!ext, state: ext ? ext.state : -1, hasStateObj: !!ext?.stateObj};"
    "})())"
)
assert ext_state["state"] == 1 and ext_state["hasStateObj"]
# Only check visual actors conditionally when we know a display is present
if ext_state.get("dockCount", 0) > 0:
    assert ext_state["visible"]
```

**ArgoCD does not auto-sync fast enough for rapid iteration.**
The default polling interval can leave the cluster 5+ minutes behind. After pushing a
WorkflowTemplate change, always force a refresh:
```bash
kubectl annotate application lab -n argocd argocd.argoproj.io/refresh=normal --overwrite
```
Then verify `kubectl get application lab -n argocd -o jsonpath='{.status.sync.revision}'`
matches your commit SHA before submitting a workflow.

**Fresh titan disks have no default browser configured.**
`xdg-settings get default-web-browser`, `xdg-mime query default x-scheme-handler/http`, and
GIO all return empty on a titan VM that has never had a user-interactive browser session.
Flatpak Firefox is not pre-deployed to `/var/lib/flatpak/exports/share/applications/` on the
base disk. Do not write scenarios that depend on a configured default browser unless the titan
disk setup explicitly configures one.

---

### 2026-05-26 — qecore caching traps and step duration as a quality signal

**`context.sandbox.shell` is cached by qecore — never use it as a liveness check.**
`qecore.TestSandbox` stores the gnome-shell AT-SPI node as a Python instance attribute after
the first successful `findApplication("gnome-shell")` call (done inside `before_scenario` via
`_wait_for_panel`). Every subsequent access to `context.sandbox.shell` is an O(1) Python
attribute lookup — no AT-SPI query, no bus round-trip, ~0 µs. A step that calls only
`context.sandbox.shell` and asserts `is not None` will trivially pass even if gnome-shell
has crashed, because the cached dead node is not None.

**Use `tree.root.application("gnome-shell")` for live checks:**
```python
# WRONG — cached, ~0s, does not detect crashed shell
shell = context.sandbox.shell
assert shell is not None

# CORRECT — live AT-SPI query, ~3ms, detects crashed/restarted shell
shell = tree.root.application("gnome-shell")
assert shell is not None
assert shell.children  # also verify the node has visible children
```

**`_enabled_extensions(context)` cache is also a trap.**
The pattern `if getattr(context, "_enabled_extensions", None): return ...` makes the first
`Extension "..." is enabled` step real and all subsequent ones instant no-ops. Remove the
cache and call `gnome-extensions list --enabled` on every step — each invocation costs ~130ms
and catches extension state changes mid-run.

**Behave JSON step duration `0.000s` is a quality signal — investigate every occurrence.**
When `behave --format json.pretty` reports duration `0.0` (rounded from sub-millisecond) on
a step that should be doing I/O (AT-SPI, subprocess, D-Bus), that step is almost certainly
hitting a cached object or a pure Python comparison. Audit these immediately:
- Zero-duration AT-SPI step → likely `context.sandbox.shell` cache
- Zero-duration assertion step → likely reading a Python variable set by the prior step
- Zero-duration `gnome-extensions` step → likely a context-level cache

After the 2026-05-26 fix (commit `70f51a8`), the smoke suite went from **31 zero-duration
active steps → 1** (the one remaining is `Last command output stripped` which reads a Python
string and is legitimately instantaneous).

**The upstream Red Hat GNOME test suite used throughout this repo is:**
`https://github.com/modehnal/GNOMETerminalAutomation` — by Michal Odehnal (Red Hat DesktopQE).
Step patterns, `environment.py` shape, and Wayland focus-window handling are all ported from
this repo. It is the canonical reference for how qecore+behave+dogtail suites are structured.
