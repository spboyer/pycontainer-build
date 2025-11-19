# Architecture: pycontainer-build

## Overview

This document describes the architecture of pycontainer-build, a native Python container image builder that creates OCI-compliant images without requiring Docker or Dockerfiles.

---

## System Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Interface Layer                     │
├─────────────────────────────────────────────────────────────────┤
│  CLI (cli.py)          │  Python API          │  Plugins        │
│  - argparse            │  - BuildConfig       │  - Poetry       │
│  - Command handling    │  - ImageBuilder      │  - Hatch        │
│  - Output formatting   │  - Programmatic API  │  - azd          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Orchestration Layer                         │
├─────────────────────────────────────────────────────────────────┤
│  ImageBuilder (builder.py)                                       │
│  - Coordinates build process                                     │
│  - Manages build phases (discover → pack → generate → output)   │
│  - Handles caching and optimization                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┼─────────────┐
                ▼             ▼             ▼
┌───────────────────┐ ┌──────────────┐ ┌─────────────────┐
│  Project Layer    │ │  OCI Layer   │ │  Registry Layer │
├───────────────────┤ ├──────────────┤ ├─────────────────┤
│  project.py       │ │  oci.py      │ │ registry_client │
│  - Pyproject.toml │ │  - Manifest  │ │  - Push/Pull    │
│  - Entry points   │ │  - Config    │ │  - Auth         │
│  - Dependencies   │ │  - Layers    │ │  - Blob upload  │
│  - Framework      │ │  - Index     │ │  - V2 API       │
│    detection      │ │  - Digests   │ │                 │
└───────────────────┘ └──────────────┘ └─────────────────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Foundation Layer                            │
├─────────────────────────────────────────────────────────────────┤
│  fs_utils.py        │  config.py        │  cache.py            │
│  - File iteration   │  - BuildConfig    │  - Blob cache        │
│  - Tar creation     │  - Validation     │  - Layer reuse       │
│  - Hashing          │  - TOML parsing   │  - Eviction policy   │
│  - Path handling    │  - Serialization  │                      │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Output Targets                              │
├─────────────────────────────────────────────────────────────────┤
│  Local Layout       │  Container Registry    │  OCI Artifacts   │
│  - dist/image/      │  - GHCR                │  - SBOM          │
│  - Blobs, manifest  │  - ACR                 │  - Signatures    │
│  - Index, refs      │  - Docker Hub          │                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. CLI Layer (`cli.py`)

**Purpose**: Command-line interface for building container images.

**Responsibilities**:
- Parse command-line arguments using `argparse`
- Validate user inputs
- Create `BuildConfig` from CLI flags
- Invoke `ImageBuilder` with config
- Display build progress and results
- Handle errors gracefully

**Key Functions**:
```python
def main():
    """Entry point for pycontainer CLI."""
    parser = create_argument_parser()
    args = parser.parse_args()
    
    config = BuildConfig.from_args(args)
    builder = ImageBuilder(config)
    
    try:
        builder.build()
        if args.push:
            builder.push()
    except BuildError as e:
        print(f"Error: {e}")
        sys.exit(1)
```

**CLI Arguments**:
- `--tag`: Image tag (e.g., `myapp:latest`)
- `--context`: Build context path
- `--workdir`: Container working directory
- `--env`: Environment variables (repeatable)
- `--base-image`: Base image (Phase 2)
- `--push`: Push to registry after build (Phase 1)
- `--registry`: Registry URL
- `--verbose`: Enable verbose logging

---

### 2. Orchestration Layer (`builder.py`)

**Purpose**: Coordinates the entire build process from project discovery to image output.

**Class Structure**:
```python
class ImageBuilder:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.output_dir = Path("dist/image")
        self.cache = BlobCache() if config.use_cache else None
    
    def build(self) -> Path:
        """Build OCI image and return path to output."""
        # Phase 1: Discover project structure
        metadata = discover_project(self.config.context_path)
        
        # Phase 2: Collect files to include
        files = collect_files(self.config.context_path, 
                              self.config.include_paths)
        
        # Phase 3: Create layer tar
        layer_path = create_layer_tar(files, self.config.workdir)
        layer_digest = hash_file(layer_path)
        
        # Phase 4: Generate OCI structures
        config_json = build_config_json(self.config, metadata)
        config_digest = write_blob(config_json, self.output_dir)
        
        layer_blob_path = move_to_blob_store(layer_path, layer_digest)
        
        manifest = build_manifest(config_digest, layer_digest)
        write_manifest(manifest, self.output_dir)
        
        return self.output_dir
    
    def push(self, registry: str = None):
        """Push built image to registry."""
        # Phase 1 feature
        pass
```

**Build Process Flow**:
1. **Discovery**: Read `pyproject.toml`, detect entry point, find source files
2. **Collection**: Gather all files to include in image
3. **Packing**: Create tar archive with proper paths and permissions
4. **OCI Generation**: Create config and manifest JSON
5. **Output**: Write blobs and manifest to disk (or registry)

---

### 3. Project Layer (`project.py`)

**Purpose**: Introspect Python projects to extract metadata, entry points, and structure.

**Key Functions**:

```python
def discover_project(context_path: Path) -> ProjectMetadata:
    """
    Discover Python project structure and metadata.
    
    Returns:
        ProjectMetadata with name, version, entry_point, include_paths
    """
    pyproject = parse_pyproject_toml(context_path / "pyproject.toml")
    entry_point = detect_entry_point(pyproject)
    include_paths = detect_include_paths(context_path)
    framework = detect_framework(context_path)
    
    return ProjectMetadata(
        name=pyproject.get("project", {}).get("name"),
        version=pyproject.get("project", {}).get("version"),
        entry_point=entry_point,
        include_paths=include_paths,
        framework=framework
    )

def detect_entry_point(pyproject: dict) -> list[str]:
    """
    Detect entry point from pyproject.toml [project.scripts].
    
    Converts script like "myapp = "myapp.cli:main"" to:
    ["python", "-m", "myapp.cli"]
    
    Falls back to ["python", "-m", "app"] if no script found.
    """
    scripts = pyproject.get("project", {}).get("scripts", {})
    if not scripts:
        return ["python", "-m", "app"]
    
    # Get first script entry
    script_name, script_target = next(iter(scripts.items()))
    module_path = script_target.split(":")[0]
    
    return ["python", "-m", module_path]

def detect_include_paths(context_path: Path) -> list[str]:
    """
    Auto-detect paths to include in image.
    
    Looks for:
    - src/ directory
    - app/ directory
    - Package directory (name matching pyproject.toml name)
    - Always includes: pyproject.toml, requirements.txt
    """
    paths = []
    
    if (context_path / "src").exists():
        paths.append("src/")
    elif (context_path / "app").exists():
        paths.append("app/")
    
    # Add essential files
    for file in ["pyproject.toml", "requirements.txt", "README.md"]:
        if (context_path / file).exists():
            paths.append(file)
    
    return paths
```

**Data Structures**:
```python
@dataclass
class ProjectMetadata:
    name: str
    version: str
    entry_point: list[str]
    include_paths: list[str]
    framework: str | None  # "fastapi", "flask", "django", None
```

---

### 4. OCI Layer (`oci.py`)

**Purpose**: Implement OCI Image Specification structures (manifest, config, layers).

**OCI Structures**:

```python
@dataclass
class OCIDescriptor:
    """OCI Content Descriptor."""
    mediaType: str
    digest: str
    size: int

@dataclass
class OCIManifest:
    """OCI Image Manifest v1."""
    schemaVersion: int = 2
    mediaType: str = "application/vnd.oci.image.manifest.v1+json"
    config: OCIDescriptor
    layers: list[OCIDescriptor]
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

@dataclass
class OCIConfig:
    """OCI Image Config."""
    architecture: str = "amd64"
    os: str = "linux"
    config: dict  # Env, Cmd, WorkingDir, etc.
    rootfs: dict  # {"type": "layers", "diff_ids": [...]}
    history: list[dict]
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

@dataclass
class OCIIndex:
    """OCI Image Index (manifest list for multi-arch)."""
    schemaVersion: int = 2
    mediaType: str = "application/vnd.oci.image.index.v1+json"
    manifests: list[OCIDescriptor]
```

**Key Functions**:

```python
def build_manifest(config_desc: OCIDescriptor, 
                   layer_descs: list[OCIDescriptor]) -> OCIManifest:
    """Create OCI manifest from config and layer descriptors."""
    return OCIManifest(
        config=config_desc,
        layers=layer_descs
    )

def build_config_json(build_config: BuildConfig, 
                      metadata: ProjectMetadata) -> OCIConfig:
    """
    Create OCI config JSON from build configuration.
    
    Includes:
    - Env vars (from build_config.env)
    - Cmd (from metadata.entry_point)
    - WorkingDir (from build_config.workdir)
    - Labels (from build_config.labels)
    """
    return OCIConfig(
        architecture="amd64",
        os="linux",
        config={
            "Env": [f"{k}={v}" for k, v in build_config.env.items()],
            "Cmd": metadata.entry_point,
            "WorkingDir": build_config.workdir,
            "Labels": build_config.labels
        },
        rootfs={
            "type": "layers",
            "diff_ids": [f"sha256:{digest}" for digest in layer_digests]
        },
        history=[
            {"created_by": "pycontainer-build"}
        ]
    )
```

---

### 5. Registry Layer (`registry_client.py`) — Phase 1

**Purpose**: Interact with container registries using Docker Registry v2 API.

**Class Structure**:

```python
class RegistryClient:
    def __init__(self, registry: str, auth: AuthProvider = None):
        self.registry = registry
        self.auth = auth
        self.session = requests.Session()
    
    def push_blob(self, data: bytes, digest: str) -> bool:
        """
        Push a blob (layer or config) to registry.
        
        Process:
        1. POST /v2/<name>/blobs/uploads/ (initiate upload)
        2. PUT /v2/<name>/blobs/uploads/<uuid>?digest=<digest> (complete)
        """
        # Initiate upload
        response = self.session.post(
            f"https://{self.registry}/v2/{name}/blobs/uploads/",
            headers={"Authorization": f"Bearer {self.auth.get_token()}"}
        )
        upload_url = response.headers["Location"]
        
        # Complete upload
        response = self.session.put(
            f"{upload_url}?digest={digest}",
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data))
            }
        )
        return response.status_code == 201
    
    def push_manifest(self, manifest: OCIManifest, tag: str) -> bool:
        """Push manifest to registry with specific tag."""
        response = self.session.put(
            f"https://{self.registry}/v2/{name}/manifests/{tag}",
            data=manifest.to_json(),
            headers={
                "Content-Type": "application/vnd.oci.image.manifest.v1+json",
                "Authorization": f"Bearer {self.auth.get_token()}"
            }
        )
        return response.status_code == 201
    
    def blob_exists(self, digest: str) -> bool:
        """Check if blob already exists in registry (for caching)."""
        response = self.session.head(
            f"https://{self.registry}/v2/{name}/blobs/{digest}"
        )
        return response.status_code == 200
```

**Authentication**:

```python
class AuthProvider:
    """Base class for registry authentication."""
    def get_token(self) -> str:
        raise NotImplementedError

class BearerTokenAuth(AuthProvider):
    """OAuth2 Bearer Token authentication."""
    def __init__(self, username: str, password: str, registry: str):
        self.token = self._exchange_token(username, password, registry)
    
    def get_token(self) -> str:
        return self.token
    
    def _exchange_token(self, username, password, registry):
        # Implement OAuth2 token exchange
        pass

class DockerConfigAuth(AuthProvider):
    """Read credentials from ~/.docker/config.json."""
    def __init__(self, registry: str):
        self.credentials = self._load_docker_config(registry)
    
    def get_token(self) -> str:
        return base64.b64decode(self.credentials).decode()
```

---

### 6. Foundation Layer

#### `fs_utils.py`

**Purpose**: File system utilities for iteration, tar creation, hashing.

**Key Functions**:

```python
def iter_files(base_path: Path, 
               include_patterns: list[str]) -> Iterator[tuple[Path, Path]]:
    """
    Iterate over files matching include patterns.
    
    Yields:
        (absolute_path, relative_path) tuples
    """
    for pattern in include_patterns:
        for path in base_path.glob(pattern):
            if path.is_file():
                yield path, path.relative_to(base_path)

def create_tar(files: list[tuple[Path, Path]], 
               workdir: str) -> Path:
    """
    Create tar archive with files prefixed by workdir.
    
    Example: src/app.py → /app/src/app.py in tar
    """
    tar_path = Path("app-layer.tar")
    with tarfile.open(tar_path, "w") as tar:
        for abs_path, rel_path in files:
            arcname = f"{workdir.lstrip('/')}/{rel_path}"
            tar.add(abs_path, arcname=arcname)
    return tar_path

def hash_file(path: Path) -> str:
    """Compute SHA256 digest of file."""
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()
```

#### `config.py`

**Purpose**: Build configuration and validation.

```python
@dataclass
class BuildConfig:
    """Configuration for container image build."""
    tag: str
    context_path: Path
    workdir: str = "/app"
    env: dict[str, str] = field(default_factory=dict)
    include_paths: list[str] = field(default_factory=list)
    base_image: str | None = None  # Phase 2
    registry: str | None = None
    use_cache: bool = True
    
    def __post_init__(self):
        self.context_path = Path(self.context_path)
        if not self.context_path.exists():
            raise ValueError(f"Context path not found: {self.context_path}")
        
        if not self.include_paths:
            # Auto-detect include paths
            self.include_paths = default_include_paths(self.context_path)
    
    @classmethod
    def from_toml(cls, toml_path: Path) -> "BuildConfig":
        """Load config from pycontainer.toml file."""
        import tomllib
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        return cls(**data["build"])
```

#### `cache.py` — Phase 1

**Purpose**: Layer and blob caching for fast incremental builds.

```python
class BlobCache:
    def __init__(self, cache_dir: Path = None):
        self.cache_dir = cache_dir or Path.home() / ".pycontainer/cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def get(self, digest: str) -> Path | None:
        """Get cached blob by digest."""
        blob_path = self.cache_dir / "blobs/sha256" / digest
        return blob_path if blob_path.exists() else None
    
    def put(self, digest: str, data: bytes) -> Path:
        """Store blob in cache."""
        blob_path = self.cache_dir / "blobs/sha256" / digest
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(data)
        return blob_path
    
    def evict_lru(self, max_size_mb: int = 1000):
        """Evict least recently used blobs to stay under size limit."""
        # Implement LRU eviction policy
        pass
```

---

## Data Flow

### Build Flow (Phase 0 - Current)

```
1. User runs: pycontainer build --tag myapp:latest

2. CLI parses args → BuildConfig(tag="myapp:latest", context_path=".")

3. ImageBuilder.build():
   ├─ discover_project() → ProjectMetadata
   ├─ collect_files() → [(abs_path, rel_path), ...]
   ├─ create_tar() → app-layer.tar
   ├─ hash_file() → layer_digest
   ├─ build_config_json() → OCIConfig
   ├─ build_manifest() → OCIManifest
   └─ write_output() → dist/image/
       ├─ manifest.json
       └─ blobs/sha256/
           ├─ <config-digest>
           └─ <layer-digest>

4. Output: dist/image/ (OCI image layout)
```

### Push Flow (Phase 1 - Planned)

```
1. User runs: pycontainer build --tag myapp:latest --push

2. ImageBuilder.build() → dist/image/

3. ImageBuilder.push():
   ├─ registry_client.push_blob(config_json)
   ├─ registry_client.push_blob(layer_tar)
   └─ registry_client.push_manifest(manifest, tag)

4. Output: Image pushed to ghcr.io/user/myapp:latest
```

### Base Image Flow (Phase 2 - Planned)

```
1. BuildConfig(base_image="python:3.11-slim")

2. ImageBuilder.build():
   ├─ registry_client.pull_manifest("python:3.11-slim")
   ├─ registry_client.pull_layers([layer1, layer2, ...])
   ├─ parse_base_config() → base_env, base_workdir
   ├─ merge_configs(base_config, user_config)
   ├─ create_app_layer()
   └─ build_manifest([base_layers..., app_layer])

3. Output: Multi-layer image with base + app
```

---

## Architectural Patterns

### 1. Dataclass-Driven Configuration

All configuration uses Python dataclasses for type safety and validation:

```python
@dataclass
class BuildConfig:
    tag: str
    context_path: Path
    # ... validates on construction

config = BuildConfig(tag="app:v1", context_path="/invalid/path")
# Raises ValueError: Context path not found
```

### 2. Pure Functions for Core Logic

Core operations are pure functions (no side effects):

```python
def build_manifest(config_desc: OCIDescriptor, 
                   layer_descs: list[OCIDescriptor]) -> OCIManifest:
    # Pure function: same inputs → same output
    return OCIManifest(config=config_desc, layers=layer_descs)
```

### 3. Composition Over Inheritance

Components composed rather than inherited:

```python
class ImageBuilder:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.registry_client = RegistryClient(config.registry) if config.registry else None
        self.cache = BlobCache() if config.use_cache else None
```

### 4. Explicit Dependencies

All dependencies passed explicitly (dependency injection):

```python
def discover_project(context_path: Path, 
                     pyproject_parser: Callable = parse_pyproject_toml):
    # Dependency injectable for testing
    pyproject = pyproject_parser(context_path / "pyproject.toml")
```

---

## Design Decisions

### Why No Docker Daemon?

**Rationale**: Enable container builds in environments without Docker:
- GitHub Codespaces (Docker not pre-installed)
- Dev Box / Cloud Dev Environments
- CI systems without Docker (faster startup)
- Locked-down corporate laptops

**Approach**: Implement OCI spec directly using Python stdlib + HTTP requests.

### Why Single Layer (Phase 0)?

**Rationale**: Simplify initial implementation, prove feasibility.

**Future**: Phase 2 adds multi-layer support (base + deps + app).

### Why Dataclasses Over Dicts?

**Rationale**: Type safety, autocompletion, validation.

**Example**:
```python
# Dict (error-prone)
config = {"tag": "app:v1", "context": "/path"}

# Dataclass (type-safe)
config = BuildConfig(tag="app:v1", context_path="/path")
```

### Why Auto-Detection?

**Rationale**: Minimize configuration, match .NET SDK experience.

**Balance**: Auto-detect defaults, allow explicit overrides.

---

## Extension Points

### Adding New Base Images (Phase 2)

1. Implement base image parser in `registry_client.py`
2. Add layer merging logic in `builder.py`
3. Update `oci.py` to handle multi-layer manifests

### Adding Framework Support (Phase 4)

1. Add framework detection in `project.py`:
   ```python
   def detect_fastapi(context_path: Path) -> bool:
       # Look for "from fastapi import FastAPI"
       pass
   ```

2. Add entrypoint generation in `oci.py`:
   ```python
   def fastapi_entrypoint(module: str) -> list[str]:
       return ["uvicorn", f"{module}:app", "--host", "0.0.0.0"]
   ```

### Adding Registry Support

1. Implement auth provider in `registry_client.py`:
   ```python
   class AzureContainerRegistryAuth(AuthProvider):
       def get_token(self) -> str:
           # Use Azure CLI credentials
           pass
   ```

2. Register in factory:
   ```python
   AUTH_PROVIDERS = {
       "ghcr.io": GitHubTokenAuth,
       "azurecr.io": AzureContainerRegistryAuth,
   }
   ```

---

## Performance Considerations

### Current Performance (Phase 0)

| Operation | Time | Bottleneck |
|-----------|------|------------|
| Project discovery | <100ms | Disk I/O (read pyproject.toml) |
| File collection | ~500ms | Disk I/O (iterate files) |
| Tar creation | ~1s | Disk I/O (write tar) |
| Hash calculation | ~500ms | CPU (SHA256) |
| JSON generation | <50ms | CPU (serialize) |
| **Total** | **~2.5s** | Disk I/O |

### Optimization Strategies (Future)

1. **Parallel Hashing**: Hash files concurrently (ThreadPoolExecutor)
2. **Incremental Tar**: Only re-pack changed files
3. **Layer Caching**: Skip tar creation if content unchanged
4. **Blob Streaming**: Stream large layers to registry (avoid disk write)

---

## Security Considerations

### Current (Phase 0)

- No network communication (local-only)
- File permissions preserved in tar
- No external dependencies (pure stdlib)

### Phase 1 (Registry Push)

- **Auth**: Support OAuth2, Basic Auth, token-based
- **TLS**: HTTPS required for registry communication
- **Credentials**: Read from ~/.docker/config.json (never log)
- **Blob Integrity**: Verify SHA256 digest on upload

### Phase 2 (Base Images)

- **Image Verification**: Validate base image signatures
- **Supply Chain**: SBOM generation (Phase 4)
- **Minimal Attack Surface**: Prefer distroless base images

---

## Future Architecture Evolution

### Phase 1: Add Registry Client

```
┌──────────────┐
│ ImageBuilder │
└──────┬───────┘
       │
       ├──> LocalOutput (dist/image/)
       └──> RegistryClient (push to registry)
```

### Phase 2: Add Base Image Support

```
┌──────────────┐
│ ImageBuilder │
└──────┬───────┘
       │
       ├──> BaseImagePuller (fetch base layers)
       ├──> LayerMerger (merge base + app)
       └──> Output (multi-layer image)
```

### Phase 3: Add Plugin System

```
┌──────────────┐
│ ImageBuilder │
└──────┬───────┘
       │
       ├──> PreBuildHook (e.g., run tests)
       ├──> PostBuildHook (e.g., security scan)
       └──> CustomLayerProvider (e.g., SBOM layer)
```

---

## Comparison to .NET SDK

| Feature | .NET SDK | pycontainer-build | Status |
|---------|----------|-------------------|--------|
| No Dockerfile | ✅ | ✅ | Complete |
| No Docker daemon | ✅ | ✅ | Complete |
| Auto-detect entry point | ✅ | ✅ | Complete |
| Base image support | ✅ | 🔜 | Phase 2 |
| Push to registry | ✅ | 🔜 | Phase 1 |
| Multi-arch builds | ✅ | 🔜 | Phase 4 |
| Layer caching | ✅ | 🔜 | Phase 1 |

---

## References

- **OCI Image Spec**: <https://github.com/opencontainers/image-spec/blob/main/spec.md>
- **OCI Image Layout**: <https://github.com/opencontainers/image-spec/blob/main/image-layout.md>
- **Docker Registry v2 API**: <https://docs.docker.com/registry/spec/api/>
- **.NET SDK Containers**: <https://github.com/dotnet/sdk-container-builds>

---

**Last Updated**: 2025-11-19  
**Document Version**: 1.0  
**Maintainers**: pycontainer-build team
