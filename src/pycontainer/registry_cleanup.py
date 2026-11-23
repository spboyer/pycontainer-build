"""Registry manifest deletion and conservative semantic-version pruning."""
import fnmatch
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .auth import get_registry_auth
from .blob_store import digest_filename
from .registry_client import (
    RegistryClient,
    RegistryRequestError,
    parse_image_reference,
)

INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MAX_INDEX_DEPTH = 4
MAX_INDEX_MANIFESTS = 1000
SHARED_DIGEST_WARNING = (
    "Deleting a manifest digest removes every tag that points to it. Storage is "
    "reclaimed only by the registry's own garbage collection."
)

_IDENTIFIER = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_IDENTIFIER}(?:\.{_IDENTIFIER})*))?"
)


def semver_key(tag: str) -> Optional[Tuple]:
    """Return a SemVer 2.0.0 precedence key, or None when the tag is not semver."""
    match = _SEMVER.fullmatch(tag)
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        return int(major), int(minor), int(patch), 1, ()
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    )
    return int(major), int(minor), int(patch), 0, identifiers


@dataclass
class TagDecision:
    tag: str
    action: str
    reason: str
    digest: Optional[str] = None


@dataclass
class PrunePlan:
    registry: str
    repository: str
    decisions: List[TagDecision]
    deletions: Dict[str, List[str]] = field(default_factory=dict)


def classify_tags(
    tags: Iterable[str], keep: int, match: str = "*", protect: Sequence[str] = ()
) -> List[TagDecision]:
    """Classify tags without registry access; only semver tags can become candidates."""
    if type(keep) is not int or keep < 1:
        raise ValueError("--keep must be a positive integer.")
    decisions: Dict[str, TagDecision] = {}
    versioned = []
    for tag in tags:
        if not fnmatch.fnmatchcase(tag, match):
            decisions[tag] = TagDecision(tag, "ignore", "does not match pattern")
        elif any(fnmatch.fnmatchcase(tag, pattern) for pattern in protect):
            decisions[tag] = TagDecision(tag, "protect", "protected by --protect")
        else:
            key = semver_key(tag)
            if key is None:
                decisions[tag] = TagDecision(
                    tag, "skip", "not a semantic version; never pruned"
                )
            else:
                versioned.append((key, tag))
    versioned.sort(key=lambda item: item[0], reverse=True)
    if versioned:
        cutoff = versioned[min(keep, len(versioned)) - 1][0]
        for key, tag in versioned:
            if key >= cutoff:
                decisions[tag] = TagDecision(tag, "keep", "within --keep")
            else:
                decisions[tag] = TagDecision(tag, "delete", "older than kept versions")
    order = {tag: index for index, tag in enumerate(decisions)}
    ranked = {tag: index for index, (_, tag) in enumerate(versioned)}
    return sorted(
        decisions.values(),
        key=lambda d: (d.tag not in ranked, ranked.get(d.tag, order[d.tag])),
    )


def _is_index(manifest: Dict) -> bool:
    return manifest.get("mediaType") in INDEX_MEDIA_TYPES or "manifests" in manifest


def _referenced_manifests(
    client: RegistryClient, manifest: Dict, depth: int = 0, seen: Optional[set] = None
) -> List[str]:
    """Return digests of manifests an index references, fetching nested indexes."""
    if not _is_index(manifest):
        return []
    if depth >= MAX_INDEX_DEPTH:
        raise RegistryRequestError(
            f"Image index nesting exceeds {MAX_INDEX_DEPTH} levels; refusing to prune."
        )
    seen = set() if seen is None else seen
    children = manifest.get("manifests")
    if not isinstance(children, list) or len(children) > MAX_INDEX_MANIFESTS:
        raise RegistryRequestError(
            "Image index has an invalid or oversized manifest list; refusing to prune."
        )
    digests = []
    for descriptor in children:
        digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
        size = descriptor.get("size") if isinstance(descriptor, dict) else None
        if not isinstance(digest, str) or (size is not None and type(size) is not int):
            raise RegistryRequestError(
                "Image index contains a malformed descriptor; refusing to prune."
            )
        digests.append(digest)
        media_type = descriptor.get("mediaType")
        if digest in seen or (media_type and media_type not in INDEX_MEDIA_TYPES):
            continue
        seen.add(digest)
        try:
            child, _ = client.pull_manifest(digest, expected_digest=digest, expected_size=size)
        except ValueError as error:
            raise RegistryRequestError(
                f"Image index references an invalid digest: {error}"
            ) from error
        digests.extend(_referenced_manifests(client, child, depth + 1, seen))
    return digests


def plan_prune(
    client: RegistryClient,
    keep: int,
    match: str = "*",
    protect: Sequence[str] = (),
    page_size: Optional[int] = None,
) -> PrunePlan:
    """List and classify tags, then withhold any digest a retained tag still uses."""
    decisions = classify_tags(client.list_tags(page_size), keep, match, protect)
    plan = PrunePlan(client.registry, client.repository, decisions)
    if not any(d.action == "delete" for d in decisions):
        return plan

    # Every tag is resolved because any tag, in any class, may alias a candidate digest.
    manifests = {}
    for decision in decisions:
        manifest, decision.digest = client.pull_manifest(decision.tag)
        try:
            digest_filename(decision.digest)
        except ValueError as error:
            raise RegistryRequestError(
                f"Tag {decision.tag} resolved to unsupported digest "
                f"{decision.digest!r}; refusing to prune."
            ) from error
        manifests[decision.tag] = manifest

    blocked: Dict[str, str] = {}
    for decision in decisions:
        if decision.action == "delete":
            continue
        owner = f"{decision.action} tag {decision.tag}"
        blocked.setdefault(decision.digest, owner)
        for child in _referenced_manifests(client, manifests[decision.tag]):
            blocked.setdefault(child, f"index referenced by {owner}")

    for decision in decisions:
        if decision.action != "delete":
            continue
        if decision.digest in blocked:
            decision.action = "retain"
            decision.reason = f"digest also used by {blocked[decision.digest]}"
        else:
            plan.deletions.setdefault(decision.digest, []).append(decision.tag)
    return plan


def format_plan(plan: PrunePlan, dry_run: bool) -> List[str]:
    title = "Prune plan" + (" (dry run; nothing deleted)" if dry_run else "")
    lines = [f"{title} for {plan.registry}/{plan.repository}:"]
    for decision in plan.decisions:
        digest = f" {decision.digest}" if decision.digest else ""
        lines.append(f"  {decision.action:<8}{decision.tag}{digest} ({decision.reason})")
    lines.append(
        f"{len(plan.deletions)} manifest digest(s) selected for deletion."
    )
    return lines


class RegistryConfirmationError(RuntimeError):
    """Raised when deletion needs confirmation that cannot be obtained."""


def confirm(
    prompt: str,
    assume_yes: bool,
    stdin=None,
    reader: Optional[Callable[[str], str]] = None,
) -> bool:
    """Return True only on explicit confirmation; raise if none is possible."""
    if assume_yes:
        return True
    stdin = sys.stdin if stdin is None else stdin
    if stdin is None or not stdin.isatty():
        raise RegistryConfirmationError(
            "Confirmation is required but standard input is not interactive; "
            "rerun with --yes to confirm deletion."
        )
    try:
        answer = (reader or input)(prompt)
    except EOFError as error:
        raise RegistryConfirmationError(
            "Confirmation was not received; rerun with --yes to confirm deletion."
        ) from error
    return answer.strip().lower() in {"y", "yes"}


def _client(reference_registry: str, repository: str, args) -> RegistryClient:
    auth = get_registry_auth(
        reference_registry, username=args.username, password=args.password
    )
    return RegistryClient(
        reference_registry,
        repository,
        auth_token=auth.bearer_token,
        username=auth.username,
        password=auth.password,
    )


def _explicit_reference(ref: str) -> bool:
    name = ref.split("@", 1)[0]
    return "@" in ref or ":" in name.rsplit("/", 1)[-1]


def run_delete(args, out=print) -> int:
    registry, repository, reference = parse_image_reference(args.reference)
    if not _explicit_reference(args.reference):
        raise ValueError(
            "registry delete requires an explicit tag or @sha256 digest; "
            "refusing to assume :latest."
        )
    client = _client(registry, repository, args)
    expected = reference if reference.startswith("sha256:") else None
    _, digest = client.pull_manifest(reference, expected_digest=expected)
    out(f"{registry}/{repository}:{reference} resolves to {digest}.")
    out(f"Warning: {SHARED_DIGEST_WARNING}")
    if args.dry_run:
        out(f"Dry run: would delete {registry}/{repository}@{digest}.")
        return 0
    if not confirm(
        f"Delete {registry}/{repository}@{digest} and every tag pointing to it? [y/N]: ",
        args.yes,
    ):
        out("Aborted; nothing deleted.")
        return 1
    client.delete_manifest(digest)
    out(f"Deleted {registry}/{repository}@{digest}.")
    return 0


def run_prune(args, out=print) -> int:
    if _explicit_reference(args.repository):
        raise ValueError("registry prune takes a repository without a tag or digest.")
    registry, repository, _ = parse_image_reference(args.repository)
    client = _client(registry, repository, args)
    plan = plan_prune(client, args.keep, args.match, args.protect or (), args.page_size)
    for line in format_plan(plan, not args.execute):
        out(line)
    if not args.execute or not plan.deletions:
        return 0
    out(f"Warning: {SHARED_DIGEST_WARNING}")
    if not confirm(
        f"Delete {len(plan.deletions)} manifest digest(s) from "
        f"{registry}/{repository}? [y/N]: ",
        args.yes,
    ):
        out("Aborted; nothing deleted.")
        return 1
    deleted = 0
    for digest, tags in plan.deletions.items():
        try:
            client.delete_manifest(digest)
        except RegistryRequestError as error:
            raise type(error)(
                f"{error} Deleted {deleted} of {len(plan.deletions)} digest(s) "
                "before this failure; no further deletions were attempted."
            ) from error
        deleted += 1
        out(f"Deleted {digest} (tags: {', '.join(tags)}).")
    return 0
