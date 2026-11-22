# GitLab CI

Copy [gitlab-ci.yml](../../examples/ci/gitlab-ci.yml) to `.gitlab-ci.yml`
and [pycontainer_ci.py](../../examples/ci/pycontainer_ci.py) to
`.ci/pycontainer_ci.py` in your application repository. Enable the project's container registry and
protect the default branch and release tags. The example runs only for push
pipelines on that protected branch or protected tags, not merge requests.
Use a Linux runner supporting the `python:3.11` image.

The builder installs from immutable merged commit
`7bdec100c773d9a51e5aae63eb0d4456894ae43f`, not the unpublished PyPI package.
Python 3.11, pip and Git are required; the Python image provides them.
Review and update this pin when adopting a newer builder release.

GitLab supplies `CI_REGISTRY` (registry host), `CI_REGISTRY_IMAGE` (full image
repository), `CI_REGISTRY_USER` and `CI_JOB_TOKEN`. The last two are mapped to
the supported `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` environment
variables; credentials never appear in command arguments. The fully qualified
`CI_REGISTRY_IMAGE` includes `CI_REGISTRY`; do not add a host-only `--registry`
override. Keep debug tracing (`CI_DEBUG_TRACE`) disabled and do not echo
credentials. The job token has short-lived access to this project's registry;
cross-project pushes need separately scoped credentials. Inherited
`REGISTRY_TOKEN` and `GITHUB_TOKEN` are cleared so they cannot override the
job's username/password binding. The helper places those credentials in a
temporary Docker config scoped to the destination registry, then removes the
generic credential variables from the builder process. This prevents a public
base-image pull from receiving the push credentials. It does not support
`--registry`, `--username` or `--password`; use a fully qualified tag instead.
Pass `--context` and either `--cache-dir` or `--no-cache` explicitly so the
helper can keep temporary credentials outside the context and cache. Other
registries used for base images must allow anonymous pulls. The builder also
runs without an inherited Azure CLI login.

The temporary config contains plaintext credentials and is removed when the
helper exits normally, including on build failure. Abrupt termination can leave
it on disk; use ephemeral runners and short-lived credentials.

Branch builds push the commit SHA. Tag pipelines accept stable versions such
as `v1.2.3` or `1.2.3` and use `--semver-tags` to also push minor and major
aliases (`v1.2`, `v1`). Other tag formats fail explicitly. No `latest` alias
is created.

GitLab caches only project-relative paths. The example therefore relocates
the default `~/.pycontainer/cache` to `.pycontainer-cache` with `--cache-dir`
and excludes that directory, `.ci-venv` and `.ci` from the image. Configure a
runner cache backend for persistence and retain GitLab's protected/unprotected
cache separation. Cache misses rebuild normally; credentials are never cached.

The application context is the repository root; adapt `--context` as needed.
These examples target `linux/amd64` with `python:3.11-slim`. Dependencies are
not included by default: add `--include-deps --requirements requirements.txt`
for supported wheel-only dependency staging. See [dependency staging](../../README.md#explicit-configuration)
for limits and target compatibility.

The [hermetic CI example tests](../../tests/test_ci_examples.py) validate build
arguments against the real CLI parser, not GitLab scheduling, registry
permissions or container startup. This example has not been executed on a real
GitLab runner. Validate it with GitLab CI Lint and run a protected test build
before production use. No Docker daemon or Docker-in-Docker service is needed.
