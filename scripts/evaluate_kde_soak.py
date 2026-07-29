#!/usr/bin/env python3
"""Evaluate the KDE rolling soak gate without requiring a consecutive streak."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


WINDOW_SIZE = 30
MAX_INFRA_FLAKES = 2


def evaluate_kde_soak(
    history: list[dict[str, Any]],
    *,
    window_size: int = WINDOW_SIZE,
    max_infra_flakes: int = MAX_INFRA_FLAKES,
) -> dict[str, Any]:
    window = history[:window_size]
    passed = sum(entry.get("status") == "passed" for entry in window)
    failed = len(window) - passed
    infra_flakes = sum(
        entry.get("status") == "failed"
        and entry.get("failure_class", "test") == "infra"
        for entry in window
    )
    test_failures = failed - infra_flakes
    pass_rate = (passed / len(window) * 100) if window else None
    qualified = len(window) >= window_size and (
        passed >= window_size - 1
        or (
            passed >= window_size - 2
            and failed == infra_flakes
            and infra_flakes <= max_infra_flakes
        )
    )

    if len(window) < window_size:
        state = "pending"
    elif qualified:
        state = "qualified"
    else:
        state = "unqualified"

    return {
        "state": state,
        "window_size": window_size,
        "runs_recorded": len(window),
        "passed_runs": passed,
        "failed_runs": failed,
        "infra_flakes": infra_flakes,
        "test_failures": test_failures,
        "pass_rate": round(pass_rate, 2) if pass_rate is not None else None,
        "max_infra_flakes": max_infra_flakes,
        "human_approval_required": qualified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_file", type=Path)
    parser.add_argument("--window-size", type=int, default=WINDOW_SIZE)
    parser.add_argument("--max-infra-flakes", type=int, default=MAX_INFRA_FLAKES)
    args = parser.parse_args()

    data = json.loads(args.results_file.read_text(encoding="utf-8"))
    summary = evaluate_kde_soak(
        data.get("history", []),
        window_size=args.window_size,
        max_infra_flakes=args.max_infra_flakes,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["state"] == "qualified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
