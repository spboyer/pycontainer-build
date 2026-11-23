"""Pull-through registry mirrors for base-image pulls."""
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .registry_client import (
    RegistryClient,
    RegistryConnectionError,
    RegistryQuotaError,
    RegistryRateLimitError,
    RegistryRequestError,
)

logger = logging.getLogger(__name__)

# Statuses after which this endpoint is skipped for the rest of the pull.
_ENDPOINT_FAILURE_STATUSES = frozenset({401, 403})


def _fallback_reason(error: RegistryRequestError) -> Optional[str]:
    """Return why a mirror error permits trying the next endpoint, if it does.

    Integrity, rate-limit, quota, malformed-response and transport-policy
    failures are never eligible so a misbehaving mirror cannot be silently skipped.
    """
    if isinstance(error, (RegistryRateLimitError, RegistryQuotaError)):
        return None
    if isinstance(error, RegistryConnectionError):
        return "connection failed"
    status = error.status
    if status is None:
        return None
    if status in _ENDPOINT_FAILURE_STATUSES:
        return f"HTTP {status} (check credentials for this mirror)"
    if status == 404:
        return "HTTP 404"
    if 500 <= status <= 599:
        return f"HTTP {status} after retries"
    return None


class MirroredPullClient:
    """Read manifests and blobs from ordered mirrors, then the upstream.

    Each endpoint is a separate :class:`RegistryClient` created lazily with its
    own credentials, so upstream credentials are never resolved for or sent to
    a mirror. Everything is fetched by the same reference and verified exactly
    as for a direct pull; a 404 only skips the endpoint for that object.
    """

    def __init__(
        self,
        upstream: str,
        endpoints: Sequence[Tuple[str, Callable[[], RegistryClient]]],
    ):
        if not endpoints:
            raise ValueError("At least one registry endpoint is required.")
        self.upstream = upstream
        self._endpoints = list(endpoints)
        self._clients: Dict[int, RegistryClient] = {}
        self._unavailable: Set[int] = set()
        self.serving_hosts: List[str] = []

    def _client(self, index: int) -> RegistryClient:
        if index not in self._clients:
            self._clients[index] = self._endpoints[index][1]()
        return self._clients[index]

    def _call(self, description: str, operation):
        last = len(self._endpoints) - 1
        for index, (host, _) in enumerate(self._endpoints):
            if index in self._unavailable:
                continue
            try:
                result = operation(self._client(index))
            except RegistryRequestError as error:
                reason = _fallback_reason(error) if index != last else None
                if reason is None:
                    raise
                if isinstance(error, RegistryConnectionError) or (
                    error.status in _ENDPOINT_FAILURE_STATUSES
                    or error.status >= 500
                ):
                    self._unavailable.add(index)
                logger.warning(
                    "Registry mirror %s for %s failed for %s (%s); trying the "
                    "next mirror or the upstream registry.",
                    host,
                    self.upstream,
                    description,
                    reason,
                )
                continue
            logger.debug("Pulled %s from %s", description, host)
            if host not in self.serving_hosts:
                self.serving_hosts.append(host)
            return result
        raise AssertionError("unreachable: the upstream endpoint always returns or raises")

    def pull_manifest(
        self,
        reference: str,
        expected_digest: Optional[str] = None,
        expected_size: Optional[int] = None,
    ):
        return self._call(
            f"manifest {reference}",
            lambda client: client.pull_manifest(
                reference, expected_digest=expected_digest, expected_size=expected_size
            ),
        )

    def pull_blob(self, digest: str, dest_path: Path, expected_size: int) -> bool:
        return self._call(
            f"blob {digest}",
            lambda client: client.pull_blob(digest, dest_path, expected_size),
        )
