# pycontainer-build for Aspire (experimental)

Aspire can already publish .NET projects as container images without a
Dockerfile or a running Docker daemon. This adapter brings the same experience
to Aspire Python apps: one call replaces the Dockerfile build and push for a
Python resource with [pycontainer-build](../../README.md), so `aspire publish`,
`aspire deploy` and `aspire do push` need neither Docker nor Podman.

`aspire run` is unchanged. In run mode `WithPyContainerBuild` does nothing.

```csharp
var registry = builder.AddContainerRegistry("registry", "ghcr.io", "my-org");

builder.AddUvicornApp("api", "../api", "main:app")
    .WithUv()
    .WithContainerRegistry(registry)
    .WithPyContainerBuild();
```

There is no Dockerfile and no pycontainer configuration file. Everything is
inferred from the Aspire model:

| Image setting | Source |
| --- | --- |
| Entrypoint | The resource's Aspire entrypoint: `AddPythonApp` → `python <script>`, `AddPythonModule` → `python -m <module>`, `AddUvicornApp`/executables → `python -m <executable>` |
| Arguments | Supplied by the deployment target, exactly as for the generated Dockerfile |
| Dependencies | `requirements.txt` if present, otherwise `uv export` of `pyproject.toml` (`--frozen` when `uv.lock` exists) |
| Base image | `python:<version>-slim` from `requires-python`, chosen by pycontainer |
| Platform | The compute environment's requested platform, otherwise `linux/amd64` |
| Registry and tag | Aspire's registry resolution and deploy tag (`ContainerImageReference`) |
| Output | `<AppHost>/obj/pycontainer/<resource>/image` (OCI layout) |

## Requirements

- Tested with Aspire `13.6.5`; earlier versions have published security
  advisories.
- `pycontainer` on `PATH` in the environment that runs the AppHost, with `pip`
  available to its interpreter (dependency staging installs target wheels).
  To use a checkout or a specific interpreter:

  ```csharp
  .WithPyContainerBuild(o =>
  {
      o.Command = "python";
      o.CommandArguments.Add("-m");
      o.CommandArguments.Add("pycontainer.cli");
  });
  ```

- `uv` on `PATH` for `pyproject.toml` projects without `requirements.txt`.
- Registry credentials that pycontainer can find: `REGISTRY_USERNAME` and
  `REGISTRY_PASSWORD`, `GITHUB_TOKEN` for `ghcr.io`, `~/.docker/config.json`,
  or the Azure CLI for `*.azurecr.io`.

The package is not published yet. Reference
`src/PyContainer.Aspire.Hosting/PyContainer.Aspire.Hosting.csproj` from the
AppHost with `IsAspireProjectResource="false"`, as the sample does.

## Options

| Option | Default | Purpose |
| --- | --- | --- |
| `Command`, `CommandArguments` | `pycontainer` | How to start pycontainer |
| `BaseImage` | inferred | Override the base image |
| `Platform` | compute environment, then `linux/amd64` | `linux/amd64` or `linux/arm64` |
| `IncludeDependencies` | `true` | Stage target-compatible dependencies |

## How it works

In publish mode Aspire turns a Python resource into a container resource whose
`build-<name>` and `push-<name>` pipeline steps call the container runtime.
`WithPyContainerBuild` replaces those two steps just before the pipeline runs.
The replacements keep the original names, tags and dependencies, so compute
environments that wait on the push still order correctly. Only the
`check-container-runtime` dependency is dropped. The build step writes an OCI
layout. The push step runs `pycontainer build --push --registry <reference>`,
reusing pycontainer's layer cache.

Host virtualenvs (`.venv`, `venv`, `env`) are always excluded because they
contain host binaries. Dependencies are installed for the Linux target instead.

## Limitations

- **Azure Container Registry with Aspire's Azure provisioning still needs a
  container CLI.** Aspire's `login-to-acr-<name>` step runs `docker login` (or
  Podman) before any push. Registries added with `AddContainerRegistry` have no
  such step. An `*.azurecr.io` endpoint added that way should authenticate
  through pycontainer's Azure CLI support, but that path is not yet validated.
- The entrypoint is read from an internal Aspire annotation by reflection. A
  breaking Aspire change fails the build with a clear error rather than
  silently producing a different image. The adapter is tested against 13.6.5.
- A local registry target (empty endpoint) is rejected: pycontainer pushes
  only to remote registries.
- Resources that copy files from other images (`PublishWithContainerFiles`)
  and archive destinations are rejected.
- An existing `Dockerfile` in the app directory is ignored once
  `WithPyContainerBuild` is used.
- Console-script executables run as `python -m <executable>`, so the package
  must be runnable as a module (uvicorn, gunicorn, flask, etc.).
- Dependency staging is wheel-only CPython for `linux/amd64` and
  `linux/arm64`. See the pycontainer README for details.
- `uv export` creates `uv.lock` when the project has none.
- The exported requirements file is copied into the image at
  `/app/.pycontainer-aspire/<resource>/requirements.txt`, because pycontainer
  applies context excludes when locating the requirements file.
- The push step rebuilds the image, and dependency layers are not yet
  byte-for-byte reproducible, so the pushed digest can differ from the
  `build-<name>` output.

Changes in Aspire that would remove the workarounds: a public Python
entrypoint annotation, and a pluggable image builder and registry login that do
not assume a container CLI.

## Sample and validation

`samples/` contains a uv-based FastAPI app and an AppHost. To push it to a
registry:

```powershell
${env:Parameters__registry-endpoint} = 'ghcr.io'
${env:Parameters__registry-repository} = '<owner>'
dotnet run --project samples/AppHost -- --operation publish --step push --output-path out
```

Unit tests run offline:

```powershell
dotnet test tests/PyContainer.Aspire.Hosting.Tests
```

The publish path was validated end to end on Windows with Aspire 13.6.5:
with no `docker` or `podman` on `PATH`, `--step push` built the sample and
pushed it to a TLS `registry:2`. The pulled image (linux/amd64, entrypoint
`python -m uvicorn`) then served HTTP 200. Docker was used only to host the
test registry and run the result.
