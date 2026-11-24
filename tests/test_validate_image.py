import copy
import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

from pycontainer import builder, cache, cli, effective_config, image_validation, registry_client
from pycontainer.fs_utils import output_directory_lock


INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
REF = "org.opencontainers.image.ref.name"
CONTAINERD_NAME = "io.containerd.image.name"
SOURCE = "org.opencontainers.image.source"
VERSION = "org.opencontainers.image.version"


def _run(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *args])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline image validation must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _blob(layout, data, media_type):
    if isinstance(data, dict):
        data = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(data).hexdigest()
    path = layout / "blobs" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"mediaType": media_type, "digest": f"sha256:{digest}", "size": len(data)}


def _path(layout, descriptor):
    return layout / "blobs" / "sha256" / descriptor["digest"].split(":", 1)[1]


def _image(
    layout, *, tag=None, architecture="amd64", config=None, bad_size=None,
    layer_type=LAYER, layer_data=b"synthetic layer",
):
    layer = _blob(layout, layer_data, layer_type)
    image = {
        "architecture": architecture,
        "os": "linux",
        "config": {
            "User": "1000",
            "Entrypoint": ["python"],
            "Cmd": [],
            "Env": ["MODE=production"],
            "Labels": {SOURCE: "https://example.test/repo", VERSION: "1.0"},
        },
        "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
    }
    if config is not None:
        image["config"].update(config)
    config_descriptor = _blob(layout, image, CONFIG)
    if bad_size == "config":
        config_descriptor["size"] += 1
    if bad_size == "layer":
        layer["size"] += 1
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "config": config_descriptor,
        "layers": [layer],
    }
    descriptor = _blob(layout, manifest, MANIFEST)
    descriptor["platform"] = {"os": "linux", "architecture": architecture}
    if tag is not None:
        descriptor["annotations"] = {REF: tag}
    if bad_size == "manifest":
        descriptor["size"] += 1
    return descriptor, config_descriptor, layer


def _layout(layout, descriptors, *, annotation=None):
    layout.mkdir(parents=True, exist_ok=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    index = {"schemaVersion": 2, "mediaType": INDEX, "manifests": descriptors}
    if annotation is not None:
        index["annotations"] = {REF: annotation}
    (layout / "index.json").write_text(json.dumps(index))


def _validate(monkeypatch, layout, *options):
    return _run(monkeypatch, ["validate", "image", "--output", str(layout), *options])


def _report(capsys):
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def _snapshot(layout):
    return {
        str(path.relative_to(layout)): path.read_bytes()
        for path in layout.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("config", "rule", "severity"),
    [
        ({"User": None}, "PCB001", "warning"),
        ({"User": ""}, "PCB001", "warning"),
        ({"User": "0"}, "PCB001", "warning"),
        ({"User": "000:staff"}, "PCB001", "warning"),
        ({"User": "root"}, "PCB001", "warning"),
        ({"User": "0:1000"}, "PCB001", "warning"),
        ({"User": "root:staff"}, "PCB001", "warning"),
        ({"Entrypoint": None, "Cmd": []}, "PCB003", "error"),
        ({"Labels": {VERSION: "1.0"}}, "PCB004", "warning"),
        ({"Labels": {SOURCE: "https://example.test/repo"}}, "PCB004", "warning"),
        ({"Env": ["db_password=value"]}, "PCB005", "error"),
        ({"Env": ["ACCESS_TOKEN=value"]}, "PCB005", "error"),
        ({"Env": ["CLIENT_SECRET=value"]}, "PCB005", "error"),
    ],
)
def test_each_rule_reports_a_stable_id_and_remediation(
    tmp_path, monkeypatch, capsys, config, rule, severity
):
    descriptor, _, _ = _image(tmp_path, config=config)
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", "json") == 1

    report = _report(capsys)
    assert set(report) == {"image", "provenance", "findings"}
    finding = next(item for item in report["findings"] if item["rule_id"] == rule)
    assert set(finding) == {"rule_id", "severity", "message", "remediation"}
    assert finding["severity"] == severity
    assert finding["message"] and finding["remediation"]


def test_clean_config_and_empty_secrets_have_no_findings(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(
        tmp_path, config={"User": "1000:0", "Env": ["PASSWORD=", "TOKEN=", "SECRET="]}
    )
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", "json") == 0

    report = _report(capsys)
    assert report["findings"] == []
    assert report["provenance"]["status"] == "unknown"


@pytest.mark.parametrize(
    "config",
    [
        {"User": "rootless"},
        {"Entrypoint": [], "Cmd": ["python"]},
        {"Entrypoint": ["python"], "Cmd": None},
        {"Env": ["PASSWORD=", "TOKEN=", "SECRET=", "NOT_A_CREDENTIAL=value"]},
    ],
)
def test_non_findings_are_not_reported(tmp_path, monkeypatch, capsys, config):
    descriptor, _, _ = _image(tmp_path, config=config)
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--format", "json") == 0
    assert _report(capsys)["findings"] == []


@pytest.mark.parametrize("format_", ["text", "json"])
def test_secret_values_never_appear_in_output(tmp_path, monkeypatch, capsys, format_):
    secret = "never-print-this-token-123"
    descriptor, _, _ = _image(
        tmp_path, config={"Env": [f"ACCESS_TOKEN={secret}", "PASSWORD=another-value"]}
    )
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", format_) == 1

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "another-value" not in captured.out + captured.err
    assert "PCB005" in captured.out


def test_annotations_are_not_verified_base_provenance(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(
        tmp_path,
        config={
            "Labels": {
                SOURCE: "https://example.test/repo",
                VERSION: "1",
                "org.opencontainers.image.base.name": "python:latest",
                "org.opencontainers.image.base.digest": "sha256:" + "a" * 64,
            }
        },
    )
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", "json", "--ignore", "PCB002") == 0
    report = _report(capsys)
    assert report["provenance"]["status"] == "unknown"
    assert not any(item["rule_id"] == "PCB002" for item in report["findings"])


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_cli_ignores_extend_config_ignores(tmp_path, monkeypatch, capsys, table):
    descriptor, _, _ = _image(
        tmp_path, config={"User": "root", "Env": ["TOKEN=value"], "Labels": {}}
    )
    _layout(tmp_path, [descriptor])
    config = tmp_path / "settings.toml"
    config.write_text(f'[{table}.validate]\nignore = ["PCB001"]\n')

    assert _validate(
        monkeypatch, tmp_path, "--config", str(config), "--ignore", "PCB005", "--format", "json"
    ) == 1

    assert [item["rule_id"] for item in _report(capsys)["findings"]] == ["PCB004"]


@pytest.mark.parametrize(
    "content",
    [
        '[build.validate]\nignore = ["PCB999"]\n',
        '[build.validate]\nignore = "PCB001"\n',
        "[build.validate]\nignore = [42]\n",
        '[build.validate]\nunknown = ["PCB001"]\n',
        '[build]\nvalidate = "invalid"\n',
    ],
)
def test_invalid_validation_configuration_is_an_error(tmp_path, monkeypatch, capsys, content):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    config = tmp_path / "settings.toml"
    config.write_text(content)

    assert _validate(monkeypatch, tmp_path, "--config", str(config)) == 2
    assert "validat" in capsys.readouterr().err.lower()
    assert _run(monkeypatch, ["validate", "config", str(config)]) == 2
    assert "validat" in capsys.readouterr().err.lower()


def test_unknown_cli_ignore_is_an_error(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--ignore", "PCB999") == 2
    assert "PCB999" in capsys.readouterr().err


def test_fail_on_error_still_reports_warnings(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, config={"User": "root"})
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", "json", "--fail-on", "error") == 0

    assert [item["rule_id"] for item in _report(capsys)["findings"]] == ["PCB001"]


def test_fail_on_error_fails_for_an_error_and_keeps_warnings(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, config={"User": "root", "Env": ["TOKEN=value"]})
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--format", "json", "--fail-on", "error") == 1
    assert {item["rule_id"] for item in _report(capsys)["findings"]} == {"PCB001", "PCB005"}


@pytest.mark.parametrize("missing", ["oci-layout", "index.json", "manifest", "config", "layer"])
def test_missing_files_are_unsuppressible_errors(tmp_path, monkeypatch, capsys, missing):
    descriptor, config, layer = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    paths = {
        "oci-layout": tmp_path / "oci-layout",
        "index.json": tmp_path / "index.json",
        "manifest": _path(tmp_path, descriptor),
        "config": _path(tmp_path, config),
        "layer": _path(tmp_path, layer),
    }
    paths[missing].unlink()

    assert _validate(
        monkeypatch, tmp_path, "--format", "json", "--ignore", "PCB001"
    ) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert paths[missing].name in captured.err


@pytest.mark.parametrize("kind", ["manifest", "config", "layer"])
@pytest.mark.parametrize("corruption", ["digest", "size"])
def test_descriptor_digest_and_size_are_verified(tmp_path, monkeypatch, capsys, kind, corruption):
    manifest, config, layer = _image(tmp_path, bad_size=kind if corruption == "size" else None)
    _layout(tmp_path, [manifest])
    if corruption == "digest":
        path = _path(tmp_path, {"manifest": manifest, "config": config, "layer": layer}[kind])
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

    assert _validate(monkeypatch, tmp_path, "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "digest" in captured.err.lower() or "size" in captured.err.lower()


@pytest.mark.parametrize("kind", ["manifest", "config", "layer"])
@pytest.mark.parametrize("corruption", ["digest", "size"])
def test_corrupt_unselected_images_fail_before_linting(
    tmp_path, monkeypatch, capsys, kind, corruption
):
    selected, _, _ = _image(tmp_path, tag="selected", config={"User": "root"})
    other, config, layer = _image(
        tmp_path, tag="other", architecture="arm64", bad_size=kind if corruption == "size" else None
    )
    _layout(tmp_path, [selected, other])
    if corruption == "digest":
        path = _path(tmp_path, {"manifest": other, "config": config, "layer": layer}[kind])
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])

    assert _validate(monkeypatch, tmp_path, "--tag", "selected", "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "PCB001" not in captured.err


def test_selection_requires_an_unambiguous_tag(tmp_path, monkeypatch, capsys):
    first, _, _ = _image(tmp_path, tag="one")
    second, _, _ = _image(tmp_path, tag="two", architecture="arm64")
    _layout(tmp_path, [first, second])

    assert _validate(monkeypatch, tmp_path) == 2
    assert "multiple" in capsys.readouterr().err.lower()
    assert _validate(monkeypatch, tmp_path, "--tag", "missing") == 2
    assert "matching" in capsys.readouterr().err.lower()
    assert _validate(monkeypatch, tmp_path, "--tag", "one") == 0


def test_legacy_index_annotation_and_multiple_tags(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor], annotation="app:latest")
    assert _validate(monkeypatch, tmp_path, "--tag", "app:latest") == 0
    capsys.readouterr()
    assert _validate(monkeypatch, tmp_path, "--tag", f"@{descriptor['digest']}") == 0
    capsys.readouterr()

    first = copy.deepcopy(descriptor)
    second = copy.deepcopy(descriptor)
    first["annotations"] = {REF: "app:one"}
    second["annotations"] = {REF: "app:two"}
    _layout(tmp_path, [first, second])
    assert _validate(monkeypatch, tmp_path) == 2
    assert _validate(monkeypatch, tmp_path, "--tag", "app:two") == 0
    capsys.readouterr()
    assert _validate(monkeypatch, tmp_path, "--tag", f"@{descriptor['digest']}") == 2
    assert "platform" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("location", ["descriptor", "index"])
@pytest.mark.parametrize("both", [False, True])
def test_containerd_name_can_select_a_reference(tmp_path, monkeypatch, capsys, location, both):
    descriptor, _, _ = _image(tmp_path)
    annotations = {CONTAINERD_NAME: "app:latest"}
    if both:
        annotations[REF] = "short"
    if location == "descriptor":
        descriptor["annotations"] = annotations
    _layout(tmp_path, [descriptor])
    if location == "index":
        index_path = tmp_path / "index.json"
        index = json.loads(index_path.read_bytes())
        index["annotations"] = annotations
        index_path.write_text(json.dumps(index))
    assert _validate(monkeypatch, tmp_path, "--tag", "app:latest") == 0
    assert not capsys.readouterr().err
    if both:
        assert _validate(monkeypatch, tmp_path, "--tag", "short") == 0
        assert not capsys.readouterr().err


def test_index_annotation_does_not_select_among_multiple_entries(tmp_path, monkeypatch, capsys):
    first, _, _ = _image(tmp_path, tag="one")
    second, _, _ = _image(tmp_path, tag="two", architecture="arm64")
    _layout(tmp_path, [first, second], annotation="legacy")

    assert _validate(monkeypatch, tmp_path, "--tag", "legacy") == 2
    assert "matching" in capsys.readouterr().err.lower()


def test_nested_index_requires_platform_for_multiple_images(tmp_path, monkeypatch, capsys):
    amd64, _, _ = _image(tmp_path, architecture="amd64")
    arm64, _, _ = _image(tmp_path, architecture="arm64")
    nested = _blob(
        tmp_path, {"schemaVersion": 2, "mediaType": INDEX, "manifests": [amd64, arm64]}, INDEX
    )
    nested["annotations"] = {REF: "app:latest"}
    _layout(tmp_path, [nested])

    assert _validate(monkeypatch, tmp_path, "--tag", "app:latest") == 2
    assert "Available platforms: linux/amd64, linux/arm64." in capsys.readouterr().err
    assert _validate(monkeypatch, tmp_path, "--tag", f"@{arm64['digest']}") == 2
    assert "matching" in capsys.readouterr().err.lower()
    assert _validate(
        monkeypatch, tmp_path, "--tag", "app:latest", "--platform", "linux/s390x"
    ) == 2
    assert "Available platforms: linux/amd64, linux/arm64." in capsys.readouterr().err
    assert _validate(
        monkeypatch, tmp_path, "--tag", "app:latest", "--platform", "linux/arm64", "--format", "json"
    ) == 0
    report = _report(capsys)
    assert report["image"]["digest"] == arm64["digest"]


def test_platform_variant_respects_index_selection(tmp_path, monkeypatch, capsys):
    descriptor, config, _ = _image(tmp_path, architecture="arm64")
    image = json.loads(_path(tmp_path, config).read_bytes())
    image["variant"] = "v8"
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    manifest["config"] = _blob(tmp_path, image, CONFIG)
    replacement = _blob(tmp_path, manifest, MANIFEST)
    replacement["platform"] = descriptor["platform"]
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path, "--platform", "linux/arm64/v8") == 2
    assert "platform" in capsys.readouterr().err.lower()
    assert _validate(monkeypatch, tmp_path, "--platform", "linux/arm64", "--format", "json") == 0
    assert _report(capsys)["image"]["platform"] == "linux/arm64/v8"

    del replacement["platform"]
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path, "--platform", "linux/arm64/v8", "--format", "json") == 0
    assert _report(capsys)["image"]["platform"] == "linux/arm64/v8"


def test_unnamed_platforms_can_be_selected(tmp_path, monkeypatch, capsys):
    amd64, _, _ = _image(tmp_path, architecture="amd64")
    arm64, _, _ = _image(tmp_path, architecture="arm64")
    _layout(tmp_path, [amd64, arm64])

    assert _validate(monkeypatch, tmp_path, "--platform", "linux/arm64", "--format", "json") == 0
    assert _report(capsys)["image"]["digest"] == arm64["digest"]


def test_multiple_candidates_require_platform_metadata(tmp_path, monkeypatch, capsys):
    amd64, _, _ = _image(tmp_path, tag="same", architecture="amd64")
    arm64, _, _ = _image(tmp_path, tag="same", architecture="arm64")
    del amd64["platform"]
    _layout(tmp_path, [amd64, arm64])

    assert _validate(monkeypatch, tmp_path, "--tag", "same", "--platform", "linux/arm64") == 2
    assert "must declare their platforms" in capsys.readouterr().err


def test_reference_ambiguity_uses_the_preferred_descriptor_name(tmp_path, monkeypatch, capsys):
    amd64, _, _ = _image(tmp_path, tag="same", architecture="amd64")
    arm64, _, _ = _image(tmp_path, tag="same", architecture="arm64")
    amd64["annotations"][CONTAINERD_NAME] = "one:latest"
    arm64["annotations"][CONTAINERD_NAME] = "two:latest"
    _layout(tmp_path, [amd64, arm64])

    assert _validate(monkeypatch, tmp_path, "--platform", "linux/arm64", "--format", "json") == 0
    assert _report(capsys)["image"]["digest"] == arm64["digest"]
    assert _validate(monkeypatch, tmp_path, "--tag", "one:latest") == 0


def test_selected_nested_index_is_rejected(tmp_path, monkeypatch, capsys):
    image, _, _ = _image(tmp_path)
    inner = _blob(tmp_path, {"schemaVersion": 2, "mediaType": INDEX, "manifests": [image]}, INDEX)
    outer = _blob(tmp_path, {"schemaVersion": 2, "mediaType": INDEX, "manifests": [inner]}, INDEX)
    _layout(tmp_path, [outer])

    assert _validate(monkeypatch, tmp_path) == 2
    assert "nested index" in capsys.readouterr().err.lower()


def test_available_platforms_escape_untrusted_control_characters(tmp_path, monkeypatch, capsys):
    amd64, _, _ = _image(tmp_path, architecture="amd64")
    other, _, _ = _image(tmp_path, architecture="arm64\x1b[31m")
    nested = _blob(
        tmp_path, {"schemaVersion": 2, "mediaType": INDEX, "manifests": [other, amd64]}, INDEX
    )
    _layout(tmp_path, [nested])

    assert _validate(monkeypatch, tmp_path) == 2
    error = capsys.readouterr().err
    assert "\x1b" not in error
    assert r"linux/arm64\u001b[31m" in error


def test_available_platforms_are_sorted_before_escaping(tmp_path, monkeypatch, capsys):
    non_ascii, _, _ = _image(tmp_path, architecture="\u00e9")
    ascii_, _, _ = _image(tmp_path, architecture="z")
    nested = _blob(
        tmp_path, {"schemaVersion": 2, "mediaType": INDEX, "manifests": [non_ascii, ascii_]}, INDEX
    )
    _layout(tmp_path, [nested])

    assert _validate(monkeypatch, tmp_path) == 2
    assert r"Available platforms: linux/z, linux/\u00e9." in capsys.readouterr().err


def test_validation_never_rewrites_the_layout(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    before = _snapshot(tmp_path)

    assert _validate(monkeypatch, tmp_path) == 0
    capsys.readouterr()
    assert _snapshot(tmp_path) == before


def test_image_validation_does_not_construct_builder_cache_or_registry(
    tmp_path, monkeypatch, capsys
):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])

    def forbidden(*args, **kwargs):
        pytest.fail("offline image validation must not build, cache or access a registry")

    monkeypatch.setattr(cli, "ImageBuilder", forbidden)
    monkeypatch.setattr(cache, "LayerCache", forbidden)
    monkeypatch.setattr(registry_client, "RegistryClient", forbidden)
    assert _validate(monkeypatch, tmp_path) == 0
    capsys.readouterr()


def test_default_text_output_matches_explicit_text(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, config={"User": "root"})
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path) == 1
    default = capsys.readouterr()
    assert _validate(monkeypatch, tmp_path, "--format", "text") == 1
    assert capsys.readouterr() == default


@pytest.mark.parametrize(
    "config",
    [
        {"Env": "TOKEN=do-not-print-this"},
        {"Env": ["MALFORMED"]},
        {"Entrypoint": "python"},
        {"Labels": {SOURCE: 42, VERSION: "1"}},
    ],
)
def test_invalid_runtime_config_fails_without_exposing_values(
    tmp_path, monkeypatch, capsys, config
):
    descriptor, _, _ = _image(tmp_path, config=config)
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "do-not-print-this" not in captured.err


@pytest.mark.parametrize("value", [-1, True, "1"])
def test_invalid_descriptor_size_is_an_error(tmp_path, monkeypatch, capsys, value):
    descriptor, _, _ = _image(tmp_path)
    descriptor["size"] = value
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path) == 2
    assert "size" in capsys.readouterr().err.lower()


def test_mismatching_unselected_platform_is_an_error(tmp_path, monkeypatch, capsys):
    selected, _, _ = _image(tmp_path, tag="selected")
    other, _, _ = _image(tmp_path, tag="other", architecture="arm64")
    other["platform"]["architecture"] = "amd64"
    _layout(tmp_path, [selected, other])
    assert _validate(monkeypatch, tmp_path, "--tag", "selected") == 2
    assert "platform" in capsys.readouterr().err.lower()


def test_rootfs_diffid_count_is_checked(tmp_path, monkeypatch, capsys):
    descriptor, config, _ = _image(tmp_path)
    image = json.loads(_path(tmp_path, config).read_bytes())
    image["rootfs"]["diff_ids"] = []
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    manifest["config"] = _blob(tmp_path, image, CONFIG)
    replacement = _blob(tmp_path, manifest, MANIFEST)
    replacement["platform"] = descriptor["platform"]
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path) == 2
    assert "diffid" in capsys.readouterr().err.lower()


def test_manifest_media_type_must_match_its_descriptor(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path)
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    manifest["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
    replacement = _blob(tmp_path, manifest, MANIFEST)
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path) == 2
    assert "media type" in capsys.readouterr().err.lower()


def test_one_blob_cannot_be_both_index_and_manifest(tmp_path, monkeypatch, capsys):
    child, config, layer = _image(tmp_path)
    hybrid = {
        "schemaVersion": 2,
        "manifests": [child],
        "config": config,
        "layers": [layer],
    }
    index = _blob(tmp_path, hybrid, INDEX)
    index["annotations"] = {REF: "index"}
    manifest = copy.deepcopy(index)
    manifest["mediaType"] = MANIFEST
    manifest["annotations"] = {REF: "image"}
    manifest["platform"] = child["platform"]
    _layout(tmp_path, [index, manifest])
    assert _validate(monkeypatch, tmp_path, "--tag", "image") == 2
    assert "conflicting" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("media_type", [INDEX, MANIFEST, CONFIG])
def test_manifest_or_config_media_types_cannot_hide_inside_a_layer(
    tmp_path, monkeypatch, capsys, media_type
):
    descriptor, _, _ = _image(tmp_path)
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    manifest["layers"][0]["mediaType"] = media_type
    replacement = _blob(tmp_path, manifest, MANIFEST)
    replacement["platform"] = descriptor["platform"]
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path) == 2
    assert "media type" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "content",
    [
        b'{"imageLayoutVersion":"1.0.0","extra":NaN}',
        b'{"imageLayoutVersion":"1.0.0","imageLayoutVersion":"1.0.0"}',
    ],
)
def test_invalid_json_constants_and_duplicate_keys_are_rejected(
    tmp_path, monkeypatch, capsys, content
):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    (tmp_path / "oci-layout").write_bytes(content)
    assert _validate(monkeypatch, tmp_path) == 2
    assert "json" in capsys.readouterr().err.lower()


def test_malformed_config_json_never_echoes_environment_values(tmp_path, monkeypatch, capsys):
    descriptor, _, layer = _image(tmp_path)
    raw = (
        b'{"os":"linux","os":"linux","architecture":"amd64",'
        b'"config":{"Env":["TOKEN=secret-from-malformed-json"]},'
        + b'"rootfs":{"type":"layers","diff_ids":["'
        + layer["digest"].encode()
        + b'"]}}'
    )
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    manifest["config"] = _blob(tmp_path, raw, CONFIG)
    replacement = _blob(tmp_path, manifest, MANIFEST)
    replacement["platform"] = descriptor["platform"]
    _layout(tmp_path, [replacement])
    assert _validate(monkeypatch, tmp_path, "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret-from-malformed-json" not in captured.err


@pytest.mark.parametrize("owner", ["root_index", "manifest", "nested_index"])
def test_corrupt_subject_image_is_verified(tmp_path, monkeypatch, capsys, owner):
    selected, _, _ = _image(tmp_path)
    subject, _, subject_layer = _image(tmp_path, architecture="arm64", layer_data=b"subject layer")
    subject_path = _path(tmp_path, subject_layer)
    data = subject_path.read_bytes()
    subject_path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    if owner == "manifest":
        manifest = json.loads(_path(tmp_path, selected).read_bytes())
        manifest["subject"] = subject
        replacement = _blob(tmp_path, manifest, MANIFEST)
        replacement["platform"] = selected["platform"]
        selected = replacement
    elif owner == "nested_index":
        selected = _blob(
            tmp_path,
            {"schemaVersion": 2, "mediaType": INDEX, "manifests": [selected], "subject": subject},
            INDEX,
        )
    _layout(tmp_path, [selected])
    if owner == "root_index":
        index_path = tmp_path / "index.json"
        index = json.loads(index_path.read_bytes())
        index["subject"] = subject
        index_path.write_text(json.dumps(index))
    assert _validate(monkeypatch, tmp_path, "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "digest" in captured.err.lower()


@pytest.mark.parametrize("failure", ["missing", "size"])
def test_missing_or_wrong_size_subject_is_an_integrity_error(
    tmp_path, monkeypatch, capsys, failure
):
    selected, _, _ = _image(tmp_path)
    subject, _, _ = _image(tmp_path, architecture="arm64", layer_data=b"subject layer")
    if failure == "missing":
        _path(tmp_path, subject).unlink()
    else:
        subject["size"] += 1
    _layout(tmp_path, [selected])
    index_path = tmp_path / "index.json"
    index = json.loads(index_path.read_bytes())
    index["subject"] = subject
    index_path.write_text(json.dumps(index))
    assert _validate(monkeypatch, tmp_path, "--ignore", "PCB001") == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize("value", [2.0, "2", None])
def test_root_index_requires_integer_schema_version(tmp_path, monkeypatch, capsys, value):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    index = json.loads((tmp_path / "index.json").read_bytes())
    index["schemaVersion"] = value
    (tmp_path / "index.json").write_text(json.dumps(index))
    assert _validate(monkeypatch, tmp_path) == 2
    assert "schema" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("value", [[], {}, False, 1, "invalid/type"])
def test_root_index_rejects_invalid_media_types(tmp_path, monkeypatch, capsys, value):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    index = json.loads((tmp_path / "index.json").read_bytes())
    index["mediaType"] = value
    (tmp_path / "index.json").write_text(json.dumps(index))
    assert _validate(monkeypatch, tmp_path) == 2
    assert "media type" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("option", ["--output", "--tag", "--platform", "--config"])
def test_empty_selection_options_are_errors(tmp_path, monkeypatch, capsys, option):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, option, "") == 2
    assert capsys.readouterr().err


def test_metadata_size_limit_accepts_boundary_and_rejects_larger(
    tmp_path, monkeypatch, capsys
):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    marker = tmp_path / "oci-layout"
    original = marker.read_bytes()
    marker.write_bytes(original + b" " * (image_validation.MAX_METADATA_BYTES - len(original)))
    assert _validate(monkeypatch, tmp_path) == 0
    capsys.readouterr()
    with marker.open("ab") as stream:
        stream.write(b" ")
    assert _validate(monkeypatch, tmp_path) == 2
    assert "size limit" in capsys.readouterr().err.lower()


def test_index_entry_limit_accepts_boundary_and_rejects_larger(tmp_path, monkeypatch, capsys):
    selected, _, _ = _image(tmp_path, tag="selected")
    other, _, _ = _image(tmp_path, tag="other", architecture="arm64")
    _layout(tmp_path, [selected] + [other] * (image_validation.MAX_INDEX_ENTRIES - 1))
    assert _validate(monkeypatch, tmp_path, "--tag", "selected") == 0
    capsys.readouterr()
    _layout(tmp_path, [selected] + [other] * image_validation.MAX_INDEX_ENTRIES)
    assert _validate(monkeypatch, tmp_path, "--tag", "selected") == 2
    assert "count limit" in capsys.readouterr().err.lower()


def test_layer_count_limit_accepts_boundary_and_rejects_larger(tmp_path, monkeypatch, capsys):
    descriptor, config, layer = _image(tmp_path)
    manifest = json.loads(_path(tmp_path, descriptor).read_bytes())
    image = json.loads(_path(tmp_path, config).read_bytes())

    def set_layer_count(count):
        image["rootfs"]["diff_ids"] = [layer["digest"]] * count
        manifest["config"] = _blob(tmp_path, image, CONFIG)
        manifest["layers"] = [layer] * count
        replacement = _blob(tmp_path, manifest, MANIFEST)
        replacement["platform"] = descriptor["platform"]
        _layout(tmp_path, [replacement])

    set_layer_count(image_validation.MAX_IMAGE_LAYERS)
    assert _validate(monkeypatch, tmp_path) == 0
    capsys.readouterr()
    set_layer_count(image_validation.MAX_IMAGE_LAYERS + 1)
    assert _validate(monkeypatch, tmp_path) == 2
    assert "layer count limit" in capsys.readouterr().err.lower()


def test_total_descriptor_limit_counts_repeated_references(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, tag="same")
    monkeypatch.setattr(image_validation, "MAX_GRAPH_DESCRIPTORS", 3)
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--tag", "same") == 0
    capsys.readouterr()
    _layout(tmp_path, [descriptor, descriptor])
    assert _validate(monkeypatch, tmp_path, "--tag", "same") == 2
    assert "descriptor graph limit" in capsys.readouterr().err.lower()


def test_total_metadata_limit_counts_verified_unique_blobs(tmp_path, monkeypatch, capsys):
    descriptor, config, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    total = descriptor["size"] + config["size"]
    monkeypatch.setattr(image_validation, "MAX_GRAPH_METADATA_BYTES", total)
    assert _validate(monkeypatch, tmp_path) == 0
    capsys.readouterr()
    monkeypatch.setattr(image_validation, "MAX_GRAPH_METADATA_BYTES", total - 1)
    assert _validate(monkeypatch, tmp_path) == 2
    assert "metadata graph size limit" in capsys.readouterr().err.lower()


def _nested_layout(layout, count, *, duplicate=False):
    selected, _, _ = _image(layout, tag="selected")
    descriptor, _, _ = _image(layout, architecture="arm64")
    leaf = descriptor
    for _ in range(count):
        children = [descriptor, descriptor] if duplicate else [descriptor]
        descriptor = _blob(
            layout, {"schemaVersion": 2, "mediaType": INDEX, "manifests": children}, INDEX
        )
    descriptor["annotations"] = {REF: "other"}
    _layout(layout, [selected, descriptor])
    return leaf


def test_nested_index_depth_is_bounded(tmp_path, monkeypatch, capsys):
    _nested_layout(tmp_path, image_validation.MAX_GRAPH_DEPTH)
    assert _validate(monkeypatch, tmp_path, "--tag", "selected") == 0
    capsys.readouterr()
    _nested_layout(tmp_path, image_validation.MAX_GRAPH_DEPTH + 1)
    assert _validate(monkeypatch, tmp_path, "--tag", "selected") == 2
    assert "nesting limit" in capsys.readouterr().err.lower()


def test_shared_nested_indexes_do_not_expand_exponentially(tmp_path, monkeypatch, capsys):
    _nested_layout(tmp_path, 24, duplicate=True)
    assert _validate(monkeypatch, tmp_path, "--tag", "selected", "--format", "json") == 0
    assert _report(capsys)["findings"] == []


def test_corrupt_deep_unselected_image_fails_before_linting(tmp_path, monkeypatch, capsys):
    leaf = _nested_layout(tmp_path, 3)
    path = _path(tmp_path, leaf)
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])

    assert _validate(monkeypatch, tmp_path, "--tag", "selected", "--format", "json") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "digest mismatch" in captured.err.lower()


@pytest.mark.parametrize("kind", ["marker", "index", "blob", "blobs_dir"])
def test_symlinked_layout_files_and_blob_directories_are_rejected(
    tmp_path, monkeypatch, capsys, kind
):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    paths = {
        "marker": tmp_path / "oci-layout",
        "index": tmp_path / "index.json",
        "blob": _path(tmp_path, descriptor),
        "blobs_dir": tmp_path / "blobs",
    }
    original = paths[kind]
    target = original.with_name(original.name + "-target")
    original.rename(target)
    try:
        original.symlink_to(target, target_is_directory=kind == "blobs_dir")
    except OSError:
        pytest.skip("creating symbolic links is unavailable")
    assert _validate(monkeypatch, tmp_path) == 2
    assert "symbolic link" in (error := capsys.readouterr().err.lower()) or "regular file" in error


def test_nonstandard_layer_media_type_is_still_verified(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(
        tmp_path, layer_type="application/vnd.oci.image.layer.nondistributable.v1.tar"
    )
    _layout(tmp_path, [descriptor])

    assert _validate(monkeypatch, tmp_path, "--format", "json") == 0
    assert _report(capsys)["findings"] == []


def _build_fixture(tmp_path, monkeypatch, *, user="1000", env=None):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    config = tmp_path / "pycontainer.toml"
    config.write_text(
        '[build]\ncontext-dir = "context"\noutput-dir = "output"\n'
        'base-image = "python:3.11-slim"\ninclude-paths = ["app.py"]\nuse-cache = false\n'
    )
    base = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "User": user,
            "Env": list(env or []),
            "Entrypoint": ["python", "app.py"],
            "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
        },
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        builder.ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], copy.deepcopy(base)),
    )
    pushes = []
    monkeypatch.setattr(builder.ImageBuilder, "push", lambda self, **kwargs: pushes.append(kwargs))
    return config, pushes


@pytest.mark.parametrize(
    ("user", "env", "expected"),
    [("1000", [], 0), ("root", [], 1), ("1000", ["TOKEN=value"], 1)],
)
def test_build_validation_gates_push(tmp_path, monkeypatch, capsys, user, env, expected):
    config, pushes = _build_fixture(tmp_path, monkeypatch, user=user, env=env)
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == expected
    assert len(pushes) == (1 if expected == 0 else 0)
    if expected:
        assert ("PCB001" if user == "root" else "PCB005") in capsys.readouterr().out


def test_build_integrity_error_prevents_push(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    original = builder.ImageBuilder.build

    def corrupt(self):
        result = original(self)
        path = Path(self.config.output_dir) / "blobs" / "sha256" / self.layers[-1].digest.split(":", 1)[1]
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        return result

    monkeypatch.setattr(builder.ImageBuilder, "build", corrupt)
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 2
    assert not pushes
    assert "digest" in capsys.readouterr().err.lower()


def test_build_suppressions_extend_config_and_allow_push(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch, user="root", env=["API_TOKEN=value"])
    with config.open("a") as stream:
        stream.write('\n[build.validate]\nignore = ["PCB001"]\n')
    assert _run(
        monkeypatch, ["build", "--config", str(config), "--validate", "--ignore", "PCB005", "--push"]
    ) == 0
    assert len(pushes) == 1
    assert "value" not in capsys.readouterr().out


def test_build_checks_unknown_ignore_before_building(tmp_path, monkeypatch, capsys):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "ImageBuilder", lambda *_: pytest.fail("must reject before building"))
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--ignore", "PCB999"]) == 2
    assert "PCB999" in capsys.readouterr().err


def test_build_rejects_unknown_config_ignore_before_building(tmp_path, monkeypatch, capsys):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    with config.open("a") as stream:
        stream.write('\n[build.validate]\nignore = ["PCB999"]\n')
    monkeypatch.setattr(cli, "ImageBuilder", lambda *_: pytest.fail("must reject before building"))
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 2
    assert "[build.validate].ignore" in capsys.readouterr().err


def test_validation_holds_output_lock_until_push(tmp_path, monkeypatch):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    called = []

    def push(self, **kwargs):
        with pytest.raises(RuntimeError, match="concurrent writes"):
            with output_directory_lock(self.config.output_dir):
                pytest.fail("output must remain locked until push")
        called.append(True)

    monkeypatch.setattr(builder.ImageBuilder, "push", push)
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 0
    assert called == [True]


@pytest.mark.parametrize(("arm_user", "expected"), [("1000", 0), ("root", 1)])
def test_build_validation_gates_every_platform(tmp_path, monkeypatch, capsys, arm_user, expected):
    config, pushes = _build_fixture(tmp_path, monkeypatch)

    def base(self, layers_dir, os_name, arch):
        return [], {
            "architecture": arch, "os": os_name,
            "config": {
                "User": arm_user if arch == "arm64" else "1000",
                "Entrypoint": ["python", "app.py"],
                "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
            },
            "rootfs": {"type": "layers", "diff_ids": []},
        }

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", base)
    assert _run(
        monkeypatch,
        ["build", "--config", str(config), "--platform", "linux/amd64,linux/arm64",
         "--validate", "--push"],
    ) == expected
    assert len(pushes) == (1 if expected == 0 else 0)
    if expected:
        assert "PCB001" in capsys.readouterr().out


def test_validation_and_scan_hold_output_lock_through_push(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from pycontainer import scanning

    real_push = builder.ImageBuilder.push
    config, _ = _build_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(builder.ImageBuilder, "push", real_push)
    scans, pushes = [], []

    @contextmanager
    def scan(output, digest, config):
        with pytest.raises(RuntimeError, match="concurrent writes"):
            with output_directory_lock(output):
                pytest.fail("the output lock must remain held during scanning")
        scans.append(digest)
        yield Path(output), {"digest": digest}

    def push(self, *args):
        with pytest.raises(RuntimeError, match="concurrent writes"):
            with output_directory_lock(self.config.output_dir):
                pytest.fail("the output lock must remain held during push")
        pushes.append(True)
        return self.config.tag

    monkeypatch.setattr(scanning, "scanned_image", scan)
    monkeypatch.setattr(builder.ImageBuilder, "_push_image", push)
    assert _run(monkeypatch, ["build", "--config", str(config), "--scan", "--validate", "--push"]) == 0
    assert len(scans) == 2
    assert scans[0] == scans[1]
    assert pushes == [True]


def test_build_validation_redacts_arguments_in_layout_errors(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    config.write_text(
        config.read_text().replace('output-dir = "output"', 'output-dir = "output-${NAME}"')
        + '\n[build.build-args]\nNAME = "private-build-value"\n'
    )

    def fail(output, **kwargs):
        raise ValueError(f"Cannot read {output}")

    monkeypatch.setattr(cli, "validate_image", fail)
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 2
    captured = capsys.readouterr()
    assert "private-build-value" not in captured.out + captured.err
    assert "<redacted>" in captured.err
    assert not pushes


def test_multi_platform_validation_rejects_replaced_index(tmp_path, monkeypatch):
    config, pushes = _build_fixture(tmp_path, monkeypatch)

    def base(self, layers_dir, os_name, arch):
        return [], {
            "architecture": arch, "os": os_name,
            "config": {
                "User": "1000", "Entrypoint": ["python"],
                "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
            },
            "rootfs": {"type": "layers", "diff_ids": []},
        }

    original = builder.ImageBuilder.build

    def replace_index(self):
        result = original(self)
        output = Path(self.config.output_dir)
        index_path = output / "blobs" / "sha256" / self.manifest_digest.split(":", 1)[1]
        index = json.loads(index_path.read_text())
        index["annotations"] = {"changed": "true"}
        replacement = _blob(output, index, INDEX)
        replacement["annotations"] = {REF: self.config.tag}
        _layout(output, [replacement])
        return result

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", base)
    monkeypatch.setattr(builder.ImageBuilder, "build", replace_index)
    assert _run(
        monkeypatch,
        ["build", "--config", str(config), "--platform", "linux/amd64,linux/arm64",
         "--validate", "--push"],
    ) == 2
    assert not pushes


def test_build_does_not_push_when_the_layout_was_replaced(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    original = builder.ImageBuilder.build

    def replace_layout(self):
        result = original(self)
        output = Path(self.config.output_dir)
        replacement, _, _ = _image(output, tag=self.config.tag)
        _layout(output, [replacement])
        return result

    monkeypatch.setattr(builder.ImageBuilder, "build", replace_layout)
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 2
    assert not pushes
    assert "built image" in capsys.readouterr().err.lower()


def test_build_without_validation_preserves_existing_layout_bytes(tmp_path, monkeypatch, capsys):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    baseline, _ = effective_config.resolve_build_config(config, {})
    baseline.output_dir = "baseline"
    builder.ImageBuilder(baseline).build()
    before = _snapshot(tmp_path / "baseline")
    capsys.readouterr()

    assert _run(monkeypatch, ["build", "--config", str(config)]) == 0
    assert _snapshot(tmp_path / "output") == before


def test_build_with_validation_does_not_change_layout_bytes(tmp_path, monkeypatch, capsys):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    baseline, _ = effective_config.resolve_build_config(config, {})
    baseline.output_dir = "baseline"
    builder.ImageBuilder(baseline).build()
    before = _snapshot(tmp_path / "baseline")
    capsys.readouterr()
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate"]) == 0
    assert _snapshot(tmp_path / "output") == before


@pytest.mark.parametrize(
    "args",
    [
        ["validate"],
        ["build", "--validate", "--dry-run"],
        ["build", "--ignore", "PCB001"],
    ],
)
def test_usage_errors_exit_two(monkeypatch, capsys, args):
    assert _run(monkeypatch, args) == 2
    assert "usage:" in capsys.readouterr().err.lower()
