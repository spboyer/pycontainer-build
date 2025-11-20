"""Tests for registry client functionality."""
import json
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

if __name__=="__main__":
    test_parse_image_reference()
    test_registry_client_construction()
    test_registry_url_construction()
    print("✅ All registry client tests passed")
