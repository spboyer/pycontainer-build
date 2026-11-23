import copy
import hashlib
import io
import json
import sys
import traceback
import urllib.error
from types import SimpleNamespace

import pytest

from pycontainer import cli
from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.oci import (
    OCIIndex,
    build_config_json,
    build_index_json,
    build_manifest_json,
)
from pycontainer.registry_client import (
    RegistryClient, RegistryQuotaError, RegistryRateLimitError, RegistryRequestError,
)


REF_NAME = "org.opencontainers.image.ref.name"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


def _digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _mock_base_image(monkeypatch, base_config, config_bytes=None):
    if config_bytes is None:
        config_bytes = json.dumps(base_config, separators=(",", ":")).encode()
    config_digest = _digest(config_bytes)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": OCI_CONFIG,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [],
        "annotations": {"example.base-manifest": "must-not-be-inherited"},
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    manifest_digest = _digest(manifest_bytes)
    index = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": [{
            "mediaType": OCI_MANIFEST,
            "digest": manifest_digest,
            "size": len(manifest_bytes),
            "platform": {"os": "linux", "architecture": "amd64"},
        }],
        "annotations": {
            REF_NAME: "base:latest",
            "example.base-index": "must-not-be-inherited",
        },
    }

    class FakeRegistryClient:
        def __init__(self, *args, **kwargs):
            pass

        def pull_manifest(self, reference, **kwargs):
            if reference == manifest_digest:
                assert kwargs["expected_digest"] == manifest_digest
                assert kwargs["expected_size"] == len(manifest_bytes)
                return manifest, manifest_digest
            return index, _digest(json.dumps(index).encode())

        def pull_blob(self, digest, path, size):
            assert digest == config_digest
            assert size == len(config_bytes)
            path.write_bytes(config_bytes)

    monkeypatch.setattr("pycontainer.builder.RegistryClient", FakeRegistryClient)
    monkeypatch.setattr(
        "pycontainer.builder.get_registry_auth",
        lambda registry: SimpleNamespace(
            bearer_token=None, username=None, password=None
        ),
    )


def test_single_image_metadata_inheritance_serialization_and_digests(
    tmp_path, monkeypatch, capsys
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": [],
            "Labels": {
                "z-base": "base-only",
                "shared": "base",
                "framework": "base-framework",
            },
            "StopSignal": "SIGQUIT",
            "Volumes": {"/data": {}},
        },
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    original_base_config = copy.deepcopy(base_config)
    _mock_base_image(monkeypatch, base_config)
    config = BuildConfig(
        tag="example:v1",
        context_dir=str(context),
        output_dir=str(tmp_path / "output"),
        use_cache=False,
        labels={"z-label": "label-secret", "shared": "explicit", "a-label": ""},
        manifest_annotations={"z-manifest": "manifest-secret", "a-manifest": ""},
        index_annotations={"z-index": "index-secret", "a-index": ""},
    )
    builder = ImageBuilder(config)
    builder.build()

    output = tmp_path / "output"
    index_bytes = (output / "index.json").read_bytes()
    index = json.loads(index_bytes)
    descriptor = index["manifests"][0]
    manifest_bytes = (
        output / "blobs" / "sha256" / descriptor["digest"].split(":", 1)[1]
    ).read_bytes()
    manifest = json.loads(manifest_bytes)
    config_descriptor = manifest["config"]
    config_bytes = (
        output / "blobs" / "sha256" / config_descriptor["digest"].split(":", 1)[1]
    ).read_bytes()
    image_config = json.loads(config_bytes)

    assert descriptor["digest"] == _digest(manifest_bytes) == builder.manifest_digest
    assert descriptor["size"] == len(manifest_bytes)
    assert config_descriptor["digest"] == _digest(config_bytes) == builder.config_digest
    assert config_descriptor["size"] == len(config_bytes)
    assert index["annotations"] == {
        REF_NAME: "example:v1", "a-index": "", "z-index": "index-secret"
    }
    assert manifest["annotations"] == {"a-manifest": "", "z-manifest": "manifest-secret"}
    assert image_config["config"]["Labels"] == {
        "z-base": "base-only",
        "shared": "explicit",
        "framework": "fastapi",
        "z-label": "label-secret",
        "a-label": "",
    }
    assert image_config["config"]["StopSignal"] == "SIGQUIT"
    assert image_config["config"]["Volumes"] == {"/data": {}}
    assert image_config["rootfs"]["diff_ids"] == [
        layer["digest"] for layer in manifest["layers"]
    ]
    assert base_config == original_base_config

    config.labels = dict(reversed(list(config.labels.items())))
    config.manifest_annotations = dict(reversed(list(config.manifest_annotations.items())))
    config.index_annotations = dict(reversed(list(config.index_annotations.items())))
    builder.build()
    assert (output / "index.json").read_bytes() == index_bytes
    assert builder.manifest_digest == descriptor["digest"]
    assert builder.config_digest == config_descriptor["digest"]
    output_text = capsys.readouterr()
    for value in ("label-secret", "manifest-secret", "index-secret"):
        assert value not in output_text.out + output_text.err


def test_metadata_targets_change_only_their_content_addressed_objects(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], None)
    )
    builder = ImageBuilder(BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"), use_cache=False,
        labels={"example.label": "one"},
        manifest_annotations={"example.manifest": "one"},
        index_annotations={"example.index": "one"},
    ))
    builder.build()
    original_config = builder.config_digest
    original_manifest = builder.manifest_digest
    original_layer = builder.layers[0].digest
    original_index = (tmp_path / "output" / "index.json").read_bytes()

    builder.config.index_annotations["example.index"] = "two"
    builder.build()
    assert builder.config_digest == original_config
    assert builder.manifest_digest == original_manifest
    assert (tmp_path / "output" / "index.json").read_bytes() != original_index

    builder.config.manifest_annotations["example.manifest"] = "two"
    builder.build()
    assert builder.config_digest == original_config
    assert builder.manifest_digest != original_manifest
    assert builder.layers[0].digest == original_layer
    annotated_manifest = builder.manifest_digest

    builder.config.labels["example.label"] = "two"
    builder.build()
    assert builder.config_digest != original_config
    assert builder.manifest_digest != annotated_manifest
    assert builder.layers[0].digest == original_layer


def test_cli_metadata_precedence_and_dry_run_redaction(tmp_path, monkeypatch, capsys, caplog):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        f"""
[build]
context-dir = "{context.as_posix()}"
dry-run = true

[build.labels]
framework = "file-framework"
shared = "file-value"
file-only = "preserved"

[build.manifest_annotations]
shared = "manifest-file"
empty = ""

[build.index_annotations]
shared = "index-file"
""",
        encoding="utf-8",
    )
    builders = []

    class RecordingBuilder(ImageBuilder):
        def __init__(self, config):
            super().__init__(config)
            builders.append(self)

    monkeypatch.setattr(cli, "ImageBuilder", RecordingBuilder)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pycontainer", "build", "--config", str(config_path),
            "--label", "shared=first",
            "--label", "shared=label-secret",
            "--manifest-annotation", "shared=manifest-secret",
            "--index-annotation", "shared=index-secret",
        ],
    )
    cli.main()

    config = builders[0].config
    assert config.labels == {
        "framework": "file-framework",
        "shared": "label-secret",
        "file-only": "preserved",
    }
    assert config.manifest_annotations == {"shared": "manifest-secret", "empty": ""}
    assert config.index_annotations == {"shared": "index-secret"}
    captured = capsys.readouterr()
    for value in ("file-framework", "label-secret", "manifest-secret", "index-secret"):
        assert value not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("field", ["labels", "manifest_annotations", "index_annotations"])
@pytest.mark.parametrize(
    "invalid",
    ["metadata-secret", {"key": ["metadata-secret"]}, {1: "metadata-secret"},
     {"": "metadata-secret"}, {"  ": "metadata-secret"}],
)
def test_invalid_metadata_is_rejected_without_values(field, invalid):
    with pytest.raises(ValueError) as error:
        BuildConfig(**{field: invalid})
    assert "metadata-secret" not in str(error.value)


@pytest.mark.parametrize("field", ["labels", "manifest_annotations", "index_annotations"])
def test_invalid_file_metadata_is_not_hidden_by_cli_override(tmp_path, field):
    path = tmp_path / "pycontainer.toml"
    path.write_text(f"[build.{field}]\nshared = 7\n", encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        config_from_file(path, {field: {"shared": "override"}})


@pytest.mark.parametrize("field", ["manifest_annotations", "index_annotations"])
def test_user_annotation_cannot_set_internal_reference(field):
    with pytest.raises(ValueError, match="protected") as error:
        BuildConfig(**{field: {REF_NAME: "metadata-secret"}})
    assert "metadata-secret" not in str(error.value)


def test_mutated_metadata_is_rejected_before_builder_and_dry_run(tmp_path):
    config = BuildConfig(context_dir=str(tmp_path), dry_run=True)
    config.index_annotations = {REF_NAME: "metadata-secret"}
    with pytest.raises(ValueError, match="protected") as error:
        ImageBuilder(config)
    assert "metadata-secret" not in str(error.value)

    builder = ImageBuilder(BuildConfig(context_dir=str(tmp_path), dry_run=True))
    builder.config.labels = {"example.key": ["metadata-secret"]}
    with pytest.raises(ValueError, match="labels") as error:
        builder.build()
    assert "metadata-secret" not in str(error.value)


def test_reference_name_label_is_not_an_internal_annotation():
    config = BuildConfig(labels={REF_NAME: "label-value"})
    image_config = build_config_json(
        "amd64", "linux", {}, "/app", None, [], labels=config.labels
    )
    assert image_config["config"]["Labels"] == {REF_NAME: "label-value"}


@pytest.mark.parametrize(
    ("option", "argument"),
    [
        ("--label", "metadata-secret"),
        ("--manifest-annotation", "=metadata-secret"),
        ("--index-annotation", f"{REF_NAME}=metadata-secret"),
    ],
)
def test_invalid_cli_metadata_does_not_echo_values(
    monkeypatch, capsys, option, argument
):
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", option, argument])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "metadata-secret" not in captured.out + captured.err


def test_generated_label_collisions_are_explicit_and_value_free():
    from pycontainer.metadata import merge_image_labels

    with pytest.raises(ValueError, match="example.notes") as error:
        merge_image_labels(
            labels={"example.notes": "explicit-secret"},
            generated_labels={"example.notes": "generated-secret"},
        )
    assert "explicit-secret" not in str(error.value)
    assert "generated-secret" not in str(error.value)
    assert merge_image_labels(
        base_labels={"shared": "base"},
        labels={"shared": "explicit"},
        generated_labels={"example.notes": "generated"},
    ) == {"example.notes": "generated", "shared": "explicit"}


def test_annotations_are_separate_and_validated_at_serialization():
    manifest = build_manifest_json("sha256:" + "a" * 64, 2, [])
    index = build_index_json("sha256:" + "b" * 64, 3, "example:v1")
    assert "annotations" not in manifest
    assert index["annotations"] == {REF_NAME: "example:v1"}

    for serializer, args in (
        (build_manifest_json, ("sha256:" + "a" * 64, 2, [])),
        (build_index_json, ("sha256:" + "b" * 64, 3, "example:v1")),
    ):
        with pytest.raises(ValueError, match="protected"):
            serializer(*args, annotations={REF_NAME: "metadata-secret"})
        with pytest.raises(ValueError):
            serializer(*args, annotations={"example.key": 7})

    with pytest.raises(ValueError, match="protected"):
        OCIIndex(2, [], {REF_NAME: "metadata-secret"}).to_json()


def test_null_base_labels_are_accepted_but_invalid_labels_are_rejected():
    base_config = {"config": {"Env": [], "Labels": None}}
    inherited = build_config_json(
        "amd64", "linux", {}, "/app", None, [], base_config=base_config,
    )
    assert inherited["config"]["Labels"] is None
    result = build_config_json(
        "amd64", "linux", {}, "/app", ["python"], [],
        labels={"example.key": "value"}, base_config=base_config,
    )
    assert result["config"]["Labels"] == {"example.key": "value"}
    assert base_config["config"]["Labels"] is None

    base_config["config"]["Labels"] = {"example.key": ["metadata-secret"]}
    with pytest.raises(ValueError) as error:
        build_config_json(
            "amd64", "linux", {}, "/app", None, [], base_config=base_config
        )
    assert "metadata-secret" not in str(error.value)


@pytest.mark.parametrize(
    "operation", ["pull", "push_manifest", "push_index", "blob_head", "upload_init"]
)
@pytest.mark.parametrize(
    "payload",
    [b"metadata-secret", b'{"errors":[{"code":"DENIED","message":"metadata-secret"}]}'],
)
def test_registry_manifest_errors_do_not_echo_response_metadata(monkeypatch, operation, payload):
    client = RegistryClient("registry.example", "example")
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (400, payload, {})
    )
    with pytest.raises(RegistryRequestError, match="HTTP 400") as error:
        if operation == "pull":
            client.pull_manifest("latest")
        elif operation == "blob_head":
            client.blob_exists("sha256:" + "a" * 64)
        elif operation == "upload_init":
            client.initiate_blob_upload()
        else:
            getattr(client, operation)("latest", payload)
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    ("status", "code", "detail", "error_type"),
    [
        (429, "TOOMANYREQUESTS", "metadata-secret", RegistryRateLimitError),
        (400, "DENIED", "storage quota exceeded metadata-secret", RegistryQuotaError),
    ],
)
def test_registry_classification_keeps_metadata_values_hidden(
    monkeypatch, status, code, detail, error_type
):
    client = RegistryClient("registry.example", "example")
    payload = json.dumps({"errors": [{"code": code, "message": detail}]}).encode()
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (status, payload, {})
    )
    with pytest.raises(error_type) as error:
        client.push_manifest("latest", b"{}")
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_invalid_manifest_json_does_not_echo_annotation_values(monkeypatch):
    client = RegistryClient("registry.example", "example")
    invalid = b'{"annotations":{"example.key":"metadata-secret"},'
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (200, invalid, {})
    )
    with pytest.raises(RegistryRequestError, match="invalid manifest JSON") as error:
        client.pull_manifest("latest")
    assert error.value.__context__ is None
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_non_utf8_manifest_is_rejected_without_echoing_annotations(monkeypatch):
    client = RegistryClient("registry.example", "example")
    invalid = b'{"annotations":{"example.key":"metadata-secret\xff"}}'
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (200, invalid, {})
    )
    with pytest.raises(RegistryRequestError, match="expected UTF-8") as error:
        client.pull_manifest("latest")
    assert error.value.__context__ is None
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_invalid_manifest_digest_does_not_echo_registry_header(monkeypatch):
    client = RegistryClient("registry.example", "example")
    monkeypatch.setattr(
        client, "_make_request",
        lambda *args, **kwargs: (
            200, b"{}", {"Docker-Content-Digest": "sha256:metadata-secret"}
        ),
    )
    with pytest.raises(BlobIntegrityError, match="digest does not match") as error:
        client.pull_manifest("latest")
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_registry_blob_upload_error_does_not_echo_label_values(tmp_path, monkeypatch):
    client = RegistryClient("registry.example", "example")
    blob = tmp_path / "config"
    content = b'{"config":{"Labels":{"example.key":"metadata-secret"}}}'
    blob.write_bytes(content)
    monkeypatch.setattr(client, "initiate_blob_upload", lambda: "https://registry.example/upload")
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (400, b"metadata-secret", {})
    )
    with pytest.raises(RegistryRequestError, match="HTTP 400") as error:
        client.upload_blob_monolithic(_digest(content), blob)
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_registry_blob_download_error_does_not_echo_label_values(tmp_path, monkeypatch):
    client = RegistryClient("registry.example", "example")

    def fail_download(*args):
        raise urllib.error.HTTPError(
            "https://registry.example/blob", 400, "Bad Request", {},
            io.BytesIO(b"metadata-secret"),
        )

    monkeypatch.setattr(client, "_download_blob_once", fail_download)
    with pytest.raises(RegistryRequestError, match="HTTP 400") as error:
        client.pull_blob("sha256:" + "a" * 64, tmp_path / "config", 1)
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_invalid_content_length_does_not_echo_header_value(tmp_path, monkeypatch):
    client = RegistryClient("registry.example", "example")

    class Response:
        status = 200
        headers = {"Content-Length": "metadata-secret"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(client, "_open_blob_response", lambda url: Response())
    with pytest.raises(RegistryRequestError, match="invalid blob Content-Length") as error:
        client.pull_blob("sha256:" + "a" * 64, tmp_path / "config", 1)
    assert error.value.__context__ is None
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))


def test_invalid_base_config_does_not_chain_json_payload(tmp_path, monkeypatch):
    invalid = b'{"config":{"Labels":{"example.key":"metadata-secret"},'
    _mock_base_image(monkeypatch, {}, config_bytes=invalid)
    builder = ImageBuilder(BuildConfig(context_dir=str(tmp_path), use_cache=False))
    layers_dir = tmp_path / "blobs"
    layers_dir.mkdir()
    with pytest.raises(RegistryRequestError, match="Base image config is not valid JSON") as error:
        builder._pull_base_image(layers_dir, "linux", "amd64")
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "metadata-secret" not in "".join(traceback.format_exception(error.value))
