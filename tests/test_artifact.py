"""Generic OCI 1.1 artifact shape, local layout and synthetic-registry round trips."""
import base64
import hashlib
import http.server
import json
import threading
import urllib.parse
import uuid

import pytest

from pycontainer.artifact import (
    EMPTY_DIGEST,
    EMPTY_MEDIA_TYPE,
    REFERRERS_NOT_FOUND,
    SUBJECT_CONFIRMED,
    SUBJECT_UNCONFIRMED,
    ArtifactPublicationError,
    Descriptor,
    InvalidArtifactError,
    UnsupportedArtifactError,
    build_artifact_manifest,
    list_referrers,
    parse_artifact_manifest,
    pull_artifact,
    push_artifact,
    read_artifact_layout,
    write_artifact_layout,
)
from pycontainer.blob_store import BlobIntegrityError
from pycontainer.registry_client import RegistryClient, RegistryRequestError


MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
ARTIFACT_TYPE = "application/vnd.example.report.v1"
PAYLOAD_TYPE = "application/vnd.example.report.v1+json"


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _payload(tmp_path, data=b'{"ok":true}\n', name="payload.json"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _manifest(**overrides):
    payload = b"payload"
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": EMPTY_MEDIA_TYPE, "digest": EMPTY_DIGEST, "size": 2},
        "layers": [{"mediaType": PAYLOAD_TYPE, "digest": _digest(payload), "size": len(payload)}],
    }
    manifest.update(overrides)
    return json.dumps(manifest).encode()


def test_manifest_uses_exact_empty_config_and_typed_payload():
    assert EMPTY_DIGEST == _digest(b"{}")
    subject = Descriptor(MANIFEST, "sha256:" + "a" * 64, 123)
    payload = Descriptor(PAYLOAD_TYPE, "sha256:" + "b" * 64, 5, annotations={"org.opencontainers.image.title": "r.json"})
    data = build_artifact_manifest(ARTIFACT_TYPE, payload, subject, {"purpose": "test"})
    manifest = json.loads(data)
    assert manifest == {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": EMPTY_MEDIA_TYPE, "digest": EMPTY_DIGEST, "size": 2},
        "layers": [payload.to_dict()],
        "subject": {"mediaType": MANIFEST, "digest": "sha256:" + "a" * 64, "size": 123},
        "annotations": {"purpose": "test"},
    }
    parsed = parse_artifact_manifest(data)
    assert parsed.payload == payload and parsed.subject == subject


@pytest.mark.parametrize("media_type", [
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
    MANIFEST,
    INDEX,
    EMPTY_MEDIA_TYPE,
])
def test_runnable_image_media_types_are_rejected(tmp_path, media_type):
    with pytest.raises(UnsupportedArtifactError, match="reserved"):
        write_artifact_layout(tmp_path / "a", _payload(tmp_path), media_type, PAYLOAD_TYPE)
    with pytest.raises(UnsupportedArtifactError, match="reserved"):
        write_artifact_layout(tmp_path / "b", _payload(tmp_path), ARTIFACT_TYPE, media_type)


@pytest.mark.parametrize("data, error", [
    (_manifest(config={"mediaType": "application/vnd.oci.image.config.v1+json", "digest": EMPTY_DIGEST, "size": 2}), UnsupportedArtifactError),
    (_manifest(config={"mediaType": EMPTY_MEDIA_TYPE, "digest": EMPTY_DIGEST, "size": 3}), UnsupportedArtifactError),
    (_manifest(artifactType=None).replace(b'"artifactType": null, ', b""), UnsupportedArtifactError),
    (_manifest(mediaType="application/vnd.docker.distribution.manifest.v2+json"), UnsupportedArtifactError),
    (_manifest(layers=[]), UnsupportedArtifactError),
    (_manifest(subject={"mediaType": MANIFEST, "digest": "sha256:ABC", "size": 1}), InvalidArtifactError),
    (_manifest(subject={"mediaType": MANIFEST, "digest": "sha256:" + "a" * 64, "size": -1}), InvalidArtifactError),
    (_manifest(subject={"mediaType": MANIFEST, "digest": "sha256:" + "a" * 64, "size": True}), InvalidArtifactError),
    (_manifest(subject={"mediaType": PAYLOAD_TYPE, "digest": "sha256:" + "a" * 64, "size": 1}), InvalidArtifactError),
    (_manifest(subject="sha256:" + "a" * 64), InvalidArtifactError),
    (_manifest(schemaVersion=True), InvalidArtifactError),
    (b'{"schemaVersion":2,"schemaVersion":2}', InvalidArtifactError),
])
def test_malformed_or_unsupported_manifests_are_rejected(data, error):
    with pytest.raises(error):
        parse_artifact_manifest(data)


def test_payload_urls_are_unsupported_and_embedded_data_is_verified():
    layer = json.loads(_manifest())["layers"][0]
    with pytest.raises(UnsupportedArtifactError, match="URLs"):
        parse_artifact_manifest(_manifest(layers=[{**layer, "urls": ["https://example.test/x"]}]))
    with pytest.raises(BlobIntegrityError, match="embedded data"):
        parse_artifact_manifest(_manifest(layers=[{**layer, "data": base64.b64encode(b"other").decode()}]))
    config = {"mediaType": EMPTY_MEDIA_TYPE, "digest": EMPTY_DIGEST, "size": 2, "data": "e30="}
    assert parse_artifact_manifest(_manifest(config=config)).artifact_type == ARTIFACT_TYPE


def test_local_layout_round_trip_with_subject(tmp_path):
    subject = {"mediaType": INDEX, "digest": "sha256:" + "c" * 64, "size": 400}
    artifact = write_artifact_layout(
        tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE,
        subject=subject, annotations={"k": "v"}, tag="report",
    )
    assert artifact.payload_path.read_bytes() == b'{"ok":true}\n'
    assert artifact.subject.to_dict() == subject
    assert artifact.descriptor.artifact_type == ARTIFACT_TYPE
    index = json.loads((tmp_path / "layout" / "index.json").read_bytes())
    assert index["manifests"] == [{
        "mediaType": MANIFEST, "digest": artifact.descriptor.digest,
        "size": artifact.descriptor.size, "artifactType": ARTIFACT_TYPE,
        "annotations": {"org.opencontainers.image.ref.name": "report"},
    }]
    assert (tmp_path / "layout" / "blobs" / "sha256" / EMPTY_DIGEST[7:]).read_bytes() == b"{}"
    for reference in ("report", artifact.descriptor.digest, None):
        assert read_artifact_layout(tmp_path / "layout", reference) == artifact


def test_rewriting_a_tag_replaces_only_that_index_entry(tmp_path):
    layout = tmp_path / "layout"
    write_artifact_layout(layout, _payload(tmp_path, b"one"), ARTIFACT_TYPE, PAYLOAD_TYPE, tag="a")
    write_artifact_layout(layout, _payload(tmp_path, b"two"), ARTIFACT_TYPE, PAYLOAD_TYPE, tag="b")
    newest = write_artifact_layout(layout, _payload(tmp_path, b"three"), ARTIFACT_TYPE, PAYLOAD_TYPE, tag="a")
    refs = [m["annotations"]["org.opencontainers.image.ref.name"] for m in json.loads((layout / "index.json").read_bytes())["manifests"]]
    assert sorted(refs) == ["a", "b"]
    assert read_artifact_layout(layout, "a") == newest
    with pytest.raises(InvalidArtifactError, match="matched 2"):
        read_artifact_layout(layout)


@pytest.mark.parametrize("target", ["payload", "manifest", "config"])
def test_local_layout_detects_corrupt_blobs(tmp_path, target):
    artifact = write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    blobs = tmp_path / "layout" / "blobs" / "sha256"
    path = {
        "payload": artifact.payload_path,
        "manifest": blobs / artifact.descriptor.digest[7:],
        "config": blobs / EMPTY_DIGEST[7:],
    }[target]
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(BlobIntegrityError, match="missing or corrupt"):
        read_artifact_layout(tmp_path / "layout")


def test_local_layout_bounds_and_rejects_non_layouts(tmp_path):
    with pytest.raises(InvalidArtifactError, match="exceeds 3 bytes"):
        write_artifact_layout(tmp_path / "a", _payload(tmp_path, b"four"), ARTIFACT_TYPE, PAYLOAD_TYPE, max_payload_bytes=3)
    assert not list((tmp_path / "a" / "blobs" / "sha256").iterdir())
    other = tmp_path / "other"
    other.mkdir()
    (other / "file").write_text("x")
    with pytest.raises(FileNotFoundError):
        write_artifact_layout(other, _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    with pytest.raises(FileNotFoundError):
        write_artifact_layout(tmp_path / "b", tmp_path / "missing", ARTIFACT_TYPE, PAYLOAD_TYPE)
    with pytest.raises(InvalidArtifactError, match="tag"):
        write_artifact_layout(tmp_path / "c", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE, tag="bad tag")


def test_artifact_layout_is_not_accepted_as_a_runnable_base_image(tmp_path):
    from pycontainer.builder import ImageBuilder
    from pycontainer.config import BuildConfig

    layout = tmp_path / "artifact"
    write_artifact_layout(layout, _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE, tag="report")
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hi')\n")
    config = BuildConfig(
        tag="test:latest", context_dir=str(context), output_dir=str(tmp_path / "out"),
        include_paths=["app.py"], use_cache=False, base_image=f"oci:{layout}:report",
    )
    with pytest.raises(RegistryRequestError, match="unsupported media type 'application/vnd.oci.empty.v1\\+json'"):
        ImageBuilder(config).build()


class _Registry(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _RegistryHandler)
        self.blobs = {}
        self.manifests = {}
        self.uploads = set()
        self.referrers = True
        self.oci_subject = True
        self.server_filter = True
        self.page_size = None
        self.corrupt_blobs = set()
        self.fail_tags = set()
        self.unverifiable_tags = set()
        self.wrong_content_type = False
        self.next_link = None


class _RegistryHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, body=b"", headers=None, head=False):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _parts(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed, parsed.path.split("/")

    def _manifest(self, reference, head):
        stored = self.server.manifests.get(reference)
        if stored is None:
            return self._send(404, head=head)
        data, media_type = stored
        if self.server.wrong_content_type:
            media_type = "application/json"
        self._send(200, data, {"Content-Type": media_type, "Docker-Content-Digest": _digest(data)}, head)

    def do_HEAD(self):
        _, parts = self._parts()
        if parts[-2] == "blobs":
            data = self.server.blobs.get(parts[-1])
            if data is None:
                return self._send(404, head=True)
            if parts[-1] in self.server.corrupt_blobs:
                data = data + b"x"
            return self._send(200, data, {"Docker-Content-Digest": parts[-1]}, head=True)
        return self._manifest(parts[-1], True)

    def do_GET(self):
        parsed, parts = self._parts()
        if parts[-2] == "blobs":
            data = self.server.blobs.get(parts[-1])
            if data is None:
                return self._send(404)
            if parts[-1] in self.server.corrupt_blobs:
                data = bytes([data[0] ^ 1]) + data[1:]
            return self._send(200, data)
        if parts[-2] == "referrers":
            return self._referrers(parsed, parts[-1])
        return self._manifest(parts[-1], False)

    def _referrers(self, parsed, subject):
        if not self.server.referrers:
            return self._send(404)
        query = urllib.parse.parse_qs(parsed.query)
        wanted = query.get("artifactType", [None])[0]
        found = {}
        for data, media_type in self.server.manifests.values():
            manifest = json.loads(data)
            if (manifest.get("subject") or {}).get("digest") != subject:
                continue
            if wanted and self.server.server_filter and manifest.get("artifactType") != wanted:
                continue
            found[_digest(data)] = {
                "mediaType": media_type, "digest": _digest(data), "size": len(data),
                "artifactType": manifest.get("artifactType"),
            }
        items = sorted(found.values(), key=lambda item: item["digest"])
        start = int(query.get("last", ["0"])[0])
        headers = {"Content-Type": INDEX}
        if wanted and self.server.server_filter:
            headers["OCI-Filters-Applied"] = "artifactType"
        if self.server.page_size:
            end = start + self.server.page_size
            if end < len(items):
                next_query = urllib.parse.urlencode({**{k: v[0] for k, v in query.items()}, "last": end})
                headers["Link"] = f'<{parsed.path}?{next_query}>; rel="next"'
            items = items[start:end]
        if self.server.next_link:
            headers["Link"] = self.server.next_link
        body = json.dumps({"schemaVersion": 2, "mediaType": INDEX, "manifests": items}).encode()
        self._send(200, body, headers)

    def do_POST(self):
        upload = uuid.uuid4().hex
        self.server.uploads.add(upload)
        self._send(202, headers={"Location": f"/v2/uploads/{upload}"})

    def do_PUT(self):
        parsed, parts = self._parts()
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if parts[-2] == "uploads":
            digest = urllib.parse.parse_qs(parsed.query)["digest"][0]
            if parts[-1] not in self.server.uploads or _digest(data) != digest:
                return self._send(400)
            self.server.blobs[digest] = data
            return self._send(201)
        reference = parts[-1]
        if reference in self.server.fail_tags:
            return self._send(500, b"boom")
        if reference in self.server.unverifiable_tags:
            self.server.manifests[reference] = (b"{}", MANIFEST)
            return self._send(201)
        digest = _digest(data)
        if reference.startswith("sha256:") and reference != digest:
            return self._send(400)
        self.server.manifests[reference] = (data, self.headers["Content-Type"])
        self.server.manifests[digest] = (data, self.headers["Content-Type"])
        headers = {"Docker-Content-Digest": digest}
        subject = json.loads(data).get("subject")
        if subject and self.server.oci_subject:
            headers["OCI-Subject"] = subject["digest"]
        self._send(201, headers=headers)


@pytest.fixture
def registry():
    server = _Registry()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RegistryClient("registry.example", "team/artifacts", timeout=5)
    client.base_url = f"http://127.0.0.1:{server.server_address[1]}/v2"
    try:
        yield server, client
    finally:
        server.shutdown()
        server.server_close()


def _push_subject_and_referrer(tmp_path, client, artifact_type=ARTIFACT_TYPE):
    base = write_artifact_layout(tmp_path / "base", _payload(tmp_path, b"base"), "application/vnd.example.base", PAYLOAD_TYPE)
    push_artifact(client, tmp_path / "base")
    referrer = write_artifact_layout(
        tmp_path / "ref", _payload(tmp_path, b"ref"), artifact_type, PAYLOAD_TYPE,
        subject=base.descriptor,
    )
    return base, referrer, push_artifact(client, tmp_path / "ref", tags=["r1", "r2"])


def test_registry_round_trip_with_subject_and_referrers(tmp_path, registry):
    server, client = registry
    base, referrer, result = _push_subject_and_referrer(tmp_path, client)
    assert result.subject_status == SUBJECT_CONFIRMED
    assert result.tags == ("r1", "r2")
    assert result.reference == f"registry.example/team/artifacts@{referrer.descriptor.digest}"

    found = list_referrers(client, base.descriptor.digest, ARTIFACT_TYPE)
    assert found.supported and found.registry_filter_applied
    assert [item.digest for item in found.manifests] == [referrer.descriptor.digest]
    assert found.manifests[0].artifact_type == ARTIFACT_TYPE

    pulled = pull_artifact(
        client, "r2", tmp_path / "pulled", expected_artifact_type=ARTIFACT_TYPE,
        expected_subject_digest=base.descriptor.digest,
    )
    assert pulled.descriptor == referrer.descriptor
    assert pulled.payload_path.read_bytes() == b"ref"
    assert read_artifact_layout(tmp_path / "pulled", "r2") == pulled
    by_digest = pull_artifact(client, referrer.descriptor.digest, tmp_path / "pulled")
    assert by_digest.descriptor == referrer.descriptor
    with pytest.raises(InvalidArtifactError, match="expected subject"):
        pull_artifact(client, "r1", tmp_path / "other", expected_subject_digest="sha256:" + "f" * 64)


def test_referrers_client_filter_and_pagination(tmp_path, registry):
    server, client = registry
    server.server_filter = False
    server.page_size = 1
    base, referrer, _ = _push_subject_and_referrer(tmp_path, client)
    other = write_artifact_layout(tmp_path / "other", _payload(tmp_path, b"other"), "application/vnd.example.other", PAYLOAD_TYPE, subject=base.descriptor)
    push_artifact(client, tmp_path / "other")
    everything = list_referrers(client, base.descriptor.digest)
    assert everything.pages == 2
    assert {item.digest for item in everything.manifests} == {referrer.descriptor.digest, other.descriptor.digest}
    filtered = list_referrers(client, base.descriptor.digest, ARTIFACT_TYPE)
    assert not filtered.registry_filter_applied
    assert [item.digest for item in filtered.manifests] == [referrer.descriptor.digest]


@pytest.mark.parametrize("link", [
    '<https://attacker.example/v2/team/artifacts/referrers/{d}>; rel="next"',
    '</v2/team/other/referrers/{d}>; rel="next"',
    '</v2/team/artifacts/referrers/{d}>; rel="next"',
])
def test_referrers_pagination_cannot_leave_the_listing(tmp_path, registry, link):
    server, client = registry
    base, _, _ = _push_subject_and_referrer(tmp_path, client)
    server.next_link = link.format(d=base.descriptor.digest)
    with pytest.raises(RegistryRequestError, match="(?i)referrers pagination"):
        list_referrers(client, base.descriptor.digest)


def test_registry_without_referrers_reports_unconfirmed_and_not_found(tmp_path, registry):
    server, client = registry
    server.referrers = False
    server.oci_subject = False
    base, _, result = _push_subject_and_referrer(tmp_path, client)
    assert result.subject_status == SUBJECT_UNCONFIRMED
    found = list_referrers(client, base.descriptor.digest)
    assert found.status == REFERRERS_NOT_FOUND and not found.supported and found.manifests == ()


def test_push_requires_existing_subject_unless_disabled(tmp_path, registry):
    _, client = registry
    subject = {"mediaType": MANIFEST, "digest": "sha256:" + "d" * 64, "size": 10}
    write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE, subject=subject)
    with pytest.raises(RegistryRequestError, match="HTTP 404"):
        push_artifact(client, tmp_path / "layout")
    assert push_artifact(client, tmp_path / "layout", verify_subject=False).subject_status == SUBJECT_CONFIRMED


def test_pull_rejects_corrupt_payload_without_publishing(tmp_path, registry):
    server, client = registry
    artifact = write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    push_artifact(client, tmp_path / "layout", tags=["v1"])
    server.corrupt_blobs.add(artifact.payload.digest)
    with pytest.raises(BlobIntegrityError, match="hashes to"):
        pull_artifact(client, "v1", tmp_path / "pulled")
    assert not (tmp_path / "pulled" / "blobs" / "sha256" / artifact.payload.digest[7:]).exists()
    assert not (tmp_path / "pulled" / "index.json").exists()


def test_pull_rejects_wrong_content_type_and_image_manifests(tmp_path, registry):
    server, client = registry
    write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    push_artifact(client, tmp_path / "layout", tags=["v1"])
    server.wrong_content_type = True
    with pytest.raises(UnsupportedArtifactError, match="not an OCI image manifest"):
        pull_artifact(client, "v1", tmp_path / "pulled")
    server.wrong_content_type = False
    image = json.dumps({
        "schemaVersion": 2, "mediaType": MANIFEST,
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:" + "e" * 64, "size": 9},
        "layers": [],
    }).encode()
    server.manifests["image"] = (image, MANIFEST)
    with pytest.raises(UnsupportedArtifactError, match="artifactType"):
        pull_artifact(client, "image", tmp_path / "pulled")
    wrong_digest = "sha256:" + "0" * 64
    server.manifests[wrong_digest] = (image, MANIFEST)
    with pytest.raises(BlobIntegrityError):
        pull_artifact(client, wrong_digest, tmp_path / "pulled")


def test_manifest_and_tag_publication_failures_are_explicit(tmp_path, registry):
    server, client = registry
    artifact = write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    server.fail_tags.add("broken")
    with pytest.raises(ArtifactPublicationError, match="tag 'broken' failed") as error:
        push_artifact(client, tmp_path / "layout", tags=["ok", "broken"])
    assert error.value.published_tags == ("ok",)
    server.fail_tags.discard("broken")
    server.unverifiable_tags.add("ghost")
    with pytest.raises(ArtifactPublicationError, match="could not be verified") as error:
        push_artifact(client, tmp_path / "layout", tags=["ok", "ghost"])
    assert error.value.published_tags == ("ok", "ghost")
    server.fail_tags.add(artifact.descriptor.digest)
    with pytest.raises(RegistryRequestError, match="HTTP 500"):
        push_artifact(client, tmp_path / "layout")


def test_existing_registry_blob_with_wrong_size_blocks_publication(tmp_path, registry):
    server, client = registry
    artifact = write_artifact_layout(tmp_path / "layout", _payload(tmp_path), ARTIFACT_TYPE, PAYLOAD_TYPE)
    server.blobs[artifact.payload.digest] = b"short"
    server.corrupt_blobs.add(artifact.payload.digest)
    with pytest.raises(BlobIntegrityError, match="Content-Length"):
        push_artifact(client, tmp_path / "layout")
    assert artifact.descriptor.digest not in server.manifests


def test_oversized_manifest_response_is_rejected(tmp_path, registry, monkeypatch):
    server, client = registry
    server.manifests["huge"] = (b" " * 5000, MANIFEST)
    with pytest.raises(RegistryRequestError, match="exceeded 4096 bytes"):
        client.fetch_manifest_bytes("huge", MANIFEST, 4096)
