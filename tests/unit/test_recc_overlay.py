import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/apply_recc_overlay.py"
SPEC = importlib.util.spec_from_file_location("apply_recc_overlay", MODULE_PATH)
OVERLAY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = OVERLAY
SPEC.loader.exec_module(OVERLAY)


# Lanes that must fail closed rather than build without nested RECC.
MANDATORY_KINDS = ("dakota", "cosmic", "bluefin-server", "bst-qa")
# The only operator-driven fixture allowed to use --pilot-cache-only.
PILOT_KINDS = ("bst-prototype",)
PILOT_PROVIDER = "components/buildbox.bst"
SDK_PROVIDER = "freedesktop-sdk.bst:components/buildbox.bst"
# Adapters with no pinned recc provider of their own.
PROVIDERLESS_KINDS = ("bst-prototype", "bst-qa")


def _apply(checkout: Path, kind: str, **kwargs):
    """Apply an overlay with the minimum contract each adapter requires."""

    if kind in MANDATORY_KINDS:
        kwargs.setdefault("runner_capability", True)
    else:
        kwargs.setdefault("pilot_cache_only", True)
    if kind in PROVIDERLESS_KINDS:
        kwargs.setdefault("recc_provider", PILOT_PROVIDER)
    return OVERLAY.apply_overlay(checkout, project_kind=kind, **kwargs)


def _checkout(tmp_path: Path, kind: str) -> Path:
    checkout = tmp_path / kind
    (checkout / "elements").mkdir(parents=True)
    (checkout / "include").mkdir()
    (checkout / "project.conf").write_text(
        """\
name: sample
min-version: 2.5
element-path: elements

(@):
  - include/aliases.yml

options:
  arch:
    type: arch
    values:
    - x86_64
""",
        encoding="utf-8",
    )
    if kind in {"bst-prototype", "bst-qa"}:
        project = checkout / "project.conf"
        project.write_text(
            project.read_text().replace("min-version: 2.5", "min-version: 2.0"),
            encoding="utf-8",
        )
    (checkout / "elements/freedesktop-sdk.bst").write_text(
        "kind: junction\n", encoding="utf-8"
    )
    if kind in {"dakota", "bluefin-server"}:
        (checkout / "elements/gnome-build-meta.bst").write_text(
            "kind: junction\n", encoding="utf-8"
        )
    if kind == "dakota":
        root = checkout / "elements/oci/layers"
        root.mkdir(parents=True)
        (root / "bluefin.bst").write_text(
            "kind: manual\n\nbuild-depends:\n"
            "- freedesktop-sdk.bst:components/gcc.bst\n",
            encoding="utf-8",
        )
        (root / "bluefin-nvidia.bst").write_text(
            "kind: manual\n\nbuild-depends:\n"
            "- freedesktop-sdk.bst:components/gcc.bst\n",
            encoding="utf-8",
        )
    elif kind == "cosmic":
        root = checkout / "elements/core"
        root.mkdir(parents=True)
        (root / "cosmic-comp.bst").write_text(
            "kind: manual\n\nbuild-depends:\n"
            "- freedesktop-sdk.bst:components/gcc-base.bst\n",
            encoding="utf-8",
        )
        (root / "cosmic-files.bst").write_text(
            "kind: manual\n\nbuild-depends:\n"
            "- freedesktop-sdk.bst:components/gcc-base.bst\n",
            encoding="utf-8",
        )
        (checkout / "elements/core-deps").mkdir()
        (checkout / "elements/core-deps/runtime-only.bst").write_text(
            "kind: manual\n\nruntime-depends:\n"
            "- freedesktop-sdk.bst:components/gcc.bst\n",
            encoding="utf-8",
        )
    elif kind == "bluefin-server":
        root = checkout / "elements/oci"
        root.mkdir(parents=True)
        for name in ("bluefin-server-ddi.bst", "bluefin-server-installer.bst"):
            (root / name).write_text(
                "kind: script\n\nbuild-depends:\n"
                "- freedesktop-sdk.bst:components/zstd.bst\n",
                encoding="utf-8",
            )
    elif kind in {"bst-prototype", "bst-qa"}:
        (checkout / "elements/components").mkdir()
        (checkout / "elements/components/buildbox.bst").write_text(
            "kind: cmake\n", encoding="utf-8"
        )
        (checkout / "elements/recc-baseline.bst").write_text(
            "kind: manual\n\n"
            'variables:\n  compiler: "recc /usr/bin/g++"\n',
            encoding="utf-8",
        )
    return checkout


@pytest.mark.parametrize(
    "kind",
    ("dakota", "cosmic", "bluefin-server", "bst-prototype", "bst-qa"),
)
def test_overlay_supports_all_lab_project_adapters(tmp_path, kind):
    checkout = _checkout(tmp_path, kind)

    diagnostics = _apply(checkout, kind)

    assert diagnostics["overlay_version"] == OVERLAY.OVERLAY_VERSION
    if kind in MANDATORY_KINDS:
        assert diagnostics["remote_execution_policy"] == "remote-execution"
        assert diagnostics["nested_socket"] == "enabled"
    else:
        assert diagnostics["remote_execution_policy"] == "cache-only"
        assert diagnostics["nested_socket"] == "disabled"
    expected_provider = (
        PILOT_PROVIDER if kind in PROVIDERLESS_KINDS else SDK_PROVIDER
    )
    assert diagnostics["recc_provider"] == expected_provider
    if kind == "bst-prototype":
        assert (
            diagnostics["digest_environment"]
            == "applied"
        )
    assert (checkout / "include/recc.yml").is_file()
    assert (checkout / "include/gcc-for-recc.yml").is_file()
    assert (checkout / "include/clang-for-recc.yml").is_file()
    assert (checkout / "elements/buildsystems/recc-wrapper.bst").is_file()
    assert (checkout / "files/recc-wrapper/recc-wrapper").stat().st_mode & 0o111
    project = yaml.safe_load((checkout / "project.conf").read_text())
    assert "include/recc.yml" in project["(@)"]
    expected_policy = (
        "remote-execution" if kind in MANDATORY_KINDS else "cache-only"
    )
    assert project["options"]["recc"]["default"] == expected_policy
    assert project["min-version"] == 2.5
    assert "digest-environment" in (
        checkout / "include/gcc-for-recc.yml"
    ).read_text()
    include_text = (checkout / "include/recc.yml").read_text()
    assert ("remote-apis-socket" in include_text) is (kind in MANDATORY_KINDS)
    environment = yaml.safe_load(include_text)["environment"]
    assert environment["RECC_SERVER"] == (
        "grpc://frontend.buildbarn.svc.cluster.local:8980"
    )
    assert environment["RECC_ACTION_UNCACHEABLE"] == "0"
    assert environment["RECC_VERBOSE"] == "1"
    assert environment["PATH"] == "/usr/recc/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    if kind == "cosmic":
        runtime_only = checkout / "elements/core-deps/runtime-only.bst"
        assert "build-depends:" not in runtime_only.read_text()


def test_existing_recc_option_is_forced_to_the_resolved_policy(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")
    project = checkout / "project.conf"
    project.write_text(
        project.read_text()
        + """
  recc:
    type: enum
    values:
    - buildstream-only
    default: buildstream-only
""",
        encoding="utf-8",
    )

    _apply(checkout, "bst-prototype")

    text = project.read_text()
    assert "default: cache-only" in text
    assert "- cache-only" in text
    assert "default: buildstream-only" not in text


def test_runner_capability_is_required_for_nested_socket(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")

    OVERLAY.apply_overlay(
        checkout,
        project_kind="cosmic",
        runner_capability=True,
    )

    include = (checkout / "include/recc.yml").read_text()
    assert "remote-apis-socket:" in include
    assert "path: /tmp/casd.sock" in include


@pytest.mark.parametrize("kind", MANDATORY_KINDS)
def test_mandatory_lanes_refuse_without_proven_runner_capability(tmp_path, kind):
    checkout = _checkout(tmp_path, kind)

    with pytest.raises(OVERLAY.OverlayError, match="remoteApisSocketPath"):
        OVERLAY.apply_overlay(checkout, project_kind=kind)

    assert not (checkout / "include/recc.yml").exists()
    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()
    assert "include/recc.yml" not in (checkout / "project.conf").read_text()


@pytest.mark.parametrize("kind", MANDATORY_KINDS)
def test_mandatory_lanes_refuse_operator_cache_only_fallback(tmp_path, kind):
    checkout = _checkout(tmp_path, kind)

    with pytest.raises(OVERLAY.OverlayError, match="operator-only flag"):
        OVERLAY.apply_overlay(checkout, project_kind=kind, pilot_cache_only=True)

    assert not (checkout / "include/recc.yml").exists()


@pytest.mark.parametrize("kind", PILOT_KINDS)
def test_pilot_fixtures_require_an_explicit_documented_mode(tmp_path, kind):
    checkout = _checkout(tmp_path, kind)

    with pytest.raises(OVERLAY.OverlayError, match="operator-only pilot"):
        OVERLAY.apply_overlay(
            checkout, project_kind=kind, recc_provider=PILOT_PROVIDER
        )

    assert not (checkout / "include/recc.yml").exists()


def test_capability_and_pilot_modes_are_mutually_exclusive(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")

    with pytest.raises(OVERLAY.OverlayError, match="mutually exclusive"):
        OVERLAY.apply_overlay(
            checkout,
            project_kind="bst-prototype",
            runner_capability=True,
            pilot_cache_only=True,
            recc_provider=PILOT_PROVIDER,
        )


@pytest.mark.parametrize("kind", PROVIDERLESS_KINDS)
def test_overlay_refuses_when_no_element_supplies_recc(tmp_path, kind):
    checkout = _checkout(tmp_path, kind)

    mode = (
        {"runner_capability": True}
        if kind in MANDATORY_KINDS
        else {"pilot_cache_only": True}
    )
    with pytest.raises(OVERLAY.OverlayError, match="cannot supply the recc binary"):
        OVERLAY.apply_overlay(checkout, project_kind=kind, **mode)

    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()
    assert "include/recc.yml" not in (checkout / "project.conf").read_text()


def test_overlay_refuses_a_recc_provider_that_is_not_in_the_checkout(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")

    with pytest.raises(OVERLAY.OverlayError, match="is unusable"):
        OVERLAY.apply_overlay(
            checkout,
            project_kind="bst-prototype",
            pilot_cache_only=True,
            recc_provider="missing-sdk.bst:components/buildbox.bst",
        )

    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()


def test_wrapper_element_declares_the_pinned_recc_provider(tmp_path):
    checkout = _checkout(tmp_path, "dakota")

    _apply(checkout, "dakota")

    wrapper = yaml.safe_load(
        (checkout / "elements/buildsystems/recc-wrapper.bst").read_text()
    )
    assert wrapper["runtime-depends"] == [
        "freedesktop-sdk.bst:components/buildbox.bst"
    ]


def test_wrapper_script_fails_closed_when_recc_is_absent(tmp_path):
    wrapper = tmp_path / "recc-wrapper"
    wrapper.write_text(OVERLAY.RECC_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()

    missing_recc = subprocess.run(
        ["/bin/sh", str(wrapper), "-c", "main.c"],
        env={
            "PATH": str(empty_bin),
            "RECC_REMOTE_PLATFORM_chrootRootDigest": "abc/1",
        },
        capture_output=True,
        text=True,
    )
    assert missing_recc.returncode == 1
    assert "recc is not staged in this sandbox" in missing_recc.stderr

    missing_digest = subprocess.run(
        ["/bin/sh", str(wrapper), "-c", "main.c"],
        env={"PATH": str(empty_bin)},
        capture_output=True,
        text=True,
    )
    assert missing_digest.returncode == 1
    assert "chrootRootDigest is required" in missing_digest.stderr


def test_commented_build_depends_section_stays_loadable_yaml(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")
    element = checkout / "elements/core/cosmic-comp.bst"
    element.write_text(
        "kind: manual\n\n"
        "build-depends:\n"
        "# keep the toolchain first\n"
        "- freedesktop-sdk.bst:components/gcc-base.bst\n",
        encoding="utf-8",
    )

    _apply(checkout, "cosmic")

    text = element.read_text()
    assert "build-depends:-" not in text
    assert "build-depends:\n" in text
    parsed = yaml.safe_load(text)
    assert parsed["kind"] == "manual"
    assert "buildsystems/recc-wrapper.bst" in parsed["build-depends"]
    assert (
        "freedesktop-sdk.bst:components/gcc-base.bst" in parsed["build-depends"]
    )
    assert "# keep the toolchain first" in text


def test_build_depends_insertion_preserves_existing_indentation(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")
    element = checkout / "elements/core/cosmic-comp.bst"
    element.write_text(
        "kind: manual\n\n"
        "build-depends:\n"
        "  - freedesktop-sdk.bst:components/gcc-base.bst\n\n"
        "config:\n"
        "  build-commands:\n"
        "  - make\n",
        encoding="utf-8",
    )

    _apply(checkout, "cosmic")

    text = element.read_text()
    parsed = yaml.safe_load(text)
    assert "  - buildsystems/recc-wrapper.bst\n" in text
    assert "buildsystems/recc-wrapper.bst" in parsed["build-depends"]
    assert parsed["config"]["build-commands"] == ["make"]


def test_build_depends_insertion_is_idempotent_for_commented_sections(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")
    element = checkout / "elements/core/cosmic-comp.bst"
    original = (
        "kind: manual\n\n"
        "build-depends:\n"
        "# comment\n"
        "- freedesktop-sdk.bst:components/gcc-base.bst\n"
    )
    element.write_text(original, encoding="utf-8")
    entries = ("buildsystems/recc-wrapper.bst",)

    once = OVERLAY._insert_build_depends(original, entries)
    twice = OVERLAY._insert_build_depends(once, entries)

    assert once == twice
    assert yaml.safe_load(twice)["build-depends"].count(entries[0]) == 1


def test_overlay_fails_closed_before_writing_unsupported_layout(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")
    (checkout / "elements/core/cosmic-comp.bst").write_text(
        "kind: manual\nbuild-depends: [freedesktop-sdk.bst:components/gcc-base.bst]\n",
        encoding="utf-8",
    )

    with pytest.raises(OVERLAY.OverlayError, match="inline build-depends"):
        _apply(checkout, "cosmic")

    assert not (checkout / "include/recc.yml").exists()
    assert "include/recc.yml" not in (checkout / "project.conf").read_text()


def test_overlay_refuses_conflicting_lab_file_without_partial_writes(tmp_path):
    checkout = _checkout(tmp_path, "dakota")
    (checkout / "include/recc.yml").write_text("environment: {}\n", encoding="utf-8")

    with pytest.raises(OVERLAY.OverlayError, match="overwrite"):
        _apply(checkout, "dakota")

    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()
    assert "include/recc.yml" not in (checkout / "project.conf").read_text()


def test_existing_overlay_is_idempotent_and_reports_checksums(tmp_path):
    checkout = _checkout(tmp_path, "dakota")

    first = _apply(checkout, "dakota")
    second = _apply(checkout, "dakota")

    assert first["changed_files"]
    assert second["changed_files"] == {}
    assert all(len(checksum) == 64 for checksum in first["changed_files"].values())
    assert second["file_checksums"] == first["file_checksums"]
    assert set(second["file_checksums"]) == set(OVERLAY.MANAGED_FILES)


def test_prototype_attaches_the_wrapper_and_pinned_provider(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")

    diagnostics = _apply(checkout, "bst-prototype")

    baseline = yaml.safe_load(
        (checkout / "elements/recc-baseline.bst").read_text()
    )
    assert baseline["build-depends"] == [
        "buildsystems/recc-wrapper.bst",
        {"(@)": "include/gcc-for-recc.yml"},
    ]
    wrapper = yaml.safe_load(
        (checkout / "elements/buildsystems/recc-wrapper.bst").read_text()
    )
    assert wrapper["runtime-depends"] == [PILOT_PROVIDER]
    assert (
        diagnostics["digest_environment"]
        == "applied"
    )


def test_prototype_provider_attachment_refuses_unsupported_layout_before_writes(
    tmp_path,
):
    checkout = _checkout(tmp_path, "bst-prototype")
    element = checkout / "elements/recc-baseline.bst"
    original = element.read_text()
    element.write_text(
        original + "build-depends: [components/buildbox.bst]\n",
        encoding="utf-8",
    )

    with pytest.raises(OVERLAY.OverlayError, match="inline build-depends"):
        _apply(checkout, "bst-prototype")

    assert element.read_text() == original + "build-depends: [components/buildbox.bst]\n"
    assert not (checkout / "include/recc.yml").exists()
    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()


def test_provider_path_must_resolve_inside_checkout_before_writes(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")
    outside_provider = tmp_path / "outside.bst"
    outside_provider.write_text("kind: junction\n", encoding="utf-8")

    with pytest.raises(OVERLAY.OverlayError, match="outside the checkout"):
        OVERLAY.apply_overlay(
            checkout,
            project_kind="bst-prototype",
            pilot_cache_only=True,
            recc_provider="../../outside.bst",
        )

    assert not (checkout / "include/recc.yml").exists()
    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()


def test_provider_symlink_must_resolve_inside_checkout_before_writes(tmp_path):
    checkout = _checkout(tmp_path, "bst-prototype")
    outside_provider = tmp_path / "outside.bst"
    outside_provider.write_text("kind: cmake\n", encoding="utf-8")
    (checkout / "elements/components/escaped.bst").symlink_to(outside_provider)

    with pytest.raises(OVERLAY.OverlayError, match="outside the checkout"):
        OVERLAY.apply_overlay(
            checkout,
            project_kind="bst-prototype",
            pilot_cache_only=True,
            recc_provider="components/escaped.bst",
        )

    assert not (checkout / "include/recc.yml").exists()
    assert not (checkout / "elements/buildsystems/recc-wrapper.bst").exists()


def test_wrapper_integration_command_runs_under_bin_sh(tmp_path):
    wrapper_element = yaml.safe_load(
        OVERLAY._wrapper_element(PILOT_PROVIDER, OVERLAY.OVERLAY_VERSION)
    )
    command = wrapper_element["public"]["bst"]["integration-commands"][0]
    host_bin = tmp_path / "usr/bin"
    wrapper_bin = tmp_path / "usr/recc/bin"
    host_bin.mkdir(parents=True)
    wrapper_bin.mkdir(parents=True)
    (wrapper_bin / "recc-wrapper").write_text("#!/bin/sh\n", encoding="utf-8")
    for compiler in (
        "cc",
        "g++",
        "clang",
        "x86_64-linux-gnu-gcc",
        "x86_64-linux-gnu-clang++",
    ):
        (host_bin / compiler).write_text("", encoding="utf-8")

    command = command.replace("/usr/recc/bin", str(wrapper_bin)).replace(
        "/usr/bin", str(host_bin)
    )
    subprocess.run(["/bin/sh", "-ceu", command], check=True)

    for compiler in (
        "cc",
        "g++",
        "clang",
        "x86_64-linux-gnu-gcc",
        "x86_64-linux-gnu-clang++",
    ):
        link = wrapper_bin / compiler
        assert link.is_symlink()
        assert os.readlink(link) == "recc-wrapper"


def test_endpoint_diagnostics_reject_credentials(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")

    with pytest.raises(OVERLAY.OverlayError, match="credentials"):
        _apply(
            checkout,
            "cosmic",
            endpoint="grpc://user:secret@example.test:8980",
        )


def test_endpoint_diagnostics_reject_unsafe_host_or_path(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")

    with pytest.raises(OVERLAY.OverlayError, match="unsafe characters"):
        _apply(
            checkout,
            "cosmic",
            endpoint="grpc://bad*host.example:8980",
        )

    with pytest.raises(OVERLAY.OverlayError, match="URL data"):
        _apply(
            checkout,
            "cosmic",
            endpoint="grpc://example.test:8980/path",
        )


def test_endpoint_without_scheme_uses_shared_grpc_contract(tmp_path):
    checkout = _checkout(tmp_path, "cosmic")

    diagnostics = _apply(
        checkout,
        "cosmic",
        endpoint="frontend.buildbarn.svc.cluster.local:8980",
    )

    assert (
        diagnostics["endpoint"]
        == "grpc://frontend.buildbarn.svc.cluster.local:8980"
    )
