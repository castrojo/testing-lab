from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "argo/workflow-templates/knuckle-qa-pipeline.yaml"


def test_knuckle_build_git_clone_declares_quota_resources():
    content = PIPELINE.read_text(encoding="utf-8")
    git_clone = content.split("    - name: git-clone", 1)[1].split(
        "    container:", 1
    )[0]

    assert "requests:" in git_clone
    assert "cpu: 100m" in git_clone
    assert "memory: 128Mi" in git_clone
    assert "limits:" in git_clone
    assert "cpu: 500m" in git_clone
    assert "memory: 512Mi" in git_clone
