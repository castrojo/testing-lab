"""
Test that push-producing scripts have guards against GHCR destinations.

Regression coverage for the GHCR push restriction plan:
- All scripts containing push commands must guard against ghcr.io
- The guard must appear before the first push command
- Screenshot scripts must be explicitly disabled
"""

from pathlib import Path
import re
import yaml


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = ROOT / "argo/workflow-templates"


def load_yaml(path: Path):
    """Load a single YAML file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def collect_env_entries(template_path: Path) -> list[tuple[str, str, str, str, str]]:
    """
    Collect all env entries from script and container blocks in a template file.

    Returns:
        List of tuples: (template_path, file_stem, template_name, env_name, env_value)
    """
    results = []
    try:
        doc = load_yaml(template_path)
    except Exception:
        return results

    if not isinstance(doc, dict):
        return results

    file_stem = template_path.stem
    for tmpl in doc.get("spec", {}).get("templates", []):
        if not isinstance(tmpl, dict):
            continue
        tmpl_name = tmpl.get("name", "")
        for section in ("script", "container"):
            block = tmpl.get(section, {})
            if not block or not isinstance(block, dict):
                continue
            for env in block.get("env", []) or []:
                if isinstance(env, dict):
                    results.append((
                        str(template_path),
                        file_stem,
                        tmpl_name,
                        env.get("name", ""),
                        str(env.get("value", "")),
                    ))

    return results


def collect_script_sources(template_path: Path, template_name: str) -> list[tuple[str, str, str]]:
    """
    Recursively collect script sources from a template.
    
    Returns:
        List of tuples: (template_path, template_name, script_source)
    """
    results = []
    try:
        doc = load_yaml(template_path)
    except Exception:
        return results
    
    if not isinstance(doc, dict):
        return results
    
    spec = doc.get("spec", {})
    templates = spec.get("templates", [])
    
    for template in templates:
        if not isinstance(template, dict):
            continue
        
        # Check for script.source
        if "script" in template and isinstance(template["script"], dict):
            source = template["script"].get("source")
            if source and isinstance(source, str):
                results.append((str(template_path), template_name, source))
        
        # Also check for container.args and container.command
        if "container" in template and isinstance(template["container"], dict):
            container = template["container"]
            args = container.get("args")
            if args and isinstance(args, list):
                # args can be a list; join them if so
                source = "\n".join(str(arg) for arg in args)
                if source:
                    results.append((str(template_path), template_name, source))
            
            # Check container.command as well
            command = container.get("command")
            if command and isinstance(command, list):
                source = "\n".join(str(c) for c in command)
                if source:
                    results.append((str(template_path), template_name, source))
    
    return results


def push_scripts() -> list[tuple[str, str, str]]:
    """
    Find all templates with push commands.
    
    Yields:
        Tuples of (path, template_name, script_source) for scripts containing
        push commands: skopeo copy, podman push, buildah push, oras push, oras cp
    """
    push_patterns = [
        r"skopeo\s+copy",
        r"podman\s+push",
        r"buildah\s+push",
        r"oras\s+push",
        r"oras\s+cp",
    ]
    combined_pattern = "|".join(f"({p})" for p in push_patterns)
    
    for template_file in sorted(TEMPLATES_PATH.glob("*.yaml")):
        template_name = template_file.stem
        
        for path, name, source in collect_script_sources(template_file, template_name):
            if re.search(combined_pattern, source):
                yield path, template_name, source


def first_push_index(source: str) -> int:
    """
    Find the index of the first push command in the script source.
    
    Returns:
        The character index of the first occurrence of any push command.
    """
    push_patterns = [
        r"skopeo\s+copy",
        r"podman\s+push",
        r"buildah\s+push",
        r"oras\s+push",
        r"oras\s+cp",
    ]
    
    min_index = len(source)
    for pattern in push_patterns:
        match = re.search(pattern, source)
        if match:
            min_index = min(min_index, match.start())
    
    return min_index


def is_executable_oras_push(source: str) -> bool:
    """
    Check if the script contains an executable oras push command.
    
    A command is executable if it is not within a conditional block that
    would prevent its execution (e.g., if [ ... ]; then ... fi).
    
    For simplicity, we look for the pattern "oras push" not preceded by
    echo or other diagnostic statements that might be within conditionals.
    """
    # Simple heuristic: look for oras push that's not just in an echo or diagnostic
    # Check for actual invocation (not in quotes or echo)
    lines = source.split("\n")
    for line in lines:
        stripped = line.strip()
        # Skip comments and echo statements
        if stripped.startswith("#") or stripped.startswith("echo"):
            continue
        # Look for actual oras push invocation
        if re.search(r"oras\s+push(?!\s+--recursive)", stripped):
            # Make sure it's not commented out or in a conditional that's disabled
            if not stripped.startswith("#"):
                return True
    return False


def test_script_env_no_ghcr_destinations():
    """
    Test that no script/container env blocks hard-code a ghcr.io destination.

    This catches cases where DESTINATION_REGISTRY (or similar) is set to a
    ghcr.io value in the YAML env: stanza rather than inline in script source,
    which source-text checks would miss entirely.
    """
    for template_file in sorted(TEMPLATES_PATH.glob("*.yaml")):
        for path, file_stem, tmpl_name, env_name, env_value in collect_env_entries(template_file):
            assert "ghcr.io" not in env_value, (
                f"{file_stem}::{tmpl_name}: env '{env_name}' hard-codes "
                f"ghcr.io destination: '{env_value}'"
            )


def test_push_scripts_reject_ghcr_before_upload():
    """
    Test that all push-producing scripts have a GHCR rejection guard before uploads.

    Every script that invokes skopeo copy / podman push / buildah push / oras push
    / oras cp must contain the sentinel string 'GHCR push destination is forbidden'
    BEFORE its first push command, regardless of whether the script source contains
    a literal 'ghcr.io' reference.  Destination variables (e.g. $DESTINATION_REGISTRY)
    can be redirected at call time, so the guard is required unconditionally.

    The only exemptions are the screenshot scripts (run-kde-tests, run-gnome-tests),
    which must instead carry an explicit 'GHCR screenshot publication disabled'
    diagnostic and must not contain an executable oras push block.
    """
    for path, template_name, source in push_scripts():
        if template_name in {"run-kde-tests", "run-gnome-tests"}:
            assert (
                "GHCR screenshot publication disabled" in source
            ), f"{template_name}: missing 'GHCR screenshot publication disabled' diagnostic"

            if is_executable_oras_push(source):
                assert False, f"{template_name}: contains executable oras push (should be disabled)"
            continue

        # All other push-producing scripts need the guard — independently of whether
        # 'ghcr.io' appears literally in the source, because the destination may be
        # supplied via a variable that could be set to ghcr.io at invocation time.
        assert (
            "GHCR push destination is forbidden" in source
        ), (
            f"{template_name}: push-producing script missing "
            f"'GHCR push destination is forbidden' guard"
        )

        guard_index = source.index("GHCR push destination is forbidden")
        push_index = first_push_index(source)
        assert (
            guard_index < push_index
        ), (
            f"{template_name}: GHCR guard appears AFTER first push command "
            f"(guard@{guard_index}, push@{push_index})"
        )


def test_push_scripts_inventory():
    """
    Verify we've found the expected push-producing templates.
    
    This helps catch if template names change or new push patterns are introduced.
    dakota-publish-pipeline has no live push commands — it is explicitly disabled;
    validate that invariant directly instead.
    """
    templates_with_pushes = set()
    for path, template_name, source in push_scripts():
        templates_with_pushes.add(template_name)
    
    # Sanity check: we should find at least a few known push templates
    expected_templates = {
        "dakota-build-pipeline",
    }
    
    for expected in expected_templates:
        assert (
            expected in templates_with_pushes
        ), f"Expected to find {expected} in push-producing templates but found: {templates_with_pushes}"

    # dakota-publish-pipeline has been explicitly disabled — it must have no live push
    # commands and must carry the explicit disable guard in publish-lane.
    assert "dakota-publish-pipeline" not in templates_with_pushes, (
        "dakota-publish-pipeline unexpectedly has live push commands; "
        "it should be disabled with an explicit 'GHCR push destination is forbidden' guard"
    )
    publish_pipeline = TEMPLATES_PATH / "dakota-publish-pipeline.yaml"
    doc = load_yaml(publish_pipeline)
    publish_lane = next(
        (t for t in doc["spec"]["templates"] if t.get("name") == "publish-lane"),
        None,
    )
    assert publish_lane is not None, "publish-lane template not found in dakota-publish-pipeline"
    lane_source = publish_lane["script"]["source"]
    assert "GHCR push destination is forbidden" in lane_source, (
        "publish-lane is missing the 'GHCR push destination is forbidden' explicit disable guard"
    )
    assert re.search(r"exit\s+1", lane_source), (
        "publish-lane is missing exit 1 after the disable guard"
    )
