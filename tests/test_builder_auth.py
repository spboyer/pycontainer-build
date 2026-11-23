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
