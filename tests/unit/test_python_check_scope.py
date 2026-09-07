"""Enforces the Python check scope declared in .python-scope.

.python-scope is the single source of truth for which trees of first-party
Python this repo verifies. This module is what makes it real: it compiles every
.py file in every declared tree, so the scope is enforced by the unit gate that
CI already runs (`pytest tests/unit/`) rather than by a tree list restated
inside each workflow.

Background: ci.yml's paths filter claimed `scripts/**` mattered, but no step in
either workflow ever looked at scripts/ -- lint.yaml syntax-checked only
`find tests/` and ci.yml linted only `ruff check tests/`. A syntax error in any
of the 20 modules under scripts/ merged green. test_every_scoped_file_compiles
below is the gate that closes it.
"""

import py_compile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPE_FILE = ROOT / ".python-scope"

# Directory names that never hold first-party Python.
EXCLUDED_DIRS = {".git", "node_modules", ".ruff_cache", "__pycache__", ".venv", "venv"}


def _scope_trees() -> list[str]:
    lines = SCOPE_FILE.read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def _python_files(tree: Path) -> list[Path]:
    return [p for p in sorted(tree.rglob("*.py")) if not EXCLUDED_DIRS & set(p.parts)]


def test_scope_file_exists_and_lists_real_trees():
    assert SCOPE_FILE.is_file(), ".python-scope is missing"

    trees = _scope_trees()
    assert trees, ".python-scope declares no trees"

    for tree in trees:
        path = ROOT / tree
        assert path.is_dir(), f".python-scope names '{tree}' which is not a directory"
        assert _python_files(path), f".python-scope names '{tree}' but it holds no .py files"


def test_scripts_tree_is_in_scope():
    """scripts/ backs Argo templates, docs datasets and the factory dashboards."""
    assert "scripts" in _scope_trees(), "scripts/ must be declared in .python-scope"


def test_every_scoped_file_compiles():
    """The syntax gate. Fails on any unparseable .py in any declared tree."""
    failures = []

    with tempfile.TemporaryDirectory() as cache:
        for tree in _scope_trees():
            for path in _python_files(ROOT / tree):
                try:
                    py_compile.compile(
                        str(path),
                        cfile=str(Path(cache) / (path.stem + ".pyc")),
                        doraise=True,
                    )
                except py_compile.PyCompileError as exc:
                    failures.append(f"{path.relative_to(ROOT)}: {str(exc).strip()}")

    assert not failures, "Python files failed to compile:\n" + "\n".join(failures)


def test_every_tree_with_first_party_python_is_in_scope():
    """Fails when a new tree of Python is added without declaring it."""
    trees = set(_scope_trees())
    missing = []

    for child in sorted(ROOT.iterdir()):
        if not child.is_dir() or child.name in EXCLUDED_DIRS or child.name.startswith("."):
            continue
        if child.name in trees:
            continue
        found = _python_files(child)
        if found:
            missing.append(f"{child.name}/ ({len(found)} .py files)")

    assert not missing, (
        "these trees contain first-party Python but are not declared in .python-scope: "
        + ", ".join(missing)
    )


def test_workflow_syntax_check_does_not_exceed_the_declared_scope():
    """A workflow may check a subset of the scope, never a tree outside it.

    lint.yaml still hardcodes `find tests/` today (rewiring it to read
    .python-scope needs a token with `workflows` permission -- see #693). That
    is safe only while the tree it names is inside the declared scope; this test
    fails the moment the two diverge.
    """
    lint_yaml = (ROOT / ".github/workflows/lint.yaml").read_text(encoding="utf-8")
    trees = set(_scope_trees())

    if ".python-scope" in lint_yaml:
        return  # already reading the single source of truth

    hardcoded = [t for t in ("tests", "scripts") if f"find {t}/ -name '*.py'" in lint_yaml]
    outside = [t for t in hardcoded if t not in trees]

    assert not outside, (
        "lint.yaml syntax-checks tree(s) not declared in .python-scope: " + ", ".join(outside)
    )


def test_justfile_check_python_uses_the_scope_file():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert "check-python:" in justfile, "Justfile must expose a check-python recipe"
    recipe = justfile.split("check-python:", 1)[1]
    assert ".python-scope" in recipe, "just check-python must read .python-scope"


def test_coverage_scope_stays_distinct_from_check_scope():
    """.coveragerc is intentionally narrower; keep that documented, not accidental."""
    coveragerc = (ROOT / ".coveragerc").read_text(encoding="utf-8")

    assert ".python-scope" in coveragerc, (
        ".coveragerc must document why its scope differs from .python-scope"
    )
