import base64
import email.message
import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest

from pycontainer import builder as builder_module
from pycontainer.auth import RegistryAuth
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

BASE_CONFIG = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
BASE_CONFIG_DIGEST = "sha256:" + hashlib.sha256(BASE_CONFIG).hexdigest()
DESTINATION_SECRETS = ("dest-user", "dest-password", "dest-token")


def _basic(username, password):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        return None


class _FakeRegistries:
    """Bearer-challenging registries that record every outgoing request."""

    def __init__(self):
        self.requests = []
        self.paths = []
        self.responses = {}

    def urlopen(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        authorization = request.get_header("Authorization")
        self.requests.append((url.hostname, request.get_method(), authorization))
        self.paths.append(url.path)
        if url.hostname.startswith("auth."):
            registry = (
                "registry-1.docker.io"
                if url.hostname == "auth.docker.io"
                else url.hostname.removeprefix("auth.")
            )
            return _Response(200, json.dumps({"token": f"issued-{registry}"}).encode())
        if authorization != f"Bearer issued-{url.hostname}":
            headers = email.message.Message()
            realm_host = (
                "auth.docker.io"
                if url.hostname == "registry-1.docker.io"
                else f"auth.{url.hostname}"
            )
            headers["Www-Authenticate"] = (
                f'Bearer realm="https://{realm_host}/token",'
                f'service="{url.hostname}",scope="repository:team/image:pull,push"'
            )
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", headers, io.BytesIO(b"")
            )
        method = request.get_method()
        if method == "GET" and url.path in self.responses:
            return _Response(200, self.responses[url.path])
        if "/manifests/" in url.path and method == "GET":
            manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": BASE_CONFIG_DIGEST,
                    "size": len(BASE_CONFIG),
                },
                "layers": [],
            }
            return _Response(200, json.dumps(manifest).encode())
        if "/blobs/" in url.path and method == "GET":
            return _Response(200, BASE_CONFIG)
        if "/blobs/" in url.path and method == "HEAD":
            return _Response(200)
        if "/manifests/" in url.path and method == "PUT":
            return _Response(201)
        raise AssertionError(f"Unexpected request {method} {request.full_url}")

    def open(self, request, timeout=None):
        return self.urlopen(request, timeout)

    def headers_for(self, *hosts):
        return [auth for host, _, auth in self.requests if host in hosts]


@pytest.fixture
def registries(tmp_path, monkeypatch):
    for name in (
        "REGISTRY_TOKEN",
        "REGISTRY_USERNAME",
        "REGISTRY_PASSWORD",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    fake = _FakeRegistries()
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake.urlopen
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *handlers: fake,
    )
    return fake, home


def _set_destination_credentials(monkeypatch, kind):
    if kind == "basic":
        monkeypatch.setenv("REGISTRY_USERNAME", "dest-user")
        monkeypatch.setenv("REGISTRY_PASSWORD", "dest-password")
    else:
        monkeypatch.setenv("REGISTRY_TOKEN", "dest-token")


def _assert_no_destination_secret(headers):
    for header in headers:
        if not header:
            continue
        decoded = header
        if header.startswith("Basic "):
            decoded += base64.b64decode(header.removeprefix("Basic ")).decode()
        for secret in DESTINATION_SECRETS:
            assert secret not in decoded


def _pull_base(tmp_path, base_image):
    builder = ImageBuilder(
        BuildConfig(base_image=base_image, context_dir=str(tmp_path))
    )
    return builder._pull_base_image(tmp_path / "blobs", "linux", "amd64")


@pytest.mark.parametrize(
    ("base_image", "host"),
    [
        ("base.example/team/image:latest", "base.example"),
        ("python:3.11-slim", "registry-1.docker.io"),
    ],
)
@pytest.mark.parametrize("kind", ["basic", "bearer"])
def test_base_pull_does_not_send_generic_destination_credentials(
    tmp_path, monkeypatch, registries, kind, base_image, host
):
    fake, _ = registries
    _set_destination_credentials(monkeypatch, kind)

    layers, config = _pull_base(tmp_path, base_image)

    assert layers == []
    assert config == {"architecture": "amd64", "os": "linux"}
    realm_host = "auth.docker.io" if host == "registry-1.docker.io" else f"auth.{host}"
    assert [request_host for request_host, _, _ in fake.requests] == [
        host,
        realm_host,
        host,
        host,
    ]
    assert fake.headers_for(realm_host) == [None]
    _assert_no_destination_secret(fake.headers_for(host, realm_host))


@pytest.mark.parametrize("kind", ["basic", "bearer"])
def test_base_pull_with_build_args_does_not_send_destination_credentials(
    tmp_path, monkeypatch, registries, kind
):
    fake, _ = registries
    _set_destination_credentials(monkeypatch, kind)
    builder = ImageBuilder(BuildConfig(
        base_image="${HOST}/team/image:latest",
        build_args={"HOST": "base.example"},
        context_dir=str(tmp_path),
    ))

    builder._pull_base_image(tmp_path / "blobs", "linux", "amd64")

    assert fake.headers_for("auth.base.example") == [None]
    _assert_no_destination_secret(fake.headers_for("base.example", "auth.base.example"))


def test_base_pull_uses_registry_scoped_docker_config(
    tmp_path, monkeypatch, registries
):
    fake, home = registries
    _set_destination_credentials(monkeypatch, "basic")
    monkeypatch.setenv("REGISTRY_TOKEN", "dest-token")
    docker_config = home / ".docker" / "config.json"
    docker_config.parent.mkdir()
    docker_config.write_text(
        json.dumps(
            {
                "auths": {
                    "base.example": {
                        "username": "base-user",
                        "password": "base-password",
                    }
                }
            }
        )
    )

    _pull_base(tmp_path, "base.example/team/image:latest")

    assert fake.headers_for("auth.base.example") == [
        _basic("base-user", "base-password")
    ]
    _assert_no_destination_secret(
        fake.headers_for("base.example", "auth.base.example")
    )


@pytest.mark.parametrize("kind", ["basic", "bearer"])
@pytest.mark.parametrize("scoped", [False, True])
def test_multi_platform_build_scopes_auth_for_each_base_pull(
    tmp_path, monkeypatch, registries, kind, scoped
):
    fake, home = registries
    _set_destination_credentials(monkeypatch, kind)
    if scoped:
        docker_config = home / ".docker" / "config.json"
        docker_config.parent.mkdir()
        docker_config.write_text(json.dumps({"auths": {"base.example": {
            "username": "base-user", "password": "base-password",
        }}}))

    descriptors = []
    config_paths = []
    for arch in ("amd64", "arm64"):
        config = json.dumps({
            "os": "linux",
            "architecture": arch,
            "config": {},
            "rootfs": {"type": "layers", "diff_ids": []},
        }).encode()
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        config_path = f"/v2/team/image/blobs/{config_digest}"
        fake.responses[config_path] = config
        config_paths.append(config_path)
        manifest = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [],
        }).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        fake.responses[f"/v2/team/image/manifests/{digest}"] = manifest
        descriptors.append({
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "size": len(manifest),
            "platform": {"os": "linux", "architecture": arch},
        })
    index = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": descriptors,
    }).encode()
    index_digest = "sha256:" + hashlib.sha256(index).hexdigest()
    fake.responses["/v2/team/image/manifests/latest"] = index
    fake.responses[f"/v2/team/image/manifests/{index_digest}"] = index
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    builder = ImageBuilder(BuildConfig(
        base_image="base.example/team/image:latest",
        tag="push.example/team/image:v1",
        context_dir=str(context),
        output_dir=str(tmp_path / "layout"),
        platform="linux/amd64,linux/arm64",
        include_deps=False,
        use_cache=False,
    ))

    builder.build()

    assert [image.platform["architecture"] for image in builder.platform_images] == [
        "amd64", "arm64",
    ]
    assert all(path in fake.paths for path in config_paths)
    assert f"/v2/team/image/manifests/{index_digest}" not in fake.paths
    assert fake.paths.count("/v2/team/image/manifests/latest") == 2  # one 401 challenge and retry
    expected = _basic("base-user", "base-password") if scoped else None
    assert fake.headers_for("auth.base.example") == [expected, expected]
    _assert_no_destination_secret(
        fake.headers_for("base.example", "auth.base.example")
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("basic", _basic("dest-user", "dest-password")),
        ("bearer", "Bearer dest-token"),
    ],
)
def test_push_still_uses_generic_destination_credentials(
    tmp_path, monkeypatch, registries, kind, expected
):
    fake, _ = registries
    _set_destination_credentials(monkeypatch, kind)
    output = tmp_path / "layout"
    blobs = output / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (blobs / "config").write_bytes(b"{}")
    (blobs / "manifest").write_bytes(b"{}")
    builder = ImageBuilder(
        BuildConfig(tag="push.example/team/image:v1", output_dir=str(output))
    )
    builder.manifest_digest = "sha256:manifest"
    builder.config_digest = "sha256:config"
    builder.layers = []

    builder.push(show_progress=False)

    assert fake.headers_for("auth.push.example") == [expected]
    assert fake.headers_for("push.example")[-1] == "Bearer issued-push.example"


def test_base_image_pull_passes_resolved_basic_credentials(tmp_path, monkeypatch):
    clients = []

    class FakeRegistryClient:
        def __init__(self, registry, repository, **auth):
            clients.append((registry, repository, auth))

        def pull_manifest(self, reference, **expected):
            return (
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": "sha256:" + "a" * 64,
                        "size": len(
                            json.dumps({"architecture": "amd64", "os": "linux"}).encode()
                        ),
                    },
                    "layers": [],
                },
                None,
            )

        def pull_blob(self, digest, path, expected_size):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"architecture": "amd64", "os": "linux"}))

    monkeypatch.setattr(
        builder_module,
        "get_registry_auth",
        lambda registry, **kwargs: RegistryAuth(
            username="docker-user", password="docker-password"
        ),
    )
    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)
    builder = ImageBuilder(
        BuildConfig(
            base_image="registry.example/team/base:latest",
            context_dir=str(tmp_path),
        )
    )

    layers, config = builder._pull_base_image(
        tmp_path / "blobs", "linux", "amd64"
    )

    assert clients == [
        (
            "registry.example",
            "team/base",
            {
                "auth_token": None,
                "username": "docker-user",
                "password": "docker-password",
                "insecure": False,
                "ca_file": None,
            },
        )
    ]
    assert layers == []
    assert config == {"architecture": "amd64", "os": "linux"}


def test_image_push_passes_resolved_basic_credentials(tmp_path, monkeypatch):
    clients = []

    class FakeRegistryClient:
        def __init__(self, registry, repository, **auth):
            clients.append((registry, repository, auth))

        def push_blob(self, digest, path, check_exists):
            return True

        def push_manifest(self, tag, body):
            return True

    output = tmp_path / "layout"
    blobs = output / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (blobs / "config").write_bytes(b"{}")
    (blobs / "manifest").write_bytes(b"{}")
    builder = ImageBuilder(
        BuildConfig(
            tag="registry.example/team/app:v1",
            output_dir=str(output),
        )
    )
    builder.manifest_digest = "sha256:manifest"
    builder.config_digest = "sha256:config"
    builder.layers = []
    monkeypatch.setattr(
        builder_module,
        "get_registry_auth",
        lambda registry, **kwargs: RegistryAuth(
            username="docker-user", password="docker-password"
        ),
    )
    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)

    builder.push(show_progress=False)

    assert clients == [
        (
            "registry.example",
            "team/app",
            {
                "auth_token": None,
                "username": "docker-user",
                "password": "docker-password",
                "insecure": False,
                "ca_file": None,
            },
        )
    ]
