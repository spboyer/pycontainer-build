"""Tests for Phase 2: Base Image Pull & Layer Merging"""
import pytest
import gzip
import hashlib
import io
import json
import tempfile
import shutil
import tarfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.oci import build_config_json, is_distroless

def test_base_image_config_merge():
    """Test merging app config with base image config."""
    base_cfg = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": ["PATH=/usr/local/bin:/usr/bin", "PYTHON_VERSION=3.11"],
            "WorkingDir": "/",
            "Entrypoint": ["/usr/bin/python"],
            "User": "nobody",
            "Labels": {"base": "python:3.11"}
        }
    }
    
    app_env = {"DEBUG": "true"}
    result = build_config_json(
        "amd64", "linux", app_env, "/app", 
        ["python", "-m", "myapp"], [], 
        labels={"app": "myapp"}, base_config=base_cfg
    )
    
    assert result["config"]["WorkingDir"] == "/app"
    assert result["config"]["Entrypoint"] == ["python", "-m", "myapp"]
    assert "PATH=/usr/local/bin:/usr/bin" in result["config"]["Env"]
    assert "DEBUG=true" in result["config"]["Env"]
    assert result["config"]["User"] == "nobody"
    assert result["config"]["Labels"] == {"base": "python:3.11", "app": "myapp"}

def test_distroless_detection():
    """Test detection of distroless images."""
    distroless_cfg = {
        "config": {
            "Labels": {
                "org.opencontainers.image.base.name": "gcr.io/distroless/python3"
            }
        }
    }
    assert is_distroless(distroless_cfg) == True
    
    regular_cfg = {
        "config": {
            "Labels": {
                "org.opencontainers.image.base.name": "python:3.11-slim"
            }
        }
    }
    assert is_distroless(regular_cfg) == False

def test_layer_ordering():
    """Test that base layers come before app layers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context"
        ctx.mkdir()
        (ctx / "app.py").write_text("print('hello')")
        (ctx / "pyproject.toml").write_text('[project]\nname="test"\nversion="0.1"')
        
        output = Path(tmpdir) / "output"
        
        with patch('pycontainer.builder.ImageBuilder._pull_base_image') as mock_pull:
            from pycontainer.oci import OCILayer
            mock_pull.return_value = (
                [
                    OCILayer("application/vnd.oci.image.layer.v1.tar+gzip", "sha256:base1", 1000, "/tmp/base1"),
                    OCILayer("application/vnd.oci.image.layer.v1.tar+gzip", "sha256:base2", 2000, "/tmp/base2")
                ],
                {"architecture": "amd64", "os": "linux", "config": {"Env": [], "WorkingDir": "/"}}
            )
            
            cfg = BuildConfig(
                tag="test:v1",
                base_image="python:3.11-slim",
                context_dir=str(ctx),
                output_dir=str(output),
                use_cache=False
            )
            builder = ImageBuilder(cfg)
            builder.build()
            
            assert len(builder.layers) == 3
            assert builder.layers[0].digest == "sha256:base1"
            assert builder.layers[1].digest == "sha256:base2"
            assert "sha256:base" not in builder.layers[2].digest

@pytest.mark.parametrize("include_deps", [False, True])
def test_build_appends_layer_diffids_without_mutating_base_config(tmp_path, monkeypatch, include_deps):
    from pycontainer.oci import OCILayer

    def layer_tar(name, content):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    output = tmp_path / "output"

    base_uncompressed_bytes = layer_tar("base.txt", b"base layer")
    compressed_base = gzip.compress(base_uncompressed_bytes)
    base_layer_digest = "sha256:" + hashlib.sha256(compressed_base).hexdigest()
    base_diff_id = "sha256:" + hashlib.sha256(base_uncompressed_bytes).hexdigest()
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": [], "WorkingDir": "/"},
        "rootfs": {"type": "layers", "diff_ids": [base_diff_id]},
    }
    original_base_config = json.loads(json.dumps(base_config))

    def pull_base_image(self, layers_dir, os_name, arch):
        path = layers_dir / base_layer_digest.split(":", 1)[1]
        path.write_bytes(compressed_base)
        return [
            OCILayer(
                "application/vnd.oci.image.layer.v1.tar+gzip",
                base_layer_digest,
                len(compressed_base),
                str(path),
            )
        ], base_config

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull_base_image)

    if include_deps:
        def create_deps_layer(self, layers_dir, **kwargs):
            layer_bytes = layer_tar("site-packages/dependency.py", b"dependency")
            digest = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
            path = layers_dir / digest.split(":", 1)[1]
            path.write_bytes(layer_bytes)
            return OCILayer(
                "application/vnd.oci.image.layer.v1.tar",
                digest,
                len(layer_bytes),
                str(path),
            )

        monkeypatch.setattr(ImageBuilder, "_create_deps_layer", create_deps_layer)

    cfg = BuildConfig(
        tag="test:v1",
        context_dir=str(context),
        output_dir=str(output),
        include_deps=include_deps,
        use_cache=False,
    )
    builder = ImageBuilder(cfg)
    builder.build()

    layers_dir = output / "blobs" / "sha256"
    manifest_path = layers_dir / builder.manifest_digest.split(":", 1)[1]
    manifest = json.loads(manifest_path.read_text())
    config_path = layers_dir / builder.config_digest.split(":", 1)[1]
    image_config = json.loads(config_path.read_text())
    diff_ids = image_config["rootfs"]["diff_ids"]

    assert base_layer_digest != base_diff_id
    assert diff_ids[0] == base_diff_id
    assert diff_ids[1:] == [layer.digest for layer in builder.layers[1:]]
    assert len(diff_ids) == len(manifest["layers"]) == len(builder.layers)
    assert base_config == original_base_config

    for layer, descriptor in zip(builder.layers, manifest["layers"]):
        blob = layers_dir / descriptor["digest"].split(":", 1)[1]
        blob_bytes = blob.read_bytes()
        assert descriptor["digest"] == layer.digest
        assert descriptor["size"] == len(blob_bytes) == layer.size
        assert descriptor["mediaType"] == layer.media_type
        assert hashlib.sha256(blob_bytes).hexdigest() == layer.digest.split(":", 1)[1]

@pytest.mark.registry_integration
def test_dependency_layer_creation():
    """Test separate dependency layer creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context"
        ctx.mkdir()
        (ctx / "app.py").write_text("import flask")
        (ctx / "requirements.txt").write_text("flask==2.0.0")
        (ctx / "pyproject.toml").write_text('[project]\nname="test"')
        
        output = Path(tmpdir) / "output"
        
        cfg = BuildConfig(
            tag="test:v1",
            context_dir=str(ctx),
            output_dir=str(output),
            include_deps=True,
            use_cache=False
        )
        builder = ImageBuilder(cfg)
        builder.build()
        
        assert len(builder.layers) >= 1

def test_env_override():
    """Test that app env vars override base env vars."""
    base_cfg = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": ["DEBUG=false", "LOG_LEVEL=info"]
        }
    }
    
    app_env = {"DEBUG": "true", "NEW_VAR": "value"}
    result = build_config_json(
        "amd64", "linux", app_env, "/app", 
        ["python"], [], base_config=base_cfg
    )
    
    env_dict = {kv.split('=')[0]: kv.split('=')[1] for kv in result["config"]["Env"] if '=' in kv}
    assert env_dict["DEBUG"] == "true"
    assert env_dict["LOG_LEVEL"] == "info"
    assert env_dict["NEW_VAR"] == "value"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
