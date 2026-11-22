# Jenkins

Copy [Jenkinsfile](../../examples/ci/Jenkinsfile) into the application
repository and create a Pipeline from SCM or trusted multibranch pipeline.
Install the Pipeline and Credentials Binding plugins and configure agent
labels `linux` and/or `windows`. Select `AGENT_LABEL` when starting the job.
Both paths require Python 3.11 (`python` on PATH), pip and Git. The Windows
path uses `bat`; the Linux path uses `sh`. Both build a **Linux/amd64** image,
including when the build host is Windows.

Change `IMAGE_REPOSITORY` to a fully qualified registry/repository, without
a tag, such as `ghcr.io/your-org/your-app`. Create a Jenkins **Username with
password** credential with ID `container-registry`, using a registry
username and write-scoped password/access token. `withCredentials` binds
`REGISTRY_USERNAME` and `REGISTRY_PASSWORD` only around build/push.
Shell tracing is disabled, Groovy strings are single-quoted (no Groovy secret
interpolation), and no secret is passed on the command line. Inherited
`REGISTRY_TOKEN` and `GITHUB_TOKEN` are cleared so they cannot override the
credential binding.

Only allow trusted branches/jobs to use this credential. Do not expose it
to fork pull requests or unreviewed Jenkinsfiles; masking does not prevent
malicious code from reading environment variables. Use isolated agents rather
than sharing a credential-bearing host with untrusted jobs.

The builder installs from immutable merged commit
`7bdec100c773d9a51e5aae63eb0d4456894ae43f`, not PyPI; review pin updates before
adopting a newer release. The Git plugin's `GIT_COMMIT`, set by the implicit
declarative checkout, supplies the image tag; do not add
`skipDefaultCheckout`, because an explicit `checkout scm` step does not
populate `env.GIT_COMMIT`. Shell or installation errors fail the stage,
and credentials are not included in cache or image paths.

The default `~/.pycontainer/cache` is used (`USERPROFILE` on Windows).
Persistence depends on the agent's home directory; ephemeral agents start
with a cold cache. This example does not add a Jenkins cache plugin.
The repository root is the app context; `.ci-venv` is explicitly excluded.
For supported wheel-only dependency staging, add
`--include-deps --requirements requirements.txt` and consult
[dependency staging](../../README.md#explicit-configuration). The base image and build
interpreter should remain on Python 3.11.

The [hermetic CI example tests](../../tests/test_ci_examples.py) check both
shell branches against the real CLI parser. They do not execute Groovy or
prove agent setup, registry access or runtime startup. This example has not
been run on real Linux or Windows Jenkins agents. Validate it with Jenkins'
Declarative Pipeline linter and run both agent paths before production use.
No Docker daemon is required.
