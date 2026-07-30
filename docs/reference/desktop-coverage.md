# Bluefin desktop coverage slices

Issue [#14](https://github.com/projectbluefin/lab/issues/14) is a coverage
epic, not a request to put GNOME scenarios in this repository. The canonical
scenario source is
[`projectbluefin/testsuite`](https://github.com/projectbluefin/testsuite);
`lab` owns the runners, VM/container boundaries, and result publication.

## Current boundary

| Layer | Owner | What belongs there |
| --- | --- | --- |
| Scenario and step code | `projectbluefin/testsuite` | `.feature` files, dogtail/AT-SPI steps, Shell.Eval helpers |
| Continuous image lane | `lab` `run-container-tests` | qecore + behave inside the published bootc image |
| Real guest lane | `lab` `run-gnome-tests` | KubeVirt guest setup, SSH/AT-SPI execution, and teardown |
| Evidence and release data | `lab` | Per-suite results, workflow evidence, and dashboard publication |

Do not add Bluefin desktop scenarios under `lab/tests/`. Add or fix them in
`testsuite`, then use the lab workflow to validate the selected suite. Use
`run-gnome-tests` when a scenario requires a real guest; the image-poll path
currently uses `run-container-tests`.

## Actionable slices

These slices turn the epic into independently verifiable work. A slice is
complete when its scenario exists in `testsuite`, runs in the intended lane,
and has a stable result published by the lab.

| Slice | Existing coverage in `testsuite` | Next useful increment | Suite / lane |
| --- | --- | --- | --- |
| Shell navigation | Overview, overview search, Quick Settings, calendar, workspaces, notifications | Add or harden app-grid/search-to-launch assertions and keep Shell.Eval for GNOME Shell 50 top-bar controls | `smoke`; guest when AT-SPI fidelity is required |
| Extensions | Bluefin extension presence/enabled checks and shell-load crash checks | Keep the extension inventory synchronized with the image and prove every shipped extension loads without a shell crash | `smoke` |
| Core desktop apps | Files navigation/search/new-folder workflows; Settings navigation and panels | Fill remaining core workflow gaps, prioritizing usable navigation over launch-only checks | `smoke` |
| Ptyxis and Homebrew | Ptyxis launch/input scenarios and Homebrew command scenarios exist but are quarantined | Resolve the restart/CI prerequisites, then unquarantine terminal input and a real Homebrew install round-trip | `developer` |
| Bazaar | Install/source checks and UI launch plus Explore/Library navigation | Add search, detail-page, install-affordance, and resource-usage checks without turning a missing optional app into a suite-wide setup failure | `software` |
| Podman and Distrobox | Podman Desktop is launch/navigation smoke; CLI checks exist in developer/common coverage | Validate rootless execution, quadlets, systemd-user behavior, and in-scope `distrobox create`/`enter`; track runtime work with #48 | `developer`/`common`; guest if UI and runtime must be correlated |
| Desktop baseline | Desktop identity, portals, desktop entries, and GNOME accessibility checks | Use these as prerequisites for the workflow slices above rather than duplicating them in each feature | `common` + `smoke` |

## Handoff checklist

Before opening a lab-side change for a desktop slice:

1. Confirm the scenario belongs in `testsuite`, not `lab`.
2. Choose `smoke`, `developer`, `software`, or `common` based on the
   contract being proved; prefer a bootc/system assertion when a UI check would
   only duplicate it.
3. Decide whether the scenario needs `run-gnome-tests`'s real guest or can run
   in the continuous container lane.
4. Run the smallest matching lab workflow and retain its structured result or
   failure evidence.
5. Keep optional applications isolated with tags/skip behavior so their
   absence does not hide unrelated desktop regressions.

The epic remains open until the intended slices are implemented and observed
in the appropriate lab lane. This document records decomposition and
ownership; it does not claim that the remaining slices are complete.
