# Skaffold and Tilt inner loop

pycontainer-build can be the custom image builder for
[Skaffold](https://skaffold.dev/docs/builders/builder-types/custom/) and
[Tilt](https://docs.tilt.dev/custom_build.html). Two subcommands read each
tool's environment contract directly, build without a Docker daemon, and
**push the image to a registry** through the same registry client as
`pycontainer build --push`:

| Tool | Command | Reads | Pushes |
|------|---------|-------|--------|
| Skaffold | `pycontainer skaffold-build` | `IMAGE`, `BUILD_CONTEXT`, `PUSH_IMAGE`, `PLATFORMS` | exactly `$IMAGE` |
| Tilt | `pycontainer tilt-build` | `EXPECTED_REF` | exactly `$EXPECTED_REF` |
| Tilt (`outputs_image_ref_to`) | `pycontainer tilt-build --image REPO --output-ref FILE` | — | `REPO:tilt-<manifest sha256>`, written to `FILE` |

Working examples: [`examples/skaffold`](../examples/skaffold/) and
[`examples/tilt`](../examples/tilt/). Contract versions checked: Skaffold
v2.25.0 (`skaffold/v4beta14` schema) and Tilt v0.37.9.

## What is supported

- **Registry push only.** pycontainer writes an OCI layout, not a Docker image
  store entry, so the tool must not expect the image in a local daemon.
  - Skaffold: set `build.local.push: true`. `PUSH_IMAGE=false` (the default for
    local clusters such as kind, minikube and Docker Desktop) is rejected with
    exit code 2 before anything is built. There is no import bridge.
  - Tilt: set `skips_local_docker=True`. Tilt then neither inspects nor pushes
    the image; pycontainer pushes it.
- **One platform.** An empty `PLATFORMS` or a single `os/arch[/variant]` is
  honored. More than one platform is rejected until multi-platform manifests
  land in [#29](https://github.com/spboyer/pycontainer-build/issues/29).
- **Explicit registries.** References must be tagged, carry no digest, and name
  their registry host (`localhost:5000/app:tag`, `example.azurecr.io/app:tag`).
  Bare names are rejected instead of being pushed to Docker Hub implicitly.
- **Registry transport.** HTTPS is the default. To use plain HTTP, opt in to
  the exact host and port with `--insecure-registry HOST[:PORT]` or the
  `[build] insecure-registries` setting. pycontainer warns and refuses HTTP
  credentials to non-loopback hosts. For private HTTPS CAs, use
  `--registry-ca-file PATH` or `[build] registry-ca-file`; certificate and
  hostname verification remain enabled. Configure Skaffold, Tilt and the
  cluster separately to access the registry. See
  [registry transport](REGISTRY_PUSH.md#insecure-http-registries-and-custom-cas).
- **The tool owns the tag.** `--tag`, `tags`, `semver_tags`, `latest_tag`,
  `dry_run`, `--push`, `--registry`, `--context` (Skaffold) and credentials are
  not accepted or are overridden; all other `pycontainer.toml` / CLI build
  settings apply. Git-derived tags are incompatible with these commands;
  configuration files must not enable `tag_from_git`.

Failures to build, authenticate or push exit non-zero, so Skaffold and Tilt
report the build as failed. In output-ref mode the reference file is written
atomically, and only after a successful push.

## Credentials

The dev-loop commands take no `--username`/`--password`, so secrets never
appear in commands that Skaffold or Tilt echo. Credentials come from the usual
providers: `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`, `REGISTRY_TOKEN`,
`GITHUB_TOKEN` for `ghcr.io`, static `auths` entries in
`~/.docker/config.json`, or the Azure CLI for `*.azurecr.io`. Docker
`credsStore`/`credHelpers` and `DOCKER_CONFIG` are **not** read; if your Docker
login uses a credential helper, provide one of the other sources.

## Skaffold

```yaml
apiVersion: skaffold/v4beta14
kind: Config
build:
  local:
    push: true
  artifacts:
    - image: pycontainer-demo
      custom:
        buildCommand: python -m pycontainer.cli skaffold-build --no-progress
        dependencies:
          paths: [app/**, pyproject.toml]
```

Skaffold runs `buildCommand` through `sh -c` (`cmd.exe /C` on Windows) in the
artifact's context directory. Keep it free of Go templates: pycontainer reads
`IMAGE` and the other values from the environment, so no untrusted value is
interpolated into a shell. After the command exits, Skaffold resolves the
pushed digest from the registry and deploys it.

```bash
cd examples/skaffold
skaffold run --default-repo example.azurecr.io/dev
```

## Tilt

```python
docker_prune_settings(disable=True)
default_registry('example.azurecr.io/dev')
custom_build(
    'pycontainer-demo',
    ['python', '-m', 'pycontainer.cli', 'tilt-build', '--no-progress'],
    deps=['app', 'pyproject.toml'],
    skips_local_docker=True,
)
```

Use the list (argv) form of the command so no shell is involved. Tilt sets
`EXPECTED_REF` to the registry-qualified ref; pycontainer pushes exactly that
reference, and Tilt deploys it (rewriting the host when the cluster pulls from
a different registry address). The example also sets
`docker_prune_settings(disable=True)` to prevent Tilt's background pruner
from modifying the local Docker cache.

`custom_build(tag=...)` is **not supported**: in that mode Tilt strips the
registry from `EXPECTED_REF` and expects a local daemon image, so the command
is rejected.

### `outputs_image_ref_to`

When `outputs_image_ref_to` is set, Tilt does not set `EXPECTED_REF`. Pass the
repository with `--image` and the same file with `--output-ref`:

```python
ref_file = os.path.join(os.getcwd(), 'dist', 'tilt-image-ref.txt')
custom_build(
    'pycontainer-demo',
    ['python', '-m', 'pycontainer.cli', 'tilt-build',
     '--image', 'example.azurecr.io/dev/pycontainer-demo', '--output-ref', ref_file],
    deps=['app', 'pyproject.toml'],
    skips_local_docker=True,
    outputs_image_ref_to=ref_file,
)
```

pycontainer pushes `REPO:tilt-<64-hex manifest digest>`, so identical inputs
produce the same tag, and writes that reference to the file. Use an absolute
`--output-ref` path; a relative one resolves against Tilt's working
directory. The local OCI layout under `dist/image` is tagged `tilt-pending` in
this mode because the content tag is only known after the manifest is built.

## Verification

- `tests/test_devloop.py` runs the commands from both examples the way each
  tool launches them (environment, working directory, shell or argv form,
  output-file deletion and parsing), against a loopback registry, and checks
  that Docker is never invoked. These tests are hermetic and run in CI.
- `scripts/check_devloop_tools.py` is an **opt-in** real check: with
  `skaffold`, `tilt`, `kubectl`, a current Kubernetes context and an HTTPS
  registry the cluster can pull from, it deploys both examples (Tilt in both
  modes), waits for the rollouts and compares each deployed reference with the
  built or reported reference. It creates and removes a unique namespace, so
  the current Kubernetes identity must have permission to create and delete
  namespaces. It pushes image tags to the supplied registry; use a test
  registry or namespace. It is not part of CI.

```bash
python scripts/check_devloop_tools.py --registry example.azurecr.io/dev
```
