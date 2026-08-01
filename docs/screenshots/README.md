# screenshots

Desktop screenshots captured during e2e test runs.

Container-based suites publish screenshots to GHCR, and the
`update-test-results` GitHub Action pulls those OCI artifacts:
`ghcr.io/projectbluefin/testsuite/desktop-screenshot:<slug>-<suite>-latest`

KDE/KubeVirt screenshots are published to the lab-local Zot registry instead;
they are retained with the workflow's hostPath evidence and are not pulled by
this GitHub Pages refresh job. KubeVirt does not expose a QEMU monitor, so
guest-side WebDriver/KPipeWire screenshots are the supported KDE capture path.

Files are named `<slug>-<suite>-latest.png`, e.g.:
- `bluefin-testing-smoke-latest.png`
- `bluefin-lts-testing-developer-latest.png`
- `dakota-testing-system-latest.png`
