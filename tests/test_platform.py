"""Tests for cross-platform build support."""
import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from pycontainer import builder as builder_module
from pycontainer.builder import (
    ImageBuilder,
    UnsupportedPlatformError,
    parse_platform,
    parse_platform_variant,
)
from pycontainer.auth import RegistryAuth
from pycontainer.config import BuildConfig
from pycontainer.oci import build_config_json, build_index_json, OCILayer

def test_parse_platform_valid():
    """Test parsing valid platform strings."""
    os_name, arch = parse_platform("linux/amd64")
    assert os_name == "linux"
    assert arch == "amd64"
    
    os_name, arch = parse_platform("linux/arm64")
    assert os_name == "linux"
    assert arch == "arm64"
    
    os_name, arch = parse_platform("linux/arm")
    assert os_name == "linux"
    assert arch == "arm"
    
    os_name, arch = parse_platform("darwin/arm64")
    assert os_name == "darwin"
    assert arch == "arm64"

    os_name, arch = parse_platform("linux/arm/v7")
    assert (os_name, arch) == ("linux", "arm")
    assert parse_platform_variant("linux/arm/v7") == "v7"
    assert parse_platform_variant("linux/arm64") is None

def test_parse_platform_invalid():
    """Test parsing invalid platform strings."""
    with pytest.raises(ValueError, match="Invalid platform format"):
        parse_platform("amd64")
    
    with pytest.raises(ValueError, match="Invalid platform format"):
        parse_platform("linux/amd64/v2/extra")
    
    with pytest.raises(ValueError, match="Invalid platform format"):
        parse_platform("")

def test_build_config_with_platform():
    """Test that build_config_json uses correct platform values."""
    result = build_config_json(
        "arm64", "linux", {"DEBUG": "true"}, "/app",
        ["python", "-m", "app"], [8080]
    )
    
    assert result["architecture"] == "arm64"
    assert result["os"] == "linux"
    assert result["config"]["Env"] == ["DEBUG=true"]
    assert result["config"]["WorkingDir"] == "/app"

def test_build_index_with_platform():
    """Test that build_index_json includes correct platform metadata."""
    index = build_index_json(
        "sha256:abc123", 1024, "myapp:v1", 
        architecture="arm64", os_name="linux"
    )
    
    assert index["manifests"][0]["platform"]["architecture"] == "arm64"
    assert index["manifests"][0]["platform"]["os"] == "linux"

    variant_index = build_index_json(
        "sha256:abc123",
        1024,
        "myapp:armv7",
        architecture="arm",
        os_name="linux",
        variant="v7",
    )
    assert variant_index["manifests"][0]["platform"]["variant"] == "v7"

def test_cross_platform_build_amd64():
    """Test building for linux/amd64 platform."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context"
        ctx.mkdir()
        (ctx / "app.py").write_text("print('hello')")
        (ctx / "pyproject.toml").write_text('[project]\nname="test"\nversion="0.1"')
        
        output = Path(tmpdir) / "output"
        
        with patch('pycontainer.builder.ImageBuilder._pull_base_image') as mock_pull:
            mock_pull.return_value = (
                [OCILayer("application/vnd.oci.image.layer.v1.tar+gzip", "sha256:base1", 1000, "/tmp/base1")],
                {"architecture": "amd64", "os": "linux", "config": {"Env": [], "WorkingDir": "/"}}
            )
            
            cfg = BuildConfig(
                tag="test:v1",
                base_image="python:3.11-slim",
                context_dir=str(ctx),
                output_dir=str(output),
                platform="linux/amd64",
                use_cache=False
            )
            builder = ImageBuilder(cfg)
            builder.build()
            
            # Verify _pull_base_image was called with correct platform
            mock_pull.assert_called_once()
            call_args = mock_pull.call_args
            assert call_args[0][1] == "linux"  # os_name
            assert call_args[0][2] == "amd64"  # arch
            
            # Verify index.json has correct platform
            index = json.loads((output / "index.json").read_text())
            assert index["manifests"][0]["platform"]["architecture"] == "amd64"
            assert index["manifests"][0]["platform"]["os"] == "linux"
            
            # Verify config blob has correct platform
            manifest_digest = index["manifests"][0]["digest"]
            manifest_blob = output / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
            manifest = json.loads(manifest_blob.read_text())
            
            config_digest = manifest["config"]["digest"]
            config_blob = output / "blobs" / "sha256" / config_digest.split(":", 1)[1]
            config = json.loads(config_blob.read_text())
            
            assert config["architecture"] == "amd64"
            assert config["os"] == "linux"

def test_cross_platform_build_arm64():
    """Test building for linux/arm64 platform."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context"
        ctx.mkdir()
        (ctx / "app.py").write_text("print('hello')")
        (ctx / "pyproject.toml").write_text('[project]\nname="test"\nversion="0.1"')
        
        output = Path(tmpdir) / "output"
        
        with patch('pycontainer.builder.ImageBuilder._pull_base_image') as mock_pull:
            mock_pull.return_value = (
                [OCILayer("application/vnd.oci.image.layer.v1.tar+gzip", "sha256:base1", 1000, "/tmp/base1")],
                {"architecture": "arm64", "os": "linux", "variant": "v8",
                 "config": {"Env": [], "WorkingDir": "/"}}
            )
            
            cfg = BuildConfig(
                tag="test:arm64",
                base_image="python:3.11-slim",
                context_dir=str(ctx),
                output_dir=str(output),
                platform="linux/arm64/v8",
                use_cache=False
            )
            builder = ImageBuilder(cfg)
            builder.build()
            
            # Verify _pull_base_image was called with correct platform
            mock_pull.assert_called_once()
            call_args = mock_pull.call_args
            assert call_args[0][1] == "linux"  # os_name
            assert call_args[0][2] == "arm64"  # arch
            
            # Verify generated metadata
            index = json.loads((output / "index.json").read_text())
            assert index["manifests"][0]["platform"]["architecture"] == "arm64"
            assert index["manifests"][0]["platform"]["os"] == "linux"
            assert index["manifests"][0]["platform"]["variant"] == "v8"

def test_platform_manifest_selection():
    """Test that correct platform manifest is selected from multi-platform base image."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context"
        ctx.mkdir()
        (ctx / "app.py").write_text("print('hello')")
        (ctx / "pyproject.toml").write_text('[project]\nname="test"\nversion="0.1"')
        
        output = Path(tmpdir) / "output"
        
        # Mock a multi-platform image index
        with patch('pycontainer.registry_client.RegistryClient.pull_manifest') as mock_manifest:
            multi_platform_index = {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "a" * 64,
                        "size": 1000,
                        "platform": {"architecture": "amd64", "os": "linux"}
                    },
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "b" * 64,
                        "size": 1000,
                        "platform": {"architecture": "arm64", "os": "linux"}
                    }
                ]
            }
            
            arm64_config = {
                "architecture": "arm64",
                "os": "linux",
                "config": {"Env": ["PATH=/usr/local/bin"], "WorkingDir": "/"}
            }
            
            arm64_manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": "sha256:" + "c" * 64,
                    "size": 1000,
                },
                "layers": [{"digest": "sha256:" + "1" * 64, "size": 5000, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]
            }
            
            # First call returns index, second call returns arm64 manifest
            mock_manifest.side_effect = [(multi_platform_index, None), (arm64_manifest, None)]
            
            def mock_pull_blob(digest, path, expected_size):
                """Mock blob pull to create config and layer files."""
                if digest == "sha256:" + "c" * 64:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(arm64_config))
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fake layer data")
            
            with patch('pycontainer.registry_client.RegistryClient.pull_blob', side_effect=mock_pull_blob):
                with patch(
                    'pycontainer.builder.get_registry_auth',
                    return_value=RegistryAuth(),
                ):
                    cfg = BuildConfig(
                        tag="test:v1",
                        base_image="python:3.11-slim",
                        context_dir=str(ctx),
                        output_dir=str(output),
                        platform="linux/arm64",
                        use_cache=False
                    )
                    builder = ImageBuilder(cfg)
                    builder.build()
                    
                    # Verify second manifest call was for arm64 digest
                    assert mock_manifest.call_count == 2
                    second_call = mock_manifest.call_args_list[1]
                    assert second_call[0][0] == "sha256:" + "b" * 64


def test_docker_manifest_list_selects_exact_variant(tmp_path, monkeypatch):
    config_bytes = json.dumps({"os": "linux", "architecture": "arm"}).encode()
    config_digest = "sha256:" + "c" * 64
    layer_digest = "sha256:" + "d" * 64
    selected_digest = "sha256:" + "b" * 64
    index = {
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "manifests": [
            {
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "digest": "sha256:" + "a" * 64,
                "size": 101,
                "platform": {"os": "linux", "architecture": "arm", "variant": "v6"},
            },
            {
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "digest": selected_digest,
                "size": 202,
                "platform": {"os": "linux", "architecture": "arm", "variant": "v7"},
            },
        ],
    }
    image_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": layer_digest,
                "size": len(b"layer"),
            }
        ],
    }
    manifest_calls = []
    blob_calls = []

    class FakeRegistryClient:
        def __init__(self, *args, **kwargs):
            pass

        def pull_manifest(self, reference, **expected):
            manifest_calls.append((reference, expected))
            if reference == "latest":
                return index, None
            return image_manifest, None

        def pull_blob(self, digest, path, expected_size):
            blob_calls.append(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                config_bytes if digest == config_digest else b"layer"
            )

    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda registry: RegistryAuth()
    )
    builder = ImageBuilder(
        BuildConfig(
            base_image="registry.example/team/base:latest",
            context_dir=str(tmp_path),
            platform="linux/arm/v7",
            use_cache=False,
        )
    )

    layers, base_config = builder._pull_base_image(
        tmp_path / "blobs", "linux", "arm"
    )

    assert manifest_calls[1] == (
        selected_digest,
        {"expected_digest": selected_digest, "expected_size": 202},
    )
    assert blob_calls == [config_digest, layer_digest]
    assert base_config["variant"] == "v7"
    assert layers[0].digest == layer_digest


def test_missing_index_platform_fails_before_blob_download(tmp_path, monkeypatch):
    index = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "a" * 64,
                "size": 100,
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ],
    }
    blob_calls = []

    class FakeRegistryClient:
        def __init__(self, *args, **kwargs):
            pass

        def pull_manifest(self, reference, **expected):
            return index, None

        def pull_blob(self, *args):
            blob_calls.append(args)

    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda registry: RegistryAuth()
    )
    builder = ImageBuilder(
        BuildConfig(
            base_image="registry.example/team/base:latest",
            context_dir=str(tmp_path),
            platform="linux/arm64",
            use_cache=False,
        )
    )

    with pytest.raises(UnsupportedPlatformError, match="no exact manifest"):
        builder._pull_base_image(tmp_path / "blobs", "linux", "arm64")
    assert blob_calls == []


def test_base_config_platform_mismatch_fails_before_layer_download(
    tmp_path, monkeypatch
):
    config_bytes = json.dumps({"os": "linux", "architecture": "arm64"}).encode()
    config_digest = "sha256:" + "c" * 64
    layer_digest = "sha256:" + "d" * 64
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": layer_digest,
                "size": 5,
            }
        ],
    }
    blob_calls = []

    class FakeRegistryClient:
        def __init__(self, *args, **kwargs):
            pass

        def pull_manifest(self, reference, **expected):
            return manifest, None

        def pull_blob(self, digest, path, expected_size):
            blob_calls.append(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(config_bytes)

    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda registry: RegistryAuth()
    )
    builder = ImageBuilder(
        BuildConfig(
            base_image="registry.example/team/base:latest",
            context_dir=str(tmp_path),
            platform="linux/amd64",
            use_cache=False,
        )
    )

    with pytest.raises(UnsupportedPlatformError, match="does not match requested"):
        builder._pull_base_image(tmp_path / "blobs", "linux", "amd64")
    assert blob_calls == [config_digest]


def test_digest_pinned_base_image_is_pulled_by_digest(tmp_path, monkeypatch):
    digest = "sha256:" + "a" * 64
    config_bytes = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
    config_digest = "sha256:" + "c" * 64
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [],
    }
    manifest_calls = []

    class FakeRegistryClient:
        def __init__(self, *args, **kwargs):
            pass

        def pull_manifest(self, reference, **expected):
            manifest_calls.append((reference, expected))
            return manifest, digest

        def pull_blob(self, blob_digest, path, expected_size):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(config_bytes)

    monkeypatch.setattr(builder_module, "RegistryClient", FakeRegistryClient)
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda registry: RegistryAuth()
    )
    builder = ImageBuilder(
        BuildConfig(
            base_image=f"registry.example/team/base@{digest}",
            context_dir=str(tmp_path),
            platform="linux/amd64",
            use_cache=False,
        )
    )

    builder._pull_base_image(tmp_path / "blobs", "linux", "amd64")
    assert manifest_calls == [
        (digest, {"expected_digest": digest})
    ]


def test_default_platform():
    """Test that default platform is linux/amd64."""
    cfg = BuildConfig()
    assert cfg.platform == "linux/amd64"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
