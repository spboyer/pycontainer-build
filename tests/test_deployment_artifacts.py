import errno
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from pycontainer import cli, deployment
from pycontainer.deployment import generate_compose, generate_kubernetes


_KUBERNETES_116_SCHEMAS = (
    "https://raw.githubusercontent.com/instrumenta/kubernetes-json-schema/"
    "133f84871ccf6a7a7d422cc40e308ae1c044c2ab/"
    "{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/"
    "{{.ResourceKind}}{{.KindSuffix}}.json"
)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _render_template(path, values):
    template = path.read_text(encoding="utf-8")
    pattern = r"{{\s*\.Values\.(name|image|containerPort|servicePort)\s*\|\s*toJson\s*}}"
    rendered = re.sub(pattern, lambda match: json.dumps(values[match[1]]), template)
    return json.loads(rendered)


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *args])
    cli.main()


def test_kubernetes_resources_and_chart_have_equivalent_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TOP_SECRET", "must-not-appear-in-artifacts")
    output = tmp_path / "deploy"
    generate_kubernetes(
        "localhost:5000/example/api:V1",
        8080,
        output,
        name="api",
        service_port=80,
    )

    assert {path.relative_to(output) for path in output.rglob("*") if path.is_file()} == {
        Path("deployment.yaml"),
        Path("service.yaml"),
        Path("chart") / "Chart.yaml",
        Path("chart") / "values.yaml",
        Path("chart") / "values.schema.json",
        Path("chart") / "templates" / "deployment.yaml",
        Path("chart") / "templates" / "service.yaml",
    }

    workload = _read_json(output / "deployment.yaml")
    service = _read_json(output / "service.yaml")
    assert (workload["apiVersion"], workload["kind"]) == ("apps/v1", "Deployment")
    assert (service["apiVersion"], service["kind"]) == ("v1", "Service")
    assert workload["metadata"]["name"] == service["metadata"]["name"] == "api"
    labels = workload["spec"]["template"]["metadata"]["labels"]
    assert workload["spec"]["selector"]["matchLabels"] == labels
    assert service["spec"]["selector"] == labels
    container = workload["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "api"
    assert container["image"] == "localhost:5000/example/api:V1"
    assert container["ports"] == [{"containerPort": 8080, "protocol": "TCP"}]
    assert "env" not in container
    assert "securityContext" not in container
    assert service["spec"]["ports"] == [
        {"port": 80, "targetPort": 8080, "protocol": "TCP"}
    ]

    chart = output / "chart"
    metadata = _read_json(chart / "Chart.yaml")
    assert metadata["apiVersion"] == "v2"
    assert metadata["name"] == "api"
    values = _read_json(chart / "values.yaml")
    assert values == {
        "name": "api",
        "image": "localhost:5000/example/api:V1",
        "containerPort": 8080,
        "servicePort": 80,
    }
    schema = _read_json(chart / "values.schema.json")
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(values)
    assert _render_template(chart / "templates" / "deployment.yaml", values) == workload
    assert _render_template(chart / "templates" / "service.yaml", values) == service
    for path in output.rglob("*"):
        if path.is_file():
            assert "must-not-appear-in-artifacts" not in path.read_text(encoding="utf-8")


def test_compose_uses_only_the_existing_image_and_loopback_tcp_port(tmp_path, monkeypatch):
    monkeypatch.setenv("TOP_SECRET", "must-not-appear-in-artifacts")
    output = tmp_path / "compose.yaml"
    generate_compose("ghcr.io/example/api:v1", 8080, output, name="api", host_port=9000)

    data = _read_json(output)
    assert set(data) == {"services"}
    assert data["services"] == {
        "api": {
            "image": "ghcr.io/example/api:v1",
            "ports": [{
                "host_ip": "127.0.0.1",
                "target": 8080,
                "published": 9000,
                "protocol": "tcp",
            }],
        }
    }
    assert "must-not-appear-in-artifacts" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "image",
    [
        "true",
        "null",
        "localhost:65535/team/my_app:V1",
        "example.com/team/my--app:v1.2",
        "example/app@sha256:" + "a" * 64,
        "example/app:v1@sha256:" + "f" * 64,
    ],
)
def test_image_references_round_trip_without_yaml_coercion(tmp_path, image):
    generate_kubernetes(image, 8080, tmp_path / "kubernetes")
    generate_compose(image, 8080, tmp_path / "compose.yaml")
    workload = _read_json(tmp_path / "kubernetes" / "deployment.yaml")
    assert workload["spec"]["template"]["spec"]["containers"][0]["image"] == image
    assert _read_json(tmp_path / "compose.yaml")["services"]["app"]["image"] == image
    schema = _read_json(tmp_path / "kubernetes" / "chart" / "values.schema.json")
    values = _read_json(tmp_path / "kubernetes" / "chart" / "values.yaml")
    Draft7Validator(schema).validate(values)


@pytest.mark.parametrize(
    "image",
    [
        "",
        None,
        123,
        "https://example.com/team/app",
        "user:secret@example.com/team/app",
        "example.com/team/$TOKEN",
        "example.com/team/${TOKEN}",
        "example.com/team/app?token=secret",
        "example.com/team/app#fragment",
        "example.com/team/app\nother",
        "example.com/team/my app",
        "example.com/team/\N{LATIN SMALL LETTER E WITH ACUTE}",
        "example.com/team/UPPER",
        "example.com/team/../app",
        "example.com\\team\\app",
        "localhost:0/app",
        "localhost:65536/app",
        "a" * 64 + ".example/app",
        "a" * 64,
        "example/app:",
        "example/app@sha256:" + "A" * 64,
        "example/app@sha256:" + "a" * 63,
        "a" * 256,
    ],
)
@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_invalid_images_are_rejected_before_writing(tmp_path, kind, image):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(ValueError, match="image"):
        generate(image, 8080, output)
    assert not output.exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["", None, "1api", "-api", "api-", "API", "a_b", "a" * 64])
@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_invalid_names_are_rejected_before_writing(tmp_path, kind, name):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(ValueError, match="name"):
        generate("example/app:v1", 8080, output, name=name)
    assert not output.exists()


@pytest.mark.parametrize("name", ["a", "a" * 63])
def test_kubernetes_name_boundaries(tmp_path, name):
    output = tmp_path / "output"
    generate_kubernetes("example/app:v1", 8080, output, name=name)
    assert _read_json(output / "service.yaml")["metadata"]["name"] == name


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8080", 1.5])
@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_invalid_container_ports_are_rejected_before_writing(tmp_path, kind, port):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(ValueError, match="port"):
        generate("example/app:v1", port, output)
    assert not output.exists()


@pytest.mark.parametrize("kind,argument", [("kubernetes", "service_port"), ("compose", "host_port")])
@pytest.mark.parametrize("port", [0, 65536, True, "80"])
def test_invalid_published_ports_are_rejected_before_writing(tmp_path, kind, argument, port):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(ValueError, match="port"):
        generate("example/app:v1", 8080, output, **{argument: port})
    assert not output.exists()


def test_chart_schema_rejects_bad_overrides(tmp_path):
    output = tmp_path / "deploy"
    generate_kubernetes("example/app:v1", 8080, output)
    chart = output / "chart"
    schema = Draft7Validator(_read_json(chart / "values.schema.json"))
    values = _read_json(chart / "values.yaml")
    for update in (
        {"name": "1bad"},
        {"name": "bad\n"},
        {"image": "user:secret@example.com/app"},
        {"image": "example/$TOKEN"},
        {"image": "localhost:65536/app"},
        {"image": "a" * 64 + ".example/app"},
        {"image": "a" * 64},
        {"containerPort": True},
        {"containerPort": 0},
        {"servicePort": 65536},
        {"extra": "not-supported"},
    ):
        assert list(schema.iter_errors({**values, **update})), update
    for required in values:
        incomplete = dict(values)
        del incomplete[required]
        assert list(schema.iter_errors(incomplete)), required


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_existing_output_is_never_replaced(tmp_path, kind):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    if kind == "kubernetes":
        output.mkdir()
        sentinel = output / "sentinel"
    else:
        sentinel = output
    sentinel.write_bytes(b"keep existing contents")
    with pytest.raises(FileExistsError):
        generate("example/app:v1", 8080, output)
    assert sentinel.read_bytes() == b"keep existing contents"
    assert sorted(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_existing_output_of_the_wrong_type_is_a_collision(tmp_path, kind):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    if kind == "kubernetes":
        output.write_bytes(b"keep")
    else:
        output.mkdir()
    with pytest.raises(FileExistsError):
        generate("example/app:v1", 8080, output)
    if kind == "kubernetes":
        assert output.read_bytes() == b"keep"
    else:
        assert not list(output.iterdir())
    assert sorted(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
@pytest.mark.parametrize("broken", [False, True])
def test_existing_output_symlinks_are_collisions(tmp_path, kind, broken):
    output = tmp_path / "output"
    target = tmp_path / "target"
    if not broken:
        if kind == "kubernetes":
            target.mkdir()
        else:
            target.write_bytes(b"keep")
    try:
        output.symlink_to(target, target_is_directory=kind == "kubernetes")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(FileExistsError):
        generate("example/app:v1", 8080, output)
    assert output.is_symlink()
    if not broken and kind == "compose":
        assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_missing_parent_is_reported_without_creating_directories(tmp_path, kind):
    output = tmp_path / "missing" / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(FileNotFoundError):
        generate("example/app:v1", 8080, output)
    assert not output.parent.exists()


def test_no_cross_device_fallback_on_compose_publication(tmp_path, monkeypatch):
    def reject_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(deployment.os, "link", reject_link)
    output = tmp_path / "compose.yaml"
    with pytest.raises(OSError, match="cross-device"):
        generate_compose("example/app:v1", 8080, output)
    assert not output.exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_staging_failure_does_not_create_the_output(tmp_path, monkeypatch, kind):
    def reject_sync(*args):
        raise OSError(errno.EIO, "sync failed")

    monkeypatch.setattr(deployment.os, "fsync", reject_sync)
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose
    with pytest.raises(OSError, match="sync failed"):
        generate("example/app:v1", 8080, output)
    assert not output.exists()
    assert not list(tmp_path.iterdir())


def test_partial_bundle_failure_preserves_completed_and_external_files(tmp_path, monkeypatch):
    output = tmp_path / "deploy"
    real_link = os.link
    count = 0

    def fail_second_link(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            (output / "external.txt").write_text("keep", encoding="utf-8")
            raise OSError(errno.ENOSPC, "disk full")
        real_link(source, destination)

    monkeypatch.setattr(deployment.os, "link", fail_second_link)
    with pytest.raises(OSError, match="partial"):
        generate_kubernetes("example/app:v1", 8080, output)
    assert _read_json(output / "deployment.yaml")["kind"] == "Deployment"
    assert (output / "external.txt").read_text(encoding="utf-8") == "keep"
    assert not (output / "service.yaml").exists()
    assert sorted(tmp_path.iterdir()) == [output]


def test_interrupted_bundle_leaves_completed_files_intact(tmp_path, monkeypatch):
    output = tmp_path / "deploy"
    real_link = os.link
    count = 0

    def interrupt_second_link(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt
        real_link(source, destination)

    monkeypatch.setattr(deployment.os, "link", interrupt_second_link)
    with pytest.raises(KeyboardInterrupt):
        generate_kubernetes("example/app:v1", 8080, output)
    assert _read_json(output / "deployment.yaml")["kind"] == "Deployment"
    assert not (output / "service.yaml").exists()
    assert sorted(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_competing_generators_have_one_winner(tmp_path, kind):
    output = tmp_path / "output"
    generate = generate_kubernetes if kind == "kubernetes" else generate_compose

    def attempt(tag):
        try:
            generate(f"example/app:{tag}", 8080, output)
            return "created"
        except FileExistsError:
            return "collision"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert sorted(results) == ["collision", "created"]
    if kind == "kubernetes":
        workload = _read_json(output / "deployment.yaml")
        image = workload["spec"]["template"]["spec"]["containers"][0]["image"]
    else:
        image = _read_json(output)["services"]["app"]["image"]
    assert image in {"example/app:one", "example/app:two"}
    assert sorted(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("kind", ["kubernetes", "compose"])
def test_cli_requires_explicit_image_and_port(tmp_path, monkeypatch, capsys, kind):
    output = tmp_path / "output"
    for args in (
        ["--port", "8080", "--output", str(output)],
        ["--image", "example/app:v1", "--output", str(output)],
        ["--image", "example/app:v1", "--port", "8080"],
    ):
        with pytest.raises(SystemExit) as error:
            _run_cli(monkeypatch, ["generate", kind, *args])
        assert error.value.code == 2
        assert "required" in capsys.readouterr().err
    assert not output.exists()


def test_cli_generates_without_building_or_running_compose(tmp_path, monkeypatch):
    def unexpected_builder(*args, **kwargs):
        pytest.fail("artifact generation must not construct an image builder")

    monkeypatch.setattr(cli, "ImageBuilder", unexpected_builder)
    output = tmp_path / "compose.yaml"
    _run_cli(
        monkeypatch,
        ["generate", "compose", "--image", "example/app:v1", "--port", "8080",
         "--name", "api", "--host-port", "9000", "--output", str(output)],
    )
    assert _read_json(output)["services"]["api"]["image"] == "example/app:v1"


def test_cli_generates_kubernetes_resources(tmp_path, monkeypatch):
    output = tmp_path / "deploy"
    _run_cli(
        monkeypatch,
        ["generate", "kubernetes", "--image", "example/app:v1", "--port", "8080",
         "--name", "api", "--service-port", "80", "--output", str(output)],
    )
    assert _read_json(output / "service.yaml")["spec"]["ports"][0]["port"] == 80


def test_cli_does_not_echo_credentials_in_an_invalid_image(tmp_path, monkeypatch, capsys):
    output = tmp_path / "compose.yaml"
    with pytest.raises(SystemExit) as error:
        _run_cli(
            monkeypatch,
            ["generate", "compose", "--image", "user:secret@example.com/app",
             "--port", "8080", "--output", str(output)],
        )
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "image" in message
    assert "secret" not in message
    assert not output.exists()


@pytest.mark.parametrize("unsupported", ["--build", "--env"])
def test_compose_cli_rejects_unsupported_inputs(tmp_path, monkeypatch, unsupported):
    output = tmp_path / "compose.yaml"
    with pytest.raises(SystemExit) as error:
        _run_cli(
            monkeypatch,
            ["generate", "compose", "--image", "example/app:v1", "--port", "8080",
             "--output", str(output), unsupported, "example"],
        )
    assert error.value.code == 2
    assert not output.exists()


def _helm_documents(rendered):
    documents = {}
    for part in re.split(r"(?m)^---\s*$", rendered):
        body = "\n".join(line for line in part.splitlines() if not line.startswith("#")).strip()
        if body:
            document = json.loads(body)
            documents[document["kind"]] = document
    return documents


def _check_schemas(arguments, rendered=None):
    result = subprocess.run(
        arguments, input=rendered, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.getenv("PYCONTAINER_VALIDATE_DEPLOYMENT_ARTIFACTS") != "1",
    reason="set PYCONTAINER_VALIDATE_DEPLOYMENT_ARTIFACTS=1 for external validation",
)
def test_external_tools_validate_artifacts(tmp_path):
    tools = {name: shutil.which(name) for name in ("helm", "kubeconform", "docker")}
    missing = [name for name, path in tools.items() if not path]
    assert not missing, f"Explicit external validation requires: {', '.join(missing)}"
    chart_output = tmp_path / "deploy"
    compose_output = tmp_path / "compose.yaml"
    generate_kubernetes("example/app:v1", 8080, chart_output, name="api", service_port=80)
    generate_compose("example/app:v1", 8080, compose_output, name="api", host_port=9000)

    helm = [tools["helm"], "template", "release", str(chart_output / "chart"),
            "--kube-version", "1.31.0"]
    rendered = subprocess.run(helm, check=True, capture_output=True, text=True, timeout=60).stdout
    documents = _helm_documents(rendered)
    assert documents == {
        "Deployment": _read_json(chart_output / "deployment.yaml"),
        "Service": _read_json(chart_output / "service.yaml"),
    }
    minimum = subprocess.run(
        [*helm[:-1], "1.16.0"], check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    assert _helm_documents(minimum) == documents
    unsupported_version = subprocess.run(
        [*helm[:-1], "1.15.0"], capture_output=True, text=True, timeout=60,
    )
    assert unsupported_version.returncode != 0
    overridden = subprocess.run(
        [*helm, "--set-string", "name=other", "--set-string", "image=example/app:v2",
         "--set", "containerPort=9000", "--set", "servicePort=90"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    changed = _helm_documents(overridden)
    assert changed["Service"]["metadata"]["name"] == "other"
    assert changed["Service"]["spec"]["ports"][0]["port"] == 90
    assert changed["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] == "example/app:v2"
    assert changed["Deployment"]["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == 9000
    rejected = subprocess.run(
        [*helm, "--set", "servicePort=0"], capture_output=True, text=True, timeout=60,
    )
    assert rejected.returncode != 0
    for argument in (
        "name=1bad",
        "image=example/$TOKEN",
        "image=localhost:65536/app",
        "image=" + "a" * 64 + ".example/app",
    ):
        rejected = subprocess.run(
            [*helm, "--set-string", argument], capture_output=True, text=True, timeout=60,
        )
        assert rejected.returncode != 0

    unsafe_values = tmp_path / "unsafe-values.json"
    unsafe_image = 'example/app:v1"},"injected":true,"other":"'
    unsafe_values.write_text(json.dumps({"image": unsafe_image}), encoding="utf-8")
    escaped = subprocess.run(
        [*helm, "--skip-schema-validation", "--values", str(unsafe_values)],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    escaped_container = _helm_documents(escaped)["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert escaped_container["image"] == unsafe_image
    assert "injected" not in escaped_container

    schema_check = [tools["kubeconform"], "-strict", "-summary", "-kubernetes-version", "1.31.0"]
    _check_schemas(
        [*schema_check, str(chart_output / "deployment.yaml"), str(chart_output / "service.yaml")],
    )
    _check_schemas(schema_check, rendered)
    _check_schemas(
        [tools["kubeconform"], "-strict", "-summary", "-kubernetes-version", "1.16.0",
         "-schema-location", _KUBERNETES_116_SCHEMAS],
        minimum,
    )
    scalar_chart = tmp_path / "scalar-deploy"
    generate_kubernetes("null", 8080, scalar_chart, name="true")
    scalar_rendered = subprocess.run(
        [tools["helm"], "template", "release", str(scalar_chart / "chart")],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    scalar_deployment = _helm_documents(scalar_rendered)["Deployment"]
    assert scalar_deployment["metadata"]["name"] == "true"
    assert scalar_deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "null"
    compose = subprocess.run(
        [tools["docker"], "compose", "-f", str(compose_output), "config", "--format", "json"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert json.loads(compose.stdout)["services"]["api"]["image"] == "example/app:v1"
    yaml_scalar_output = tmp_path / "compose-scalar.yaml"
    generate_compose("null", 8080, yaml_scalar_output)
    scalar = subprocess.run(
        [tools["docker"], "compose", "-f", str(yaml_scalar_output), "config", "--format", "json"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert json.loads(scalar.stdout)["services"]["app"]["image"] == "null"
