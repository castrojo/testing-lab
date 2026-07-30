#!/usr/bin/env python3
"""Render bounded USB-4 execution telemetry as Prometheus text."""

from __future__ import annotations

import json
from typing import Any

ALLOWED_NODES = {"ghost", "exo-0"}


def render_metrics(dataset: dict[str, Any]) -> str:
    lines = [
        "# HELP lab_usb4_execution_throughput_bytes_per_second Measured distributed execution throughput.",
        "# TYPE lab_usb4_execution_throughput_bytes_per_second gauge",
        "# HELP lab_usb4_execution_latency_ms Measured USB-4 action latency.",
        "# TYPE lab_usb4_execution_latency_ms gauge",
        "# HELP lab_usb4_execution_actions_total Measured remote execution actions.",
        "# TYPE lab_usb4_execution_actions_total gauge",
    ]
    for row in dataset.get("rows", []):
        node = row.get("node")
        if node not in ALLOWED_NODES or row.get("state") != "available":
            continue
        labels = f'node="{node}"'
        metrics = (
            ("lab_usb4_execution_throughput_bytes_per_second", row.get("throughput")),
            ("lab_usb4_execution_latency_ms", row.get("latency_ms")),
            ("lab_usb4_execution_actions_total", row.get("action_count")),
        )
        for name, value in metrics:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{name}{{{labels}}} {value}")
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    args = parser.parse_args()
    print(render_metrics(json.loads(open(args.dataset).read())), end="")


if __name__ == "__main__":
    main()
