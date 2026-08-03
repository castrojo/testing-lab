#!/usr/bin/env python3
"""Report bounded rolling traffic, workload activity, and registry signals."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any


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


def bounded_rows(
    base_url: str, expression: str, limit: int
) -> list[tuple[float, dict[str, str]]]:
    return rows(query_prometheus(base_url, f"topk({limit}, {expression})"))


def identity(metric: dict[str, str]) -> str:
    namespace = metric.get(
        "namespace", metric.get("container_label_io_kubernetes_pod_namespace", "")
    )
    pod = metric.get("pod", metric.get("container_label_io_kubernetes_pod_name", ""))
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
        (value, metric) for value, metric in samples if identity(metric) != "<unknown>"
    ]
    if not samples:
        print("  unavailable (cAdvisor exposed no workload labels)")
        return
    for value, metric in samples:
        print(f"  {bytes_value(value):>14}  {identity(metric)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--window", default="24h")
    parser.add_argument("--interface", default="enp191s0")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    interface = urllib.parse.quote(args.interface, safe="")
    window = urllib.parse.quote(args.window, safe="")
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
    workload_rx = bounded_rows(
        args.prometheus_url,
        f'sum by (namespace,pod,container,container_label_io_kubernetes_pod_namespace,container_label_io_kubernetes_pod_name,container_label_io_kubernetes_container_name) (increase(container_network_receive_bytes_total{{interface="{interface}",id!="/"}}[{window}]))',
        args.limit,
    )
    workload_tx = bounded_rows(
        args.prometheus_url,
        f'sum by (namespace,pod,container,container_label_io_kubernetes_pod_namespace,container_label_io_kubernetes_pod_name,container_label_io_kubernetes_container_name) (increase(container_network_transmit_bytes_total{{interface="{interface}",id!="/"}}[{window}]))',
        args.limit,
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

    print(f"External traffic on {args.interface} ({args.window} rolling window)")
    print("Incoming:")
    for value, metric in rows(rx):
        print(f"  {bytes_value(value):>14}  {metric.get('instance', '<unknown>')}")
    print("Outgoing:")
    for value, metric in rows(tx):
        print(f"  {bytes_value(value):>14}  {metric.get('instance', '<unknown>')}")
    print()
    print_workload_section("Workload incoming (cAdvisor counters):", workload_rx)
    print_workload_section("Workload outgoing (cAdvisor counters):", workload_tx)
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
        "Notes: uplink bytes are boundary traffic. Workload bytes are grouped "
        "only when cAdvisor provides Kubernetes labels. Zot metrics rank pull "
        "activity but do not expose per-repository byte totals. cAdvisor has no "
        "remote address/port labels, so destination-level flow telemetry needs "
        "a bounded eBPF/flow exporter; this report does not invent it."
    )


if __name__ == "__main__":
    main()
