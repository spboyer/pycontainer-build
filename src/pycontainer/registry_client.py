"""Docker Registry v2 API client implementation."""
import urllib.request, urllib.parse, urllib.error, http.client, json, re, base64
from pathlib import Path
from typing import Optional, Dict, Tuple

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """HTTP handler that doesn't follow redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class RegistryClient:
    def __init__(self, registry: str, repository: str, auth_token: Optional[str]=None, username: Optional[str]=None, password: Optional[str]=None):
        self.registry=registry.rstrip('/')
        if self.registry=='docker.io':
            self.registry='registry-1.docker.io'
        self.repository=repository
        self.auth_token=auth_token
        self.username=username
        self.password=password
        self.base_url=f"https://{self.registry}/v2"
        self._bearer_token=None
    
    def _parse_www_authenticate(self, header: str) -> Optional[Dict[str, str]]:
        """Parse Www-Authenticate header for OAuth2 challenge."""
        if not header or not header.startswith('Bearer '): return None
        params={}
        pattern=r'(\w+)="([^"]+)"'
        for match in re.finditer(pattern, header):
            params[match.group(1)]=match.group(2)
        return params if params else None
    
    def _get_bearer_token(self, auth_params: Dict[str, str]) -> Optional[str]:
        """Exchange credentials for bearer token via OAuth2."""
        realm=auth_params.get('realm')
        service=auth_params.get('service')
        scope=auth_params.get('scope')
        
        if not realm: return None
        
        params=[]
        if service: params.append(f'service={service}')
        if scope: params.append(f'scope={scope}')
        
        url=f"{realm}?{'&'.join(params)}"
        headers={}
        
        if self.username and self.password:
            creds=base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            headers['Authorization']=f'Basic {creds}'
        elif self.password:
            headers['Authorization']=f'Bearer {self.password}'
        
        try:
            req=urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                data=json.loads(resp.read())
                return data.get('token') or data.get('access_token')
        except: return None
    
    def _make_request(self, method: str, url: str, data: Optional[bytes]=None, headers: Optional[Dict]=None, retry_auth: bool=True) -> Tuple[int, bytes, Dict]:
        h=headers or {}
        
        token=self._bearer_token or self.auth_token
        if token:
            h['Authorization']=f'Bearer {token}'
        elif self.username and self.password:
            creds=base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            h['Authorization']=f'Basic {creds}'
        
        req=urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code==401 and retry_auth and not self._bearer_token:
                www_auth=e.headers.get('Www-Authenticate')
                if www_auth:
                    auth_params=self._parse_www_authenticate(www_auth)
                    if auth_params:
                        self._bearer_token=self._get_bearer_token(auth_params)
                        if self._bearer_token:
                            return self._make_request(method, url, data, headers, retry_auth=False)
            body=b''
            try:
                body=e.read()
            except: pass
            return e.code, body, dict(e.headers)
        except Exception as ex:
            raise RuntimeError(f"Request failed for {url}: {ex}")
    
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
    
    def pull_manifest(self, reference: str) -> Tuple[Dict, str]:
        """Pull manifest from registry, returns (manifest_dict, digest)."""
        url=f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers={'Accept':'application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.index.v1+json'}
        status, body, resp_headers=self._make_request('GET', url, headers=headers)
        if status!=200:
            body_text=body.decode() if body else '(empty response)'
            raise RuntimeError(f"Failed to pull manifest: {status} {body_text}")
        if not body:
            raise RuntimeError(f"Empty response body from registry for {reference}")
        try:
            manifest=json.loads(body)
        except json.JSONDecodeError as e:
            body_preview=body[:500].decode('utf-8', errors='replace')
            raise RuntimeError(f"Invalid JSON in manifest response: {e}\nBody preview: {body_preview}")
        digest=resp_headers.get('Docker-Content-Digest') or resp_headers.get('docker-content-digest')
        return manifest, digest
    
    def pull_blob(self, digest: str, dest_path: Path) -> bool:
        """Download blob from registry to local path."""
        url=f"{self.base_url}/{self.repository}/blobs/{digest}"
        
        req=urllib.request.Request(url)
        token=self._bearer_token or self.auth_token
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        elif self.username and self.password:
            creds=base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            req.add_header('Authorization', f'Basic {creds}')
        
        opener=urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    redirect_url=resp.headers.get('Location')
                    if redirect_url:
                        redirect_req=urllib.request.Request(redirect_url)
                        with urllib.request.urlopen(redirect_req) as redirect_resp:
                            body=redirect_resp.read()
                            dest_path.write_bytes(body)
                            return True
                elif resp.status==200:
                    body=resp.read()
                    dest_path.write_bytes(body)
                    return True
                else:
                    body=resp.read()
                    error_msg=body.decode('utf-8', errors='replace') if body else '(empty)'
                    raise RuntimeError(f"Failed to pull blob {digest}: {resp.status} - {error_msg}")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                redirect_url=e.headers.get('Location')
                if redirect_url:
                    redirect_req=urllib.request.Request(redirect_url)
                    with urllib.request.urlopen(redirect_req) as redirect_resp:
                        body=redirect_resp.read()
                        dest_path.write_bytes(body)
                        return True
            body=e.read() if hasattr(e, 'read') else b''
            error_msg=body.decode('utf-8', errors='replace') if body else '(empty)'
            raise RuntimeError(f"Failed to pull blob {digest}: {e.code} - {error_msg}")

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
