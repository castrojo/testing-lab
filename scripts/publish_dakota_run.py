#!/usr/bin/env python3
"""Validate, append, report, and publish compact Dakota run records."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HISTORY_PATH = Path("docs/data/history/build-runs.ndjson")
REPO_URL = "https://github.com/projectbluefin/lab.git"
REPO = "projectbluefin/dakota"
LANE = "dakota-testing"
SCHEMA_VERSION = "1.0"
KINDS = {"build", "publish"}
INPUT_FIELDS = {
    "schema_version",
    "kind",
    "workflow_name",
    "status",
    "started_at",
    "finished_at",
    "recorded_at",
    "repo",
    "lane",
    "run_url",
    "commit_sha",
    "digest",
    "failure_class",
    "failure_stage",
    "failure_hint",
    "metrics",
    "attempt",
}
OUTPUT_FIELDS = {
    "schema_version",
    "recorded_at",
    "plane",
    "record_type",
    "repo",
    "lane",
    "run_id",
    "workflow_name",
    "status",
    "started_at",
    "finished_at",
    "duration_min",
    "run_url",
    "failure_stage",
    "failure_class",
    "commit_sha",
    "digest",
    "metrics",
    "attempt",
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
SAFE_STAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,119}$")
SAFE_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|credential|log|message|password|secret|stderr|stdout|token)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = re.compile(
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|bearer\s+\S+|x-access-token:[^@\s]+",
    re.IGNORECASE,
)


class RecordError(ValueError):
    """A record or history file violates the durable history contract."""


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RecordError(f"{field} must be a non-empty ISO8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError(f"{field} must be valid ISO8601") from exc
    if parsed.tzinfo is None:
        raise RecordError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: object, field: str, *, required: bool = False, limit: int = 200) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise RecordError(f"{field} must be a non-empty string")
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise RecordError(f"{field} must be a single-line string no longer than {limit} characters")
    if SENSITIVE_VALUE.search(value):
        raise RecordError(f"{field} contains a secret-like value")
    return value


def validate_url(value: object) -> str:
    url = clean_text(value, "run_url", required=True, limit=500)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RecordError("run_url must be an HTTP(S) URL without credentials, query, or fragment")
    return url


def normalize_status(value: object) -> str:
    aliases = {
        "passed": "passed",
        "success": "passed",
        "succeeded": "passed",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
    }
    if not isinstance(value, str) or value.lower() not in aliases:
        raise RecordError("status must be a terminal passed/failed value")
    return aliases[value.lower()]


def normalize_failure(value: object, hint: object, stage: object) -> str:
    aliases = {
        "auth": "authentication",
        "authentication": "authentication",
        "bst": "build",
        "build": "build",
        "infra": "infrastructure",
        "infrastructure": "infrastructure",
        "network": "network",
        "publish": "publish",
        "registry": "publish",
        "re": "remote-execution",
        "remote_execution": "remote-execution",
        "remote-execution": "remote-execution",
        "storage": "storage",
        "timeout": "timeout",
        "unknown": "unknown",
        "validation": "validation",
    }
    if value is not None:
        if not isinstance(value, str) or value.lower() not in aliases:
            raise RecordError("failure_class is not a supported class")
        return aliases[value.lower()]

    text = " ".join(
        item.lower()
        for item in (
            clean_text(hint, "failure_hint", limit=256),
            clean_text(stage, "failure_stage", limit=120),
        )
        if item
    )
    patterns = (
        ("timeout", r"timeout|timed out|deadline exceeded"),
        ("authentication", r"unauthorized|forbidden|authentication|permission denied"),
        ("storage", r"no space|disk|pvc|storage|volume|blob unknown"),
        ("network", r"dns|network|connection|tls|no route|name resolution"),
        ("remote-execution", r"buildbarn|remote.?execution|reapi|cas|worker"),
        ("publish", r"publish|push|registry|manifest|cosign|signature"),
        ("validation", r"validate|validation|lint|test|qa"),
        ("build", r"buildstream|\bbst\b|compile|build"),
        ("infrastructure", r"evict|oom|schedul|pod|node|container"),
    )
    return next((name for name, pattern in patterns if re.search(pattern, text)), "unknown")


def validate_metrics(value: object) -> dict[str, float | int | None]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise RecordError("metrics must be an object with at most 32 numeric fields")
    metrics: dict[str, float | int | None] = {}
    for key, metric in value.items():
        if not isinstance(key, str) or not SAFE_METRIC.fullmatch(key) or SENSITIVE_KEY.search(key):
            raise RecordError("metric names must be safe, non-sensitive snake_case identifiers")
        if metric is None:
            metrics[key] = None
            continue
        if isinstance(metric, bool) or not isinstance(metric, (int, float)):
            raise RecordError(f"metric {key} must be numeric or null")
        if not math.isfinite(metric) or metric < 0:
            raise RecordError(f"metric {key} must be finite and non-negative")
        metrics[key] = metric
    return metrics


def normalize_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise RecordError("record must be a JSON object")
    unknown = set(record) - INPUT_FIELDS
    if unknown:
        raise RecordError(f"unsupported record fields: {', '.join(sorted(unknown))}")
    if record.get("schema_version", SCHEMA_VERSION) not in {1, "1", SCHEMA_VERSION}:
        raise RecordError(f"schema_version must be {SCHEMA_VERSION}")

    kind = record.get("kind")
    if kind not in KINDS:
        raise RecordError("kind must be build or publish")
    workflow_name = clean_text(record.get("workflow_name"), "workflow_name", required=True)
    if not SAFE_NAME.fullmatch(workflow_name):
        raise RecordError("workflow_name contains unsupported characters")
    status = normalize_status(record.get("status"))
    started_at = parse_timestamp(record.get("started_at"), "started_at")
    finished_at = parse_timestamp(record.get("finished_at"), "finished_at")
    if finished_at < started_at:
        raise RecordError("finished_at cannot precede started_at")
    recorded_at = parse_timestamp(record.get("recorded_at", record.get("finished_at")), "recorded_at")
    if recorded_at < finished_at:
        raise RecordError("recorded_at cannot precede finished_at")

    repo = clean_text(record.get("repo", REPO), "repo", required=True)
    lane = clean_text(record.get("lane", LANE), "lane", required=True)
    if repo != REPO or lane != LANE:
        raise RecordError(f"this publisher only accepts {REPO} records for {LANE}")

    commit_sha = clean_text(record.get("commit_sha"), "commit_sha", limit=64)
    if commit_sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        raise RecordError("commit_sha must be a hexadecimal Git object id")
    digest = clean_text(record.get("digest"), "digest", limit=80)
    if digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise RecordError("digest must be a sha256 digest")
    if kind == "publish" and status == "passed" and not digest:
        raise RecordError("successful publish records require digest")

    stage = clean_text(record.get("failure_stage"), "failure_stage", limit=120)
    failure_hint = clean_text(record.get("failure_hint"), "failure_hint", limit=256)
    if stage and not SAFE_STAGE.fullmatch(stage):
        raise RecordError("failure_stage must be a short stage name, not raw output")
    if status == "passed" and any((record.get("failure_class"), stage, failure_hint)):
        raise RecordError("passed records cannot contain failure details")
    failure_class = (
        normalize_failure(record.get("failure_class"), failure_hint, stage) if status == "failed" else None
    )
    attempt = record.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise RecordError("attempt must be a positive integer")

    return {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": format_timestamp(recorded_at),
        "plane": "lab",
        "record_type": kind,
        "repo": repo,
        "lane": lane,
        "run_id": workflow_name,
        "workflow_name": workflow_name,
        "status": status,
        "started_at": format_timestamp(started_at),
        "finished_at": format_timestamp(finished_at),
        "duration_min": round((finished_at - started_at).total_seconds() / 60, 6),
        "run_url": validate_url(record.get("run_url")),
        "failure_stage": stage if status == "failed" else None,
        "failure_class": failure_class,
        "commit_sha": commit_sha.lower() if commit_sha else None,
        "digest": digest.lower() if digest else None,
        "metrics": validate_metrics(record.get("metrics")),
        "attempt": attempt,
    }


def validate_stored_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != OUTPUT_FIELDS:
        raise RecordError("stored Dakota record fields do not match schema 1.0")
    normalized = normalize_record(
        {
            "schema_version": record["schema_version"],
            "kind": record["record_type"],
            "workflow_name": record["workflow_name"],
            "status": record["status"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "recorded_at": record["recorded_at"],
            "repo": record["repo"],
            "lane": record["lane"],
            "run_url": record["run_url"],
            "commit_sha": record["commit_sha"],
            "digest": record["digest"],
            "failure_class": record["failure_class"],
            "failure_stage": record["failure_stage"],
            "metrics": record["metrics"],
            "attempt": record["attempt"],
        }
    )
    if normalized != record:
        raise RecordError("stored Dakota record is not canonical")
    return normalized


def read_json_record(path: str) -> dict[str, object]:
    try:
        if path == "-":
            return normalize_record(json.load(sys.stdin))
        with Path(path).open(encoding="utf-8") as handle:
            return normalize_record(json.load(handle))
    except json.JSONDecodeError as exc:
        raise RecordError("input is not valid JSON") from exc
    except OSError as exc:
        raise RecordError(f"cannot read record file: {exc.strerror}") from exc


def read_history(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(record, dict):
            raise RecordError(f"{path}:{line_number}: history line must be an object")
        records.append(record)
    return records


def validate_history(path: Path) -> int:
    records = read_history(path)
    seen: set[str] = set()
    validated = 0
    for line_number, record in enumerate(records, 1):
        workflow_name = record.get("workflow_name")
        if workflow_name:
            if workflow_name in seen:
                raise RecordError(f"{path}:{line_number}: duplicate workflow_name")
            seen.add(workflow_name)
        if record.get("schema_version") == SCHEMA_VERSION and record.get("repo") == REPO:
            try:
                validate_stored_record(record)
            except RecordError as exc:
                raise RecordError(f"{path}:{line_number}: {exc}") from exc
            validated += 1
    return validated


def append_record(path: Path, record: dict[str, object]) -> bool:
    records = read_history(path)
    for existing in records:
        if existing.get("workflow_name") != record["workflow_name"]:
            continue
        if existing == record:
            return False
        raise RecordError("workflow_name already exists with different record content")
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
    with path.open("a", encoding="utf-8") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    return True


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)
    return round(value, 3)


def summarize(records: list[dict[str, object]]) -> dict[str, object]:
    durations = [round(float(record["duration_min"]) * 60, 3) for record in records]
    failures = [record for record in records if record["status"] == "failed"]
    commits: dict[str, list[str]] = defaultdict(list)
    metrics: dict[str, list[float]] = defaultdict(list)
    for record in records:
        if record.get("commit_sha"):
            commits[str(record["commit_sha"])].append(str(record["status"]))
        for key, value in record.get("metrics", {}).items():
            if value is not None:
                metrics[key].append(float(value))
    repeated = [statuses for statuses in commits.values() if len(statuses) > 1]
    flaky = [statuses for statuses in repeated if len(set(statuses)) > 1]
    return {
        "count": len(records),
        "failure_rate_pct": round(len(failures) * 100 / len(records), 3) if records else None,
        "failure_classes": dict(sorted(Counter(record["failure_class"] for record in failures).items())),
        "duration_seconds": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95)},
        "repeated_commits": len(repeated),
        "flaky_commits": len(flaky),
        "flakiness_rate_pct": round(len(flaky) * 100 / len(repeated), 3) if repeated else None,
        "metrics": {
            key: {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            for key, values in sorted(metrics.items())
        },
    }


def difference(current: object, previous: object) -> float | None:
    if current is None or previous is None:
        return None
    return round(float(current) - float(previous), 3)


def build_report(
    records: list[dict[str, object]],
    *,
    window: int,
    kind: str | None = None,
) -> list[dict[str, object]]:
    if window < 1:
        raise RecordError("window must be positive")
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        if record.get("schema_version") != SCHEMA_VERSION or record.get("repo") != REPO:
            continue
        validated = validate_stored_record(record)
        if kind and validated["record_type"] != kind:
            continue
        groups[(str(validated["lane"]), str(validated["record_type"]))].append(validated)

    report = []
    for (group_lane, group_kind), group_records in sorted(groups.items()):
        ordered = sorted(group_records, key=lambda item: parse_timestamp(item["started_at"], "started_at"))
        current = summarize(ordered[-window:])
        previous = summarize(ordered[-2 * window : -window])
        report.append(
            {
                "lane": group_lane,
                "record_type": group_kind,
                "window": window,
                "current": current,
                "previous": previous,
                "comparison": {
                    "duration_p50_seconds_delta": difference(
                        current["duration_seconds"]["p50"], previous["duration_seconds"]["p50"]
                    ),
                    "duration_p95_seconds_delta": difference(
                        current["duration_seconds"]["p95"], previous["duration_seconds"]["p95"]
                    ),
                    "failure_rate_delta_pp": difference(
                        current["failure_rate_pct"], previous["failure_rate_pct"]
                    ),
                    "flakiness_rate_delta_pp": difference(
                        current["flakiness_rate_pct"], previous["flakiness_rate_pct"]
                    ),
                },
            }
        )
    return report


def format_value(value: object, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:g}{suffix}"


def print_report(report: list[dict[str, object]]) -> None:
    if not report:
        print("No Dakota build/publish history records available.")
        return
    for group in report:
        current = group["current"]
        comparison = group["comparison"]
        print(
            f"{group['lane']} {group['record_type']}: n={current['count']} "
            f"p50={format_value(current['duration_seconds']['p50'], 's')} "
            f"p95={format_value(current['duration_seconds']['p95'], 's')} "
            f"failures={format_value(current['failure_rate_pct'], '%')} "
            f"flakiness={format_value(current['flakiness_rate_pct'], '%')} "
            f"Δp50={format_value(comparison['duration_p50_seconds_delta'], 's')} "
            f"Δfailure={format_value(comparison['failure_rate_delta_pp'], 'pp')}"
        )


def git_env(auth_dir: Path, token: str) -> dict[str, str]:
    askpass = auth_dir / "git-askpass"
    askpass.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
        '  *) printf "%s\\n" "$GITHUB_TOKEN" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env = os.environ.copy()
    env.update({"GITHUB_TOKEN": token, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"})
    return env


def run_git(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def require_git(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode:
        raise RecordError(f"git {operation} failed with exit code {result.returncode}")


def publish_record(
    record: dict[str, object],
    *,
    token: str,
    work_dir: Path,
    attempts: int = 3,
) -> bool:
    if not token:
        raise RecordError("GITHUB_TOKEN is required for publication")

    work_dir.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    clone_dir = work_dir / f".publish-dakota-run-{identifier}"
    auth_dir = work_dir / f".publish-dakota-auth-{identifier}"
    try:
        auth_dir.mkdir(mode=0o700)
        env = git_env(auth_dir, token)
        require_git(
            run_git(
                ["clone", "--depth", "1", "--branch", "main", REPO_URL, str(clone_dir)],
                cwd=work_dir,
                env=env,
            ),
            "clone",
        )
        require_git(run_git(["config", "user.name", "github-actions[bot]"], cwd=clone_dir, env=env), "config")
        require_git(
            run_git(
                ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
                cwd=clone_dir,
                env=env,
            ),
            "config",
        )
        for attempt in range(attempts):
            if attempt:
                require_git(run_git(["fetch", "origin", "main"], cwd=clone_dir, env=env), "fetch")
                require_git(run_git(["reset", "--hard", "origin/main"], cwd=clone_dir, env=env), "reset")
            if not append_record(clone_dir / HISTORY_PATH, record):
                return False
            require_git(run_git(["add", str(HISTORY_PATH)], cwd=clone_dir, env=env), "add")
            require_git(
                run_git(
                    [
                        "commit",
                        "-m",
                        f"chore: record Dakota {record['record_type']} run ({record['workflow_name']})",
                        "-m",
                        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
                    ],
                    cwd=clone_dir,
                    env=env,
                ),
                "commit",
            )
            pushed = run_git(["push", "origin", "HEAD:main"], cwd=clone_dir, env=env)
            if pushed.returncode == 0:
                return True
        raise RecordError(f"git push did not succeed after {attempts} attempts")
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
        shutil.rmtree(auth_dir, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate and normalize one compact JSON record")
    validate.add_argument("record", help="JSON file, or - for stdin")

    append = commands.add_parser("append", help="append one record to local NDJSON")
    append.add_argument("record", help="JSON file, or - for stdin")
    append.add_argument("--history", type=Path, default=HISTORY_PATH)

    history = commands.add_parser("validate-history", help="validate the NDJSON history file")
    history.add_argument("--history", type=Path, default=HISTORY_PATH)

    report = commands.add_parser("report", help="compare trailing and previous history windows")
    report.add_argument("--history", type=Path, default=HISTORY_PATH)
    report.add_argument("--window", type=int, default=20)
    report.add_argument("--kind", choices=sorted(KINDS))
    report.add_argument("--json", action="store_true", dest="as_json")

    publish = commands.add_parser("publish", help="append, commit, and push one record to lab main")
    publish.add_argument("record", help="JSON file, or - for stdin")
    publish.add_argument("--work-dir", type=Path, default=Path.cwd())
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            print(json.dumps(read_json_record(args.record), separators=(",", ":"), sort_keys=True))
        elif args.command == "append":
            added = append_record(args.history, read_json_record(args.record))
            print("appended" if added else "already recorded")
        elif args.command == "validate-history":
            count = validate_history(args.history)
            print(f"valid: {count} Dakota records")
        elif args.command == "report":
            result = build_report(read_history(args.history), window=args.window, kind=args.kind)
            if args.as_json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print_report(result)
        elif args.command == "publish":
            added = publish_record(
                read_json_record(args.record),
                token=os.environ.get("GITHUB_TOKEN", ""),
                work_dir=args.work_dir,
            )
            print("published" if added else "already recorded")
        return 0
    except RecordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
