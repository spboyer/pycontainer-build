"""Tests for registry client functionality."""
import base64
import io
import json
import urllib.error
from email.message import Message

from pycontainer.registry_client import parse_image_reference, RegistryClient

def test_parse_image_reference():
    """Test parsing various image reference formats."""
    assert parse_image_reference("ghcr.io/user/app:v1")==("ghcr.io","user/app","v1")
    assert parse_image_reference("docker.io/library/python:3.11")==("docker.io","library/python","3.11")
    assert parse_image_reference("localhost:5000/test:latest")==("localhost:5000","test","latest")
    assert parse_image_reference("myapp:v2")==("docker.io","library/myapp","v2")
    assert parse_image_reference("user/app:tag")==("docker.io","user/app","tag")
    assert parse_image_reference("localhost:5000/test")==("localhost:5000","test","latest")
    assert parse_image_reference("alpine")==("docker.io","library/alpine","latest")

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

        def read(self):
            return self.body

    def fake_urlopen(request):
        authorization=request.get_header("Authorization")
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


if __name__=="__main__":
    test_parse_image_reference()
    test_registry_client_construction()
    test_registry_url_construction()
    print("✅ All registry client tests passed")
