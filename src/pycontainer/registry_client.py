"""Docker Registry v2 API client implementation."""
import base64
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Tuple, Union

from .blob_store import (
    BlobIntegrityError,
    blob_matches,
    create_temporary_file,
    digest_filename,
    hash_file,
    publish_content_addressed,
)


DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 5
RETRY_BACKOFF_SECONDS = 0.25
TRANSFER_CHUNK_SIZE = 1024 * 1024
MAX_ERROR_BODY_BYTES = 4096
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class RegistryRequestError(RuntimeError):
    """Raised when a registry request fails operationally."""


class RegistryAuthenticationError(RegistryRequestError):
    """Raised when registry credentials cannot be exchanged or applied."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent forwarding registry authorization across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def _retry_delay(headers, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    delay = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                delay = (retry_time - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None
    if delay is None:
        delay = RETRY_BACKOFF_SECONDS * (2**attempt)
    return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))


def _append_digest(url: str, digest: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True
        )
        if key != "digest"
    ]
    query.append(("digest", digest))
    return urllib.parse.urlunsplit(
        parsed._replace(query=urllib.parse.urlencode(query))
    )


class RegistryClient:
    def __init__(
        self,
        registry: str,
        repository: str,
        auth_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        if timeout <= 0:
            raise ValueError("Registry request timeout must be greater than zero.")
        self.registry = registry.rstrip("/")
        if self.registry == "docker.io":
            self.registry = "registry-1.docker.io"
        self.repository = repository
        self.auth_token = auth_token
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url = f"https://{self.registry}/v2"
        self._bearer_token = None
        self._blob_opener = None

    def _parse_www_authenticate(self, header: str) -> Optional[Dict[str, str]]:
        """Parse Www-Authenticate header for OAuth2 challenge."""
        if not header or not header.startswith("Bearer "):
            return None
        params = {}
        pattern = r'(\w+)="([^"]+)"'
        for match in re.finditer(pattern, header):
            params[match.group(1)] = match.group(2)
        return params if params else None

    def _is_registry_url(self, url: str) -> bool:
        actual = urllib.parse.urlsplit(url)
        registry = urllib.parse.urlsplit(self.base_url)
        return (
            actual.scheme.lower() == registry.scheme.lower()
            and actual.netloc.lower() == registry.netloc.lower()
        )

    def _authorization_headers(self, url: str) -> Dict[str, str]:
        if not self._is_registry_url(url):
            return {}
        token = self._bearer_token or self.auth_token
        if token:
            return {"Authorization": f"Bearer {token}"}
        if self.username and self.password:
            credentials = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            return {"Authorization": f"Basic {credentials}"}
        return {}

    def _get_bearer_token(self, auth_params: Dict[str, str]) -> str:
        """Exchange registry credentials for a bearer token."""
        realm = auth_params.get("realm")
        if not realm:
            raise RegistryAuthenticationError(
                "Registry authentication challenge did not include a token realm."
            )

        parsed = urllib.parse.urlsplit(realm)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        for key in ("service", "scope"):
            value = auth_params.get(key)
            if value:
                query.append((key, value))
        url = urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query))
        )
        headers = {}
        if self.username and self.password:
            credentials = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credentials}"
        elif self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        elif self.password:
            headers["Authorization"] = f"Bearer {self.password}"

        request = urllib.request.Request(url, headers=headers)
        body = None
        for attempt in range(MAX_REQUEST_RETRIES + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout
                ) as response:
                    body = response.read(MAX_ERROR_BODY_BYTES * 16 + 1)
                break
            except urllib.error.HTTPError as error:
                if (
                    error.code in RETRYABLE_STATUSES
                    and attempt < MAX_REQUEST_RETRIES
                ):
                    time.sleep(_retry_delay(error.headers, attempt))
                    continue
                raise RegistryAuthenticationError(
                    f"Registry bearer-token exchange failed with HTTP {error.code}."
                ) from error
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as error:
                if attempt < MAX_REQUEST_RETRIES:
                    time.sleep(_retry_delay({}, attempt))
                    continue
                raise RegistryAuthenticationError(
                    "Registry bearer-token exchange failed due to a network error."
                ) from error
        if body is None:
            raise RegistryAuthenticationError(
                "Registry bearer-token exchange failed without a response."
            )
        if len(body) > MAX_ERROR_BODY_BYTES * 16:
            raise RegistryAuthenticationError(
                "Registry token response exceeded the supported size."
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise RegistryAuthenticationError(
                "Registry bearer-token response was not valid JSON."
            ) from error
        if not isinstance(data, dict):
            raise RegistryAuthenticationError(
                "Registry bearer-token response was not a JSON object."
            )

        token = data.get("token") or data.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryAuthenticationError(
                "Registry bearer-token response did not contain a token."
            )
        return token

    def _make_request(
        self,
        method: str,
        url: str,
        data: Optional[Union[bytes, BinaryIO]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry_auth: bool = True,
    ) -> Tuple[int, bytes, Dict]:
        method = method.upper()
        retry_safe = method in {"GET", "HEAD"}
        body_position = None
        if data is not None and hasattr(data, "tell"):
            body_position = data.tell()

        for attempt in range((MAX_REQUEST_RETRIES + 1) if retry_safe else 1):
            if body_position is not None:
                data.seek(body_position)
            request_headers = dict(headers or {})
            request_headers.update(self._authorization_headers(url))
            request = urllib.request.Request(
                url, data=data, headers=request_headers, method=method
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout
                ) as response:
                    return response.status, response.read(), dict(response.headers)
            except urllib.error.HTTPError as error:
                response_headers = dict(error.headers)
                if (
                    error.code == 401
                    and retry_auth
                    and self._is_registry_url(url)
                    and not self._bearer_token
                ):
                    challenge = error.headers.get("Www-Authenticate")
                    auth_params = self._parse_www_authenticate(challenge)
                    if auth_params:
                        self._bearer_token = self._get_bearer_token(auth_params)
                        if body_position is not None:
                            data.seek(body_position)
                        return self._make_request(
                            method,
                            url,
                            data,
                            headers,
                            retry_auth=False,
                        )
                if (
                    retry_safe
                    and error.code in RETRYABLE_STATUSES
                    and attempt < MAX_REQUEST_RETRIES
                ):
                    time.sleep(_retry_delay(error.headers, attempt))
                    continue
                try:
                    body = error.read(MAX_ERROR_BODY_BYTES)
                except OSError:
                    body = b""
                return error.code, body, response_headers
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as error:
                if retry_safe and attempt < MAX_REQUEST_RETRIES:
                    time.sleep(_retry_delay({}, attempt))
                    continue
                raise RegistryRequestError(
                    f"{method} request to {_safe_url(url)} failed after "
                    f"{attempt + 1} attempt(s): {type(error).__name__}."
                ) from error

        raise RegistryRequestError(f"{method} request to {_safe_url(url)} failed.")

    def blob_exists(self, digest: str) -> bool:
        """Check if blob exists in registry via HEAD request."""
        url = f"{self.base_url}/{self.repository}/blobs/{digest}"
        status, body, _ = self._make_request("HEAD", url)
        if status == 200:
            return True
        if status == 404:
            return False
        detail = body.decode("utf-8", errors="replace")
        raise RegistryRequestError(
            f"Registry blob HEAD failed with HTTP {status}: {detail or '(empty response)'}"
        )

    def initiate_blob_upload(self) -> str:
        """Initiate blob upload and return its absolute upload URL."""
        url = f"{self.base_url}/{self.repository}/blobs/uploads/"
        status, body, headers = self._make_request("POST", url, data=b"")
        if status not in (200, 202):
            detail = body.decode("utf-8", errors="replace")
            raise RegistryRequestError(
                f"Failed to initiate blob upload (HTTP {status}): "
                f"{detail or '(empty response)'}"
            )
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise RegistryRequestError(
                "Registry did not return a Location for the blob upload."
            )
        return urllib.parse.urljoin(url, location)

    def upload_blob_monolithic(self, digest: str, blob_path: Path) -> bool:
        """Upload a content-addressed file without loading it into memory."""
        if blob_path.is_symlink() or not blob_path.is_file():
            raise FileNotFoundError(f"Registry blob is not a regular file: {blob_path}")
        actual_digest, size = hash_file(blob_path)
        if actual_digest != digest:
            raise BlobIntegrityError(
                f"Local blob hashes to {actual_digest}, not requested digest {digest}."
            )

        upload_url = self.initiate_blob_upload()
        final_url = _append_digest(upload_url, digest)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        with blob_path.open("rb") as source:
            status, body, _ = self._make_request(
                "PUT", final_url, data=source, headers=headers
            )
        if status not in (201, 202):
            detail = body.decode("utf-8", errors="replace")
            raise RegistryRequestError(
                f"Blob upload failed with HTTP {status}: "
                f"{detail or '(empty response)'}"
            )
        return True

    def push_blob(self, digest: str, blob_path: Path, check_exists: bool = True) -> bool:
        """Push blob to registry, optionally checking if it exists first."""
        if check_exists and self.blob_exists(digest):
            return False
        self.upload_blob_monolithic(digest, blob_path)
        return True

    def push_manifest(self, reference: str, manifest_data: bytes) -> bool:
        """Push manifest by tag or digest."""
        url = f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
        status, body, _ = self._make_request(
            "PUT", url, data=manifest_data, headers=headers
        )
        if status not in (200, 201):
            detail = body.decode("utf-8", errors="replace")
            raise RegistryRequestError(
                f"Manifest push failed with HTTP {status}: "
                f"{detail or '(empty response)'}"
            )
        return True

    def push_index(self, reference: str, index_data: bytes) -> bool:
        """Push OCI index."""
        url = f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers = {"Content-Type": "application/vnd.oci.image.index.v1+json"}
        status, body, _ = self._make_request(
            "PUT", url, data=index_data, headers=headers
        )
        if status not in (200, 201):
            detail = body.decode("utf-8", errors="replace")
            raise RegistryRequestError(
                f"Index push failed with HTTP {status}: "
                f"{detail or '(empty response)'}"
            )
        return True

    def pull_manifest(
        self,
        reference: str,
        expected_digest: Optional[str] = None,
        expected_size: Optional[int] = None,
    ) -> Tuple[Dict, str]:
        """Pull manifest, returning its decoded JSON and registry digest."""
        if expected_digest is not None:
            digest_filename(expected_digest)
        if expected_size is not None and (
            type(expected_size) is not int or expected_size < 0
        ):
            raise ValueError("Expected manifest size must be a non-negative integer.")

        url = f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers = {
            "Accept": (
                "application/vnd.oci.image.manifest.v1+json,"
                "application/vnd.docker.distribution.manifest.v2+json,"
                "application/vnd.oci.image.index.v1+json,"
                "application/vnd.docker.distribution.manifest.list.v2+json"
            )
        }
        status, body, response_headers = self._make_request(
            "GET", url, headers=headers
        )
        if status != 200:
            body_text = body.decode("utf-8", errors="replace") if body else "(empty response)"
            raise RegistryRequestError(
                f"Failed to pull manifest (HTTP {status}): {body_text}"
            )
        if not body:
            raise RegistryRequestError(
                f"Registry returned an empty manifest response for {reference}."
            )
        if expected_size is not None and len(body) != expected_size:
            raise BlobIntegrityError(
                f"Downloaded manifest has size {len(body)}, expected {expected_size}."
            )
        body_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if expected_digest is not None and body_digest != expected_digest:
            raise BlobIntegrityError(
                f"Downloaded manifest hashes to {body_digest}, expected "
                f"{expected_digest}."
            )
        try:
            manifest = json.loads(body)
        except json.JSONDecodeError as error:
            preview = body[:500].decode("utf-8", errors="replace")
            raise RegistryRequestError(
                f"Registry returned invalid manifest JSON: {error}\n"
                f"Body preview: {preview}"
            ) from error
        if not isinstance(manifest, dict):
            raise RegistryRequestError(
                f"Registry returned a non-object manifest for {reference}."
            )
        digest = response_headers.get("Docker-Content-Digest") or response_headers.get(
            "docker-content-digest"
        )
        if digest and digest.startswith("sha256:") and digest != body_digest:
            raise BlobIntegrityError(
                f"Registry reported manifest digest {digest}, but its body hashes "
                f"to {body_digest}."
            )
        return manifest, digest or body_digest

    def _open_blob_response(self, url: str):
        request = urllib.request.Request(
            url, headers=self._authorization_headers(url)
        )
        # Building an opener creates a fresh TLS context and reloads the
        # system certificate store, so reuse one per client.
        if self._blob_opener is None:
            self._blob_opener = urllib.request.build_opener(NoRedirect())
        try:
            response = self._blob_opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            if error.code not in REDIRECT_STATUSES:
                raise
            location = error.headers.get("Location")
            error.close()
            if not location:
                raise RegistryRequestError(
                    "Registry blob redirect did not include a Location."
                ) from error
            target = urllib.parse.urljoin(url, location)
            return urllib.request.urlopen(
                urllib.request.Request(target), timeout=self.timeout
            )

        if response.status in REDIRECT_STATUSES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise RegistryRequestError(
                    "Registry blob redirect did not include a Location."
                )
            target = urllib.parse.urljoin(url, location)
            return urllib.request.urlopen(
                urllib.request.Request(target), timeout=self.timeout
            )
        return response

    def _download_blob_once(
        self,
        url: str,
        staged: Path,
        digest: str,
        expected_size: int,
    ) -> None:
        hasher = hashlib.sha256()
        size = 0
        with self._open_blob_response(url) as response:
            if response.status != 200:
                raise RegistryRequestError(
                    f"Blob download failed with HTTP {response.status}."
                )
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    response_size = int(content_length)
                except ValueError as error:
                    raise RegistryRequestError(
                        "Registry returned an invalid blob Content-Length."
                    ) from error
                if response_size != expected_size:
                    raise BlobIntegrityError(
                        f"Blob Content-Length is {response_size}, expected "
                        f"{expected_size}."
                    )

            with staged.open("wb") as target:
                while True:
                    chunk = response.read(TRANSFER_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        raise BlobIntegrityError(
                            f"Downloaded blob exceeds expected size {expected_size}."
                        )
                    hasher.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

        actual_digest = f"sha256:{hasher.hexdigest()}"
        if size != expected_size:
            raise BlobIntegrityError(
                f"Downloaded blob has size {size}, expected {expected_size}."
            )
        if actual_digest != digest:
            raise BlobIntegrityError(
                f"Downloaded blob hashes to {actual_digest}, expected {digest}."
            )

    def pull_blob(self, digest: str, dest_path: Path, expected_size: int) -> bool:
        """Stream a blob to a verified temporary file, then publish atomically."""
        digest_filename(digest)
        if type(expected_size) is not int or expected_size < 0:
            raise ValueError("Expected registry blob size must be a non-negative integer.")

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if dest_path.exists() or dest_path.is_symlink():
            if blob_matches(dest_path, digest, expected_size):
                return False
            raise BlobIntegrityError(
                f"Existing registry blob {dest_path} does not match {digest} "
                f"and size {expected_size}; refusing to overwrite it."
            )

        staged = create_temporary_file(dest_path.parent, dest_path.name)
        url = f"{self.base_url}/{self.repository}/blobs/{digest}"
        try:
            for attempt in range(MAX_REQUEST_RETRIES + 1):
                try:
                    self._download_blob_once(url, staged, digest, expected_size)
                    publish_content_addressed(
                        staged, dest_path, digest, expected_size
                    )
                    return True
                except urllib.error.HTTPError as error:
                    if (
                        error.code in RETRYABLE_STATUSES
                        and attempt < MAX_REQUEST_RETRIES
                    ):
                        time.sleep(_retry_delay(error.headers, attempt))
                        continue
                    detail = error.read(MAX_ERROR_BODY_BYTES).decode(
                        "utf-8", errors="replace"
                    )
                    raise RegistryRequestError(
                        f"Blob download failed with HTTP {error.code}: "
                        f"{detail or '(empty response)'}"
                    ) from error
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    http.client.HTTPException,
                ) as error:
                    if attempt < MAX_REQUEST_RETRIES:
                        time.sleep(_retry_delay({}, attempt))
                        continue
                    raise RegistryRequestError(
                        f"GET request to {_safe_url(url)} failed after "
                        f"{attempt + 1} attempt(s): {type(error).__name__}."
                    ) from error
            raise RegistryRequestError(
                f"Blob download failed after {MAX_REQUEST_RETRIES + 1} attempts."
            )
        finally:
            staged.unlink(missing_ok=True)


def parse_image_reference(ref: str) -> Tuple[str, str, str]:
    """Parse an image reference into registry, repository, and tag or digest."""
    if not isinstance(ref, str) or not ref or ref != ref.strip():
        raise ValueError("Image reference must be a non-empty string.")
    if (
        "://" in ref
        or any(character.isspace() for character in ref)
        or any(character in ref for character in "?#")
    ):
        raise ValueError(
            "Image references must not include a URL scheme, query, fragment, "
            "or whitespace."
        )

    name, separator, digest = ref.partition("@")
    if separator:
        if "@" in digest:
            raise ValueError("Image reference contains more than one digest separator.")
        try:
            digest_filename(digest)
        except ValueError as error:
            raise ValueError(
                "Image digest references must use a lowercase sha256 digest."
            ) from error

    final_slash = name.rfind("/")
    final_component = name[final_slash + 1 :]
    if not final_component:
        raise ValueError("Image reference is missing a repository name.")
    if ":" in final_component:
        repository_component, tag = final_component.rsplit(":", 1)
        if not tag:
            raise ValueError("Image reference contains an empty tag.")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError(f"Image reference contains an invalid tag: {tag!r}.")
        name = name[: final_slash + 1] + repository_component
    else:
        tag = "latest"

    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        raise ValueError("Image reference contains an invalid repository name.")

    parts = name.split("/")
    first = parts[0]
    has_registry = len(parts) > 1 and (
        first == "localhost" or "." in first or ":" in first
    )
    if has_registry:
        registry = first
        repository = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repository = name
        if "/" not in repository:
            repository = f"library/{repository}"

    if not repository or any(
        part in {"", ".", ".."} for part in repository.split("/")
    ):
        raise ValueError("Image reference contains an invalid repository name.")
    if registry in {"docker.io", "index.docker.io"} and "/" not in repository:
        repository = f"library/{repository}"

    return registry, repository, digest if separator else tag
