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

    def urlopen(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        authorization = request.get_header("Authorization")
        self.requests.append((url.hostname, request.get_method(), authorization))
        if url.hostname.startswith("auth."):
            registry = url.hostname.removeprefix("auth.")
            return _Response(200, json.dumps({"token": f"issued-{registry}"}).encode())
        if authorization != f"Bearer issued-{url.hostname}":
            headers = email.message.Message()
            headers["Www-Authenticate"] = (
                f'Bearer realm="https://auth.{url.hostname}/token",'
                f'service="{url.hostname}",scope="repository:team/image:pull,push"'
            )
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", headers, io.BytesIO(b"")
            )
        method = request.get_method()
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
    assert [request_host for request_host, _, _ in fake.requests] == [
        host,
        f"auth.{host}",
        host,
        host,
    ]
    assert fake.headers_for(f"auth.{host}") == [None]
    _assert_no_destination_secret(fake.headers_for(host, f"auth.{host}"))


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
            },
        )
    ]
