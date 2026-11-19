import json

from pycontainer import builder as builder_module
from pycontainer.auth import RegistryAuth
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


def test_base_image_pull_passes_resolved_basic_credentials(tmp_path, monkeypatch):
    clients = []

    class FakeRegistryClient:
        def __init__(self, registry, repository, **auth):
            clients.append((registry, repository, auth))

        def pull_manifest(self, reference):
            return ({"config": {"digest": "sha256:config"}, "layers": []}, None)

        def pull_blob(self, digest, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"architecture": "amd64"}))

    monkeypatch.setattr(
        builder_module,
        "get_registry_auth",
        lambda registry: RegistryAuth(
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
    assert config == {"architecture": "amd64"}


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
