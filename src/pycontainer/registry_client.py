"""Docker Registry v2 API client implementation."""
import base64
import hashlib
import http.client
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Tuple, Union

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
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
MAX_REPORTED_ERRORS = 5
MAX_ERROR_TEXT_CHARS = 200
LOW_RATE_LIMIT_REMAINING = 10
QUOTA_STATUSES = {413, 507}

logger = logging.getLogger(__name__)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_AUTH_SCHEME_VALUE = re.compile(r"(?i)(bearer|basic)\s+[^\s\"',;]+")
_TEXT_URL = re.compile(r"""(?i)\bhttps?://[^\s"'<>]+""")
_SECRET_FIELD_VALUE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|id_token|token|username|password|"
    r"secret|authorization)\b[\"']?\s*[:=]\s*)"
    r"""(?:"(?:\\[\s\S]|[^"\\])*"?|'(?:\\[\s\S]|[^'\\])*'?|[^\s,;}&]+)"""
)
_QUOTA_TEXT = re.compile(
    r"(?i)quota|storage limit|storage capacity|insufficient storage|too large"
)


class RegistryRequestError(RuntimeError):
    """Raised when a registry request fails operationally."""


class RegistryAuthenticationError(RegistryRequestError):
    """Raised when registry credentials cannot be exchanged or applied."""


class RegistryRateLimitError(RegistryRequestError):
    """Raised when a registry rejects requests because of a rate limit."""

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RegistryQuotaError(RegistryRequestError):
    """Raised when a registry rejects an upload for size or storage quota."""

    def __init__(self, message: str, blob_size: Optional[int] = None):
        super().__init__(message)
        self.blob_size = blob_size


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent forwarding registry authorization across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_safe_https_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return bool(
            parsed.scheme.lower() == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


class SafeRegistryRedirect(urllib.request.HTTPRedirectHandler):
    """Keep registry credentials off cross-origin or insecure redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_https_url(newurl):
            raise RegistryRequestError("Registry redirects must use HTTPS without embedded credentials.")
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and (
            source.scheme.lower(), source.netloc.lower()
        ) != (target.scheme.lower(), target.netloc.lower()):
            for name in ("Authorization", "Proxy-Authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


def _open_registry_request(request, timeout):
    return urllib.request.build_opener(SafeRegistryRedirect()).open(
        request, timeout=timeout
    )


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
    )


def _header(headers, name: str) -> Optional[str]:
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, item in headers.items():
            if key.lower() == lowered:
                return item
    return value


def _retry_after_seconds(headers) -> Optional[float]:
    """Return the uncapped Retry-After delay, or None when absent or malformed."""
    retry_after = _header(headers, "Retry-After")
    if not retry_after:
        return None
    retry_after = retry_after.strip()
    try:
        delay = float(retry_after)
    except ValueError:
        try:
            retry_time = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, OverflowError, IndexError):
            return None
        if retry_time is None:
            return None
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=timezone.utc)
        delay = (retry_time - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


def _retry_delay(headers, attempt: int) -> float:
    delay = _retry_after_seconds(headers)
    if delay is None:
        delay = RETRY_BACKOFF_SECONDS * (2**attempt)
    return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))


def _should_retry(status: int, headers, attempt: int) -> bool:
    """Retry transient statuses unless a rate limit outlasts the retry budget."""
    if status not in RETRYABLE_STATUSES or attempt >= MAX_REQUEST_RETRIES:
        return False
    if status == 429:
        delay = _retry_after_seconds(headers)
        if delay is not None and delay > MAX_RETRY_DELAY_SECONDS:
            return False
    return True


def _rate_limit_value(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    count = value.split(";", 1)[0].strip()
    if not re.fullmatch(r"\d{1,12}", count):
        return None
    return int(count)


def _rate_limit(headers) -> Tuple[Optional[int], Optional[int]]:
    return (
        _rate_limit_value(_header(headers, "RateLimit-Limit")),
        _rate_limit_value(_header(headers, "RateLimit-Remaining")),
    )


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{size} bytes ({value:.1f} {unit})"
    return f"{size} bytes"


def _redact_text_url(match: re.Match) -> str:
    try:
        return _safe_url(match.group())
    except ValueError:
        return "[REDACTED URL]"


def _truncate_text(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _sanitize_text(
    text: str,
    secrets: Iterable[str],
    limit: Optional[int],
    username: Optional[str] = None,
) -> str:
    text = _TEXT_URL.sub(_redact_text_url, text)
    values = {(secret, False) for secret in secrets if secret}
    if username and not any(secret == username for secret, _ in values):
        values.add((username, True))
    for secret, word_only in tuple(values):
        values.add((urllib.parse.quote(secret, safe=""), word_only))
        values.add((urllib.parse.quote_plus(secret, safe=""), word_only))
    for secret, word_only in sorted(
        values, key=lambda value: len(value[0]), reverse=True
    ):
        pattern = re.sub(
            r"%[0-9a-f]{2}",
            lambda match: f"(?i:{match.group()})",
            re.escape(secret),
            flags=re.IGNORECASE,
        )
        if word_only:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        text = re.sub(pattern, "[REDACTED]", text)
    text = _AUTH_SCHEME_VALUE.sub(r"\1 [REDACTED]", text)
    text = _SECRET_FIELD_VALUE.sub(r"\1[REDACTED]", text)
    text = " ".join(_CONTROL_CHARACTERS.sub(" ", text).split())
    return _truncate_text(text, limit) if limit is not None else text


def _registry_errors(body: bytes) -> Optional[List[Tuple[str, str]]]:
    """Parse an OCI distribution error body into (code, message) pairs."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("errors"), list):
        return None
    entries = []
    for entry in document["errors"]:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        message = entry.get("message")
        entries.append(
            (
                code if isinstance(code, str) else "",
                message if isinstance(message, str) else "",
            )
        )
    return entries or None


def _registry_error(
    operation: str,
    url: str,
    status: int,
    body: Optional[bytes],
    headers,
    secrets: Iterable[str] = (),
    blob_size: Optional[int] = None,
    repository: Optional[str] = None,
    error_class=RegistryRequestError,
    username: Optional[str] = None,
) -> RegistryRequestError:
    """Build an actionable error from an untrusted registry error response."""
    secrets = [secret for secret in secrets if secret]
    body = (body or b"")[:MAX_ERROR_BODY_BYTES]
    host = _sanitize_text(
        urllib.parse.urlsplit(_safe_url(url)).netloc or "registry",
        secrets,
        MAX_ERROR_TEXT_CHARS,
        username=username,
    )
    entries = _registry_errors(body) if body else None
    codes = set()
    quota_message = False
    if entries:
        parts = []
        for index, (code, message) in enumerate(entries):
            codes.add(code.upper())
            code = _sanitize_text(code, secrets, 64, username=username).upper()
            message = _sanitize_text(message, secrets, None, username=username)
            quota_message = quota_message or bool(_QUOTA_TEXT.search(message))
            if index < MAX_REPORTED_ERRORS:
                message = _truncate_text(message, MAX_ERROR_TEXT_CHARS)
                parts.append(
                    f"{code}: {message}" if code and message else code or message
                )
        if len(entries) > MAX_REPORTED_ERRORS:
            parts.append(f"{len(entries) - MAX_REPORTED_ERRORS} more error(s) omitted")
        detail = "; ".join(part for part in parts if part) or "(empty error list)"
    elif body:
        detail = _sanitize_text(
            body.decode("utf-8", errors="replace"),
            secrets,
            None,
            username=username,
        )
        quota_message = bool(_QUOTA_TEXT.search(detail))
        detail = _truncate_text(detail, MAX_ERROR_TEXT_CHARS) or "(empty response)"
    else:
        detail = "(empty response)"

    message = f"{operation} failed with HTTP {status} from {host}: {detail}"
    if status == 429 or "TOOMANYREQUESTS" in codes:
        retry_after = _retry_after_seconds(headers)
        if retry_after is None:
            message += " The registry rate limit was exceeded and it did not report when the limit resets."
        else:
            seconds = math.ceil(retry_after)
            message += (
                f" The registry rate limit was exceeded; it resets in about "
                f"{seconds} second{'s' if seconds != 1 else ''}"
            )
            try:
                reset_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            except OverflowError:
                message += "."
            else:
                message += f" (at {reset_at.strftime('%Y-%m-%dT%H:%M:%SZ')})."
        limit, remaining = _rate_limit(headers)
        if remaining is not None:
            message += f" Rate limit: {remaining} of {limit if limit is not None else 'unknown'} remaining."
        message += (
            " Authenticate to the registry (Docker Hub grants higher limits to "
            "signed-in users), wait for the reset, or use a registry mirror."
        )
        return RegistryRateLimitError(message, retry_after)

    if (
        status in QUOTA_STATUSES
        or any("QUOTA" in code for code in codes)
        or quota_message
        or _QUOTA_TEXT.search(detail)
    ):
        if blob_size is not None:
            message += f" Upload size: {_format_size(blob_size)}."
        message += (
            " The registry rejected the upload as exceeding a size or storage "
            "quota; free space in the repository, raise its quota, or reduce "
            "the image size."
        )
        return RegistryQuotaError(message, blob_size)

    if status in (401, 403) or codes & {"DENIED", "UNAUTHORIZED"}:
        target = ""
        if repository:
            target = " for " + _sanitize_text(
                repository, secrets, MAX_ERROR_TEXT_CHARS, username=username
            )
        message += (
            f" Check that the credentials have pull or push permission{target}; "
            "some registries also report exhausted quotas as DENIED."
        )
    return error_class(message)


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
        self._rate_limit_warned = False

    def _secrets(self) -> List[str]:
        values = [self._bearer_token, self.auth_token, self.password]
        if self.username and self.password:
            values.append(
                base64.b64encode(
                    f"{self.username}:{self.password}".encode()
                ).decode()
            )
        return [value for value in values if value]

    def _safe_request_url(self, url: str) -> str:
        return _sanitize_text(
            _safe_url(url),
            self._secrets(),
            MAX_ERROR_TEXT_CHARS,
            username=self.username,
        )

    def _error(self, operation: str, url: str, status: int, body, headers, **kwargs):
        return _registry_error(
            operation,
            url,
            status,
            body,
            headers,
            secrets=self._secrets(),
            repository=self.repository,
            username=self.username,
            **kwargs,
        )

    def _observe_rate_limit(self, headers, url: str) -> None:
        """Log registry rate-limit headers and warn once when nearly exhausted."""
        limit, remaining = _rate_limit(headers)
        if limit is None and remaining is None:
            return
        host = _sanitize_text(
            urllib.parse.urlsplit(_safe_url(url)).netloc,
            self._secrets(),
            MAX_ERROR_TEXT_CHARS,
            username=self.username,
        )
        logger.debug(
            "Registry %s rate limit: limit=%s remaining=%s",
            host,
            "unknown" if limit is None else limit,
            "unknown" if remaining is None else remaining,
        )
        if remaining is None or self._rate_limit_warned:
            return
        threshold = LOW_RATE_LIMIT_REMAINING
        if limit:
            threshold = max(1, min(LOW_RATE_LIMIT_REMAINING, limit // 10))
        if remaining <= threshold:
            self._rate_limit_warned = True
            logger.warning(
                "Registry %s rate limit is nearly exhausted: %s of %s requests "
                "remaining. Authenticate or reduce pulls to avoid HTTP 429 errors.",
                host,
                remaining,
                "unknown" if limit is None else limit,
            )

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

    def _is_unauthenticated_loopback_request(self, url: str) -> bool:
        # A caller-configured loopback registry must never receive credentials.
        try:
            target = urllib.parse.urlsplit(url)
            base = urllib.parse.urlsplit(self.base_url)
            return bool(
                target.scheme == base.scheme == "http"
                and target.hostname == base.hostname == "127.0.0.1"
                and target.netloc == base.netloc
                and not target.username
                and not target.password
                and all(
                    value is None
                    for value in (self.auth_token, self.username, self.password, self._bearer_token)
                )
            )
        except ValueError:
            return False

    def _get_bearer_token(self, auth_params: Dict[str, str]) -> str:
        """Exchange registry credentials for a bearer token."""
        realm = auth_params.get("realm")
        if not realm:
            raise RegistryAuthenticationError(
                "Registry authentication challenge did not include a token realm."
            )

        if not _is_safe_https_url(realm):
            raise RegistryAuthenticationError(
                "Registry bearer-token realm must use HTTPS without embedded credentials."
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
                with _open_registry_request(request, timeout=self.timeout) as response:
                    body = response.read(MAX_ERROR_BODY_BYTES * 16 + 1)
                break
            except RegistryRequestError:
                raise RegistryAuthenticationError(
                    "Registry bearer-token redirect was rejected."
                ) from None
            except urllib.error.HTTPError as error:
                if _should_retry(error.code, error.headers, attempt):
                    time.sleep(_retry_delay(error.headers, attempt))
                    continue
                try:
                    error_body = error.read(MAX_ERROR_BODY_BYTES)
                except OSError:
                    error_body = b""
                # HTTP reason phrases can contain credentials.
                raise _registry_error(
                    "Registry bearer-token exchange",
                    url,
                    error.code,
                    error_body,
                    error.headers,
                    secrets=self._secrets(),
                    error_class=RegistryAuthenticationError,
                    username=self.username,
                ) from None
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
                    "Registry bearer-token exchange failed due to a network error "
                    f"after {attempt + 1} attempt(s): {type(error).__name__}."
                ) from None
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
        if not _is_safe_https_url(url):
            sensitive_headers = any(
                name.lower() in {"authorization", "proxy-authorization", "cookie"}
                for name in (headers or {})
            )
            if sensitive_headers or not self._is_unauthenticated_loopback_request(url):
                raise RegistryRequestError(
                    "Registry requests must use HTTPS without embedded credentials."
                )
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
                with _open_registry_request(request, timeout=self.timeout) as response:
                    response_headers = dict(response.headers)
                    self._observe_rate_limit(response_headers, url)
                    return response.status, response.read(), response_headers
            except urllib.error.HTTPError as error:
                response_headers = dict(error.headers)
                self._observe_rate_limit(response_headers, url)
                if (
                    error.code == 401
                    and retry_auth
                    and self._is_registry_url(url)
                    and not self._bearer_token
                ):
                    challenge = error.headers.get("Www-Authenticate")
                    auth_params = self._parse_www_authenticate(challenge)
                    if auth_params:
                        try:
                            self._bearer_token = self._get_bearer_token(auth_params)
                        except RegistryRequestError as token_error:
                            # Do not expose the original 401 reason in a traceback.
                            raise token_error from None
                        if body_position is not None:
                            data.seek(body_position)
                        return self._make_request(
                            method,
                            url,
                            data,
                            headers,
                            retry_auth=False,
                        )
                if retry_safe and _should_retry(error.code, error.headers, attempt):
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
                    f"{method} request to {self._safe_request_url(url)} failed after "
                    f"{attempt + 1} attempt(s): {type(error).__name__}."
                ) from None

        raise RegistryRequestError(
            f"{method} request to {self._safe_request_url(url)} failed."
        )

    def blob_exists(self, digest: str) -> bool:
        """Check if blob exists in registry via HEAD request."""
        url = f"{self.base_url}/{self.repository}/blobs/{digest}"
        status, body, headers = self._make_request("HEAD", url)
        if status == 200:
            return True
        if status == 404:
            return False
        raise self._error("Registry blob HEAD", url, status, body, headers)

    def initiate_blob_upload(self, blob_size: Optional[int] = None) -> str:
        """Initiate blob upload and return its absolute upload URL."""
        url = f"{self.base_url}/{self.repository}/blobs/uploads/"
        status, body, headers = self._make_request("POST", url, data=b"")
        if status not in (200, 202):
            raise self._error(
                "Blob upload initiation", url, status, body, headers, blob_size=blob_size
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

        try:
            upload_url = self.initiate_blob_upload()
        except RegistryQuotaError as error:
            if error.blob_size is not None:
                raise
            raise RegistryQuotaError(
                f"{error} Upload size: {_format_size(size)}.", size
            ) from error
        final_url = _append_digest(upload_url, digest)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        with blob_path.open("rb") as source:
            status, body, response_headers = self._make_request(
                "PUT", final_url, data=source, headers=headers
            )
        if status not in (201, 202):
            raise self._error(
                "Blob upload", final_url, status, body, response_headers, blob_size=size
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
        status, body, response_headers = self._make_request(
            "PUT", url, data=manifest_data, headers=headers
        )
        if status not in (200, 201):
            raise self._error(
                "Manifest push",
                url,
                status,
                body,
                response_headers,
                blob_size=len(manifest_data),
            )
        return True

    def push_index(self, reference: str, index_data: bytes) -> bool:
        """Push OCI index."""
        url = f"{self.base_url}/{self.repository}/manifests/{reference}"
        headers = {"Content-Type": "application/vnd.oci.image.index.v1+json"}
        status, body, response_headers = self._make_request(
            "PUT", url, data=index_data, headers=headers
        )
        if status not in (200, 201):
            raise self._error(
                "Index push",
                url,
                status,
                body,
                response_headers,
                blob_size=len(index_data),
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
        headers = {"Accept": MANIFEST_ACCEPT}
        status, body, response_headers = self._make_request(
            "GET", url, headers=headers
        )
        if status != 200:
            raise self._error("Manifest pull", url, status, body, response_headers)
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
            preview = _sanitize_text(
                body[:500].decode("utf-8", errors="replace"),
                self._secrets(),
                MAX_ERROR_TEXT_CHARS,
                username=self.username,
            )
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

    def verify_manifest_descriptor(
        self, reference: str, expected_digest: str, expected_size: int, expected_media_type: str
    ) -> None:
        """Verify the manifest descriptor a signer resolves through HEAD."""
        digest_filename(expected_digest)
        if type(expected_size) is not int or expected_size < 0:
            raise ValueError("Expected manifest size must be a non-negative integer.")
        url = f"{self.base_url}/{self.repository}/manifests/{reference}"
        status, _, headers = self._make_request("HEAD", url, headers={"Accept": MANIFEST_ACCEPT})
        if status != 200:
            raise RegistryRequestError(f"Manifest descriptor HEAD failed with HTTP {status}.")
        normalized = {name.lower(): value for name, value in headers.items()}
        try:
            size = int(normalized.get("content-length", ""))
        except (TypeError, ValueError):
            size = None
        media_type = (normalized.get("content-type") or "").split(";", 1)[0].strip().lower()
        if (
            normalized.get("docker-content-digest") != expected_digest
            or size != expected_size
            or media_type != expected_media_type.lower()
        ):
            raise BlobIntegrityError(
                "Registry manifest HEAD descriptor does not match the built digest, size, or media type."
            )

    def _open_blob_response(self, url: str):
        if not _is_safe_https_url(url) and not self._is_unauthenticated_loopback_request(url):
            raise RegistryRequestError(
                "Registry blob requests must use HTTPS without embedded credentials."
            )
        request = urllib.request.Request(
            url, headers=self._authorization_headers(url)
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            self._observe_rate_limit(error.headers, url)
            if error.code not in REDIRECT_STATUSES:
                raise
            location = error.headers.get("Location")
            error.close()
            if not location:
                raise RegistryRequestError(
                    "Registry blob redirect did not include a Location."
                ) from None
            target = urllib.parse.urljoin(url, location)
            if not _is_safe_https_url(target):
                raise RegistryRequestError(
                    "Registry blob redirects must use HTTPS without embedded credentials."
                )
            return _open_registry_request(
                urllib.request.Request(target), timeout=self.timeout
            )

        self._observe_rate_limit(response.headers, url)
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
                    error_url = getattr(error, "url", None) or url
                    if not self._is_registry_url(error_url):
                        self._observe_rate_limit(error.headers, error_url)
                    if _should_retry(error.code, error.headers, attempt):
                        time.sleep(_retry_delay(error.headers, attempt))
                        continue
                    try:
                        error_body = error.read(MAX_ERROR_BODY_BYTES)
                    except OSError:
                        error_body = b""
                    raise self._error(
                        "Blob download",
                        error_url,
                        error.code,
                        error_body,
                        error.headers,
                    ) from None
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
                        f"GET request to {self._safe_request_url(url)} failed after "
                        f"{attempt + 1} attempt(s): {type(error).__name__}."
                    ) from None
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
