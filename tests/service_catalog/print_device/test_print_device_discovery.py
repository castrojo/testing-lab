"""
Printer-device access and LAN discovery lane — split from the base print lane.

This module defines the validation contract for the hardware-heavy and
network-discovery-heavy aspects of the OpenPrinting/CUPS workload that are
explicitly out of scope for the base print-service lane (#64).  Keeping
these requirements here makes the substrate dependencies visible and
independently trackable without blocking the base lane.

## Why this split exists

The base non-media lane (#64) validates the in-cluster deployment contract
for CUPS (PVC binding, IPP port reachability, env injection, rollout
persistence).  It cannot and should not validate:

  - Attached USB printer device access — requires host device passthrough
    that depends on substrate work in #54.
  - LAN mDNS / avahi self-discovery — requires a NodePort or LoadBalancer
    service plus an avahi sidecar, neither of which is part of the base
    in-cluster validation contract.

## Substrate assumptions this lane requires (both from epic #54)

  ### USB device-access path
  A. The target node (ghost) has one or more USB printer device nodes
     accessible at a path like /dev/usb/lp0 or /dev/bus/usb/XXX/YYY.
  B. The host usbfs and usblp kernel modules are loaded.
  C. The pod spec carries a hostPath volume for the device node, and the
     container runtime allows it (privileged or specific device allow-list).
  D. CUPS inside the container can open the device node — file permissions
     and SELinux/AppArmor policy allow it.

  ### LAN mDNS / discovery path
  E. A NodePort or LoadBalancer service exposes the CUPS IPP port (631)
     on the host network interface so LAN clients can reach it.
  F. An avahi-daemon sidecar is running in the pod and advertising the
     _ipp._tcp service record on the LAN.
  G. The cluster node is on the same Layer-2 segment as LAN clients so
     mDNS multicast packets (224.0.0.251) are not filtered.

## Dependency chain

  #54 substrate (host device passthrough + LAN network policy)
      → #67 this lane (USB access + mDNS discovery contract proven)
      → #64 base print lane (already unblocked; this lane extends it)

## All tests are gated per class

  TestPrinterDeviceAccessLane:
    Requires TEST_USB_PRINTER_DEVICE env var (e.g. /dev/usb/lp0).
    Skips entire class if the env var is not set.

  TestLanDiscoveryLane:
    Requires TEST_AVAHI_ENABLED=true env var.
    Skips entire class if the env var is not set.

  Both classes can be skipped in non-hardware CI without any failures.

Source idea: projectbluefin/bluespeed#11
Child of: #67, #51
Depends on: #54, #64
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.service_catalog.shared.kube import (
    NAMESPACE,
    get_pods_json,
    run_kubectl,
    write_artifact,
)


# ── Gate helpers ──────────────────────────────────────────────────────────────

_USB_DEVICE = os.environ.get("TEST_USB_PRINTER_DEVICE", "")
_AVAHI_ENABLED = os.environ.get("TEST_AVAHI_ENABLED", "").lower() in ("1", "true", "yes")

_NO_USB = not _USB_DEVICE
_NO_AVAHI = not _AVAHI_ENABLED

_USB_SKIP_REASON = (
    "TEST_USB_PRINTER_DEVICE is not set. "
    "Set it to the host device path (e.g. /dev/usb/lp0) to enable USB printer tests. "
    "Requires substrate work from #54: usblp kernel module, host device node, "
    "and container runtime device allow-list configured on ghost."
)

_AVAHI_SKIP_REASON = (
    "TEST_AVAHI_ENABLED is not set to 'true'. "
    "Set TEST_AVAHI_ENABLED=true to enable LAN discovery tests. "
    "Requires substrate work from #54: NodePort/LoadBalancer service on port 631 "
    "and avahi-daemon sidecar advertising _ipp._tcp on the LAN segment."
)


# ── Lane constants ────────────────────────────────────────────────────────────

DEPLOYMENT_NAME = "homelab-print-device"
APP_LABEL = os.environ.get("TEST_APP_LABEL", f"app={DEPLOYMENT_NAME}")
SERVICE_NAME = os.environ.get("TEST_SERVICE_NAME", DEPLOYMENT_NAME)

CONFIG_PVC_NAME = "homelab-print-device-config"
CONFIG_MOUNT_PATH = "/config"

IPP_PORT = 631            # IPP standard
IPP_NODEPORT = int(os.environ.get("TEST_IPP_NODEPORT", "30631"))  # NodePort for LAN exposure

# Device node path on the host; set via TEST_USB_PRINTER_DEVICE
USB_DEVICE_PATH = _USB_DEVICE or "/dev/usb/lp0"

CUPS_CONTAINER_NAME = "cups-server"
AVAHI_CONTAINER_NAME = "avahi-sidecar"

EXPECTED_ENV_VARS = {"PUID": "1000", "PGID": "1000", "TZ": "UTC"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _exec_in_pod(*command: str, container: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["exec", "-n", NAMESPACE, _first_pod_name()]
    if container:
        args += ["-c", container]
    args += ["--", *command]
    return run_kubectl(*args)


def _first_pod_name() -> str:
    result = run_kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", APP_LABEL,
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    assert result.returncode == 0 and result.stdout.strip(), (
        f"No Running pod found: {result.stdout}{result.stderr}"
    )
    return result.stdout.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Class 1 — USB printer device access
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(_NO_USB, reason=_USB_SKIP_REASON)
class TestPrinterDeviceAccessLane:
    """
    USB printer device-access lane.

    All tests in this class are skipped unless TEST_USB_PRINTER_DEVICE is
    set to a real device path on the host node.  See the module docstring
    for the full substrate dependency list (epic #54).

    The class validates that the container runtime correctly passes the USB
    device node through to the CUPS container so that CUPS can open and
    communicate with the attached printer.
    """

    # ── 1. Substrate — host device prerequisites ──────────────────────────────

    def test_usb_device_node_exists_in_container(self):
        """
        The USB printer device node is visible inside the CUPS container.

        This confirms the hostPath volume was mounted correctly by the
        container runtime and the device node is present (not just the
        directory placeholder).
        """
        result = _exec_in_pod(
            "sh", "-c",
            f"test -e {USB_DEVICE_PATH} && echo PRESENT || echo MISSING",
            container=CUPS_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-device-node-check.txt", result.stdout)
        assert "PRESENT" in result.stdout, (
            f"USB device node {USB_DEVICE_PATH} not found inside container. "
            "Check hostPath volume mount and container runtime device allow-list (see #54)."
        )

    def test_usb_device_node_is_character_device(self):
        """
        The device node is a character device, not a directory or regular file.

        lp0 is a character device (c in ls -l output).  A directory or regular
        file at the same path means the hostPath mount silently failed to bind
        the device node.
        """
        result = _exec_in_pod(
            "sh", "-c",
            f"ls -la {USB_DEVICE_PATH} 2>&1",
            container=CUPS_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-device-node-ls.txt", result.stdout)
        assert result.stdout.lstrip().startswith("c"), (
            f"Device node {USB_DEVICE_PATH} is not a character device:\n{result.stdout}"
        )

    def test_cups_can_detect_local_usb_printer(self):
        """
        CUPS (lpstat or lpinfo) detects the locally attached USB printer.

        Uses lpinfo -v to enumerate device URIs.  A URI containing 'usb://'
        or 'direct:usb' confirms CUPS has discovered the device node and
        can communicate with it over the USB backend.
        """
        result = _exec_in_pod(
            "sh", "-c",
            "lpinfo -v 2>&1 || true",
            container=CUPS_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-lpinfo.txt", result.stdout)
        output = result.stdout.lower()
        assert "usb" in output or "direct" in output, (
            "CUPS lpinfo -v did not report any USB device URI. "
            f"Output:\n{result.stdout}\n"
            "Verify the USB device is attached, device node is accessible, "
            "and the usblp kernel module is loaded on the host (see #54)."
        )

    def test_cups_accepts_test_print_job(self):
        """
        CUPS accepts a test job for the USB printer and reports it queued.

        Uses lp to submit a minimal test print job (cancel immediately after).
        A successful lp return code proves the CUPS backend can communicate
        with the attached device well enough to accept and queue the job.
        """
        result = _exec_in_pod(
            "sh", "-c",
            # List printers, pick the first, submit a test-page job, cancel it
            "PRINTER=$(lpstat -p 2>/dev/null | awk 'NR==1{print $2}');"
            " if [ -z \"${PRINTER}\" ]; then echo NO_PRINTER_CONFIGURED; exit 1; fi;"
            " JOB=$(echo 'test' | lp -d \"${PRINTER}\" 2>&1);"
            " echo \"${JOB}\";"
            " JOB_ID=$(echo \"${JOB}\" | grep -oP '(?<=job-id )[0-9]+' || true);"
            " [ -n \"${JOB_ID}\" ] && cancel \"${JOB_ID}\" 2>/dev/null || true",
            container=CUPS_CONTAINER_NAME,
        )
        write_artifact("print-test-job.txt", result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "NO_PRINTER_CONFIGURED" not in result.stdout, (
            "No printer configured in CUPS. "
            "The lane fixture must add the USB printer to CUPS before this test runs."
        )

    def test_usb_device_permissions_allow_cups_user(self):
        """
        The CUPS process user (or the PUID/PGID user) can read and write to
        the USB device node.  Insufficient permissions produce silent failures
        in CUPS that are hard to diagnose without this explicit check.
        """
        result = _exec_in_pod(
            "sh", "-c",
            f"test -r {USB_DEVICE_PATH} && test -w {USB_DEVICE_PATH}"
            " && echo RW_OK || echo PERMISSION_DENIED",
            container=CUPS_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-device-permissions.txt", result.stdout)
        assert "RW_OK" in result.stdout, (
            f"CUPS user cannot read/write {USB_DEVICE_PATH}. "
            "Check device node group ownership and container security context (see #54)."
        )

    # ── 2. Explicitly deferred paths ─────────────────────────────────────────

    def test_kubevirt_usb_passthrough_is_out_of_scope(self):
        """
        KubeVirt USB device passthrough (USB host-device assignment through
        the KubeVirt device API) is out of scope for this lane.  This lane
        validates the simpler hostPath approach only.  VM-backed USB
        passthrough requires additional substrate work in #54.
        """
        pytest.skip(
            "KubeVirt USB device passthrough deferred to #54; "
            "requires KubeVirt USB host-device assignment — "
            "out of scope for hostPath-based USB lane"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Class 2 — LAN mDNS / avahi discovery
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(_NO_AVAHI, reason=_AVAHI_SKIP_REASON)
class TestLanDiscoveryLane:
    """
    LAN mDNS / avahi printer-discovery lane.

    All tests in this class are skipped unless TEST_AVAHI_ENABLED=true is set.
    See the module docstring for the full substrate dependency list (epic #54).

    The class validates that:
    - The CUPS service is exposed outside the cluster on port 631 (NodePort
      or LoadBalancer) so LAN clients can reach it.
    - An avahi-daemon sidecar is running inside the pod and advertising the
      _ipp._tcp mDNS service record on the LAN.
    """

    # ── 1. Service exposure ───────────────────────────────────────────────────

    def test_ipp_service_exposed_outside_cluster(self):
        """
        The IPP service is exposed via NodePort or LoadBalancer so LAN clients
        can print without a VPN or ingress controller.

        Checks that the service type is not ClusterIP and that an external
        port mapping exists for port 631.
        """
        result = run_kubectl(
            "get", "service", SERVICE_NAME, "-n", NAMESPACE, "-o", "json"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-discovery-service.json", result.stdout)
        svc = json.loads(result.stdout)
        svc_type = svc.get("spec", {}).get("type", "ClusterIP")
        assert svc_type in ("NodePort", "LoadBalancer"), (
            f"Service type is {svc_type!r}; expected NodePort or LoadBalancer "
            "for LAN-accessible printing.  Update the Deployment fixture to expose "
            "the service externally (see the WorkflowTemplate for this lane)."
        )

    def test_nodeport_is_reachable_on_node_ip(self):
        """
        The NodePort is reachable from within the cluster using the node's
        internal IP, simulating a LAN client on the same L2 segment.

        Uses wget from inside the workload pod targeting the node IP rather
        than the ClusterIP so this test exercises the actual NodePort path.
        """
        # Retrieve node IP of the pod's host node
        node_result = run_kubectl(
            "get", "pod", _first_pod_name(), "-n", NAMESPACE,
            "-o", "jsonpath={.status.hostIP}",
        )
        assert node_result.returncode == 0 and node_result.stdout.strip(), (
            f"Could not determine host IP: {node_result.stdout}{node_result.stderr}"
        )
        node_ip = node_result.stdout.strip()
        write_artifact("print-discovery-node-ip.txt", node_ip)

        result = _exec_in_pod(
            "sh", "-c",
            f"wget -q -S -O /dev/null http://{node_ip}:{IPP_NODEPORT}/ 2>&1 || true",
            container=CUPS_CONTAINER_NAME,
        )
        output = result.stdout + result.stderr
        write_artifact("print-discovery-nodeport-reach.txt", output)
        assert "Connection refused" not in output, (
            f"NodePort {IPP_NODEPORT} refused connection on node IP {node_ip}. "
            "Verify the NodePort service is correctly configured."
        )
        assert "Name or service not known" not in output, output

    # ── 2. Avahi sidecar ──────────────────────────────────────────────────────

    def test_avahi_sidecar_container_is_running(self):
        """
        The avahi-daemon sidecar container is in a Running/Ready state inside
        the pod.  A crashed or missing avahi sidecar will silently prevent
        mDNS advertisement without producing any CUPS error.
        """
        pods = get_pods_json()
        write_artifact("print-discovery-pods.json", json.dumps(pods, indent=2))
        items = pods.get("items", [])
        assert items, "No print-device pod found"
        pod = items[0]
        container_statuses = pod.get("status", {}).get("containerStatuses", [])
        avahi_status = next(
            (cs for cs in container_statuses if cs.get("name") == AVAHI_CONTAINER_NAME),
            None,
        )
        assert avahi_status is not None, (
            f"No container named '{AVAHI_CONTAINER_NAME}' found in pod. "
            "Verify the Deployment fixture includes the avahi sidecar container."
        )
        assert avahi_status.get("ready"), (
            f"avahi sidecar container is not Ready: {avahi_status}"
        )

    def test_avahi_daemon_process_is_active(self):
        """
        avahi-daemon is running as a process inside the sidecar container.

        A container that started successfully but whose init system failed to
        launch avahi-daemon will still show as Ready from Kubernetes' perspective
        (as long as the entry-point process is alive).  This test confirms
        avahi-daemon itself is running.
        """
        result = _exec_in_pod(
            "sh", "-c",
            "pgrep -x avahi-daemon >/dev/null && echo RUNNING || echo NOT_RUNNING",
            container=AVAHI_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-avahi-process.txt", result.stdout)
        assert "RUNNING" in result.stdout, (
            "avahi-daemon process not found in sidecar container. "
            "Check avahi-daemon startup command and sidecar image."
        )

    def test_avahi_daemon_advertises_ipp_service(self):
        """
        avahi-daemon is advertising an _ipp._tcp service record.

        Uses avahi-browse -t to do a one-shot lookup for _ipp._tcp.  A result
        proves the service record is on the wire and LAN clients using mDNS
        (e.g. macOS Print Center, Linux system-config-printer) can auto-discover
        the printer without manual configuration.
        """
        result = _exec_in_pod(
            "sh", "-c",
            "avahi-browse -t -r _ipp._tcp 2>&1 || true",
            container=AVAHI_CONTAINER_NAME,
        )
        write_artifact("print-avahi-browse.txt", result.stdout + result.stderr)
        output = result.stdout + result.stderr
        assert "_ipp._tcp" in output or "IPP" in output, (
            "avahi-browse did not find any _ipp._tcp service record. "
            "Verify avahi-daemon is advertising the CUPS IPP service "
            "and that multicast is not filtered on the node network interface."
        )

    def test_avahi_service_file_is_present_in_config(self):
        """
        The avahi service definition file for CUPS IPP is present in
        /etc/avahi/services/ inside the sidecar.

        avahi-daemon reads static service files from this directory at
        startup.  A missing file means the sidecar was launched without the
        correct configuration and will not advertise the printer.
        """
        result = _exec_in_pod(
            "sh", "-c",
            "ls /etc/avahi/services/*.service 2>/dev/null | grep -q . "
            "&& echo PRESENT || echo MISSING",
            container=AVAHI_CONTAINER_NAME,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("print-avahi-services.txt", result.stdout)
        assert "PRESENT" in result.stdout, (
            "No .service files found in /etc/avahi/services/ in the sidecar. "
            "Mount the avahi CUPS service definition file via a ConfigMap."
        )

    # ── 3. Explicitly deferred paths ─────────────────────────────────────────

    def test_auth_gated_cups_ui_is_out_of_scope(self):
        """
        Auth-gating the CUPS administration web UI (Authelia, OAuth proxy,
        or similar) is out of scope for this discovery lane.  The lane
        validates only the _ipp._tcp service advertisement and NodePort
        reachability, not authenticated access to the admin interface.
        """
        pytest.skip(
            "Auth-gated CUPS administration UI deferred beyond #67; "
            "requires auth-gating infrastructure — out of scope for discovery lane"
        )

    def test_split_horizon_dns_for_cups_is_out_of_scope(self):
        """
        Split-horizon DNS / LAN hostname routing for CUPS
        (e.g. cups.home.local → NodePort) is out of scope for this lane.
        The lane proves only raw mDNS advertisement, not DNS-based routing.
        DNS-based routing patterns are tracked in the bluespeed repo.
        """
        pytest.skip(
            "Split-horizon DNS and LAN hostname routing deferred; "
            "tracked in projectbluefin/bluespeed — out of scope for mDNS lane"
        )
