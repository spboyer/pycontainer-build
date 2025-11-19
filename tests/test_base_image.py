"""Tests for Phase 2: Base Image Pull & Layer Merging"""
import pytest
import json
import tempfile
import shutil
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
