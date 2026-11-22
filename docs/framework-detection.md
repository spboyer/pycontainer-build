# Framework detection

The builder inspects selected files and declared dependencies without importing
or executing project code. Detection chooses image defaults; it does not install
dependencies. Use `--include-deps` with a supported requirements file or
provide the packages in the base image.

| Framework | Evidence | Default entrypoint | Ports |
|---|---|---|---|
| FastAPI | Assignment to `FastAPI(...)` | `uvicorn <module>:<variable> --host 0.0.0.0 --port 8000` | 8000 |
| Flask | Assignment to `Flask(...)` | `flask run --host=0.0.0.0 --port=5000` | 5000 |
| Django | Root `manage.py` referring to Django | `python manage.py runserver 0.0.0.0:8000` | 8000 |
| Streamlit | A script importing Streamlit and the `streamlit` dependency | `streamlit run <file> --server.address=0.0.0.0 --server.port=8501` | 8501 |
| Gradio | A script creating and launching `Blocks` or `Interface`, and the `gradio` dependency | `python <file>` | 7860 |
| Jupyter | A notebook and the `jupyterlab` or `notebook` dependency | `jupyter lab --ip=0.0.0.0 --port=8888 --no-browser`, or `jupyter notebook` with the same options | 8888 |
| Celery | A module-level assignment to `Celery(...)` and the `celery` dependency | `celery -A <module>:<variable> worker --loglevel=INFO` | None |

Gradio defaults `GRADIO_SERVER_NAME` to `0.0.0.0` without replacing a value
provided in `[build.env]`. JupyterLab wins when both Jupyter dependencies are
declared.

## Precedence and ambiguity

The order is **FastAPI, Flask, Django, Streamlit, Gradio, Jupyter, Celery**.
When several frameworks are detected, the builder warns and uses the first.
A Gradio app mounted in FastAPI is treated as FastAPI. A detected Celery worker
alongside a web application needs a separate image or an explicit entrypoint.

Multiple Celery apps or multiple Streamlit or Gradio scripts cannot be selected
safely: the build asks for an explicit entrypoint or command. An unlaunched
Gradio application and a Celery app without an importable module-level name
also require an explicit entrypoint. A valid higher-precedence framework still
wins over ambiguous lower-precedence candidates. Dynamic factories and other
patterns that cannot be established statically may need manual configuration.

For the new detectors, dependency names are read from the configured
requirements file (including local `-r` files inside the context),
`[project].dependencies`, non-optional `[tool.poetry.dependencies]`, and
`[options] install_requires` in `setup.cfg`. Dependency extras, version
constraints, and markers are not resolved by detection. Optional dependency
groups and remote requirements includes are not inspected.

The normal include/exclude rules apply. Test and virtualenv paths are excluded
by default. With automatic file selection, notebook files outside excluded
paths are included when Jupyter is declared; explicit includes remain
authoritative. Notebooks without a declared Jupyter dependency do not trigger
detection, and `--verbose` reports why.

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
For API callers, `BuildConfig.exposed_ports` is `None` when unspecified; use
`[]` to explicitly clear exposed ports.

## Jupyter authentication

Jupyter authentication is not disabled and no token is baked into the image.
The server normally generates a token unless a password or other authentication
is configured. To supply a token, set `JUPYTER_TOKEN` through the container
runtime's secret mechanism (or use `JUPYTER_TOKEN_FILE`); do not put it in
`[build.env]`. Restrict access to the exposed port and see the
[Jupyter Server security documentation](https://jupyter-server.readthedocs.io/en/stable/operators/security.html).
