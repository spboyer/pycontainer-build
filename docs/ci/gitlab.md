# GitLab CI

Copy [gitlab-ci.yml](../../examples/ci/gitlab-ci.yml) to `.gitlab-ci.yml`
in your application repository. Enable the project's container registry and
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
job's username/password binding.

Branch builds push the commit SHA. Tag pipelines accept stable versions such
as `v1.2.3` or `1.2.3` and use `--semver-tags` to also push minor and major
aliases (`v1.2`, `v1`). Other tag formats fail explicitly. No `latest` alias
is created.

GitLab caches only project-relative paths. The example therefore relocates
the default `~/.pycontainer/cache` to `.pycontainer-cache` with `--cache-dir`
and excludes both that directory and `.ci-venv` from the image. Configure a
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
