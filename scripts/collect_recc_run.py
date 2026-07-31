#!/usr/bin/env python3
"""Collect machine-readable evidence from one RECC baseline run.

The collector deliberately consumes caller-supplied artifacts.  It parses
metadata, BuildStream output, RECC_VERBOSE output, and two Prometheus
snapshots, but never stores the raw artifacts in its result.  Prometheus
metric names are either supplied by the caller or discovered from the
snapshots; no BuildBarn metric names are guessed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
TIMING_FIELDS = ("wall_seconds", "fetch_seconds", "build_seconds", "push_seconds")
RECC_FIELDS = (
    "action_count",
    "cache_hits",
    "cache_misses",
    "local_fallbacks",
    "compile_seconds",
    "link_seconds",
)
PHASE_NAMES = ("cold", "warm")
BUILDBARN_WORKER_METRICS = (
    "buildbarn_builder_build_executor_duration_seconds_count",
    "buildbarn_builder_build_executor_duration_seconds_sum",
    "buildbarn_builder_in_memory_build_queue_tasks_queued_duration_seconds_count",
    "buildbarn_builder_in_memory_build_queue_tasks_queued_duration_seconds_sum",
    "buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_count",
    "buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_sum",
)
BUILDBARN_CAS_METRICS = (
    "buildbarn_blobstore_blob_access_operations_blob_size_bytes_count",
    "buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum",
    "buildbarn_blobstore_blob_access_operations_duration_seconds_count",
    "buildbarn_blobstore_blob_access_operations_duration_seconds_sum",
)

RECC_PREFIX = re.compile(
    r"^\s*(?:\[(?:RECC|RECC_VERBOSE)\]|(?:RECC|RECC_VERBOSE)"
    r"(?:\s*[:|]|\s+))\s*(?P<body>.*)$",
    re.IGNORECASE,
)
RECC_SECTION_START = re.compile(
    r"^\s*(?:\[(?:RECC|RECC_VERBOSE)\]|(?:===|---)\s*(?:BEGIN\s+)?"
    r"(?:RECC|RECC_VERBOSE)(?:\s+LOG)?\s*(?:===|---))\s*$",
    re.IGNORECASE,
)
RECC_SECTION_END = re.compile(
    r"^\s*(?:\[/\s*(?:RECC|RECC_VERBOSE)\s*\]|(?:===|---)\s*END\s+"
    r"(?:RECC|RECC_VERBOSE)(?:\s+LOG)?\s*(?:===|---))\s*$",
    re.IGNORECASE,
)
FIXTURE_STDOUT_MARKER = re.compile(
    r"^\s*(?:\[FIXTURE_STDOUT\]|fixture\s+stdout\s*[:|])\s*(?P<body>.*)$",
    re.IGNORECASE,
)


def parse_timestamp(value: Any) -> dt.datetime | None:
    """Return a timezone-aware UTC timestamp, or ``None`` for unusable input."""

    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(dt.timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def format_timestamp(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_duration(value: Any) -> float | None:
    """Parse seconds, ``HH:MM:SS``, or a human-readable duration."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or "--:--:--" in text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    clock = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text)
    if clock:
        hours = float(clock.group(1) or 0)
        return hours * 3600 + float(clock.group(2)) * 60 + float(clock.group(3))

    total = 0.0
    found = False
    for number, unit in re.findall(
        r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|sec|s|milliseconds?|msecs?|ms)\b",
        text,
    ):
        found = True
        amount = float(number)
        if unit.startswith("h"):
            total += amount * 3600
        elif unit.startswith("m") and unit not in {"ms", "msec", "msecs", "millisecond", "milliseconds"}:
            total += amount * 60
        elif unit.startswith("ms") or unit.startswith("millisecond"):
            total += amount / 1000
        else:
            total += amount
    return total if found else None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _parse_key_value_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*[:=]\s*(.*?)\s*$", line)
        if match:
            values[match.group(1).lower().replace("-", "_")] = match.group(2)
    return values


def parse_run_metadata(metadata: Any) -> dict[str, Any]:
    """Normalize run identity and timestamps from JSON or key/value text."""

    if isinstance(metadata, str):
        try:
            decoded = json.loads(metadata)
        except json.JSONDecodeError:
            decoded = _parse_key_value_text(metadata)
        metadata = decoded
    if not isinstance(metadata, dict):
        metadata = {}
    nested = metadata.get("run") or metadata.get("metadata")
    if isinstance(nested, dict):
        metadata = {**metadata, **nested}

    run_id = _first(metadata, "run_id", "id", "workflow_name", "workflow")
    mode = _first(metadata, "mode", "recc_mode", "option")
    started = parse_timestamp(
        _first(metadata, "started_at", "started", "start_time", "start")
    )
    finished = parse_timestamp(
        _first(metadata, "finished_at", "finished", "finish_time", "finish", "ended_at")
    )
    duration = parse_duration(_first(metadata, "duration_seconds", "duration"))
    if duration is None and started and finished and finished >= started:
        duration = (finished - started).total_seconds()
    raw_phases = _first(metadata, "phases")
    phases = raw_phases if isinstance(raw_phases, dict) else {}

    values = {
        "run_id": run_id,
        "mode": mode,
        "started_at": format_timestamp(started),
        "finished_at": format_timestamp(finished),
        "duration_seconds": duration,
        "phases": phases,
    }
    missing = {
        field: "run metadata did not provide this field"
        for field, value in values.items()
        if value is None or (field == "phases" and not value)
    }
    return {
        **values,
        "state": "available" if values["run_id"] or started or finished else "unavailable",
        "state_reason": (
            None
            if values["run_id"] or started or finished
            else "no run metadata was supplied"
        ),
        "unavailable_fields": missing,
    }


def _load_json_text(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_recc_text(text: str) -> tuple[str | None, str]:
    """Return only text explicitly identified as RECC evidence."""

    lines: list[str] = []
    in_section = False
    marked = False
    for line in text.splitlines():
        if RECC_SECTION_END.match(line):
            in_section = False
            marked = True
            continue
        if RECC_SECTION_START.match(line):
            in_section = True
            marked = True
            continue
        prefix = RECC_PREFIX.match(line)
        if prefix:
            marked = True
            lines.append(prefix.group("body"))
        elif in_section:
            lines.append(line)
    if not marked:
        return None, "RECC output lacked an explicit RECC marker or dedicated RECC log section"
    if not any(line.strip() for line in lines):
        return None, "RECC marker or dedicated section contained no evidence lines"
    return "\n".join(lines), ""


def _structured_recc_source(decoded: Any) -> tuple[dict[str, Any] | None, str]:
    """Find a structured RECC section without treating generic JSON as RECC."""

    if not isinstance(decoded, dict):
        return None, "RECC output was not a structured object"
    for key in ("recc", "recc_verbose", "recc_log"):
        section = decoded.get(key)
        if isinstance(section, dict):
            return section, ""
        if isinstance(section, str):
            nested = _load_json_text(section)
            if isinstance(nested, dict):
                return nested, ""
    section_name = str(
        _first(decoded, "section", "section_name", "source", "source_name") or ""
    ).lower()
    if "recc" in section_name:
        return decoded, ""
    if any(field in decoded for field in RECC_FIELDS):
        return decoded, ""
    return None, "structured output lacked a dedicated RECC section or RECC fields"


def _phase_names_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    found = {
        match.group(1).lower()
        for match in re.finditer(
            r"^\s*(?:===\s*)?(cold|warm)\s+phase\b", text, re.IGNORECASE | re.MULTILINE
        )
    }
    return [name for name in PHASE_NAMES if name in found]


def _phase_record(
    timings: dict[str, float | None],
    *,
    reason_prefix: str,
) -> dict[str, Any]:
    unavailable = {
        f"timings.{field}": f"{reason_prefix}; {field.replace('_', ' ')} was not supplied"
        for field, value in timings.items()
        if value is None
    }
    available = any(value is not None for value in timings.values())
    return {
        "timings": timings,
        "state": "available" if available else "unavailable",
        "state_reason": None if available else reason_prefix,
        "unavailable_fields": unavailable,
    }


def _parse_phase_timings(
    source: dict[str, Any],
    text: str | None,
    expected_phases: Iterable[str] | None,
) -> dict[str, dict[str, Any]]:
    names = {str(name).lower() for name in (expected_phases or ())}
    raw_phases = source.get("phases")
    if not isinstance(raw_phases, dict):
        raw_phases = source.get("phase_timings")
    if isinstance(raw_phases, dict):
        names.update(str(name).lower() for name in raw_phases)
    names.update(_phase_names_from_text(text))
    if not names:
        return {}

    phases: dict[str, dict[str, Any]] = {}
    for name in PHASE_NAMES:
        if name not in names:
            continue
        raw_phase = raw_phases.get(name) if isinstance(raw_phases, dict) else None
        raw_phase = raw_phase if isinstance(raw_phase, dict) else {}
        timing_source = (
            raw_phase.get("timings")
            if isinstance(raw_phase.get("timings"), dict)
            else raw_phase
        )
        timings = {
            "wall_seconds": _duration_from_mapping(
                timing_source, "wall_seconds", "wall", "elapsed_seconds", "elapsed"
            ),
            "fetch_seconds": _duration_from_mapping(
                timing_source, "fetch_seconds", "fetch", "fetch_duration"
            ),
            "build_seconds": _duration_from_mapping(
                timing_source, "build_seconds", "build", "build_duration"
            ),
            "push_seconds": _duration_from_mapping(
                timing_source, "push_seconds", "push", "push_duration"
            ),
        }
        if text:
            for field, label in (
                ("wall_seconds", "wall"),
                ("fetch_seconds", "fetch"),
                ("build_seconds", "build"),
                ("push_seconds", "push"),
            ):
                if timings[field] is not None:
                    continue
                match = re.search(
                    rf"^\s*{re.escape(name)}\s+{label}(?:_seconds)?\s*[:=]\s*([^\n,]+)",
                    text,
                    re.IGNORECASE | re.MULTILINE,
                )
                if match:
                    timings[field] = parse_duration(match.group(1))
        phases[name] = _phase_record(
            timings,
            reason_prefix=f"no {name} phase timing evidence was supplied",
        )
    return phases


def _duration_from_mapping(mapping: dict[str, Any], *keys: str) -> float | None:
    value = _first(mapping, *keys)
    if isinstance(value, dict):
        value = _first(value, "seconds", "duration", "value")
    return parse_duration(value)


def _section_status(
    values: dict[str, Any],
    unavailable_fields: dict[str, str],
    empty_reason: str,
) -> tuple[str, str | None]:
    if any(value is not None for value in values.values()):
        return "available", None
    return "unavailable", empty_reason


def _parse_element(element: Any, default_name: str | None = None) -> dict[str, Any] | None:
    if isinstance(element, str):
        return {
            "name": element,
            "state": None,
            "key": None,
            "cache_origin": None,
            "digest": None,
            "unavailable_fields": {
                "state": "BuildStream did not provide the element state",
                "key": "BuildStream did not provide the element key",
                "cache_origin": "BuildStream did not provide the element cache origin",
                "digest": "BuildStream did not provide the element digest",
            },
        }
    if not isinstance(element, dict):
        return None
    name = _first(element, "name", "element", "element_name") or default_name
    if not name:
        return None
    state = _first(element, "state", "status", "element_state")
    key = _first(element, "key", "element_key", "cache_key")
    cache_origin = _first(
        element,
        "cache_origin",
        "cache_source",
        "artifact_cache_origin",
        "artifact_cache",
    )
    digest = _first(element, "digest", "output_digest", "artifact_digest")
    values = {
        "state": state,
        "key": key,
        "cache_origin": cache_origin,
        "digest": digest,
    }
    return {
        "name": name,
        **values,
        "unavailable_fields": {
            field: f"BuildStream did not provide the element {field}"
            for field, value in values.items()
            if value is None
        },
    }


def parse_buildstream_output(
    text: Any,
    *,
    expected_phases: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Parse BuildStream timing and element evidence from JSON or text."""

    decoded = _load_json_text(text)
    source = decoded if isinstance(decoded, dict) else {}
    timings_source = source.get("timings") if isinstance(source.get("timings"), dict) else source
    timings = {
        "wall_seconds": _duration_from_mapping(
            timings_source, "wall_seconds", "wall", "elapsed_seconds", "elapsed"
        ),
        "fetch_seconds": _duration_from_mapping(
            timings_source, "fetch_seconds", "fetch", "fetch_duration"
        ),
        "build_seconds": _duration_from_mapping(
            timings_source, "build_seconds", "build", "build_duration"
        ),
        "push_seconds": _duration_from_mapping(
            timings_source, "push_seconds", "push", "push_duration"
        ),
    }

    elements: list[dict[str, Any]] = []
    raw_elements = source.get("elements") if isinstance(source, dict) else None
    if isinstance(raw_elements, dict):
        raw_elements = [
            {"name": name, **value} if isinstance(value, dict) else {"name": name, "state": value}
            for name, value in raw_elements.items()
        ]
    if isinstance(raw_elements, list):
        elements = [parsed for item in raw_elements if (parsed := _parse_element(item))]

    output_digest = _first(source, "output_digest", "result_digest", "artifact_digest")
    fixture_stdout = _first(
        source, "fixture_stdout", "fixture_output", "stdout"
    )

    if decoded is None and text:
        for field, labels in {
            "wall_seconds": r"(?:(?:wall|total|elapsed)\s*(?:build\s*)?(?:time|duration)?|total\s+build)",
            "fetch_seconds": r"fetch(?:ing)?",
            "build_seconds": r"build(?:ing)?",
            "push_seconds": r"push(?:ing)?",
        }.items():
            if timings[field] is not None:
                continue
            pattern = rf"{labels}\s*[:=]\s*([^\n,]+)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                timings[field] = parse_duration(match.group(1))

        current: dict[str, Any] = {}
        for line in text.splitlines():
            name_match = re.search(
                r"(?:element|name)\s*[:=]\s*([^\s,]+)", line, re.IGNORECASE
            )
            if name_match and current:
                parsed = _parse_element(current)
                if parsed and parsed not in elements:
                    elements.append(parsed)
                current = {}
            for field, pattern in (
                ("name", r"(?:element|name)\s*[:=]\s*([^\s,]+)"),
                ("state", r"(?:state|status)\s*[:=]\s*([^\s,]+)"),
                ("key", r"(?:element[_ ]?key|cache[_ ]?key|key)\s*[:=]\s*([^\s,]+)"),
                (
                    "cache_origin",
                    r"(?:cache[_ ]?origin|cache[_ ]?source|artifact[_ ]?cache)\s*[:=]\s*([^\s,]+)",
                ),
                ("digest", r"(?:output[_ ]?digest|artifact[_ ]?digest|digest)\s*[:=]\s*([^\s,]+)"),
            ):
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    current[field] = match.group(1).rstrip(".,")
            stdout_match = FIXTURE_STDOUT_MARKER.match(line)
            if stdout_match:
                fixture_stdout = stdout_match.group("body")
            digest_match = re.search(
                r"^\s*(?:output[_ ]?digest|artifact[_ ]?digest)\s*[:=]\s*([^\s,]+)",
                line,
                re.IGNORECASE,
            )
            if digest_match:
                output_digest = digest_match.group(1).rstrip(".,")
        if current:
            parsed = _parse_element(current)
            if parsed and parsed not in elements:
                elements.append(parsed)

    if output_digest is None and len(elements) == 1:
        output_digest = elements[0].get("digest")

    phases = _parse_phase_timings(source, text if isinstance(text, str) else None, expected_phases)
    unavailable = {
        field: f"BuildStream output did not contain {field.replace('_', ' ')}"
        for field, value in timings.items()
        if value is None
    }
    elements_available = any(
        any(
            element.get(field) is not None
            for field in ("state", "key", "cache_origin", "digest")
        )
        for element in elements
    )
    if not elements:
        unavailable["elements"] = "BuildStream output did not contain element state/key/digest evidence"
    elif not elements_available:
        unavailable["elements"] = (
            "BuildStream output contained element names but no element evidence fields"
        )
    else:
        if any(element.get("key") is None for element in elements):
            unavailable["element_key"] = (
                "BuildStream did not provide an element key for every element"
            )
        if any(element.get("cache_origin") is None for element in elements):
            unavailable["element_cache_origin"] = (
                "BuildStream did not provide an element cache origin for every element"
            )
    if output_digest is None:
        unavailable["output_digest"] = "BuildStream output did not contain an output digest"
    if not isinstance(fixture_stdout, str) or not fixture_stdout.strip():
        fixture_stdout = None
        unavailable["fixture_stdout"] = (
            "BuildStream output did not contain an explicit fixture stdout field"
        )
    phases_available = any(
        phase["state"] == "available" for phase in phases.values()
    )
    state, reason = _section_status(
        {
            **timings,
            "elements": elements if elements_available else None,
            "output_digest": output_digest,
            "fixture_stdout": fixture_stdout,
            "phases": phases if phases_available else None,
        },
        unavailable,
        "no BuildStream timing or element evidence was found",
    )
    return {
        "timings": timings,
        "phases": phases,
        "elements": elements,
        "output_digest": output_digest,
        "fixture_stdout": fixture_stdout,
        "state": state,
        "state_reason": reason,
        "unavailable_fields": unavailable,
    }


def _number_from_mapping(mapping: dict[str, Any], *keys: str) -> float | int | None:
    value = _first(mapping, *keys)
    if isinstance(value, dict):
        value = _first(value, "count", "total", "value", "seconds", "duration")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return int(value) if float(value).is_integer() else float(value)
    return None


def parse_recc_verbose(text: Any) -> dict[str, Any]:
    """Parse conservative evidence from explicitly identified RECC output."""

    decoded = _load_json_text(text)
    marker_reason = ""
    if isinstance(decoded, dict):
        source, marker_reason = _structured_recc_source(decoded)
        source = source or {}
    elif isinstance(text, str):
        marked_text, marker_reason = _extract_recc_text(text)
        decoded_marked = _load_json_text(marked_text)
        if isinstance(decoded_marked, dict):
            source = decoded_marked
        else:
            source = {}
            text = marked_text
    else:
        source = {}
        marker_reason = "RECC output was not supplied"

    timings_source = source.get("timings") if isinstance(source.get("timings"), dict) else source
    values: dict[str, Any] = {
        "action_count": _number_from_mapping(
            source, "action_count", "actions", "actions_total"
        ),
        "cache_hits": _number_from_mapping(source, "cache_hits", "hits", "action_cache_hits"),
        "cache_misses": _number_from_mapping(
            source, "cache_misses", "misses", "action_cache_misses"
        ),
        "local_fallbacks": _number_from_mapping(
            source, "local_fallbacks", "fallbacks", "local_fallback_count"
        ),
        "compile_seconds": _duration_from_mapping(
            timings_source, "compile_seconds", "compile", "compile_duration"
        ),
        "link_seconds": _duration_from_mapping(
            timings_source, "link_seconds", "link", "link_duration"
        ),
    }

    if not source and isinstance(text, str) and text:
        lines = text.splitlines()
        detected_started_actions = 0
        detected_completed_actions = 0
        detected_hits = 0
        detected_misses = 0
        detected_fallbacks = 0
        for line in lines:
            lowered = line.lower()
            if re.search(r"(?:action\s+)?cache\s+hit", lowered):
                detected_hits += 1
            if re.search(r"(?:action\s+)?cache\s+miss", lowered):
                detected_misses += 1
            if re.search(
                r"local\s+(?:compile\s+)?fallback|fallback\s+to\s+local|falling\s+back\s+to\s+local",
                lowered,
            ):
                detected_fallbacks += 1
            if re.search(r"\baction\b.*\b(?:started|submitted|executed)\b", lowered):
                detected_started_actions += 1
            elif re.search(r"\baction\b.*\b(?:completed|finished)\b", lowered):
                detected_completed_actions += 1

            for field, label in (("compile_seconds", "compile"), ("link_seconds", "link")):
                if values[field] is None:
                    match = re.search(
                        rf"\b{label}\b(?:\s+(?:time|duration))?\s*(?::|=|took)\s*([^\n,]+)",
                        line,
                        re.IGNORECASE,
                    )
                    if match:
                        values[field] = parse_duration(match.group(1))
        if values["action_count"] is None:
            detected_actions = detected_started_actions or detected_completed_actions
            if detected_actions:
                values["action_count"] = detected_actions
        if values["cache_hits"] is None and detected_hits:
            values["cache_hits"] = detected_hits
        if values["cache_misses"] is None and detected_misses:
            values["cache_misses"] = detected_misses
        if values["local_fallbacks"] is None and detected_fallbacks:
            values["local_fallbacks"] = detected_fallbacks

    unavailable = {
        field: f"RECC_VERBOSE output did not contain {field.replace('_', ' ')} evidence"
        for field, value in values.items()
        if value is None
    }
    if marker_reason:
        unavailable = {
            field: f"{reason}; {detail}"
            for field, detail in unavailable.items()
            for reason in [marker_reason]
        }
    state, reason = _section_status(
        values, unavailable, "no RECC_VERBOSE evidence was found"
    )
    if marker_reason and state == "unavailable":
        reason = marker_reason
    return {
        **values,
        "state": state,
        "state_reason": reason,
        "unavailable_fields": unavailable,
    }


PROMETHEUS_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)
PROMETHEUS_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')


def _parse_prometheus_samples(text: str | None) -> dict[str, list[dict[str, Any]]]:
    samples: dict[str, list[dict[str, Any]]] = {}
    if not text:
        return samples
    for line in text.splitlines():
        match = PROMETHEUS_SAMPLE.match(line.strip())
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = {
            key: bytes(raw, "utf-8").decode("unicode_escape")
            for key, raw in PROMETHEUS_LABEL.findall(match.group("labels") or "")
        }
        samples.setdefault(match.group("name"), []).append(
            {"labels": labels, "value": value}
        )
    return samples


def discover_metric_names(text: str | None) -> list[str]:
    """Discover metric families present in a Prometheus text snapshot."""

    return sorted(_parse_prometheus_samples(text))


def parse_prometheus_snapshot(
    text: str | None, metric_names: Iterable[str] | None = None
) -> dict[str, Any]:
    samples = _parse_prometheus_samples(text)
    requested = sorted(set(metric_names)) if metric_names else sorted(samples)
    missing = [name for name in requested if name not in samples]
    if not text:
        return {
            "metrics": {},
            "metric_names": requested,
            "state": "unavailable",
            "state_reason": "Prometheus snapshot was not supplied",
            "unavailable_metrics": requested,
        }
    if requested and not any(name in samples for name in requested):
        return {
            "metrics": {},
            "metric_names": requested,
            "state": "unavailable",
            "state_reason": "none of the requested Prometheus metrics were present",
            "unavailable_metrics": requested,
        }
    if not samples:
        return {
            "metrics": {},
            "metric_names": requested,
            "state": "unavailable",
            "state_reason": "Prometheus snapshot contained no parseable samples",
            "unavailable_metrics": requested,
        }
    return {
        "metrics": {name: samples[name] for name in requested if name in samples},
        "metric_names": requested,
        "state": "available",
        "state_reason": None,
        "unavailable_metrics": missing,
    }


def _sample_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def _sum_metric_deltas(
    metrics: dict[str, list[dict[str, Any]]],
    name: str,
    predicate: Any = None,
) -> float | None:
    rows = metrics.get(name, [])
    values = [
        row["delta"]
        for row in rows
        if row.get("delta") is not None
        and (predicate is None or predicate(row.get("labels", {})))
    ]
    return sum(values) if values else None


def _metric_group(
    delta: dict[str, Any],
    names: Iterable[str],
    *,
    group: str,
) -> dict[str, Any] | None:
    requested = [name for name in names if name in delta["metric_names"]]
    if not requested:
        return None
    metrics = {name: delta["metrics"].get(name, []) for name in requested}
    available = any(
        row["state"] == "available"
        for rows in metrics.values()
        for row in rows
    )
    unavailable = {
        name: delta["unavailable_metrics"][name]
        for name in requested
        if name in delta["unavailable_metrics"]
    }
    result: dict[str, Any] = {
        "group": group,
        "metric_names": requested,
        "metrics": metrics,
        "state": "available" if available else "unavailable",
        "state_reason": (
            None
            if available
            else f"no {group} metric samples were present in both snapshots"
        ),
        "unavailable_metrics": unavailable,
        "unavailable_fields": {},
    }
    if group == "worker":
        result["summary"] = {
            "actions_executed": _sum_metric_deltas(
                metrics,
                "buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_count",
            ),
            "queue_seconds": _sum_metric_deltas(
                metrics,
                "buildbarn_builder_in_memory_build_queue_tasks_queued_duration_seconds_sum",
            ),
            "execution_seconds": _sum_metric_deltas(
                metrics,
                "buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_sum",
            ),
        }
    else:
        def operation_is(operation: str):
            return lambda labels: labels.get("operation") == operation

        result["summary"] = {
            "get_requests": _sum_metric_deltas(
                metrics,
                "buildbarn_blobstore_blob_access_operations_blob_size_bytes_count",
                operation_is("Get"),
            ),
            "put_requests": _sum_metric_deltas(
                metrics,
                "buildbarn_blobstore_blob_access_operations_blob_size_bytes_count",
                operation_is("Put"),
            ),
            "get_bytes": _sum_metric_deltas(
                metrics,
                "buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum",
                operation_is("Get"),
            ),
            "put_bytes": _sum_metric_deltas(
                metrics,
                "buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum",
                operation_is("Put"),
            ),
        }
    for field, value in result["summary"].items():
        if value is None:
            result["unavailable_fields"][f"summary.{field}"] = (
                f"the selected BuildBarn {group} metrics did not provide {field}"
            )
    return result


def prometheus_delta(
    before_text: str | None,
    after_text: str | None,
    metric_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    before = _parse_prometheus_samples(before_text)
    after = _parse_prometheus_samples(after_text)
    if not metric_names:
        names = sorted(set(before) | set(after))
    else:
        names = sorted(set(metric_names))

    metrics: dict[str, list[dict[str, Any]]] = {}
    unavailable: dict[str, str] = {}
    for name in names:
        before_by_label = {
            _sample_key(sample["labels"]): sample for sample in before.get(name, [])
        }
        after_by_label = {
            _sample_key(sample["labels"]): sample for sample in after.get(name, [])
        }
        keys = sorted(set(before_by_label) | set(after_by_label))
        if not keys:
            unavailable[name] = "metric was not present in both snapshots"
            continue
        rows = []
        for key in keys:
            before_sample = before_by_label.get(key)
            after_sample = after_by_label.get(key)
            row: dict[str, Any] = {
                "labels": dict(key),
                "before": before_sample["value"] if before_sample else None,
                "after": after_sample["value"] if after_sample else None,
                "delta": (
                    after_sample["value"] - before_sample["value"]
                    if before_sample and after_sample
                    else None
                ),
                "state": "available" if before_sample and after_sample else "unavailable",
                "state_reason": (
                    None
                    if before_sample and after_sample
                    else "metric sample was missing from one snapshot"
                ),
            }
            rows.append(row)
        metrics[name] = rows

    available = any(
        sample["state"] == "available" for rows in metrics.values() for sample in rows
    )
    snapshot_reason = (
        "worker and CAS deltas require explicit before/after metric snapshots"
        if not before_text or not after_text
        else "worker and CAS metric families were not explicitly identified; generic deltas are retained without classification"
    )
    result = {
        "metrics": metrics,
        "metric_names": names,
        "state": "available" if available else "unavailable",
        "state_reason": (
            None if available else "no metric samples were present in both snapshots"
        ),
        "unavailable_metrics": unavailable,
        "worker_deltas": None,
        "cas_deltas": None,
        "unavailable_fields": {
            "worker_deltas": snapshot_reason,
            "cas_deltas": snapshot_reason,
        },
    }
    result["worker_deltas"] = _metric_group(
        result, BUILDBARN_WORKER_METRICS, group="worker"
    )
    result["cas_deltas"] = _metric_group(
        result, BUILDBARN_CAS_METRICS, group="cas"
    )
    return result


def collect_run(
    metadata: Any = None,
    *,
    buildstream_output: str | None = None,
    recc_verbose: str | None = None,
    prometheus_before: str | None = None,
    prometheus_after: str | None = None,
    metric_names: Iterable[str] | None = None,
    source_url: str | None = None,
    collected_at: Any = None,
) -> dict[str, Any]:
    """Assemble one parsed run record without retaining raw artifacts."""

    run = parse_run_metadata(metadata)
    structured_buildbarn: dict[str, Any] = {}
    if isinstance(metadata, dict):
        if buildstream_output is None:
            buildstream_output = metadata.get("buildstream")
        if recc_verbose is None:
            recc_verbose = metadata.get("recc")
        if isinstance(metadata.get("buildbarn"), dict):
            structured_buildbarn = metadata["buildbarn"]
    buildstream = parse_buildstream_output(
        buildstream_output,
        expected_phases=run.get("phases", {}).keys()
        if isinstance(run.get("phases"), dict)
        else None,
    )
    recc = parse_recc_verbose(recc_verbose)
    before = parse_prometheus_snapshot(prometheus_before, metric_names)
    after = parse_prometheus_snapshot(prometheus_after, metric_names)
    buildbarn_delta = prometheus_delta(
        prometheus_before, prometheus_after, metric_names
    )
    for field, aliases in (
        ("worker_deltas", ("worker_deltas", "worker")),
        ("cas_deltas", ("cas_deltas", "cas")),
    ):
        value = _first(structured_buildbarn, *aliases)
        if value is not None:
            buildbarn_delta[field] = value
            buildbarn_delta["unavailable_fields"].pop(field, None)

    collected = parse_timestamp(collected_at) or dt.datetime.now(dt.timezone.utc)
    sections = (run, buildstream, recc, buildbarn_delta)
    available = any(section["state"] == "available" for section in sections)
    return {
        "schema_version": SCHEMA_VERSION,
        "run": run,
        "buildstream": buildstream,
        "recc": recc,
        "buildbarn": {
            "before": before,
            "after": after,
            "delta": buildbarn_delta,
            "state": buildbarn_delta["state"],
            "state_reason": buildbarn_delta["state_reason"],
        },
        "source_url": source_url,
        "collected_at": format_timestamp(collected),
        "derivation": (
            "Parsed caller-supplied run metadata, BuildStream output, "
            "RECC_VERBOSE output, and before/after Prometheus text snapshots."
        ),
        "state": "available" if available else "unavailable",
        "state_reason": (
            None
            if available
            else "no parseable evidence was supplied for this run"
        ),
        "unavailable_fields": (
            {}
            if source_url
            else {"source_url": "no canonical source URL was supplied for local artifacts"}
        ),
    }


def _read_input(path: str | None) -> str | None:
    if not path:
        return None
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _metric_args(values: list[str]) -> list[str]:
    names = []
    for value in values:
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", value):
            raise ValueError(f"invalid Prometheus metric name: {value}")
        names.append(value)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", help="JSON or key/value metadata artifact")
    parser.add_argument("--buildstream-log", help="BuildStream output artifact")
    parser.add_argument("--recc-log", help="RECC_VERBOSE output artifact")
    parser.add_argument("--prometheus-before", help="Prometheus text snapshot before the run")
    parser.add_argument("--prometheus-after", help="Prometheus text snapshot after the run")
    parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="Prometheus metric family to compare; repeat for multiple metrics",
    )
    parser.add_argument("--source-url")
    parser.add_argument("--collected-at")
    parser.add_argument("--output", help="Write JSON here instead of stdout")
    args = parser.parse_args(argv)

    try:
        metric_names = _metric_args(args.metric)
        metadata_text = _read_input(args.metadata)
        metadata: Any = metadata_text
        if metadata_text:
            try:
                metadata = json.loads(metadata_text)
            except json.JSONDecodeError:
                pass
        record = collect_run(
            metadata,
            buildstream_output=_read_input(args.buildstream_log),
            recc_verbose=_read_input(args.recc_log),
            prometheus_before=_read_input(args.prometheus_before),
            prometheus_after=_read_input(args.prometheus_after),
            metric_names=metric_names or None,
            source_url=args.source_url,
            collected_at=args.collected_at,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    rendered = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
