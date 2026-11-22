# CircleCI

Copy [circleci-config.yml](../../examples/ci/circleci-config.yml) to
`.circleci/config.yml` in the application repository. This is a CircleCI 2.1
**config example, not a published orb**. Orb publishing is deferred until
publishing ownership and maintenance are established.

Change `IMAGE_REPOSITORY` to a fully qualified registry/repository without a
tag. Create the `registry-push` context containing `REGISTRY_USERNAME` and
`REGISTRY_PASSWORD` (a write-scoped password/access token). Restrict context
access to trusted projects/users and protect `main`. Do not allow fork builds
or unreviewed config to access the context. The workflow runs only on `main`;
tag workflows are not enabled.

The `cimg/python:3.11` executor provides Python, pip and Git. The builder
installs in a job-local venv from immutable merged commit
`7bdec100c773d9a51e5aae63eb0d4456894ae43f`, rather than the unpublished PyPI
package. Review pin updates when adopting newer releases. Each `run` step
activates the venv independently.

Builds push a `CIRCLE_SHA1` tag to `IMAGE_REPOSITORY`. Secrets are read through
the supported environment auth provider; missing credentials fail before the
build. Inherited `REGISTRY_TOKEN` and `GITHUB_TOKEN` are cleared so they cannot
override the context's username/password binding. Do not enable shell tracing
or print credentials. A fully qualified
tag selects the registry, so no host-only `--registry` override is used.

Cache restore/save uses the executor user's default
`~/.pycontainer/cache` (`/home/circleci/.pycontainer/cache`). CircleCI caches
are immutable: a per-revision save key and branch prefix restore the newest
available cache. Bump the `v1` prefix to reset it. Restrict cache access to
trusted builds; it contains application layer contents, not credentials.
Cache misses rebuild normally. No remote Docker setup is needed; the executor
is a container, but the builder does not use a Docker daemon.

The app context is the repository root and excludes `.ci-venv`. Builds target
`linux/amd64` with `python:3.11-slim`. Add
`--include-deps --requirements requirements.txt` if the app needs supported
wheel-only staging; consult [dependency staging](../../README.md#explicit-configuration)
before changing Python/platform settings.

The [hermetic CI example tests](../../tests/test_ci_examples.py) validate CLI
arguments without network access. They do not validate CircleCI's schema,
contexts, cache service, registry access or runtime startup. This example
has not been executed on real CircleCI. Run `circleci config validate`
and a restricted test build before production use.
