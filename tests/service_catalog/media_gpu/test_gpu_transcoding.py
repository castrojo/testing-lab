"""
GPU transcoding and hardware-passthrough lane — split from the base media lane.

This module defines the validation contract for hardware-accelerated media
transcoding.  It is explicitly separated from the base media-service lane
(issue #59) so that the base lane can stay implementable without GPU
hardware, while the hardware path remains visible and independently
trackable.

## Substrate assumptions this lane requires (all from epic #54)

  A. A GPU device-plugin DaemonSet (nvidia-device-plugin or amd-device-plugin)
     is running on the target node so Kubernetes can schedule pods with
     `resources.limits: nvidia.com/gpu: 1` (or amd.com/gpu).

  B. The host node exposes the correct device nodes:
     - NVIDIA: /dev/nvidia*, /dev/nvidiactl, /dev/nvidia-uvm
     - AMD/Intel: /dev/dri/renderD128 (or similar DRI node)

  C. The host kernel module stack is loaded:
     - NVIDIA: nvidia, nvidia-uvm
     - AMD: amdgpu
     - Intel: i915 / xe

  D. Container runtime is configured to pass GPU devices through
     (nvidia-container-toolkit or equivalent).  For KubeVirt VM-backed
     workloads this is a separate follow-up (#54).

## All tests in this module are gated

  If no node in the cluster reports allocatable GPU resources, the entire
  module is skipped with a clear reason string.  This means the GPU lane
  can be included in the standard CI run without failing on non-GPU
  clusters — it will only execute when hardware is present.

## Dependency chain

  #54 substrate (GPU device plugin + driver stack on ghost)
      → this lane (GPU transcoding contract proven)
      → base media lane (#59) (unblocked from GPU split)

## Out-of-scope for this lane (explicit follow-ups)

  - KubeVirt VM-backed GPU passthrough   → tracked in #54
  - Multi-GPU scheduling / MIG slicing   → deferred, no multi-GPU hardware
  - AMD GPU path (ROCm)                  → deferred; primary target is NVIDIA
  - Intel QSV / VAAPI path               → deferred until iGPU is validated

Source ideas: projectbluefin/bluespeed#4, projectbluefin/bluespeed#1
Child of: #63, #51, #54
Depends on: #54, #59
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from tests.service_catalog.shared.kube import (
    NAMESPACE,
    get_pods_json,
    run_kubectl,
    write_artifact,
)


# ── GPU availability gate ─────────────────────────────────────────────────────


def _gpu_allocatable_on_any_node() -> bool:
    """Return True if any cluster node reports a GPU resource as allocatable."""
    result = run_kubectl("get", "nodes", "-o", "json")
    if result.returncode != 0:
        return False
    try:
        nodes = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    for node in nodes.get("items", []):
        allocatable = node.get("status", {}).get("allocatable", {})
        for resource_key in allocatable:
            if "gpu" in resource_key.lower():
                return True
    return False


_NO_GPU = not _gpu_allocatable_on_any_node()

pytestmark = pytest.mark.skipif(
    _NO_GPU,
    reason=(
        "No GPU resource allocatable on any cluster node. "
        "This lane requires substrate work from #54 (GPU device-plugin + driver "
        "stack on ghost).  Add an nvidia-device-plugin DaemonSet and confirm "
        "nvidia.com/gpu appears in 'kubectl get nodes -o json' allocatable before "
        "running this suite."
    ),
)


# ── Lane constants ────────────────────────────────────────────────────────────

DEPLOYMENT_NAME = "homelab-media-gpu"
APP_LABEL = os.environ.get("TEST_APP_LABEL", f"app={DEPLOYMENT_NAME}")
SERVICE_NAME = os.environ.get("TEST_SERVICE_NAME", DEPLOYMENT_NAME)

CONFIG_PVC_NAME = "homelab-media-gpu-config"
CONFIG_MOUNT_PATH = "/config"
TRANSCODE_MOUNT_PATH = "/transcode"

MEDIA_GPU_PORT = 8096  # Consistent with base media lane

# GPU resource key; set TEST_GPU_RESOURCE_KEY to override for AMD/Intel
GPU_RESOURCE_KEY = os.environ.get("TEST_GPU_RESOURCE_KEY", "nvidia.com/gpu")

# Device plugin DaemonSet label to verify in test_device_plugin_daemonset_is_running
DEVICE_PLUGIN_LABEL = os.environ.get(
    "TEST_GPU_DEVICE_PLUGIN_LABEL", "app=nvidia-device-plugin"
)

EXPECTED_ENV_VARS = {"PUID": "1000", "PGID": "1000", "TZ": "UTC"}
EXPECTED_STORAGE_CLASS = "local-path"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _exec_in_pod(*command: str, pod_name: str | None = None) -> subprocess.CompletedProcess[str]:
    name = pod_name or _first_gpu_pod_name()
    return run_kubectl("exec", "-n", NAMESPACE, name, "--", *command)


def _first_gpu_pod_name() -> str:
    result = run_kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", APP_LABEL,
        "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}",
    )
    assert result.returncode == 0 and result.stdout.strip(), (
        f"No Running GPU pod found: {result.stdout}{result.stderr}"
    )
    return result.stdout.strip()


def _node_allocatable(resource_key: str) -> int:
    """Sum of allocatable <resource_key> across all nodes."""
    result = run_kubectl("get", "nodes", "-o", "json")
    if result.returncode != 0:
        return 0
    nodes = json.loads(result.stdout)
    total = 0
    for node in nodes.get("items", []):
        val = node.get("status", {}).get("allocatable", {}).get(resource_key, "0")
        try:
            total += int(val)
        except ValueError:
            pass
    return total


# ── Test class ────────────────────────────────────────────────────────────────


class TestMediaGpuTranscodingLane:
    """
    GPU transcoding and hardware-passthrough lane.

    All tests are skipped at module import time when no GPU resource is
    allocatable on any cluster node.  See the module docstring for the full
    substrate dependency chain (epic #54).
    """

    # ── 1. Substrate prerequisites ────────────────────────────────────────────

    def test_gpu_node_has_allocatable_capacity(self):
        """
        At least one node reports allocatable GPU resource (e.g. nvidia.com/gpu).

        This is the primary substrate gate: if this test fails, the device
        plugin is either not installed or not reporting capacity correctly.
        """
        capacity = _node_allocatable(GPU_RESOURCE_KEY)
        assert capacity >= 1, (
            f"Expected at least 1 allocatable {GPU_RESOURCE_KEY} but got {capacity}. "
            "Install the nvidia-device-plugin DaemonSet (see #54)."
        )
        write_artifact("gpu-node-allocatable.txt", str(capacity))

    def test_device_plugin_daemonset_is_running(self):
        """
        The GPU device-plugin DaemonSet has at least one Running pod on the
        GPU node.  Without the device plugin, resource limits on pods are
        silently rejected and no GPU is actually allocated.
        """
        result = run_kubectl(
            "get", "pods", "--all-namespaces",
            "-l", DEVICE_PLUGIN_LABEL,
            "-o", "json",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("gpu-device-plugin-pods.json", result.stdout)
        pods = json.loads(result.stdout)
        running = [
            p for p in pods.get("items", [])
            if p.get("status", {}).get("phase") == "Running"
        ]
        assert running, (
            f"No Running pods found with label '{DEVICE_PLUGIN_LABEL}'. "
            "Verify nvidia-device-plugin DaemonSet is installed and healthy (see #54)."
        )

    # ── 2. Deployment ─────────────────────────────────────────────────────────

    def test_gpu_deployment_becomes_ready(self):
        """
        Workload requesting GPU resource reaches availableReplicas >= 1.

        A pod that specifies 'resources.limits: nvidia.com/gpu: 1' will stay
        Pending indefinitely if the device plugin is absent or if no node has
        allocatable GPU capacity.  This test confirms Kubernetes has scheduled
        and started the pod with the resource limit satisfied.
        """
        result = run_kubectl(
            "get", "deployment", DEPLOYMENT_NAME, "-n", NAMESPACE, "-o", "json"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("gpu-deployment.json", result.stdout)
        data = json.loads(result.stdout)
        status = data.get("status", {})
        assert status.get("availableReplicas", 0) >= 1, json.dumps(status, indent=2)
        assert status.get("readyReplicas", 0) >= 1, json.dumps(status, indent=2)

    def test_gpu_pod_reaches_running_state(self):
        """Pod with GPU resource limit reaches Running state."""
        pods = get_pods_json()
        write_artifact("gpu-pods.json", json.dumps(pods, indent=2))
        items = pods.get("items", [])
        assert items, "No GPU media pod found"
        pod = items[0]
        phase = pod.get("status", {}).get("phase")
        assert phase == "Running", json.dumps(pod.get("status", {}), indent=2)
        container_statuses = pod.get("status", {}).get("containerStatuses", [])
        assert container_statuses, "No containerStatuses found"
        for cs in container_statuses:
            assert cs.get("ready"), f"Container {cs.get('name')} not ready: {cs}"

    def test_gpu_resource_limit_present_in_pod_spec(self):
        """
        Pod spec carries an explicit GPU resource limit so Kubernetes enforces
        device scheduling rather than silently running without GPU access.
        """
        pods = get_pods_json()
        items = pods.get("items", [])
        assert items, "No GPU media pod found"
        pod = items[0]
        containers = pod.get("spec", {}).get("containers", [])
        assert containers, "No containers in GPU pod spec"
        found_limit = False
        for container in containers:
            limits = container.get("resources", {}).get("limits", {})
            if GPU_RESOURCE_KEY in limits:
                found_limit = True
                break
        assert found_limit, (
            f"GPU resource limit '{GPU_RESOURCE_KEY}' not found in any container spec.\n"
            + json.dumps([c.get("resources") for c in containers], indent=2)
        )

    # ── 3. Device visibility ──────────────────────────────────────────────────

    def test_gpu_device_node_visible_in_container(self):
        """
        At least one NVIDIA device node (/dev/nvidia*) is visible inside the
        container.  This proves nvidia-container-toolkit mounted the device
        through rather than the pod receiving an empty device list.
        """
        result = _exec_in_pod("sh", "-c", "ls /dev/nvidia* 2>/dev/null || echo MISSING")
        assert result.returncode == 0, result.stdout + result.stderr
        write_artifact("gpu-dev-nodes.txt", result.stdout)
        assert "MISSING" not in result.stdout, (
            "No /dev/nvidia* device nodes found inside the container. "
            "Verify nvidia-container-toolkit is installed and the container "
            "runtime is configured for GPU passthrough (see #54)."
        )

    def test_nvidia_smi_reports_gpu_in_container(self):
        """
        nvidia-smi runs successfully inside the container and reports the GPU.

        This confirms the driver userspace library path is correctly mapped
        into the container, not just the device node.
        """
        result = _exec_in_pod("nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader")
        write_artifact("gpu-nvidia-smi.txt", result.stdout + result.stderr)
        assert result.returncode == 0, (
            "nvidia-smi failed inside container. "
            "Check that the NVIDIA driver is loaded on the host and "
            "nvidia-container-toolkit is configured (see #54).\n"
            + result.stderr
        )
        assert result.stdout.strip(), "nvidia-smi returned empty GPU list"

    # ── 4. Transcoding capability ─────────────────────────────────────────────

    def test_ffmpeg_lists_nvenc_encoder(self):
        """
        ffmpeg reports the nvenc hardware encoder as available.

        This is the pre-flight check before any actual transcoding: if nvenc
        is absent, the CUDA encoder stack is not functional regardless of
        device visibility.
        """
        result = _exec_in_pod("sh", "-c", "ffmpeg -encoders 2>&1 | grep nvenc || echo NVENC_MISSING")
        write_artifact("gpu-ffmpeg-encoders.txt", result.stdout + result.stderr)
        assert "NVENC_MISSING" not in result.stdout, (
            "nvenc encoder not listed by ffmpeg. "
            "Verify the container image includes ffmpeg with CUDA/NVENC support "
            "and the NVIDIA driver version is compatible."
        )

    def test_ffmpeg_hardware_transcode_completes(self):
        """
        ffmpeg performs a minimal hardware-accelerated transcode using NVENC.

        Generates a 1-second synthetic video stream in software and re-encodes
        it using the NVENC hardware encoder.  A successful exit code confirms
        the full encode pipeline works end-to-end: device access, driver
        compatibility, and encoder initialization.

        This is the primary transcoding contract test.
        """
        result = _exec_in_pod(
            "sh", "-c",
            "ffmpeg -hide_banner -loglevel error"
            " -f lavfi -i testsrc=duration=1:size=1280x720:rate=30"
            " -c:v h264_nvenc -preset fast -f null - 2>&1",
        )
        write_artifact("gpu-transcode-output.txt", result.stdout + result.stderr)
        assert result.returncode == 0, (
            "Hardware transcode failed. "
            "Check that nvidia-smi and nvenc are working inside the container.\n"
            + result.stdout + result.stderr
        )

    # ── 5. Resource lifecycle ─────────────────────────────────────────────────

    def test_gpu_resource_is_released_after_pod_deletion(self):
        """
        After the GPU pod is deleted, the GPU resource appears allocatable on
        the node again within 60 s.

        This test validates that the device plugin correctly tracks resource
        release, which is a prerequisite for the GPU lane to be idempotent
        (re-runnable without stranding GPU capacity).
        """
        before = _node_allocatable(GPU_RESOURCE_KEY)
        write_artifact("gpu-allocatable-before-delete.txt", str(before))

        pod_name = _first_gpu_pod_name()
        delete = run_kubectl("delete", "pod", pod_name, "-n", NAMESPACE, "--wait=false")
        assert delete.returncode == 0, delete.stdout + delete.stderr
        write_artifact("gpu-pod-delete.txt", delete.stdout + delete.stderr)

        # Wait for the pod to disappear and GPU capacity to restore
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            after = _node_allocatable(GPU_RESOURCE_KEY)
            if after >= before:
                write_artifact("gpu-allocatable-after-delete.txt", str(after))
                return
            time.sleep(5)

        after = _node_allocatable(GPU_RESOURCE_KEY)
        write_artifact("gpu-allocatable-after-delete.txt", str(after))
        assert after >= before, (
            f"GPU allocatable capacity did not recover: was {before}, now {after} "
            "after pod deletion.  Check device-plugin health."
        )

    # ── 6. Explicitly deferred paths ─────────────────────────────────────────

    def test_kubevirt_vm_gpu_passthrough_is_out_of_scope_for_this_lane(self):
        """
        KubeVirt VM-backed GPU passthrough (VFIO/IOMMU path) is out of scope
        for the base GPU transcoding lane.  That path requires additional
        substrate work tracked in #54 (homelab substrate epic).  This lane
        validates k8s-native container GPU access only (device-plugin +
        nvidia-container-toolkit path).
        """
        pytest.skip(
            "KubeVirt VM-backed GPU passthrough deferred to #54; "
            "requires VFIO/IOMMU substrate configuration — "
            "out of scope for container-native GPU lane"
        )

    def test_multi_gpu_and_mig_slicing_is_out_of_scope_for_this_lane(self):
        """
        Multi-GPU scheduling and MIG (Multi-Instance GPU) slicing are out of
        scope for this lane.  The lab has at most one GPU; MIG requires Ampere
        or later.  Tracked as a separate follow-up if multi-GPU hardware
        becomes available.
        """
        pytest.skip(
            "Multi-GPU scheduling and MIG slicing deferred; "
            "no multi-GPU hardware in current lab — out of scope for base GPU lane"
        )

    def test_amd_and_intel_gpu_paths_are_out_of_scope_for_this_lane(self):
        """
        AMD ROCm and Intel QSV/VAAPI transcoding paths are out of scope for
        this lane.  Primary target is NVIDIA CUDA/NVENC.  AMD and Intel iGPU
        paths are deferred until the NVIDIA path is validated and hardware is
        available.
        """
        pytest.skip(
            "AMD ROCm and Intel QSV/VAAPI transcoding deferred; "
            "primary target is NVIDIA CUDA/NVENC — deferred until NVIDIA path is validated"
        )
