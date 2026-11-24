"""Hash-pinned dependency layer reuse from the local cache (issue #144)."""
import base64
import csv
import hashlib
import http.server
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import pycontainer.builder as builder_module
import pycontainer.dependency_cache as dependency_cache
import pycontainer.dependencies as dependencies
import pycontainer.lockfiles as lockfiles
from pycontainer.builder import ImageBuilder
from pycontainer.cache import CacheIndexError, LayerCache
from pycontainer.config import BuildConfig
from pycontainer.dependency_cache import (
    DependencyCacheMiss,
    ambient_pip_settings,
    decode_inventory,
    dependency_cache_key,
    encode_inventory,
    parse_pip_config,
)
from pycontainer.lockfiles import LockfileError, pipfile_hash

from test_dependency_staging import BASE_CONFIG, _write_wheel
from test_lockfiles import (  # noqa: F401 - pytest fixture
    _named_wheel,
    _write_pipenv_project,
    _write_uv_project,
    indexes,
)

SECRET = "hunter2-secret"


@pytest.fixture(autouse=True)
def _no_ambient_pip(monkeypatch):
    for name in [
        name for name in os.environ
        if name.upper().startswith(("PIP_", "PYTHON"))
        or name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    ]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NETRC", os.devnull)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setattr(dependency_cache, "_system_proxies", lambda: {})
    monkeypatch.setattr(
        dependency_cache, "ambient_pip_settings",
        lambda environment: dependency_cache.AmbientPipSnapshot((), (), (), dict(environment)),
    )


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _project(root, requirements=None):
    context = root / "context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "app.py").write_text("print('app')\n")
    wheel = _write_wheel(context / "wheelhouse")
    (context / "requirements.txt").write_text(
        requirements
        or f"--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0 --hash=sha256:{_sha(wheel)}\n"
    )
    return context


def _builder(tmp_path, monkeypatch, context, name, cache_dir=None, **options):
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG)
    )
    options.setdefault("include_deps", True)
    options.setdefault("generate_sbom", True)
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / name),
            cache_dir=str(cache_dir or tmp_path / "cache"),
            **options,
        )
    )


def _forbid_install(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("a proven cache hit must not run pip")

    monkeypatch.setattr(builder_module, "install_requirements", fail)
    monkeypatch.setattr(builder_module, "install_locked_requirements", fail)


def _count_installs(monkeypatch):
    calls = []
    original = builder_module.install_requirements

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder_module, "install_requirements", counted)
    return calls


def _image(builder):
    output = Path(builder.config.output_dir)
    config = json.loads(
        (output / "blobs" / "sha256" / builder.config_digest.split(":", 1)[1]).read_text()
    )
    sbom = json.loads((output / "sbom.json").read_text())
    return (
        builder.layers[0].digest,
        config["rootfs"]["diff_ids"],
        builder.config_digest,
        [(package["name"], package["versionInfo"]) for package in sbom["packages"]],
        builder.manifest_digest,
    )


def _dependency_keys(cache_dir):
    index_path = Path(cache_dir) / "index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text())
    return {key: value for key, value in index.items() if key.startswith("deps-")}


def test_hash_pinned_requirements_hit_reuses_identical_layer_without_pip(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    assert "Dependency layer cache miss: no verified entry" in capsys.readouterr().out

    _forbid_install(monkeypatch)
    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(second) == _image(first)
    assert _image(second)[3] == [("fixture-dep", "1.0")]
    assert Path(second.layers[0].tar_path).read_bytes() == Path(first.layers[0].tar_path).read_bytes()


def test_dependency_cache_hit_preserves_conditional_app_selection(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    debug = context / "debug"
    debug.mkdir()
    (debug / "inspect.py").write_text("print('debug')\n")
    (debug / "secret.py").write_text("print('excluded')\n")
    rules = [
        {"when": 'MODE == "dev"', "include": "debug/**"},
        {"when": 'MODE == "dev"', "exclude": "debug/secret.py"},
    ]

    def build(mode, name):
        image = _builder(
            tmp_path, monkeypatch, context, name, generate_sbom=False,
            include_paths=["app.py"], build_args={"MODE": mode}, file_rules=rules,
        )
        image.build()
        return image

    dev = build("dev", "dev")
    capsys.readouterr()
    _forbid_install(monkeypatch)
    production = build("production", "production")
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert production.layers[0].digest == dev.layers[0].digest
    assert production.layers[-1].digest != dev.layers[-1].digest

    for image, includes_debug in ((dev, True), (production, False)):
        with tarfile.open(image.layers[-1].tar_path) as archive:
            names = set(archive.getnames())
        assert ("app/debug/inspect.py" in names) == includes_debug
        assert "app/debug/secret.py" not in names

    repeat = build("dev", "repeat")
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert [layer.digest for layer in repeat.layers] == [layer.digest for layer in dev.layers]


@pytest.mark.parametrize("initial_strip", [False, True])
def test_cache_separates_type_stub_stripping(tmp_path, monkeypatch, capsys, initial_strip):
    context = _project(tmp_path)
    stub = _named_wheel(context / "wheelhouse", "types-fixture", stub_only=True)
    requirements = context / "requirements.txt"
    requirements.write_text(
        requirements.read_text() + f"types-fixture==1.0 --hash=sha256:{_sha(stub)}\n"
    )
    initial = _builder(
        tmp_path, monkeypatch, context, "initial", strip_type_stubs=initial_strip
    )
    initial.build()
    capsys.readouterr()

    changed = _builder(
        tmp_path, monkeypatch, context, "changed", strip_type_stubs=not initial_strip
    )
    changed.build()
    assert "Dependency layer cache miss: no verified entry" in capsys.readouterr().out
    assert initial.layers[0].digest != changed.layers[0].digest
    assert (("types-fixture", "1.0") in _image(initial)[3]) == (not initial_strip)
    assert (("types-fixture", "1.0") in _image(changed)[3]) == initial_strip

    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "hit", strip_type_stubs=initial_strip)
    hit.build()
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(hit) == _image(initial)


@pytest.mark.parametrize("nested", [False, True])
def test_requirements_changed_during_install_cannot_poison_cache(tmp_path, monkeypatch, nested):
    context = _project(tmp_path)
    requirements = context / "requirements.txt"
    original = requirements.read_text()
    alternate = _named_wheel(context / "wheelhouse", "alternate-dep")
    changed = f"alternate-dep==1.0 --hash=sha256:{_sha(alternate)}\n"
    if nested:
        nested_dir = context / "nested"
        nested_dir.mkdir()
        requirements.write_text("--no-index\n--find-links ./wheelhouse\n-r nested/deps.txt\n")
        requirements = nested_dir / "deps.txt"
        original = original.splitlines()[2] + "\n"
        requirements.write_text(original)
    else:
        changed = "--no-index\n--find-links ./wheelhouse\n" + changed
    install = builder_module.install_requirements

    def install_with_changed_requirements(*args, **kwargs):
        requirements.write_text(changed)
        try:
            return install(*args, **kwargs)
        finally:
            requirements.write_text(original)

    with monkeypatch.context() as patch:
        patch.setattr(builder_module, "install_requirements", install_with_changed_requirements)
        _builder(tmp_path, monkeypatch, context, "changing").build()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt")
    rebuilt.build()

    assert _image(rebuilt)[3] == [("fixture-dep", "1.0")]


def test_find_links_changed_during_install_cannot_poison_cache(tmp_path, monkeypatch):
    context = _project(tmp_path)
    wheel = next((context / "wheelhouse").iterdir())
    original = wheel.read_bytes()
    alternate = _write_wheel(tmp_path / "alternate", function="alternate").read_bytes()
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0 "
        f"--hash=sha256:{hashlib.sha256(original).hexdigest()} "
        f"--hash=sha256:{hashlib.sha256(alternate).hexdigest()}\n"
    )
    expected = _builder(tmp_path, monkeypatch, context, "uncached", use_cache=False)
    expected.build()
    install = builder_module.install_requirements

    def install_with_changed_wheel(*args, **kwargs):
        wheel.write_bytes(alternate)
        try:
            return install(*args, **kwargs)
        finally:
            wheel.write_bytes(original)

    with monkeypatch.context() as patch:
        patch.setattr(builder_module, "install_requirements", install_with_changed_wheel)
        _builder(tmp_path, monkeypatch, context, "changing").build()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt")
    rebuilt.build()

    assert rebuilt.layers[0].digest == expected.layers[0].digest


def test_changed_find_links_during_snapshot_misses_without_storing(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    wheel = next((context / "wheelhouse").iterdir())
    original = wheel.read_bytes()
    alternate = _write_wheel(tmp_path / "alternate", function="alternate").read_bytes()
    materialize = builder_module.materialize_requirements_snapshot

    def snapshot_with_changed_wheel(*args, **kwargs):
        wheel.write_bytes(alternate)
        try:
            return materialize(*args, **kwargs)
        finally:
            wheel.write_bytes(original)

    monkeypatch.setattr(
        builder_module, "materialize_requirements_snapshot", snapshot_with_changed_wheel
    )
    _builder(tmp_path, monkeypatch, context, "changing").build()

    assert "local find-links changed while being snapshotted" in capsys.readouterr().out
    assert not _dependency_keys(tmp_path / "cache")


@pytest.mark.parametrize(
    "limit, reason",
    [("MAX_STAGED_BYTES", "size limit"), ("MAX_STAGED_FILES", "file limit")],
)
def test_oversized_input_snapshot_is_an_explicit_miss(tmp_path, monkeypatch, capsys, limit, reason):
    context = _project(tmp_path)
    monkeypatch.setattr(dependency_cache, limit, 0)

    _builder(tmp_path, monkeypatch, context, "out").build()

    assert f"local input snapshot exceeds its {reason}" in capsys.readouterr().out
    assert not _dependency_keys(tmp_path / "cache")


def test_unverifiable_pip_configuration_snapshot_is_an_explicit_miss(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    monkeypatch.setattr(
        dependency_cache,
        "ambient_pip_settings",
        lambda environment: dependency_cache.AmbientPipSnapshot(
            (), (), (("index-url", "https://example.invalid/simple"),), dict(environment)
        ),
    )

    _builder(tmp_path, monkeypatch, context, "out").build()

    assert "pip configuration snapshot could not be verified" in capsys.readouterr().out
    assert not _dependency_keys(tmp_path / "cache")


@pytest.mark.parametrize("source", ["config", "environment"])
def test_pip_index_changed_during_install_cannot_poison_cache(
    tmp_path, monkeypatch, indexes, source
):  # noqa: F811
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    first_wheel = _write_wheel(tmp_path / "first", function="first")
    second_wheel = _write_wheel(tmp_path / "second", function="second")
    first_index, second_index = indexes(first_wheel), indexes(second_wheel)
    (context / "requirements.txt").write_text(
        "fixture-dep==1.0 "
        f"--hash=sha256:{_sha(first_wheel)} --hash=sha256:{_sha(second_wheel)}\n"
    )
    config = tmp_path / "pip.conf"
    if source == "config":
        config.write_text(f"[global]\nindex-url = {first_index.url}\n")
        monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    else:
        monkeypatch.setenv("PIP_INDEX_URL", first_index.url)
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", ambient_pip_settings)
    expected = _builder(tmp_path, monkeypatch, context, "uncached", use_cache=False)
    expected.build()
    install = builder_module.install_requirements

    def install_with_changed_index(*args, **kwargs):
        if source == "config":
            config.write_text(f"[global]\nindex-url = {second_index.url}\n")
        else:
            monkeypatch.setenv("PIP_INDEX_URL", second_index.url)
        try:
            return install(*args, **kwargs)
        finally:
            if source == "config":
                config.write_text(f"[global]\nindex-url = {first_index.url}\n")
            else:
                monkeypatch.setenv("PIP_INDEX_URL", first_index.url)

    with monkeypatch.context() as patch:
        patch.setattr(builder_module, "install_requirements", install_with_changed_index)
        _builder(tmp_path, monkeypatch, context, "changing").build()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt")
    rebuilt.build()

    assert rebuilt.layers[0].digest == expected.layers[0].digest


@pytest.mark.parametrize("proxy_setting", ["PIP_PROXY", "HTTP_PROXY"])
def test_changed_proxy_routing_cannot_reuse_a_different_pinned_wheel(
    tmp_path, monkeypatch, capsys, proxy_setting
):
    class PackageProxy:
        def __init__(self, wheel):
            self.wheel = wheel
            self.requests = []
            proxy = self

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    path = urlsplit(self.path).path
                    proxy.requests.append(path)
                    if path.startswith("/simple/"):
                        body = (
                            f'<a href="http://fixture.invalid/files/{proxy.wheel.name}">'
                            f"{proxy.wheel.name}</a>"
                        ).encode()
                        content_type = "text/html"
                    elif path.startswith("/files/"):
                        body, content_type = proxy.wheel.read_bytes(), "application/octet-stream"
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_):
                    pass

            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.url = f"http://127.0.0.1:{self.server.server_port}"
            threading.Thread(target=self.server.serve_forever, daemon=True).start()

        def close(self):
            self.server.shutdown()
            self.server.server_close()

    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    wheels = [_write_wheel(tmp_path / name, function=name) for name in ("first", "second")]
    hashes = " ".join(f"--hash=sha256:{_sha(wheel)}" for wheel in wheels)
    (context / "requirements.txt").write_text(f"fixture-dep==1.0 {hashes}\n")
    proxies = [PackageProxy(wheel) for wheel in wheels]
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", ambient_pip_settings)
    monkeypatch.setenv("PIP_INDEX_URL", "http://fixture.invalid/simple")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "fixture.invalid")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        monkeypatch.setenv(proxy_setting, proxies[0].url)
        first = _builder(tmp_path, monkeypatch, context, "first")
        first.build()
        first_output = capsys.readouterr().out

        monkeypatch.setenv(proxy_setting, proxies[1].url)
        second = _builder(tmp_path, monkeypatch, context, "second")
        second.build()
        second_output = capsys.readouterr().out

        assert "Dependency layer cache miss: a proxy" in first_output
        assert "Dependency layer cache miss: a proxy" in second_output
        assert first.layers[0].digest != second.layers[0].digest
        assert all(proxy.requests for proxy in proxies)
        assert not _dependency_keys(tmp_path / "cache")
    finally:
        for proxy in proxies:
            proxy.close()


def test_index_auth_routing_cannot_reuse_another_pinned_wheel(tmp_path, monkeypatch, capsys):
    wheels = {
        name: _write_wheel(tmp_path / name, function=name) for name in ("first", "second")
    }
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            credentials = self.headers.get("Authorization", "")
            expected = {
                f"Basic {base64.b64encode(f'{name}:{SECRET}'.encode()).decode()}": name
                for name in wheels
            }
            name = expected.get(credentials)
            if name is None:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                self.end_headers()
                return
            requests.append(name)
            wheel = wheels[name]
            if self.path.startswith("/simple/"):
                body = f'<a href="/files/{wheel.name}">{wheel.name}</a>'.encode()
                content_type = "text/html"
            elif self.path.startswith("/files/"):
                body, content_type = wheel.read_bytes(), "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    hashes = " ".join(f"--hash=sha256:{_sha(wheel)}" for wheel in wheels.values())
    (context / "requirements.txt").write_text(f"fixture-dep==1.0 {hashes}\n")
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", ambient_pip_settings)
    try:
        builds = []
        for name in wheels:
            monkeypatch.setenv(
                "PIP_INDEX_URL", f"http://{name}:{SECRET}@127.0.0.1:{server.server_port}/simple"
            )
            build = _builder(tmp_path, monkeypatch, context, name)
            build.build()
            builds.append(build)
        output = capsys.readouterr().out
        assert output.count("credentials can change the effective package source") == 2
        assert builds[0].layers[0].digest != builds[1].layers[0].digest
        assert set(requests) == set(wheels)
        assert "Dependency layer cache hit" not in output
        assert SECRET not in output
        assert not _dependency_keys(tmp_path / "cache")
    finally:
        server.shutdown()
        server.server_close()


def test_cache_preserves_pip_configuration_precedence(tmp_path, monkeypatch, indexes):  # noqa: F811
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    config_wheel = _write_wheel(tmp_path / "config-wheel", function="config")
    env_wheel = _write_wheel(tmp_path / "env-wheel", function="environment")
    config_index, env_index = indexes(config_wheel), indexes(env_wheel)
    (context / "requirements.txt").write_text(
        "fixture-dep==1.0 "
        f"--hash=sha256:{_sha(config_wheel)} --hash=sha256:{_sha(env_wheel)}\n"
    )
    config = tmp_path / "pip.conf"
    config.write_text(
        f"[global]\nbreak-system-packages = true\n[install]\nindex-url = {config_index.url}\n"
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    monkeypatch.setenv("PIP_INDEX_URL", env_index.url)
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", ambient_pip_settings)

    expected = _builder(tmp_path, monkeypatch, context, "uncached", use_cache=False)
    expected.build()
    cached = _builder(tmp_path, monkeypatch, context, "cached")
    cached.build()
    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "hit")
    hit.build()

    assert not config_index.requests and env_index.requests
    assert cached.layers[0].digest == hit.layers[0].digest == expected.layers[0].digest


def test_nested_find_links_fallback_is_snapshotted(tmp_path, monkeypatch):
    context = _project(tmp_path)
    wheel = next((context / "wheelhouse").iterdir())
    nested = context / "nested"
    nested.mkdir()
    (context / "requirements.txt").write_text("-r nested/deps.txt\n")
    (nested / "deps.txt").write_text(
        f"--no-index\n--find-links wheelhouse\nfixture-dep==1.0 --hash=sha256:{_sha(wheel)}\n"
    )

    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "hit")
    hit.build()

    assert _image(first) == _image(hit)


def test_pythonpath_customization_cannot_reuse_an_existing_dependency_layer(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    original = _builder(tmp_path, monkeypatch, context, "original")
    original.build()
    existing_entries = _dependency_keys(tmp_path / "cache")
    capsys.readouterr()

    startup = tmp_path / SECRET
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--target' in sys.argv:\n"
        "    target = Path(sys.argv[sys.argv.index('--target') + 1])\n"
        "    (target / 'startup_payload.py').write_text('STARTUP = True\\n')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(startup))

    uncached = _builder(tmp_path, monkeypatch, context, "uncached", use_cache=False)
    uncached.build()
    assert uncached.layers[0].digest == original.layers[0].digest
    capsys.readouterr()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt")
    rebuilt.build()
    output = capsys.readouterr().out

    assert "Dependency layer cache miss: the builder Python startup environment" in output
    assert SECRET not in output
    assert rebuilt.layers[0].digest == uncached.layers[0].digest
    assert _dependency_keys(tmp_path / "cache") == existing_entries


def test_relative_pip_config_does_not_alias_distinct_indexes(
    tmp_path, monkeypatch, capsys, indexes
):  # noqa: F811
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    first_wheel = _write_wheel(tmp_path / "first-wheel", function="first")
    second_wheel = _write_wheel(tmp_path / "second-wheel", function="second")
    first_index, second_index = indexes(first_wheel), indexes(second_wheel)
    (context / "requirements.txt").write_text(
        "fixture-dep==1.0 "
        f"--hash=sha256:{_sha(first_wheel)} --hash=sha256:{_sha(second_wheel)}\n"
    )
    config = context / "local-pip.conf"
    config.write_text(f"[global]\nindex-url = {first_index.url}\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", config.name)

    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    assert "Dependency layer cache miss: a relative pip configuration path" in capsys.readouterr().out
    config.write_text(f"[global]\nindex-url = {second_index.url}\n")

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert "Dependency layer cache miss: a relative pip configuration path" in capsys.readouterr().out
    assert first_index.requests and second_index.requests
    assert first.layers[0].digest != second.layers[0].digest
    assert not _dependency_keys(tmp_path / "cache")


def test_context_pip_module_cannot_hide_ambient_pip_settings(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    (context / "pip.py").write_text("")
    monkeypatch.chdir(context)
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", ambient_pip_settings)
    monkeypatch.setenv("PIP_INDEX_URL", "https://first.invalid/simple")

    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    capsys.readouterr()
    monkeypatch.setenv("PIP_INDEX_URL", "https://second.invalid/simple")

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()
    output = capsys.readouterr().out

    assert "Dependency layer cache miss: no verified entry" in output
    assert SECRET not in output
    assert _image(second) == _image(first)
    assert len(_dependency_keys(tmp_path / "cache")) == 4


def test_cache_hit_checks_nested_requirements_for_shipped_credentials(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    nested = context / "custom" / "pip-input.in"
    nested.parent.mkdir()
    nested.write_text((context / "requirements.txt").read_text())
    (context / "requirements.txt").write_text("-r custom/pip-input.in\n")
    include_paths = ["app.py", "requirements.txt", "custom"]
    _builder(tmp_path, monkeypatch, context, "first", include_paths=include_paths).build()
    capsys.readouterr()

    lookup = ImageBuilder._cached_dependency_layer

    def add_credentials_after_lookup(self, *args, **kwargs):
        layer = lookup(self, *args, **kwargs)
        if layer:
            nested.write_text(
                nested.read_text()
                + f"--extra-index-url https://user:{SECRET}@example.invalid/simple\n"
            )
        return layer

    monkeypatch.setattr(ImageBuilder, "_cached_dependency_layer", add_credentials_after_lookup)
    with pytest.raises(ValueError, match="pip-input.in contains a URL with literal credentials") as error:
        _builder(tmp_path, monkeypatch, context, "second", include_paths=include_paths).build()
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert SECRET not in str(error.value)


def test_locked_build_hit_makes_no_index_requests(tmp_path, monkeypatch, indexes):  # noqa: F811
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    recorded = indexes(wheel)
    _write_uv_project(context, wheel, registry=recorded.url)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    requests = list(recorded.requests)
    assert requests

    _forbid_install(monkeypatch)
    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert recorded.requests == requests
    assert _image(second) == _image(first)


def _pipenv_with_optional_extra(
    tmp_path, indexes, requires=("missing-child>=1.0; extra == 'feature'",)
):
    index = indexes()
    context = _write_pipenv_project(
        tmp_path / "context",
        requires=requires,
        extras=["feature"],
        registry=index.url,
    )
    index.add(context / "wheelhouse" / "marker_root-1.0-py3-none-any.whl")
    manifest = context / "Pipfile"
    with_extra = manifest.read_text()
    without_extra = with_extra.replace(', extras = ["feature"]', "")
    lock_path = context / "Pipfile.lock"

    def update_manifest(content):
        manifest.write_text(content)
        lock = json.loads(lock_path.read_text())
        lock["_meta"]["hash"]["sha256"] = pipfile_hash(tomllib.loads(content))
        lock_path.write_text(json.dumps(lock))

    return context, update_manifest, with_extra, without_extra


def test_locked_cache_freezes_proxy_absence_during_install(tmp_path, monkeypatch, indexes, capsys):
    context, _, _, _ = _pipenv_with_optional_extra(tmp_path, indexes, requires=())
    install = builder_module.install_locked_requirements

    def install_after_proxy_change(*args, **kwargs):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        try:
            return install(*args, **kwargs)
        finally:
            monkeypatch.delenv("HTTP_PROXY", raising=False)

    with monkeypatch.context() as patch:
        patch.setattr(builder_module, "install_locked_requirements", install_after_proxy_change)
        first = _builder(tmp_path, monkeypatch, context, "first")
        first.build()
    capsys.readouterr()

    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "hit")
    hit.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(hit) == _image(first)


def test_locked_cache_uses_the_manifest_that_selected_its_packages(tmp_path, monkeypatch, indexes):
    context, update_manifest, with_extra, without_extra = _pipenv_with_optional_extra(
        tmp_path, indexes
    )
    update_manifest(without_extra)
    _builder(tmp_path, monkeypatch, context, "warm").build()
    existing = _dependency_keys(tmp_path / "cache")
    assert existing
    update_manifest(with_extra)
    roots = builder_module._locked_verification_roots

    def roots_after_manifest_change(*args, **kwargs):
        update_manifest(without_extra)
        try:
            return roots(*args, **kwargs)
        finally:
            update_manifest(with_extra)

    monkeypatch.setattr(builder_module, "_locked_verification_roots", roots_after_manifest_change)
    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        _builder(tmp_path, monkeypatch, context, "changing").build()
    assert _dependency_keys(tmp_path / "cache") == existing


def test_locked_build_without_cache_verifies_the_selected_manifest(
    tmp_path, monkeypatch, indexes
):
    context, update_manifest, _, without_extra = _pipenv_with_optional_extra(tmp_path, indexes)
    install = builder_module.install_locked_requirements

    def install_before_manifest_change(*args, **kwargs):
        result = install(*args, **kwargs)
        update_manifest(without_extra)
        return result

    monkeypatch.setattr(builder_module, "install_locked_requirements", install_before_manifest_change)
    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        _builder(tmp_path, monkeypatch, context, "uncached", use_cache=False).build()


def test_locked_cache_keys_manifest_extras_for_wheel_closure(
    tmp_path, monkeypatch, indexes, capsys
):  # noqa: F811
    context, update_manifest, with_extra, without_extra = _pipenv_with_optional_extra(
        tmp_path, indexes
    )
    update_manifest(without_extra)
    _builder(tmp_path, monkeypatch, context, "initial").build()
    capsys.readouterr()
    update_manifest(with_extra)

    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        _builder(tmp_path, monkeypatch, context, "changed").build()
    output = capsys.readouterr().out
    assert "Dependency layer cache miss: no verified entry" in output
    assert "Dependency layer cache hit" not in output


def test_locked_cache_hits_only_the_matching_requested_extras(
    tmp_path, monkeypatch, indexes, capsys
):  # noqa: F811
    context, update_manifest, with_extra, without_extra = _pipenv_with_optional_extra(
        tmp_path, indexes, requires=()
    )
    with_feature = _builder(tmp_path, monkeypatch, context, "with-feature")
    with_feature.build()
    capsys.readouterr()

    update_manifest(without_extra)
    without_feature = _builder(tmp_path, monkeypatch, context, "without-feature")
    without_feature.build()
    assert "Dependency layer cache miss: no verified entry" in capsys.readouterr().out
    assert with_feature.layers[0].digest == without_feature.layers[0].digest

    update_manifest(with_extra)
    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "with-feature-again")
    hit.build()
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(hit) == _image(with_feature)


def test_locked_cache_rejects_manifest_symlink_outside_context(
    tmp_path, monkeypatch, indexes
):  # noqa: F811
    context, _, _, _ = _pipenv_with_optional_extra(tmp_path, indexes, requires=())
    _builder(tmp_path, monkeypatch, context, "initial").build()
    # Keep the manifest out of app selection to exercise the lockfile check.
    symlinked = _builder(
        tmp_path, monkeypatch, context, "symlinked", include_paths=["Pipfile.lock"]
    )

    manifest = context / "Pipfile"
    external = tmp_path / "external-Pipfile"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(external)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Cannot create a file symlink: {error}")

    _forbid_install(monkeypatch)
    with pytest.raises(LockfileError, match="(?i)(manifest|pipfile|context)"):
        symlinked.build()


def test_locked_cache_rechecks_manifest_containment_after_lookup(tmp_path, monkeypatch, indexes):
    context, _, _, _ = _pipenv_with_optional_extra(tmp_path, indexes, requires=())
    _builder(tmp_path, monkeypatch, context, "initial").build()
    manifest = context / "Pipfile"
    external = tmp_path / "external-Pipfile"
    lookup = ImageBuilder._cached_dependency_layer

    def replace_manifest_after_lookup(self, *args, **kwargs):
        layer = lookup(self, *args, **kwargs)
        if layer:
            manifest.rename(external)
            try:
                manifest.symlink_to(external)
            except (OSError, NotImplementedError) as error:
                pytest.skip(f"Cannot create a file symlink: {error}")
        return layer

    monkeypatch.setattr(ImageBuilder, "_cached_dependency_layer", replace_manifest_after_lookup)
    with pytest.raises(LockfileError, match="inside the build context"):
        _builder(tmp_path, monkeypatch, context, "hit").build()


def test_locked_cache_verifies_the_manifest_roots_used_for_its_key(
    tmp_path, monkeypatch, indexes
):  # noqa: F811
    context, update_manifest, _, without_extra = _pipenv_with_optional_extra(
        tmp_path, indexes
    )
    install = builder_module.install_locked_requirements

    def change_manifest_after_install(*args, **kwargs):
        install(*args, **kwargs)
        update_manifest(without_extra)

    monkeypatch.setattr(builder_module, "install_locked_requirements", change_manifest_after_install)
    with pytest.raises(LockfileError, match="marker-root.*missing-child"):
        _builder(tmp_path, monkeypatch, context, "changing").build()
    assert not _dependency_keys(tmp_path / "cache")


def test_locked_build_keys_the_actual_verification_backend(
    tmp_path, monkeypatch, indexes, capsys
):  # noqa: F811
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    index = indexes(wheel)
    _write_uv_project(context, wheel, registry=index.url)
    monkeypatch.setattr(builder_module, "_locked_verification_backend", lambda: "packaging:old")
    first = _builder(tmp_path, monkeypatch, context, "old")
    first.build()
    capsys.readouterr()

    monkeypatch.setattr(builder_module, "_locked_verification_backend", lambda: "packaging:new")
    second = _builder(tmp_path, monkeypatch, context, "new")
    second.build()
    assert "Dependency layer cache miss: no verified entry" in capsys.readouterr().out
    assert _image(second) == _image(first)

    _forbid_install(monkeypatch)
    third = _builder(tmp_path, monkeypatch, context, "hit")
    third.build()
    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(third) == _image(second)


def test_locked_build_does_not_cache_an_unknown_verification_backend(
    tmp_path, monkeypatch, indexes, capsys
):  # noqa: F811
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    index = indexes(wheel)
    _write_uv_project(context, wheel, registry=index.url)
    monkeypatch.setattr(builder_module, "_locked_verification_backend", lambda: None)

    _builder(tmp_path, monkeypatch, context, "unknown").build()

    assert "Dependency layer cache miss: the locked verification inputs are unavailable" in (
        capsys.readouterr().out
    )
    assert not _dependency_keys(tmp_path / "cache")


def test_hash_pinned_target_markers_reuse_only_the_matching_architecture(
    tmp_path, monkeypatch, capsys
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    wheelhouse = context / "wheelhouse"
    root = _named_wheel(
        wheelhouse,
        "marker-root",
        requires=[
            'marker-amd==1.0; platform_machine == "x86_64"',
            'marker-arm==1.0; platform_machine == "aarch64"',
        ],
    )
    amd = _named_wheel(wheelhouse, "marker-amd")
    arm = _named_wheel(wheelhouse, "marker-arm")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        f"marker-root==1.0 --hash=sha256:{_sha(root)}\n"
        f'marker-amd==1.0; platform_machine == "x86_64" --hash=sha256:{_sha(amd)}\n'
        f'marker-arm==1.0; platform_machine == "aarch64" --hash=sha256:{_sha(arm)}\n'
    )

    x86 = _builder(tmp_path, monkeypatch, context, "x86", platform="linux/amd64")
    x86.build()
    aarch64 = _builder(tmp_path, monkeypatch, context, "arm", platform="linux/arm64")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], {**BASE_CONFIG, "architecture": arch}),
    )
    aarch64.build()

    assert set(_image(x86)[3]) == {("marker-root", "1.0"), ("marker-amd", "1.0")}
    assert set(_image(aarch64)[3]) == {("marker-root", "1.0"), ("marker-arm", "1.0")}
    assert x86.layers[0].digest != aarch64.layers[0].digest
    capsys.readouterr()

    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "arm-hit", platform="linux/arm64")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], {**BASE_CONFIG, "architecture": arch}),
    )
    hit.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(hit) == _image(aarch64)


KEY_DEFAULTS = dict(
    source_kind="requirements",
    os_name="linux",
    arch="amd64",
    python_version="3.11",
    python_full_version="3.11.8",
    base_image="python:3.11-slim",
    base_config=BASE_CONFIG,
    base_image_tag_verified=True,
    workdir="/app",
    reproducible=True,
)


def _key(context, **overrides):
    options = {**KEY_DEFAULTS, "context_dir": context, "requirements": context / "requirements.txt"}
    options.update(overrides)
    return dependency_cache_key(**options).layer


def _nested_project(tmp_path):
    context = _project(tmp_path)
    digest = _sha(next((context / "wheelhouse").iterdir()))
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n-r nested/base.txt\n-c constraints.txt\n"
    )
    (context / "nested").mkdir()
    (context / "nested" / "base.txt").write_text(f"fixture-dep==1.0 --hash=sha256:{digest}\n")
    (context / "constraints.txt").write_text("fixture-dep==1.0\n")
    return context


def _append(path, text):
    path.write_text(path.read_text() + text)


INPUT_CHANGES = {
    "nested requirements": lambda c, m: _append(
        c / "nested" / "base.txt", "other-dep==2.0 --hash=sha256:" + "b" * 64 + "\n"
    ),
    "constraint file": lambda c, m: (c / "constraints.txt").write_text("fixture-dep===1.0\n"),
    "find-links wheel content": lambda c, m: _append(c / "wheelhouse" / "extra.whl", "x"),
    "pip version": lambda c, m: m.setattr(
        dependency_cache.metadata, "version", lambda name: "0.0-test"
    ),
    "ambient index": lambda c, m: m.setattr(
        dependency_cache,
        "ambient_pip_settings",
        lambda environment: dependency_cache.AmbientPipSnapshot(
            (("index-url", "https://other.example.invalid/simple"),), (), (), dict(environment)
        ),
    ),
    "installer backend": lambda c, m: m.setattr(dependency_cache, "INSTALLER_BACKEND", "other"),
}


@pytest.mark.parametrize("change", sorted(INPUT_CHANGES))
def test_every_layer_affecting_input_changes_the_key(tmp_path, monkeypatch, change):
    context = _nested_project(tmp_path)
    (context / "wheelhouse" / "extra.whl").write_text("")
    before = _key(context)

    INPUT_CHANGES[change](context, monkeypatch)

    assert _key(context) != before


@pytest.mark.parametrize(
    "overrides",
    [
        {"arch": "arm64"},
        {"python_version": "3.12", "python_full_version": "3.12.1"},
        {"python_full_version": "3.11.9"},
        {"workdir": "/srv"},
        {
            "base_image": "python:3.11-alpine",
            "base_config": {
                "config": {
                    "Env": [
                        "PYTHON_IMPLEMENTATION=CPython",
                        "PYTHON_VERSION=3.11.8",
                        "PYTHON_LIBC=musl-1.2",
                    ]
                }
            },
        },
        {
            "base_config": {
                "config": {
                    "Env": [
                        "PYTHON_IMPLEMENTATION=CPython",
                        "PYTHON_VERSION=3.11.8",
                        "PYTHON_LIBC=glibc-2.28",
                    ]
                }
            },
        },
        {
            "base_image": "example.invalid/custom:1",
            "base_config": {
                "config": {
                    "Env": [
                        "PYTHON_IMPLEMENTATION=CPython",
                        "PYTHON_VERSION=3.11.8",
                        "PYTHON_LIBC=musl-1.2",
                    ]
                }
            },
        },
    ],
)
def test_target_tag_set_python_and_workdir_change_the_key(tmp_path, overrides):
    context = _project(tmp_path)

    assert _key(context, **overrides) != _key(context)


def test_cache_key_includes_every_target_platform_tag(tmp_path, monkeypatch):
    context = _project(tmp_path)
    original_options = dependency_cache._target_options
    before = _key(context)

    def add_compatible_platform(*args, **kwargs):
        return original_options(*args, **kwargs) + [
            "--platform",
            "manylinux_2_28_x86_64",
        ]

    monkeypatch.setattr(dependency_cache, "_target_options", add_compatible_platform)
    assert _key(context) != before


def test_relocated_context_keeps_the_key(tmp_path):
    first = _nested_project(tmp_path / "a")
    second = tmp_path / "b" / "elsewhere"
    shutil.copytree(first, second)

    assert _key(first) == _key(second)


def _lock_key(
    url, text="fixture-dep==1.0 --hash=sha256:" + "a" * 64, lock_verifier="pip-vendored"
):
    return dependency_cache_key(
        **{**KEY_DEFAULTS, "source_kind": "lock"},
        context_dir=Path("."),
        requirement_sets=[(url, f"--index-url {url}\n{text}\n")],
        lock_name="uv.lock",
        lock_kind="uv",
        lock_roots=(),
        lock_verifier=lock_verifier,
    ).layer


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"])
def test_locked_cache_misses_for_python_startup_overrides(monkeypatch, name):
    monkeypatch.setenv(name, SECRET)
    with pytest.raises(DependencyCacheMiss, match="builder Python startup environment") as error:
        _lock_key("https://pypi.org/simple")
    assert SECRET not in str(error.value)


def test_relative_pip_config_does_not_affect_locked_cache_identity(monkeypatch):
    expected = _lock_key("https://pypi.org/simple")
    monkeypatch.setenv("PIP_CONFIG_FILE", "local-pip.conf")
    assert _lock_key("https://pypi.org/simple") == expected


@pytest.mark.parametrize("name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
@pytest.mark.parametrize("source_kind", ["requirements", "lock"])
def test_proxy_environment_is_a_secret_free_cache_miss(tmp_path, monkeypatch, name, source_kind):
    context = _project(tmp_path)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv(name, f"http://user:{SECRET}@proxy.invalid")

    with pytest.raises(DependencyCacheMiss, match="a proxy") as error:
        _key(context) if source_kind == "requirements" else _lock_key("https://pypi.org/simple")

    assert SECRET not in str(error.value)


def test_system_proxy_is_a_secret_free_cache_miss(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(
        dependency_cache, "_system_proxies", lambda: {"https": f"http://user:{SECRET}@proxy.invalid"}
    )
    with pytest.raises(DependencyCacheMiss, match="a proxy") as error:
        _lock_key("https://pypi.org/simple")
    assert SECRET not in str(error.value)


def test_proxy_bypassed_for_all_hosts_remains_cacheable(tmp_path, monkeypatch):
    context = _project(tmp_path)
    expected = _key(context)
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    monkeypatch.setenv("NO_PROXY", "*")

    assert _key(context) == expected


def test_locked_cache_ignores_pip_proxy_that_locked_installs_strip(monkeypatch):
    expected = _lock_key("https://pypi.org/simple")
    monkeypatch.setenv("PIP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    assert _lock_key("https://pypi.org/simple") == expected


@pytest.mark.parametrize("source_kind", ["requirements", "lock"])
def test_cache_misses_when_netrc_is_configured(tmp_path, monkeypatch, source_kind):
    context = _project(tmp_path)
    monkeypatch.setenv("NETRC", str(tmp_path / SECRET))
    with pytest.raises(DependencyCacheMiss, match="netrc credentials") as error:
        _key(context) if source_kind == "requirements" else _lock_key("https://pypi.org/simple")
    assert SECRET not in str(error.value)


def test_cache_disables_later_default_netrc_for_an_eligible_install(tmp_path, monkeypatch):
    context = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("NETRC")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    key = dependency_cache_key(
        **KEY_DEFAULTS, context_dir=context, requirements=context / "requirements.txt"
    )
    (home / ".netrc").write_text("machine example.invalid login first password token\n")

    assert key.environment["NETRC"] == os.devnull
    with pytest.raises(DependencyCacheMiss, match="netrc credentials"):
        _key(context)


@pytest.mark.parametrize(
    "setting,value,reason",
    [
        ("PIP_CLIENT_CERT", "client.pem", "client certificate"),
        ("PIP_KEYRING_PROVIDER", "subprocess", "keyring"),
    ],
)
def test_ambient_authentication_settings_cause_secret_free_miss(
    tmp_path, monkeypatch, setting, value, reason
):
    monkeypatch.setenv(setting, str(tmp_path / SECRET) if setting == "PIP_CLIENT_CERT" else value)
    with pytest.raises(DependencyCacheMiss, match=reason) as error:
        ambient_pip_settings()
    assert SECRET not in str(error.value)


def test_python_image_metadata_does_not_disable_locked_cache(monkeypatch):
    expected = _lock_key("https://pypi.org/simple")
    monkeypatch.setenv("PYTHON_VERSION", "3.11.9")
    monkeypatch.setenv("PYTHON_GET_PIP_URL", f"https://u:{SECRET}@example.invalid/get-pip.py")
    assert _lock_key("https://pypi.org/simple") == expected


def test_locked_cache_keys_the_verification_backend():
    url = "https://pypi.org/simple"
    assert _lock_key(url, lock_verifier="packaging:25.0") != _lock_key(
        url, lock_verifier="packaging:26.0"
    )
    assert _lock_key(url, lock_verifier="pip-vendored") != _lock_key(
        url, lock_verifier="packaging:26.0"
    )


def test_secret_bearing_index_path_is_not_cacheable():
    url = f"https://dl.cloudsmith.io/{SECRET}/simple"
    with pytest.raises(DependencyCacheMiss) as error:
        _lock_key(url)
    assert SECRET not in str(error.value)


def test_locked_cache_rejects_a_shadowed_verification_backend(tmp_path, monkeypatch):
    assert lockfiles._locked_verification_backend() is not None
    shadow = tmp_path / "packaging" / "requirements.py"
    shadow.parent.mkdir()
    shadow.write_text("")
    module = sys.modules[lockfiles.Requirement.__module__]
    monkeypatch.setattr(module, "__file__", str(shadow))

    assert lockfiles._locked_verification_backend() is None


@pytest.mark.parametrize("source_kind", ["requirements", "lock"])
def test_cache_key_tracks_the_installer_target_marker_environment(
    tmp_path, monkeypatch, source_kind
):
    context = _project(tmp_path)
    key = (
        (lambda: _key(context))
        if source_kind == "requirements"
        else (lambda: _lock_key("https://pypi.org/simple"))
    )
    before = key()
    original = dependencies._target_marker_environment

    def changed(*args, **kwargs):
        return {**original(*args, **kwargs), "implementation_version": "3.11.99"}

    monkeypatch.setattr(dependency_cache, "_target_marker_environment", changed)

    assert key() != before


@pytest.mark.parametrize(
    "old_backend",
    [
        "pip-target-v1", "pip-target-v2", "pip-target-v3", "pip-target-v4",
        "pip-target-v5", "pip-target-v6", "pip-target-v7", "pip-target-v8",
    ],
)
def test_cache_does_not_reuse_previous_installer_backends(tmp_path, monkeypatch, old_backend):
    context = _project(tmp_path)
    current_backend = dependency_cache.INSTALLER_BACKEND
    monkeypatch.setattr(dependency_cache, "INSTALLER_BACKEND", old_backend)
    legacy_key = _key(context)
    cache = LayerCache(tmp_path / "cache")
    cache.store_bytes(legacy_key, b"legacy dependency layer", ["requirements.txt"])

    monkeypatch.setattr(dependency_cache, "INSTALLER_BACKEND", current_backend)

    assert cache.get_entry(_key(context)) is None


def test_lock_selection_is_recomputed_for_a_different_target(tmp_path, monkeypatch):
    context = _write_uv_project(tmp_path / "context")
    for path in (context / "pyproject.toml", context / "uv.lock"):
        path.write_text(
            path.read_text().replace("sys_platform == 'win32'", "platform_machine == 'aarch64'")
        )
    builder = _builder(tmp_path, monkeypatch, context, "out")

    amd = builder._lock_selection(BASE_CONFIG, "linux", "amd64", base_image_tag_verified=True)
    arm = builder._lock_selection(
        {**BASE_CONFIG, "architecture": "arm64"},
        "linux", "arm64", base_image_tag_verified=True,
    )

    assert {package.name for package in amd.packages} == {"fixture-dep"}
    assert {package.name for package in arm.packages} == {"fixture-dep", "colorama"}


def test_unknown_full_version_cannot_reuse_known_marker_environment(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    wheel = next((context / "wheelhouse").iterdir())
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        f'fixture-dep==1.0; python_full_version >= "3.11.1" --hash=sha256:{_sha(wheel)}\n'
    )
    _builder(tmp_path, monkeypatch, context, "known").build()
    existing = _dependency_keys(tmp_path / "cache")
    capsys.readouterr()

    unknown = _builder(tmp_path, monkeypatch, context, "unknown")
    unknown_config = {
        **BASE_CONFIG,
        "config": {
            **BASE_CONFIG["config"],
            "Env": [
                "PYTHON_VERSION=3.11",
                "PYTHON_IMPLEMENTATION=CPython",
                "PYTHON_LIBC=glibc-2.17",
            ],
        },
    }
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], unknown_config),
    )
    with pytest.raises(ValueError, match="python_full_version"):
        unknown.build()

    assert "Dependency layer cache hit" not in capsys.readouterr().out
    assert _dependency_keys(tmp_path / "cache") == existing


def test_unverified_base_cannot_reuse_a_trusted_dependency_layer(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "trusted").build()
    capsys.readouterr()
    _forbid_install(monkeypatch)

    unverified = _builder(
        tmp_path, monkeypatch, context, "unverified",
        base_image="python:3.11-slim@sha256:" + "a" * 64,
    )
    unverified_config = {
        **BASE_CONFIG,
        "config": {**BASE_CONFIG["config"], "Env": ["PYTHON_VERSION=3.11.8"]},
    }
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], unverified_config),
    )
    with pytest.raises(ValueError, match="Python ABI"):
        unverified.build()

    assert "Dependency layer cache hit" not in capsys.readouterr().out


def test_index_credentials_and_query_routing_are_explicit_misses():
    plain = _lock_key("https://pkgs.example.invalid/simple")

    for url in (
        "https://${INDEX_USER}:${INDEX_TOKEN}@pkgs.example.invalid/simple",
        f"https://user:{SECRET}@pkgs.example.invalid/simple",
    ):
        with pytest.raises(DependencyCacheMiss, match="credentials can change") as miss:
            _lock_key(url)
        assert SECRET not in str(miss.value) and "INDEX_TOKEN" not in str(miss.value)
    assert _lock_key("https://pkgs.example.invalid/other/simple") != plain
    for url in (
        "https://pkgs.example.invalid/simple?",
        "https://pkgs.example.invalid/simple?feed=tenant-a",
        "https://pkgs.example.invalid/simple?feed=tenant-b",
        "https://pkgs.example.invalid/simple?token=${INDEX_TOKEN}",
    ):
        with pytest.raises(DependencyCacheMiss, match="query string") as miss:
            _lock_key(url)
        assert "tenant" not in str(miss.value) and "INDEX_TOKEN" not in str(miss.value)
    with pytest.raises(DependencyCacheMiss, match="placeholder in its host or path"):
        _lock_key("https://${INDEX_HOST}/simple")
    with pytest.raises(DependencyCacheMiss, match="fragment"):
        _lock_key("https://pkgs.example.invalid/simple#tenant")
    with pytest.raises(DependencyCacheMiss, match="not hash-pinned"):
        _lock_key("https://pkgs.example.invalid/simple", "fixture-dep==1.0")


def test_find_links_url_path_trailing_slashes_do_not_alias(tmp_path):
    context = _project(tmp_path)
    requirements = context / "requirements.txt"
    content = requirements.read_text().replace(
        "./wheelhouse", "https://pkgs.example.invalid/wheels"
    )
    requirements.write_text(content)
    without_slash = _key(context)
    requirements.write_text(content.replace("/wheels", "/wheels/"))

    assert _key(context) != without_slash


def test_utf16_file_used_as_requirement_and_constraint_is_checked_in_both_roles(tmp_path):
    context = _project(tmp_path)
    (context / "requirements.txt").write_text(
        "-r shared.txt\n-c shared.txt\n", encoding="utf-16"
    )
    shared = context / "shared.txt"
    shared.write_text("fixture-dep==1.0\n", encoding="utf-16")
    with pytest.raises(DependencyCacheMiss, match="not fully pinned"):
        _key(context)

    shared.write_text(
        "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-16"
    )
    original = _key(context)
    shared.write_text(
        "fixture-dep==1.0 --hash=sha256:" + "b" * 64 + "\n", encoding="utf-16"
    )
    assert _key(context) != original


@pytest.mark.parametrize(
    "requirements, reason",
    [
        ("fixture-dep==1.0\n", "not fully pinned"),
        ("fixture-dep>=1.0 --hash=sha256:" + "a" * 64 + "\n", "not fully pinned"),
        ("-e ./local\n", "not fully pinned"),
        ("--no-deps\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n", "--no-deps"),
        (
            "--index-url https://pkgs.example.invalid/simple?feed=a\n"
            "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "query string",
        ),
        (
            "--find-links https://pkgs.example.invalid/wheels?feed=a\n"
            "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "query string",
        ),
        (
            "--find-links file:///wheels\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "file: URL find-links",
        ),
        (
            "--find-links file:/wheels\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "file: URL find-links",
        ),
        (
            "--index-url file:///index\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "non-HTTP index",
        ),
        (
            "--find-links https://pkgs.example.invalid/wheels#sha256=abc\n"
            "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "fragment or ambiguous comment",
        ),
        (
            "--index-url https://${HOST}/simple\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "placeholder in its host or path",
        ),
    ],
)
def test_unprovable_requirements_are_explicit_misses(tmp_path, requirements, reason):
    context = _project(tmp_path, requirements)

    with pytest.raises(DependencyCacheMiss, match=reason):
        _key(context)


def test_direct_url_constraints_are_misses(tmp_path):
    context = _project(tmp_path, "-c constraints.txt\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n")
    (context / "constraints.txt").write_text("fixture-dep @ https://example.invalid/f.whl\n")

    with pytest.raises(DependencyCacheMiss, match="direct URL"):
        _key(context)


@pytest.mark.parametrize(
    "options, reason",
    [
        ({"reproducible": False}, "non-reproducible"),
        ({"source_kind": "venv"}, "virtualenv"),
    ],
)
def test_venv_and_non_reproducible_sources_are_misses(tmp_path, options, reason):
    with pytest.raises(DependencyCacheMiss, match=reason):
        _key(_project(tmp_path), **options)


def test_builder_reports_miss_reason_and_stores_nothing_for_unpinned(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path, "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n")

    _builder(tmp_path, monkeypatch, context, "out").build()

    assert "Dependency layer cache miss: requirements are not fully pinned" in capsys.readouterr().out
    assert _dependency_keys(tmp_path / "cache") == {}


def _wheel_requiring(directory, dependency):
    path = directory / "needs_more-1.0-py3-none-any.whl"
    files = {
        "needs_more/__init__.py": b"",
        "needs_more-1.0.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: needs-more\nVersion: 1.0\nRequires-Dist: {dependency}\n\n"
        ).encode(),
        "needs_more-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((name, f"sha256={digest}", len(content)))
    writer.writerow(("needs_more-1.0.dist-info/RECORD", "", ""))
    files["needs_more-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_transitively_unpinned_closure_fails_without_storing(tmp_path, monkeypatch):
    context = _project(tmp_path)
    wheel = _wheel_requiring(context / "wheelhouse", "fixture-dep")
    (context / "requirements.txt").write_text(
        f"--no-index\n--find-links ./wheelhouse\nneeds-more==1.0 --hash=sha256:{_sha(wheel)}\n"
    )

    with pytest.raises(RuntimeError):
        _builder(tmp_path, monkeypatch, context, "out").build()

    assert _dependency_keys(tmp_path / "cache") == {}


def _layer_blob(cache_dir):
    key = next(key for key in _dependency_keys(cache_dir) if key.startswith("deps-v"))
    digest = json.loads((Path(cache_dir) / "index.json").read_text())[key]["digest"]
    return Path(cache_dir) / "blobs" / "sha256" / digest.split(":", 1)[1]


def _inventory_key(cache_dir):
    return next(key for key in _dependency_keys(cache_dir) if key.startswith("deps-inventory"))


def test_corrupt_layer_blob_is_discarded_and_rebuilt(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    _layer_blob(tmp_path / "cache").write_bytes(b"corrupt")
    installs = _count_installs(monkeypatch)
    capsys.readouterr()

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert len(installs) == 1
    assert "no verified entry" in capsys.readouterr().out
    assert _image(second) == _image(first)


@pytest.mark.parametrize("damage", ["missing", "other-layer", "garbage"])
def test_missing_or_mismatched_inventory_is_a_miss(tmp_path, monkeypatch, damage):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "first").build()
    cache = LayerCache(tmp_path / "cache")
    key = _inventory_key(tmp_path / "cache")
    if damage == "missing":
        index_path = tmp_path / "cache" / "index.json"
        index = json.loads(index_path.read_text())
        del index[key]
        index_path.write_text(json.dumps(index))
    else:
        content = (
            encode_inventory("sha256:" + "0" * 64, [("fixture-dep", "1.0")])
            if damage == "other-layer"
            else b"not json"
        )
        cache.store_bytes(key, content, [])
    installs = _count_installs(monkeypatch)

    _builder(tmp_path, monkeypatch, context, "second").build()

    assert len(installs) == 1


def test_decode_inventory_rejects_wrong_shapes():
    layer = "sha256:" + "1" * 64
    assert decode_inventory(encode_inventory(layer, [("a", "1")]), layer) == [("a", "1")]
    assert decode_inventory(b'{"version":1,"layer":"' + layer.encode() + b'","packages":[["a"]]}', layer) is None
    assert decode_inventory(b"\xff", layer) is None


def test_malformed_cache_index_fails_explicitly(tmp_path, monkeypatch):
    context = _project(tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "index.json").write_text("{not json")

    with pytest.raises(CacheIndexError):
        _builder(tmp_path, monkeypatch, context, "out").build()


def test_interruption_between_layer_and_inventory_is_a_later_miss(tmp_path, monkeypatch):
    context = _project(tmp_path)

    def interrupted(self, key, content, source_files):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(LayerCache, "store_bytes", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _builder(tmp_path, monkeypatch, context, "first").build()
    installs = _count_installs(monkeypatch)

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()
    _forbid_install(monkeypatch)
    third = _builder(tmp_path, monkeypatch, context, "third")
    third.build()

    assert len(installs) == 1
    assert _image(third) == _image(second)


def _store_dependency_entry(cache_dir, layer_path, digest, barrier):
    cache = LayerCache(Path(cache_dir))
    barrier.wait(timeout=20)
    cache.store_entry("deps-v1:sha256:" + "c" * 64, digest, Path(layer_path), ["requirements.txt"])
    cache.store_bytes(
        "deps-inventory-v1:sha256:" + "c" * 64,
        encode_inventory(digest, [("fixture-dep", "1.0")]),
        ["requirements.txt"],
    )


def test_concurrent_writers_publish_one_verified_entry(tmp_path):
    layer = tmp_path / "layer.tar"
    layer.write_bytes(b"dependency layer" * 1024)
    digest = f"sha256:{_sha(layer)}"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [
        context.Process(
            target=_store_dependency_entry, args=(str(tmp_path / "cache"), str(layer), digest, barrier)
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0

    cache = LayerCache(tmp_path / "cache")
    assert cache.get_entry("deps-v1:sha256:" + "c" * 64)[0] == digest
    content = cache.get_bytes("deps-inventory-v1:sha256:" + "c" * 64, 1024)
    assert decode_inventory(content, digest) == [("fixture-dep", "1.0")]
    assert not list((tmp_path / "cache").glob(".entry-*"))


def test_disabled_cache_is_silent_and_relocated_cache_hits(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "off", use_cache=False).build()
    assert "Dependency layer cache" not in capsys.readouterr().out

    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    relocated_context = tmp_path / "moved" / "context"
    shutil.copytree(context, relocated_context)
    shutil.copytree(tmp_path / "cache", tmp_path / "moved-cache")
    _forbid_install(monkeypatch)
    capsys.readouterr()

    second = _builder(
        tmp_path, monkeypatch, relocated_context, "second", cache_dir=tmp_path / "moved-cache"
    )
    second.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(second) == _image(first)


def test_cache_metadata_and_output_never_contain_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INDEX_USER", "user")
    monkeypatch.setenv("INDEX_TOKEN", SECRET)
    monkeypatch.setattr(
        dependency_cache,
        "ambient_pip_settings",
        lambda environment: dependency_cache.AmbientPipSnapshot(
            (("extra-index-url", "https://ambient.example.invalid/simple"),),
            ({":env:.extra-index-url": f"https://user:{SECRET}@ambient.example.invalid/simple"},),
            (("extra-index-url", f"https://user:{SECRET}@ambient.example.invalid/simple"),),
            dict(environment),
        ),
    )
    context = _project(tmp_path)
    wheel_digest = _sha(next((context / "wheelhouse").iterdir()))
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        "--extra-index-url https://${INDEX_USER}:${INDEX_TOKEN}@pkgs.example.invalid/simple\n"
        f"fixture-dep==1.0 --hash=sha256:{wheel_digest}\n",
    )

    _builder(tmp_path, monkeypatch, context, "first").build()
    _builder(tmp_path, monkeypatch, context, "second").build()

    output = capsys.readouterr().out
    assert output.count("credentials can change the effective package source") == 2
    assert not _dependency_keys(tmp_path / "cache")
    assert SECRET not in output and "INDEX_TOKEN" not in output and "user:" not in output


def test_parse_pip_config_keys_sanitized_settings_and_rejects_unprovable_ones():
    rows = parse_pip_config(
        ":env:.index-url='https://Pkgs.Example.invalid/simple/'\n"
        "global.timeout='60'\n"
        ":env:.config-file='/etc/pip.conf'\n"
        "install.only-binary=':all:'\n"
        "freeze.all='true'\n"
    )
    assert rows == [
        [":env:", "index-url", "https://pkgs.example.invalid/simple/"],
        ["install", "only-binary", ":all:"],
    ]
    for line, reason in [
        (":env:.constraint='/tmp/c.txt'", "constraint is not provably keyed"),
        ("global.find-links='/wheels'", "local files"),
        (f":env:.index-url='https://u:{SECRET}@example.invalid/simple'", "credentials can change"),
        ("global.client-cert='/tmp/client.pem'", "client certificate"),
        ("global.keyring-provider='subprocess'", "keyring"),
        ("global.index-url='https://example.invalid/simple?feed=a'", "query string"),
        ("global.only-binary='${PKGS}'", "cannot be keyed"),
        ("not a config line", "could not be parsed"),
        ("global.index-url=unquoted", "could not be parsed"),
    ]:
        with pytest.raises(DependencyCacheMiss, match=reason) as miss:
            parse_pip_config(line + "\n")
        assert "/tmp" not in str(miss.value) and "feed" not in str(miss.value)


@pytest.mark.parametrize("nested", [False, True], ids=["pip-25-flat", "pip-26-files"])
@pytest.mark.parametrize("unsafe_shadowed", [False, True])
def test_pip_config_snapshot_preserves_levels_and_effective_options(
    monkeypatch, capsys, nested, unsafe_shadowed,
):
    from pip._internal import commands, configuration

    global_settings = {
        "global.index-url": "https://example.invalid/simple?feed=private"
        if unsafe_shadowed else "https://example.invalid/simple",
        "global.timeout": "10",
    }
    user_settings = {"global.timeout": "20"}
    env_settings = {":env:.index-url": "https://public.invalid/simple"}
    levels = [global_settings, user_settings, env_settings]
    config = SimpleNamespace(
        load=lambda: None,
        _config={
            variant: {f"source-{variant}": values} if nested else values
            for variant, values in enumerate(levels)
        },
    )
    parser = SimpleNamespace(
        config=config,
        _get_ordered_configuration_items=lambda: [
            ("timeout", "20"), ("index-url", "https://public.invalid/simple"),
        ],
    )
    monkeypatch.setattr(configuration, "OVERRIDE_ORDER", (0, 1, 2))
    monkeypatch.setattr(commands, "create_command", lambda name: SimpleNamespace(parser=parser))
    exec(dependency_cache._PIP_CONFIG_SNAPSHOT, {})
    captured = capsys.readouterr().out
    assert json.loads(captured)["configuration"] == levels
    monkeypatch.setattr(
        dependency_cache.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=captured),
    )
    if unsafe_shadowed:
        with pytest.raises(DependencyCacheMiss, match="query string"):
            ambient_pip_settings()
    else:
        snapshot = ambient_pip_settings()
        assert snapshot.configuration == tuple(levels)
        assert snapshot.ordered == (
            ("timeout", "20"), ("index-url", "https://public.invalid/simple"),
        )
        assert any(value == "https://example.invalid/simple" for _, value in snapshot.identity)


def test_pip_config_snapshot_rejects_mixed_shapes(monkeypatch):
    from pip._internal import commands, configuration

    config = SimpleNamespace(
        load=lambda: None,
        _config={0: {"global.timeout": "10", "source": {"global.timeout": "20"}}},
    )
    monkeypatch.setattr(configuration, "OVERRIDE_ORDER", (0,))
    monkeypatch.setattr(
        commands, "create_command", lambda name: SimpleNamespace(parser=SimpleNamespace(config=config)),
    )
    with pytest.raises(ValueError, match="unsupported pip configuration shape"):
        exec(dependency_cache._PIP_CONFIG_SNAPSHOT, {})


def test_real_frozen_pip_configuration_preserves_source_precedence():
    snapshot = {
        "configuration": [
            {"global.index-url": "https://global.invalid/simple", "global.timeout": "10"},
            {"install.index-url": "https://install.invalid/simple"},
            {":env:.index-url": "https://captured.invalid/simple"},
        ],
        "ordered": [
            ["index-url", "https://global.invalid/simple"], ["timeout", "10"],
            ["index-url", "https://install.invalid/simple"],
            ["index-url", "https://captured.invalid/simple"],
        ],
    }
    script = """
import json, os, sys
from pycontainer._pip_target import _pin_pip_configuration, _freeze_pip_configuration
from pip._internal.commands import create_command
os.environ["PIP_INDEX_URL"] = "https://changed.invalid/simple"
_pin_pip_configuration({"index-url", "timeout"})
_freeze_pip_configuration(json.loads(sys.argv[1]))
options, _ = create_command("install").parse_args([])
print(json.dumps([options.index_url, options.timeout, options.platforms]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(snapshot)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(dependency_cache.__file__).parents[1])},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["https://captured.invalid/simple", 10, None]


def test_real_ambient_pip_config_is_read_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.undo()
    for name in [name for name in os.environ if name.upper().startswith("PIP_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_INDEX_URL", "https://pkgs.example.invalid/caf\u00e9")
    startup = tmp_path / "pythonpath"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text("print('unexpected startup hook')\n")
    monkeypatch.setenv("PYTHONPATH", str(startup))

    rows = dependency_cache.ambient_pip_settings().identity

    assert any(
        name.endswith(":env:.index-url") and value == "https://pkgs.example.invalid/caf\u00e9"
        for name, value in rows
    )
    assert SECRET not in json.dumps(rows)
    assert metadata.version("pip")


def test_shadowed_pip_config_query_is_an_explicit_miss(tmp_path, monkeypatch):
    config = tmp_path / "pip.conf"
    config.write_text(
        "[global]\nbreak-system-packages = true\n"
        "index-url = https://example.invalid/simple?feed=private\n"
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")

    with pytest.raises(DependencyCacheMiss, match="query string") as error:
        ambient_pip_settings()

    assert "private" not in str(error.value)


def test_pip_config_proxy_is_a_secret_free_cache_miss(tmp_path, monkeypatch):
    config = tmp_path / "pip.conf"
    config.write_text(
        f"[global]\nbreak-system-packages = true\nproxy = http://user:{SECRET}@proxy.invalid\n"
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", str(config))

    with pytest.raises(DependencyCacheMiss, match="a proxy") as error:
        ambient_pip_settings()

    assert SECRET not in str(error.value)


def test_undecodable_pip_configuration_is_an_explicit_miss(monkeypatch):
    def undecodable(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

    monkeypatch.setattr(dependency_cache.subprocess, "run", undecodable)
    with pytest.raises(DependencyCacheMiss, match="ambient pip configuration could not be read"):
        ambient_pip_settings()
