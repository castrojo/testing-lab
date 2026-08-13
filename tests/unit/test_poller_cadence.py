from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_manifest(name: str) -> dict:
    return yaml.safe_load((ROOT / "manifests" / name).read_text(encoding="utf-8"))


def test_stable_image_pollers_are_suspended_by_default():
    for name, schedule in (
        ("image-poll-bluefin-stable.yaml", "4/10 * * * *"),
        ("image-poll-lts-stable.yaml", "6/10 * * * *"),
    ):
        spec = load_manifest(name)["spec"]
        assert spec["suspend"] is True
        assert spec["schedules"] == [schedule]


def test_active_testing_and_dakota_pollers_keep_staggered_freshness_checks():
    expected = {
        "image-poll-bluefin-testing.yaml": "0/10 * * * *",
        "image-poll-lts-testing.yaml": "2/10 * * * *",
        "image-poll-dakota.yaml": "8/10 * * * *",
    }

    for name, schedule in expected.items():
        manifest = load_manifest(name)
        spec = manifest["spec"]
        parameters = {
            parameter["name"]: parameter.get("value")
            for parameter in spec["workflowSpec"]["arguments"]["parameters"]
        }

        assert spec["suspend"] is False
        assert spec["schedules"] == [schedule]
        assert parameters["run-qa"] == "false"


def test_testing_nightly_workflows_remain_the_daily_qa_path():
    expected = {
        "nightly-smoke.yaml": ("0 2 * * *", "bluefin"),
        "nightly-smoke-lts.yaml": ("30 2 * * *", "bluefin-lts"),
        "nightly-dakota.yaml": ("0 3 * * *", "dakota"),
    }

    for name, (schedule, variant) in expected.items():
        spec = load_manifest(name)["spec"]
        parameters = {
            parameter["name"]: parameter.get("value")
            for parameter in spec["workflowSpec"]["arguments"]["parameters"]
        }

        assert spec["suspend"] is False
        assert spec["schedules"] == [schedule]
        assert parameters["variant"] == variant
