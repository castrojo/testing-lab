# Dakota Build Trends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a provenance-preserving Dakota build trend dataset from raw `build-runs.ndjson`.

**Architecture:** Keep `docs/data/history/build-runs.ndjson` as the append-only source of truth. Add a pure aggregation helper to `publish_dakota_run.py`, have `generate_page_datasets.py` load and validate the raw Dakota records and write `docs/data/dakota-build-trends.json`, and document the contract.

**Tech Stack:** Python 3, pytest, JSON/NDJSON.

## Global Constraints

- Preserve raw append-only history.
- Never invent throughput or duration values.
- Every trend row and summary metric carries provenance and explicit state.
- Do not edit dashboard HTML.

### Task 1: Dakota trend aggregation

**Files:**
- Modify: `scripts/publish_dakota_run.py`
- Test: `tests/unit/test_publish_dakota_run.py`

- [ ] Write failing tests for daily throughput/duration aggregation and unavailable input.
- [ ] Run the focused tests and confirm failure because the aggregation API is absent.
- [ ] Implement a pure `build_trend_dataset(records, collected_at, window_days=180)` helper.
- [ ] Run focused tests until green.

### Task 2: Page dataset generation

**Files:**
- Modify: `scripts/generate_page_datasets.py`
- Test: `tests/unit/test_page_dataset_collector.py`

- [ ] Write failing tests proving the generated dataset is included and reads validated Dakota history.
- [ ] Run focused tests and confirm failure.
- [ ] Add deterministic raw-history loading and write `dakota-build-trends.json`.
- [ ] Run focused tests until green.

### Task 3: Contract documentation and generated artifact

**Files:**
- Modify: `docs/reference/page-contracts.md`
- Create: `docs/data/dakota-build-trends.json`

- [ ] Document the dataset shape, retention, provenance, and unavailable policy.
- [ ] Generate the checked-in artifact with a fixed collection timestamp.
- [ ] Run focused tests and inspect the generated JSON.
