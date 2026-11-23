import hashlib
import json
import logging
import shutil
import tarfile
import tomllib
from pathlib import Path

import pytest

from pycontainer import lockfiles as lockfile_module
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import _analyze_requirements, install_requirements, staged_packages
from pycontainer.lockfiles import (
    LockfileError,
    detect_lockfile,
    load_lockfile,
    pdm_content_hash,
    pipfile_hash,
    poetry_content_hash,
    preflight_lockfile,
    target_environment,
    verify_locked_installation,
)
from test_dependency_staging import BASE_CONFIG, _builder, _write_named_wheel, _write_wheel

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
        manifest = context / "pyproject.toml"
        project = manifest.read_text(encoding="utf-8").replace("httpx[socks]>=0.27", "httpx>=0.27")
        manifest.write_text(project, encoding="utf-8")
        metadata = tomllib.loads(text)["metadata"]
        socksio = socksio.replace('groups = ["main"]', 'groups = ["main"]\nmarkers = "extra == \'proxy\'"', 1)
        socksio = socksio.replace(metadata["content-hash"], poetry_content_hash(tomllib.loads(project)), 1)
    lock.write_text(before + 'name = "socksio"' + socksio, encoding="utf-8")

    selection = load_lockfile(context, Path("poetry.lock"), PY311)
    assert ("socksio" in _selected(selection)) is not root_extra


@pytest.mark.parametrize(
    ("constraint", "version", "accepted"),
    [
        (">=3.11,<3.14", "3.14.4", False),
        ("^3.11", "3.12.4", True),
        ("^3.11", "3.10.9", False),
        ("~3.11", "3.12.4", False),
        ("*", "3.12.4", True),
    ],
)
def test_poetry_legacy_python_range_is_enforced(tmp_path, constraint, version, accepted):
    context = _copy(tmp_path, "poetry")
    manifest = context / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8").replace('requires-python = ">=3.10"\n', "")
    text += f'\n[tool.poetry.dependencies]\npython = "{constraint}"\n'
    manifest.write_text(text, encoding="utf-8")
    lock = context / "poetry.lock"
    content = lock.read_text(encoding="utf-8")
    metadata = tomllib.loads(content)["metadata"]
    before, after = content.split("[metadata]", 1)
    after = after.replace(
        f'python-versions = "{metadata["python-versions"]}"',
        f'python-versions = "{constraint}"',
        1,
    ).replace(metadata["content-hash"], poetry_content_hash(tomllib.loads(text)), 1)
    lock.write_text(before + "[metadata]" + after, encoding="utf-8")
    environment = target_environment("linux", "amd64", version.rsplit(".", 1)[0], version)

    if accepted:
        assert load_lockfile(context, Path("poetry.lock"), environment).packages
    else:
        with pytest.raises(LockfileError, match="requires Python"):
            load_lockfile(context, Path("poetry.lock"), environment)


def test_poetry_rejects_unverifiable_python_constraint(tmp_path):
    context = _copy(tmp_path, "poetry")
    lock = context / "poetry.lock"
    content = lock.read_text(encoding="utf-8")
    before, after = content.split("[metadata]", 1)
    metadata = tomllib.loads("[metadata]" + after)["metadata"]
    after = after.replace(
        f'python-versions = "{metadata["python-versions"]}"',
        'python-versions = "^3.11 || ^4.0"',
        1,
    )
    lock.write_text(before + "[metadata]" + after, encoding="utf-8")

    with pytest.raises(LockfileError, match="Unsupported Poetry Python constraint"):
        load_lockfile(context, Path("poetry.lock"), PY312)


def _drop_poetry_package(lock, missing):
    parts = lock.read_text(encoding="utf-8").split("[[package]]\n")
    lock.write_text(
        parts[0] + "".join("[[package]]\n" + part for part in parts[1:] if not part.startswith(f'name = "{missing}"\n')),
        encoding="utf-8",
    )


@pytest.mark.parametrize("missing", ["click", "httpcore", "socksio"])
def test_poetry_rejects_missing_locked_dependencies(tmp_path, missing):
    context = _copy(tmp_path, "poetry")
    _drop_poetry_package(context / "poetry.lock", missing)

    with pytest.raises(LockfileError, match=missing):
        load_lockfile(context, Path("poetry.lock"), PY311)


@pytest.mark.parametrize("missing", ["exceptiongroup", "pluggy"])
def test_poetry_does_not_require_inactive_dependencies(tmp_path, missing):
    context = _copy(tmp_path, "poetry")
    _drop_poetry_package(context / "poetry.lock", missing)

    assert missing not in _selected(load_lockfile(context, Path("poetry.lock"), PY311))


def test_poetry_requires_target_conditional_transitive_dependency(tmp_path):
    context = _copy(tmp_path, "poetry")
    _drop_poetry_package(context / "poetry.lock", "exceptiongroup")
    python310 = target_environment("linux", "amd64", "3.10", "3.10.9")

    with pytest.raises(LockfileError, match="exceptiongroup"):
        load_lockfile(context, Path("poetry.lock"), python310)


def test_poetry_rejects_wrong_transitive_version(tmp_path):
    context = _copy(tmp_path, "poetry")
    lock = context / "poetry.lock"
    text = lock.read_text(encoding="utf-8").replace(
        'name = "httpcore"\nversion = "1.0.9"',
        'name = "httpcore"\nversion = "2.0.0"',
        1,
    )
    lock.write_text(text, encoding="utf-8")

    with pytest.raises(LockfileError, match="httpcore"):
        load_lockfile(context, Path("poetry.lock"), PY311)


@pytest.mark.parametrize("constraint", ["1.0.9", "1.0.*"])
def test_poetry_accepts_exact_or_wildcard_dependency_constraints(tmp_path, constraint):
    context = _copy(tmp_path, "poetry")
    lock = context / "poetry.lock"
    text = lock.read_text(encoding="utf-8").replace(
        'httpcore = "==1.*"', f'httpcore = "{constraint}"', 1
    )
    lock.write_text(text, encoding="utf-8")

    assert _selected(load_lockfile(context, Path("poetry.lock"), PY311))["httpcore"] == "1.0.9"


def test_poetry_rejects_package_with_incompatible_python_range(tmp_path):
    context = _copy(tmp_path, "poetry")
    lock = context / "poetry.lock"
    before, after = lock.read_text(encoding="utf-8").split('name = "click"', 1)
    after = after.replace('python-versions = ">=3.10"', 'python-versions = ">=3.12"', 1)
    lock.write_text(before + 'name = "click"' + after, encoding="utf-8")

    with pytest.raises(LockfileError, match="click.*requires Python"):
        load_lockfile(context, Path("poetry.lock"), PY311)


def test_poetry_checks_transitively_requested_extras(tmp_path):
    context = _copy(tmp_path, "poetry")
    manifest = context / "pyproject.toml"
    project = manifest.read_text(encoding="utf-8").replace("httpx[socks]>=0.27", "httpx>=0.27")
    manifest.write_text(project, encoding="utf-8")
    lock = context / "poetry.lock"
    text = lock.read_text(encoding="utf-8").replace(
        'httpcore = "==1.*"', 'httpcore = {version = "==1.*", extras = ["socks"]}', 1
    )
    before, after = text.split("[metadata]", 1)
    metadata = tomllib.loads("[metadata]" + after)["metadata"]
    after = after.replace(metadata["content-hash"], poetry_content_hash(tomllib.loads(project)), 1)
    lock.write_text(before + "[metadata]" + after, encoding="utf-8")
    _drop_poetry_package(lock, "socksio")

    with pytest.raises(LockfileError, match="socksio"):
        load_lockfile(context, Path("poetry.lock"), PY311)


def test_poetry_does_not_require_unrequested_dependency_extra(tmp_path):
    context = _copy(tmp_path, "poetry")
    manifest = context / "pyproject.toml"
    project = manifest.read_text(encoding="utf-8").replace("httpx[socks]>=0.27", "httpx>=0.27")
    manifest.write_text(project, encoding="utf-8")
    lock = context / "poetry.lock"
    before, after = lock.read_text(encoding="utf-8").split("[metadata]", 1)
    metadata = tomllib.loads("[metadata]" + after)["metadata"]
    after = after.replace(metadata["content-hash"], poetry_content_hash(tomllib.loads(project)), 1)
    lock.write_text(before + "[metadata]" + after, encoding="utf-8")
    _drop_poetry_package(lock, "socksio")

    assert "socksio" not in _selected(load_lockfile(context, Path("poetry.lock"), PY311))


def test_poetry_checks_legacy_manifest_root_dependencies(tmp_path):
    context = _copy(tmp_path, "poetry")
    project = (
        '[tool.poetry]\nname = "lockdemo"\nversion = "0.1.0"\n'
        '[tool.poetry.dependencies]\npython = ">=3.10"\nclick = ">=8.1"\n'
    )
    (context / "pyproject.toml").write_text(project, encoding="utf-8")
    lock = context / "poetry.lock"
    before, after = lock.read_text(encoding="utf-8").split("[metadata]", 1)
    metadata = tomllib.loads("[metadata]" + after)["metadata"]
    after = after.replace(metadata["content-hash"], poetry_content_hash(tomllib.loads(project)), 1)
    lock.write_text(before + "[metadata]" + after, encoding="utf-8")
    _drop_poetry_package(lock, "click")

    with pytest.raises(LockfileError, match="click"):
        load_lockfile(context, Path("poetry.lock"), PY311)


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


def test_uv_freshness_accepts_equivalent_python_specifier_order(tmp_path):
    context = _copy(tmp_path, "uv")
    manifest = context / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace(
        'requires-python = ">=3.10"', 'requires-python = "<4,>=3.10"'
    ))
    lock = context / "uv.lock"
    lock.write_text(lock.read_text().replace(
        'requires-python = ">=3.10"', 'requires-python = ">=3.10,<4"', 1
    ))

    assert _selected(load_lockfile(context, Path("uv.lock"), PY311)) == EXPECTED_311


def _set_uv_environments(context, project_markers, lock_markers):
    manifest = context / "pyproject.toml"
    manifest.write_text(manifest.read_text() + f"\n[tool.uv]\nenvironments = {json.dumps(project_markers)}\n")
    lock = context / "uv.lock"
    lock.write_text(lock.read_text().replace(
        'requires-python = ">=3.10"\n',
        'requires-python = ">=3.10"\n' + f"supported-markers = {json.dumps(lock_markers)}\n",
        1,
    ))


def test_uv_rejects_target_outside_locked_environments(tmp_path):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(context, ["sys_platform == 'win32'"], ["sys_platform == 'win32'"])

    with pytest.raises(LockfileError, match="does not support the target"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_accepts_matching_locked_environment(tmp_path):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(context, ["sys_platform == 'linux'"], ["sys_platform == 'linux'"])

    assert _selected(load_lockfile(context, Path("uv.lock"), PY311)) == EXPECTED_311


def test_uv_rejects_changed_locked_environments(tmp_path):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(context, ["sys_platform == 'linux'"], ["sys_platform == 'win32'"])

    with pytest.raises(LockfileError, match="Cannot verify.*environments"):
        preflight_lockfile(context, Path("uv.lock"))
    with pytest.raises(LockfileError, match="Cannot verify.*environments"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_environment_normalization_does_not_change_marker_literals(tmp_path):
    context = _copy(tmp_path, "uv")
    project_marker = "sys_platform == 'linux' or platform_system == 'python_version >= \"3.13\"'"
    lock_marker = "sys_platform == 'linux' or platform_system == 'python_full_version >= \"3.13\"'"
    _set_uv_environments(context, [project_marker], [lock_marker])

    with pytest.raises(LockfileError, match="Cannot verify.*environments"):
        load_lockfile(context, Path("uv.lock"), PY311)


@pytest.mark.parametrize(
    "markers",
    [
        ["platform_release >= '6'"],
        ["sys_platform == 'linux'", "platform_release >= '6'"],
    ],
)
def test_uv_rejects_unknown_locked_environment_marker(tmp_path, markers):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(context, markers, markers)

    with pytest.raises(LockfileError, match="platform_release is unknown"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_accepts_normalized_python_environment_marker(tmp_path):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(context, ["python_version >= '3.13'"], ["python_full_version >= '3.13'"])
    supported = target_environment("linux", "amd64", "3.13", "3.13.4")

    assert _selected(load_lockfile(context, Path("uv.lock"), supported))["iniconfig"] == "2.1.0"
    with pytest.raises(LockfileError, match="does not support the target"):
        load_lockfile(context, Path("uv.lock"), PY312)


def test_uv_accepts_normalized_compound_python_environment_marker(tmp_path):
    context = _copy(tmp_path, "uv")
    _set_uv_environments(
        context,
        ["python_version >= '3.13' and sys_platform == 'linux'"],
        ["python_full_version >= '3.13' and sys_platform == 'linux'"],
    )
    supported = target_environment("linux", "amd64", "3.13", "3.13.4")

    assert _selected(load_lockfile(context, Path("uv.lock"), supported))["iniconfig"] == "2.1.0"
    with pytest.raises(LockfileError, match="does not support the target"):
        load_lockfile(context, Path("uv.lock"), PY312)


@pytest.mark.parametrize(
    ("minor", "full"),
    [("3.13", "3.13.0rc1"), ("3.12", "3.13.4")],
)
def test_lock_requires_consistent_final_target_version(tmp_path, minor, full):
    context = _copy(tmp_path, "uv")
    environment = target_environment("linux", "amd64", minor, full)

    with pytest.raises(LockfileError, match="consistent, final full target Python version"):
        load_lockfile(context, Path("uv.lock"), environment)


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


def test_pipenv_uses_implicit_default_source_for_unindexed_packages(tmp_path):
    context = _copy(tmp_path, "pipenv")
    manifest = context / "Pipfile"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace("https://pypi.org/simple", "https://mirror.example.invalid/simple")
    text = text.replace('click = ">=8.1"\n', "").replace('iniconfig = "==2.0.0"\n', "")
    manifest.write_text(text, encoding="utf-8")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["default"].pop("click")
    lock["default"].pop("iniconfig")
    lock["_meta"]["sources"][0]["url"] = "https://mirror.example.invalid/simple"
    lock["_meta"]["hash"]["sha256"] = pipfile_hash(tomllib.loads(text))
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)

    assert selection.index_urls == ("https://mirror.example.invalid/simple",)


def test_pipenv_rejects_missing_default_source(tmp_path):
    context = _copy(tmp_path, "pipenv")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["_meta"]["sources"] = []
    for entry in lock["default"].values():
        entry.pop("index", None)
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(LockfileError, match="no default source"):
        load_lockfile(context, Path("Pipfile.lock"), PY311)


@pytest.mark.parametrize("name", ["", "invalid name", "bad\n--index-url https://example.invalid/simple"])
def test_locked_package_names_cannot_inject_requirements(tmp_path, name):
    context = _copy(tmp_path, "pipenv")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["default"][name] = {
        "version": "==1.0", "hashes": ["sha256:" + "a" * 64], "index": "pypi",
    }
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(LockfileError, match="invalid package name"):
        load_lockfile(context, Path("Pipfile.lock"), PY311)


@pytest.mark.parametrize(("kind", "missing"), [
    ("pipenv", "click"),
    ("pdm", "click"),
    ("pdm", "socksio"),
])
def test_pipenv_and_pdm_reject_missing_locked_dependencies(tmp_path, kind, missing):
    context = _copy(tmp_path, kind)
    if kind == "pipenv":
        lock_path = context / "Pipfile.lock"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        del lock["default"][missing]
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    else:
        lock_path = context / "pdm.lock"
        blocks = lock_path.read_text(encoding="utf-8").split("\n[[package]]\n")
        blocks = [block for block in blocks if not block.startswith(f'name = "{missing}"\n')]
        lock_path.write_text("\n[[package]]\n".join(blocks), encoding="utf-8")

    with pytest.raises(LockfileError, match=rf"does not lock {missing}"):
        load_lockfile(context, Path(FORMATS[kind][0]), PY311)


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


@pytest.mark.parametrize("registry", [
    "http://${INDEX_USER}:${INDEX_TOKEN}@example.com/simple",
    "http://example.com/simple?token=${INDEX_TOKEN}",
    "http://example.com/${INDEX_TOKEN}/simple",
])
def test_index_credentials_require_https(tmp_path, registry):
    context = _write_uv_project(tmp_path / "context", registry=registry)
    with pytest.raises(LockfileError, match="HTTPS"):
        load_lockfile(context, Path("uv.lock"), PY311)


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


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("python_full_version >= '3.12'", "does not identify one locked package"),
        ("platform_release >= '6'", "platform_release is unknown"),
    ],
)
def test_uv_rejects_invalid_single_candidate_resolution_marker(tmp_path, marker, message):
    context = _copy(tmp_path, "uv")
    lock = context / "uv.lock"
    before, after = lock.read_text(encoding="utf-8").split('name = "iniconfig"\nversion = "2.0.0"', 1)
    after = after.replace("python_full_version < '3.12'", marker, 1)
    lock.write_text(before + 'name = "iniconfig"\nversion = "2.0.0"' + after, encoding="utf-8")

    with pytest.raises(LockfileError, match=message):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_marker_literal_matching_an_unknown_variable_is_not_treated_as_variable(tmp_path):
    context = _write_uv_project(tmp_path / "ctx")
    for path in (context / "pyproject.toml", context / "uv.lock"):
        path.write_text(path.read_text().replace(
            "sys_platform == 'win32'", "platform_system == 'platform_release'"
        ))

    selection = load_lockfile(context, Path("uv.lock"), PY311)

    assert "fixture-dep" in _selected(selection)
    assert "colorama" not in _selected(selection)


@pytest.mark.parametrize("has_extra_dependencies", [False, True])
def test_uv_root_extra_without_dependency_edge(tmp_path, has_extra_dependencies):
    context = _write_uv_project(tmp_path / "ctx")
    manifest = context / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace("fixture-dep==1.0", "fixture-dep[empty]==1.0"))
    lock = context / "uv.lock"
    text = lock.read_text().replace(
        '{ name = "fixture-dep", specifier = "==1.0" }',
        '{ name = "fixture-dep", extras = ["empty"], specifier = "==1.0" }',
    )
    if has_extra_dependencies:
        text += '\n[package.optional-dependencies]\nempty = [{ name = "missing-dependency" }]\n'
    lock.write_text(text)

    if has_extra_dependencies:
        with pytest.raises(LockfileError, match="does not lock fixture-dep"):
            load_lockfile(context, Path("uv.lock"), PY311)
    else:
        assert "fixture-dep" in _selected(load_lockfile(context, Path("uv.lock"), PY311))


@pytest.mark.parametrize("parent", ["root", "transitive"])
def test_uv_explicit_empty_extra_without_optional_dependencies(tmp_path, parent):
    context = _write_uv_project(tmp_path / "ctx")
    lock = context / "uv.lock"
    text = lock.read_text()
    if parent == "root":
        manifest = context / "pyproject.toml"
        manifest.write_text(manifest.read_text().replace("fixture-dep==1.0", "fixture-dep[empty]==1.0"))
        text = text.replace(
            '{ name = "fixture-dep" },', '{ name = "fixture-dep", extra = ["empty"] },', 1
        ).replace(
            '{ name = "fixture-dep", specifier = "==1.0" }',
            '{ name = "fixture-dep", extras = ["empty"], specifier = "==1.0" }',
        )
    else:
        text += f"""
dependencies = [{{ name = "empty-child", extra = ["empty"] }}]

[[package]]
name = "empty-child"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://example.invalid/empty_child-1.0-py3-none-any.whl", hash = "sha256:{'2' * 64}" }}]
"""
    lock.write_text(text)

    selected = _selected(load_lockfile(context, Path("uv.lock"), PY311))
    assert "fixture-dep" in selected
    if parent == "transitive":
        assert "empty-child" in selected


def test_uv_lock_without_wheels_fails(tmp_path):
    context = _write_uv_project(tmp_path / "ctx")
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    sdist = f'sdist = {{ url = "https://example.invalid/fixture_dep-1.0.tar.gz", hash = "sha256:{"2" * 64}" }}\n'
    lock.write_text(text.rsplit("wheels = ", 1)[0] + sdist, encoding="utf-8")

    with pytest.raises(LockfileError, match="no locked wheels"):
        load_lockfile(context, Path("uv.lock"), PY311)


def test_uv_dynamic_dependencies_fail_before_installation(tmp_path):
    context = _write_uv_project(tmp_path / "ctx")
    manifest = context / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        'dependencies = ["fixture-dep==1.0", "colorama>=0.4; sys_platform == \'win32\'"]',
        'dynamic = ["dependencies"]',
    )
    manifest.write_text(text, encoding="utf-8")

    with pytest.raises(LockfileError, match="Dynamic dependencies are unsupported"):
        preflight_lockfile(context, Path("uv.lock"))


def _offline_wheelhouse(monkeypatch, context):
    wheel = _write_wheel(context / "wheelhouse")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    return wheel


def _layer_names(layer):
    with tarfile.open(layer.tar_path) as archive:
        return archive.getnames()


def _write_pipenv_project(context, *, requires=(), extras=()):
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(wheelhouse, "marker-root", requires=requires, extras=extras)
    wheel = wheelhouse / "marker_root-1.0-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    extra_text = ", extras = [" + ", ".join(f'"{extra}"' for extra in extras) + "]" if extras else ""
    pipfile = (
        '[[source]]\nname = "pypi"\nurl = "https://pypi.org/simple"\nverify_ssl = true\n'
        f'\n[packages]\nmarker-root = {{version = "==1.0"{extra_text}}}\n'
    )
    (context / "Pipfile").write_text(pipfile, encoding="utf-8")
    lock = {
        "_meta": {
            "pipfile-spec": 6,
            "hash": {"sha256": pipfile_hash(tomllib.loads(pipfile))},
            "requires": {},
            "sources": [{"name": "pypi", "url": "https://pypi.org/simple", "verify_ssl": True}],
        },
        "default": {
            "marker-root": {"version": "==1.0", "hashes": [f"sha256:{digest}"], "index": "pypi"},
        },
    }
    (context / "Pipfile.lock").write_text(json.dumps(lock), encoding="utf-8")
    return context


def test_pipenv_build_rejects_missing_target_transitive_dependency(tmp_path, monkeypatch):
    context = _write_pipenv_project(
        tmp_path / "context",
        requires=["missing-child>=1.0; platform_machine == 'x86_64'"],
    )
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        builder.build()


@pytest.mark.parametrize(("arch", "requirement", "message"), [
    ("arm64", "missing-child>=1; platform_machine == 'x86_64'", None),
    ("amd64", "missing-child>=1; platform_machine == 'x86_64'", "marker-root.*missing-child"),
    ("amd64", "missing-child; platform_release >= '6'", "platform_release is unknown"),
])
def test_pipenv_staged_wheel_closure_uses_target_markers(tmp_path, arch, requirement, message):
    context = _write_pipenv_project(tmp_path / "context")
    environment = target_environment("linux", arch, "3.11", "3.11.8")
    selection = load_lockfile(context, Path("Pipfile.lock"), environment)
    site = tmp_path / "site-packages"
    metadata = site / "marker_root-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: marker-root\nVersion: 1.0\nRequires-Dist: {requirement}\n\n",
        encoding="utf-8",
    )

    if message:
        with pytest.raises(LockfileError, match=message):
            verify_locked_installation(selection, context, site, environment)
    else:
        verify_locked_installation(selection, context, site, environment)


def test_staged_wheel_closure_allows_an_extra_with_no_dependencies(tmp_path):
    context = _write_pipenv_project(tmp_path / "context", extras=["feature"])
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    metadata = site / "marker_root-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "Metadata-Version: 2.1\nName: marker-root\nVersion: 1.0\nProvides-Extra: feature\n\n",
        encoding="utf-8",
    )

    verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize(
    ("operator", "literal", "expected"),
    [
        ("==", "Speed_Ups", True),
        ("!=", "Speed_Ups", False),
        ("in", "other,Speed_Ups", True),
        ("not in", "other,Speed_Ups", False),
    ],
)
def test_extra_markers_normalize_with_legacy_packaging(monkeypatch, operator, literal, expected):
    marker_type = lockfile_module.Marker

    class LegacyMarker:
        def __init__(self, expression):
            self.expression = expression
            self._markers = marker_type(expression)._markers
            self._markers[0][2].value = literal

        def __str__(self):
            return self.expression

        def evaluate(self, values):
            extra, value = values["extra"], self._markers[0][2].value
            return {
                "==": extra == value,
                "!=": extra != value,
                "in": extra in value,
                "not in": extra not in value,
            }[operator]

    monkeypatch.setattr(lockfile_module, "Marker", LegacyMarker)

    assert lockfile_module._marker_applies(
        f"extra {operator} '{literal}'", PY311, "wheel metadata", "speed-ups"
    ) is expected


def test_lock_marker_rejects_unrecognized_packaging_structure(monkeypatch):
    class UnsupportedMarker:
        def __init__(self, expression):
            self._markers = [object()]

        def __str__(self):
            return "extra == 'feature'"

        def evaluate(self, values):
            return True

    monkeypatch.setattr(lockfile_module, "Marker", UnsupportedMarker)

    with pytest.raises(LockfileError, match="Cannot inspect environment marker"):
        lockfile_module._marker_applies("extra == 'feature'", PY311, "wheel metadata", "feature")


def test_extra_marker_normalization_does_not_change_platform_values():
    assert not lockfile_module._marker_applies(
        "sys_platform == 'LINUX' or extra == 'Speed_Ups'", PY311, "wheel metadata", "other"
    )


def test_staged_wheel_closure_checks_normalized_requested_extras(tmp_path):
    context = _write_pipenv_project(tmp_path / "context", extras=["speed-ups"])
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    metadata = site / "marker_root-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "Metadata-Version: 2.1\nName: marker-root\nVersion: 1.0\n"
        "Requires-Dist: missing-child; extra == 'Speed_Ups'\n\n",
        encoding="utf-8",
    )

    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize("requested", [False, True])
def test_staged_wheel_closure_rejects_only_applicable_direct_urls(tmp_path, requested):
    context = _write_pipenv_project(tmp_path / "context", extras=["feature"] if requested else [])
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    metadata = site / "marker_root-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "Metadata-Version: 2.1\nName: marker-root\nVersion: 1.0\n"
        "Requires-Dist: optional @ https://example.invalid/optional.whl ; extra == 'feature'\n\n",
        encoding="utf-8",
    )

    if requested:
        with pytest.raises(LockfileError, match="Direct URL requirement optional"):
            verify_locked_installation(selection, context, site, PY311)
    else:
        verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize(("root_dependency", "middle_dependency", "message"), [
    ("middle[feature]>=1.0", "missing-child>=1.0; extra == 'feature'", "middle.*missing-child"),
    ("middle>=2.0", "missing-child; extra == 'feature'", "marker-root.*middle"),
])
def test_pipenv_staged_wheel_closure_checks_versions_and_transitive_extras(
    tmp_path, root_dependency, middle_dependency, message
):
    context = _write_pipenv_project(tmp_path / "context")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["default"]["middle"] = {
        "version": "==1.0", "hashes": ["sha256:" + "a" * 64], "index": "pypi",
    }
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    for name, dependency in (
        ("marker-root", root_dependency),
        ("middle", middle_dependency),
    ):
        metadata = site / f"{name.replace('-', '_')}-1.0.dist-info" / "METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\nRequires-Dist: {dependency}\n\n",
            encoding="utf-8",
        )

    with pytest.raises(LockfileError, match=message):
        verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize("headers", [
    "Name: marker-root\nName: other\nVersion: 1.0\n",
    "Name: marker-root\nVersion: 1.0\nRequires-Python: >=3.10\nRequires-Python: >=3.12\n",
    "Name: marker-root\nVersion: 1.0\nRequires-Dist missing-colon\n",
])
def test_staged_wheel_closure_rejects_malformed_metadata(tmp_path, headers):
    context = _write_pipenv_project(tmp_path / "context")
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    metadata = site / "marker_root-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Metadata-Version: 2.1\n{headers}\n", encoding="utf-8")

    with pytest.raises(LockfileError, match="Invalid staged package metadata"):
        verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize(
    "invalid", ["oversized", "mismatched-directory", "unsupported-version", "missing"]
)
def test_staged_wheel_closure_checks_metadata_bounds_and_identity(tmp_path, monkeypatch, invalid):
    context = _write_pipenv_project(tmp_path / "context")
    selection = load_lockfile(context, Path("Pipfile.lock"), PY311)
    site = tmp_path / "site-packages"
    directory = "other-1.0.dist-info" if invalid == "mismatched-directory" else "marker_root-1.0.dist-info"
    metadata = site / directory / "METADATA"
    metadata.parent.mkdir(parents=True)
    if invalid != "missing":
        metadata_version = "3.0" if invalid == "unsupported-version" else "2.1"
        metadata.write_text(
            f"Metadata-Version: {metadata_version}\nName: marker-root\nVersion: 1.0\n\n",
            encoding="utf-8",
        )
    if invalid == "oversized":
        monkeypatch.setattr("pycontainer.dependencies.MAX_METADATA_BYTES", 16)

    with pytest.raises(LockfileError, match="Invalid staged package metadata"):
        verify_locked_installation(selection, context, site, PY311)


@pytest.mark.parametrize("kind", ["uv", "uv-extra", "poetry", "poetry-legacy"])
def test_staged_wheel_closure_rejects_unrecorded_dependencies(tmp_path, kind):
    context = tmp_path / "context"
    if kind in {"uv", "uv-extra"}:
        _write_uv_project(context)
        lock_name = "uv.lock"
        if kind == "uv-extra":
            manifest = context / "pyproject.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace("fixture-dep==1.0", "fixture-dep[feature]==1.0"),
                encoding="utf-8",
            )
            lock_path = context / lock_name
            lock_path.write_text(
                lock_path.read_text(encoding="utf-8")
                .replace('{ name = "fixture-dep" },', '{ name = "fixture-dep", extra = ["feature"] },', 1)
                .replace(
                    '{ name = "fixture-dep", specifier = "==1.0" }',
                    '{ name = "fixture-dep", extras = ["feature"], specifier = "==1.0" }',
                    1,
                ),
                encoding="utf-8",
            )
    else:
        context.mkdir()
        if kind == "poetry":
            manifest = (
                '[project]\nname = "demo"\nversion = "1.0"\nrequires-python = ">=3.10"\n'
                'dependencies = ["fixture-dep[feature]==1.0"]\n'
            )
        else:
            manifest = (
                '[tool.poetry]\nname = "demo"\nversion = "1.0"\n'
                '[tool.poetry.dependencies]\npython = ">=3.10"\n'
                'fixture-dep = {version = "1.0", extras = ["feature"]}\n'
            )
        (context / "pyproject.toml").write_text(manifest, encoding="utf-8")
        (context / "poetry.lock").write_text(
            f'''[[package]]
name = "fixture-dep"
version = "1.0"
python-versions = ">=3.10"
groups = ["main"]
files = [{{file = "fixture_dep-1.0-py3-none-any.whl", hash = "sha256:{"a" * 64}"}}]

[metadata]
lock-version = "2.1"
python-versions = ">=3.10"
content-hash = "{poetry_content_hash(tomllib.loads(manifest))}"
''',
            encoding="utf-8",
        )
        lock_name = "poetry.lock"
    selection = load_lockfile(context, Path(lock_name), PY311)
    site = tmp_path / "site-packages"
    metadata = site / "fixture_dep-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    marker = "" if kind == "uv" else "; extra == 'feature'"
    metadata.write_text(
        "Metadata-Version: 2.1\nName: fixture-dep\nVersion: 1.0\n"
        f"Requires-Dist: missing-child>=1.0{marker}\n\n",
        encoding="utf-8",
    )

    with pytest.raises(LockfileError, match="fixture-dep.*missing-child"):
        verify_locked_installation(selection, context, site, PY311)


def test_pdm_build_rejects_missing_extra_dependency_from_wheel_metadata(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(
        wheelhouse, "marker-root",
        requires=["missing-child>=1.0; extra == 'feature'"], extras=["feature"],
    )
    wheel = wheelhouse / "marker_root-1.0-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = (
        '[project]\nname = "demo"\nversion = "1.0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["marker-root[feature]==1.0"]\n'
    )
    (context / "pyproject.toml").write_text(manifest, encoding="utf-8")
    (context / "pdm.lock").write_text(
        f'''[metadata]
lock_version = "4.5.1"
strategy = ["inherit_metadata"]
content_hash = "sha256:{pdm_content_hash(tomllib.loads(manifest))}"

[[package]]
name = "marker-root"
version = "1.0"
groups = ["default"]
files = [{{file = "{wheel.name}", hash = "sha256:{digest}"}}]
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        builder.build()


def _write_target_marker_uv_project(context):
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(
        wheelhouse,
        "fixture-dep",
        requires=[
            'marker-trans-amd; platform_machine == "x86_64"',
            'marker-trans-arm; platform_machine == "aarch64"',
            'marker-trans-win; sys_platform == "win32"',
        ],
    )
    names = (
        "marker-root-amd", "marker-root-arm", "marker-trans-amd", "marker-trans-arm",
        "marker-trans-win",
    )
    for name in names:
        _write_named_wheel(wheelhouse, name)
    _write_uv_project(context, wheelhouse / "fixture_dep-1.0-py3-none-any.whl")
    manifest = context / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        '"colorama>=0.4; sys_platform == \'win32\'"]',
        '"colorama>=0.4; sys_platform == \'win32\'", '
        '"marker-root-amd==1.0; platform_machine == \'x86_64\'", '
        '"marker-root-arm==1.0; platform_machine == \'aarch64\'"]',
    )
    manifest.write_text(text, encoding="utf-8")

    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8").replace(
        '    { name = "fixture-dep" },\n]',
        '    { name = "fixture-dep" },\n'
        '    { name = "marker-root-amd", marker = "platform_machine == \'x86_64\'" },\n'
        '    { name = "marker-root-arm", marker = "platform_machine == \'aarch64\'" },\n]',
        1,
    ).replace(
        '    { name = "fixture-dep", specifier = "==1.0" },\n]',
        '    { name = "fixture-dep", specifier = "==1.0" },\n'
        '    { name = "marker-root-amd", marker = "platform_machine == \'x86_64\'", specifier = "==1.0" },\n'
        '    { name = "marker-root-arm", marker = "platform_machine == \'aarch64\'", specifier = "==1.0" },\n]',
        1,
    )
    before, package = text.split('name = "fixture-dep"\nversion = "1.0"\n', 1)
    package = package.replace(
        'source = { registry = "https://pypi.org/simple" }\n',
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [\n'
        '    { name = "marker-trans-amd", marker = "platform_machine == \'x86_64\'" },\n'
        '    { name = "marker-trans-arm", marker = "platform_machine == \'aarch64\'" },\n'
        '    { name = "marker-trans-win", marker = "sys_platform == \'win32\'" },\n'
        ']\n',
        1,
    )
    text = before + 'name = "fixture-dep"\nversion = "1.0"\n' + package
    for name in names:
        wheel = wheelhouse / f"{name.replace('-', '_')}-1.0-py3-none-any.whl"
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        text += (
            f'\n[[package]]\nname = "{name}"\nversion = "1.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'wheels = [{{ url = "https://example.invalid/{wheel.name}", hash = "sha256:{digest}" }}]\n'
        )
    lock.write_text(text, encoding="utf-8")


def test_locked_build_stages_root_and_transitive_markers_for_each_target(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_target_marker_uv_project(context)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    for arch, suffix in (("amd64", "amd"), ("arm64", "arm")):
        layers = tmp_path / f"layers-{arch}"
        layers.mkdir()
        layer = builder._create_deps_layer(
            layers, base_config={**BASE_CONFIG, "architecture": arch}, os_name="linux", arch=arch
        )
        names = _layer_names(layer)
        assert f"app/.python/site-packages/marker_root_{suffix}/__init__.py" in names
        assert f"app/.python/site-packages/marker_trans_{suffix}/__init__.py" in names
        other = "arm" if suffix == "amd" else "amd"
        assert not any(f"marker_root_{other}/" in name or f"marker_trans_{other}/" in name for name in names)
        assert not any("marker_trans_win/" in name for name in names)


@pytest.mark.parametrize("location", ["root", "transitive"])
def test_locked_build_rejects_unknown_target_markers_before_pip(tmp_path, monkeypatch, location):
    context = tmp_path / "context"
    _write_target_marker_uv_project(context)
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    if location == "root":
        manifest = context / "pyproject.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "platform_machine == 'x86_64'", "platform_release >= '6'"
            ),
            encoding="utf-8",
        )
        text = text.replace(
            "platform_machine == 'x86_64'", "platform_release >= '6'", 2
        )
    else:
        before, package = text.split('name = "fixture-dep"\nversion = "1.0"\n', 1)
        package = package.replace("platform_machine == 'x86_64'", "platform_release >= '6'", 1)
        text = before + 'name = "fixture-dep"\nversion = "1.0"\n' + package
    lock.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        "pycontainer.dependencies.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("pip must not run for an unknown target marker"),
    )
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layers = tmp_path / "layers"
    layers.mkdir()

    with pytest.raises(LockfileError, match="platform_release is unknown"):
        builder._create_deps_layer(layers, base_config=BASE_CONFIG)


def test_lock_selection_cache_distinguishes_target_python_versions(tmp_path, monkeypatch):
    context = _copy(tmp_path, "uv")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True, base_image="python:latest")
    python312 = {
        **BASE_CONFIG,
        "config": {**BASE_CONFIG["config"], "Env": ["PYTHON_VERSION=3.12.4"]},
    }

    first = builder._lock_selection(BASE_CONFIG, "linux", "amd64", base_image_tag_verified=True)
    second = builder._lock_selection(python312, "linux", "amd64", base_image_tag_verified=True)

    assert _selected(first)["iniconfig"] == "2.0.0"
    assert _selected(second)["iniconfig"] == "2.1.0"


def test_lock_selection_cache_distinguishes_target_python_patch_versions(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_target_marker_uv_project(context)
    for filename in ("pyproject.toml", "uv.lock"):
        path = context / filename
        text = path.read_text(encoding="utf-8")
        for machine, marker in (
            ("x86_64", "python_full_version < '3.11.9'"),
            ("aarch64", "python_full_version >= '3.11.9'"),
        ):
            text = text.replace(f"platform_machine == '{machine}'", marker, 2)
        path.write_text(text, encoding="utf-8")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    python3119 = {
        **BASE_CONFIG,
        "config": {**BASE_CONFIG["config"], "Env": ["PYTHON_VERSION=3.11.9"]},
    }

    first = builder._lock_selection(BASE_CONFIG, "linux", "amd64", base_image_tag_verified=True)
    second = builder._lock_selection(python3119, "linux", "amd64", base_image_tag_verified=True)

    assert "marker-root-amd" in _selected(first)
    assert "marker-root-arm" in _selected(second)


def test_repeated_build_rechecks_lockfile_freshness(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheel = _offline_wheelhouse(monkeypatch, context)
    _write_uv_project(context, wheel)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    builder.build()
    manifest = context / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("fixture-dep==1.0", "fixture-dep==1.1"),
        encoding="utf-8",
    )

    with pytest.raises(LockfileError, match="out of date"):
        builder.build()


@pytest.mark.parametrize(
    ("arch", "expected"), [("amd64", "marker-amd"), ("arm64", "marker-arm")]
)
def test_locked_requirements_use_target_marker_bootstrap(tmp_path, arch, expected):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    for name in ("marker-amd", "marker-arm"):
        _write_named_wheel(wheelhouse, name)
    requirements = context / "requirements.txt"
    lines = ["--no-index", "--find-links ./wheelhouse"]
    for name, machine in (("marker-amd", "x86_64"), ("marker-arm", "aarch64")):
        wheel = wheelhouse / f"{name.replace('-', '_')}-1.0-py3-none-any.whl"
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lines.append(f'{name}==1.0; platform_machine == "{machine}" --hash=sha256:{digest}')
    requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination = tmp_path / "stage" / "site-packages"

    install_requirements(
        requirements, destination, context, "linux", arch, "3.11", "python:3.11-slim",
        BASE_CONFIG, locked=True, base_image_tag_verified=True,
    )

    assert {name for name, _ in staged_packages(destination)} == {expected}


def test_locked_requirements_fail_closed_for_unknown_target_marker(tmp_path):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(wheelhouse, "marker-root")
    wheel = wheelhouse / "marker_root-1.0-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    requirements = context / "requirements.txt"
    requirements.write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        f'marker-root==1.0; platform_release >= "1" --hash=sha256:{digest}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="refusing to stage.*platform_release"):
        install_requirements(
            requirements, tmp_path / "stage" / "site-packages", context, "linux", "amd64",
            "3.11", "python:3.11-slim", BASE_CONFIG, locked=True, base_image_tag_verified=True,
        )


@pytest.mark.parametrize(
    ("line", "message"),
    [("--platform=win_amd64", "cannot override"), ("-r ../outside.txt", "inside the build context")],
)
def test_generated_locked_requirements_validate_options_before_pip(tmp_path, monkeypatch, line, message):
    context = tmp_path / "context"
    stage = tmp_path / "stage"
    context.mkdir()
    stage.mkdir()
    (tmp_path / "outside.txt").write_text("outside==1.0\n", encoding="utf-8")
    requirements = context / "locked-requirements.txt"
    requirements.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "pycontainer.dependencies.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("pip must not run for invalid generated requirements"),
    )

    with pytest.raises(ValueError, match=message):
        install_requirements(
            requirements, stage / "site-packages", context, "linux", "amd64", "3.11",
            "python:3.11-slim", BASE_CONFIG, locked=True, base_image_tag_verified=True,
        )


@pytest.mark.parametrize("invalid", ["oversized", "outside-context"])
def test_generated_locked_requirements_enforce_size_and_location(tmp_path, monkeypatch, invalid):
    context = tmp_path / "context"
    stage = tmp_path / "stage"
    context.mkdir()
    stage.mkdir()
    requirements = (
        context / "locked-requirements.txt"
        if invalid == "oversized"
        else stage / "locked-requirements.txt"
    )
    requirements.write_text("fixture-dep==1.0 --hash=sha256:" + "a" * 64, encoding="utf-8")
    if invalid == "oversized":
        monkeypatch.setattr("pycontainer.dependencies.MAX_REQUIREMENTS_BYTES", 32)
    monkeypatch.setattr(
        "pycontainer.dependencies.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("pip must not run for invalid generated requirements"),
    )
    message = "combined .* limit" if invalid == "oversized" else "inside the build context"

    with pytest.raises(ValueError, match=message):
        install_requirements(
            requirements, stage / "site-packages", context, "linux", "amd64", "3.11",
            "python:3.11-slim", BASE_CONFIG, locked=True, base_image_tag_verified=True,
        )


@pytest.mark.parametrize("installation_fails", [False, True])
@pytest.mark.parametrize("include_paths", [
    ["."],
    [".pycontainer-lockfile-concurrent/requirements.txt", "app.py"],
])
def test_locked_build_keeps_generated_requirements_inside_context_and_cleans_up(
    tmp_path, monkeypatch, installation_fails, include_paths
):
    context = _write_uv_project(tmp_path / "context")
    other_build = context / ".pycontainer-lockfile-concurrent" / "requirements.txt"
    other_build.parent.mkdir()
    other_build.write_text("temporary requirements from another build\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    builder = _builder(
        tmp_path, monkeypatch, context=Path("context"), include_deps=True,
        include_paths=include_paths, exclude_defaults=False,
    )
    requirements_paths = []

    def install(requirements, destination, context_dir, *args, **kwargs):
        requirements_paths.append(requirements)
        assert requirements.is_absolute()
        assert requirements.resolve().is_relative_to(context_dir.resolve())
        assert "--hash=sha256:" in requirements.read_text(encoding="utf-8")
        assert kwargs["locked"] is True
        if installation_fails:
            raise RuntimeError("installation failed")
        destination.mkdir(parents=True)
        (destination / "fixture_dep.py").write_text("value = 1\n", encoding="utf-8")
        metadata = destination / "fixture_dep-1.0.dist-info" / "METADATA"
        metadata.parent.mkdir()
        metadata.write_text("Metadata-Version: 2.1\nName: fixture-dep\nVersion: 1.0\n\n", encoding="utf-8")

    monkeypatch.setattr("pycontainer.builder.install_requirements", install)
    layers = tmp_path / "layers"
    layers.mkdir()
    if installation_fails:
        with pytest.raises(RuntimeError, match="installation failed"):
            builder._create_deps_layer(layers, base_config=BASE_CONFIG)
    else:
        builder._create_deps_layer(layers, base_config=BASE_CONFIG)
        app_layer = builder._create_app_layer(layers, builder.context_include_paths)
        assert not any(".pycontainer-lockfile-" in name for name in _layer_names(app_layer))

    assert len(requirements_paths) == 1
    assert not requirements_paths[0].exists()
    assert not requirements_paths[0].parent.exists()
    assert other_build.exists()


@pytest.mark.parametrize(
    ("base_image", "environment", "wheel_tag"),
    [
        ("python:3.11-slim", [], "cp311-cp311-manylinux2014_x86_64"),
        ("python:3.11-alpine", [], "cp311-cp311-musllinux_1_2_x86_64"),
        (
            "python:3.11-slim@sha256:" + "a" * 64,
            ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_LIBC=musl-1.2"],
            "cp311-cp311-musllinux_1_2_x86_64",
        ),
    ],
)
def test_locked_build_uses_verified_base_abi_and_libc(
    tmp_path, monkeypatch, base_image, environment, wheel_tag
):
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse", tag=wheel_tag)
    _write_uv_project(context, wheel)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    base_config = {
        **BASE_CONFIG,
        "config": {
            **BASE_CONFIG["config"],
            "Env": [*BASE_CONFIG["config"]["Env"], *environment],
        },
    }
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True, base_image=base_image
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: ([], base_config))

    builder.build()

    assert "app/.python/site-packages/fixture_dep/__init__.py" in _layer_names(builder.layers[0])


@pytest.mark.parametrize(
    ("base_image", "environment", "message"),
    [
        (
            "python:3.11-slim@sha256:" + "a" * 64,
            ["PYTHON_IMPLEMENTATION=CPython"],
            "unverified",
        ),
        ("python@sha256:" + "a" * 64, ["PYTHON_LIBC=musl-1.2"], "Python ABI"),
        ("python:3.11-slim", ["PYTHON_LIBC=musl-1.2"], "conflicts"),
    ],
)
def test_locked_build_rejects_unverified_base_before_pip(
    tmp_path, monkeypatch, base_image, environment, message
):
    context = _write_uv_project(tmp_path / "context")
    base_config = {
        **BASE_CONFIG,
        "config": {
            **BASE_CONFIG["config"],
            "Env": [*BASE_CONFIG["config"]["Env"], *environment],
        },
    }
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True, base_image=base_image
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: ([], base_config))
    monkeypatch.setattr(
        "pycontainer.dependencies.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("pip ran before validating the base image"),
    )

    with pytest.raises(ValueError, match=message):
        builder.build()


def test_locked_build_installs_hashed_target_selection_reproducibly(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheel = _offline_wheelhouse(monkeypatch, context)
    _write_uv_project(context, wheel)
    (context / "requirements.txt").write_text("unrelated-package==9.9\n")
    monkeypatch.chdir(tmp_path)

    digests = []
    for run in range(2):
        builder = _builder(
            tmp_path / f"run-{run}", monkeypatch,
            context=Path("context") if run == 0 else context,
            include_deps=True, generate_sbom=True,
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


def test_locked_build_requires_a_matching_wheel_hash_for_each_target(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    amd = _write_wheel(wheelhouse, tag="cp311-cp311-manylinux2014_x86_64")
    arm = _write_wheel(wheelhouse, tag="cp311-cp311-manylinux2014_aarch64")
    amd_hash = hashlib.sha256(amd.read_bytes()).hexdigest()
    arm_hash = hashlib.sha256(arm.read_bytes()).hexdigest()
    _write_uv_project(context, digest=amd_hash)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    layers = tmp_path / "layers"
    layers.mkdir()
    base = {**BASE_CONFIG, "architecture": "arm64"}

    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    with pytest.raises(RuntimeError, match="must match their recorded hashes"):
        builder._create_deps_layer(layers, base_config=base, os_name="linux", arch="arm64")

    lock = context / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            f'hash = "sha256:{amd_hash}" }}]',
            f'hash = "sha256:{amd_hash}" }}, '
            f'{{ url = "https://example.invalid/{arm.name}", hash = "sha256:{arm_hash}" }}]',
        ),
        encoding="utf-8",
    )
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layer = builder._create_deps_layer(layers, base_config=base, os_name="linux", arch="arm64")
    assert "app/.python/site-packages/fixture_dep/__init__.py" in _layer_names(layer)


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
        (["-e."], False),
        (["-e ."], False),
        (["-r base.txt", "idna==3.7 --hash=sha256:" + "b" * 64], False),
        (["-c constraints.txt", "idna==3.7 --hash=sha256:" + "b" * 64], True),
    ],
)
def test_requirements_hash_pinning_detection(tmp_path, lines, pinned):
    (tmp_path / "base.txt").write_text("certifi==2024.1.1\n")
    (tmp_path / "constraints.txt").write_text("certifi<2025\n")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("\n".join(lines) + "\n")

    _, hash_pinned = _analyze_requirements(requirements, tmp_path)
    assert hash_pinned is pinned


@pytest.mark.parametrize(
    "includes",
    ["-r shared.txt\n-c shared.txt\n", "-c shared.txt\n-r shared.txt\n"],
)
def test_requirements_also_used_as_constraints_still_need_hashes(tmp_path, monkeypatch, includes):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(includes)
    shared = tmp_path / "shared.txt"
    shared.write_text("fixture-dep==1.0\n")
    monkeypatch.setattr(
        "pycontainer.dependencies.MAX_REQUIREMENTS_BYTES",
        requirements.stat().st_size + shared.stat().st_size,
    )

    roots, hash_pinned = _analyze_requirements(requirements, tmp_path)
    assert roots == ["fixture-dep==1.0"]
    assert not hash_pinned


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
