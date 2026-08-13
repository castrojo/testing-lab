#!/usr/bin/env python3
"""Report bounded rolling traffic, workload activity, and registry signals."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any


HOST_NETWORK_POD_REGEX = "usb4-link-monitor-.*"


def query_prometheus(
    base_url: str, expression: str, timeout: float = 10
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]


def bytes_value(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def rows(
    samples: Iterable[dict[str, Any]],
) -> list[tuple[float, dict[str, str]]]:
    return sorted(
        (
            (float(sample["value"][1]), sample["metric"])
            for sample in samples
        ),
        key=lambda item: item[0],
        reverse=True,
    )


def node_name(metric: dict[str, str]) -> str:
    return metric.get("node") or metric.get("instance", "<unknown>")


def pod_name(metric: dict[str, str]) -> str:
    return metric.get("pod") or metric.get(
        "container_label_io_kubernetes_pod_name", ""
    )


def is_host_network_mirror(metric: dict[str, str]) -> bool:
    """Identify host-network counters that mirror node traffic."""
    host_network_labels = (
        "hostNetwork",
        "host_network",
        "container_label_io_kubernetes_pod_hostnetwork",
        "container_label_io_kubernetes_pod_host_network",
    )
    return any(
        metric.get(label, "").lower() in {"1", "true", "yes"}
        for label in host_network_labels
    ) or pod_name(metric).startswith("usb4-link-monitor-")


def bounded_rows(
    base_url: str, expression: str, limit: int
) -> list[tuple[float, dict[str, str]]]:
    return rows(query_prometheus(base_url, f"topk({limit}, {expression})"))


def promql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def identity(metric: dict[str, str]) -> str:
    namespace = metric.get(
        "namespace", metric.get("container_label_io_kubernetes_pod_namespace", "")
    )
    pod = pod_name(metric)
    container = metric.get(
        "container", metric.get("container_label_io_kubernetes_container_name", "")
    )
    parts = [part for part in (namespace, pod, container) if part]
    return "/".join(parts) or metric.get("instance", "<unknown>")


def print_workload_section(
    title: str, samples: list[tuple[float, dict[str, str]]]
) -> None:
    print(title)
    samples = [
        (value, metric)
        for value, metric in samples
        if identity(metric) != "<unknown>" and not is_host_network_mirror(metric)
    ]
    if not samples:
        print("  unavailable (cAdvisor exposed no non-hostNetwork workload labels)")
        return
    for value, metric in samples:
        print(f"  {bytes_value(value):>14}  {identity(metric)}")


def total_bytes(samples: Iterable[tuple[float, dict[str, str]]]) -> float:
    return sum(value for value, _ in samples)


def node_totals(
    samples: Iterable[tuple[float, dict[str, str]]],
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for value, metric in samples:
        node = node_name(metric)
        totals[node] = totals.get(node, 0.0) + value
    return totals


def estimate_non_node_egress(
    tx_samples: Iterable[tuple[float, dict[str, str]]],
    rx_samples: Iterable[tuple[float, dict[str, str]]],
) -> dict[str, float | None]:
    """Estimate per-node non-peer egress from paired node-root counters.

    For each node, subtract the receive total of every other visible node from
    its transmit total. A negative or incomplete subtraction is unavailable
    rather than being clamped into a fabricated WAN value.
    """
    tx_by_node = node_totals(tx_samples)
    rx_by_node = node_totals(rx_samples)
    nodes = set(tx_by_node) | set(rx_by_node)
    estimates: dict[str, float | None] = {}
    if len(nodes) < 2:
        return {node: None for node in nodes}

    for node in nodes:
        tx_value = tx_by_node.get(node)
        peer_nodes = nodes - {node}
        if tx_value is None or not peer_nodes.issubset(rx_by_node):
            estimates[node] = None
            continue
        estimate = tx_value - sum(rx_by_node[peer] for peer in peer_nodes)
        estimates[node] = estimate if estimate >= 0 else None
    return estimates


def cache_hit_ratio(
    upstream_rx_samples: Iterable[tuple[float, dict[str, str]]],
    cache_tx_samples: Iterable[tuple[float, dict[str, str]]],
) -> float | None:
    """Estimate byte hit ratio from Zot upstream receive and cache transmit."""
    upstream_rx = total_bytes(upstream_rx_samples)
    cache_tx = total_bytes(cache_tx_samples)
    if cache_tx <= 0 or upstream_rx < 0 or upstream_rx > cache_tx:
        return None
    return (cache_tx - upstream_rx) / cache_tx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--window", default="24h")
    parser.add_argument("--interface", default="enp191s0")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    interface = promql_escape(args.interface)
    window = args.window
    rx = query_prometheus(
        args.prometheus_url,
        f'sum by (instance,interface) (increase(container_network_receive_bytes_total{{id="/",interface="{interface}"}}[{window}]))',
    )
    tx = query_prometheus(
        args.prometheus_url,
        f'sum by (instance,interface) (increase(container_network_transmit_bytes_total{{id="/",interface="{interface}"}}[{window}]))',
    )
    pulls = query_prometheus(
        args.prometheus_url,
        f'sum by (service,repo) (increase(zot_repo_downloads_total[{window}]))',
    )
    workload_selector = (
        f'interface="{interface}",id!="/",'
        f'pod!~"{HOST_NETWORK_POD_REGEX}",'
        f'container_label_io_kubernetes_pod_name!~"{HOST_NETWORK_POD_REGEX}"'
    )
    workload_group = (
        "namespace,pod,container,"
        "container_label_io_kubernetes_pod_namespace,"
        "container_label_io_kubernetes_pod_name,"
        "container_label_io_kubernetes_container_name,"
        "hostNetwork,host_network,"
        "container_label_io_kubernetes_pod_hostnetwork,"
        "container_label_io_kubernetes_pod_host_network"
    )
    workload_rx = bounded_rows(
        args.prometheus_url,
        f"sum by ({workload_group}) (increase(container_network_receive_bytes_total{{{workload_selector}}}[{window}]))",
        args.limit,
    )
    workload_tx = bounded_rows(
        args.prometheus_url,
        f"sum by ({workload_group}) (increase(container_network_transmit_bytes_total{{{workload_selector}}}[{window}]))",
        args.limit,
    )
    zot_selector = (
        'id!="/",namespace="local-registry",pod=~"zot-cache-.*",interface!="lo"'
    )
    zot_upstream_rx = query_prometheus(
        args.prometheus_url,
        f'sum by (node,instance) (increase(container_network_receive_bytes_total{{{zot_selector}}}[{window}]))',
    )
    zot_cache_tx = query_prometheus(
        args.prometheus_url,
        f'sum by (node,instance) (increase(container_network_transmit_bytes_total{{{zot_selector}}}[{window}]))',
    )
    github_requests = bounded_rows(
        args.prometheus_url,
        f'sum by (client,endpoint,status) (increase(github_api_requests_total[{window}]))',
        args.limit,
    )
    github_throttled = bounded_rows(
        args.prometheus_url,
        f'sum by (client,reason) (increase(github_api_throttled_total[{window}]))',
        args.limit,
    )

    print(
        f"Uplink interface totals on {args.interface} "
        f"(LAN + WAN; {args.window} rolling window)"
    )
    print("Incoming:")
    for value, metric in rows(rx):
        print(f"  {bytes_value(value):>14}  {node_name(metric)}")
    print("Outgoing:")
    for value, metric in rows(tx):
        print(f"  {bytes_value(value):>14}  {node_name(metric)}")
    print()
    print("WAN estimate (partial; not a flow measurement):")
    print("  Image-pull WAN ingress (Zot upstream RX estimate):")
    if zot_upstream_rx:
        for value, metric in rows(zot_upstream_rx):
            print(f"    {bytes_value(value):>14}  {node_name(metric)}")
    else:
        print("    unavailable (no zot-cache cAdvisor receive series)")
    print("  Non-node uplink egress (uplink TX minus peer-node RX):")
    egress_estimates = estimate_non_node_egress(rows(tx), rows(rx))
    if not egress_estimates:
        print("    unavailable (fewer than two node-root series)")
    else:
        for node in sorted(egress_estimates):
            estimate = egress_estimates[node]
            if estimate is None:
                print(f"    unavailable  {node}")
            else:
                print(f"    {bytes_value(estimate):>14}  {node}")
    print("  Other WAN/LAN components: unavailable (no remote-address labels)")
    print()
    print("Zot cache signals (cAdvisor pod counters; estimated):")
    print("  Upstream RX (image-pull WAN ingress):")
    if zot_upstream_rx:
        for value, metric in rows(zot_upstream_rx):
            print(f"    {bytes_value(value):>14}  {node_name(metric)}")
    else:
        print("    unavailable (no zot-cache cAdvisor receive series)")
    print("  TX (LAN cache serving):")
    if zot_cache_tx:
        for value, metric in rows(zot_cache_tx):
            print(f"    {bytes_value(value):>14}  {node_name(metric)}")
    else:
        print("    unavailable (no zot-cache cAdvisor transmit series)")
    ratio = cache_hit_ratio(rows(zot_upstream_rx), rows(zot_cache_tx))
    if ratio is None:
        print("  Approximate byte cache hit ratio: unavailable")
    else:
        print(f"  Approximate byte cache hit ratio: {ratio:.1%}")
    print()
    print_workload_section(
        "Workload incoming (cAdvisor counters; host-network mirrors excluded):",
        workload_rx,
    )
    print_workload_section(
        "Workload outgoing (cAdvisor counters; host-network mirrors excluded):",
        workload_tx,
    )
    print()
    print("Registry pulls (count, not bytes):")
    for value, metric in rows(pulls)[: args.limit]:
        print(
            f"  {value:>10.0f}  "
            f"{metric.get('service', '<unknown>')}/{metric.get('repo', '<unknown>')}"
        )
    print()
    print("GitHub API requests (optional bounded exporter metrics):")
    for value, metric in github_requests:
        labels = "/".join(
            metric.get(label, "<unknown>") for label in ("client", "endpoint", "status")
        )
        print(f"  {value:>10.0f}  {labels}")
    if not github_requests:
        print("  unavailable (no github_api_requests_total series)")
    print("GitHub API throttles (optional bounded exporter metrics):")
    for value, metric in github_throttled:
        print(
            f"  {value:>10.0f}  "
            f"{metric.get('client', '<unknown>')}/{metric.get('reason', '<unknown>')}"
        )
    if not github_throttled:
        print("  unavailable (no github_api_throttled_total series)")
    print()
    print(
        "Notes: uplink bytes are interface totals, not external traffic; "
        "enp191s0 carries LAN, node-to-node, and hairpin traffic. The WAN "
        "estimate treats zot-cache RX as upstream image-pull bytes and subtracts "
        "visible peer-node RX from each node's uplink TX; missing or negative "
        "components stay unavailable. Workload bytes are grouped only when "
        "cAdvisor provides Kubernetes labels, and known hostNetwork "
        "usb4-link-monitor-* node mirrors are excluded. The cache-hit ratio is "
        "(Zot TX - upstream RX) / Zot TX under the same miss-traffic assumption. "
        "Zot pull metrics rank activity but do not expose per-repository byte "
        "totals. cAdvisor has no remote address/port labels, so destination-level "
        "flow telemetry needs a bounded eBPF/flow exporter; this report does not "
        "invent it."
    )


if __name__ == "__main__":
    main()
