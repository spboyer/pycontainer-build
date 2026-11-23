"""Tests for registry client functionality."""
import base64
import hashlib
import io
import json
import logging
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
    RegistryQuotaError,
    RegistryRateLimitError,
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


@pytest.mark.parametrize("blob", [False, True])
def test_auth_replay_failure_does_not_expose_challenge_context(monkeypatch, blob):
    client = RegistryClient("registry.example", "team/app")
    secret = "synthetic-challenge-secret"
    attempts = 0

    def open_request(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            headers = Message()
            headers["Www-Authenticate"] = 'Bearer realm="https://auth.example/token"'
            raise urllib.error.HTTPError(
                request.full_url, 401, secret, headers, io.BytesIO()
            )
        raise ValueError("replay failed")

    monkeypatch.setattr(client, "_get_bearer_token", lambda params: "token")
    monkeypatch.setattr(client._blob_opener if blob else client._opener, "open", open_request)
    with pytest.raises(ValueError) as error:
        if blob:
            client._open_blob_response(f"{client.base_url}/blobs/test")
        else:
            client._make_request("GET", f"{client.base_url}/test")
    assert secret not in "".join(traceback.format_exception(error.value))


def test_upload_auth_rewind_failure_does_not_expose_challenge_context(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    secret = "synthetic-challenge-secret"

    class BrokenRewind(io.BytesIO):
        seeks = 0

        def seek(self, offset, whence=0):
            self.seeks += 1
            if self.seeks > 1:
                raise ValueError("rewind failed")
            return super().seek(offset, whence)

    def reject(request, timeout):
        headers = Message()
        headers["Www-Authenticate"] = 'Bearer realm="https://auth.example/token"'
        raise urllib.error.HTTPError(request.full_url, 401, secret, headers, io.BytesIO())

    monkeypatch.setattr(client, "_get_bearer_token", lambda params: "token")
    monkeypatch.setattr(client._opener, "open", reject)
    with pytest.raises(ValueError) as error:
        client._make_request("PUT", f"{client.base_url}/upload", data=BrokenRewind(b"x"))
    assert secret not in "".join(traceback.format_exception(error.value))


def test_concurrent_initial_challenges_reuse_acquired_token(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    initial_requests = threading.Barrier(2)
    replay_completed = threading.Event()
    acquisitions = []

    def acquire(_params):
        acquisitions.append(True)
        return "shared-token"

    def open_request(request, timeout):
        if request.get_header("Authorization") == "Bearer shared-token":
            replay_completed.set()
            return _Response(b"ok")
        if initial_requests.wait(timeout=10) == 1:
            assert replay_completed.wait(timeout=10)
        raise _http_error(
            request.full_url, 401,
            {"Www-Authenticate": 'Bearer realm="https://auth.example/token"'},
        )

    monkeypatch.setattr(client, "_get_bearer_token", acquire)
    monkeypatch.setattr(client._opener, "open", open_request)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(client._make_request, "GET", f"{client.base_url}/test")
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]
    assert [result[0] for result in results] == [200, 200]
    assert len(acquisitions) == 1


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("existing_token", [None, "rejected-token"])
@pytest.mark.parametrize("auth_token", [None, "rejected-token"])
def test_rejected_bearer_token_does_not_loop_or_refresh(
    monkeypatch, method, existing_token, auth_token
):
    client = RegistryClient("registry.example", "team/app", auth_token=auth_token)
    client._bearer_token = existing_token
    requests = []
    acquisitions = []

    def acquire(_params):
        acquisitions.append(True)
        return "rejected-token"

    def reject(request, timeout):
        requests.append(request)
        raise _http_error(
            request.full_url, 401,
            {"Www-Authenticate": 'Bearer realm="https://auth.example/token"'},
        )

    monkeypatch.setattr(client, "_get_bearer_token", acquire)
    monkeypatch.setattr(client._opener, "open", reject)
    assert client._make_request(method, f"{client.base_url}/test")[0] == 401
    assert len(requests) == (1 if existing_token else 2)
    assert len(acquisitions) == (0 if existing_token else 1)


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

    def fake_request(method, url, headers=None, max_response_bytes=None):
        requests.append((method, url, headers, max_response_bytes))
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
    assert requests[0][3] == len(body)

    with pytest.raises(BlobIntegrityError, match="size"):
        client.pull_manifest(digest, expected_size=len(body) + 1)

    with pytest.raises(BlobIntegrityError, match="hashes"):
        client.pull_manifest("sha256:" + "b" * 64, expected_digest="sha256:" + "b" * 64)


@pytest.mark.parametrize("mismatch", [None, "digest", "size", "media_type", "missing"])
def test_head_manifest_verifies_signer_visible_descriptor(monkeypatch, mismatch):
    client = RegistryClient("registry.example", "team/app")
    digest = "sha256:" + "a" * 64
    media_type = "application/vnd.oci.image.manifest.v1+json"
    headers = {
        "Docker-Content-Digest": digest,
        "Content-Length": "42",
        "Content-Type": media_type + "; charset=utf-8",
    }
    if mismatch == "digest":
        headers["Docker-Content-Digest"] = "sha256:" + "b" * 64
    elif mismatch == "size":
        headers["Content-Length"] = "43"
    elif mismatch == "media_type":
        headers["Content-Type"] = "application/octet-stream"
    elif mismatch == "missing":
        del headers["Content-Length"]

    def fake_request(method, url, headers=None):
        assert method == "HEAD"
        assert "application/vnd.oci.image.manifest.v1+json" in headers["Accept"]
        return 200, b"", response_headers

    response_headers = headers
    monkeypatch.setattr(client, "_make_request", fake_request)
    if mismatch is None:
        client.verify_manifest_descriptor(digest, digest, 42, media_type)
    else:
        with pytest.raises(BlobIntegrityError, match="HEAD descriptor"):
            client.verify_manifest_descriptor(digest, digest, 42, media_type)

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

    monkeypatch.setattr(client._opener, "open", fake_urlopen)

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

    monkeypatch.setattr(client._opener, "open", fake_urlopen)

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

    monkeypatch.setattr(client._opener, "open", fake_urlopen)
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

    destination = tmp_path / "blobs" / digest.split(":", 1)[1]
    client = RegistryClient("registry.example", "team/app", auth_token="secret")
    client._blob_opener = Opener()

    assert client.pull_blob(digest, destination, len(content))
    assert destination.read_bytes() == content
    assert requests[0][0].get_header("Authorization") == "Bearer secret"
    assert requests[0][1] == client.timeout
    assert all(0 < size <= TRANSFER_CHUNK_SIZE for size in response.read_sizes)
    assert not list(destination.parent.glob(".*.tmp"))
    assert not client.pull_blob(digest, destination, len(content))


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

    destination = tmp_path / "blob"
    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
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

    monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _delay: None)
    destination = tmp_path / "blob"
    sentinel = tmp_path / ".blob-another-writer.tmp"
    sentinel.write_bytes(b"other writer")
    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    content = b"complete"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    with pytest.raises(RegistryRequestError, match="failed after 3 attempt"):
        client.pull_blob(digest, destination, len(content))

    assert not destination.exists()
    assert list(tmp_path.glob(".*.tmp")) == [sentinel]


def test_blob_download_rejects_insecure_redirect(monkeypatch):
    client = RegistryClient("registry.example", "team/app", auth_token="secret")

    class Opener:
        def open(self, request, timeout):
            raise _http_error(request.full_url, 302, {"Location": "http://storage.example/blob"})

    client._blob_opener = Opener()
    with pytest.raises(RegistryRequestError, match="HTTPS-to-HTTP"):
        client._open_blob_response("https://registry.example/v2/team/app/blobs/sha256:abc")


@pytest.mark.parametrize(
    "location",
    ["http://storage.example/blob", "https://user:password@storage.example/blob"],
)
def test_blob_response_redirect_rejects_downgrade_and_userinfo(monkeypatch, location):
    client = RegistryClient("127.0.0.1:5000", "team/app", auth_token="secret", insecure=True)

    class Opener:
        def open(self, request, timeout):
            return _Response(status=307, headers={"Location": location})

    client._blob_opener = Opener()
    monkeypatch.setattr(
        client._opener, "open",
        lambda *args, **kwargs: pytest.fail("unsafe redirect must not be contacted"),
    )
    with pytest.raises(RegistryRequestError):
        client._open_blob_response("https://127.0.0.1:5000/v2/team/app/blobs/sha256:abc")


@pytest.mark.parametrize("insecure", [False, True])
def test_blob_response_redirect_never_forwards_auth(monkeypatch, insecure):
    host = "127.0.0.1:5000" if insecure else "registry.example"
    client = RegistryClient(host, "team/app", auth_token="secret", insecure=insecure)
    scheme = "http" if insecure else "https"
    redirected = []

    class Opener:
        def open(self, request, timeout):
            return _Response(status=307, headers={"Location": f"{scheme}://storage.example/blob"})

    client._blob_opener = Opener()
    monkeypatch.setattr(
        client._opener, "open",
        lambda request, timeout: redirected.append(request) or _Response(),
    )
    client._open_blob_response(f"{scheme}://{host}/v2/team/app/blobs/sha256:abc")
    assert len(redirected) == 1
    for header in ("Authorization", "Proxy-Authorization", "Cookie"):
        assert redirected[0].get_header(header) is None


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
        "pycontainer.registry_client.time.sleep", delays.append
    )
    destination = tmp_path / "blob"
    client = RegistryClient("registry.example", "team/app", auth_token="registry-secret")
    client._blob_opener = Opener()
    monkeypatch.setattr(client._opener, "open", fake_urlopen)

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
    monkeypatch.setattr(client._opener, "open", fake_urlopen)

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

    monkeypatch.setattr(client._opener, "open", fake_urlopen)

    client.upload_blob_monolithic(
        f"sha256:{hashlib.sha256(content).hexdigest()}", source
    )

    assert requests[0].get_header("Authorization") is None


def test_upload_rejects_insecure_external_location_without_exposing_query(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.blob"
    source.write_bytes(b"upload")
    client = RegistryClient("registry.example", "team/app", auth_token="secret")
    monkeypatch.setattr(
        client, "initiate_blob_upload",
        lambda: "http://storage.example/upload?token=synthetic-secret",
    )
    monkeypatch.setattr(
        client._opener, "open",
        lambda *args, **kwargs: pytest.fail("insecure upload must not be contacted"),
    )
    with pytest.raises(RegistryRequestError, match="HTTPS-to-HTTP") as raised:
        client.upload_blob_monolithic(
            f"sha256:{hashlib.sha256(b'upload').hexdigest()}", source
        )
    assert "synthetic-secret" not in str(raised.value)


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

    monkeypatch.setattr(client._opener, "open", fake_urlopen)
    monkeypatch.setattr(
        "pycontainer.registry_client.time.sleep", delays.append
    )

    with pytest.raises(RegistryRequestError, match="GET request.*TimeoutError"):
        client._make_request("GET", "https://registry.example/v2/team/app/test")

    assert len(requests) == MAX_REQUEST_RETRIES + 1
    assert all(timeout == 2.5 for _, timeout in requests)
    assert len(delays) == MAX_REQUEST_RETRIES


def _errors_body(*entries):
    return json.dumps(
        {"errors": [{"code": code, "message": message} for code, message in entries]}
    ).encode()


def _patch_urlopen(monkeypatch, handler):
    requests = []
    delays = []

    def fake_open(_opener, request, timeout):
        requests.append(request)
        return handler(request, len(requests))

    monkeypatch.setattr(
        "pycontainer.registry_client.urllib.request.OpenerDirector.open", fake_open
    )
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", delays.append)
    return requests, delays


def test_denied_manifest_push_reports_operation_and_permission_hint(
    monkeypatch,
):
    client = RegistryClient("registry.example", "team/app")
    requests, delays = _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(
                request.full_url,
                403,
                body=_errors_body(("DENIED", "requested access to the resource is denied")),
            )
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client.push_manifest("latest", b"{}")

    message = str(error.value)
    assert not isinstance(error.value, (RegistryRateLimitError, RegistryQuotaError))
    assert "Manifest push failed with HTTP 403 from registry.example" in message
    assert "(response details omitted)" in message
    assert "requested access to the resource is denied" not in message
    assert "push permission" in message
    assert len(requests) == 1
    assert delays == []


def test_rate_limit_with_long_retry_after_fails_fast_with_reset_time(monkeypatch):
    client = RegistryClient("docker.io", "library/python")
    headers = {
        "Retry-After": "3600",
        "ratelimit-limit": "100;w=21600",
        "ratelimit-remaining": "0;w=21600",
    }
    requests, delays = _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(
                request.full_url,
                429,
                headers,
                _errors_body(("TOOMANYREQUESTS", "You have reached your pull rate limit.")),
            )
        ),
    )

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("3.11")

    message = str(error.value)
    assert error.value.retry_after_seconds == 3600
    assert "registry-1.docker.io" in message
    assert "(response details omitted)" in message
    assert "You have reached your pull rate limit." not in message
    assert "resets in about 3600 seconds" in message
    assert "0 of 100 remaining" in message
    assert "Authenticate" in message
    assert len(requests) == 1
    assert delays == []


def test_rate_limit_http_date_reports_utc_reset(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    retry_after = reset.strftime("%a, %d %b %Y %H:%M:%S GMT")
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 429, {"Retry-After": retry_after})
        ),
    )

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("latest")

    assert 7100 <= error.value.retry_after_seconds <= 7200
    assert reset.strftime("%Y-%m-%dT%H:%M:%SZ") in str(error.value)


def test_get_rate_limit_with_short_retry_after_retries_then_reports(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    requests, delays = _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 429, {"Retry-After": "1"})
        ),
    )

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("latest")

    assert len(requests) == MAX_REQUEST_RETRIES + 1
    assert delays == [1.0] * MAX_REQUEST_RETRIES
    assert "resets in about 1 second" in str(error.value)
    assert "(empty response)" in str(error.value)


@pytest.mark.parametrize("retry_after", ["soon", "nan", "inf", "-5"])
def test_malformed_retry_after_uses_bounded_backoff(monkeypatch, retry_after):
    client = RegistryClient("registry.example", "team/app")
    requests, delays = _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 429, {"Retry-After": retry_after})
        ),
    )

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("latest")

    assert len(requests) == MAX_REQUEST_RETRIES + 1
    assert delays == [0.25, 0.5]
    assert error.value.retry_after_seconds is None
    assert "did not report when the limit resets" in str(error.value)


@pytest.mark.parametrize("retry_after", ["1000000000000", "1e100"])
def test_large_retry_after_remains_actionable(monkeypatch, retry_after):
    client = RegistryClient("registry.example", "team/app")
    requests, delays = _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 429, {"Retry-After": retry_after})
        ),
    )

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("latest")

    assert error.value.retry_after_seconds == float(retry_after)
    assert f"resets in about {int(float(retry_after))} seconds" in str(error.value)
    assert "Authenticate" in str(error.value)
    assert len(requests) == 1
    assert delays == []


def test_past_retry_after_date_has_no_delay():
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    retry_after = past.strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert _retry_delay({"Retry-After": retry_after}, 0) == 0


@pytest.mark.parametrize("operation", ["initiate", "blob", "manifest", "index"])
def test_write_requests_are_never_retried_on_rate_limit(
    tmp_path, monkeypatch, operation
):
    client = RegistryClient("registry.example", "team/app")
    content = b"layer"
    blob = tmp_path / "blob"
    blob.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    def handler(request, _count):
        if operation == "blob" and request.get_method() == "POST":
            return _Response(status=202, headers={"Location": "/upload/1"})
        raise _http_error(request.full_url, 429, {"Retry-After": "1"})

    requests, delays = _patch_urlopen(monkeypatch, handler)

    with pytest.raises(RegistryRateLimitError):
        if operation == "initiate":
            client.initiate_blob_upload()
        elif operation == "blob":
            client.upload_blob_monolithic(digest, blob)
        elif operation == "manifest":
            client.push_manifest("latest", b"{}")
        else:
            client.push_index("latest", b"{}")

    write_methods = [r.get_method() for r in requests if r.get_method() in {"PUT", "POST"}]
    assert len(write_methods) == len(requests)
    assert len(requests) == (2 if operation == "blob" else 1)
    assert delays == []


@pytest.mark.parametrize("rate_limited", [False, True])
def test_manifest_write_replays_only_for_bearer_challenge(monkeypatch, rate_limited):
    client = RegistryClient("registry.example", "team/app")

    def handler(request, _count):
        if not request.full_url.startswith(client.base_url):
            return _Response(b'{"token": "synthetic-token"}')
        if not request.get_header("Authorization"):
            raise _http_error(
                request.full_url,
                401,
                {"Www-Authenticate": 'Bearer realm="https://auth.example/token"'},
            )
        assert request.get_header("Authorization") == "Bearer synthetic-token"
        if rate_limited:
            raise _http_error(request.full_url, 429, {"Retry-After": "1"})
        return _Response(status=201)

    requests, delays = _patch_urlopen(monkeypatch, handler)
    if rate_limited:
        with pytest.raises(RegistryRateLimitError):
            client.push_manifest("latest", b"{}")
    else:
        assert client.push_manifest("latest", b"{}")

    assert [request.get_method() for request in requests] == ["PUT", "GET", "PUT"]
    assert [request.data for request in requests if request.get_method() == "PUT"] == [
        b"{}",
        b"{}",
    ]
    assert delays == []


@pytest.mark.parametrize(
    ("fail_method", "status", "body"),
    [
        ("PUT", 413, b""),
        ("POST", 413, b""),
        ("PUT", 403, _errors_body(("DENIED", "storage quota exceeded for namespace"))),
        ("PUT", 400, _errors_body(("QUOTA_EXCEEDED", "repository is over quota"))),
    ],
)
def test_upload_quota_errors_name_blob_size(
    tmp_path, monkeypatch, fail_method, status, body
):
    client = RegistryClient("registry.example", "team/app")
    content = b"x" * 2048
    blob = tmp_path / "blob"
    blob.write_bytes(content)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    def handler(request, _count):
        if request.get_method() == fail_method:
            raise _http_error(request.full_url, status, body=body)
        return _Response(status=202, headers={"Location": "/upload/1"})

    _patch_urlopen(monkeypatch, handler)

    with pytest.raises(RegistryQuotaError) as error:
        client.upload_blob_monolithic(digest, blob)

    assert error.value.blob_size == 2048
    assert "2048 bytes (2.0 KiB)" in str(error.value)
    assert "quota" in str(error.value).lower()


def test_error_bodies_are_truncated_sanitized_and_redacted(monkeypatch):
    client = RegistryClient(
        "registry.example",
        "team/app",
        username="robot",
        password="super-secret-password",
    )
    body = (
        b"<html>\x1b[31mBearer abc.def.ghi Basic cm9ib3Q6c2VjcmV0 "
        b'"access_token": "tok-123" password=super-secret-password '
        + b"A" * 10000
    )
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 400, body=body)
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    message = str(error.value)
    assert len(message) < 1000
    for secret in ("abc.def.ghi", "cm9ib3Q6c2VjcmV0", "tok-123", "super-secret-password"):
        assert secret not in message
    assert "[REDACTED]" in message
    assert "\x1b" not in message
    assert "..." in message


@pytest.mark.parametrize(
    "detail",
    [
        'password="marker-first marker-second"',
        'password="marker-first marker-second',
        'password="marker-first\\\nmarker-second"',
        "'password': 'marker-first marker-second'",
        'token="marker-first marker-second"',
        'authorization="marker-first marker-second"',
        "username='marker-first marker-second'",
    ],
)
def test_error_body_redacts_complete_credential_fields(monkeypatch, detail):
    client = RegistryClient("registry.example", "team/app")
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 403, body=_errors_body(("DENIED", detail)))
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert "marker-first" not in str(error.value)
    assert "marker-second" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_error_body_redacts_overlapping_credentials_and_username(monkeypatch):
    client = RegistryClient(
        "registry.example",
        "team/app",
        auth_token="credential-prefix-distinct-suffix",
        username="sensitive-user",
        password="sensitive-password",
    )
    client._bearer_token = "credential-prefix"
    body = _errors_body(
        ("DENIED", "Identity sensitive-user used credential-prefix-distinct-suffix")
    )
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 403, body=body)
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert "sensitive-user" not in str(error.value)
    assert "distinct-suffix" not in str(error.value)


@pytest.mark.parametrize("structured", [True, False])
def test_error_body_redacts_credentials_and_query_in_urls(monkeypatch, structured):
    client = RegistryClient("registry.example", "team/app")
    detail = (
        "See https://marker-user:marker-password@storage.example/layer"
        "?sig=marker-signature&token=marker-token"
    )
    body = _errors_body(("DENIED", detail)) if structured else detail.encode()
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 400, body=body)
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert "storage.example/layer" in str(error.value)
    for secret in ("marker-user", "marker-password", "marker-signature", "marker-token"):
        assert secret not in str(error.value)


def test_error_body_redacts_malformed_url(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    detail = "See https://marker-user:marker-password@[broken/path?sig=marker-signature"
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 400, body=detail.encode())
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert "[REDACTED URL]" in str(error.value)
    for secret in ("marker-user", "marker-password", "marker-signature"):
        assert secret not in str(error.value)


def test_malformed_and_oversized_error_json_is_summarized(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    many = _errors_body(*[("DENIED", f"entry {index} " + "m" * 400) for index in range(8)])
    bodies = iter([b'{"errors": [{"code": "DENIED"', many])
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 403, body=next(bodies))
        ),
    )

    with pytest.raises(RegistryRequestError) as malformed:
        client._get_bearer_token({"realm": "https://auth.example/token"})
    with pytest.raises(RegistryRequestError) as oversized:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert '{"errors": [{"code": "DENIED"' in str(malformed.value)
    message = str(oversized.value)
    assert "entry 4" in message
    assert "entry 5" not in message
    assert "3 more error(s)" in message
    assert len(message) < 2000


def test_deeply_nested_error_body_is_summarized(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 400, body=b"[" * 4096)
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client._get_bearer_token({"realm": "https://auth.example/token"})

    assert "HTTP 400" in str(error.value)
    assert "..." in str(error.value)
    assert len(str(error.value)) < 1000


@pytest.mark.parametrize(
    ("code", "detail", "error_type", "hint"),
    [
        ("TOOMANYREQUESTS", "slow down", RegistryRateLimitError, "rate limit"),
        ("DENIED", "storage quota exceeded", RegistryQuotaError, "quota"),
        ("DENIED", "access forbidden", RegistryRequestError, "permission"),
    ],
)
def test_omitted_distribution_error_still_controls_classification(
    monkeypatch, code, detail, error_type, hint
):
    client = RegistryClient("registry.example", "team/app")
    body = _errors_body(*([("UNKNOWN", "failure")] * 5), (code, detail))
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 400, body=body)
        ),
    )

    with pytest.raises(error_type) as error:
        client.push_manifest("latest", b"{}")

    assert hint in str(error.value).lower()
    assert "(response details omitted)" in str(error.value)


@pytest.mark.parametrize("structured", [True, False])
def test_quota_classification_uses_full_sanitized_message(monkeypatch, structured):
    client = RegistryClient(
        "registry.example", "team/app", username="a", password="pw"
    )
    detail = "x" * 250 + "storage quota exceeded"
    body = _errors_body(("DENIED", detail)) if structured else detail.encode()
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 403, body=body)
        ),
    )

    with pytest.raises(RegistryQuotaError) as error:
        client.push_manifest("latest", b"{}")

    assert "registry.example" in str(error.value)


@pytest.mark.parametrize("structured", [True, False])
def test_credentials_mentioning_quota_do_not_trigger_quota_classification(
    monkeypatch, structured
):
    client = RegistryClient("registry.example", "team/app")
    detail = 'password="storage quota exceeded"'
    body = _errors_body(("DENIED", detail)) if structured else detail.encode()
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: (_ for _ in ()).throw(
            _http_error(request.full_url, 403, body=body)
        ),
    )

    with pytest.raises(RegistryRequestError) as error:
        client.push_manifest("latest", b"{}")

    assert not isinstance(error.value, RegistryQuotaError)
    assert "storage quota exceeded" not in str(error.value)


def test_malformed_manifest_preview_is_redacted(monkeypatch):
    client = RegistryClient("registry.example", "team/app", auth_token="tok-secret-1")
    _patch_urlopen(
        monkeypatch,
        lambda request, _count: _Response(b"not json Bearer leaked-token tok-secret-1"),
    )

    with pytest.raises(RegistryRequestError, match="invalid manifest JSON") as error:
        client.pull_manifest("latest")

    assert "leaked-token" not in str(error.value)
    assert "tok-secret-1" not in str(error.value)


def test_rate_limit_headers_are_logged_and_low_remaining_warns_once(
    monkeypatch, caplog
):
    client = RegistryClient("docker.io", "library/python")
    _patch_urlopen(
        monkeypatch,
        lambda request, count: _Response(
            b"{}",
            headers={
                "RateLimit-Limit": "100;w=21600",
                "RateLimit-Remaining": f"{10 - count};w=21600",
            },
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="pycontainer.registry_client"):
        client._make_request("GET", f"{client.base_url}/")
        client._make_request("GET", f"{client.base_url}/")

    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("remaining=9" in line and "limit=100" in line for line in debug)
    assert len(warnings) == 1
    assert "9 of 100" in warnings[0]


@pytest.mark.parametrize(
    "headers",
    [
        {"ratelimit-limit": "lots", "ratelimit-remaining": "-1"},
        {"ratelimit-remaining": "99999999999999999999999999;w=1x"},
        {"ratelimit-limit": "100;w=21600", "ratelimit-remaining": "50;w=21600"},
    ],
)
def test_malformed_or_healthy_rate_limit_headers_do_not_warn(
    monkeypatch, caplog, headers
):
    client = RegistryClient("registry.example", "team/app")
    _patch_urlopen(monkeypatch, lambda request, _count: _Response(headers=headers))

    with caplog.at_level(logging.DEBUG, logger="pycontainer.registry_client"):
        status, _, _ = client._make_request("GET", f"{client.base_url}/")

    assert status == 200
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_token_exchange_rate_limit_reports_reset_without_echoing_secrets(
    monkeypatch,
):
    client = RegistryClient(
        "registry.example", "team/app", username="robot", password="pw-secret-value"
    )

    def handler(request, _count):
        if request.full_url.startswith(client.base_url):
            raise _http_error(
                request.full_url,
                401,
                {"Www-Authenticate": 'Bearer realm="https://auth.example/token"'},
            )
        raise _http_error(
            request.full_url,
            429,
            {"Retry-After": "120"},
            _errors_body(("TOOMANYREQUESTS", "token rate exceeded for pw-secret-value")),
        )

    requests, delays = _patch_urlopen(monkeypatch, handler)

    with pytest.raises(RegistryRateLimitError) as error:
        client.pull_manifest("latest")

    message = str(error.value)
    assert "bearer-token exchange failed with HTTP 429 from auth.example" in message
    assert "resets in about 120 seconds" in message
    assert "pw-secret-value" not in message
    assert delays == []
    assert len(requests) == 2


@pytest.mark.parametrize("userinfo", ["synthetic-user:synthetic-password@", ""])
def test_token_exchange_redacts_url_credentials_and_http_error_traceback(
    monkeypatch, userinfo
):
    client = RegistryClient("registry.example", "team/app")
    reason_token = "synthetic-status-token"

    def fake_urlopen(request, timeout):
        if request.full_url.startswith(client.base_url):
            challenge = Message()
            challenge["Www-Authenticate"] = (
                f'Bearer realm="https://{userinfo}auth.example/token"'
            )
            raise urllib.error.HTTPError(
                request.full_url, 401, f"Bearer {reason_token}", challenge, io.BytesIO(b"")
            )
        headers = Message()
        headers["Retry-After"] = "120"
        raise urllib.error.HTTPError(
            request.full_url, 429, f"Bearer {reason_token}", headers, io.BytesIO(b"")
        )

    monkeypatch.setattr(client._opener, "open", fake_urlopen)
    expected = RegistryAuthenticationError if userinfo else RegistryRateLimitError
    with pytest.raises(expected) as error:
        client.pull_manifest("latest")

    rendered = "".join(traceback.format_exception(error.value))
    for secret in ("synthetic-user", "synthetic-password", reason_token):
        assert secret not in str(error.value)
        assert secret not in rendered


def test_invalid_token_challenge_hides_http_error_traceback(monkeypatch):
    client = RegistryClient("registry.example", "team/app")
    reason_token = "synthetic-challenge-token"
    headers = Message()
    headers["Www-Authenticate"] = 'Bearer service="registry.example"'

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, f"Bearer {reason_token}", headers, io.BytesIO(b"")
        )

    monkeypatch.setattr(client._opener, "open", fake_urlopen)
    with pytest.raises(RegistryAuthenticationError, match="token realm") as error:
        client.pull_manifest("latest")

    assert reason_token not in "".join(traceback.format_exception(error.value))


def test_blob_redirect_rejects_url_credentials_and_hides_http_traceback(
    tmp_path, monkeypatch, caplog
):
    redirect = (
        "https://synthetic-user:synthetic-password@storage.example/blob"
        "?token=synthetic-query-token"
    )
    reason_token = "synthetic-status-token"

    class Opener:
        def open(self, request, timeout):
            raise _http_error(request.full_url, 307, {"Location": redirect})

    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"

    with caplog.at_level(logging.DEBUG, logger="pycontainer.registry_client"):
        with pytest.raises(RegistryRequestError, match="embedded credentials") as error:
            client.pull_blob(digest, tmp_path / "blob", 1)

    rendered = "".join(traceback.format_exception(error.value))
    for secret in (
        "synthetic-user",
        "synthetic-password",
        "synthetic-query-token",
        reason_token,
    ):
        assert secret not in str(error.value)
        assert secret not in caplog.text
        assert secret not in rendered


@pytest.mark.parametrize("operation", ["manifest", "blob"])
def test_auth_replay_failure_hides_initial_http_error_traceback(
    tmp_path, monkeypatch, operation
):
    reason_token = "synthetic-initial-challenge-secret"
    client = RegistryClient("registry.example", "team/app")
    requests = []
    acquisitions = []

    def acquire(params):
        acquisitions.append(params)
        return "synthetic-replay-token"

    def open_request(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            headers = Message()
            headers["Www-Authenticate"] = (
                'Bearer realm="https://auth.example/token"'
            )
            raise urllib.error.HTTPError(
                request.full_url, 401, f"Bearer {reason_token}", headers,
                io.BytesIO(b""),
            )
        assert request.get_header("Authorization") == "Bearer synthetic-replay-token"
        if operation == "manifest":
            return _Response(b"oversized")
        return _Response(
            status=307,
            headers={"Location": "https://user:synthetic-password@storage.example/blob"},
        )

    monkeypatch.setattr(client, "_get_bearer_token", acquire)
    monkeypatch.setattr(client._opener, "open", open_request)
    monkeypatch.setattr(client, "_blob_opener", client._opener)
    monkeypatch.setattr("pycontainer.registry_client.MAX_MANIFEST_BYTES", 1)
    expected = "exceeds 1 bytes" if operation == "manifest" else "credentials"
    with pytest.raises(RegistryRequestError, match=expected) as error:
        if operation == "manifest":
            client.pull_manifest("latest")
        else:
            digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"
            client.pull_blob(digest, tmp_path / "blob", 1)

    assert len(requests) == 2
    assert len(acquisitions) == 1
    assert reason_token not in str(error.value)
    assert reason_token not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    if operation == "blob":
        assert not (tmp_path / "blob").exists()


class _SyntheticExchangeFailure(Exception):
    pass


@pytest.mark.parametrize("operation", ["manifest", "blob"])
@pytest.mark.parametrize("failure", ["invalid-utf8", "unexpected"])
def test_token_exchange_failure_hides_initial_http_error_traceback(
    tmp_path, monkeypatch, operation, failure
):
    reason_token = "synthetic-401-reason-secret"
    client = RegistryClient("registry.example", "team/app")
    requests = []

    def open_request(request, timeout):
        requests.append(request.full_url)
        if request.full_url.startswith("https://auth.example/"):
            if failure == "unexpected":
                raise _SyntheticExchangeFailure("synthetic exchange failure")
            return _Response(b"\xff")
        headers = Message()
        headers["Www-Authenticate"] = 'Bearer realm="https://auth.example/token"'
        raise urllib.error.HTTPError(
            request.full_url, 401, f"Bearer {reason_token}", headers, io.BytesIO(b"")
        )

    monkeypatch.setattr(client._opener, "open", open_request)
    monkeypatch.setattr(client, "_blob_opener", client._opener)
    expected = (
        (RegistryAuthenticationError, "not valid JSON")
        if failure == "invalid-utf8"
        else (_SyntheticExchangeFailure, "synthetic exchange failure")
    )
    with pytest.raises(expected[0], match=expected[1]) as error:
        if operation == "manifest":
            client.pull_manifest("latest")
        else:
            digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"
            client.pull_blob(digest, tmp_path / "blob", 1)

    assert len(requests) == 2
    assert client._bearer_token is None
    rendered = "".join(traceback.format_exception(error.value))
    assert reason_token not in str(error.value)
    assert reason_token not in rendered
    assert "HTTPError" not in rendered


def test_blob_redirect_error_hides_redirect_http_error_traceback(
    tmp_path, monkeypatch
):
    reason_token = "synthetic-redirect-reason-secret"
    headers = Message()
    headers["Location"] = "https://user:synthetic-password@storage.example/blob"

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 307, f"Redirect {reason_token}", headers,
                io.BytesIO(b""),
            )

    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    monkeypatch.setattr(client._opener, "open", lambda request, timeout: pytest.fail())
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"
    with pytest.raises(RegistryRequestError, match="credentials") as error:
        client.pull_blob(digest, tmp_path / "blob", 1)

    rendered = "".join(traceback.format_exception(error.value))
    for secret in (reason_token, "synthetic-password"):
        assert secret not in str(error.value)
        assert secret not in rendered
    assert error.value.__context__ is None


@pytest.mark.parametrize("rewind_fails", [False, True])
def test_auth_upload_replay_preserves_nonzero_body_offset(monkeypatch, rewind_fails):
    reason_token = "synthetic-upload-challenge-secret"
    requests = []

    class UploadBody(io.BytesIO):
        def seek(self, offset, whence=0):
            if rewind_fails and requests:
                raise OSError("Upload rewind failed")
            return super().seek(offset, whence)

    body = UploadBody(b"prefix-payload")
    body.seek(len(b"prefix-"))
    client = RegistryClient("registry.example", "team/app")

    def open_request(request, timeout):
        requests.append((request, request.data.read()))
        if len(requests) == 1:
            headers = Message()
            headers["Www-Authenticate"] = (
                'Bearer realm="https://auth.example/token"'
            )
            raise urllib.error.HTTPError(
                request.full_url, 401, f"Bearer {reason_token}", headers,
                io.BytesIO(b""),
            )
        assert request.get_header("Authorization") == "Bearer synthetic-upload-token"
        return _Response(status=201)

    monkeypatch.setattr(client._opener, "open", open_request)
    monkeypatch.setattr(client, "_get_bearer_token", lambda params: "synthetic-upload-token")
    url = f"{client.base_url}/team/app/blobs/uploads/test"
    if rewind_fails:
        with pytest.raises(OSError, match="Upload rewind failed") as error:
            client._make_request("PUT", url, data=body)
        assert len(requests) == 1
        assert reason_token not in str(error.value)
        assert reason_token not in "".join(traceback.format_exception(error.value))
        assert error.value.__context__ is None
    else:
        assert client._make_request("PUT", url, data=body)[0] == 201
        assert [payload for _, payload in requests] == [b"payload", b"payload"]


def test_blob_redirect_rate_limit_redacts_url_and_http_traceback(
    tmp_path, monkeypatch, caplog
):
    redirect = "https://storage.example/blob?token=synthetic-query-token"
    reason_token = "synthetic-status-token"

    class Opener:
        def open(self, request, timeout):
            raise _http_error(request.full_url, 307, {"Location": redirect})

    def fake_open(request, timeout):
        headers = Message()
        headers["Retry-After"] = "120"
        headers["RateLimit-Remaining"] = "0"
        raise urllib.error.HTTPError(
            request.full_url, 429, f"Bearer {reason_token}", headers, io.BytesIO(b"")
        )

    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    monkeypatch.setattr(client._opener, "open", fake_open)
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"

    with caplog.at_level(logging.DEBUG, logger="pycontainer.registry_client"):
        with pytest.raises(RegistryRateLimitError) as error:
            client.pull_blob(digest, tmp_path / "blob", 1)

    rendered = "".join(traceback.format_exception(error.value))
    assert "storage.example" in str(error.value)
    assert "storage.example" in caplog.text
    for secret in ("synthetic-query-token", reason_token):
        assert secret not in str(error.value)
        assert secret not in caplog.text
        assert secret not in rendered


def test_missing_blob_redirect_location_hides_http_error_traceback(
    tmp_path, monkeypatch
):
    reason_token = "synthetic-redirect-token"

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                307,
                f"Bearer {reason_token}",
                Message(),
                io.BytesIO(b""),
            )

    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"

    with pytest.raises(RegistryRequestError, match="did not include a Location") as error:
        client.pull_blob(digest, tmp_path / "blob", 1)

    assert reason_token not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("operation", ["manifest", "token", "blob", "push"])
def test_network_error_traceback_does_not_expose_credentials(
    tmp_path, monkeypatch, operation
):
    token = "synthetic-network-token"
    username = "synthetic-network-user"
    client = RegistryClient(
        "registry.example",
        f"{username}/app",
        auth_token=token,
        username=username,
    )
    delays = []

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(f"Bearer {token}")

    monkeypatch.setattr(client._opener, "open", fail)
    monkeypatch.setattr(client, "_open_blob_response", fail)
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", delays.append)
    error_type = (
        RegistryAuthenticationError if operation == "token" else RegistryRequestError
    )

    with pytest.raises(error_type) as error:
        if operation == "manifest":
            client.pull_manifest(f"latest-{token}")
        elif operation == "token":
            client._get_bearer_token({"realm": "https://auth.example/token"})
        elif operation == "blob":
            digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"
            client.pull_blob(digest, tmp_path / "blob", 1)
        else:
            client.push_manifest(f"latest-{token}", b"{}")

    assert "URLError" in str(error.value)
    for secret in (token, username):
        assert secret not in str(error.value)
        assert secret not in "".join(traceback.format_exception(error.value))
    assert len(delays) == (0 if operation == "push" else MAX_REQUEST_RETRIES)


@pytest.mark.parametrize("operation", ["network", "response"])
def test_url_encoded_credentials_are_redacted(monkeypatch, operation):
    token = "synthetic/token+secret"
    encoded_token = urllib.parse.quote(token, safe="").lower()
    url = f"https://registry.example/path/{encoded_token}"
    client = RegistryClient("registry.example", "team/app", auth_token=token)

    def fail(request, timeout):
        if operation == "network":
            raise urllib.error.URLError("synthetic failure")
        raise _http_error(
            request.full_url, 400, body=f"Failed at {url}".encode()
        )

    monkeypatch.setattr(client._opener, "open", fail)
    with pytest.raises(RegistryRequestError) as error:
        if operation == "network":
            client._make_request("PUT", url, data=b"")
        else:
            client._get_bearer_token({"realm": "https://auth.example/token"})

    assert token not in str(error.value)
    assert encoded_token not in str(error.value)
    assert encoded_token not in "".join(traceback.format_exception(error.value))
    assert "[REDACTED]" in str(error.value)


def test_pull_blob_rate_limit_fails_fast_and_observes_redirect_headers(
    tmp_path, monkeypatch, caplog
):
    attempts = []

    class Opener:
        def open(self, request, timeout):
            attempts.append(request)
            raise _http_error(
                request.full_url,
                429,
                {
                    "Retry-After": "600",
                    "ratelimit-limit": "100",
                    "ratelimit-remaining": "0",
                },
                _errors_body(("TOOMANYREQUESTS", "slow down")),
            )

    delays = []
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", delays.append)
    client = RegistryClient("registry.example", "team/app")
    client._blob_opener = Opener()
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"

    with caplog.at_level(logging.DEBUG, logger="pycontainer.registry_client"):
        with pytest.raises(RegistryRateLimitError) as error:
            client.pull_blob(digest, tmp_path / "blob", 1)

    assert "Blob download failed with HTTP 429 from registry.example" in str(error.value)
    assert "resets in about 600 seconds" in str(error.value)
    assert len(attempts) == 1
    assert delays == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


if __name__=="__main__":
    test_parse_image_reference()
    test_registry_client_construction()
    test_registry_url_construction()
    print("✅ All registry client tests passed")
