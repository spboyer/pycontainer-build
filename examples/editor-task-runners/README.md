# Editor and task-runner examples

These are opt-in examples for a Python project with a `pycontainer.toml` at
its root. They run the installed CLI as `python -m pycontainer.cli` and pass
both `--context` and `--config` explicitly. No editor plugin or container
daemon is needed. A real build pulls its base image from a registry and writes
an OCI layout; a dry-run does neither.

**Dry-run disclosure:** the current build plan prints configured environment
and label values. The supplied config uses synthetic values only. Do not put
secrets in these examples or share dry-run output containing sensitive values.
The CLI currently ends a dry-run with `Built: <tag>` even though it has only
displayed the plan.

## Prerequisites and safe setup

- Python 3.11 or newer, with `pip` and pycontainer-build installed in the
  interpreter used by the editor or task runner.
- The relevant tool: PyCharm with Python support, Vim with `+job`, Emacs
  25.1 or newer, GNU Make, or [Go Task](https://taskfile.dev/) version 3.
  The Vim example targets Vim's `job_start` API, not Neovim.
- A `pycontainer.toml` in the project root. Adapt
  [pycontainer.example.toml](pycontainer.example.toml) if you do not already
  have one; do not overwrite an existing config. It selects a dedicated
  `dist/pycontainer-example` output directory. Choose an unused directory:
  builds can replace OCI layout metadata in their configured output, and use
  the normal pycontainer cache.

The core package is not yet published on PyPI
([#79](https://github.com/spboyer/pycontainer-build/issues/79)). From a
checkout, install it into the **project's** selected interpreter:

```powershell
& 'C:\path to project\.venv\Scripts\python.exe' -m pip install --no-deps -e 'C:\path to pycontainer-build'
```

```sh
'/path to project/.venv/bin/python' -m pip install --no-deps -e '/path to pycontainer-build'
```

Use an existing environment or create a new one at an unused path first.
These commands may need network access for the build backend. Adjust the paths
for your machine; no credentials or real environment values are needed.

## PyCharm

Open the application project, select the interpreter containing pycontainer,
and ensure `pycontainer.toml` is at the project root. In **Run | Edit
Configurations**, create two Python configurations using **Module name**
`pycontainer.cli`, the project interpreter, and working directory
`$ProjectFileDir$`. Set their parameters to:

```text
build --context . --config pycontainer.toml
build --context . --config pycontainer.toml --dry-run
```

The explicit paths are relative to the selected project working directory.
Alternatively, import the two [shared run configuration templates](pycharm/)
as project files, using new filenames; do not replace existing `.idea` or
`.run` files. In each configuration dialog, select the application project
module and verify its interpreter contains pycontainer; do not rely on an
automatically selected module in a multi-module project. Run either
configuration in PyCharm; a failed CLI process is shown as a nonzero exit in
the Run tool window.

## Vim

Source [pycontainer.vim](vim/pycontainer.vim) explicitly for the current Vim
session; this does not change `.vimrc` or any project file. For a path with
spaces, use `fnameescape`, for example:

```vim
let g:pycontainer_python = '/path to project/.venv/bin/python'
execute 'source ' . fnameescape('/path to pycontainer-build/examples/editor-task-runners/vim/pycontainer.vim')
```

For native Windows Vim, use Windows paths:

```vim
let g:pycontainer_python = 'C:\path to project\.venv\Scripts\python.exe'
execute 'source ' . fnameescape('C:\path to pycontainer-build\examples\editor-task-runners\vim\pycontainer.vim')
```

Git for Windows' MSYS Vim uses paths such as `/c/path to project/...`;
`cygpath -u` can convert a Windows path for that environment.

Open a project file and set its absolute project root in that buffer:

```vim
let b:pycontainer_project_root = '/path to project'
```

Use the corresponding Windows or MSYS path for that Vim installation. Repeat
this setting in each project buffer. Run `:PycontainerBuild` or
`:PycontainerDryRun`; the commands use that root as the context and working
directory, require a readable `pycontainer.toml` there, and wait for the CLI.
Output appears in Vim's messages. A failure raises an error and its exit status
is available as `g:pycontainer_last_status`. Set `g:pycontainer_python` to the
project interpreter; it defaults to `python` on `PATH`. Without an explicit
root, the command fails rather than selecting an unrelated project.

## Emacs

Use `M-x load-file` to load [pycontainer.el](emacs/pycontainer.el) for the
current session; it does not edit `init.el` or `.dir-locals.el`. Set the
interpreter with, for example:

```elisp
(setq pycontainer-python-executable "/path to project/.venv/bin/python")
```

For native Windows Emacs, escape the backslashes in the string:

```elisp
(setq pycontainer-python-executable "C:\\path to project\\.venv\\Scripts\\python.exe")
```

Open a project buffer and set its absolute project root:

```elisp
(setq-local pycontainer-project-root "/path to project")
```

For Windows paths, escape backslashes as in the interpreter example above.
Repeat this setting in each project buffer. Run `M-x pycontainer-build` or
`M-x pycontainer-dry-run`; the commands use that root as the context and
working directory, require a readable `pycontainer.toml` there, and wait for
the CLI. Output appears in `*pycontainer*`; a failure signals an error and
records the exit status in `pycontainer-last-status`. The interpreter defaults
to `python` on `PATH`. Without an explicit root, the command fails rather than
selecting an unrelated project.

## GNU Make and Go Task

Run these commands **from the application project root**. Point to the
templates in this checkout instead of replacing an existing `Makefile` or
`Taskfile.yml`. Put the project interpreter's directory on `PATH` so that
`python` resolves to the interpreter containing pycontainer:

```sh
export PATH='/path to project/.venv/bin':"$PATH"
make -f '/path to pycontainer-build/examples/editor-task-runners/tasks/Makefile.pycontainer' pycontainer-dry-run
make -f '/path to pycontainer-build/examples/editor-task-runners/tasks/Makefile.pycontainer' pycontainer-build
task --taskfile '/path to pycontainer-build/examples/editor-task-runners/tasks/Taskfile.pycontainer.yml' --dir . dry-run
task --taskfile '/path to pycontainer-build/examples/editor-task-runners/tasks/Taskfile.pycontainer.yml' --dir . build
```

In PowerShell, for example:

```powershell
$env:PATH = 'C:\path to project\.venv\Scripts' + ';' + $env:PATH
make -f 'C:\path to pycontainer-build\examples\editor-task-runners\tasks\Makefile.pycontainer' pycontainer-dry-run
make -f 'C:\path to pycontainer-build\examples\editor-task-runners\tasks\Makefile.pycontainer' pycontainer-build
task --taskfile 'C:\path to pycontainer-build\examples\editor-task-runners\tasks\Taskfile.pycontainer.yml' --dir . dry-run
task --taskfile 'C:\path to pycontainer-build\examples\editor-task-runners\tasks\Taskfile.pycontainer.yml' --dir . build
```

Both runners stop with a nonzero status when the CLI fails. They pass
`--context . --config pycontainer.toml` from the invocation directory, so
both paths resolve to the application project.

## Optional manual checks

With the editor and interpreter configured, run the dry-run from a project
whose path contains spaces and verify the displayed context and the synthetic
environment and label values from the config.
Temporarily select a deliberately failing interpreter, or use an invalid config
in a disposable scratch project, to confirm failure is reported. Restore the
interpreter afterwards. A dry-run is not evidence that the built image starts.
