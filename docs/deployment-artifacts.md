# Deployment artifacts

`pycontainer generate` writes deployment configuration for an **existing**
image. It does not build, push, pull, apply, start containers, or contact a
registry. Push the image first, or load it into the target runtime separately.
The image must be accessible to the Kubernetes nodes or Compose runtime.

## Generate

```powershell
pycontainer generate kubernetes --image ghcr.io/example/api:v1 --port 8000 --name api --service-port 80 --output deploy
pycontainer generate compose --image ghcr.io/example/api:v1 --port 8000 --name api --host-port 8080 --output compose.yaml
```

Both commands require an image, a container TCP port, and an output path. The
name defaults to `app`. The Kubernetes service port and Compose host port each
default to the container port.

The Kubernetes command creates this **new** directory:

```text
deploy/
  deployment.yaml
  service.yaml
  chart/
    Chart.yaml
    values.yaml
    values.schema.json
    templates/
      deployment.yaml
      service.yaml
```

It generates one `apps/v1` Deployment with one replica and one `v1` ClusterIP
Service. The Helm chart's default values render equivalent resources. The chart
uses the name in its values, not the Helm release name, for resource names;
choose different names for separate installations in the same namespace.

The Compose command writes one service with an `image` and a TCP port mapping
bound to `127.0.0.1`. It does not generate a `build` section, import or
translate Compose files, or run Docker Compose. Change the binding manually if
access beyond the local host is intended.

The generator does not read build settings or environment variables and does
not accept environment values, credentials, secret values, volumes, health
checks, or runtime-security settings. Configure those separately as needed.
Image metadata does not enforce runtime security controls; these artifacts do
not claim to provide such enforcement.

## Input and format contract

- Names are 1-63 lowercase ASCII letters, digits, or hyphens, beginning with
  a letter and ending with a letter or digit.
- Ports are integers from 1 through 65535.
- Image references are at most 255 ASCII characters, using lowercase registry
  hosts and repository components, an optional registry port, an optional tag,
  and/or an optional lowercase `sha256` digest. Tags may contain uppercase
  letters. Schemes, credentials, whitespace, dollar signs, backslashes,
  queries, fragments, and IPv6 registry hosts are not supported.
- Files use JSON-formatted YAML, a YAML-compatible representation that
  preserves scalar types and quotes strings. Helm templates serialize values
  with `toJson`; the generated `values.schema.json` validates the supported
  fields and rejects unknown fields during normal Helm operations. Disabling
  Helm schema validation bypasses those input checks.

These are deliberately bounded artifacts. They use Kubernetes `apps/v1` and
`v1` APIs available in Kubernetes 1.16 and later, Helm 3's chart `apiVersion:
v2`, and the Compose Specification as implemented by Docker Compose CLI 2 or
later. They do not prove image existence, platform compatibility, a working
container entrypoint, or successful deployment. The application must listen on
the selected container port and an interface reachable inside the container.

## Output safety

The output's parent directory must already exist. Existing output files,
directories, and symlinks, including dangling symlinks, are refused; there is
no overwrite or force option. Files are staged in a temporary directory on the
same filesystem, flushed, and published by exclusive hard links. Each
published file appears atomically and is never replaced, including when two
generators race. The filesystem must support hard links; failures are reported
instead of falling back to an unsafe copy.

The Kubernetes directory is reserved exclusively, but its several files are
**not** published as a single transaction. An interruption or I/O error can
leave a new, partial directory; inspect and remove it yourself before retrying.
Do not treat the bundle as complete until the command succeeds. A crash may
also leave staging files, and no power-failure durability guarantee is made.
Use a trusted output parent: existing parent symlinks are followed, and the
generator does not defend against another process replacing parent directories
or symlinks while it runs.

## Optional validation

With Helm 3.22, [kubeconform](https://github.com/yannh/kubeconform), and Docker
Compose installed, the following checks do not apply resources or start a
container. Older Helm 3 releases can render the chart, but may not support
the `--kube-version` flag used here:

```powershell
helm template review .\deploy\chart --kube-version 1.31.0
kubeconform -strict -summary -kubernetes-version 1.31.0 .\deploy\deployment.yaml .\deploy\service.yaml
helm template review .\deploy\chart --kube-version 1.31.0 | kubeconform -strict -summary -kubernetes-version 1.31.0
docker compose -f .\compose.yaml config --quiet
```

By default, kubeconform downloads Kubernetes schemas and therefore needs
network access; a local schema location can be supplied for offline use. Helm
rendering and `docker compose config` do not require a cluster, registry, or
running Docker daemon. These checks do not establish registry reachability,
cluster admission, or runtime behavior.

The external-tool regression is opt-in and fails if a required tool is absent.
It requires Helm with `--kube-version` and `--skip-schema-validation` support,
kubeconform, Docker Compose, and network access to the default Kubernetes
schema registry and a pinned historical schema snapshot for Kubernetes 1.16:

```powershell
$env:PYTHONPATH = 'src'
$env:PYCONTAINER_VALIDATE_DEPLOYMENT_ARTIFACTS = '1'
python -m pytest tests\test_deployment_artifacts.py -q -k external_tools
```

The default hermetic test suite does not run these external checks.
