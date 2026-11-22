"""Private package index credentials stay out of images, metadata, logs and errors.

Every secret here is synthetic, the index is a loopback HTTP server, and the
fixture isolates pip from the developer's real config, netrc and keyring.
"""
import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.credentials import redact, url_has_credentials
from pycontainer.dependencies import (
    _pip_failure_message,
    reject_credentialed_direct_urls,
    install_requirements,
    staged_packages,
    validate_requirements,
)
from test_dependency_staging import BASE_CONFIG, _builder, _write_wheel


USER = "synthetic-user"
SECRET = "synthetic-s3cret-147"


class _Index:
    def __init__(self, wheel: Path, artifact_url=None, redirect=False):
        self.wheel = wheel
        self.artifact_url = artifact_url
        self.redirect = redirect
        self.authorized = 0
        self.rejected = 0
        expected = base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
        index = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != f"Basic {expected}":
                    index.rejected += 1
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="synthetic"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                index.authorized += 1
                if index.redirect and self.path.startswith("/packages/"):
                    self.send_response(302)
                    self.send_header("Location", f"{index.artifact_url}{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = index._body(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html"
                    if self.path.startswith("/simple/")
                    else "application/octet-stream",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/simple/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _body(self, path):
        if path.rstrip("/") == "/simple/fixture-dep":
            digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
            name = self.wheel.name
            base = "" if self.redirect else (self.artifact_url or "")
            return (
                f'<html><body><a href="{base}/packages/{name}#sha256={digest}">{name}</a>'
                "</body></html>"
            ).encode()
        if path == f"/packages/{self.wheel.name}":
            return self.wheel.read_bytes()
        return None

    def url_with(self, userinfo):
        return self.url.replace("http://", f"http://{userinfo}@")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every credential and config lookup away from real login state."""
    home = tmp_path / "home"
    home.mkdir()
    for name in list(os.environ):
        if name.upper().startswith(("PIP_", "_PIP_")) or name.upper() in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        }:
            monkeypatch.delenv(name, raising=False)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("NETRC", str(home / "missing-netrc"))
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_KEYRING_PROVIDER", "disabled")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    return home


@pytest.fixture
def index(tmp_path):
    with _Index(_write_wheel(tmp_path / "index-files")) as server:
        yield server


def _context(tmp_path, requirements="fixture-dep==1.0\n"):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    (context / "requirements.txt").write_text(requirements)
    return context


def _install(tmp_path, context):
    destination = tmp_path / "stage" / "site-packages"
    install_requirements(
        context / "requirements.txt",
        destination,
        context,
        "linux",
        "amd64",
        "3.11",
        "python:3.11-slim",
        BASE_CONFIG,
    )
    return {name for name, _ in staged_packages(destination)}


def _assert_secret_absent(root: Path, *texts: str) -> None:
    needles = [SECRET.encode(), base64.b64encode(f"{USER}:{SECRET}".encode())]
    for text in texts:
        assert SECRET not in text
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data[:2] == b"\x1f\x8b":
            data += gzip.decompress(data)
        for needle in needles:
            assert needle not in data, f"credential found in {path}"


def _set_netrc(home, monkeypatch):
    netrc = home / "netrc"
    netrc.write_text(f"machine 127.0.0.1 login {USER} password {SECRET}\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def _set_keyring(home, monkeypatch):
    script = home / "fake_keyring.py"
    script.write_text(
        "import sys\n"
        f"if sys.argv[1:2] == ['get'] and sys.argv[-1] == {USER!r}:\n"
        f"    print({SECRET!r})\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    bin_dir = home / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "keyring.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\r\n'
        )
    else:
        launcher = bin_dir / "keyring"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PIP_KEYRING_PROVIDER", "subprocess")


@pytest.mark.parametrize(
    "source", ["environment", "pip-config", "netrc", "keyring", "placeholder"]
)
def test_private_index_credentials_never_reach_image_sbom_or_logs(
    tmp_path, monkeypatch, capfd, home, index, source
):
    requirements = "fixture-dep==1.0\n"
    if source == "environment":
        monkeypatch.setenv("PIP_INDEX_URL", index.url_with(f"{USER}:{SECRET}"))
    elif source == "pip-config":
        config = home / "pip.ini"
        config.write_text(f"[global]\nindex-url = {index.url_with(f'{USER}:{SECRET}')}\n")
        monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    elif source == "netrc":
        _set_netrc(home, monkeypatch)
        monkeypatch.setenv("PIP_INDEX_URL", index.url)
    elif source == "keyring":
        _set_keyring(home, monkeypatch)
        monkeypatch.setenv("PIP_INDEX_URL", index.url_with(USER))
    else:
        monkeypatch.setenv("PRIVATE_INDEX_USER", USER)
        monkeypatch.setenv("PRIVATE_INDEX_TOKEN", SECRET)
        requirements = (
            "--index-url "
            + index.url_with("${PRIVATE_INDEX_USER}:${PRIVATE_INDEX_TOKEN}")
            + "\nfixture-dep==1.0\n"
        )
    context = _context(tmp_path, requirements)
    builder = _builder(
        tmp_path,
        monkeypatch,
        context=context,
        include_deps=True,
        generate_sbom=True,
    )

    builder.build()

    assert index.authorized >= 2
    sbom = json.loads((tmp_path / "output" / "sbom.json").read_text())
    assert any(
        package.get("name") == "fixture-dep" for package in sbom.get("packages", [])
    )
    captured = capfd.readouterr()
    _assert_secret_absent(tmp_path / "output", captured.out, captured.err)


def test_wrong_index_credentials_fail_with_redacted_actionable_error(
    tmp_path, monkeypatch, capfd, home, index
):
    wrong = "synthetic-wrong-token-147"
    monkeypatch.setenv("PIP_INDEX_URL", index.url_with(f"{USER}:{wrong}"))

    with pytest.raises(RuntimeError) as error:
        _install(tmp_path, _context(tmp_path))

    message = str(error.value)
    assert "Authentication to a package index failed" in message
    assert "PIP_KEYRING_PROVIDER" in message
    assert index.rejected
    captured = capfd.readouterr()
    for text in (message, captured.out, captured.err):
        assert wrong not in text


def test_missing_index_credentials_fail_with_authentication_hint(
    tmp_path, monkeypatch, home, index
):
    monkeypatch.setenv("PIP_INDEX_URL", index.url)

    with pytest.raises(RuntimeError, match="Authentication to a package index failed"):
        _install(tmp_path, _context(tmp_path))


def test_literal_requirement_credentials_are_rejected_before_pip(
    tmp_path, monkeypatch, home, index
):
    context = _context(
        tmp_path,
        f"--extra-index-url {index.url_with(f'{USER}:{SECRET}')}\nfixture-dep==1.0\n",
    )

    with pytest.raises(ValueError) as error:
        _install(tmp_path, context)

    message = str(error.value)
    assert "requirements.txt contains a URL with literal credentials at line 1" in message
    assert "http://127.0.0.1:" in message and "${VAR}" in message
    assert SECRET not in message and USER not in message
    assert index.authorized == index.rejected == 0


def test_literal_credentials_in_included_constraints_are_rejected(tmp_path):
    context = _context(tmp_path, "-c constraints/pins.txt\nfixture-dep==1.0\n")
    (context / "constraints").mkdir()
    (context / "constraints" / "pins.txt").write_text(
        f"# mirror https://token-{SECRET}@example.invalid/simple\n"
    )

    with pytest.raises(ValueError, match=r"constraints/pins\.txt .* line 1") as error:
        validate_requirements(context / "requirements.txt", context)

    assert SECRET not in str(error.value)


def test_shipped_credentials_are_rejected_without_dependency_staging(
    tmp_path, monkeypatch
):
    context = _context(
        tmp_path, f"--index-url https://{USER}:{SECRET}@example.invalid/simple\n"
    )
    builder = _builder(tmp_path, monkeypatch, context=context)

    with pytest.raises(ValueError, match="literal credentials") as error:
        builder.build()

    assert SECRET not in str(error.value)
    assert not (tmp_path / "output" / "blobs").exists() or not any(
        SECRET.encode() in path.read_bytes()
        for path in (tmp_path / "output").rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("name", [".netrc", "_netrc"])
def test_shipped_netrc_is_rejected(tmp_path, monkeypatch, name):
    context = _context(tmp_path)
    (context / name).write_text(f"machine example.invalid password {SECRET}\n")
    builder = _builder(tmp_path, monkeypatch, context=context, include_paths=[name])

    with pytest.raises(ValueError, match="credentials file"):
        builder.build()


def _win_only_context(tmp_path):
    context = _context(
        tmp_path, "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    _write_wheel(context / "wheelhouse", tag="py3-none-win_amd64")
    return context


def test_host_platform_override_would_change_raw_pip_selection(tmp_path, home):
    context = _win_only_context(tmp_path)
    environment = dict(os.environ, PIP_PLATFORM="win_amd64")

    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--target", str(tmp_path / "raw"),
            "--platform", "manylinux2014_x86_64", "--python-version", "3.11",
            "--implementation", "cp", "--only-binary=:all:",
            "-r", "requirements.txt",
        ],
        cwd=context,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source", ["environment", "pip-config"])
def test_host_platform_override_cannot_select_foreign_wheels(
    tmp_path, monkeypatch, capsys, home, source
):
    if source == "environment":
        monkeypatch.setenv("PIP_PLATFORM", "win_amd64")
    else:
        config = home / "pip.ini"
        config.write_text("[install]\nplatform = win_amd64\n")
        monkeypatch.setenv("PIP_CONFIG_FILE", str(config))

    with pytest.raises(RuntimeError, match="Only wheels compatible with linux/amd64"):
        _install(tmp_path, _win_only_context(tmp_path))
    if source == "environment":
        assert "Ignoring host pip settings" in capsys.readouterr().out


def test_host_pip_overrides_do_not_change_target_install(
    tmp_path, monkeypatch, capsys, home
):
    context = _context(
        tmp_path, "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    _write_wheel(context / "wheelhouse")
    overrides = {
        "PIP_NO_BINARY": ":all:",
        "PIP_USER": "1",
        "PIP_PREFIX": str(tmp_path / "prefix"),
        "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_IGNORE_REQUIRES_PYTHON": "1",
        "PIP_ABI": "cp99",
        "PIP_IMPLEMENTATION": "pp",
        "PIP_ROOT": str(tmp_path / "root"),
        "PIP_SRC": str(tmp_path / "src"),
        "_PIP_STANDALONE_CERT": str(tmp_path / "cert.pem"),
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    assert _install(tmp_path, context) == {"fixture-dep"}
    output = capsys.readouterr().out
    assert all(name in output for name in overrides)
    assert str(tmp_path / "prefix") not in output


def test_installed_direct_url_credentials_are_rejected(tmp_path):
    dist_info = tmp_path / "fixture_dep-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": f"https://{USER}:{SECRET}@example.invalid/pkg.whl"})
    )

    with pytest.raises(ValueError, match="records a URL with credentials") as error:
        reject_credentialed_direct_urls(tmp_path)

    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:token@example.invalid/simple", True),
        ("https://ghp_synthetictoken@example.invalid/simple", True),
        ("https://example.invalid/simple?access_token=abc123", True),
        ("https://user:${TOKEN}@example.invalid/simple", False),
        ("https://${USER}:${TOKEN}@example.invalid/simple", False),
        ("https://${TOKEN}@example.invalid/simple", False),
        ("git+ssh://git@github.com/org/repo.git", False),
        ("https://example.invalid/simple?page=2", False),
    ],
)
def test_url_credential_detection(url, expected):
    assert url_has_credentials(url) is expected


def test_redaction_removes_userinfo_query_tokens_and_known_values():
    text = (
        f"GET https://{USER}:{SECRET}@example.invalid/simple?token=abc123 "
        f"and raw {SECRET} and encoded synthetic%2Fs3cret"
    )

    redacted = redact(text, [SECRET, "synthetic/s3cret"])

    assert SECRET not in redacted and "abc123" not in redacted
    assert "synthetic%2Fs3cret" not in redacted
    assert "https://****@example.invalid/simple?token=****" in redacted


def test_proxy_authentication_failures_are_classified():
    message = _pip_failure_message(
        "ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: "
        "407 Proxy Authentication Required'))",
        "linux",
        "amd64",
        "3.11",
    )

    assert message.startswith("Proxy authentication failed")


class _ArtifactHost:
    """A separate origin that serves wheels without auth and records headers."""

    def __init__(self, wheel: Path):
        self.authorization = []
        self.requests = 0
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host.requests += 1
                if self.headers.get("Authorization") is not None:
                    host.authorization.append(self.headers["Authorization"])
                if self.path != f"/packages/{wheel.name}":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = wheel.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A different hostname as well as port, so host-scoped netrc cannot match.
        self.url = f"http://localhost:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("mode", ["linked", "redirected"])
@pytest.mark.parametrize("source", ["environment", "netrc", "placeholder"])
def test_index_credentials_are_not_forwarded_to_separate_artifact_host(
    tmp_path, monkeypatch, home, source, mode
):
    wheel = _write_wheel(tmp_path / "index-files")
    requirements = "fixture-dep==1.0\n"
    with _ArtifactHost(wheel) as artifacts, _Index(
        wheel, artifact_url=artifacts.url, redirect=mode == "redirected"
    ) as index:
        if source == "environment":
            monkeypatch.setenv("PIP_INDEX_URL", index.url_with(f"{USER}:{SECRET}"))
        elif source == "netrc":
            _set_netrc(home, monkeypatch)
            monkeypatch.setenv("PIP_INDEX_URL", index.url)
        else:
            monkeypatch.setenv("PRIVATE_INDEX_TOKEN", SECRET)
            requirements = (
                f"--index-url {index.url_with(USER + ':${PRIVATE_INDEX_TOKEN}')}\n"
                + requirements
            )

        assert _install(tmp_path, _context(tmp_path, requirements)) == {"fixture-dep"}

    assert index.authorized >= 1
    assert artifacts.requests >= 1
    assert artifacts.authorization == []


def test_cached_build_keeps_index_credentials_out_of_cache(
    tmp_path, monkeypatch, capfd, home, index
):
    monkeypatch.setenv("PIP_INDEX_URL", index.url_with(f"{USER}:{SECRET}"))
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )
    cache = tmp_path / "cache"
    context = _context(tmp_path)
    for _ in range(2):
        ImageBuilder(
            BuildConfig(
                context_dir=str(context),
                output_dir=str(tmp_path / "output"),
                cache_dir=str(cache),
                include_deps=True,
            )
        ).build()

    assert any(path.is_file() for path in cache.rglob("*"))
    captured = capfd.readouterr()
    _assert_secret_absent(tmp_path / "output", captured.out, captured.err)
    _assert_secret_absent(cache)


def test_virtualenv_direct_url_credentials_are_rejected(tmp_path, monkeypatch):
    context = tmp_path / "context"
    site_packages = context / ".venv" / "lib" / "python3.11" / "site-packages"
    dist_info = site_packages / "fixture_dep-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (context / ".venv" / "pyvenv.cfg").write_text("version = 3.11.8\n")
    (site_packages / "fixture_dep.py").write_text("")
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-dep\nVersion: 1.0\n\n"
    )
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": f"https://{USER}:{SECRET}@example.invalid/pkg.whl"})
    )
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    (tmp_path / "layers").mkdir()

    with pytest.raises(ValueError, match="records a URL with credentials") as error:
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

    assert SECRET not in str(error.value)


def test_context_local_pip_config_file_is_scanned(tmp_path, monkeypatch, home):
    context = _context(tmp_path)
    (context / "private.cfg").write_text(
        f"[global]\nindex-url = https://{USER}:{SECRET}@example.invalid/simple\n"
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", "private.cfg")
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_paths=["app.py", "private.cfg"]
    )

    with pytest.raises(ValueError, match=r"private\.cfg contains a URL with literal"):
        builder.build()

def test_context_local_netrc_file_is_rejected(tmp_path, monkeypatch, home):
    context = _context(tmp_path)
    (context / "creds.txt").write_text(
        f"machine example.invalid login {USER} password {SECRET}\n"
    )
    monkeypatch.setenv("NETRC", "creds.txt")
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_paths=["app.py", "creds.txt"]
    )

    with pytest.raises(ValueError, match=r"creds\.txt") as error:
        builder.build()

    assert SECRET not in str(error.value)