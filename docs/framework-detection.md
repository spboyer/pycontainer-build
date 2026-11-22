# Framework detection

The builder inspects selected files and declared dependencies without importing
or executing project code. Detection chooses image defaults; it does not install
dependencies. Use `--include-deps` with a supported requirements file or
provide the packages in the base image.

| Framework | Evidence | Default entrypoint | Ports |
|---|---|---|---|
| FastAPI | Module-level assignment to `FastAPI(...)`, including imported aliases | `uvicorn <module>:<variable> --host 0.0.0.0 --port 8000` | 8000 |
| Flask | Module-level assignment to `Flask(...)`, including imported aliases | `flask run --host=0.0.0.0 --port=5000` | 5000 |
| Django | Root `manage.py` referring to Django | `python manage.py runserver 0.0.0.0:8000` | 8000 |
| Streamlit | A script importing Streamlit and the `streamlit` dependency | `streamlit run <file> --server.address=0.0.0.0 --server.port=8501` | 8501 |
| Gradio | A script creating and launching `Blocks` or `Interface`, and the `gradio` dependency | `python <file>` | 7860 |
| Jupyter | A notebook and the `jupyterlab` or `notebook` dependency | `jupyter lab --ip=0.0.0.0 --port=8888 --no-browser`, or `jupyter notebook` with the same options | 8888 |
| Celery | A module-level assignment to `Celery(...)` and the `celery` dependency | `celery -A <module>:<variable> worker --loglevel=INFO` | None |

Gradio defaults `GRADIO_SERVER_NAME` to `0.0.0.0` only when neither the build
configuration nor the base image supplies it. A literal `launch(server_port=...)`
or `GRADIO_SERVER_PORT` determines its default exposed port; configure ports
explicitly when the listener port cannot be inferred. For Streamlit, a single
explicitly exposed port or `STREAMLIT_SERVER_PORT` changes the generated
listener port. When no listener is specified by the build, an inherited
`STREAMLIT_SERVER_PORT` is honored even if exposed-port metadata is explicitly
cleared. An explicit command's `--server.port` before `--` takes precedence.
Conflicting explicit values for a generated command are rejected. JupyterLab
wins when both Jupyter dependencies are declared.

## Precedence and ambiguity

The order is **FastAPI, Flask, Django, Streamlit, Gradio, Jupyter, Celery**.
When several frameworks are detected, the builder warns and uses the first,
unless a configured command identifies another detected application.
A Gradio app mounted in FastAPI is treated as FastAPI. A detected Celery worker
alongside a web application needs a separate image or an explicit entrypoint.

Multiple FastAPI, Flask, or Celery applications or multiple Streamlit or Gradio
scripts cannot be selected safely: the build asks for an explicit entrypoint
or command. An unlaunched Gradio application and a Celery app without an
importable module-level name also require an explicit entrypoint. A valid
higher-precedence framework still wins over ambiguous lower-precedence
candidates. Dynamic factories and other patterns that cannot be established
statically may need manual configuration. Celery applications under `src/`
use that directory as a Python import root.

Python sources that appear relevant are parsed statically, up to 1 MiB per
file. If a relevant source cannot be parsed or is too large and could change
the selected framework, specify the entrypoint or command explicitly. A
dynamic or conflicting Gradio `server_port` also requires explicit exposed
ports. Port ambiguity in an unselected script does not prevent selecting
another script explicitly.

For the new detectors, dependency names are read from the configured
requirements file (including local `-r` files inside the context),
`[project].dependencies`, non-optional `[tool.poetry.dependencies]`, and
`[options] install_requires` in `setup.cfg`. Dependency extras, version
constraints, and markers are not resolved by detection. Optional dependency
groups and remote requirements includes are not inspected. Local requirements
includes support pip's `${NAME}` environment-variable syntax and must stay
inside the build context; their combined size is limited to 1 MiB and nesting
to 64 files. Local `file:` requirements in `setup.cfg` are also inspected.

The normal include/exclude rules apply, including when a caller supplies a
preselected file iterable. Test, virtualenv, and
`.ipynb_checkpoints` paths are excluded by default; `--no-default-excludes`
opts out. With automatic file selection, notebook files outside excluded paths
are included when Jupyter is declared; explicit includes remain authoritative.
Source and notebook symlinks are skipped with a warning unless every target in
the link chain is selected, not excluded, and a relative POSIX-style path.
Notebooks without a declared Jupyter dependency do not trigger detection, and
`--verbose` reports why. Include any accompanying data files explicitly when
they are outside the other automatically selected paths.

## Explicit configuration

Set overrides in `pycontainer.toml` and pass `--config pycontainer.toml`:

```toml
[build]
entrypoint = ["python", "-m", "myapp"]
cmd = ["--worker"]
exposed-ports = []
```

Configured entrypoints, commands, and ports are not replaced by detection.
An empty list explicitly clears a value. A command without an entrypoint
suppresses the generated framework entrypoint and retains any base-image
entrypoint; set `entrypoint = []` to clear it. Generated framework entrypoints
clear the base image's default command so it is not appended to the invocation.
For API callers, `BuildConfig.exposed_ports` remains a mutable list. Leaving its
default untouched permits detection and base-image inheritance; passing `[]`
or explicitly clearing the list clears exposed ports.

## Jupyter authentication

Jupyter authentication is not disabled and no token is baked into the image.
The server normally generates a token unless a password or other authentication
is configured. To supply a token, set `JUPYTER_TOKEN` through the container
runtime's secret mechanism (or use `JUPYTER_TOKEN_FILE`); do not put it in
`[build.env]`. Restrict access to the exposed port and see the
[Jupyter Server security documentation](https://jupyter-server.readthedocs.io/en/stable/operators/security.html).

When the image user is root, the generated command adds `--allow-root` because
Jupyter otherwise refuses to run as root. This does not disable authentication,
but a compromised server can act with root privileges. Prefer a non-root image
user with appropriate writable directories.

When dependency staging includes Jupyter data files, the builder adds their
locations to Jupyter's search paths and sets `JUPYTERLAB_DIR` if it is not
already configured. A copied virtualenv may not contain JupyterLab's
`share/jupyter/lab` data under `site-packages`; use a supported requirements
file or provide those assets in the base image.
