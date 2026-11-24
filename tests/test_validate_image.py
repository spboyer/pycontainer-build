import copy
import gzip
import hashlib
import json
import socket
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from pycontainer import builder, cache, cli, config_loader, effective_config, image_validation, registry_client
from pycontainer.config import BuildConfig
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
    assert set(report) == {"image", "provenance", "findings", "size"}
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


SECRET_ARGUMENT = "REDACTION_FIXTURE_2459"


def _assert_redacted(capsys):
    captured = capsys.readouterr()
    assert SECRET_ARGUMENT not in captured.out + captured.err
    assert "<redacted>" in captured.err


@pytest.mark.parametrize(
    "validate_table",
    [
        f'ignore = ["{SECRET_ARGUMENT}"]\n',
        f'{SECRET_ARGUMENT} = ["PCB001"]\n',
    ],
)
def test_invalid_validation_configuration_redacts_build_arguments(tmp_path, monkeypatch, capsys, validate_table):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    config = tmp_path / "settings.toml"
    config.write_text(
        f'[build.build-args]\nTOKEN = "{SECRET_ARGUMENT}"\n[build.validate]\n{validate_table}'
    )

    assert _validate(monkeypatch, tmp_path, "--config", str(config)) == 2
    _assert_redacted(capsys)


@pytest.mark.parametrize("command", ["config", "image"])
def test_config_key_collisions_redact_declared_build_arguments(tmp_path, monkeypatch, capsys, command):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    secret = "redaction-fixture-2159"
    config = tmp_path / "settings.toml"
    config.write_text(
        f'[build]\n{secret} = true\n{secret.replace("-", "_")} = true\n'
        f'[build.build-args]\nTOKEN = "{secret}"\n'
    )

    if command == "image":
        code = _validate(monkeypatch, tmp_path, "--config", str(config))
    else:
        code = _run(monkeypatch, ["validate", "config", str(config)])
    captured = capsys.readouterr()

    assert code == 2
    assert secret not in captured.out + captured.err
    assert "normalize to the same setting 'redaction_fixture_2159'" in captured.err
    assert "<redacted>" in captured.err


def test_unknown_cli_ignore_redacts_configured_build_arguments(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path)
    _layout(tmp_path, [descriptor])
    config = tmp_path / "settings.toml"
    config.write_text(f'[build.build-args]\nTOKEN = {{ default = "{SECRET_ARGUMENT}" }}\n')

    assert _validate(monkeypatch, tmp_path, "--config", str(config), "--ignore", SECRET_ARGUMENT) == 2
    _assert_redacted(capsys)


def test_layout_errors_redact_configured_build_arguments(tmp_path, monkeypatch, capsys):
    config = tmp_path / "settings.toml"
    config.write_text(f'[build.build-args]\nTOKEN = "{SECRET_ARGUMENT}"\n')

    assert _validate(
        monkeypatch, tmp_path, "--config", str(config), "--output", str(tmp_path / f"output-{SECRET_ARGUMENT}")
    ) == 2
    _assert_redacted(capsys)


def test_size_thresholds_preserve_redaction_on_layout_errors(tmp_path, monkeypatch, capsys):
    config = tmp_path / "settings.toml"
    config.write_text(
        f'[build.build-args]\nTOKEN = "{SECRET_ARGUMENT}"\n'
        '[build.validate]\nmax_layer_size_mb = 1\n'
    )
    assert _validate(
        monkeypatch, tmp_path, "--config", str(config),
        "--output", str(tmp_path / f"output-{SECRET_ARGUMENT}"),
        "--max-image-size-mb", "1",
    ) == 2
    _assert_redacted(capsys)


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


def _squash_fixture(tmp_path, monkeypatch):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    (tmp_path / "context" / "deps.py").write_text("VALUE = 1\n")
    config.write_text(config.read_text().replace('include-paths = ["app.py"]', 'include-paths = ["app.py", "deps.py"]'))

    def base(self, layers_dir, os_name, arch):
        image = {
            "architecture": arch, "os": os_name,
            "config": {
                "User": "1000", "Entrypoint": ["python", "app.py"],
                "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
            },
            "rootfs": {"type": "layers", "diff_ids": []},
        }
        return [], image

    def deps_layer(self, layers_dir, **kwargs):
        return self._create_app_layer(layers_dir, ["deps.py"])

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", base)
    monkeypatch.setattr(builder, "target_libc_config", lambda *args, **kwargs: args[3])
    monkeypatch.setattr(builder.ImageBuilder, "_create_deps_layer", deps_layer)
    return config, pushes


@pytest.mark.parametrize("platforms", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_build_validation_accepts_squashed_layouts(tmp_path, monkeypatch, capsys, platforms):
    config, pushes = _squash_fixture(tmp_path, monkeypatch)
    assert _run(monkeypatch, [
        "build", "--config", str(config), "--platform", platforms,
        "--include-deps", "--squash", "--analyze-layers", "--validate", "--push",
    ]) == 0
    assert len(pushes) == 1
    output = capsys.readouterr().out
    assert output.count("Squashed 2 builder layers") == len(platforms.split(","))
    assert output.count("No image validation findings.") == len(platforms.split(","))
    assert output.count("Contributions: base 0 bytes") == len(platforms.split(","))
    for platform in platforms.split(","):
        assert _validate(monkeypatch, tmp_path / "output", "--platform", platform, "--format", "json") == 0
        report = _report(capsys)
        assert report["image"]["platform"] == platform
        assert report["findings"] == []


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


def test_build_unknown_ignore_redacts_build_arguments(tmp_path, monkeypatch, capsys):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    config.write_text(config.read_text() + f'\n[build.build-args]\nTOKEN = "{SECRET_ARGUMENT}"\n')
    monkeypatch.setattr(cli, "ImageBuilder", lambda *_: pytest.fail("must reject before building"))
    assert _run(
        monkeypatch, ["build", "--config", str(config), "--validate", "--ignore", SECRET_ARGUMENT]
    ) == 2
    _assert_redacted(capsys)


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

# --- Image size thresholds (#121) ---

MIB = 1024 * 1024
GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def _config_size(tmp_path):
    _, config, _ = _image(tmp_path / "scratch")
    return config["size"]


def _sized_image(layout, layer_bytes, **kwargs):
    descriptor, config, layer = _image(layout, layer_data=b"x" * layer_bytes, **kwargs)
    _layout(layout, [descriptor])
    return descriptor, config, layer


@pytest.mark.parametrize(("delta", "expected"), [(-1, 0), (0, 0), (1, 1)])
def test_image_size_threshold_boundaries(tmp_path, monkeypatch, capsys, delta, expected):
    layout = tmp_path / "layout"
    _sized_image(layout, MIB - _config_size(tmp_path) + delta)

    assert _validate(monkeypatch, layout, "--format", "json", "--max-image-size-mb", "1") == expected

    report = _report(capsys)
    assert report["size"]["image_bytes"] == MIB + delta
    assert [item["rule_id"] for item in report["findings"]] == (["PCB006"] if expected else [])


@pytest.mark.parametrize(("delta", "expected"), [(-1, 0), (0, 0), (1, 1)])
def test_layer_size_threshold_boundaries(tmp_path, monkeypatch, capsys, delta, expected):
    _sized_image(tmp_path, MIB + delta)

    assert _validate(monkeypatch, tmp_path, "--format", "json", "--max-layer-size-mb", "1") == expected

    report = _report(capsys)
    assert report["size"]["largest_layer"]["bytes"] == MIB + delta
    assert [item["rule_id"] for item in report["findings"]] == (["PCB007"] if expected else [])


@pytest.mark.parametrize(("layer_bytes", "expected"), [(104857, 0), (104858, 1)])
def test_decimal_thresholds_compare_exactly(tmp_path, monkeypatch, capsys, layer_bytes, expected):
    # 0.1 MiB is 104857.6 bytes; a binary float conversion must not move the boundary.
    _sized_image(tmp_path, layer_bytes)
    assert _validate(monkeypatch, tmp_path, "--format", "json", "--max-layer-size-mb", "0.1") == expected
    assert len(_report(capsys)["findings"]) == expected


def _precise_threshold(actual, delta):
    with localcontext() as context:
        context.prec = 80
        return Decimal(actual) / Decimal(MIB) + Decimal(delta) * Decimal("1e-50")


@pytest.mark.parametrize(("name", "rule"), [
    ("max_image_size_mb", "PCB006"), ("max_layer_size_mb", "PCB007"),
])
@pytest.mark.parametrize("source", ["cli", "build", "tool.pycontainer"])
@pytest.mark.parametrize("format_", ["text", "json"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_threshold_precision_survives_loading_and_reporting(
    tmp_path, monkeypatch, capsys, name, rule, source, format_, delta
):
    _, config, _ = _sized_image(tmp_path, 1)
    actual = config["size"] + 1 if name == "max_image_size_mb" else 1
    threshold = _precise_threshold(actual, delta)
    options = ["--format", format_]
    if source == "cli":
        options += ["--" + name.replace("_", "-"), format(threshold, "f")]
    else:
        settings = tmp_path / "settings.toml"
        settings.write_text(f"[{source}.validate]\n{name} = {threshold:f}\n")
        options += ["--config", str(settings)]

    assert _validate(monkeypatch, tmp_path, *options) == (1 if delta < 0 else 0)
    if format_ == "json":
        report = _report(capsys)
        assert report["size"]["thresholds"][name] == str(threshold)
        findings = report["findings"]
        assert [item["rule_id"] for item in findings] == ([rule] if delta < 0 else [])
        output = " ".join(item["message"] for item in findings)
    else:
        output = capsys.readouterr().out
    if delta < 0:
        assert f"above {name} = {threshold}" in output


@pytest.mark.parametrize("name", ["max_image_size_mb", "max_layer_size_mb"])
@pytest.mark.parametrize("source", ["cli", "build", "tool.pycontainer"])
@pytest.mark.parametrize("format_", ["text", "json"])
@pytest.mark.parametrize(("value", "expected"), [
    ("1e-400", 1), ("1e5000", 2), ("1e-5000", 2), ("1e9999999999999999999999", 2),
])
def test_threshold_magnitudes_are_exact_or_explicit_errors(
    tmp_path, monkeypatch, capsys, name, source, format_, value, expected
):
    _sized_image(tmp_path, 1)
    options = ["--format", format_]
    if source == "cli":
        options += ["--" + name.replace("_", "-"), value]
    else:
        settings = tmp_path / "settings.toml"
        settings.write_text(f"[{source}.validate]\n{name} = {value}\n")
        options += ["--config", str(settings)]
    assert _validate(monkeypatch, tmp_path, *options) == expected
    if expected == 2:
        captured = capsys.readouterr()
        assert not captured.out and "exponent" in captured.err
        diagnostic_name = name.replace("_", "-") if source == "cli" else name
        assert diagnostic_name in captured.err
    elif format_ == "json":
        assert _report(capsys)["size"]["thresholds"][name] == "1E-400"
    else:
        assert f"above {name} = 1E-400" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["max_image_size_mb", "max_layer_size_mb"])
@pytest.mark.parametrize("source", ["cli", "build"])
def test_excessive_threshold_precision_is_a_usage_error(tmp_path, monkeypatch, capsys, name, source):
    value = "1." + "1" * 1000
    options = ["--" + name.replace("_", "-"), value]
    if source == "build":
        settings = tmp_path / "settings.toml"
        settings.write_text(f"[build.validate]\n{name} = {value}\n")
        options = ["--config", str(settings)]
    assert _validate(monkeypatch, tmp_path, *options) == 2
    captured = capsys.readouterr()
    assert not captured.out and "1000" in captured.err and "digits" in captured.err


@pytest.mark.parametrize(("exponent", "valid"), [(1000, True), (-1000, True), (1001, False), (-1001, False)])
def test_size_threshold_exponent_limits(exponent, valid):
    value = f"1e{exponent}"
    if valid:
        assert image_validation.parse_size_threshold(value, "threshold") == Decimal(value)
    else:
        with pytest.raises(ValueError, match="exponent"):
            image_validation.parse_size_threshold(value, "threshold")


def test_exact_toml_thresholds_preserve_unrelated_float_types(tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text(
        "[build]\nother = { numbers = [0.1, 1e-400, 1e9999999999999999999999] }\n"
        "[build.validate]\nmax_image_size_mb = 0.00000095367431640624999999999999999999\n"
    )
    config = config_loader.load_config_file(settings)
    assert config["validate"]["max_image_size_mb"] == Decimal("0.00000095367431640624999999999999999999")
    assert config["other"]["numbers"] == [0.1, 0.0, float("inf")]
    assert all(type(value) is float for value in config["other"]["numbers"])


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
@pytest.mark.parametrize("name", ["max_image_size_mb", "max_layer_size_mb"])
@pytest.mark.parametrize("literal", [
    "1e-400", "1e1000", "1e-1000",
    "0.00000095367431640624999999999999999999",
])
def test_exact_threshold_build_config_json_round_trip(tmp_path, table, name, literal):
    from dataclasses import asdict, fields, replace
    from fractions import Fraction

    settings = tmp_path / "settings.toml"
    settings.write_text(f"[{table}.validate]\n{name} = {literal}\n")
    original = config_loader.config_from_file(settings)
    snapshot = json.loads(json.dumps(asdict(original)))
    restored = BuildConfig(**{
        field.name: snapshot[field.name] for field in fields(BuildConfig) if field.init
    })
    for config in (original, replace(original), restored):
        assert isinstance(config.validate[name], str)
        assert image_validation.parse_size_threshold(config.validate[name], name) == Decimal(literal)
        boundary = Decimal("0.00000095367431640624999999999999999999")
        if Decimal(literal) == boundary:
            assert Fraction(image_validation.parse_size_threshold(config.validate[name], name)) * MIB < 1
    _sized_image(tmp_path / "layout", 1)
    report = image_validation.validate_image(tmp_path / "layout", thresholds=restored.validate)
    assert report["size"]["thresholds"][name] == str(Decimal(literal))
    rule = "PCB006" if name == "max_image_size_mb" else "PCB007"
    assert (rule in {item["rule_id"] for item in report["findings"]}) == (literal != "1e1000")


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
@pytest.mark.parametrize("name", ["max_image_size_mb", "max_layer_size_mb"])
def test_snapshot_threshold_strings_are_not_accepted_in_toml(tmp_path, table, name):
    settings = tmp_path / "settings.toml"
    settings.write_text(f'[{table}.validate]\n{name} = "1E-400"\n')
    with pytest.raises(ValueError, match="expected a number"):
        config_loader.config_from_file(settings)


def test_size_threshold_and_socket_port_rules_share_report(tmp_path, monkeypatch, capsys):
    _sized_image(tmp_path, 1, config={
        "Entrypoint": ["gunicorn", "app:app", "--bind", "unix:/tmp/app.sock"],
        "ExposedPorts": {"8000/tcp": {}},
    })
    assert _validate(
        monkeypatch, tmp_path, "--format", "json", "--max-layer-size-mb", "1e-400"
    ) == 1
    report = _report(capsys)
    assert {item["rule_id"] for item in report["findings"]} == {"PCB007", "PCB010"}
    assert report["size"]["thresholds"]["max_layer_size_mb"] == "1E-400"


@pytest.mark.parametrize("format_", ["text", "json"])
def test_no_thresholds_report_stored_size_without_warnings(tmp_path, monkeypatch, capsys, format_):
    _, config, layer = _sized_image(tmp_path, 10, layer_type=GZIP_LAYER)

    assert _validate(monkeypatch, tmp_path, "--format", format_) == 0

    if format_ == "text":
        out = capsys.readouterr().out
        assert "No image validation findings." in out
        assert "stored blob sizes" in out and "Network transfer" in out
        assert "unknown (the layout does not record layer origin)" in out
        return
    report = _report(capsys)
    size = report["size"]
    assert report["findings"] == []
    assert size["image_bytes"] == config["size"] + layer["size"]
    assert size["thresholds"] == {"max_image_size_mb": None, "max_layer_size_mb": None}
    assert size["contributions"] == {"status": "unknown", "base_bytes": None, "application_bytes": None}
    assert size["layers"] == [{
        "digest": layer["digest"], "bytes": 10, "media_type": GZIP_LAYER,
        "compression": "gzip", "role": "unknown",
    }]
    assert "Network transfer" in size["note"]


def test_image_size_sums_every_layer_descriptor(tmp_path, monkeypatch, capsys):
    layer = _blob(tmp_path, b"shared layer", LAYER)
    image = {
        "architecture": "amd64", "os": "linux",
        "config": {"User": "1000", "Entrypoint": ["python"],
                   "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"}},
        "rootfs": {"type": "layers", "diff_ids": [layer["digest"], layer["digest"]]},
    }
    config = _blob(tmp_path, image, CONFIG)
    manifest = _blob(
        tmp_path, {"schemaVersion": 2, "mediaType": MANIFEST, "config": config, "layers": [layer, layer]},
        MANIFEST,
    )
    _layout(tmp_path, [manifest])

    assert _validate(monkeypatch, tmp_path, "--format", "json") == 0
    assert _report(capsys)["size"]["image_bytes"] == config["size"] + 2 * layer["size"]


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
@pytest.mark.parametrize(
    ("toml_value", "cli_value", "expected"),
    [("1", None, 1), ("100", None, 0), ("100", "1", 1), ("1", "100", 0), (None, "1", 1)],
)
def test_threshold_cli_overrides_toml_only_when_supplied(
    tmp_path, monkeypatch, capsys, table, toml_value, cli_value, expected
):
    _sized_image(tmp_path, MIB + 1)
    config = tmp_path / "settings.toml"
    config.write_text(
        f"[{table}.validate]\n" + (f"max_layer_size_mb = {toml_value}\n" if toml_value else "")
    )
    options = ["--config", str(config), "--format", "json"]
    if cli_value is not None:
        options += ["--max-layer-size-mb", cli_value]

    assert _validate(monkeypatch, tmp_path, *options) == expected
    report = _report(capsys)
    assert [item["rule_id"] for item in report["findings"]] == (["PCB007"] if expected else [])
    effective = cli_value or toml_value
    assert report["size"]["thresholds"]["max_layer_size_mb"] == effective


@pytest.mark.parametrize("name", ["max_image_size_mb", "max_layer_size_mb"])
@pytest.mark.parametrize("value", ["0", "-1", "0.0", '"1"', "true", "nan", "inf", "[1]"])
def test_invalid_toml_thresholds_are_configuration_errors(tmp_path, monkeypatch, capsys, name, value):
    _sized_image(tmp_path, 1)
    config = tmp_path / "settings.toml"
    config.write_text(f"[build.validate]\n{name} = {value}\n")

    assert _validate(monkeypatch, tmp_path, "--config", str(config)) == 2
    captured = capsys.readouterr()
    assert not captured.out and f"[build.validate].{name}" in captured.err
    assert _run(monkeypatch, ["validate", "config", str(config)]) == 2
    assert name in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--max-image-size-mb", "--max-layer-size-mb"])
@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", "0x10", "", "1_0", "1,5", "١"])
def test_invalid_cli_thresholds_are_usage_errors(tmp_path, monkeypatch, capsys, option, value):
    _sized_image(tmp_path, 1)
    assert _validate(monkeypatch, tmp_path, f"{option}={value}") == 2
    captured = capsys.readouterr()
    assert not captured.out and option in captured.err


@pytest.mark.parametrize("kind", ["config", "layer"])
@pytest.mark.parametrize("corruption", ["digest", "size"])
def test_size_rules_never_mask_integrity_errors(tmp_path, monkeypatch, capsys, kind, corruption):
    manifest, config, layer = _image(tmp_path, bad_size=kind if corruption == "size" else None)
    _layout(tmp_path, [manifest])
    if corruption == "digest":
        path = _path(tmp_path, {"config": config, "layer": layer}[kind])
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

    assert _validate(
        monkeypatch, tmp_path, "--format", "json", "--max-image-size-mb", "0.000001",
        "--max-layer-size-mb", "0.000001", "--ignore", "PCB006", "--ignore", "PCB007",
    ) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert corruption in captured.err.lower()


def test_size_findings_can_be_ignored(tmp_path, monkeypatch, capsys):
    _sized_image(tmp_path, MIB + 1)
    assert _validate(
        monkeypatch, tmp_path, "--format", "json", "--max-layer-size-mb", "1",
        "--max-image-size-mb", "1", "--ignore", "PCB006", "--ignore", "PCB007",
    ) == 0
    assert _report(capsys)["findings"] == []


def test_attribution_requires_the_expected_manifest_digest(tmp_path):
    _sized_image(tmp_path, 1)
    with pytest.raises(ValueError, match="expected manifest digest"):
        image_validation.validate_image(tmp_path, base_layer_count=0)


def _with_base_layer(monkeypatch, tmp_path, data=b"compressed base layer"):
    original = builder.ImageBuilder._pull_base_image

    def pull(self, layers_dir, os_name, arch):
        _, base = original(self, layers_dir, os_name, arch)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        path = layers_dir / digest.split(":", 1)[1]
        path.write_bytes(data)
        base["rootfs"]["diff_ids"] = ["sha256:" + "1" * 64]
        return [builder.OCILayer(GZIP_LAYER, digest, len(data), str(path))], base

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", pull)
    return len(data)


def test_build_validation_attributes_base_and_application_layers(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    base_bytes = _with_base_layer(monkeypatch, tmp_path)

    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 0

    out = capsys.readouterr().out
    assert f"Contributions: base {base_bytes} bytes" in out
    assert "application/dependency" in out and "stored blob sizes" in out
    assert len(pushes) == 1


def test_build_reports_attributed_contributions_in_engine(tmp_path, monkeypatch):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    base_bytes = _with_base_layer(monkeypatch, tmp_path)
    cfg, _ = effective_config.resolve_build_config(config, {})
    built = builder.ImageBuilder(cfg)
    built.build()
    image = built.platform_images[0]

    size = image_validation.validate_image(
        Path(cfg.output_dir), tag=cfg.tag, expected_digest=built.manifest_digest,
        base_layer_count=len(image.layers) - image.app_layer_count,
    )["size"]

    roles = [layer["role"] for layer in size["layers"]]
    assert roles == ["base", "application"]
    assert size["layers"][0]["compression"] == "gzip" and size["layers"][1]["compression"] == "none"
    assert size["contributions"] == {
        "status": "determined", "base_bytes": base_bytes,
        "application_bytes": size["layers"][1]["bytes"],
    }
    assert size["image_bytes"] == size["config_bytes"] + base_bytes + size["layers"][1]["bytes"]


def test_multi_platform_size_validation_uses_each_platforms_base_layers(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    base_sizes = {}

    def base(self, layers_dir, os_name, arch):
        contents = [b"a" * 50] if arch == "amd64" else [b"b" * 50, b"c" * 20000]
        layers, diff_ids = [], []
        for content in contents:
            data = gzip.compress(content, compresslevel=0, mtime=0)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            path = layers_dir / digest.split(":", 1)[1]
            path.write_bytes(data)
            layers.append(builder.OCILayer(GZIP_LAYER, digest, len(data), str(path)))
            diff_ids.append("sha256:" + hashlib.sha256(content).hexdigest())
        base_sizes[arch] = sum(layer.size for layer in layers)
        return layers, {
            "architecture": arch, "os": os_name,
            "config": {
                "User": "1000", "Entrypoint": ["python"],
                "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
            },
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", base)
    args = [
        "build", "--config", str(config), "--platform", "linux/amd64,linux/arm64",
        "--validate", "--push",
    ]
    assert _run(monkeypatch, args) == 0
    out = capsys.readouterr().out
    assert f"Contributions: base {base_sizes['amd64']} bytes" in out
    assert f"Contributions: base {base_sizes['arm64']} bytes" in out
    assert len(pushes) == 1

    pushes.clear()
    assert _run(monkeypatch, [*args, "--max-layer-size-mb", "0.015"]) == 1
    out = capsys.readouterr().out
    assert f"Contributions: base {base_sizes['amd64']} bytes" in out
    assert f"Contributions: base {base_sizes['arm64']} bytes" in out
    assert "PCB007" in out
    assert not pushes


def test_build_toml_threshold_blocks_push(tmp_path, monkeypatch, capsys):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    with config.open("a") as stream:
        stream.write("\n[build.validate]\nmax_image_size_mb = 0.000001\n")
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 1
    assert not pushes
    assert "PCB006" in capsys.readouterr().out
    assert _run(monkeypatch, [
        "build", "--config", str(config), "--validate", "--push", "--max-image-size-mb", "100",
    ]) == 0
    assert len(pushes) == 1


@pytest.mark.parametrize(("name", "rule"), [
    ("max_image_size_mb", "PCB006"), ("max_layer_size_mb", "PCB007"),
])
def test_build_preserves_exact_toml_thresholds(tmp_path, monkeypatch, capsys, name, rule):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    args = ["build", "--config", str(config), "--validate", "--push"]
    assert _run(monkeypatch, args) == 0
    size = image_validation.validate_image(tmp_path / "output")["size"]
    actual = size["image_bytes"] if name == "max_image_size_mb" else size["largest_layer"]["bytes"]
    threshold = _precise_threshold(actual, -1)
    with config.open("a") as stream:
        stream.write(f"\n[build.validate]\n{name} = {threshold:f}\n")
    pushes.clear()
    capsys.readouterr()
    assert _run(monkeypatch, ["validate", "config", str(config)]) == 0
    assert _run(monkeypatch, args) == 1
    assert not pushes and rule in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["--max-image-size-mb", "1"],
        ["--max-layer-size-mb", "1"],
        ["--validate", "--max-layer-size-mb", "0"],
    ],
)
def test_build_threshold_usage_errors_precede_building(tmp_path, monkeypatch, capsys, args):
    config, _ = _build_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "ImageBuilder", lambda *_: pytest.fail("must reject before building"))
    assert _run(monkeypatch, ["build", "--config", str(config), *args]) == 2
    assert "usage:" in capsys.readouterr().err.lower()

def _port_findings(tmp_path, monkeypatch, capsys, config, *options):
    descriptor, _, _ = _image(tmp_path, config=config)
    _layout(tmp_path, [descriptor])
    code = _validate(monkeypatch, tmp_path, "--format", "json", *options)
    findings = [item for item in _report(capsys)["findings"] if item["rule_id"] >= "PCB008"]
    return code, {item["rule_id"]: item for item in findings}


UVICORN = ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"Entrypoint": UVICORN, "ExposedPorts": {"8000/tcp": {}}}, set()),
        ({"Entrypoint": ["/venv/bin/uvicorn", "main:app", "--port=8000"], "ExposedPorts": {"8000": {}}}, set()),
        ({"Entrypoint": ["python", "-m", "uvicorn"], "Cmd": ["main:app", "--port", "9000"],
          "ExposedPorts": {"9000/tcp": {}}}, set()),
        ({"Entrypoint": UVICORN, "ExposedPorts": {"9000/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": UVICORN, "ExposedPorts": None}, {"PCB009"}),
        ({"Entrypoint": UVICORN, "ExposedPorts": {"8000/tcp": {}, "8001/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["uvicorn", "main:app", "--port", "8000", "--port", "9000"],
          "ExposedPorts": {"8000/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["flask", "run", "--host=0.0.0.0", "--port=5000"], "ExposedPorts": {"5000/tcp": {}}}, set()),
        ({"Entrypoint": ["flask", "run", "--port", "000080"],
          "ExposedPorts": {"000080/tcp": {}}}, {"PCB011"}),
        ({"Entrypoint": ["uvicorn", "main:app", "--port", "0" * 5000 + "8000"],
          "ExposedPorts": {"0" * 5000 + "8000/tcp": {}}}, set()),
        ({"Entrypoint": ["python", "-m", "flask", "--app", "app", "run", "-p", "5001"],
          "ExposedPorts": {"5000/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["python", "manage.py", "runserver", "0.0.0.0:8000"], "ExposedPorts": {"8000/tcp": {}}}, set()),
        ({"Entrypoint": ["python", "manage.py", "runserver", "8000", "--noreload", "--verbosity", "2"],
          "ExposedPorts": {"8000/tcp": {}}}, set()),
        ({"Entrypoint": ["python3", "manage.py", "runserver", "--noreload", "[::]:8001"],
          "ExposedPorts": {"8000/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["python", "-m", "django", "runserver", "--verbosity", "2", "-v=3", "0.0.0.0:8000"],
          "ExposedPorts": {"8000/tcp": {}}}, set()),
        ({"Entrypoint": ["gunicorn", "-b", "0.0.0.0:8000", "--bind", "unix:/tmp/s", "--bind=[::]:8001", "app:app"],
          "ExposedPorts": {"8000/tcp": {}, "8001/tcp": {}}}, set()),
        ({"Entrypoint": ["gunicorn", "--bind", "unix:/tmp/s", "--bind", "0.0.0.0:8000", "app:app"],
          "ExposedPorts": {"8001/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"],
          "ExposedPorts": {"8501/tcp": {}}}, set()),
        ({"Entrypoint": ["streamlit", "run", "app.py", "--server.port", "8502"],
          "ExposedPorts": {"8501/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser"],
          "ExposedPorts": {"8888/tcp": {}}}, set()),
        ({"Entrypoint": ["python", "-m", "jupyterlab", "--port", "8889"], "ExposedPorts": {"8888/tcp": {}}}, {"PCB009"}),
        ({"Entrypoint": UVICORN, "ExposedPorts": {"8000/tcp": {}, "53/udp": {}}}, {"PCB011", "PCB012"}),
    ],
)
def test_listener_and_exposed_port_rules(tmp_path, monkeypatch, capsys, config, expected):
    code, findings = _port_findings(tmp_path, monkeypatch, capsys, config)
    assert set(findings) == expected
    assert code == (1 if expected & {"PCB009", "PCB011"} else 0)
    if "PCB009" in findings:
        assert findings["PCB009"]["severity"] == "warning"


@pytest.mark.parametrize(
    "config",
    [
        {"Entrypoint": ["uvicorn", "main:app"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["uvicorn", "main:app", "--uds", "/tmp/s", "--port", "8000"]},
        {"Entrypoint": ["uvicorn", "main:app", "--port", "0"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["uvicorn", "main:app", "--port", "70000"]},
        {"Entrypoint": ["uvicorn", "main:app", "--port"]},
        {"Entrypoint": ["flask", "run"], "ExposedPorts": {"5000/tcp": {}}},
        {"Entrypoint": ["gunicorn", "app:app"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["gunicorn", "--bind", "unix:/tmp/sock", "app:app"],
         "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["gunicorn", "--bind", "unix:/tmp/a", "--bind", "unix:/tmp/b", "app:app"],
         "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["gunicorn", "-b", "fd://3", "app:app"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["gunicorn", "-b", "localhost", "app:app"]},
        {"Entrypoint": ["gunicorn", "-b", "0.0.0.0:8080", "-b0.0.0.0:8081", "app:app"],
         "ExposedPorts": {"8080/tcp": {}, "8081/tcp": {}}},
        {"Entrypoint": ["gunicorn", "-Rb", "0.0.0.0:8081", "app:app"], "ExposedPorts": {"8081/tcp": {}}},
        {"Entrypoint": ["gunicorn", "--bind", "0.0.0.0:8080", "--bin", "0.0.0.0:8081", "app:app"],
         "ExposedPorts": {"8080/tcp": {}, "8081/tcp": {}}},
        {"Entrypoint": ["flask", "run", "--port", "5000", "-p8000"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["flask", "run", "--port", "5000", "--extra-files", "--port=9000"],
         "ExposedPorts": {"5000/tcp": {}}},
        {"Entrypoint": ["flask", "run", "--extra-files", "--port=9000", "--port", "5000"],
         "ExposedPorts": {"5000/tcp": {}}},
        {"Entrypoint": ["uvicorn", "main:app", "--por=9000"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["python", "manage.py", "runserver"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["python", "manage.py", "runserver", "--custom", "2", "0.0.0.0:8000"],
         "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["python", "manage.py", "runserver", "8000", "--custom", "9000"],
         "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["python", "manage.py", "runserver", "8000", "9000"],
         "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["streamlit", "run", "app.py"], "Env": ["STREAMLIT_SERVER_PORT=9000"],
         "ExposedPorts": {"8501/tcp": {}}},
        {"Entrypoint": ["gradio", "app.py"], "ExposedPorts": {"7860/tcp": {}}},
        {"Entrypoint": ["python", "app.py"], "Env": ["GRADIO_SERVER_PORT=7861"], "ExposedPorts": {"7860/tcp": {}}},
        {"Entrypoint": ["sh", "-c", "uvicorn main:app --port 9000"], "ExposedPorts": {"8000/tcp": {}}},
        {"Entrypoint": ["python", "-u", "-m", "uvicorn", "main:app", "--port", "9000"],
         "ExposedPorts": {"8000/tcp": {}}},
    ],
)
def test_undeterminable_listener_is_skipped_with_info(tmp_path, monkeypatch, capsys, config):
    code, findings = _port_findings(tmp_path, monkeypatch, capsys, config)
    assert set(findings) == {"PCB010"}
    assert findings["PCB010"]["severity"] == "info"
    assert code == 0


@pytest.mark.parametrize(
    "config",
    [
        {"Entrypoint": ["python", "worker.py"]},
        {"Entrypoint": ["flask", "shell"]},
        {"Entrypoint": ["python", "manage.py", "migrate"]},
        {"Entrypoint": None, "Cmd": None, "ExposedPorts": {"8000/tcp": {}}},
    ],
)
def test_unrecognized_command_without_ports_is_not_reported(tmp_path, monkeypatch, capsys, config):
    _, findings = _port_findings(tmp_path, monkeypatch, capsys, config)
    assert "PCB009" not in findings and "PCB010" not in findings


@pytest.mark.parametrize(
    "keys",
    [
        ["0/tcp"], ["65536/tcp"], ["-1/tcp"], [" 80/tcp"], ["80/"], ["80/icmp"], ["80/tcp/x"],
        ["8000-8010/tcp"], ["\u0668\u0660/tcp"], ["http/tcp"], ["8000", "8000/tcp"], ["53/udp", "53/UDP"],
        ["9" * 5000 + "/tcp"], ["080/tcp", "80/tcp"], ["000080/tcp", "80/tcp"],
        ["0" * 5000 + "80/tcp", "080/tcp"], ["000000/tcp"], ["00065536/tcp"],
    ],
)
def test_invalid_or_duplicate_exposed_port_keys_are_errors(tmp_path, monkeypatch, capsys, keys):
    config = {"Entrypoint": ["python", "app.py"], "ExposedPorts": {key: {} for key in keys}}
    code, findings = _port_findings(tmp_path, monkeypatch, capsys, config, "--ignore", "PCB010")
    assert findings["PCB008"]["severity"] == "error"
    assert code == 1


def test_exposed_port_messages_escape_untrusted_keys(tmp_path, monkeypatch, capsys):
    key = "\x1b[31mbad" + "x" * 100
    config = {"Entrypoint": ["python", "app.py"], "ExposedPorts": {key: {}}}
    _, findings = _port_findings(tmp_path, monkeypatch, capsys, config, "--ignore", "PCB010")
    message = findings["PCB008"]["message"]
    assert "\x1b" not in message and "\\u001b" in message and "x" * 40 not in message


@pytest.mark.parametrize("zeros", [5, 5000])
def test_zero_padded_exposed_ports_are_normalized_duplicates(tmp_path, monkeypatch, capsys, zeros):
    config = {"ExposedPorts": {"0" * zeros + "80/tcp": {}, "80/tcp": {}}}
    _, findings = _port_findings(tmp_path, monkeypatch, capsys, config)
    message = findings["PCB008"]["message"]
    assert "duplicate:" in message and "invalid:" not in message


PORT_REDACTION_VALUES = [
    "buildarg-unique-value-123",
    'unicode-\u00eb-quote"-backslash\\-value',
    "long-build-argument-" * 4,
]


@pytest.mark.parametrize("format_", ["text", "json"])
@pytest.mark.parametrize("rule_id", ["PCB008", "PCB013"])
@pytest.mark.parametrize("secret", PORT_REDACTION_VALUES)
def test_port_findings_redact_configured_values_before_encoding(
    tmp_path, monkeypatch, capsys, format_, rule_id, secret
):
    key = f"bad-{secret}-port"
    descriptor, _, _ = _image(
        tmp_path, config={"ExposedPorts": {key: {}} if rule_id == "PCB008" else {}}
    )
    _layout(tmp_path, [descriptor])
    config = tmp_path / "settings.toml"
    config.write_text(f'[build.build-args]\nTOKEN = {json.dumps(secret)}\n')
    if rule_id == "PCB013":
        original = cli.validate_image

        def validate(*args, **kwargs):
            kwargs["base_exposed_ports"] = [key]
            return original(*args, **kwargs)

        monkeypatch.setattr(cli, "validate_image", validate)
    assert _validate(
        monkeypatch, tmp_path, "--config", str(config), "--format", format_
    ) == (1 if rule_id == "PCB008" else 0)
    captured = capsys.readouterr()
    assert not captured.err
    if format_ == "json":
        report = json.loads(captured.out)
        assert set(report) == {"image", "provenance", "findings", "size"}
        finding = next(item for item in report["findings"] if item["rule_id"] == rule_id)
        assert set(finding) == {"rule_id", "severity", "message", "remediation"}
        message = finding["message"]
    else:
        message = next(line for line in captured.out.splitlines() if f": {rule_id}:" in line)
    assert json.dumps("bad-<redacted>-port") in message


def test_validate_image_keeps_prior_positional_parameters(tmp_path):
    key = "bad-secret-port"
    descriptor, _, _ = _image(tmp_path, config={"ExposedPorts": {}})
    _layout(tmp_path, [descriptor])
    report = image_validation.validate_image(
        tmp_path, None, None, (), [key], lambda text: text.replace("secret", "<redacted>")
    )
    findings = {item["rule_id"]: item["message"] for item in report["findings"]}
    assert "PCB013" in findings
    assert "bad-<redacted>-port" in findings["PCB013"]
    assert report["size"]["thresholds"] == {"max_image_size_mb": None, "max_layer_size_mb": None}


def test_non_object_exposed_ports_is_a_config_error(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, config={"ExposedPorts": ["8000/tcp"]})
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path) == 2
    assert "ExposedPorts" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("user", "ports", "expected"),
    [
        ("1000", {"80/tcp": {}}, True),
        ("app", {"443/tcp": {}}, True),
        ("app:0", {"80/tcp": {}}, True),
        ("1000", {"1023/udp": {}}, True),
        ("1000", {"1024/tcp": {}}, False),
        ("root", {"80/tcp": {}}, False),
        ("0:1000", {"80/tcp": {}}, False),
        (None, {"80/tcp": {}}, False),
    ],
)
def test_privileged_port_with_non_root_user(tmp_path, monkeypatch, capsys, user, ports, expected):
    config = {"User": user, "Entrypoint": ["python", "app.py"], "ExposedPorts": ports}
    _, findings = _port_findings(tmp_path, monkeypatch, capsys, config, "--ignore", "PCB010")
    assert ("PCB011" in findings) is expected


def test_non_tcp_ports_are_reported_and_excluded_from_listener_check(tmp_path, monkeypatch, capsys):
    config = {"Entrypoint": UVICORN, "ExposedPorts": {"8000/tcp": {}, "8000/udp": {}, "9000/sctp": {}}}
    code, findings = _port_findings(tmp_path, monkeypatch, capsys, config)
    assert set(findings) == {"PCB012"}
    assert "8000/udp" in findings["PCB012"]["message"] and "9000/sctp" in findings["PCB012"]["message"]
    assert code == 0


def test_port_rules_can_be_ignored(tmp_path, monkeypatch, capsys):
    config = {"Entrypoint": UVICORN, "ExposedPorts": {"80/tcp": {}}}
    code, findings = _port_findings(
        tmp_path, monkeypatch, capsys, config, "--ignore", "PCB009", "--ignore", "PCB011"
    )
    assert findings == {} and code == 0


def test_info_findings_are_printed_in_text_without_failing(tmp_path, monkeypatch, capsys):
    descriptor, _, _ = _image(tmp_path, config={"Entrypoint": ["uvicorn", "main:app"]})
    _layout(tmp_path, [descriptor])
    assert _validate(monkeypatch, tmp_path, "--fail-on", "warning") == 0
    assert "info: PCB010:" in capsys.readouterr().out


def _port_build(tmp_path, monkeypatch, base_ports, toml=""):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    original = builder.ImageBuilder._pull_base_image

    def pull(self, layers_dir, os_name, arch):
        layers, base = original(self, layers_dir, os_name, arch)
        if base_ports is not None:
            base["config"]["ExposedPorts"] = base_ports
        return layers, base

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", pull)
    with config.open("a") as stream:
        stream.write(toml)
    return config, pushes


def test_build_reports_replaced_base_ports(tmp_path, monkeypatch, capsys):
    config, pushes = _port_build(
        tmp_path, monkeypatch, {"8080/tcp": {}, "53/udp": {}, "9000/tcp": {}}, "exposed-ports = [9000]\n"
    )
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 0
    out = capsys.readouterr().out
    assert "info: PCB013:" in out and '"8080/tcp"' in out and '"53/udp"' in out
    assert '"9000/tcp"' not in out.split("PCB013", 1)[1].splitlines()[0]
    assert len(pushes) == 1


@pytest.mark.parametrize("bind", ["unix:/tmp/sock", "fd://3"])
def test_build_socket_only_listener_does_not_block_push(tmp_path, monkeypatch, capsys, bind):
    config, pushes = _port_build(
        tmp_path, monkeypatch, {"8000/tcp": {}},
        f'entrypoint = ["gunicorn", "--bind", "{bind}", "app:app"]\n',
    )
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 0
    output = capsys.readouterr().out
    assert "info: PCB010:" in output and "PCB009" not in output
    assert len(pushes) == 1


@pytest.mark.parametrize(
    ("argv", "port"),
    [
        (["flask", "run", "--port", "5000", "--extra-files", "--port=9000"], 5000),
        (["python", "manage.py", "runserver", "8000", "--custom", "9000"], 8000),
    ],
)
def test_build_ambiguous_option_values_do_not_block_push(tmp_path, monkeypatch, capsys, argv, port):
    config, pushes = _port_build(
        tmp_path, monkeypatch, {f"{port}/tcp": {}}, f"entrypoint = {json.dumps(argv)}\n",
    )
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--push"]) == 0
    output = capsys.readouterr().out
    assert "info: PCB010:" in output and "PCB009" not in output
    assert len(pushes) == 1


@pytest.mark.parametrize("rule_id", ["PCB008", "PCB013"])
@pytest.mark.parametrize("secret", PORT_REDACTION_VALUES)
def test_build_port_findings_redact_live_argument_values(
    tmp_path, monkeypatch, capsys, rule_id, secret
):
    key = f"bad-{secret}-port"
    config, pushes = _port_build(
        tmp_path, monkeypatch, {key: {}},
        ("exposed-ports = [9000]\n" if rule_id == "PCB013" else "")
        + '\n[build.build-args]\nTOKEN = "declared-default"\n',
    )
    assert _run(
        monkeypatch,
        ["build", "--config", str(config), "--build-arg", f"TOKEN={secret}", "--validate", "--push"],
    ) == (1 if rule_id == "PCB008" else 0)
    captured = capsys.readouterr()
    assert not captured.err
    message = next(line for line in captured.out.splitlines() if f": {rule_id}:" in line)
    assert json.dumps("bad-<redacted>-port") in message
    assert len(pushes) == (0 if rule_id == "PCB008" else 1)


def test_multi_platform_validation_uses_each_base_image_ports(tmp_path, monkeypatch):
    config, pushes = _build_fixture(tmp_path, monkeypatch)
    with config.open("a") as stream:
        stream.write("exposed-ports = [9000]\n")

    def base(self, layers_dir, os_name, arch):
        port = "8080/tcp" if arch == "amd64" else "8081/tcp"
        return [], {
            "architecture": arch, "os": os_name,
            "config": {
                "User": "1000", "Entrypoint": ["python", "app.py"],
                "ExposedPorts": {port: {}},
                "Labels": {SOURCE: "https://example.test/repo", VERSION: "1"},
            },
            "rootfs": {"type": "layers", "diff_ids": []},
        }

    reports = []
    original = cli.validate_image

    def validate(*args, **kwargs):
        report = original(*args, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(builder.ImageBuilder, "_pull_base_image", base)
    monkeypatch.setattr(cli, "validate_image", validate)
    assert _run(
        monkeypatch, ["build", "--config", str(config), "--platform", "linux/amd64,linux/arm64",
                      "--validate", "--push"]
    ) == 0
    assert len(pushes) == 1
    assert [report["image"]["platform"] for report in reports] == ["linux/amd64", "linux/arm64"]
    for report, expected, other in zip(reports, ("8080/tcp", "8081/tcp"), ("8081/tcp", "8080/tcp")):
        message = next(finding["message"] for finding in report["findings"] if finding["rule_id"] == "PCB013")
        assert expected in message and other not in message


def test_build_inherits_base_ports_without_replacement(tmp_path, monkeypatch, capsys):
    config, _ = _port_build(tmp_path, monkeypatch, {"8080/tcp": {}, "5353/udp": {}})
    assert _run(monkeypatch, ["build", "--config", str(config), "--validate", "--ignore", "PCB010"]) == 0
    out = capsys.readouterr().out
    assert "PCB013" not in out and "info: PCB012:" in out and "5353/udp" in out
    blobs = Path(tmp_path / "output" / "blobs" / "sha256")
    configs = [json.loads(p.read_bytes()) for p in blobs.iterdir() if p.read_bytes().startswith(b'{"architecture"')]
    assert configs[0]["config"]["ExposedPorts"] == {"8080/tcp": {}, "5353/udp": {}}


@pytest.mark.parametrize("ports", ["[8000, 8000]", "[0]", "[65536]", "[true]"])
@pytest.mark.parametrize("command", ["build", "validate"])
def test_invalid_configured_ports_fail_before_any_blob_is_written(
    tmp_path, monkeypatch, capsys, ports, command
):
    config, pushes = _port_build(tmp_path, monkeypatch, None, f"exposed-ports = {ports}\n")
    args = ["build", "--config", str(config), "--push"] if command == "build" else ["validate", "config", str(config)]
    assert _run(monkeypatch, args) == 2
    assert "exposed_ports" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()
    assert not pushes


@pytest.mark.parametrize("ports", [[8000, 8000], [0], [65536], ["8000"], [True]])
def test_builder_rejects_invalid_ports_before_writing(tmp_path, monkeypatch, ports):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    monkeypatch.setattr(
        builder.ImageBuilder, "_pull_base_image",
        lambda *args: pytest.fail("ports must be validated before pulling or writing"),
    )
    config = builder.BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"), use_cache=False,
        include_paths=["app.py"], exposed_ports=ports,
    )
    with pytest.raises(ValueError, match="exposed port"):
        builder.ImageBuilder(config).build()
    assert not (tmp_path / "output").exists()
