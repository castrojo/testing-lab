# QA-run evidence contract

`docs/data/history/qa-runs.ndjson` is an append-only, lab-sourced history of
QA Workflow snapshots. Each `qa-run` record conforms to
`schemas/v2/qa-run.schema.json` and has a deterministic `snapshot_id`; the
publisher never replaces an existing record. A workflow therefore produces a
running snapshot and, later, a terminal snapshot when its observable evidence
changes.

The record captures the Workflow UID/name, creation/start/finish/observation
timestamps, lane and selected suite, image reference and digest availability,
result and screenshot availability, terminal phase, failure state, and the
cluster Workflow identity. Every artifact has an explicit availability state
and provenance. Missing image digests or result outputs remain `unavailable`; they are never
inferred. Runner `result` output parameters are summaries derived from parsed
structured results and have no fallback default. A non-empty, validated
`failed-scenarios` output may independently establish that result evidence is
available when the summary is absent, but it never changes the Workflow's
terminal failure state or implies execution success.

When a runner exposes its `failed-scenarios` result output, the publisher may
add `artifacts.results.failed_scenarios`: at most 20 unique, bounded scenario
names. It reads only that JSON result artifact, rejects malformed or
sensitive-looking values, and never exports raw errors, logs, or stack traces.
Absent scenario output stays absent rather than being inferred.

`qa-run-reconciler` is a GitOps-managed CronWorkflow. It reads only Workflows
explicitly labelled `bluefin.io/evidence-contract=qa-run-v1`, then uses the
existing `github-token` secret only when present to append validated records.
The exported `workflow_url` is always `null`: Argo is private, so records link
only to their public Git history and never contain cluster addresses or
credentials. Add the label through a QA WorkflowTemplate's
`spec.workflowMetadata`; do not broaden the reconciler selector.
