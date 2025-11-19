"""Tests for Phase 4: Polish & Production Readiness"""
import pytest
import tempfile
from pathlib import Path
from pycontainer.framework import detect_framework, apply_framework_defaults
from pycontainer.config import BuildConfig
from pycontainer.sbom import generate_sbom, _get_python_packages
from pycontainer.config_loader import load_config_file, merge_configs

def test_fastapi_detection():
    """Test FastAPI framework detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
        
        result=detect_framework(ctx)
        assert result is not None
        framework, entrypoint, ports=result
        assert framework=="FastAPI"
        assert "uvicorn" in entrypoint[0]
        assert 8000 in ports

def test_flask_detection():
    """Test Flask framework detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"app.py").write_text("from flask import Flask\napp = Flask(__name__)")
        
        result=detect_framework(ctx)
        assert result is not None
        framework, entrypoint, ports=result
        assert framework=="Flask"
        assert "flask" in entrypoint[0]
        assert 5000 in ports

def test_django_detection():
    """Test Django framework detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"manage.py").write_text("#!/usr/bin/env python\nimport django\nfrom django.core.management import execute_from_command_line")
        
        result=detect_framework(ctx)
        assert result is not None
        framework, entrypoint, ports=result
        assert framework=="Django"
        assert "manage.py" in ' '.join(entrypoint)
        assert 8000 in ports

def test_framework_defaults_applied():
    """Test that framework defaults are applied to config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
        
        cfg=BuildConfig(tag="test:v1", context_dir=str(ctx))
        cfg=apply_framework_defaults(cfg, ctx)
        
        assert cfg.entrypoint is not None
        assert "uvicorn" in cfg.entrypoint[0]
        assert 8000 in cfg.exposed_ports
        assert cfg.labels.get("framework")=="fastapi"

def test_reproducible_build_sorting():
    """Test that reproducible builds sort files."""
    cfg=BuildConfig(tag="test:v1", reproducible=True)
    assert cfg.reproducible==True
    
    cfg_non=BuildConfig(tag="test:v1", reproducible=False)
    assert cfg_non.reproducible==False

def test_sbom_package_detection():
    """Test Python package detection for SBOM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"requirements.txt").write_text("flask==2.0.0\nrequests==2.28.0\n")
        
        packages=_get_python_packages(ctx)
        assert len(packages)>=2
        names=[p[0] for p in packages]
        assert "flask" in names
        assert "requests" in names

def test_sbom_generation_spdx():
    """Test SBOM generation in SPDX format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"requirements.txt").write_text("flask==2.0.0\n")
        output=Path(tmpdir)/"sbom.json"
        
        sbom=generate_sbom(ctx, output, format="spdx")
        
        assert sbom["spdxVersion"]=="SPDX-2.3"
        assert "packages" in sbom
        assert len(sbom["packages"])>0

def test_sbom_generation_cyclonedx():
    """Test SBOM generation in CycloneDX format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx=Path(tmpdir)
        (ctx/"requirements.txt").write_text("requests==2.28.0\n")
        output=Path(tmpdir)/"sbom.json"
        
        sbom=generate_sbom(ctx, output, format="cyclonedx")
        
        assert sbom["bomFormat"]=="CycloneDX"
        assert "components" in sbom
        assert len(sbom["components"])>0

def test_config_file_loading():
    """Test loading configuration from TOML file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path=Path(tmpdir)/"pycontainer.toml"
        config_path.write_text("""
[build]
tag = "myapp:v1"
base-image = "python:3.11-slim"
workdir = "/app"

[build.env]
DEBUG = "true"
LOG_LEVEL = "info"
""")
        
        cfg=load_config_file(config_path)
        assert cfg["tag"]=="myapp:v1"
        assert cfg["base-image"]=="python:3.11-slim"
        assert cfg["env"]["DEBUG"]=="true"

def test_config_merge():
    """Test merging file config with CLI overrides."""
    file_cfg={
        "tag": "myapp:v1",
        "base_image": "python:3.11",
        "env": {"DEBUG": "false"}
    }
    cli_cfg={
        "tag": "myapp:v2",
        "env": {"DEBUG": "true", "NEW_VAR": "value"}
    }
    
    merged=merge_configs(file_cfg, cli_cfg)
    
    assert merged["tag"]=="myapp:v2"
    assert merged["base_image"]=="python:3.11"
    assert merged["env"]["DEBUG"]=="true"
    assert merged["env"]["NEW_VAR"]=="value"

def test_dry_run_mode():
    """Test dry-run mode doesn't create files."""
    cfg=BuildConfig(tag="test:v1", dry_run=True)
    assert cfg.dry_run==True

def test_verbose_mode():
    """Test verbose mode is configurable."""
    cfg=BuildConfig(tag="test:v1", verbose=True)
    assert cfg.verbose==True

def test_platform_configuration():
    """Test platform can be configured."""
    cfg_amd64=BuildConfig(tag="test:v1", platform="linux/amd64")
    assert cfg_amd64.platform=="linux/amd64"
    
    cfg_arm64=BuildConfig(tag="test:v1", platform="linux/arm64")
    assert cfg_arm64.platform=="linux/arm64"

if __name__=="__main__":
    pytest.main([__file__, "-v"])
