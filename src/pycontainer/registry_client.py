"""Docker Registry v2 API client implementation."""
import urllib.request, urllib.parse, urllib.error, json
from pathlib import Path
from typing import Optional, Dict, Tuple

class RegistryClient:
    def __init__(self, registry: str, repository: str, auth_token: Optional[str]=None):
        self.registry=registry.rstrip('/')
        self.repository=repository
        self.auth_token=auth_token
        self.base_url=f"https://{self.registry}/v2"
    
    def _make_request(self, method: str, url: str, data: Optional[bytes]=None, headers: Optional[Dict]=None) -> Tuple[int, bytes, Dict]:
        h=headers or {}
        if self.auth_token:
            h['Authorization']=f'Bearer {self.auth_token}'
        req=urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
    
    def blob_exists(self, digest: str) -> bool:
        """Check if blob exists in registry via HEAD request."""
        url=f"{self.base_url}/{self.repository}/blobs/{digest}"
        status, _, _=self._make_request('HEAD', url)
        return status==200
    
    def initiate_blob_upload(self) -> str:
        """Initiate blob upload, returns upload URL."""
        url=f"{self.base_url}/{self.repository}/blobs/uploads/"
        status, body, headers=self._make_request('POST', url, data=b'')
        if status not in (200, 202):
            raise RuntimeError(f"Failed to initiate upload: {status} {body.decode()}")
        location=headers.get('Location') or headers.get('location')
        if not location:
            raise RuntimeError("No Location header in upload response")
        if not location.startswith('http'):
            location=f"https://{self.registry}{location}"
        return location
    
    def upload_blob_monolithic(self, digest: str, data: bytes) -> bool:
        """Upload blob in single request (monolithic upload)."""
        upload_url=self.initiate_blob_upload()
        final_url=f"{upload_url}&digest={digest}"
        headers={'Content-Type':'application/octet-stream','Content-Length':str(len(data))}
        status, body, _=self._make_request('PUT', final_url, data=data, headers=headers)
        if status not in (201, 202):
            raise RuntimeError(f"Blob upload failed: {status} {body.decode()}")
        return True
    
    def push_blob(self, digest: str, blob_path: Path, check_exists: bool=True) -> bool:
        """Push blob to registry, optionally checking if it exists first."""
        if check_exists and self.blob_exists(digest):
            return False
        data=blob_path.read_bytes()
        self.upload_blob_monolithic(digest, data)
        return True
    
    def push_manifest(self, reference: str, manifest_data: bytes) -> bool:
        """Push manifest to registry. Reference can be tag or digest."""
        url=f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers={'Content-Type':'application/vnd.oci.image.manifest.v1+json'}
        status, body, _=self._make_request('PUT', url, data=manifest_data, headers=headers)
        if status not in (200, 201):
            raise RuntimeError(f"Manifest push failed: {status} {body.decode()}")
        return True
    
    def push_index(self, reference: str, index_data: bytes) -> bool:
        """Push OCI index to registry."""
        url=f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers={'Content-Type':'application/vnd.oci.image.index.v1+json'}
        status, body, _=self._make_request('PUT', url, data=index_data, headers=headers)
        if status not in (200, 201):
            raise RuntimeError(f"Index push failed: {status} {body.decode()}")
        return True

def parse_image_reference(ref: str) -> Tuple[str, str, str]:
    """Parse image reference into (registry, repository, tag).
    
    Examples:
        ghcr.io/user/app:v1 -> (ghcr.io, user/app, v1)
        docker.io/library/python:3.11 -> (docker.io, library/python, 3.11)
        localhost:5000/test:latest -> (localhost:5000, test, latest)
    """
    if '/' not in ref:
        return 'docker.io', f'library/{ref.split(":")[0]}', ref.split(":")[-1] if ':' in ref else 'latest'
    
    parts=ref.split('/')
    if '.' in parts[0] or ':' in parts[0]:
        registry=parts[0]
        repo='/'.join(parts[1:])
    else:
        registry='docker.io'
        repo=ref
    
    if ':' in repo:
        repo, tag=repo.rsplit(':', 1)
    else:
        tag='latest'
    
    return registry, repo, tag
