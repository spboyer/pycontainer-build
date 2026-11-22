"""Detect and redact package-index credentials in text destined for images or errors."""
import re
from typing import Iterable, List, Mapping, Set, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlsplit

REDACTED = "****"
# pip expands only ${NAME} placeholders with these characters in requirements files.
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_SECRET_QUERY_NAME = re.compile(
    r"token|key|secret|passw|pwd|auth|sig|credential", re.IGNORECASE
)
_URL_ENVIRONMENT = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_MIN_SECRET_LENGTH = 4


def _split_authority(url: str) -> Tuple[str, str, str]:
    scheme, rest = url.split("://", 1)
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    return scheme, authority, rest[len(authority):]


def _userinfo_is_secret(userinfo: str) -> bool:
    user, separator, password = userinfo.partition(":")
    if PLACEHOLDER.sub("", password):
        return True
    literal_user = PLACEHOLDER.sub("", user)
    if not separator:
        # A bare username is commonly a token; "git" is the conventional VCS user.
        return bool(literal_user) and user != "git"
    return not password and bool(literal_user)


def _secret_query(url: str) -> bool:
    try:
        query = urlsplit(url).query
    except ValueError:
        return False
    return any(
        value and _SECRET_QUERY_NAME.search(unquote(name))
        for name, value in parse_qsl(query, keep_blank_values=True)
    )


def url_has_credentials(url: str) -> bool:
    if "://" not in url:
        return False
    _, authority, _ = _split_authority(url)
    if "@" in authority and _userinfo_is_secret(authority.rsplit("@", 1)[0]):
        return True
    return _secret_query(url)


def describe_url(url: str) -> str:
    """Scheme and host only; paths may also carry tokens."""
    scheme, authority, _ = _split_authority(url)
    return f"{scheme}://{authority.rsplit('@', 1)[-1]}"


def find_credentials(text: str) -> List[Tuple[int, str]]:
    """Return (line number, redacted URL) for URLs carrying literal credentials."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _URL.finditer(line):
            if url_has_credentials(match.group(0)):
                findings.append((number, describe_url(match.group(0))))
    return findings


def _redact_url(match: "re.Match[str]") -> str:
    url = match.group(0)
    scheme, authority, remainder = _split_authority(url)
    if "@" in authority:
        userinfo, host = authority.rsplit("@", 1)
        if _userinfo_is_secret(userinfo):
            authority = f"{REDACTED}@{host}"
    remainder = re.sub(
        r"([?&])([^=&#\s]+)=([^&#\s]+)",
        lambda part: (
            f"{part.group(1)}{part.group(2)}={REDACTED}"
            if _SECRET_QUERY_NAME.search(unquote(part.group(2)))
            else part.group(0)
        ),
        remainder,
    )
    return f"{scheme}://{authority}{remainder}"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    text = _URL.sub(_redact_url, text)
    values: Set[str] = set()
    for secret in secrets:
        if len(secret) >= _MIN_SECRET_LENGTH:
            values.update({secret, quote(secret, safe="")})
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def known_secrets(
    environment: Mapping[str, str], placeholder_names: Iterable[str] = ()
) -> Set[str]:
    """Credential values the builder can see without reading credential stores."""
    secrets = {
        environment[name]
        for name in placeholder_names
        if environment.get(name)
    }
    for name in _URL_ENVIRONMENT:
        for url in _URL.findall(environment.get(name, "")):
            _, authority, _ = _split_authority(url)
            if "@" not in authority:
                continue
            user, _, password = authority.rsplit("@", 1)[0].partition(":")
            for part in (password,) if password else (user,):
                if part:
                    secrets.update({part, unquote(part)})
    return secrets
