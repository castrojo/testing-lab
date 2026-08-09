#!/usr/bin/env python3
"""Validate lab documentation structure and internal links."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_MAX_LINES = 300
REFERENCE_MAX_LINES = 500
FORBIDDEN = [
    re.compile(r"jorge@"),
    re.compile(r"192\.168\."),
    re.compile(r"copilot-config"),
]


def skills() -> list[Path]:
    return list((ROOT / "docs" / "skills").glob("*/SKILL.md"))


def validate_frontmatter(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append(f"{path}: missing YAML frontmatter opener")
        return errors
    end = text.find("\n---\n", 4)
    if end == -1:
        errors.append(f"{path}: missing YAML frontmatter closer")
        return errors
    fm = text[4:end]
    if "name:" not in fm:
        errors.append(f"{path}: frontmatter missing 'name'")
    if "description:" not in fm:
        errors.append(f"{path}: frontmatter missing 'description'")
    return errors


def validate_size(path: Path, max_lines: int) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > max_lines:
        return [f"{path}: {len(lines)} lines exceeds {max_lines}"]
    return []


def collect_md_files() -> set[Path]:
    files: set[Path] = set()
    for p in (ROOT / "docs").rglob("*.md"):
        files.add(p.relative_to(ROOT))
    for p in ROOT.glob("*.md"):
        files.add(p.relative_to(ROOT))
    for p in (ROOT / "argo").rglob("*.md"):
        files.add(p.relative_to(ROOT))
    for p in (ROOT / ".github").rglob("*.md"):
        files.add(p.relative_to(ROOT))
    return files


LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def validate_links(files: set[Path]) -> list[str]:
    errors: list[str] = []
    md_paths = {f: ROOT / f for f in files}
    for p in md_paths:
        text = md_paths[p].read_text(encoding="utf-8")
        for m in LINK_RE.finditer(text):
            target = m.group(2).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            base = target.split("#", 1)[0]
            if not base:
                continue
            if base.startswith("/"):
                resolved = ROOT / base.lstrip("/")
            else:
                resolved = (ROOT / p).parent / base
            if not resolved.exists():
                errors.append(f"{p}: broken link to {target}")
    return errors


def find_forbidden(files: set[Path]) -> list[str]:
    hits: list[str] = []
    for p in files:
        text = (ROOT / p).read_text(encoding="utf-8", errors="ignore")
        for idx, line in enumerate(text.splitlines(), 1):
            for pat in FORBIDDEN:
                if pat.search(line):
                    hits.append(f"{p}:{idx}: forbidden pattern '{pat.pattern}'")
    return hits


def validate_router() -> list[str]:
    """Enforce the projectbluefin/common doc structure: AGENTS.md routes to
    docs/SKILL.md (the skill router), which indexes every skill."""
    errors: list[str] = []
    router = ROOT / "docs" / "SKILL.md"
    if not router.exists():
        return ["docs/SKILL.md: missing skill router (common doc structure)"]
    text = router.read_text(encoding="utf-8")
    for section in ("Read order", "Skill index"):
        if f"## {section}" not in text:
            errors.append(f"docs/SKILL.md: missing '## {section}' section")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if "docs/SKILL.md" not in agents:
        errors.append("AGENTS.md: must route skill discovery to docs/SKILL.md")
    for skill in skills():
        rel = skill.relative_to(router.parent)
        if rel.parts[0].startswith("_") or any(part.startswith("_") for part in rel.parts):
            continue  # underscore-prefixed dirs are templates, not live skills
        if f"]({rel})" not in text:
            errors.append(f"docs/SKILL.md: skill index does not list {rel}")
    return errors


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    for skill in skills():
        errors.extend(validate_frontmatter(skill))
        warnings.extend(f"{skill}: {len(skill.read_text(encoding='utf-8').splitlines())} lines exceeds {SKILL_MAX_LINES}"
                        for _ in [0] if len(skill.read_text(encoding="utf-8").splitlines()) > SKILL_MAX_LINES)

    files = collect_md_files()
    for ref_dir in (ROOT / "docs" / "reference", ROOT / "docs" / "ops"):
        if ref_dir.exists():
            for p in ref_dir.glob("*.md"):
                if p.name == "RUNBOOK.md":
                    continue
                warnings.extend(f"{p}: {len(p.read_text(encoding='utf-8').splitlines())} lines exceeds {REFERENCE_MAX_LINES}"
                                for _ in [0] if len(p.read_text(encoding="utf-8").splitlines()) > REFERENCE_MAX_LINES)

    errors.extend(validate_links(files))
    errors.extend(validate_router())
    warnings.extend(find_forbidden(files))

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  {w}")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("Documentation passes validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
