#!/usr/bin/env python3
"""Compute the per-lane Release Verdict and write docs/data/release-verdict.json.

Verdict definition (docs/adr/0002-release-verdict-definition.md): the latest
published digest for a lane is GOOD iff:
  1. build   - the publishing workflow run concluded successfully
               (factory-stats.json image_builds, public GitHub Actions data)
  2. qa      - the lab QA pipeline passed for the lane and its evidence is not
               older than the digest publish signal (tests-matrix.json)
  3. signature - `cosign verify` (keyless, GitHub Actions OIDC) passes for the
               digest

Security regression is displayed alongside, never gating (Phase 3 wires the
publisher-side CVE summary; until then that input is explicitly unavailable).

Every row follows the docs/data/page-contracts.md row contract. Missing
inputs yield state "unavailable" with a state_reason - never invented values.

cosign verification is cached by digest in the previous output file, so the
5-minute refresh cadence only re-verifies when a lane's digest changes.
Also appends one NDJSON row per lane per refresh to
docs/data/history/release-verdict.ndjson (only when the digest or verdict
changed), capped at HISTORY_DAYS.
"""

import datetime
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

OUT_PATH = Path("docs/data/release-verdict.json")
HISTORY_PATH = Path("docs/data/history/release-verdict.ndjson")
HISTORY_DAYS = 365

CERT_IDENTITY_RE = "^https://github.com/projectbluefin/"
CERT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

# lane id -> (ghcr repository, tag, factory-stats image_builds key, tests-matrix variant/branch)
LANES = {
    "bluefin-stable": ("projectbluefin/bluefin", "stable", "bluefin-stable", ("bluefin", "stable")),
    "bluefin-testing": ("projectbluefin/bluefin", "testing", "bluefin-testing", ("bluefin", "testing")),
    "bluefin-lts-stable": ("projectbluefin/bluefin-lts", "stable", "bluefin-lts-stable", ("bluefin-lts", "stable")),
    "bluefin-lts-testing": ("projectbluefin/bluefin-lts", "testing", "bluefin-lts-testing", ("bluefin-lts", "testing")),
    "dakota-testing": ("projectbluefin/dakota", "testing", "dakota", ("dakota", "testing")),
}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def ghcr_digest(repository, tag):
    """Resolve a public GHCR tag to its manifest digest anonymously."""
    try:
        tok_req = urllib.request.Request(
            f"https://ghcr.io/token?scope=repository:{repository}:pull"
        )
        with urllib.request.urlopen(tok_req, timeout=15) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{tag}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ", ".join(
                    [
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                    ]
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Docker-Content-Digest")
    except Exception:
        return None


def cosign_verify(image_ref):
    """Keyless cosign verify. Returns (ok, detail)."""
    if shutil.which("cosign") is None:
        return None, "cosign binary not available in refresh environment"
    try:
        proc = subprocess.run(
            [
                "cosign", "verify", image_ref,
                "--certificate-identity-regexp", CERT_IDENTITY_RE,
                "--certificate-oidc-issuer", CERT_OIDC_ISSUER,
                "-o", "json",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            return True, "keyless cosign verify passed (GitHub Actions OIDC identity)"
        return False, (proc.stderr.strip().splitlines() or ["cosign verify failed"])[-1][:300]
    except Exception as exc:
        return None, f"cosign verify errored: {exc}"[:300]


def latest_build(image_builds, key):
    runs = image_builds.get(key) or []
    dated = [r for r in runs if r.get("started_at")]
    if not dated:
        return None
    return max(dated, key=lambda r: r["started_at"])


def _qa_substatus(rows, build_finished_at, current_digest, label):
    """Compute a pass/fail/pending/unavailable status for a subset of QA rows."""
    if not rows:
        return {
            "status": "unavailable",
            "reason": f"no {label} QA suites tracked for this lane",
            "rows": rows,
        }
    ran = [r for r in rows if r.get("last_run")]
    if not ran:
        return {
            "status": "unavailable",
            "reason": f"no {label} lab QA run has published results for this lane yet",
            "rows": rows,
        }

    latest = max(r["last_run"] for r in ran)
    failed_suites = []
    pending_suites = []

    for r in rows:
        if not r.get("last_run"):
            pending_suites.append(r.get("suite"))
            continue

        suite_digest = r.get("digest")
        if suite_digest and current_digest:
            is_match = suite_digest == current_digest
        else:
            is_match = bool(build_finished_at and r["last_run"] >= build_finished_at)

        if not is_match:
            pending_suites.append(r.get("suite"))
        elif r.get("result_status") == "failed":
            failed_suites.append(r.get("suite"))

    if pending_suites:
        return {
            "status": "pending",
            "reason": (
                f"{label} QA evidence pending for suite(s): "
                + ", ".join(pending_suites)
                + f" against digest {current_digest[:12] if current_digest else 'none'}"
            ),
            "rows": rows,
            "last_run": latest,
        }
    if failed_suites:
        return {
            "status": "failed",
            "reason": f"{label} QA suite(s) failing: " + ", ".join(failed_suites),
            "rows": rows,
            "last_run": latest,
        }
    return {"status": "passed", "reason": None, "rows": rows, "last_run": latest}


def qa_input(tests_rows, variant, branch, build_finished_at, current_digest):
    """QA gate from tests-matrix rows for the lane.

    Gating considers only suites whose role is "gate" (smoke, system, flatcar).
    Informational suites (developer, software, common) are reported separately
    and never block the lane verdict (ADR 0002 gating amendment).
    """
    rows = [r for r in tests_rows if r.get("variant") == variant and r.get("branch") == branch]
    if not rows:
        return {
            "status": "unavailable",
            "reason": "no QA suites tracked for this lane",
            "rows": [],
            "informational": {"status": "unavailable", "reason": "no QA suites tracked for this lane", "rows": []},
        }

    gate_rows = [r for r in rows if r.get("role") == "gate"]
    info_rows = [r for r in rows if r.get("role") == "info"]

    gate = _qa_substatus(gate_rows, build_finished_at, current_digest, "gate")
    info = _qa_substatus(info_rows, build_finished_at, current_digest, "informational")

    return {
        "status": gate["status"],
        "reason": gate["reason"],
        "rows": gate["rows"],
        "last_run": gate.get("last_run"),
        "informational": info,
    }


def append_history(new_rows, now):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except Exception:
                        continue
    last_by_lane = {}
    for row in existing:
        last_by_lane[row.get("lane")] = row
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=HISTORY_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    appended = 0
    for row in new_rows:
        prev = last_by_lane.get(row["lane"])
        if prev and prev.get("digest") == row["digest"] and prev.get("verdict") == row["verdict"]:
            continue
        existing.append(row)
        appended += 1
    existing = [r for r in existing if (r.get("recorded_at") or now) >= cutoff]
    with open(HISTORY_PATH, "w") as f:
        for row in existing:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return appended


def main():
    now = now_iso()
    stats = load_json("docs/data/factory-stats.json") or {}
    tests_matrix = load_json("docs/data/tests-matrix.json") or {}
    prev = load_json(OUT_PATH) or {}
    prev_rows = {r.get("id"): r for r in prev.get("rows", [])}

    image_builds = stats.get("image_builds") or {}
    tests_rows = tests_matrix.get("rows") or []

    rows = []
    history_rows = []
    for lane_id, (repository, tag, build_key, (variant, branch)) in LANES.items():
        image_ref = f"ghcr.io/{repository}:{tag}"
        digest = ghcr_digest(repository, tag)

        # -- input 1: build ------------------------------------------------
        build = latest_build(image_builds, build_key)
        if build:
            build_input = {
                "status": "passed" if build.get("overall") == "passed" else "failed",
                "reason": None if build.get("overall") == "passed" else f"latest publishing run concluded {build.get('overall')}",
                "run_url": build.get("run_url"),
                "finished_at": build.get("finished_at"),
            }
        else:
            build_input = {
                "status": "unavailable",
                "reason": "no publishing workflow runs tracked in factory-stats.json for this lane",
                "run_url": None, "finished_at": None,
            }

        # -- input 2: qa ----------------------------------------------------
        qa = qa_input(tests_rows, variant, branch, build_input.get("finished_at"), digest)

        # -- input 3: signature (cached by digest) ---------------------------
        prev_row = prev_rows.get(lane_id) or {}
        if digest is None:
            sig_status, sig_reason = "unavailable", "could not resolve lane digest from GHCR"
        elif (
            prev_row.get("digest") == digest
            and prev_row.get("signature", {}).get("status") in ("passed", "failed")
        ):
            sig_status = prev_row["signature"]["status"]
            sig_reason = prev_row["signature"].get("reason")
        else:
            ok, detail = cosign_verify(f"ghcr.io/{repository}@{digest}")
            if ok is True:
                sig_status, sig_reason = "passed", None
            elif ok is False:
                sig_status, sig_reason = "failed", detail
            else:
                sig_status, sig_reason = "unavailable", detail

        # -- verdict ---------------------------------------------------------
        inputs = (build_input["status"], qa["status"], sig_status)
        if all(s == "passed" for s in inputs):
            verdict = "good"
        elif "failed" in inputs:
            verdict = "bad"
        else:
            verdict = "pending"

        state = "available" if digest else "unavailable"
        row = {
            "id": lane_id,
            "lane": lane_id,
            "variant": variant,
            "branch": branch,
            "image_ref": image_ref,
            "digest": digest,
            "verdict": verdict,
            "build": {"status": build_input["status"], "reason": build_input["reason"], "run_url": build_input["run_url"], "finished_at": build_input["finished_at"]},
            "qa": {
                "status": qa["status"],
                "reason": qa["reason"],
                "last_run": qa.get("last_run"),
                "lab_sourced": True,
                "informational": qa.get("informational", {"status": "unavailable", "reason": "informational suite status unavailable", "rows": []}),
            },
            "signature": {
                "status": sig_status,
                "reason": sig_reason,
                "method": "cosign keyless (GitHub Actions OIDC)",
                "source_url": image_ref,
                "collected_at": now,
                "derivation": "Direct cosign verification of this digest; a passing signature does not prove SBOM or provenance coverage.",
            },
            "sbom": {
                "status": "unavailable",
                "reason": "release verdict has no published SBOM artifact manifest",
                "source_url": None,
                "collected_at": now,
                "derivation": "No SBOM artifact evidence is available; publisher capability flags are not evidence.",
            },
            "provenance": {
                "status": "unavailable",
                "reason": "release verdict has no published provenance attestation artifact",
                "source_url": None,
                "collected_at": now,
                "derivation": "Signatures do not prove a provenance attestation.",
            },
            "security_regression": {
                "status": "unavailable",
                "reason": "publisher-side CVE summary export not yet wired (phase 3); displayed alongside, never gating (ADR 0002)",
            },
            "source_url": f"https://github.com/{repository.split('/')[0]}/{repository.split('/')[1]}",
            "collected_at": now,
            "derivation": (
                "ADR 0002: build (factory-stats image_builds) + lab QA gating suites only "
                "(smoke/system gate; flatcar gate for the flatcar lane; developer/software/common informational) "
                "+ cosign keyless verify per digest; QA evidence older than the current build renders pending."
            ),
            "state": state,
            "state_reason": None if digest else "lane digest unresolvable from GHCR",
        }
        rows.append(row)
        history_rows.append({
            "recorded_at": now,
            "lane": lane_id,
            "digest": digest,
            "verdict": verdict,
            "build": build_input["status"],
            "qa": qa["status"],
            "signature": sig_status,
            "signature_evidence": {
                "status": sig_status,
                "reason": sig_reason,
                "source_url": image_ref,
                "collected_at": now,
                "derivation": "Direct cosign verification of this digest; a passing signature does not prove SBOM or provenance coverage.",
            },
            "sbom": row["sbom"],
            "provenance": row["provenance"],
        })

    appended = append_history(history_rows, now)

    good = sum(1 for r in rows if r["verdict"] == "good")
    doc = {
        "schema_version": 1,
        "_meta": {
            "page": "index",
            "description": "Per-lane release verdict (ADR 0002): build + lab QA + cosign signature per latest published digest",
            "generated_at": now,
            "starter_artifact": False,
            "status": "live",
        },
        "summary_metrics": [
            {
                "id": "lanes_good",
                "label": "Lanes with a good latest release",
                "value": good,
                "total": len(rows),
                "source_url": "https://github.com/projectbluefin/lab/blob/main/docs/adr/0002-release-verdict-definition.md",
                "collected_at": now,
                "derivation": "count of lanes whose verdict is good under ADR 0002",
            }
        ],
        "rows": rows,
    }
    OUT_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"release-verdict: {good}/{len(rows)} lanes good; {appended} history rows appended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
