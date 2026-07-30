#!/usr/bin/env python3
"""Collect BuildStream / remote-execution cache usage across both cache
drives (ghost, exo-0) and write docs/data/bst-cache.json.

Two independent cache backends currently exist:
  - bazel-remote ("bst-artifact-server", argo/bst-artifact-server), pinned to
    ghost only, backing /var/mnt/ghost-data/bst-artifact-cache. Exposes a
    live JSON /status endpoint with real byte counts.
  - Buildbarn's 2-shard CAS/AC (buildbarn/storage-0, storage-1), scheduled
    across ghost + exo-0 via podAntiAffinity. Each shard uses fixed-size
    block-device-backed storage (always fully preallocated on disk), so
    there is no direct "bytes used" gauge; usage is derived from the
    allocations_total/releases_total counters times the known block size
    (see manifests/buildbarn-config.yaml: 20GiB/35 blocks for CAS,
    20MiB/33 blocks for AC).

All cluster reads go through the Kubernetes API server's service/pod proxy
subresource (`kubectl get --raw .../proxy/...`) rather than requiring direct
network reachability to ClusterIPs or new NodePorts/manifests, matching the
existing kubectl-based reachability pattern in refresh_factory_stats.py.
Any read that fails (cluster unreachable, pod not scheduled) yields a row
with state: "unavailable" and an explicit state_reason -- never invented
values, per docs/data/page-contracts.md.
"""
import json
import subprocess
import datetime
import re
from pathlib import Path

OUT_PATH = "docs/data/bst-cache.json"
HISTORY_PATH = "docs/data/history/cache-heat.ndjson"
HISTORY_RETENTION_DAYS = 180

# Buildbarn block-device-backed storage constants (manifests/buildbarn-config.yaml)
CAS_CAPACITY_BYTES = 20 * 1024 * 1024 * 1024
CAS_BLOCKS = 8 + 24 + 3
AC_CAPACITY_BYTES = 20 * 1024 * 1024
AC_BLOCKS = 8 + 24 + 1


def run_raw(path):
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "--raw", path],
            text=True, stderr=subprocess.DEVNULL, timeout=12,
        )
        return out
    except Exception:
        return None


def run_json_raw(path):
    out = run_raw(path)
    if out is None:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def parse_metric_value(metrics_text, metric_name, labels):
    """Pull a single Prometheus metric value matching all given labels."""
    if not metrics_text:
        return None
    for line in metrics_text.splitlines():
        if not line.startswith(metric_name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            try:
                return float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
    return None


def parse_metric_samples(metrics_text, metric_name):
    """Return (labels, value) samples for one Prometheus metric family."""
    if not metrics_text:
        return []
    samples = []
    pattern = re.compile(r"^" + re.escape(metric_name) + r"(?:\{([^}]*)\})?\s+([-+0-9.eE]+)")
    for line in metrics_text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        labels = dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"])*)"', match.group(1) or ""))
        try:
            samples.append((labels, float(match.group(2))))
        except ValueError:
            continue
    return samples


def _metric_total(metrics_text, metric_name, storage_type, operation, suffix):
    total = 0.0
    found = False
    for labels, value in parse_metric_samples(metrics_text, metric_name + suffix):
        if labels.get("storage_type") == storage_type and labels.get("operation") == operation:
            total += value
            found = True
    return total if found else None


def collect_cache_heat(
    metrics_text,
    pod,
    storage_type,
    logical_fill_bytes=None,
    logical_capacity_bytes=None,
    logical_fill_state=None,
    logical_fill_reason=None,
):
    """Build one honest cache-heat record from BuildBarn's exposed metrics.

    BuildBarn exposes blob access counts, bytes, timings, and gRPC status
    counts. ``OK`` and ``NotFound`` Get results are the cache hit/miss signal.
    """
    size_metric = "buildbarn_blobstore_blob_access_operations_blob_size_bytes"
    duration_metric = "buildbarn_blobstore_blob_access_operations_duration_seconds"
    requests = _metric_total(metrics_text, size_metric, storage_type, "Get", "_count")
    if requests is None:
        requests = _metric_total(metrics_text, duration_metric, storage_type, "Get", "_count")
    bytes_total = _metric_total(metrics_text, size_metric, storage_type, "Get", "_sum")
    duration = None
    for labels, value in parse_metric_samples(metrics_text, duration_metric + "_sum"):
        if (
            labels.get("storage_type") == storage_type
            and labels.get("operation") == "Get"
        ):
            duration = (duration or 0.0) + value
    hit_count = None
    miss_count = 0.0
    miss_seen = False
    status_total = 0.0
    hit_seen = False
    for labels, value in parse_metric_samples(metrics_text, duration_metric + "_count"):
        if labels.get("storage_type") == storage_type and labels.get("operation") == "Get":
            status_total += value
            if labels.get("grpc_code") == "OK":
                hit_count = (hit_count or 0.0) + value
                hit_seen = True
            elif labels.get("grpc_code") == "NotFound":
                miss_count += value
                miss_seen = True
    if not miss_seen and hit_seen and status_total == hit_count:
        miss_count = 0.0
    elif not miss_seen:
        miss_count = None
    effectiveness = None
    if hit_count is not None and miss_count is not None and hit_count + miss_count:
        effectiveness = round(hit_count / (hit_count + miss_count), 6)
        requests = hit_count + miss_count
    metrics_available = hit_count is not None and miss_count is not None
    latency_ms = None
    if duration is not None and status_total:
        latency_ms = round(duration / status_total * 1000, 6)
    logical_fill_percent = None
    if (
        logical_fill_bytes is not None
        and logical_capacity_bytes is not None
        and logical_capacity_bytes > 0
    ):
        logical_fill_percent = round(
            100 * logical_fill_bytes / logical_capacity_bytes, 6
        )
    return {
        "cache_backend": "buildbarn",
        "pod": pod,
        "storage_type": storage_type,
        "hit_count": int(hit_count) if hit_count is not None and hit_count.is_integer() else hit_count,
        "miss_count": int(miss_count) if miss_count is not None and miss_count.is_integer() else miss_count,
        "effectiveness": effectiveness,
        "requests": int(requests) if requests is not None and requests.is_integer() else requests,
        "bytes": int(bytes_total) if bytes_total is not None and bytes_total.is_integer() else bytes_total,
        "duration_seconds": duration,
        "latency_ms": latency_ms,
        "logical_fill_bytes": logical_fill_bytes,
        "logical_capacity_bytes": logical_capacity_bytes,
        "logical_fill_percent": logical_fill_percent,
        "logical_fill_state": logical_fill_state or (
            "available" if logical_fill_bytes is not None else "unavailable"
        ),
        "logical_fill_reason": logical_fill_reason,
        "state": "available" if metrics_available else "unavailable",
        "state_reason": (
            None
            if metrics_available
            else (
                "BuildBarn Get gRPC status counters unavailable; timing and byte "
                "fields are included when available"
                if requests is not None or bytes_total is not None or duration is not None
                else "BuildBarn blob access metrics unavailable"
            )
        ),
        "source_url": f"kubectl proxy: pods/{pod}:9980/proxy/metrics (namespace buildbarn)",
        "derivation": (
            "BuildBarn blob_access_operations Get histogram counters/sums; "
            "OK is a hit and NotFound is a miss; latency is duration_sum / "
            "status_count; logical fill is supplied from allocator counters"
        ),
    }


def persist_cache_heat(records, recorded_at):
    """Append cache heat records, deduplicating a repeated collection."""
    path = Path(HISTORY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("recorded_at") != recorded_at:
                existing.append(record)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=HISTORY_RETENTION_DAYS
    )
    retained = []
    for record in existing:
        try:
            when = datetime.datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when >= cutoff:
            retained.append(record)
    with path.open("w") as stream:
        for record in retained + [
            {"schema_version": "1.0", "recorded_at": recorded_at, **record}
            for record in records
        ]:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def node_for_pod(namespace, pod):
    doc = run_json_raw(f"/api/v1/namespaces/{namespace}/pods/{pod}")
    if not doc:
        return None
    return doc.get("spec", {}).get("nodeName")


def collect_usb4_telemetry(collected_at):
    """Collect only the USB-4 link observations backed by node annotations."""
    links = {}
    observed_at = {}
    latency_ms = {}
    for node in ("ghost", "exo-0"):
        doc = run_json_raw(f"/api/v1/nodes/{node}")
        annotations = (doc or {}).get("metadata", {}).get("annotations", {})
        link = annotations.get("lab.projectbluefin.io/usb4-link")
        if link in {"up", "down"}:
            links[node] = link
        observed = annotations.get("lab.projectbluefin.io/usb4-link-observed-at")
        if observed:
            observed_at[node] = observed
        latency = annotations.get("lab.projectbluefin.io/usb4-latency-ms")
        if latency not in (None, "", "unavailable"):
            try:
                latency_ms[node] = float(latency)
            except ValueError:
                pass

    if links.get("ghost") == "up" and links.get("exo-0") == "up":
        status = "up"
    elif "down" in (links.get("ghost"), links.get("exo-0")):
        status = "down"
    else:
        status = "unavailable"

    return {
        "status": status,
        "bandwidth_gbps": None,
        "latency_ms": latency_ms.get("ghost")
        if latency_ms.get("ghost") is not None
        else latency_ms.get("exo-0"),
        "ghost_link": links.get("ghost"),
        "ghost_observed_at": observed_at.get("ghost"),
        "exo0_link": links.get("exo-0"),
        "exo0_observed_at": observed_at.get("exo-0"),
        "cold_build_duration_min": None,
        "warm_build_duration_min": None,
        "speedup_ratio": None,
        "rechunk_duration_sec": None,
        "work_distribution": {"ghost": None, "exo-0": None},
        "source_url": "kubectl proxy: nodes/ghost and nodes/exo-0",
        "collected_at": collected_at,
        "derivation": (
            "USB-4 link status and probe latency are read from node annotations; bandwidth, "
            "build durations, rechunk time, and work distribution require direct "
            "measurements not exposed by this collector"
        ),
        "state": "unavailable",
        "state_reason": (
            "USB-4 link annotations do not provide measured distributed-build "
            "telemetry"
        ),
    }


def main():
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    heat_records = []

    # -- USB-4 Link & Distributed Build Telemetry ----------------------
    usb4_telemetry = collect_usb4_telemetry(now)

    # -- bazel-remote (ghost only) --------------------------------------
    # bst-artifact-server is nodeSelector-pinned to ghost (manifests/bst-artifact-server.yaml)
    status = run_json_raw("/api/v1/namespaces/argo/services/bst-artifact-server:8080/proxy/status")
    if status:
        used = status.get("CurrSize")
        capacity = status.get("MaxSize")
        rows.append({
            "id": "bazel-remote-ghost",
            "node": "ghost",
            "drive": "/var/mnt/ghost-data",
            "cache_backend": "bazel-remote",
            "storage_type": "cas",
            "used_bytes": used,
            "capacity_bytes": capacity,
            "percent": round(100 * used / capacity, 3) if used is not None and capacity else None,
            "source_url": "http://192.168.1.102:32746 (kubectl proxy: services/bst-artifact-server:8080/proxy/status)",
            "collected_at": now,
            "derivation": "live /status endpoint (CurrSize / MaxSize) via kubectl API-server service proxy",
            "state": "available",
            "state_reason": None,
        })
        heat_records.append({
            "cache_backend": "bazel-remote",
            "pod": None,
            "storage_type": "cas",
            "hit_count": None,
            "miss_count": None,
            "effectiveness": None,
            "requests": None,
            "bytes": None,
            "duration_seconds": None,
            "latency_ms": None,
            "logical_fill_bytes": used if status else None,
            "logical_capacity_bytes": capacity if status else None,
            "logical_fill_percent": (
                round(100 * used / capacity, 6)
                if status and used is not None and capacity
                else None
            ),
            "logical_fill_state": "available" if status and used is not None else "unavailable",
            "logical_fill_reason": (
                None
                if status and used is not None
                else "bazel-remote /status exposes occupancy only when available"
            ),
            "state": "unavailable",
            "state_reason": "bazel-remote /status exposes occupancy only, not hit/miss metrics",
            "source_url": "kubectl proxy: services/bst-artifact-server:8080/proxy/status",
            "derivation": (
                "bazel-remote /status exposes occupancy only; hit, miss, request, "
                "bytes, and latency metrics are not exposed"
            ),
        })
    else:
        rows.append({
            "id": "bazel-remote-ghost",
            "node": "ghost",
            "drive": "/var/mnt/ghost-data",
            "cache_backend": "bazel-remote",
            "storage_type": "cas",
            "used_bytes": None,
            "capacity_bytes": None,
            "percent": None,
            "source_url": "kubectl proxy: services/bst-artifact-server:8080/proxy/status",
            "collected_at": now,
            "derivation": "bazel-remote /status occupancy contract via kubectl API-server service proxy",
            "state": "unavailable",
            "state_reason": "cluster unreachable or bst-artifact-server not responding on /status",
        })
        heat_records.append({
            "cache_backend": "bazel-remote",
            "pod": None,
            "storage_type": "cas",
            "hit_count": None,
            "miss_count": None,
            "effectiveness": None,
            "requests": None,
            "bytes": None,
            "duration_seconds": None,
            "latency_ms": None,
            "logical_fill_bytes": None,
            "logical_capacity_bytes": None,
            "logical_fill_percent": None,
            "logical_fill_state": "unavailable",
            "logical_fill_reason": "bazel-remote /status unavailable",
            "state": "unavailable",
            "state_reason": "bazel-remote /status unavailable",
            "source_url": "kubectl proxy: services/bst-artifact-server:8080/proxy/status",
            "derivation": (
                "bazel-remote /status occupancy contract via kubectl API-server "
                "service proxy; endpoint unavailable"
            ),
        })

    # -- Buildbarn 2-shard CAS/AC (ghost + exo-0) ------------------------
    for pod in ("storage-0", "storage-1"):
        node = node_for_pod("buildbarn", pod)
        metrics = run_raw(f"/api/v1/namespaces/buildbarn/pods/{pod}:9980/proxy/metrics")
        for storage_type, capacity, block_count in (
            ("cas", CAS_CAPACITY_BYTES, CAS_BLOCKS),
            ("ac", AC_CAPACITY_BYTES, AC_BLOCKS),
        ):
            row_id = f"buildbarn-{pod}-{storage_type}"
            if node is None or metrics is None:
                heat_records.append({
                    "cache_backend": "buildbarn",
                    "pod": pod,
                    "storage_type": storage_type,
                    "hit_count": None,
                    "miss_count": None,
                    "effectiveness": None,
                    "requests": None,
                    "bytes": None,
                    "duration_seconds": None,
                    "latency_ms": None,
                    "logical_fill_bytes": None,
                    "logical_capacity_bytes": capacity,
                    "logical_fill_percent": None,
                    "logical_fill_state": "unavailable",
                    "logical_fill_reason": "BuildBarn metrics endpoint unavailable",
                    "state": "unavailable",
                    "state_reason": "BuildBarn metrics endpoint unavailable",
                    "source_url": f"kubectl proxy: pods/{pod}:9980/proxy/metrics (namespace buildbarn)",
                    "derivation": (
                        "BuildBarn blob access metrics endpoint was unavailable; "
                        "allocator and cache heat fields remain null"
                    ),
                })
                rows.append({
                    "id": row_id,
                    "node": None,
                    "drive": None,
                    "cache_backend": "buildbarn",
                    "storage_type": storage_type,
                    "used_bytes": None,
                    "capacity_bytes": capacity,
                    "percent": None,
                    "source_url": None,
                    "collected_at": now,
                    "derivation": None,
                    "state": "unavailable",
                    "state_reason": f"cluster unreachable or {pod} not scheduled/responding on diagnostics port 9980",
                })
                continue

            allocations = parse_metric_value(
                metrics, "buildbarn_blobstore_block_device_backed_block_allocator_allocations_total",
                {"storage_type": storage_type},
            )
            releases = parse_metric_value(
                metrics, "buildbarn_blobstore_block_device_backed_block_allocator_releases_total",
                {"storage_type": storage_type},
            )
            block_size = capacity / block_count
            used = None
            if allocations is not None and releases is not None:
                used = max(0.0, allocations - releases) * block_size
                used = min(used, float(capacity))

            heat_records[-1] = collect_cache_heat(
                metrics,
                pod,
                storage_type,
                logical_fill_bytes=used,
                logical_capacity_bytes=capacity,
                logical_fill_state="available" if used is not None else "unavailable",
                logical_fill_reason=(
                    None if used is not None else "allocator counters not readable from /metrics"
                ),
            )

            rows.append({
                "id": row_id,
                "node": node,
                "drive": "/var/mnt/ghost-data" if node == "ghost" else "/var/mnt/exo0-data",
                "cache_backend": "buildbarn",
                "storage_type": storage_type,
                "used_bytes": used,
                "capacity_bytes": capacity,
                "percent": round(100 * used / capacity, 3) if used is not None else None,
                "source_url": f"kubectl proxy: pods/{pod}:9980/proxy/metrics (namespace buildbarn)",
                "collected_at": now,
                "derivation": (
                    "(allocations_total - releases_total) * block_size, where block_size = "
                    f"{capacity} bytes / {block_count} blocks (manifests/buildbarn-config.yaml); "
                    "block-device-backed storage is fixed-size on disk, so this is an estimate "
                    "of logical fill, not physical allocation"
                ),
                "state": "available" if used is not None else "unavailable",
                "state_reason": None if used is not None else "allocator counters not readable from /metrics",
            })

    doc = {
        "schema_version": 1,
        "_meta": {
            "page": "index",
            "description": "BuildStream / remote-execution cache usage across both cache drives (ghost, exo-0)",
            "generated_at": now,
            "starter_artifact": False,
            "status": "live",
            "freshness_state": "fresh" if any(row["state"] == "available" for row in rows) else "unavailable",
        },
        "usb4_telemetry": usb4_telemetry,
        "summary_metrics": [
            {
                "id": "cache_cells_total",
                "value": len(rows),
                "source_url": None,
                "collected_at": now,
                "derivation": "number of cache storage cells in the collector contract",
            },
            {
                "id": "cache_cells_available",
                "value": sum(row["state"] == "available" for row in rows),
                "source_url": None,
                "collected_at": now,
                "derivation": "cache cells with readable live storage metrics",
            },
            {
                "id": "cache_cells_unavailable",
                "value": sum(row["state"] == "unavailable" for row in rows),
                "source_url": None,
                "collected_at": now,
                "derivation": "cache cells without readable live storage metrics",
            },
        ],
        "rows": rows,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    persist_cache_heat(heat_records, now)


if __name__ == "__main__":
    main()
