from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_kde_source_tracker_template_exists_and_follows_naming():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    assert template["metadata"]["name"] == "kde-source-tracker"
    assert template["spec"]["entrypoint"] == "track"

    templates = {item["name"]: item for item in template["spec"]["templates"]}
    assert {"track", "resolve-source", "update-state", "record-history"} <= set(templates)


def test_kde_source_tracker_defaults_target_kde_linux_mr_534():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    params = {
        item["name"]: item.get("value")
        for item in template["spec"]["arguments"]["parameters"]
    }
    assert params["gitlab-host"] == "invent.kde.org"
    assert params["project"] == "kde-linux/kde-linux"
    assert params["mr-iid"] == "534"
    assert params["target-branch"] == "master"
    assert params["state-key"] == "kde-mr-534"
    assert params["state-configmap"] == "kde-source-state"
    assert params["force"] == "false"
    assert params["history-limit"] == "100"


def test_resolve_source_polls_gitlab_mr_and_validates_sha():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["resolve-source"]["script"]["source"]

    assert "api/v4/projects/${PROJECT_ENC}" in source
    assert '"${API_BASE}/merge_requests/${MR_IID}"' in source
    assert "PRIVATE-TOKEN: ${GITLAB_TOKEN}" in source
    assert '[[ "$MR_SHA" =~ ^[0-9a-f]{40}$ ]]' in source
    assert '[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]' in source
    assert "jq -r '.sha //" in source
    assert "set -euo pipefail" in source
    # Never enable tracing for authenticated API calls.
    assert "set -x" not in source


def test_tracker_switches_to_master_after_merge():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["resolve-source"]["script"]["source"]

    assert '[[ "$MR_STATE" == "merged" ]]' in source
    assert "SOURCE_MODE=\"master-head\"" in source
    assert "SOURCE_MODE=\"mr-head\"" in source
    assert "api/v4/projects/${PROJECT_ENC}" in source
    assert '"${API_BASE}/repository/branches/${TARGET_BRANCH}"' in source
    assert "jq -r '.commit.id //" in source


def test_update_state_rejects_stale_writes():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["update-state"]["script"]["source"]

    assert "State changed since admission" in source
    assert "kubectl patch configmap" in source
    assert source.index("State changed since admission") < source.index("kubectl patch configmap")


def test_record_history_appends_bounded_log():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["record-history"]["script"]["source"]

    assert "source_sha" in source
    assert "source_mode" in source
    assert "mr_state" in source
    assert "mr_url" in source
    assert "tail -n \"$LIMIT\"" in source
    assert "kubectl patch configmap" in source


def test_track_dag_records_history_only_after_successful_state_update():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    tasks = {item["name"]: item for item in templates["track"]["dag"]["tasks"]}

    assert tasks["resolve-source"]["template"] == "resolve-source"
    assert tasks["update-state"]["depends"] == "resolve-source.Succeeded"
    assert tasks["update-state"]["when"] == "{{tasks.resolve-source.outputs.parameters.changed}} == true"
    assert tasks["record-history"]["depends"] == "update-state.Succeeded"


def test_kde_source_tracker_cron_is_scheduled_and_not_suspended():
    cron = load("manifests/kde-source-tracker.yaml")
    assert cron["metadata"]["name"] == "kde-source-tracker"
    assert cron["spec"]["suspend"] is False
    assert cron["spec"]["schedules"] == ["9/10 * * * *"]
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["workflowSpec"]["entrypoint"] == "track"
    assert cron["spec"]["workflowSpec"]["workflowTemplateRef"]["name"] == "kde-source-tracker"
    assert cron["spec"]["workflowMetadata"]["labels"]["bluefin.io/source-tracker"] == "true"

    arguments = {
        item["name"]: item["value"]
        for item in cron["spec"]["workflowSpec"]["arguments"]["parameters"]
    }
    assert arguments["project"] == "kde-linux/kde-linux"
    assert arguments["mr-iid"] == "534"
    assert arguments["force"] == "false"


def test_justfile_includes_force_kde_source_poll():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    assert "force-kde-source-poll:" in justfile
    assert "--from cronworkflow/kde-source-tracker" in justfile
    assert "-p force=true" in justfile


def test_kde_source_tracker_declares_resource_limits():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    for item in template["spec"]["templates"]:
        script = item.get("script")
        if not script:
            continue
        assert "resources" in script
        assert "requests" in script["resources"]
        assert "limits" in script["resources"]


def test_kde_source_tracker_uses_allowlisted_image():
    template = load("argo/workflow-templates/kde-source-tracker.yaml")
    for item in template["spec"]["templates"]:
        image = item.get("script", {}).get("image", "")
        if image:
            assert image.startswith("ghcr.io/projectbluefin/lab-runner")
