import hashlib
import json
import logging
import shutil
import tarfile
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import validate_requirements
from pycontainer.lockfiles import (
    LockfileError,
    detect_lockfile,
    load_lockfile,
    target_environment,
)
from test_dependency_staging import _builder, _write_wheel

FIXTURES = Path(__file__).parent / "fixtures" / "lockfiles"
FORMATS = {
    "uv": ("uv.lock", "pyproject.toml"),
    "pdm": ("pdm.lock", "pyproject.toml"),
    "poetry": ("poetry.lock", "pyproject.toml"),
    "pipenv": ("Pipfile.lock", "Pipfile"),
}
PY311 = target_environment("linux", "amd64", "3.11", "3.11.8")
PY312 = target_environment("linux", "arm64", "3.12", "3.12.4")
EXPECTED_311 = {
    "anyio": "4.16.0",
    "certifi": "2026.10.23",
    "click": "8.5.1",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "httpx": "0.28.1",
    "idna": "3.20",
    "iniconfig": "2.0.0",
    "socksio": "1.0.0",
    "typing-extensions": "4.16.0",
}


def _copy(tmp_path, kind):
    context = tmp_path / kind
    shutil.copytree(FIXTURES / kind, context)
    return context


def _selected(selection):
    return {package.name: package.version for package in selection.packages}


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_real_lockfiles_select_target_transitive_closure(tmp_path, kind):
    context = _copy(tmp_path, kind)
    lock_name = FORMATS[kind][0]

    assert detect_lockfile(context) == lock_name
    selection = load_lockfile(context, Path(lock_name), PY311)

    # Host-only (win32), dev-group and older-Python packages are excluded.
    assert _selected(selection) == EXPECTED_311
    assert all(package.hashes for package in selection.packages)
    assert selection.index_urls == ()
    rendered = selection.render_requirements()
    assert "iniconfig==2.0.0 --hash=sha256:" in rendered
    assert rendered == load_lockfile(context, Path(lock_name), PY311).render_requirements()


@pytest.mark.parametrize("kind", ["uv", "pdm", "poetry"])
def test_python_version_forks_resolve_for_target(tmp_path, kind):
    context = _copy(tmp_path, kind)

    selection = load_lockfile(context, Path(FORMATS[kind][0]), PY312)

    assert _selected(selection)["iniconfig"] == "2.1.0"


@pytest.mark.parametrize("root_extra", [False, True])
def test_poetry_package_optional_flag_does_not_override_main_marker(tmp_path, root_extra):
    context = _copy(tmp_path, "poetry")
    lock = context / "poetry.lock"
    text = lock.read_text(encoding="utf-8")
    before, socksio = text.split('name = "socksio"', 1)
    socksio = socksio.replace("optional = false", "optional = true", 1)
    if root_extra:
        socksio = socksio.replace('groups = ["main"]', 'groups = ["main"]\nmarkers = "extra == \'proxy\'"', 1)
    lock.write_text(before + 'name = "socksio"' + socksio, encoding="utf-8")

    selection = load_lockfile(context, Path("poetry.lock"), PY311)
    assert ("socksio" in _selected(selection)) is not root_extra


def test_pipenv_lock_rejects_target_python_mismatch(tmp_path):
    context = _copy(tmp_path, "pipenv")

    with pytest.raises(LockfileError, match="requires Python 3.11.*3.12.4"):
        load_lockfile(context, Path("Pipfile.lock"), PY312)


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_stale_lockfiles_fail_before_install(tmp_path, kind):
    context = _copy(tmp_path, kind)
    lock_name, manifest_name = FORMATS[kind]
    manifest = context / manifest_name
    text = manifest.read_text(encoding="utf-8")
    old = '">=8.1"' if kind == "pipenv" else "click>=8.1"
    manifest.write_text(text.replace(old, old.replace("8.1", "8.2")), encoding="utf-8")

    with pytest.raises(LockfileError, match="out of date"):
        load_lockfile(context, Path(lock_name), PY311)


def test_uv_freshness_compares_markers_semantically(tmp_path):
    context = _copy(tmp_path, "uv")
    manifest = context / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("sys_platform == 'win32'", "sys_platform == 'linux'"), encoding="utf-8")

    with pytest.raises(LockfileError, match="out of date"):
        load_lockfile(context, Path("uv.lock"), PY311)


@pytest.mark.parametrize("manifest_index", ["https://new.example.invalid/simple", None])
def test_uv_lock_rejects_replaced_or_removed_index(tmp_path, manifest_index):
    old = "https://old.example.invalid/simple"
    context = _write_uv_project(tmp_path / "uv", registry=old, declare_index=False)
    manifest = context / "pyproject.toml"
    original = manifest.read_text()
    manifest.write_text(
        original + f'\n[[tool.uv.index]]\nurl = "{old}/"\n'
    )
    assert load_lockfile(context, Path("uv.lock"), PY311).index_urls == (old,)
    if manifest_index:
        manifest.write_text(manifest.read_text().replace(old, manifest_index))
    else:
        manifest.write_text(original)

    with pytest.raises(LockfileError, match="out of date.*uv lock"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_lock_accepts_declared_explicit_registry(tmp_path):
    registry = "https://example.invalid/simple"
    context = _write_uv_project(tmp_path / "uv", registry=registry)
    manifest = context / "pyproject.toml"
    manifest.write_text(manifest.read_text() + "explicit = true\n")

    assert load_lockfile(context, Path("uv.lock"), PY311).index_urls == (registry,)


def test_multiple_lockfiles_require_an_explicit_choice(tmp_path):
    context = _copy(tmp_path, "uv")
    shutil.copy(FIXTURES / "pdm" / "pdm.lock", context / "pdm.lock")

    with pytest.raises(LockfileError, match="Multiple lockfiles found.*--lockfile"):
        detect_lockfile(context)
    selection = load_lockfile(context, Path("uv.lock"), PY311)
    assert selection.kind == "uv"


def test_missing_full_target_python_version_is_explicit(tmp_path):
    context = _copy(tmp_path, "uv")

    with pytest.raises(LockfileError, match="full target Python version"):
        load_lockfile(context, Path("uv.lock"), target_environment("linux", "amd64", "3.11", None))


def _pipenv_with_entry(tmp_path, name, entry):
    context = _copy(tmp_path, "pipenv")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["default"][name] = entry
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return context


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"git": "https://example.com/repo.git", "ref": "abc"}, "unsupported git source"),
        ({"editable": True, "path": "."}, "unsupported editable source"),
        ({"version": "==1.0", "hashes": []}, "no artifact hashes"),
        ({"version": "==1.0", "hashes": ["md5:abc"]}, "Unsupported artifact hash"),
        (
            {"version": "==1.0", "hashes": ["sha256:" + "0" * 64], "markers": "platform_release >= '6'"},
            "platform_release is unknown",
        ),
    ],
)
def test_unsupported_or_unverifiable_lock_entries_fail(tmp_path, entry, message):
    context = _pipenv_with_entry(tmp_path, "extra-package", entry)

    with pytest.raises(LockfileError, match=message):
        load_lockfile(context, Path("Pipfile.lock"), PY311)


def _write_uv_project(
    context, wheel=None, *, registry="https://pypi.org/simple", digest=None, declare_index=True
):
    context.mkdir(parents=True, exist_ok=True)
    (context / "app.py").write_text("print('app')\n")
    (context / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["fixture-dep==1.0", "colorama>=0.4; sys_platform == \'win32\'"]\n'
        + (f'\n[[tool.uv.index]]\nurl = "{registry}"\n' if declare_index else "")
    )
    if digest is None:
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if wheel else "0" * 64
    (context / "uv.lock").write_text(
        f"""version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "colorama"
version = "0.4.6"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://example.invalid/colorama-0.4.6-py2.py3-none-any.whl", hash = "sha256:{'1' * 64}" }}]

[[package]]
name = "demo"
version = "0.1.0"
source = {{ virtual = "." }}
dependencies = [
    {{ name = "colorama", marker = "sys_platform == 'win32'" }},
    {{ name = "fixture-dep" }},
]

[package.metadata]
requires-dist = [
    {{ name = "colorama", marker = "sys_platform == 'win32'", specifier = ">=0.4" }},
    {{ name = "fixture-dep", specifier = "==1.0" }},
]

[[package]]
name = "fixture-dep"
version = "1.0"
source = {{ registry = "{registry}" }}
wheels = [{{ url = "https://example.invalid/fixture_dep-1.0-py3-none-any.whl", hash = "sha256:{digest}" }}]
"""
    )
    return context


def test_index_credentials_must_be_placeholders(tmp_path):
    secret = _write_uv_project(tmp_path / "secret", registry="https://user:hunter2@example.com/simple")
    with pytest.raises(LockfileError, match="literal credentials"):
        load_lockfile(secret, Path("uv.lock"), PY311)

    placeholder = _write_uv_project(
        tmp_path / "placeholder", registry="https://${INDEX_USER}:${INDEX_TOKEN}@example.com/simple/"
    )
    selection = load_lockfile(placeholder, Path("uv.lock"), PY311)
    assert selection.index_urls == ("https://${INDEX_USER}:${INDEX_TOKEN}@example.com/simple",)
    assert "--extra-index-url https://${INDEX_USER}" in selection.render_requirements()


@pytest.mark.parametrize(
    "registry, message",
    [
        ("https://example.com/simple?token=hunter2", "literal credentials or query"),
        ("https://example.com/simple\n-r /outside/requirements.txt", "control characters"),
    ],
)
def test_index_urls_reject_query_secrets_and_injected_directives(tmp_path, registry, message):
    context = _write_uv_project(tmp_path / "ctx", registry=registry.replace("\n", "\\n"))

    with pytest.raises(LockfileError, match=message):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_lock_missing_root_dependency_edge_fails(tmp_path):
    context = _copy(tmp_path, "uv")
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    lock.write_text(text.replace('    { name = "click" },\n', "", 1), encoding="utf-8")

    with pytest.raises(LockfileError, match="does not lock click"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_lock_without_wheels_fails(tmp_path):
    context = _write_uv_project(tmp_path / "ctx")
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    sdist = f'sdist = {{ url = "https://example.invalid/fixture_dep-1.0.tar.gz", hash = "sha256:{"2" * 64}" }}\n'
    lock.write_text(text.rsplit("wheels = ", 1)[0] + sdist, encoding="utf-8")

    with pytest.raises(LockfileError, match="no locked wheels"):
        load_lockfile(context, Path("uv.lock"), PY311)


def _offline_wheelhouse(monkeypatch, context):
    wheel = _write_wheel(context / "wheelhouse")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    return wheel


def _layer_names(layer):
    with tarfile.open(layer.tar_path) as archive:
        return archive.getnames()


def test_locked_build_installs_hashed_target_selection_reproducibly(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheel = _offline_wheelhouse(monkeypatch, context)
    _write_uv_project(context, wheel)
    (context / "requirements.txt").write_text("unrelated-package==9.9\n")

    digests = []
    for run in range(2):
        builder = _builder(
            tmp_path / f"run-{run}", monkeypatch, context=context, include_deps=True, generate_sbom=True
        )
        builder.build()
        digests.append(builder.layers[0].digest)

    names = _layer_names(builder.layers[0])
    assert "app/.python/site-packages/fixture_dep/__init__.py" in names
    assert not [name for name in names if "colorama" in name or name.endswith("requirements.txt")]
    assert digests[0] == digests[1]
    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert [(package["name"], package["versionInfo"]) for package in sbom["packages"]] == [
        ("fixture-dep", "1.0")
    ]


def test_locked_build_rejects_artifact_hash_mismatch(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _offline_wheelhouse(monkeypatch, context)
    _write_uv_project(context, digest="2" * 64)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(RuntimeError, match="must match their recorded hashes"):
        builder.build()


def _source(tmp_path, context, **options):
    builder = ImageBuilder(
        BuildConfig(context_dir=str(context), output_dir=str(tmp_path / "out"), use_cache=False, **options)
    )
    kind, value = builder._dependency_source()
    return kind, value if kind != "venv" else "venv"


def test_dependency_source_precedence(tmp_path, capsys):
    context = _write_uv_project(tmp_path / "context")
    (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    (context / "requirements-prod.txt").write_text("fixture-dep==1.0\n")
    site = context / ".venv" / "lib" / "python3.11" / "site-packages" / "pkg"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("")

    assert _source(tmp_path, context, include_deps=True) == ("lock", Path("uv.lock"))
    assert "Using lockfile uv.lock" in capsys.readouterr().out
    assert _source(tmp_path, context, include_deps=True, requirements_file="requirements-prod.txt") == (
        "requirements",
        Path("requirements-prod.txt"),
    )
    assert _source(tmp_path, context, include_deps=True, use_lockfile=False) == ("venv", "venv")
    shutil.rmtree(context / ".venv")
    assert _source(tmp_path, context, include_deps=True, use_lockfile=False) == (
        "requirements",
        Path("requirements.txt"),
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        BuildConfig(lockfile="uv.lock", requirements_file="requirements.txt")
    with pytest.raises(ValueError, match="stay inside the context"):
        _source(tmp_path, context, include_deps=True, lockfile="../uv.lock")


def test_dry_run_reports_dependency_source(tmp_path, capsys):
    context = _write_uv_project(tmp_path / "context")
    builder = ImageBuilder(
        BuildConfig(context_dir=str(context), output_dir=str(tmp_path / "out"), include_deps=True, dry_run=True)
    )

    builder.build()

    output = capsys.readouterr().out
    assert "Dependency Source: lock (uv.lock)" in output
    assert "including uv marker freshness" in output


def test_dry_run_can_preview_without_dependencies(tmp_path, capsys):
    builder = ImageBuilder(BuildConfig(context_dir=str(tmp_path), include_deps=True, dry_run=True))

    assert builder.build() == builder.config.tag
    assert "no supported lockfile, virtualenv site-packages or requirements.txt" in capsys.readouterr().out


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_dry_run_rejects_stale_lock_offline(tmp_path, kind, monkeypatch):
    context = _copy(tmp_path, kind)
    _, manifest_name = FORMATS[kind]
    manifest = context / manifest_name
    manifest.write_text(manifest.read_text().replace("8.1", "8.2"))
    output = tmp_path / "out"
    builder = ImageBuilder(
        BuildConfig(context_dir=str(context), output_dir=str(output), include_deps=True, dry_run=True)
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("dry-run pulled the base"))

    with pytest.raises(LockfileError, match="out of date"):
        builder.build()
    assert not output.exists()


def test_dry_run_rejects_ambiguous_lockfiles(tmp_path):
    context = _copy(tmp_path, "uv")
    (context / "poetry.lock").touch()
    builder = ImageBuilder(
        BuildConfig(context_dir=str(context), output_dir=str(tmp_path / "out"), include_deps=True, dry_run=True)
    )

    with pytest.raises(LockfileError, match="Multiple lockfiles found"):
        builder.build()


def test_cli_and_toml_select_lockfile_options(tmp_path, monkeypatch):
    from test_cli_config import _capture_config

    config = _capture_config(monkeypatch, ["build", "--lockfile", "poetry.lock"])
    assert config.lockfile == "poetry.lock"
    assert _capture_config(monkeypatch, ["build", "--no-lockfile"]).use_lockfile is False

    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text('[build]\nlockfile = "uv.lock"\n')
    assert _capture_config(monkeypatch, ["build", "--config", str(config_path)]).lockfile == "uv.lock"


@pytest.mark.parametrize(
    ("lines", "pinned"),
    [
        (["requests==2.32.3 --hash=sha256:" + "a" * 64], True),
        (["requests[socks]==2.32.3 ; python_version < '3.12' \\", "    --hash sha256:" + "a" * 64], True),
        (["requests==2.32.3"], False),
        (["requests>=2,<3 --hash=sha256:" + "a" * 64], False),
        (["-r base.txt", "idna==3.7 --hash=sha256:" + "b" * 64], False),
        (["-c constraints.txt", "idna==3.7 --hash=sha256:" + "b" * 64], True),
    ],
)
def test_requirements_hash_pinning_detection(tmp_path, lines, pinned):
    (tmp_path / "base.txt").write_text("certifi==2024.1.1\n")
    (tmp_path / "constraints.txt").write_text("certifi<2025\n")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("\n".join(lines) + "\n")

    assert validate_requirements(requirements, tmp_path) is pinned


def test_unhashed_requirements_warn_that_layer_is_not_reproducible(tmp_path, monkeypatch, caplog):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    wheel = _write_wheel(context / "wheelhouse")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    prefix = "--no-index\n--find-links ./wheelhouse\n"

    (context / "requirements.txt").write_text(prefix + "fixture-dep==1.0\n")
    with caplog.at_level(logging.WARNING, logger="pycontainer.dependencies"):
        _builder(tmp_path / "a", monkeypatch, context=context, include_deps=True).build()
    assert "not reproducible" in caplog.text

    caplog.clear()
    (context / "requirements.txt").write_text(prefix + f"fixture-dep==1.0 --hash=sha256:{digest}\n")
    with caplog.at_level(logging.WARNING, logger="pycontainer.dependencies"):
        _builder(tmp_path / "b", monkeypatch, context=context, include_deps=True).build()
    assert "not reproducible" not in caplog.text
