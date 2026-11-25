# Your first image

pycontainer builds an OCI image layout from a Python project without a Dockerfile
or a Docker daemon. It can also push that image to a registry. Building does not
start your application; running an image still needs a container runtime.

The project is experimental. Start with a small application and review
[images and dependencies](images-and-dependencies.md) before staging packages.

## 1. Install from source

Use Python 3.11 or newer. Until package publication is available, install from
a repository checkout rather than assuming a PyPI release exists.

```bash
git clone https://github.com/spboyer/pycontainer-build.git
cd pycontainer-build
python -m pip install --no-deps -e .
pycontainer --help
```

For contributor tooling, use
`uv sync --locked --group test --group build` instead. If you do not activate that
environment, prefix commands with `uv run --locked --group test --group build`.

## 2. Preview the example

The repository includes a FastAPI application. Preview its configuration without
pulling a base image or building layers:

```bash
pycontainer build --context examples/fastapi-app --tag demo:local --dry-run
```

For your own project, run `pycontainer init --template fastapi --app main:app`
from its directory. The template writes configuration, not application code.
See [configuration templates](init-config.md) for Flask and generic projects.

## 3. Build an OCI layout

```bash
pycontainer build --context examples/fastapi-app --tag demo:local --base-image python:3.11-slim-bookworm --include-deps
```

This pulls the base image and stages the example's dependencies. It needs network
access and pip in the builder's Python environment. Wheel-only staging is not
cross-compilation; unsupported packages fail rather than falling back to source
builds.

The output is under the build context: `examples/fastapi-app/dist/image/`.
It contains `oci-layout`, `index.json`, content-addressed blobs and tag references.
It is not a Docker archive.

## 4. Validate and run

Inspect the output offline:

```bash
pycontainer validate image --output examples/fastapi-app/dist/image
```

Validation checks OCI structure and image metadata. It does not prove that the
application starts.

With Skopeo installed and a running Docker daemon, import the layout and run it:

```bash
skopeo copy oci:examples/fastapi-app/dist/image docker-daemon:demo:local
docker run --rm -p 8000:8000 demo:local
```

Open `http://localhost:8000`. Docker is used here to run the result, not to build it.
For further local workflows, see [local development](local-development.md).

## 5. Publish when ready

Use a registry repository you can write to:

```bash
pycontainer build --context examples/fastapi-app --tag ghcr.io/YOUR_USER/demo:latest --base-image python:3.11-slim-bookworm --include-deps --push
```

Do not put credentials in commands, configuration files or image layers.
Follow [registry authentication](AUTHENTICATION.md) and
[publication](REGISTRY_PUSH.md) for host-scoped authentication and transport rules.

## Where next?

- [Configuration](../README.md#configuration): CLI settings, TOML, dependencies and cache.
- [Image validation](validation.md): exact checks and coverage limits.
- [Deployment artifacts](deployment-artifacts.md): generate Kubernetes, Helm or Compose files.
- [Skaffold and Tilt](skaffold-tilt.md): registry-based development loops.
