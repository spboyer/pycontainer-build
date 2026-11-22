"""Tests for registry client functionality."""
import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.message import Message

import pytest

from pycontainer.blob_store import BlobIntegrityError
from pycontainer.registry_client import (
    MAX_REQUEST_RETRIES,
    TRANSFER_CHUNK_SIZE,
    RegistryAuthenticationError,
    RegistryClient,
    RegistryRequestError,
    _retry_delay,
    parse_image_reference,
)


class _Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = io.BytesIO(body)
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        return None

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body.read(size)


def _http_error(url, status, headers=None, body=b""):
    response_headers = Message()
    for key, value in (headers or {}).items():
        response_headers[key] = value
    return urllib.error.HTTPError(
        url, status, "HTTP error", response_headers, io.BytesIO(body)
    )

def test_parse_image_reference():
    """Test parsing various image reference formats."""
    assert parse_image_reference("ghcr.io/user/app:v1")==("ghcr.io","user/app","v1")
    assert parse_image_reference("docker.io/library/python:3.11")==("docker.io","library/python","3.11")
    assert parse_image_reference("localhost:5000/test:latest")==("localhost:5000","test","latest")
    assert parse_image_reference("localhost:5000/team/app:3.11")==("localhost:5000","team/app","3.11")
    assert parse_image_reference("myapp:v2")==("docker.io","library/myapp","v2")
    assert parse_image_reference("user/app:tag")==("docker.io","user/app","tag")
    assert parse_image_reference("localhost:5000/test")==("localhost:5000","test","latest")
    assert parse_image_reference("alpine")==("docker.io","library/alpine","latest")
    digest = "sha256:" + "a" * 64
    assert parse_image_reference(f"alpine@{digest}") == (
        "docker.io",
        "library/alpine",
        digest,
    )
    assert parse_image_reference(f"registry.example/team/app:v1@{digest}") == (
        "registry.example",
        "team/app",
        digest,
    )


@pytest.mark.parametrize(
    "reference",
    [
        "registry.example/team/app@sha512:" + "a" * 128,
        "registry.example/team/app@",
        "registry.example/team/app:",
        "https://registry.example/team/app:latest",
        "registry.example/team/app?tag=latest",
        "registry.example/team/../app:latest",
    ],
)
def test_parse_image_reference_rejects_invalid_references(reference):
    with pytest.raises(ValueError):
        parse_image_reference(reference)


def test_pull_manifest_verifies_digest_and_size(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    body = json.dumps({"schemaVersion": 2, "mediaType": "application/test"}).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    requests = []

    def fake_request(method, url, headers=None):
        requests.append((method, url, headers))
        return 200, body, {"Docker-Content-Digest": digest}

    monkeypatch.setattr(client, "_make_request", fake_request)

    manifest, returned_digest = client.pull_manifest(
        digest, expected_digest=digest, expected_size=len(body)
    )
    assert manifest["schemaVersion"] == 2
    assert returned_digest == digest
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in (
        requests[0][2]["Accept"]
    )

    with pytest.raises(BlobIntegrityError, match="size"):
        client.pull_manifest(digest, expected_size=len(body) + 1)

    with pytest.raises(BlobIntegrityError, match="hashes"):
        client.pull_manifest("sha256:" + "b" * 64, expected_digest="sha256:" + "b" * 64)

def test_registry_client_construction():
    """Test RegistryClient initialization."""
    client=RegistryClient("ghcr.io","user/repo")
    assert client.registry=="ghcr.io"
    assert client.repository=="user/repo"
    assert client.base_url=="https://ghcr.io/v2"
    assert client.auth_token is None
    
    client_auth=RegistryClient("localhost:5000","test","token123")
    assert client_auth.auth_token=="token123"
    
    # Test docker.io translation to registry-1.docker.io
    client_dockerhub=RegistryClient("docker.io","library/python")
    assert client_dockerhub.registry=="registry-1.docker.io"
    assert client_dockerhub.base_url=="https://registry-1.docker.io/v2"

def test_registry_url_construction():
    """Test URL construction for registry operations."""
    client=RegistryClient("ghcr.io","user/app")
    
    blob_url=f"{client.base_url}/{client.repository}/blobs/sha256:abc123"
    assert blob_url=="https://ghcr.io/v2/user/app/blobs/sha256:abc123"
    
    manifest_url=f"{client.base_url}/{client.repository}/manifests/v1.0"
    assert manifest_url=="https://ghcr.io/v2/user/app/manifests/v1.0"
    
    upload_url=f"{client.base_url}/{client.repository}/blobs/uploads/"
    assert upload_url=="https://ghcr.io/v2/user/app/blobs/uploads/"


def test_basic_credentials_exchange_for_bearer_token(monkeypatch, caplog):
    client=RegistryClient(
        "registry.example",
        "team/app",
        username="registry-user",
        password="registry-password",
    )
    challenge=Message()
    challenge["Www-Authenticate"] = (
        'Bearer realm="https://auth.example/token",'
        'service="registry.example",scope="repository:team/app:pull"'
    )
    requests=[]

    class Response:
        status=200
        headers={}

        def __init__(self, body):
            self.body=body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size=-1):
            return self.body if size < 0 else self.body[:size]

    def fake_urlopen(request, timeout=None):
        authorization=request.get_header("Authorization")
        assert timeout == client.timeout
        requests.append((request.full_url, authorization))
        if request.full_url.startswith(client.base_url):
            if authorization.startswith("Basic "):
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    challenge,
                    io.BytesIO(b""),
                )
            assert authorization=="Bearer exchanged-token"
            return Response(b"registry response")

        assert request.full_url.startswith("https://auth.example/token?")
        assert authorization==(
            "Basic "
            + base64.b64encode(
                b"registry-user:registry-password"
            ).decode()
        )
        return Response(b'{"token":"exchanged-token"}')

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )

    status, body, _=client._make_request(
        "GET", f"{client.base_url}/{client.repository}/manifests/latest"
    )

    assert status==200
    assert body==b"registry response"
    assert [request[1] for request in requests]==[
        "Basic "
        + base64.b64encode(
            b"registry-user:registry-password"
        ).decode(),
        "Basic "
        + base64.b64encode(
            b"registry-user:registry-password"
        ).decode(),
        "Bearer exchanged-token",
    ]
    assert "registry-user" not in caplog.text
    assert "registry-password" not in caplog.text
    assert "exchanged-token" not in caplog.text


def test_bearer_exchange_failure_is_explicit_without_exposing_credentials(
    monkeypatch,
):
    client = RegistryClient(
        "registry.example",
        "team/app",
        username="private-user",
        password="private-password",
    )

    def fake_urlopen(request, timeout):
        if request.full_url.startswith(client.base_url):
            raise _http_error(
                request.full_url,
                401,
                {
                    "Www-Authenticate": (
                        'Bearer realm="https://auth.example/token",'
                        'service="registry.example"'
                    )
                },
            )
        raise _http_error(request.full_url, 403)

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )

    with pytest.raises(
        RegistryAuthenticationError, match="exchange failed with HTTP 403"
    ) as error:
        client._make_request(
            "GET", f"{client.base_url}/{client.repository}/manifests/latest"
        )

    assert "private-user" not in str(error.value)
    assert "private-password" not in str(error.value)


def test_bearer_exchange_retries_retry_after(monkeypatch):
    client = RegistryClient(
        "registry.example",
        "team/app",
        username="user",
        password="password",
    )
    token_attempts = []
    delays = []

    def fake_urlopen(request, timeout):
        if request.full_url.startswith(client.base_url):
            if request.get_header("Authorization").startswith("Basic "):
                raise _http_error(
                    request.full_url,
                    401,
                    {
                        "Www-Authenticate": (
                            'Bearer realm="https://auth.example/token"'
                        )
                    },
                )
            return _Response(b"registry response")

        token_attempts.append(request)
        if len(token_attempts) == 1:
            raise _http_error(
                request.full_url, 503, {"Retry-After": "0"}
            )
        return _Response(b'{"token":"access-token"}')

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.time.sleep", delays.append
    )

    status, body, _ = client._make_request(
        "GET", f"{client.base_url}/{client.repository}/manifests/latest"
    )

    assert (status, body) == (200, b"registry response")
    assert len(token_attempts) == 2
    assert delays == [0]


def test_pull_blob_streams_and_atomically_publishes_verified_content(
    tmp_path, monkeypatch
):
    content = b"blob-data" * (TRANSFER_CHUNK_SIZE // 9 + 3)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    response = _Response(
        content, headers={"Content-Length": str(len(content))}
    )
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return response

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *_handlers: Opener(),
    )
    destination = tmp_path / "blobs" / digest.split(":", 1)[1]
    client = RegistryClient("registry.example", "team/app", auth_token="secret")

    assert client.pull_blob(digest, destination, len(content))
    assert destination.read_bytes() == content
    assert requests[0][0].get_header("Authorization") == "Bearer secret"
    assert requests[0][1] == client.timeout
    assert all(0 < size <= TRANSFER_CHUNK_SIZE for size in response.read_sizes)
    assert not list(destination.parent.glob(".*.tmp"))
    assert not client.pull_blob(digest, destination, len(content))


def test_pull_blob_reuses_one_opener_per_client(tmp_path, monkeypatch):
    contents = [b"first blob", b"second blob"]
    openers = []
    requests = []

    class Opener:
        def open(self, request, timeout):
            content = contents[len(requests)]
            requests.append(request)
            return _Response(content, headers={"Content-Length": str(len(content))})

    def build_opener(*_handlers):
        openers.append(Opener())
        return openers[-1]

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener", build_opener
    )
    client = RegistryClient("registry.example", "team/app")

    for content in contents:
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        assert client.pull_blob(digest, tmp_path / digest.split(":", 1)[1], len(content))

    assert len(requests) == 2
    assert len(openers) == 1


def test_pull_blob_rejects_corrupt_existing_blob_without_overwriting(
    tmp_path, monkeypatch
):
    destination = tmp_path / "blob"
    destination.write_bytes(b"corrupt")
    client = RegistryClient("registry.example", "team/app")

    def unexpected_download(*args, **kwargs):
        raise AssertionError("existing corrupt blob must not be overwritten")

    monkeypatch.setattr(client, "_download_blob_once", unexpected_download)
    correct = b"correct"
    digest = f"sha256:{hashlib.sha256(correct).hexdigest()}"
    with pytest.raises(BlobIntegrityError, match="refusing to overwrite"):
        client.pull_blob(digest, destination, len(correct))

    assert destination.read_bytes() == b"corrupt"


def test_pull_blob_rejects_unsafe_digest_before_creating_path(tmp_path):
    client = RegistryClient("registry.example", "team/app")
    destination = tmp_path / "blobs" / "placeholder"

    with pytest.raises(ValueError, match="lowercase sha256"):
        client.pull_blob("sha256:../../outside", destination, 1)

    assert not destination.parent.exists()


@pytest.mark.parametrize(
    ("body", "expected_content", "expected_size"),
    [
        (b"corrupt", b"expected", len(b"corrupt")),
        (b"short", b"complete", len(b"complete")),
    ],
)
def test_pull_blob_rejects_digest_or_size_mismatch_without_publishing(
    tmp_path, monkeypatch, body, expected_content, expected_size
):
    response = _Response(
        body, headers={"Content-Length": str(expected_size)}
    )

    class Opener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *_handlers: Opener(),
    )
    destination = tmp_path / "blob"
    client = RegistryClient("registry.example", "team/app")
    digest = f"sha256:{hashlib.sha256(expected_content).hexdigest()}"

    with pytest.raises(BlobIntegrityError):
        client.pull_blob(digest, destination, expected_size)

    assert not destination.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_pull_blob_cleans_only_owned_temp_after_interrupted_download(
    tmp_path, monkeypatch
):
    class InterruptedResponse(_Response):
        def read(self, size=-1):
            if not self.read_sizes:
                self.read_sizes.append(size)
                return b"partial"
            raise ConnectionResetError("connection interrupted")

    class Opener:
        def open(self, request, timeout):
            return InterruptedResponse()

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *_handlers: Opener(),
    )
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _delay: None)
    destination = tmp_path / "blob"
    sentinel = tmp_path / ".blob-another-writer.tmp"
    sentinel.write_bytes(b"other writer")
    client = RegistryClient("registry.example", "team/app")
    content = b"complete"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    with pytest.raises(RegistryRequestError, match="failed after 3 attempt"):
        client.pull_blob(digest, destination, len(content))

    assert not destination.exists()
    assert list(tmp_path.glob(".*.tmp")) == [sentinel]


def test_pull_blob_retries_retry_after_and_preserves_no_auth_redirect(
    tmp_path, monkeypatch
):
    content = b"redirected blob"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    registry_attempts = []
    redirected_requests = []
    delays = []

    class Opener:
        def open(self, request, timeout):
            registry_attempts.append((request, timeout))
            if len(registry_attempts) == 1:
                raise _http_error(
                    request.full_url,
                    429,
                    {"Retry-After": "0"},
                )
            raise _http_error(
                request.full_url,
                302,
                {"Location": "https://storage.example/blob?signature=signed"},
            )

    def fake_urlopen(request, timeout):
        redirected_requests.append((request, timeout))
        return _Response(
            content, headers={"Content-Length": str(len(content))}
        )

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.build_opener",
        lambda *_handlers: Opener(),
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.time.sleep", delays.append
    )
    destination = tmp_path / "blob"
    client = RegistryClient("registry.example", "team/app", auth_token="registry-secret")

    assert client.pull_blob(digest, destination, len(content))
    assert destination.read_bytes() == content
    assert len(registry_attempts) == 2
    assert all(timeout == client.timeout for _, timeout in registry_attempts)
    assert registry_attempts[0][0].get_header("Authorization") == "Bearer registry-secret"
    assert redirected_requests[0][0].get_header("Authorization") is None
    assert redirected_requests[0][1] == client.timeout
    assert delays == [0]


@pytest.mark.parametrize(
    ("location", "expected_query"),
    [
        ("https://registry.example/upload/session", {}),
        (
            "https://registry.example/upload/session?state=abc",
            {"state": "abc"},
        ),
    ],
)
def test_upload_streams_and_appends_digest_to_location_query(
    tmp_path, monkeypatch, location, expected_query
):
    content = b"upload-data" * (TRANSFER_CHUNK_SIZE // 11 + 1)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    source = tmp_path / "source.blob"
    source.write_bytes(content)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        uploaded = bytearray()
        while True:
            chunk = request.data.read(64 * 1024)
            if not chunk:
                break
            uploaded.extend(chunk)
            assert len(chunk) <= 64 * 1024
        assert bytes(uploaded) == content
        return _Response(status=201)

    client = RegistryClient("registry.example", "team/app", auth_token="token")
    monkeypatch.setattr(client, "initiate_blob_upload", lambda: location)
    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )

    assert client.upload_blob_monolithic(digest, source)
    request, timeout = requests[0]
    assert request.get_method() == "PUT"
    assert request.get_header("Authorization") == "Bearer token"
    assert request.get_header("Content-length") == str(len(content))
    assert timeout == client.timeout
    parsed = urllib.parse.urlsplit(request.full_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query.pop("digest") == [digest]
    assert query == {key: [value] for key, value in expected_query.items()}


def test_upload_does_not_forward_registry_auth_to_external_location(
    tmp_path, monkeypatch
):
    content = b"upload"
    source = tmp_path / "source.blob"
    source.write_bytes(content)
    client = RegistryClient("registry.example", "team/app", auth_token="secret")
    monkeypatch.setattr(
        client,
        "initiate_blob_upload",
        lambda: "https://storage.example/upload?signature=signed",
    )
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        request.data.read()
        return _Response(status=201)

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )

    client.upload_blob_monolithic(
        f"sha256:{hashlib.sha256(content).hexdigest()}", source
    )

    assert requests[0].get_header("Authorization") is None


def test_upload_rejects_local_blob_digest_mismatch_before_initiating_upload(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.blob"
    source.write_bytes(b"incorrect")
    client = RegistryClient("registry.example", "team/app")
    monkeypatch.setattr(
        client,
        "initiate_blob_upload",
        lambda: pytest.fail("invalid local blob must not start an upload"),
    )

    with pytest.raises(BlobIntegrityError, match="not requested digest"):
        client.upload_blob_monolithic("sha256:" + "0" * 64, source)


def test_retry_after_date_is_bounded():
    from email.utils import format_datetime

    future = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True
    )
    assert _retry_delay({"Retry-After": future}, 0) == 5
    assert _retry_delay({"Retry-After": "0"}, 1) == 0


def test_safe_control_requests_retry_with_timeout_and_clear_error(
    monkeypatch,
):
    requests = []
    delays = []
    client = RegistryClient("registry.example", "team/app", timeout=2.5)

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        raise TimeoutError("not included in public error")

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setattr(
        "pycontainer.registry_client.time.sleep", delays.append
    )

    with pytest.raises(RegistryRequestError, match="GET request.*TimeoutError"):
        client._make_request("GET", "https://registry.example/v2/team/app/test")

    assert len(requests) == MAX_REQUEST_RETRIES + 1
    assert all(timeout == 2.5 for _, timeout in requests)
    assert len(delays) == MAX_REQUEST_RETRIES


if __name__=="__main__":
    test_parse_image_reference()
    test_registry_client_construction()
    test_registry_url_construction()
    print("✅ All registry client tests passed")
