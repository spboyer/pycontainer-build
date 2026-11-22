import hashlib
import http.server
import json
import logging
import re
import shutil
import tarfile
import threading
import tomllib
import zipfile
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import (
    _merge_site_packages,
    install_locked_requirements,
    validate_requirements,
)
from pycontainer.lockfiles import (
    LockedPackage,
    LockfileError,
    LockSelection,
    _finalize,
    detect_lockfile,
    load_lockfile,
    pdm_content_hash,
    poetry_content_hash,
    preflight_lockfile,
    target_environment,
)
from test_dependency_staging import BASE_CONFIG, _builder, _write_wheel

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
    assert {package.index for package in selection.packages} == {None}
    sets = selection.requirement_sets()
    assert [url for url, _ in sets] == ["https://pypi.org/simple"]
    assert "--index-url https://pypi.org/simple\n" in sets[0][1]
    assert "extra-index-url" not in sets[0][1]
    assert "iniconfig==2.0.0 --hash=sha256:" in sets[0][1]
    assert sets == load_lockfile(context, Path(lock_name), PY311).requirement_sets()


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


PRIVATE = "https://private.example.invalid/simple"


def _poetry_with_source(tmp_path, priority, locked_source=True, declared=True):
    context = _copy(tmp_path, "poetry")
    manifest = context / "pyproject.toml"
    if declared:
        manifest.write_text(
            manifest.read_text()
            + f'\n[[tool.poetry.source]]\nname = "private"\nurl = "{PRIVATE}"\npriority = "{priority}"\n'
        )
    lock_path = context / "poetry.lock"
    lock = lock_path.read_text(encoding="utf-8")
    if locked_source:
        lock = lock.replace(
            'name = "click"\n',
            f'name = "click"\nsource = {{ type = "legacy", url = "{PRIVATE}", reference = "private" }}\n',
            1,
        )
    lock = re.sub(
        r'content-hash = "[0-9a-f]+"',
        f'content-hash = "{poetry_content_hash(tomllib.loads(manifest.read_text()))}"',
        lock,
    )
    lock_path.write_text(lock, encoding="utf-8")
    return context


def test_poetry_records_each_packages_source(tmp_path):
    context = _poetry_with_source(tmp_path, "supplemental")

    selection = load_lockfile(context, Path("poetry.lock"), PY311)

    indexes = {package.name: package.index for package in selection.packages}
    assert indexes.pop("click") == PRIVATE
    assert set(indexes.values()) == {None}
    assert [url for url, _ in selection.requirement_sets()] == ["https://pypi.org/simple", PRIVATE]


def test_poetry_source_missing_from_manifest_is_stale(tmp_path):
    context = _poetry_with_source(tmp_path, "supplemental", declared=False)

    with pytest.raises(LockfileError, match="out of date.*poetry lock"):
        load_lockfile(context, Path("poetry.lock"), PY311)


def test_poetry_package_without_source_fails_when_pypi_is_disabled(tmp_path):
    context = _poetry_with_source(tmp_path, "primary")

    with pytest.raises(LockfileError, match="PyPI is not a configured Poetry source"):
        load_lockfile(context, Path("poetry.lock"), PY311)


def _pipenv_sources(tmp_path, private_first=False):
    context = _copy(tmp_path, "pipenv")
    lock_path = context / "Pipfile.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    private = {"name": "private", "url": PRIVATE, "verify_ssl": True}
    lock["_meta"]["sources"].insert(0 if private_first else 1, private)
    lock["default"]["click"]["index"] = "private"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return context


@pytest.mark.parametrize("private_first", [False, True])
def test_pipenv_maps_package_indexes_and_default_source(tmp_path, private_first):
    selection = load_lockfile(_pipenv_sources(tmp_path, private_first), Path("Pipfile.lock"), PY311)

    indexes = {package.name: package.index for package in selection.packages}
    assert indexes.pop("click") == PRIVATE
    assert indexes.pop("iniconfig") is None
    # Packages without an index come from Pipenv's default (first) source.
    assert set(indexes.values()) == {PRIVATE if private_first else None}


def test_pipenv_search_all_sources_cannot_establish_provenance(tmp_path):
    context = _pipenv_sources(tmp_path)
    pipfile = context / "Pipfile"
    pipfile.write_text(pipfile.read_text() + "\n[pipenv]\ninstall_search_all_sources = true\n")

    with pytest.raises(LockfileError, match="anyio records no index"):
        load_lockfile(context, Path("Pipfile.lock"), PY311)


def _pdm_with_sources(tmp_path, *sources):
    context = _copy(tmp_path, "pdm")
    manifest = context / "pyproject.toml"
    manifest.write_text(
        manifest.read_text()
        + "".join(f'\n[[tool.pdm.source]]\nname = "{name}"\nurl = "{url}"\n' for name, url in sources)
    )
    lock_path = context / "pdm.lock"
    lock_path.write_text(
        re.sub(
            r'content_hash = "sha256:[0-9a-f]+"',
            f'content_hash = "sha256:{pdm_content_hash(tomllib.loads(manifest.read_text()))}"',
            lock_path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )
    return context


def test_pdm_single_replacement_index_applies_to_every_package(tmp_path):
    context = _pdm_with_sources(tmp_path, ("pypi", PRIVATE))

    selection = load_lockfile(context, Path("pdm.lock"), PY311)

    assert {package.index for package in selection.packages} == {PRIVATE}


@pytest.mark.parametrize("preflight", [False, True])
def test_pdm_multiple_indexes_fail_closed_including_preflight(tmp_path, preflight):
    context = _pdm_with_sources(tmp_path, ("private", PRIVATE))

    with pytest.raises(LockfileError, match="single effective index"):
        if preflight:
            preflight_lockfile(context, Path("pdm.lock"))
        else:
            load_lockfile(context, Path("pdm.lock"), PY311)


def test_pdm_toml_index_settings_are_rejected(tmp_path):
    context = _copy(tmp_path, "pdm")
    (context / "pdm.toml").write_text(f'[pypi]\nurl = "{PRIVATE}"\n')

    with pytest.raises(LockfileError, match=r"pdm.toml \[pypi\]"):
        load_lockfile(context, Path("pdm.lock"), PY311)


def test_uv_records_each_packages_registry(tmp_path):
    context = _write_uv_project(tmp_path / "uv", registry=PRIVATE)

    selection = load_lockfile(context, Path("uv.lock"), PY311)

    assert [(package.name, package.index) for package in selection.packages] == [("fixture-dep", PRIVATE)]


def test_package_selected_from_two_indexes_fails():
    packages = [
        LockedPackage("demo", "1.0", ("sha256:" + "0" * 64,), None),
        LockedPackage("demo", "1.0", ("sha256:" + "1" * 64,), PRIVATE),
    ]

    with pytest.raises(LockfileError, match="more than one package index"):
        _finalize(packages, "uv.lock")


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
    assert "--index-url https://${INDEX_USER}" in selection.requirement_sets()[0][1]


@pytest.mark.parametrize(
    "registry, message",
    [
        ("https://example.com/simple?token=hunter2", "literal credentials or query"),
        ("https://example.com/simple\n-r /outside/requirements.txt", "control characters"),
        ("https://${INDEX_HOST}/simple", "placeholder in its host or path"),
        ("https://example.com/${INDEX_PATH}", "placeholder in its host or path"),
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


class _SimpleIndex:
    """Loopback PEP 503 index that records every request path."""

    def __init__(self, wheels=()):
        self.wheels = {}
        self.requests = []
        for wheel in wheels:
            self.add(wheel)
        index = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                index.requests.append(self.path)
                parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
                body, content_type = None, "text/html"
                if len(parts) == 2 and parts[0] == "simple":
                    links = [
                        f'<a href="/files/{wheel.name}#sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}">'
                        f"{wheel.name}</a>"
                        for name, wheel in index.wheels.items()
                        if name == parts[1]
                    ]
                    if links:
                        body = ("<html><body>" + "".join(links) + "</body></html>").encode()
                elif len(parts) == 2 and parts[0] == "files":
                    wheel = next((w for w in index.wheels.values() if w.name == parts[1]), None)
                    if wheel:
                        body, content_type = wheel.read_bytes(), "application/octet-stream"
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/simple"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, wheel):
        self.wheels[wheel.name.split("-", 1)[0].replace("_", "-")] = wheel

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def indexes():
    servers = []

    def create(*wheels):
        servers.append(_SimpleIndex(wheels))
        return servers[-1]

    yield create
    for server in servers:
        server.close()


def _named_wheel(directory, name, requires=(), extras=()):
    module = name.replace("-", "_")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{module}-1.0-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    metadata += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    files = {
        f"{module}/__init__.py": b"",
        f"{module}-1.0.dist-info/METADATA": (metadata + "\n").encode(),
        f"{module}-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{module}-1.0.dist-info/RECORD"] = "".join(f"{item},,\n" for item in [*files, f"{module}-1.0.dist-info/RECORD"]).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for item, content in files.items():
            archive.writestr(zipfile.ZipInfo(item, (2020, 1, 1, 0, 0, 0)), content)
    return path


def _ambient_pip(monkeypatch, tmp_path, rogue_url, wheelhouse):
    """Point every ambient pip channel at an unauthorized source."""
    config = tmp_path / "pip.conf"
    config.write_text(f"[global]\nindex-url = {rogue_url}\nextra-index-url = {rogue_url}\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    monkeypatch.setenv("PIP_INDEX_URL", rogue_url)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", rogue_url)
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setenv("pip_no_index", "1")


def _layer_names(layer):
    with tarfile.open(layer.tar_path) as archive:
        return archive.getnames()


def test_locked_build_installs_only_from_recorded_index_reproducibly(tmp_path, monkeypatch, indexes):
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    recorded, rogue = indexes(wheel), indexes(wheel)
    _ambient_pip(monkeypatch, tmp_path, rogue.url, context / "wheelhouse")
    _write_uv_project(context, wheel, registry=recorded.url)
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
    assert any(path.startswith("/files/fixture_dep-1.0") for path in recorded.requests)
    assert rogue.requests == []
    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert [(package["name"], package["versionInfo"]) for package in sbom["packages"]] == [
        ("fixture-dep", "1.0")
    ]


def test_locked_build_does_not_fall_back_to_another_source_with_the_same_hash(
    tmp_path, monkeypatch, indexes
):
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    recorded, rogue = indexes(), indexes(wheel)
    _ambient_pip(monkeypatch, tmp_path, rogue.url, context / "wheelhouse")
    _write_uv_project(context, wheel, registry=recorded.url)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(RuntimeError, match="must match their recorded hashes"):
        builder.build()
    assert recorded.requests and rogue.requests == []


def test_locked_build_rejects_artifact_hash_mismatch(tmp_path, monkeypatch, indexes):
    context = tmp_path / "context"
    recorded = indexes(_write_wheel(context / "wheelhouse"))
    _write_uv_project(context, digest="2" * 64, registry=recorded.url)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(RuntimeError, match="must match their recorded hashes"):
        builder.build()


def test_locked_packages_install_from_their_own_indexes(tmp_path, monkeypatch, indexes):
    first = _write_wheel(tmp_path / "wheels")
    second = _named_wheel(tmp_path / "wheels", "other-dep")
    index_a, index_b = indexes(first, second), indexes(first, second)
    _ambient_pip(monkeypatch, tmp_path, index_b.url, tmp_path / "wheels")
    digest = lambda wheel: (f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}",)
    selection = LockSelection(
        "uv",
        Path("uv.lock"),
        (
            LockedPackage("fixture-dep", "1.0", digest(first), index_a.url),
            LockedPackage("other-dep", "1.0", digest(second), index_b.url),
        ),
    )
    site_packages = tmp_path / "stage" / "site-packages"
    site_packages.parent.mkdir()

    install_locked_requirements(
        selection.requirement_sets(), site_packages, tmp_path, "linux", "amd64", "3.11", "python:3.11-slim", None
    )

    assert {path.name for path in site_packages.iterdir()} >= {"fixture_dep", "other_dep"}
    assert not [path for path in index_a.requests if "other" in path]
    assert not [path for path in index_b.requests if "fixture" in path]
    assert any("fixture" in path for path in index_a.requests)
    assert any("other" in path for path in index_b.requests)


@pytest.mark.parametrize(
    ("arch", "selected_arch", "excluded_arch"),
    [
        ("amd64", "amd-dep", "arm-dep"),
        ("arm64", "arm-dep", "amd-dep"),
    ],
)
def test_locked_build_selects_target_transitive_markers_across_indexes(
    tmp_path, monkeypatch, indexes, arch, selected_arch, excluded_arch
):
    wheelhouse = tmp_path / "wheels"
    wheels = {
        "root-dep": _named_wheel(
            wheelhouse,
            "root-dep",
            requires=[
                "common-dep==1.0; python_version < '3.12'",
                "newpy-dep==1.0; python_version >= '3.12'",
                "amd-dep==1.0; platform_machine == 'x86_64'",
                "arm-dep==1.0; platform_machine == 'aarch64'",
                "win-dep==1.0; sys_platform == 'win32'",
                "feature-dep==1.0; extra == 'feature' and sys_platform == 'linux'",
            ],
            extras=["feature"],
        ),
    }
    for name in ("common-dep", "newpy-dep", "amd-dep", "arm-dep", "win-dep", "feature-dep"):
        wheels[name] = _named_wheel(wheelhouse, name)
    index_a, index_b, rogue = (indexes(*wheels.values()) for _ in range(3))
    _ambient_pip(monkeypatch, tmp_path, rogue.url, wheelhouse)

    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    (context / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["root-dep[feature]==1.0"]\n'
        f'\n[[tool.uv.index]]\nurl = "{index_a.url}"\n'
        f'\n[[tool.uv.index]]\nurl = "{index_b.url}"\n'
    )
    lock = f"""version = 1
requires-python = ">=3.10"

[[package]]
name = "demo"
version = "0.1.0"
source = {{ virtual = "." }}
dependencies = [{{ name = "root-dep", extra = ["feature"] }}]

[package.metadata]
requires-dist = [{{ name = "root-dep", extras = ["feature"], specifier = "==1.0" }}]
"""
    for name, wheel in wheels.items():
        registry = index_a.url if name in {"root-dep", "common-dep"} else index_b.url
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lock += f"""
[[package]]
name = "{name}"
version = "1.0"
source = {{ registry = "{registry}" }}
wheels = [{{ url = "{registry}/{wheel.name}", hash = "sha256:{digest}" }}]
"""
        if name == "root-dep":
            lock += """
dependencies = [
    { name = "common-dep", marker = "python_version < '3.12'" },
    { name = "newpy-dep", marker = "python_version >= '3.12'" },
    { name = "amd-dep", marker = "platform_machine == 'x86_64'" },
    { name = "arm-dep", marker = "platform_machine == 'aarch64'" },
    { name = "win-dep", marker = "sys_platform == 'win32'" },
]

[package.optional-dependencies]
feature = [{ name = "feature-dep", marker = "sys_platform == 'linux'" }]
"""
    (context / "uv.lock").write_text(lock)
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True,
        generate_sbom=True, platform=f"linux/{arch}",
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, target_arch: ([], {**BASE_CONFIG, "architecture": target_arch}),
    )

    builder.build()

    expected = {"root-dep", "common-dep", "feature-dep", selected_arch}
    names = _layer_names(builder.layers[0])
    assert {
        name for name in wheels
        if f"app/.python/site-packages/{name.replace('-', '_')}/__init__.py" in names
    } == expected
    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert {(package["name"], package["versionInfo"]) for package in sbom["packages"]} == {
        (name, "1.0") for name in expected
    }
    requested_a = {path.split("/")[2] for path in index_a.requests if path.startswith("/simple/")}
    requested_b = {path.split("/")[2] for path in index_b.requests if path.startswith("/simple/")}
    assert requested_a == {"root-dep", "common-dep"}
    assert requested_b == {"feature-dep", selected_arch}
    assert excluded_arch not in requested_a | requested_b
    assert "win-dep" not in requested_a | requested_b
    assert rogue.requests == []


def test_plain_requirements_keep_ambient_pip_configuration(tmp_path, monkeypatch, indexes):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    configured = indexes(_write_wheel(tmp_path / "wheels"))
    monkeypatch.setenv("PIP_INDEX_URL", configured.url)
    (context / "requirements.txt").write_text("fixture-dep==1.0\n")

    _builder(tmp_path, monkeypatch, context=context, include_deps=True).build()

    assert any(path.startswith("/files/fixture_dep-1.0") for path in configured.requests)


def test_merging_index_installs_rejects_conflicting_files(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    for root, content in ((source, "same"), (destination, "same")):
        (root / "namespace").mkdir(parents=True)
        (root / "namespace" / "__init__.py").write_text(content)
    (source / "namespace" / "module.py").write_text("new")
    _merge_site_packages(source, destination)
    assert (destination / "namespace" / "module.py").read_text() == "new"

    (source / "namespace" / "__init__.py").write_text("different")
    with pytest.raises(RuntimeError, match="different indexes both install namespace/__init__.py"):
        _merge_site_packages(source, destination)


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
