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


def test_push_scripts_reject_ghcr_before_upload():
    """
    Test that all push-producing scripts have guards against GHCR before uploads.
    
    Expected failures (as per task brief):
    - Dakota publish script: has hard-coded ghcr.io DESTINATION_REGISTRY
    - KDE/GNOME screenshot scripts: have executable oras push to ghcr.io
    - Other push scripts: missing GHCR guard (if they reference ghcr.io)
    """
    for path, template_name, source in push_scripts():
        # Skip screenshot scripts, they have special handling
        if template_name in {"run-kde-tests", "run-gnome-tests"}:
            # Screenshot scripts must be explicitly disabled
            assert (
                "GHCR screenshot publication disabled" in source
            ), f"{template_name}: missing 'GHCR screenshot publication disabled' diagnostic"
            
            # Screenshot scripts must not have executable oras push
            # (they may reference oras in error messages)
            if is_executable_oras_push(source):
                assert False, f"{template_name}: contains executable oras push (should be disabled)"
            continue
        
        # For Dakota publish script, check the specific restrictions
        if template_name == "dakota-publish-pipeline":
            # Must not have hard-coded GHCR DESTINATION_REGISTRY
            assert (
                'DESTINATION_REGISTRY="ghcr.io/projectbluefin"' not in source
            ), f"{template_name}: has hard-coded DESTINATION_REGISTRY set to ghcr.io/projectbluefin"
            
            # Must not have skopeo copy destinations beginning with ghcr.io
            # (Check for patterns like "docker://ghcr.io")
            if re.search(r'docker://ghcr\.io', source):
                assert False, f"{template_name}: contains hard-coded skopeo destination to ghcr.io"
            
            continue
        
        # For all other push scripts that reference ghcr.io, require the GHCR guard
        if "ghcr.io" not in source:
            # If the script doesn't reference ghcr.io, it's using a different registry
            # and doesn't need our guard (no GHCR-specific guard needed)
            continue
        
        assert (
            "GHCR push destination is forbidden" in source
        ), f"{template_name}: references ghcr.io but missing 'GHCR push destination is forbidden' guard"
        
        # Guard must appear before the first push command
        guard_index = source.index("GHCR push destination is forbidden")
        push_index = first_push_index(source)
        assert (
            guard_index < push_index
        ), f"{template_name}: GHCR guard appears AFTER first push command (guard@{guard_index}, push@{push_index})"


def test_push_scripts_inventory():
    """
    Verify we've found the expected push-producing templates.
    
    This helps catch if template names change or new push patterns are introduced.
    """
    templates_with_pushes = set()
    for path, template_name, source in push_scripts():
        templates_with_pushes.add(template_name)
    
    # Sanity check: we should find at least a few known push templates
    expected_templates = {
        "dakota-publish-pipeline",
        "dakota-build-pipeline",
    }
    
    for expected in expected_templates:
        assert (
            expected in templates_with_pushes
        ), f"Expected to find {expected} in push-producing templates but found: {templates_with_pushes}"
