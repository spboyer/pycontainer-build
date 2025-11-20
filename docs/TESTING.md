# Testing Strategy: pycontainer-build

## Overview

This document defines the comprehensive testing strategy for pycontainer-build, ensuring quality, reliability, and correctness at every phase of development.

---

## Testing Philosophy

### Core Principles

1. **Test-Driven Development (TDD)**: Write tests before or alongside implementation
2. **Fast Feedback**: Unit tests run in <10s, full suite in <5 minutes
3. **Realistic Integration**: Test against real registries and OCI tools
4. **Comprehensive Coverage**: Target >80% code coverage with meaningful tests
5. **CI-First**: All tests run in CI before merge

### Test Pyramid

```
                    /\
                   /  \
                  / E2E \              5-10 tests
                 /  (5%) \             Slow, real deployments
                /________\
               /          \
              / Integration \          20-30 tests
             /    (25%)      \         Real registries, OCI tools
            /________________\
           /                  \
          /    Unit Tests      \      100+ tests
         /       (70%)          \     Fast, isolated, mocked
        /________________________\
```

---

## Test Levels

### 1. Unit Tests (70% of test suite)

**Goal**: Test individual functions and classes in isolation with mocked dependencies.

#### Characteristics

- **Speed**: <10s for entire unit test suite
- **Dependencies**: None (all mocked)
- **Coverage Target**: >85%
- **Location**: `tests/unit/`

#### Test Structure

```python
# tests/unit/test_builder.py
import pytest
from unittest.mock import Mock, patch
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

class TestImageBuilder:
    def test_build_creates_layer_tar(self, tmp_path):
        """Test that build() creates a tar file for the layer."""
        config = BuildConfig(
            tag="test:latest",
            context_path=tmp_path,
            include_paths=["app.py"]
        )
        
        # Create test file
        (tmp_path / "app.py").write_text("print('hello')")
        
        builder = ImageBuilder(config)
        with patch("pycontainer.builder.hash_file") as mock_hash:
            mock_hash.return_value = "abc123"
            builder.build()
        
        assert (tmp_path / "dist/image/blobs/sha256/abc123").exists()
```

#### Key Test Files

##### `tests/unit/test_config.py`

- `test_build_config_defaults()` — Verify default values
- `test_build_config_validation()` — Invalid inputs raise errors
- `test_build_config_env_merging()` — Environment variable merging
- `test_build_config_from_dict()` — Create from dictionary
- `test_build_config_to_dict()` — Serialize to dictionary

##### `tests/unit/test_oci.py`

- `test_oci_manifest_structure()` — Correct JSON structure
- `test_oci_config_structure()` — Correct config JSON
- `test_oci_layer_descriptor()` — Layer descriptor format
- `test_oci_manifest_digest_calculation()` — SHA256 digest correct
- `test_oci_config_env_serialization()` — Env vars format correctly

##### `tests/unit/test_project.py`

- `test_detect_entry_point_from_pyproject()` — Read [project.scripts]
- `test_detect_entry_point_fallback()` — Fallback to python -m
- `test_default_include_paths_src()` — Detect src/ directory
- `test_default_include_paths_app()` — Detect app/ directory
- `test_parse_pyproject_toml()` — Parse project metadata
- `test_parse_pyproject_toml_missing()` — Handle missing file

##### `tests/unit/test_fs_utils.py`

- `test_iter_files_recursive()` — Recursively iterate files
- `test_iter_files_exclude_patterns()` — Exclude __pycache__, etc.
- `test_ensure_dir_creates_path()` — Directory creation
- `test_hash_file_sha256()` — Correct SHA256 hash
- `test_hash_file_nonexistent()` — Error handling

##### `tests/unit/test_builder.py`

- `test_build_creates_layer()` — Layer tar created
- `test_build_creates_manifest()` — Manifest JSON written
- `test_build_creates_config()` — Config JSON written
- `test_build_with_custom_workdir()` — Custom working directory
- `test_build_with_env_vars()` — Environment variables included
- `test_build_output_structure()` — Correct dist/image/ structure

#### Running Unit Tests

```bash
# Run all unit tests
pytest tests/unit/

# Run with coverage
pytest tests/unit/ --cov=pycontainer --cov-report=html

# Run specific test file
pytest tests/unit/test_builder.py

# Run specific test
pytest tests/unit/test_builder.py::TestImageBuilder::test_build_creates_layer
```

---

### 2. Integration Tests (25% of test suite)

**Goal**: Test interactions between components and with external systems (registries, OCI tools).

#### Characteristics

- **Speed**: 1-3 minutes for integration test suite
- **Dependencies**: Docker registry, skopeo, test projects
- **Coverage**: Critical paths and external integrations
- **Location**: `tests/integration/`

#### Test Environment Setup

```bash
# Start local Docker registry
docker run -d -p 5000:5000 --name test-registry registry:2

# Export registry URL
export TEST_REGISTRY=localhost:5000

# Run integration tests
pytest tests/integration/
```

#### Test Structure

```python
# tests/integration/test_registry_push.py
import pytest
import subprocess
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

@pytest.fixture
def local_registry():
    """Start and stop local Docker registry."""
    subprocess.run(["docker", "run", "-d", "-p", "5000:5000", 
                    "--name", "test-registry", "registry:2"])
    yield "localhost:5000"
    subprocess.run(["docker", "rm", "-f", "test-registry"])

def test_push_to_local_registry(tmp_path, local_registry):
    """Test pushing an image to a local registry."""
    # Create test app
    (tmp_path / "app.py").write_text("print('test')")
    
    # Build and push
    config = BuildConfig(
        tag=f"{local_registry}/test:latest",
        context_path=tmp_path
    )
    builder = ImageBuilder(config)
    builder.build()
    builder.push()  # Phase 1 feature
    
    # Verify with skopeo
    result = subprocess.run(
        ["skopeo", "inspect", f"docker://{local_registry}/test:latest"],
        capture_output=True
    )
    assert result.returncode == 0
```

#### Key Test Scenarios

##### Registry Operations (Phase 1)

- `test_push_to_local_registry()` — Push to localhost:5000
- `test_push_to_ghcr()` — Push to GitHub Container Registry
- `test_push_to_acr()` — Push to Azure Container Registry
- `test_push_to_dockerhub()` — Push to Docker Hub
- `test_push_with_authentication()` — Auth flow works
- `test_push_skips_existing_blobs()` — Layer reuse works

##### Layer Operations (Phase 1)

- `test_layer_caching()` — Cached layers not rebuilt
- `test_layer_digest_consistency()` — Same files = same digest
- `test_layer_tar_structure()` — Correct tar format

##### Base Image Support (Phase 2)

- `test_build_on_python_slim()` — Layer on python:3.11-slim
- `test_build_on_distroless()` — Layer on distroless image
- `test_base_image_env_inheritance()` — Inherit base env vars
- `test_base_image_layer_merging()` — Correct layer order

##### Dependency Packaging (Phase 2)

- `test_package_venv_dependencies()` — Include venv in image
- `test_package_requirements_txt()` — Install from requirements.txt
- `test_dependency_layer_caching()` — Dependencies cached separately

##### Framework Detection (Phase 4)

- `test_detect_fastapi_entrypoint()` — Auto-configure FastAPI
- `test_detect_flask_entrypoint()` — Auto-configure Flask
- `test_detect_django_entrypoint()` — Auto-configure Django

#### Integration Test Helpers

```python
# tests/integration/conftest.py
import pytest
import subprocess
import tempfile
from pathlib import Path

@pytest.fixture
def test_project(tmp_path):
    """Create a minimal test Python project."""
    project = tmp_path / "testapp"
    project.mkdir()
    
    (project / "app.py").write_text("print('Hello from container')")
    (project / "pyproject.toml").write_text("""
[project]
name = "testapp"
version = "0.1.0"

[project.scripts]
testapp = "app:main"
""")
    
    return project

@pytest.fixture
def verify_image():
    """Helper to verify OCI image with external tools."""
    def _verify(image_path: Path):
        # Check with skopeo
        result = subprocess.run(
            ["skopeo", "inspect", f"oci:{image_path}"],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"Invalid OCI image: {result.stderr}"
        return result.stdout
    
    return _verify
```

#### Running Integration Tests

```bash
# Run all integration tests
pytest tests/integration/

# Run specific integration test
pytest tests/integration/test_registry_push.py

# Run with verbose output
pytest tests/integration/ -v -s

# Skip integration tests (for fast local dev)
pytest -m "not integration"
```

---

### 3. End-to-End Tests (5% of test suite)

**Goal**: Test complete workflows from build to deployment in realistic environments.

#### Characteristics

- **Speed**: 5-15 minutes per test
- **Dependencies**: Cloud resources (AKS, ACA), CI/CD
- **Coverage**: Critical user journeys
- **Location**: `tests/e2e/`
- **Execution**: CI only (not run locally)

#### Test Scenarios

##### E2E Test 1: FastAPI App to Azure Container Apps

```python
# tests/e2e/test_fastapi_to_aca.py
import pytest
import subprocess
import requests
import time

@pytest.mark.e2e
@pytest.mark.skipif(not os.getenv("AZURE_SUBSCRIPTION_ID"), 
                    reason="Azure subscription required")
def test_fastapi_app_deployment():
    """
    End-to-end test: Build FastAPI app, push to ACR, deploy to ACA, verify.
    """
    # 1. Create FastAPI app
    project = create_fastapi_app()
    
    # 2. Build container
    builder = ImageBuilder(BuildConfig(
        tag="testacr.azurecr.io/fastapi-test:e2e",
        context_path=project,
        base_image="python:3.11-slim"
    ))
    builder.build()
    builder.push()
    
    # 3. Deploy to Azure Container Apps
    app_name = f"fastapi-test-{uuid.uuid4().hex[:8]}"
    subprocess.run([
        "az", "containerapp", "create",
        "--name", app_name,
        "--resource-group", "pycontainer-e2e-tests",
        "--image", "testacr.azurecr.io/fastapi-test:e2e",
        "--ingress", "external",
        "--target-port", "8000"
    ], check=True)
    
    # 4. Get app URL
    result = subprocess.run([
        "az", "containerapp", "show",
        "--name", app_name,
        "--resource-group", "pycontainer-e2e-tests",
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv"
    ], capture_output=True, text=True, check=True)
    app_url = f"https://{result.stdout.strip()}"
    
    # 5. Wait for deployment
    time.sleep(30)
    
    # 6. Verify app responds
    response = requests.get(f"{app_url}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
    
    # 7. Cleanup
    subprocess.run([
        "az", "containerapp", "delete",
        "--name", app_name,
        "--resource-group", "pycontainer-e2e-tests",
        "--yes"
    ])
```

##### E2E Test 2: Django App with PostgreSQL on AKS

```python
# tests/e2e/test_django_to_aks.py
@pytest.mark.e2e
def test_django_app_with_db():
    """
    Build Django app, deploy to AKS with PostgreSQL, run migrations, verify.
    """
    # 1. Create Django app with models
    project = create_django_app_with_db()
    
    # 2. Build and push
    builder = ImageBuilder(BuildConfig(
        tag="ghcr.io/test/django-app:e2e",
        context_path=project,
        include_deps=True  # Include pip dependencies
    ))
    builder.build()
    builder.push()
    
    # 3. Deploy to AKS with Kubernetes manifests
    kubectl_apply("tests/e2e/fixtures/django-deployment.yaml")
    
    # 4. Wait for pod ready
    wait_for_pod_ready("app=django-test")
    
    # 5. Run migrations
    kubectl_exec("django-test", "python manage.py migrate")
    
    # 6. Verify app
    response = requests.get("http://<ingress-ip>/admin/")
    assert response.status_code == 200
    
    # 7. Cleanup
    kubectl_delete("tests/e2e/fixtures/django-deployment.yaml")
```

##### E2E Test 3: CI/CD Pipeline (GitHub Actions)

```python
# tests/e2e/test_github_actions_workflow.py
@pytest.mark.e2e
def test_github_actions_workflow():
    """
    Trigger GitHub Actions workflow, verify image pushed to GHCR.
    """
    # 1. Create test repo with workflow
    repo = create_test_github_repo()
    
    # 2. Trigger workflow
    trigger_workflow(repo, "build-container.yml")
    
    # 3. Wait for completion
    run = wait_for_workflow_completion(repo)
    assert run.status == "success"
    
    # 4. Verify image in GHCR
    image = f"ghcr.io/{repo}/app:main"
    result = subprocess.run(["skopeo", "inspect", f"docker://{image}"])
    assert result.returncode == 0
    
    # 5. Cleanup
    delete_github_repo(repo)
```

##### E2E Test 4: azd Integration

```python
# tests/e2e/test_azd_integration.py
@pytest.mark.e2e
def test_azd_up_without_docker():
    """
    Test azd up workflow using pycontainer (no Docker installed).
    """
    # 1. Create azd project
    project = create_azd_python_project()
    
    # 2. Configure azd to use pycontainer
    update_azure_yaml(project, build_type="python-sdk-container")
    
    # 3. Run azd up (provisions + deploys)
    subprocess.run(["azd", "up", "--no-prompt"], cwd=project, check=True)
    
    # 4. Get endpoint
    endpoint = get_azd_endpoint(project)
    
    # 5. Verify app
    response = requests.get(endpoint)
    assert response.status_code == 200
    
    # 6. Cleanup
    subprocess.run(["azd", "down", "--force", "--purge"], cwd=project)
```

#### E2E Test Infrastructure

**Azure Resources** (deployed once, shared across tests):

- Resource Group: `pycontainer-e2e-tests`
- Azure Container Registry: `pycontainere2e.azurecr.io`
- AKS Cluster: `pycontainer-e2e-aks`
- Azure Container Apps Environment: `pycontainer-e2e-env`

**Cleanup Strategy**:

- Tag all resources with `test-run-id`
- Cleanup script runs after tests
- Failsafe: Nightly cleanup job deletes resources older than 24h

#### Running E2E Tests

```bash
# Run all E2E tests (requires Azure credentials)
pytest tests/e2e/ -m e2e

# Run specific E2E test
pytest tests/e2e/test_fastapi_to_aca.py

# Skip E2E tests (default for local dev)
pytest -m "not e2e"
```

---

## Test Data & Fixtures

### Test Projects

Located in `tests/fixtures/`:

#### Minimal Python App

```
tests/fixtures/minimal/
├── app.py
└── pyproject.toml
```

#### FastAPI App

```
tests/fixtures/fastapi_app/
├── main.py
├── requirements.txt
└── pyproject.toml
```

#### Django App

```
tests/fixtures/django_app/
├── manage.py
├── myapp/
│   ├── __init__.py
│   ├── settings.py
│   ├── models.py
│   └── views.py
├── requirements.txt
└── pyproject.toml
```

#### Flask App with Dependencies

```
tests/fixtures/flask_app/
├── app.py
├── templates/
│   └── index.html
├── requirements.txt
└── pyproject.toml
```

### Shared Fixtures (conftest.py)

```python
# tests/conftest.py
import pytest
from pathlib import Path

@pytest.fixture
def fixtures_dir():
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"

@pytest.fixture
def minimal_project(fixtures_dir, tmp_path):
    """Copy minimal project to temp directory."""
    import shutil
    project = tmp_path / "minimal"
    shutil.copytree(fixtures_dir / "minimal", project)
    return project

@pytest.fixture
def mock_registry():
    """Mock registry client for unit tests."""
    from unittest.mock import Mock
    mock = Mock()
    mock.push_blob.return_value = True
    mock.push_manifest.return_value = True
    return mock
```

---

## CI/CD Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, dev]
  pull_request:
    branches: [main]

jobs:
  unit-tests:
    name: Unit Tests
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v4
        with:
          python-version: ${{ matrix.python-version }}
      
      - name: Install dependencies
        run: |
          pip install -e .[dev]
      
      - name: Run unit tests
        run: |
          pytest tests/unit/ --cov=pycontainer --cov-report=xml
      
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml

  integration-tests:
    name: Integration Tests
    runs-on: ubuntu-latest
    services:
      registry:
        image: registry:2
        ports:
          - 5000:5000
    
    steps:
      - uses: actions/checkout@v3
      
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      
      - name: Install tools
        run: |
          sudo apt-get update
          sudo apt-get install -y skopeo
      
      - name: Install dependencies
        run: pip install -e .[dev]
      
      - name: Run integration tests
        run: pytest tests/integration/
        env:
          TEST_REGISTRY: localhost:5000

  e2e-tests:
    name: E2E Tests
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    
    steps:
      - uses: actions/checkout@v3
      
      - uses: azure/login@v1
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      
      - name: Install dependencies
        run: pip install -e .[dev]
      
      - name: Run E2E tests
        run: pytest tests/e2e/ -m e2e
        env:
          AZURE_SUBSCRIPTION_ID: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      
      - name: Cleanup resources
        if: always()
        run: python tests/e2e/cleanup.py

  lint:
    name: Lint & Type Check
    runs-on: ubuntu-latest
    
    steps:
      - uses: actions/checkout@v3
      
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      
      - name: Install dependencies
        run: pip install -e .[dev]
      
      - name: Run black
        run: black --check src/ tests/
      
      - name: Run mypy
        run: mypy src/
      
      - name: Run pylint
        run: pylint src/ --fail-under=8.0
```

---

## Code Quality Metrics

### Coverage Requirements

| Component | Target Coverage | Current Coverage |
|-----------|-----------------|------------------|
| `builder.py` | >85% | TBD |
| `config.py` | >90% | TBD |
| `oci.py` | >90% | TBD |
| `project.py` | >80% | TBD |
| `fs_utils.py` | >85% | TBD |
| `registry_client.py` | >80% | TBD |
| **Overall** | **>80%** | TBD |

### Static Analysis

- **Linter**: `pylint` (score >8.0)
- **Formatter**: `black` (enforced in CI)
- **Type Checker**: `mypy` (strict mode)
- **Import Sorter**: `isort`

### Performance Benchmarks

| Operation | Target | Measured |
|-----------|--------|----------|
| Unit test suite | <10s | TBD |
| Integration test suite | <3min | TBD |
| Build (minimal app, no cache) | <5s | TBD |
| Build (cached) | <1s | TBD |
| Push to registry | <10s | TBD |

---

## Testing Best Practices

### 1. Test Naming

Use descriptive, behavior-focused names:

```python
# Good
def test_build_includes_all_files_from_src_directory():
    ...

def test_registry_push_retries_on_transient_failure():
    ...

# Bad
def test_build():
    ...

def test_push():
    ...
```

### 2. Arrange-Act-Assert Pattern

```python
def test_layer_digest_matches_file_content():
    # Arrange
    test_file = tmp_path / "app.py"
    test_file.write_text("print('hello')")
    expected_digest = hashlib.sha256(test_file.read_bytes()).hexdigest()
    
    # Act
    config = BuildConfig(context_path=tmp_path, include_paths=["app.py"])
    builder = ImageBuilder(config)
    builder.build()
    
    # Assert
    manifest = json.loads((tmp_path / "dist/image/manifest.json").read_text())
    actual_digest = manifest["layers"][0]["digest"].split(":")[1]
    assert actual_digest == expected_digest
```

### 3. Use Fixtures for Setup

```python
@pytest.fixture
def fastapi_project(tmp_path):
    """Create a FastAPI project structure."""
    project = tmp_path / "fastapi_app"
    project.mkdir()
    
    (project / "main.py").write_text("""
from fastapi import FastAPI
app = FastAPI()

@app.get("/")
def root():
    return {"message": "Hello World"}
""")
    
    (project / "requirements.txt").write_text("fastapi\nuvicorn")
    
    return project

def test_fastapi_entrypoint_detection(fastapi_project):
    entrypoint = detect_entrypoint(fastapi_project)
    assert "uvicorn" in entrypoint
```

### 4. Parameterized Tests

```python
@pytest.mark.parametrize("base_image,expected_os", [
    ("python:3.11-slim", "linux"),
    ("python:3.11-alpine", "linux"),
    ("mcr.microsoft.com/python/distroless", "linux"),
])
def test_base_image_os_detection(base_image, expected_os):
    config = BuildConfig(base_image=base_image)
    assert config.platform_os == expected_os
```

### 5. Mock External Services

```python
@patch("pycontainer.registry_client.requests.post")
def test_push_blob_handles_network_error(mock_post):
    # Mock network failure
    mock_post.side_effect = requests.exceptions.ConnectionError()
    
    client = RegistryClient("localhost:5000")
    
    with pytest.raises(RegistryError):
        client.push_blob(b"data", "sha256:abc123")
```

---

## Debugging Failed Tests

### Common Failures

#### Unit Test Failure: Digest Mismatch

**Symptom**: `AssertionError: Digest sha256:abc123 != sha256:def456`

**Cause**: File content changed or tar format inconsistent

**Fix**: Check tar member order, mtimes, permissions

#### Integration Test Failure: Registry Push Timeout

**Symptom**: `requests.exceptions.Timeout: Connection timed out`

**Cause**: Registry not started or network issue

**Fix**: Verify `docker ps | grep registry`, check `TEST_REGISTRY` env var

#### E2E Test Failure: App Returns 500

**Symptom**: `AssertionError: 500 != 200`

**Cause**: Missing dependencies, incorrect entrypoint, or misconfiguration

**Fix**: Check container logs: `kubectl logs <pod>` or `az containerapp logs`

### Debug Mode

Run tests with verbose output and no capture:

```bash
# See all print statements and logs
pytest tests/integration/test_registry.py -v -s

# Drop into debugger on failure
pytest tests/unit/test_builder.py --pdb

# Run only failed tests from last run
pytest --lf
```

---

## Test Maintenance

### Adding New Tests

1. **Identify what to test**: New feature, bug fix, or coverage gap
2. **Choose test level**: Unit, integration, or E2E
3. **Write test**: Follow naming and structure conventions
4. **Run locally**: Ensure test passes
5. **Update CI**: Add new fixtures or test dependencies if needed
6. **Document**: Add test description to this file

### Updating Existing Tests

- When refactoring code, update corresponding tests
- If test becomes flaky, investigate root cause (don't just retry)
- Archive deprecated tests (don't delete—keep for reference)

### Test Ownership

| Test Category | Owner/Team | Review Required |
|---------------|------------|-----------------|
| Unit Tests | Developer (self-review OK) | No |
| Integration Tests | Developer + Reviewer | Yes |
| E2E Tests | Team Lead + DevOps | Yes |

---

## Test Environments

### Local Development

- **OS**: macOS, Linux, Windows WSL2
- **Python**: 3.10, 3.11, 3.12
- **Tools**: Docker (optional), skopeo

### CI (GitHub Actions)

- **OS**: ubuntu-latest
- **Python**: 3.10, 3.11, 3.12 (matrix)
- **Services**: Local Docker registry (registry:2)

### E2E (Azure)

- **Resources**: Dedicated test subscription
- **Cleanup**: Automated nightly + post-test
- **Access**: Restricted to CI service principal

---

## Resources

- **pytest docs**: <https://docs.pytest.org/>
- **unittest.mock guide**: <https://docs.python.org/3/library/unittest.mock.html>
- **OCI Image Spec**: <https://github.com/opencontainers/image-spec>
- **Skopeo docs**: <https://github.com/containers/skopeo>

---

**Last Updated**: 2025-11-19  
**Document Version**: 1.0  
**Status**: Living document
