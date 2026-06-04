"""
HTTPS service-exposure lane checks (#58).

Validates certificate, TLS version, HTTPS reachability, and host-routing
for the homelab-access fixture. Auth-gating is out of scope — see #61.
"""

from __future__ import annotations

import os
import re
import subprocess

from tests.service_catalog.shared.kube import RESULTS_DIR, write_artifact


NAMESPACE = os.environ["TEST_NAMESPACE"]
SERVICE_NAME = os.environ.get("TEST_SERVICE_NAME", "homelab-access")
HOSTNAME = "homelab-access.local"
SERVICE_FQDN = f"{SERVICE_NAME}.{NAMESPACE}.svc.cluster.local"
HTTPS_PORT = 8443

TIMEOUT_SECONDS = 30
MIN_TLS_VERSION = "TLSv1.2"
VALID_TLS_VERSIONS = {"TLSv1.2", "TLSv1.3"}


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)


class TestHTTPSExposure:
    """Evidence suite for the HTTPS service-exposure lane (#58)."""

    def test_cluster_dns_resolves(self):
        result = run("getent", "hosts", SERVICE_FQDN)
        write_artifact("https-dns.txt", result.stdout + result.stderr)
        assert result.returncode == 0, f"DNS resolution failed for {SERVICE_FQDN}: {result.stderr}"

    def test_tls_handshake_completes(self):
        result = run(
            "openssl", "s_client",
            "-connect", f"{SERVICE_FQDN}:{HTTPS_PORT}",
            "-servername", HOSTNAME,
            "-brief",
        )
        combined = result.stdout + result.stderr
        write_artifact("https-tls-handshake.txt", combined)
        assert result.returncode == 0, f"TLS handshake failed: {combined}"
        assert "Protocol version" in combined, f"No protocol version in handshake output: {combined}"

    def test_certificate_subject_matches(self):
        result = run(
            "openssl", "s_client",
            "-connect", f"{SERVICE_FQDN}:{HTTPS_PORT}",
            "-servername", HOSTNAME,
        )
        combined = result.stdout + result.stderr
        cert_result = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-ext", "subjectAltName"],
            input=result.stdout,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        evidence = cert_result.stdout + cert_result.stderr
        write_artifact("https-cert-subject.txt", evidence)
        assert HOSTNAME in evidence, (
            f"Certificate subject/SAN does not contain {HOSTNAME}: {evidence}"
        )

    def test_tls_version_is_acceptable(self):
        result = run(
            "openssl", "s_client",
            "-connect", f"{SERVICE_FQDN}:{HTTPS_PORT}",
            "-servername", HOSTNAME,
            "-brief",
        )
        combined = result.stdout + result.stderr
        write_artifact("https-tls-version.txt", combined)

        match = re.search(r"Protocol version\s*:\s*(TLSv[\d.]+)", combined)
        assert match, f"Could not parse TLS version from: {combined}"
        negotiated = match.group(1)
        assert negotiated in VALID_TLS_VERSIONS, (
            f"Negotiated {negotiated}, expected one of {VALID_TLS_VERSIONS}"
        )

    def test_https_reachability(self):
        result = run(
            "curl", "-sk",
            "--resolve", f"{HOSTNAME}:{HTTPS_PORT}:{SERVICE_FQDN}",
            f"https://{HOSTNAME}:{HTTPS_PORT}/healthz",
        )
        write_artifact("https-reachability.txt", result.stdout + result.stderr)
        assert result.returncode == 0, f"HTTPS request failed: {result.stderr}"
        assert result.stdout.strip() == "access-ok", (
            f"Expected 'access-ok', got: {result.stdout.strip()}"
        )

    def test_wrong_host_rejected(self):
        result = run(
            "curl", "-sk",
            "-o", "/dev/null", "-w", "%{http_code}",
            "-H", "Host: wrong-host.example.com",
            f"https://{SERVICE_FQDN}:{HTTPS_PORT}/healthz",
        )
        write_artifact("https-wrong-host.txt", result.stdout + result.stderr)
        assert result.returncode == 0, f"curl failed: {result.stderr}"
        assert result.stdout.strip() == "421", (
            f"Expected HTTP 421 for wrong host, got: {result.stdout.strip()}"
        )
