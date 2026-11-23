"""Fake-registry regressions for registry delete and prune (no real registry)."""
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import hashlib
from email.message import Message

import pytest

from pycontainer import cli
from pycontainer.registry_cleanup import classify_tags, semver_key
from pycontainer.registry_client import RegistryClient

HOST = "registry.example"
REPO = "team/app"
USER = "synthetic-user"
PASSWORD = "synthetic-password"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"


class _Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size=-1):
        return self._body.read(size)


def _error(url, status, headers=None, body=b""):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, status, "error", message, io.BytesIO(body))


def _errors(code, message):
    return json.dumps({"errors": [{"code": code, "message": message}]}).encode()


def _patch_transport(monkeypatch, handler):
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.OpenerDirector.open",
        lambda _opener, request, timeout=None: handler(request, timeout),
    )


class FakeRegistry:
    """In-memory Registry v2 subset: manifests, tags/list pagination, DELETE."""

    def __init__(self, page_size=2, require_token=False, host=HOST):
        self.host = host
        self.manifests = {}
        self.media_types = {}
        self.tags = {}
        self.page_size = page_size
        self.require_token = require_token
        self.requests = []
        self.faults = []
        self.link_override = None
        self.digest_header = None

    def add_manifest(self, name, media_type=MANIFEST, children=()):
        if media_type == INDEX:
            document = {
                "schemaVersion": 2,
                "mediaType": INDEX,
                "manifests": [
                    {"mediaType": MANIFEST, "digest": child, "size": len(self.manifests[child])}
                    for child in children
                ],
            }
        else:
            document = {
                "schemaVersion": 2,
                "mediaType": MANIFEST,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": "sha256:" + "0" * 64, "size": 2},
                "layers": [],
                "annotations": {"name": name},
            }
        body = json.dumps(document).encode()
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        self.manifests[digest] = body
        self.media_types[digest] = media_type
        return digest

    def tag(self, digest, *tags):
        for tag in tags:
            self.tags[tag] = digest

    def fail(self, method, path_fragment, status, body=b"", headers=None, times=None):
        self.faults.append([method, path_fragment, status, body, headers or {}, times])

    def methods(self, method):
        return [url for m, url, _ in self.requests if m == method]

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        auth = request.get_header("Authorization")
        self.requests.append((method, url, auth))
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc == "auth.example":
            scope = dict(urllib.parse.parse_qsl(parsed.query)).get("scope", "")
            action = scope.rsplit(":", 1)[-1]
            return _Response(json.dumps({"token": f"token-{action}"}).encode())
        assert parsed.netloc == self.host

        for fault in self.faults:
            f_method, fragment, status, body, headers, times = fault
            if f_method == method and fragment in url and times != 0:
                if times is not None:
                    fault[5] -= 1
                if isinstance(status, Exception):
                    raise status
                raise _error(url, status, headers, body)

        if self.require_token:
            action = "delete" if method == "DELETE" else "pull"
            if auth != f"Bearer token-{action}":
                raise _error(url, 401, {
                    "Www-Authenticate": (
                        'Bearer realm="https://auth.example/token",'
                        f'service="{self.host}",scope="repository:{REPO}:{action}"'
                    )
                })

        prefix = f"/v2/{REPO}/"
        path = parsed.path[len(prefix):]
        if path == "tags/list":
            return self._tags_page(url, parsed)
        assert path.startswith("manifests/")
        reference = path[len("manifests/"):]
        if method == "DELETE":
            if reference not in self.manifests:
                raise _error(url, 404, body=_errors("MANIFEST_UNKNOWN", "unknown"))
            del self.manifests[reference]
            self.tags = {t: d for t, d in self.tags.items() if d != reference}
            return _Response(status=202)
        digest = reference if reference.startswith("sha256:") else self.tags.get(reference)
        if digest not in self.manifests:
            raise _error(url, 404, body=_errors("MANIFEST_UNKNOWN", "manifest unknown"))
        return _Response(self.manifests[digest], headers={
            "Docker-Content-Digest": self.digest_header or digest,
            "Content-Type": self.media_types[digest],
        })

    def _tags_page(self, url, parsed):
        query = dict(urllib.parse.parse_qsl(parsed.query))
        size = min(int(query.get("n", self.page_size)), self.page_size)
        ordered = sorted(self.tags)
        start = ordered.index(query["last"]) + 1 if "last" in query else 0
        page = ordered[start:start + size]
        headers = {}
        if self.link_override is not None:
            headers["Link"] = self.link_override
        elif start + size < len(ordered):
            next_query = urllib.parse.urlencode({"n": size, "last": page[-1]})
            headers["Link"] = f'</v2/{REPO}/tags/list?{next_query}>; rel="next"'
        return _Response(json.dumps({"name": REPO, "tags": page}).encode(), headers=headers)


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    _patch_transport(monkeypatch, fake)
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _delay: None)
    return fake


def _run(monkeypatch, *argv, tty=False, answer=None, credentials=True):
    stdin = io.StringIO()
    stdin.isatty = lambda: tty
    monkeypatch.setattr(sys, "stdin", stdin)
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    auth_args = ("--username", USER, "--password", PASSWORD) if credentials else ()
    monkeypatch.setattr(sys, "argv", ["pycontainer", "registry", *argv, *auth_args])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


# --- delete -----------------------------------------------------------------

def test_delete_resolves_tag_then_deletes_digest_once(monkeypatch, registry, capsys):
    digest = registry.add_manifest("one")
    registry.tag(digest, "v1.0.0", "stable")

    code = _run(monkeypatch, "delete", f"{HOST}/{REPO}:stable", "--yes")

    out = capsys.readouterr().out
    assert code == 0
    assert registry.methods("DELETE") == [f"https://{HOST}/v2/{REPO}/manifests/{digest}"]
    assert "removes every tag" in out and digest in out
    assert registry.tags == {}


def test_delete_by_digest_verifies_and_deletes(monkeypatch, registry):
    digest = registry.add_manifest("one")
    registry.tag(digest, "v1.0.0")

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}@{digest}", "--yes") == 0
    assert len(registry.methods("DELETE")) == 1


def test_delete_dry_run_never_sends_delete(monkeypatch, registry, capsys):
    digest = registry.add_manifest("one")
    registry.tag(digest, "v1.0.0")

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--dry-run") == 0
    assert registry.methods("DELETE") == []
    assert "Dry run: would delete" in capsys.readouterr().out


def test_delete_without_tty_or_yes_fails_explicitly(monkeypatch, registry, capsys):
    registry.tag(registry.add_manifest("one"), "v1.0.0")

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0") == 1
    assert "rerun with --yes" in capsys.readouterr().err
    assert registry.methods("DELETE") == []


@pytest.mark.parametrize("answer, code, deletes", [("yes", 0, 1), ("n", 1, 0), ("", 1, 0)])
def test_delete_interactive_confirmation(monkeypatch, registry, answer, code, deletes):
    registry.tag(registry.add_manifest("one"), "v1.0.0")

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", tty=True, answer=answer) == code
    assert len(registry.methods("DELETE")) == deletes


def test_delete_requires_explicit_tag_or_digest(monkeypatch, registry, capsys):
    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}", "--yes") == 2
    assert "explicit tag" in capsys.readouterr().err
    assert registry.requests == []


@pytest.mark.parametrize("status, body", [
    (405, b""),
    (400, _errors("UNSUPPORTED", "The operation is unsupported.")),
])
def test_unsupported_deletion_is_explicit_and_not_retried(monkeypatch, registry, capsys, status, body):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", status, body)

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    assert "does not allow manifest deletion" in capsys.readouterr().err
    assert len(registry.methods("DELETE")) == 1


def test_denied_deletion_reports_code_and_delete_permission(monkeypatch, registry, capsys):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", 403, _errors("DENIED", "requested access is denied"))

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    err = capsys.readouterr().err
    assert "DENIED" in err
    assert "delete permission" in err
    assert PASSWORD not in err
    assert len(registry.methods("DELETE")) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_delete_statuses_are_not_retried(monkeypatch, registry, capsys, status):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", status, headers={"Retry-After": "1"})

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    assert len(registry.methods("DELETE")) == 1


def test_delete_transport_error_reports_unknown_outcome(monkeypatch, registry, capsys):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", TimeoutError("timed out"))

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    assert "deletion outcome" in capsys.readouterr().err
    assert len(registry.methods("DELETE")) == 1


def test_delete_reexchanges_token_for_delete_scope(monkeypatch, registry):
    registry.require_token = True
    registry.tag(registry.add_manifest("one"), "v1.0.0")

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 0
    deletes = [(url, auth) for m, url, auth in registry.requests if m == "DELETE"]
    assert [auth for _, auth in deletes][-1] == "Bearer token-delete"
    assert len(deletes) == 2  # rejected by 401 before any effect, then authorized once
    assert registry.tags == {}


def test_authorization_is_not_copied_to_redirects(monkeypatch):
    client = RegistryClient(HOST, REPO, auth_token="synthetic-token")
    seen = []

    def capture(request, timeout=None):
        seen.append(request)
        return _Response(b"{}")

    _patch_transport(monkeypatch, capture)
    client._make_request("GET", f"{client.base_url}/{REPO}/tags/list")

    request = seen[0]
    assert request.get_header("Authorization") == "Bearer synthetic-token"
    # urllib copies only ordinary headers to a followed redirect.
    assert "Authorization" not in request.headers


def test_token_exchange_credentials_are_not_copied_to_redirects(monkeypatch, registry):
    registry.require_token = True
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    token_requests = []
    original = registry.__call__

    def capture(request, timeout=None):
        if urllib.parse.urlsplit(request.full_url).netloc == "auth.example":
            token_requests.append(request)
        return original(request, timeout)

    _patch_transport(monkeypatch, capture)
    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--dry-run") == 0
    assert token_requests
    for request in token_requests:
        assert request.get_header("Authorization", "").startswith("Basic ")
        assert "Authorization" not in request.headers


def test_repeated_delete_challenge_exchanges_once_then_fails(monkeypatch, registry, capsys):
    registry.require_token = True
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", 401, headers={"Www-Authenticate": (
        f'Bearer realm="https://auth.example/token",service="{HOST}",'
        f'scope="repository:{REPO}:delete"')})

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    assert len(registry.methods("DELETE")) == 2
    exchanges = [url for m, url, _ in registry.requests
                 if "auth.example" in url and url.endswith("%3Adelete")]
    assert len(exchanges) == 1
    assert PASSWORD not in capsys.readouterr().err


def test_delete_redirect_is_not_followed(monkeypatch, registry, capsys):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.fail("DELETE", "/manifests/", 307, headers={"Location": "https://other.example/x"})

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") == 1
    assert len(registry.methods("DELETE")) == 1
    assert not any("other.example" in url for _, url, _ in registry.requests)


# --- prune ------------------------------------------------------------------

def _repo_tags(registry, *tags):
    digests = {}
    for tag in tags:
        digests[tag] = registry.add_manifest(tag)
        registry.tag(digests[tag], tag)
    return digests


@pytest.mark.parametrize("command", ["delete", "prune"])
@pytest.mark.parametrize("host, expected_auth, uses_azure_cli", [
    ("ghcr.io", "Bearer synthetic-github-token", False),
    ("ghcr.io.example.com", None, False),
    ("myregistry.azurecr.io", "Bearer synthetic-acr-token", True),
    ("myregistry.azurecr.io.example.com", None, False),
])
def test_cleanup_commands_scope_provider_credentials(
    monkeypatch, command, host, expected_auth, uses_azure_cli
):
    registry = FakeRegistry(host=host)
    _patch_transport(monkeypatch, registry)
    monkeypatch.setattr(
        "pycontainer.auth.DockerConfigAuthProvider._load_config", lambda self: None
    )
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
    for name in ("REGISTRY_TOKEN", "REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    azure_calls = []

    def fake_azure_cli(args, **kwargs):
        azure_calls.append(args)
        return subprocess.CompletedProcess(
            args, 0,
            stdout=json.dumps({
                "loginServer": host,
                "accessToken": "synthetic-acr-token",
            }),
            stderr="",
        )

    monkeypatch.setattr("pycontainer.auth.subprocess.run", fake_azure_cli)
    digests = _repo_tags(registry, "v1.0.0", "v2.0.0")
    if command == "delete":
        args = ("delete", f"{host}/{REPO}:v1.0.0", "--yes")
    else:
        args = ("prune", f"{host}/{REPO}", "--keep", "1", "--execute", "--yes")

    assert _run(monkeypatch, *args, credentials=False) == 0
    assert len(registry.methods("DELETE")) == 1
    assert registry.tags == {"v2.0.0": digests["v2.0.0"]}
    assert all(auth == expected_auth for _, _, auth in registry.requests)
    assert bool(azure_calls) == uses_azure_cli


def test_semver_precedence_and_rejections():
    ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
               "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.9.0", "1.10.0"]
    keys = [semver_key(tag) for tag in ordered]
    assert keys == sorted(keys)
    assert semver_key("v1.10.0") == semver_key("1.10.0")
    for tag in ("latest", "1.0", "01.0.0", "1.0.0-01", "1.0.0.1", "v1", "sha-abc"):
        assert semver_key(tag) is None


def test_classify_keeps_ties_at_boundary_and_never_orders_non_semver():
    decisions = {d.tag: d.action for d in classify_tags(
        ["v1.0.0", "1.0.0", "v0.9.0", "latest", "nightly"], keep=1)}
    assert decisions == {"v1.0.0": "keep", "1.0.0": "keep", "v0.9.0": "delete",
                         "latest": "skip", "nightly": "skip"}


def test_prune_dry_run_is_default_and_paginates(monkeypatch, registry, capsys):
    _repo_tags(registry, "v1.2.0", "v1.9.0", "v1.10.0", "v2.0.0-rc.1", "v2.0.0", "latest")

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "2") == 0

    out = capsys.readouterr().out
    assert registry.methods("DELETE") == []
    assert len(registry.methods("GET")) >= 3
    pages = [url for url in registry.methods("GET") if "tags/list" in url]
    assert len(pages) == 3
    lines = {line.split()[1]: line.split()[0] for line in out.splitlines()[1:-1]}
    assert lines == {"v2.0.0": "keep", "v2.0.0-rc.1": "keep", "v1.10.0": "delete",
                     "v1.9.0": "delete", "v1.2.0": "delete", "latest": "skip"}
    assert "dry run" in out


def test_prune_never_deletes_digest_shared_with_any_retained_tag(monkeypatch, registry, capsys):
    digests = _repo_tags(registry, "v3.0.0", "v2.0.0", "v1.0.0")
    registry.tag(digests["v3.0.0"], "v0.1.0")      # alias of kept
    shared_protect = registry.add_manifest("protected")
    registry.tag(shared_protect, "v0.2.0", "v0.2.0-hotfix")   # protected alias
    shared_skip = registry.add_manifest("skip")
    registry.tag(shared_skip, "v0.3.0", "latest")            # non-semver alias
    shared_unmatched = registry.add_manifest("unmatched")
    registry.tag(shared_unmatched, "v0.4.0", "release-0.4")  # unmatched alias
    child = registry.add_manifest("arm64")
    registry.tag(child, "v0.5.0")
    registry.tag(registry.add_manifest("index", INDEX, [child]), "v9.0.0")  # kept index
    double = registry.add_manifest("double")
    registry.tag(double, "v0.6.0", "v0.6.1")                  # two candidates, one digest

    code = _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "3", "--match", "v*",
                "--protect", "*-hotfix", "--execute", "--yes")

    out = capsys.readouterr().out
    assert code == 0
    deleted = sorted(url.rsplit("/", 1)[1] for url in registry.methods("DELETE"))
    assert deleted == sorted([digests["v1.0.0"], double])
    for kept in (digests["v3.0.0"], shared_protect, shared_skip, shared_unmatched, child):
        assert kept in registry.manifests
    assert "retain  v0.1.0" in out and "retain  v0.5.0" in out
    assert set(registry.tags) >= {"v3.0.0", "v0.1.0", "v0.2.0", "latest", "release-0.4", "v0.5.0"}


def test_prune_execute_requires_confirmation(monkeypatch, registry, capsys):
    _repo_tags(registry, "v2.0.0", "v1.0.0")

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--execute") == 1
    assert "rerun with --yes" in capsys.readouterr().err
    assert registry.methods("DELETE") == []


@pytest.mark.parametrize("fault", [
    (503, b"", {}),
    (200, b"not-json", {}),
])
def test_interrupted_listing_deletes_nothing(monkeypatch, registry, capsys, fault):
    _repo_tags(registry, "v1.0.0", "v2.0.0", "v3.0.0", "v4.0.0")
    status, body, headers = fault
    if status == 200:
        original = registry._tags_page

        def broken(url, parsed):
            if "last=" in url:
                return _Response(body)
            return original(url, parsed)
        registry._tags_page = broken
    else:
        registry.fail("GET", "last=", status, body, headers)

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--execute", "--yes") == 1
    assert registry.methods("DELETE") == []
    assert not [u for u in registry.methods("GET") if "/manifests/" in u]


@pytest.mark.parametrize("link", [
    '<https://evil.example/v2/team/app/tags/list?last=a>; rel="next"',
    '</v2/team/other/tags/list?last=a>; rel="next"',
    f'</v2/{REPO}/tags/list?n=2>; rel="next"',
    f'</v2/{REPO}/tags/list?last=a#frag>; rel="next"',
])
def test_unsafe_or_looping_pagination_is_rejected(monkeypatch, registry, capsys, link):
    _repo_tags(registry, "v1.0.0", "v2.0.0")
    registry.link_override = link

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--page-size", "2",
                "--execute", "--yes") == 1
    assert registry.methods("DELETE") == []
    assert all(urllib.parse.urlsplit(url).netloc != "evil.example" for _, url, _ in registry.requests)


def test_listing_rate_limit_keeps_diagnostics(monkeypatch, registry, capsys):
    _repo_tags(registry, "v1.0.0", "v2.0.0", "v3.0.0")
    registry.fail("GET", "tags/list", 429, _errors("TOOMANYREQUESTS", "slow down"),
                  {"Retry-After": "120"})

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1") == 1
    err = capsys.readouterr().err
    assert "HTTP 429" in err and "resets in about 120 seconds" in err
    assert "response details omitted" in err


def test_unresolvable_tag_aborts_before_any_delete(monkeypatch, registry, capsys):
    _repo_tags(registry, "v1.0.0", "v2.0.0", "latest")
    registry.fail("GET", "/manifests/latest", 404, _errors("MANIFEST_UNKNOWN", "gone"))

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--execute", "--yes") == 1
    assert registry.methods("DELETE") == []


def test_failed_delete_stops_remaining_deletions(monkeypatch, registry, capsys):
    _repo_tags(registry, "v4.0.0", "v3.0.0", "v2.0.0", "v1.0.0")
    registry.fail("DELETE", "/manifests/", 500, times=1)

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--execute", "--yes") == 1
    assert len(registry.methods("DELETE")) == 1
    assert "Deleted 0 of 3" in capsys.readouterr().err


UNSUPPORTED_DIGEST = "blake3:" + "0" * 64


def test_unsupported_digest_header_is_never_deleted_by_tag(monkeypatch, registry, capsys):
    registry.tag(registry.add_manifest("one"), "v1.0.0")
    registry.digest_header = UNSUPPORTED_DIGEST

    assert _run(monkeypatch, "delete", f"{HOST}/{REPO}:v1.0.0", "--yes") != 0
    assert registry.methods("DELETE") == []


def test_unsupported_digest_header_aborts_prune_before_any_delete(monkeypatch, registry, capsys):
    _repo_tags(registry, "v3.0.0", "v2.0.0", "v1.0.0")
    registry.digest_header = UNSUPPORTED_DIGEST

    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "1", "--execute", "--yes") == 1
    assert registry.methods("DELETE") == []


def test_prune_rejects_tagged_repository_and_invalid_keep(monkeypatch, registry, capsys):
    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}:v1", "--keep", "1") == 2
    assert _run(monkeypatch, "prune", f"{HOST}/{REPO}", "--keep", "0") == 2
    assert registry.requests == []
