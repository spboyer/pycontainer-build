"""Hermetic tests for the registry-backed remote application-layer cache."""
import base64
import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from pycontainer import remote_cache
from pycontainer import auth as auth_module
from pycontainer.auth import (
    AzureCLIAuthProvider,
    ChainAuthProvider,
    EnvironmentAuthProvider,
    RegistryAuth,
)
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import (
    RegistryClient,
    RegistryRequestError,
    SameOriginAuthRedirect,
)
from pycontainer.remote_cache import (
    RegistryCacheRef,
    RegistryLayerCache,
    RemoteCacheError,
    app_layer_cache_key,
    build_record,
    cache_tag,
    parse_cache_spec,
)


CACHE_HOST = "cache.example"
CACHE_REPO = "team/app-cache"
CACHE_SPEC = f"type=registry,ref={CACHE_HOST}/{CACHE_REPO}"
STORAGE_HOST = "storage.example"
CACHE_AUTH = "Basic " + base64.b64encode(b"cache-user:cache-pass").decode()
BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _digest(data):
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class _Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size if size is not None else -1)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _http_error(url, code, headers=None, body=b""):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "error", message, io.BytesIO(body))


class FakeRegistry:
    """In-memory Registry v2 subset reached through patched transports."""

    def __init__(self):
        self.blobs = {}
        self.manifests = {}
        self.requests = []
        self.fail = {}
        self.redirect_blobs = False
        self.lock = threading.Lock()
        self._uploads = 0

    def open(self, request, timeout=None):
        if isinstance(request, str):
            request = urllib.request.Request(request)
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        method = request.get_method()
        with self.lock:
            self.requests.append(
                (parsed.netloc, method, parsed.path, request.get_header("Authorization"))
            )
        if parsed.netloc == STORAGE_HOST:
            digest = urllib.parse.parse_qs(parsed.query)["digest"][0]
            return self._blob_response(digest)
        if parsed.netloc != CACHE_HOST:
            raise urllib.error.URLError(f"unknown host {parsed.netloc}")
        if request.get_header("Authorization") != CACHE_AUTH:
            raise _http_error(url, 401)

        prefix = f"/v2/{CACHE_REPO}/"
        path = parsed.path[len(prefix):]
        for (fail_method, fail_path), code in self.fail.items():
            if method == fail_method and path.startswith(fail_path):
                raise _http_error(url, code)
        if path.startswith("manifests/"):
            return self._manifest(url, method, path.split("/", 1)[1], request)
        if path == "blobs/uploads/" and method == "POST":
            with self.lock:
                self._uploads += 1
                upload = self._uploads
            return _Response(202, headers={"Location": f"{prefix}blobs/uploads/{upload}"})
        if path.startswith("blobs/uploads/") and method == "PUT":
            digest = urllib.parse.parse_qs(parsed.query)["digest"][0]
            data = request.data.read() if hasattr(request.data, "read") else request.data
            if _digest(data) != digest:
                raise _http_error(url, 400)
            with self.lock:
                self.blobs[digest] = data
            return _Response(201)
        if path.startswith("blobs/"):
            digest = path.split("/", 1)[1]
            if digest not in self.blobs:
                raise _http_error(url, 404)
            if method == "HEAD":
                return _Response(200)
            if self.redirect_blobs:
                raise _http_error(
                    url, 307, {"Location": f"https://{STORAGE_HOST}/b?digest={digest}"}
                )
            return self._blob_response(digest)
        raise _http_error(url, 404)

    def _blob_response(self, digest):
        data = self.blobs[digest]
        return _Response(200, data, {"Content-Length": str(len(data))})

    def _manifest(self, url, method, reference, request):
        if method == "PUT":
            with self.lock:
                self.manifests[reference] = bytes(request.data)
            return _Response(201)
        if reference not in self.manifests:
            raise _http_error(url, 404)
        body = self.manifests[reference]
        return _Response(200, body, {"Docker-Content-Digest": _digest(body)})

    def put_record(self, tag, manifest):
        self.manifests[tag] = json.dumps(manifest).encode()

    def manifest_puts(self):
        return [r for r in self.requests if r[1] == "PUT" and "/manifests/" in r[2]]


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    auth_requests = []

    def fake_auth(host, **_):
        auth_requests.append(host)
        if host == CACHE_HOST:
            return RegistryAuth(username="cache-user", password="cache-pass")
        return RegistryAuth()

    class _Opener:
        def open(self, request, timeout=None):
            return fake.open(request, timeout)

    monkeypatch.setattr("pycontainer.registry_client._open_url", fake.open)
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *handlers: _Opener(),
    )
    monkeypatch.setattr(remote_cache, "get_registry_auth", fake_auth)
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )
    fake.auth_requests = auth_requests
    return fake


def _builder(tmp_path, name, **options):
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    if not (context / "app.py").exists():
        (context / "app.py").write_text("print('hello')\n")
    options.setdefault("cache_dir", str(tmp_path / f"{name}-cache"))
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / f"{name}-out"),
            include_paths=["app.py"],
            **options,
        )
    )


def _app_layer(builder, tmp_path, name):
    layers = tmp_path / f"{name}-layers"
    layers.mkdir()
    return builder._create_app_layer(layers, builder.context_include_paths)


def _key_for(builder):
    files = [(Path(builder.config.context_dir) / "app.py", Path("app.py"))]
    identity = builder.cache.files_identity(files, "/app", True)
    return app_layer_cache_key(identity)


def _forbid_tar_build(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("expected a remote cache hit, not a rebuild")

    monkeypatch.setattr(ImageBuilder, "_write_app_layer", fail)


def _export(tmp_path, registry):
    writer = _builder(tmp_path, "writer", cache_to=CACHE_SPEC)
    layer = _app_layer(writer, tmp_path, "writer")
    return writer, layer


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("type=s3,ref=bucket/app", "#61"),
        ("type=registry", "requires ref"),
        ("ref=cache.example/app", "only type=registry"),
        (f"{CACHE_SPEC},mode=max", "unsupported key"),
        ("type=registry,type=registry,ref=cache.example/app", "more than once"),
        ("type=registry,ref=cache.example/app:tag", "without a tag"),
        (f"type=registry,ref=cache.example/app@sha256:{'a' * 64}", "without a tag"),
        ("registry", "key=value"),
    ],
)
def test_cache_spec_rejects_unsupported_input(spec, message):
    with pytest.raises(ValueError, match=message):
        parse_cache_spec(spec)


def test_cache_spec_and_config_validation():
    assert parse_cache_spec("type=registry,ref=localhost:5000/cache") == RegistryCacheRef(
        "localhost:5000", "cache"
    )
    with pytest.raises(ValueError, match="#61"):
        BuildConfig(cache_from="type=gcs,ref=bucket/cache")
    with pytest.raises(ValueError, match="reproducible"):
        BuildConfig(cache_to=CACHE_SPEC, reproducible=False)


def test_cache_key_covers_sources_and_toolchain(monkeypatch):
    baseline = app_layer_cache_key("v2:sha256:" + "1" * 64)
    assert app_layer_cache_key("v2:sha256:" + "2" * 64) != baseline
    monkeypatch.setattr(remote_cache.platform, "python_version", lambda: "0.0.0")
    assert app_layer_cache_key("v2:sha256:" + "1" * 64) != baseline
    assert len(cache_tag(baseline)) <= 128


def test_cold_export_then_warm_import_skips_rebuild(tmp_path, registry, monkeypatch):
    writer, cold = _export(tmp_path, registry)
    key = _key_for(writer)
    assert cache_tag(key) in registry.manifests
    assert cold.digest in registry.blobs

    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    _forbid_tar_build(monkeypatch)
    warm = _app_layer(reader, tmp_path, "reader")

    assert (warm.digest, warm.size) == (cold.digest, cold.size)
    assert _digest(Path(warm.tar_path).read_bytes()) == cold.digest
    registry.requests.clear()
    local = _app_layer(reader, tmp_path, "reader-local")
    assert local.digest == cold.digest
    assert registry.requests == []


@pytest.mark.xfail(strict=True, reason="hard-link cache identity requires #230")
def test_cache_key_distinguishes_hardlinks(tmp_path, registry):
    writer = _builder(tmp_path, "writer", cache_to=CACHE_SPEC)
    writer.context_include_paths.append("copy.py")
    context = Path(writer.config.context_dir)
    source, copy = context / "app.py", context / "copy.py"
    copy.write_bytes(source.read_bytes())
    independent = _app_layer(writer, tmp_path, "writer")
    with tarfile.open(independent.tar_path) as archive:
        assert archive.getmember("app/copy.py").isreg()

    copy.unlink()
    try:
        os.link(source, copy)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")
    writer.remote_cache_to = None
    local = _app_layer(writer, tmp_path, "writer-linked")
    with tarfile.open(local.tar_path) as archive:
        assert archive.getmember("app/copy.py").islnk()
    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    reader.context_include_paths.append("copy.py")
    linked = _app_layer(reader, tmp_path, "reader")
    with tarfile.open(linked.tar_path) as archive:
        assert archive.getmember("app/copy.py").islnk()
    assert linked.digest == local.digest
    assert linked.digest != independent.digest


def test_full_build_uses_remote_layer(tmp_path, registry, monkeypatch):
    _, cold = _export(tmp_path, registry)
    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    _forbid_tar_build(monkeypatch)

    reader.build()

    assert [layer.digest for layer in reader.layers] == [cold.digest]
    assert (Path(reader.config.output_dir) / "index.json").is_file()


def test_no_cache_disables_remote_import(tmp_path):
    builder = _builder(tmp_path, "nocache", cache_from=CACHE_SPEC, cache_to=CACHE_SPEC, use_cache=False)
    assert builder.remote_cache_from is None
    assert builder.remote_cache_to is not None


def test_export_is_idempotent_and_concurrent_writers_agree(tmp_path, registry):
    writer, layer = _export(tmp_path, registry)
    key = _key_for(writer)
    tag = cache_tag(key)
    record = registry.manifests[tag]

    puts_before = len(registry.manifest_puts())
    assert writer.remote_cache_to.store(key, layer.digest, Path(layer.tar_path), layer.size) is False
    assert len(registry.manifest_puts()) == puts_before

    del registry.manifests[tag]
    errors = []

    def write():
        try:
            RegistryLayerCache(writer.remote_cache_to.ref).store(
                key, layer.digest, Path(layer.tar_path), layer.size
            )
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert registry.manifests[tag] == record
    assert RegistryLayerCache(writer.remote_cache_to.ref).fetch(key, tmp_path / "x") is not None


@pytest.mark.parametrize("missing", ["layer", "config"])
def test_export_restores_missing_blob_for_current_record(tmp_path, registry, missing):
    writer, layer = _export(tmp_path, registry)
    key = _key_for(writer)
    tag = cache_tag(key)
    record = json.loads(registry.manifests[tag])
    digest = layer.digest if missing == "layer" else record["config"]["digest"]
    del registry.blobs[digest]
    manifest_puts = len(registry.manifest_puts())

    assert writer.remote_cache_to.store(
        key, layer.digest, Path(layer.tar_path), layer.size
    ) is False
    assert digest in registry.blobs
    assert len(registry.manifest_puts()) == manifest_puts
    assert RegistryLayerCache(writer.remote_cache_to.ref).fetch(key, tmp_path / "reader")


@pytest.mark.parametrize("failure", [("PUT", "manifests/"), ("POST", "blobs/uploads/")])
def test_partial_publication_raises_and_readers_miss(tmp_path, registry, failure):
    registry.fail[failure] = 500
    writer = _builder(tmp_path, "writer", cache_to=CACHE_SPEC)

    with pytest.raises(RemoteCacheError, match="Failed to export"):
        _app_layer(writer, tmp_path, "writer")

    assert registry.manifests == {}
    registry.fail.clear()
    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    rebuilt = _app_layer(reader, tmp_path, "reader")
    assert _digest(Path(rebuilt.tar_path).read_bytes()) == rebuilt.digest


def _rewrite_record(registry, tag, mutate):
    manifest = json.loads(registry.manifests[tag])
    mutate(manifest)
    registry.put_record(tag, manifest)


def _add_config(registry, config):
    data = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    registry.blobs[_digest(data)] = data
    return {"mediaType": remote_cache.CACHE_CONFIG_MEDIA_TYPE, "digest": _digest(data), "size": len(data)}


def _swap_layer(registry):
    other = b"not the application layer"
    registry.blobs[_digest(other)] = other

    def mutate(manifest):
        manifest["layers"][0].update(digest=_digest(other), size=len(other))

    return mutate


@pytest.mark.parametrize(
    "case",
    [
        "tampered-layer",
        "layer-swap",
        "wrong-artifact-type",
        "extra-field",
        "two-layers",
        "config-key-mismatch",
        "oversized-manifest",
        "uppercase-digest",
        "nested-manifest",
        "nested-config",
    ],
)
def test_invalid_records_are_rejected_and_rebuilt(tmp_path, registry, monkeypatch, caplog, case):
    writer, cold = _export(tmp_path, registry)
    key = _key_for(writer)
    tag = cache_tag(key)

    if case == "tampered-layer":
        registry.blobs[cold.digest] = b"x" * cold.size
    elif case == "layer-swap":
        _rewrite_record(registry, tag, _swap_layer(registry))
    elif case == "wrong-artifact-type":
        _rewrite_record(registry, tag, lambda m: m.update(artifactType="application/x"))
    elif case == "extra-field":
        _rewrite_record(registry, tag, lambda m: m.update(annotations={}))
    elif case == "two-layers":
        _rewrite_record(registry, tag, lambda m: m["layers"].append(m["layers"][0]))
    elif case == "config-key-mismatch":
        layer = {"mediaType": remote_cache.APP_LAYER_MEDIA_TYPE, "digest": cold.digest, "size": cold.size}
        config = _add_config(registry, remote_cache._record_config("0" * 64, layer))
        _rewrite_record(registry, tag, lambda m: m.update(config=config))
    elif case == "oversized-manifest":
        registry.manifests[tag] = b" " * (remote_cache.MAX_RECORD_MANIFEST_BYTES + 1)
    elif case == "uppercase-digest":
        _rewrite_record(
            registry, tag, lambda m: m["layers"][0].update(digest=m["layers"][0]["digest"].upper())
        )
    elif case == "nested-manifest":
        registry.manifests[tag] = b"[" * 20000 + b"]" * 20000
    elif case == "nested-config":
        nested = b"[" * 7000 + b"]" * 7000
        registry.blobs[_digest(nested)] = nested
        config = {"mediaType": remote_cache.CACHE_CONFIG_MEDIA_TYPE, "digest": _digest(nested), "size": len(nested)}
        _rewrite_record(registry, tag, lambda m: m.update(config=config))

    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    calls = []
    original = ImageBuilder._write_app_layer
    monkeypatch.setattr(
        ImageBuilder,
        "_write_app_layer",
        lambda self, *args: calls.append(1) or original(self, *args),
    )
    with caplog.at_level(logging.WARNING, logger="pycontainer.remote_cache"):
        layer = _app_layer(reader, tmp_path, "reader")

    assert calls == [1]
    assert layer.digest == cold.digest
    assert _digest(Path(layer.tar_path).read_bytes()) == cold.digest
    assert "Remote cache miss" in caplog.text


def test_record_for_another_key_is_rejected(tmp_path, registry, caplog):
    writer, _ = _export(tmp_path, registry)
    key = _key_for(writer)
    other_key = "f" * 64
    registry.manifests[cache_tag(other_key)] = registry.manifests[cache_tag(key)]

    with caplog.at_level(logging.WARNING, logger="pycontainer.remote_cache"):
        result = RegistryLayerCache(writer.remote_cache_to.ref).fetch(other_key, tmp_path / "l")

    assert result is None
    assert "rejected record" in caplog.text


def test_missing_record_is_a_quiet_miss(tmp_path, registry, caplog):
    cache = RegistryLayerCache(parse_cache_spec(CACHE_SPEC))
    with caplog.at_level(logging.WARNING, logger="pycontainer.remote_cache"):
        assert cache.fetch("0" * 64, tmp_path) is None
    assert caplog.text == ""


@pytest.mark.parametrize("failure", ["auth", "network"])
def test_import_errors_warn_and_build_correctly(tmp_path, registry, monkeypatch, caplog, failure):
    _, cold = _export(tmp_path, registry)
    if failure == "auth":
        monkeypatch.setattr(
            remote_cache, "get_registry_auth", lambda host, **_: RegistryAuth(username="x", password="y")
        )
    else:
        def offline(request, timeout=None):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr("pycontainer.registry_client._open_url", offline)
        monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _: None)

    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    with caplog.at_level(logging.WARNING, logger="pycontainer.remote_cache"):
        layer = _app_layer(reader, tmp_path, "reader")

    assert layer.digest == cold.digest
    assert "could not import" in caplog.text
    assert "cache-pass" not in caplog.text


def test_cache_credentials_stay_on_cache_host_and_off_storage(tmp_path, registry, monkeypatch):
    _export(tmp_path, registry)
    registry.redirect_blobs = True
    registry.requests.clear()
    reader = _builder(tmp_path, "reader", cache_from=CACHE_SPEC)
    _forbid_tar_build(monkeypatch)

    _app_layer(reader, tmp_path, "reader")

    assert set(registry.auth_requests) == {CACHE_HOST}
    storage = [r for r in registry.requests if r[0] == STORAGE_HOST]
    assert storage and all(auth is None for *_, auth in storage)
    cache_requests = [r for r in registry.requests if r[0] == CACHE_HOST]
    assert all(auth == CACHE_AUTH for *_, auth in cache_requests)


@pytest.mark.parametrize("option", ["cache_from", "cache_to"])
@pytest.mark.parametrize(
    ("host", "authorization", "azure_calls", "login_server"),
    [
        ("ghcr.io", "Bearer synthetic-github-token", 0, None),
        ("ghcr.io:0443", "Bearer synthetic-github-token", 0, None),
        ("ghcr.io.attacker.test", None, 0, None),
        ("trusted.azurecr.io", "Bearer synthetic-azure-token", 1, "trusted.azurecr.io"),
        ("trusted.azurecr.io:0443", "Bearer synthetic-azure-token", 1, "trusted.azurecr.io"),
        ("trusted.azurecr.io", None, 2, "other.azurecr.io"),
        ("trusted.azurecr.io.attacker.test", None, 0, None),
    ],
)
def test_cache_registry_credentials_are_scoped_to_the_actual_host(
    tmp_path, monkeypatch, option, host, authorization, azure_calls, login_server
):
    for name in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD", "REGISTRY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
    monkeypatch.setattr(
        auth_module,
        "get_default_auth_provider",
        lambda **options: ChainAuthProvider([
            EnvironmentAuthProvider(**options), AzureCLIAuthProvider()
        ]),
    )
    azure_requests = []

    def fake_azure(command, **kwargs):
        azure_requests.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({
                "accessToken": "synthetic-azure-token",
                "loginServer": login_server,
            })
        )

    monkeypatch.setattr(auth_module.subprocess, "run", fake_azure)
    requests = []

    def fake_open(request, timeout=None):
        requests.append((request.full_url, request.get_header("Authorization")))
        if request.get_method() in {"GET", "HEAD"}:
            return _Response(404)
        if request.get_method() == "POST":
            return _Response(
                202, headers={"Location": f"/v2/{CACHE_REPO}/blobs/uploads/1"}
            )
        return _Response(201)

    monkeypatch.setattr("pycontainer.registry_client._open_url", fake_open)
    builder = _builder(
        tmp_path, "credentials", **{option: f"type=registry,ref={host}/{CACHE_REPO}"}
    )
    _app_layer(builder, tmp_path, "credentials")

    assert requests
    assert all(urllib.parse.urlsplit(url).netloc == host for url, _ in requests)
    assert all(header == authorization for _, header in requests)
    assert len(azure_requests) == azure_calls


@pytest.mark.parametrize("option", ["cache_from", "cache_to"])
@pytest.mark.parametrize("generic_auth", ["bearer", "basic", "both"])
@pytest.mark.parametrize("scoped_cache_auth", [False, True])
@pytest.mark.parametrize("image_registry", ["push.example", CACHE_HOST])
def test_cache_ignores_generic_environment_credentials(
    tmp_path, registry, monkeypatch, option, generic_auth, scoped_cache_auth, image_registry
):
    _export(tmp_path, registry)
    registry.requests.clear()
    if option == "cache_to":
        registry.manifests.clear()
    home = tmp_path / "home"
    docker_dir = home / ".docker"
    docker_dir.mkdir(parents=True)
    auths = {
        host: {"username": "cache-user", "password": "cache-pass"}
        for host in ("push.example", f"{CACHE_HOST}:8443", f"{CACHE_HOST}.attacker.test")
    }
    if scoped_cache_auth:
        auths[CACHE_HOST] = {"username": "cache-user", "password": "cache-pass"}
    (docker_dir / "config.json").write_text(json.dumps({"auths": auths}))
    monkeypatch.setattr(auth_module.Path, "home", lambda: home)
    monkeypatch.setattr(remote_cache, "get_registry_auth", auth_module.get_registry_auth)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD", "REGISTRY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if generic_auth in {"bearer", "both"}:
        monkeypatch.setenv("REGISTRY_TOKEN", "synthetic-push-token")
    if generic_auth in {"basic", "both"}:
        monkeypatch.setenv("REGISTRY_USERNAME", "push-user")
        monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-push-password")

    push_auth = auth_module.get_registry_auth(image_registry)
    if generic_auth == "basic":
        assert (push_auth.username, push_auth.password) == (
            "push-user", "synthetic-push-password"
        )
    else:
        assert push_auth.bearer_token == "synthetic-push-token"

    builder = _builder(
        tmp_path, "credentials", tag=f"{image_registry}/team/app:latest", **{option: CACHE_SPEC}
    )
    if option == "cache_to" and not scoped_cache_auth:
        with pytest.raises(RemoteCacheError, match="Failed to export"):
            _app_layer(builder, tmp_path, "credentials")
    else:
        if option == "cache_from" and scoped_cache_auth:
            _forbid_tar_build(monkeypatch)
        _app_layer(builder, tmp_path, "credentials")

    assert registry.requests
    expected = CACHE_AUTH if scoped_cache_auth else None
    assert all(host == CACHE_HOST for host, *_ in registry.requests)
    assert all(header == expected for *_, header in registry.requests)
    if option == "cache_to" and scoped_cache_auth:
        assert registry.manifest_puts()


def test_blob_redirect_to_plain_http_is_refused():
    client = RegistryClient(CACHE_HOST, CACHE_REPO)
    with pytest.raises(RegistryRequestError, match="HTTPS"):
        client._open_redirected_blob(
            f"https://{CACHE_HOST}/v2/x/blobs/y", "http://storage.example/blob"
        )


@pytest.mark.parametrize(
    "location",
    ["https://user:secret@storage.example/blob", "https://@storage.example/blob"],
)
def test_blob_redirect_rejects_embedded_credentials(monkeypatch, location):
    def forbidden(*args, **kwargs):
        pytest.fail("A credential-bearing redirect must not be opened")

    monkeypatch.setattr("pycontainer.registry_client.urllib.request.build_opener", forbidden)
    with pytest.raises(RegistryRequestError):
        RegistryClient(CACHE_HOST, CACHE_REPO)._open_redirected_blob(
            f"https://{CACHE_HOST}/blob", location
        )


@pytest.mark.parametrize(
    ("location", "allowed"),
    [
        ("https://storage.example/final", True),
        ("http://storage.example/final", False),
        ("https://user:secret@storage.example/final", False),
    ],
)
def test_blob_redirect_checks_every_hop(monkeypatch, location, allowed):
    requests = []

    class StorageHandler(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, request):
            return self._respond(request)

        def http_open(self, request):
            return self._respond(request)

        def _respond(self, request):
            requests.append(request)
            headers = Message()
            if request.full_url.endswith("/first"):
                headers["Location"] = location
                code, content = 302, b""
            else:
                code, content = 200, b"blob"
            response = urllib.response.addinfourl(
                io.BytesIO(content), headers, request.full_url, code
            )
            response.msg = "redirect" if code == 302 else "OK"
            return response

    build_opener = urllib.request.build_opener
    default_opener = build_opener(urllib.request.ProxyHandler({}), StorageHandler())
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen",
        lambda request, timeout: default_opener.open(request, timeout=timeout),
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *handlers: build_opener(
            urllib.request.ProxyHandler({}), *handlers, StorageHandler()
        ),
    )
    client = RegistryClient(CACHE_HOST, CACHE_REPO, auth_token="registry-secret")
    if allowed:
        with client._open_redirected_blob(
            f"https://{CACHE_HOST}/blob", "https://storage.example/first"
        ) as response:
            assert response.read() == b"blob"
        assert len(requests) == 2
        assert all(request.get_header("Authorization") is None for request in requests)
    else:
        with pytest.raises(RegistryRequestError):
            client._open_redirected_blob(
                f"https://{CACHE_HOST}/blob", "https://storage.example/first"
            )
        assert len(requests) == 1


@pytest.mark.parametrize(
    ("target", "keeps_auth"),
    [
        (f"https://{CACHE_HOST}/v2/other", True),
        (f"https://{STORAGE_HOST}/blob", False),
        (f"http://{CACHE_HOST}/v2/other", False),
        (f"https://{CACHE_HOST}:8443/v2/other", False),
    ],
)
def test_control_redirects_strip_authorization_across_origins(target, keeps_auth):
    request = urllib.request.Request(
        f"https://{CACHE_HOST}/v2/x/manifests/y", headers={"Authorization": CACHE_AUTH}
    )
    redirected = SameOriginAuthRedirect().redirect_request(
        request, None, 307, "redirect", Message(), target
    )
    assert redirected.has_header("Authorization") is keeps_auth


def test_record_serialization_is_deterministic():
    first = build_record("a" * 64, "sha256:" + "b" * 64, 10)
    assert first == build_record("a" * 64, "sha256:" + "b" * 64, 10)
    manifest = json.loads(first[1])
    assert manifest["artifactType"] == remote_cache.CACHE_ARTIFACT_TYPE
    assert manifest["config"]["digest"] == _digest(first[0])


def test_oversized_layer_export_is_rejected(tmp_path):
    cache = RegistryLayerCache(parse_cache_spec(CACHE_SPEC), client=object())
    with pytest.raises(RemoteCacheError, match="limited"):
        cache.store("a" * 64, "sha256:" + "b" * 64, tmp_path, remote_cache.MAX_CACHED_LAYER_BYTES + 1)


def test_dry_run_plan_shows_remote_cache(tmp_path, capsys):
    builder = _builder(tmp_path, "plan", cache_from=CACHE_SPEC, cache_to=CACHE_SPEC, dry_run=True)
    assert builder.remote_cache_from is None and builder.remote_cache_to is None
    builder.build()
    output = capsys.readouterr().out
    assert f"Cache From: {CACHE_SPEC}" in output and f"Cache To: {CACHE_SPEC}" in output
