"""Generate bounded deployment artifacts for an existing container image."""

import json
import os
import re
import tempfile
from pathlib import Path

from .registry_client import parse_image_reference


_NAME_PATTERN = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REGISTRY_PORT = (
    r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
    r"65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
)
_REGISTRY = rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::{_REGISTRY_PORT})?"
_REPOSITORY = rf"{_COMPONENT}(?:/{_COMPONENT})*"
_REFERENCE_SUFFIX = r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?"
_IMAGE_PATTERN = rf"(?:{_REGISTRY}/)?{_REPOSITORY}{_REFERENCE_SUFFIX}"
_REGISTRY_IMAGE_PATTERN = rf"{_REGISTRY}/{_REPOSITORY}{_REFERENCE_SUFFIX}"
_REGISTRY_PREFIX_PATTERN = r"^([^/]*[.:][^/]*|localhost)/"
_MAX_IMAGE_LENGTH = 255

_TEMPLATE_NAME = "__pycontainer_name_value__"
_TEMPLATE_IMAGE = "__pycontainer_image_value__"
_TEMPLATE_CONTAINER_PORT = "__pycontainer_container_port_value__"
_TEMPLATE_SERVICE_PORT = "__pycontainer_service_port_value__"
_TEMPLATE_VALUES = {
    _TEMPLATE_NAME: "name",
    _TEMPLATE_IMAGE: "image",
    _TEMPLATE_CONTAINER_PORT: "containerPort",
    _TEMPLATE_SERVICE_PORT: "servicePort",
}


def _validate_image(image: str) -> None:
    if (
        not isinstance(image, str)
        or len(image) > _MAX_IMAGE_LENGTH
        or not re.fullmatch(_IMAGE_PATTERN, image)
        or (
            re.match(_REGISTRY_PREFIX_PATTERN, image)
            and not re.fullmatch(_REGISTRY_IMAGE_PATTERN, image)
        )
    ):
        raise ValueError(
            "image must be a supported container image reference without credentials"
        )
    try:
        parse_image_reference(image)
    except ValueError as error:
        raise ValueError("image must be a valid container image reference") from error


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(_NAME_PATTERN, name):
        raise ValueError(
            "name must be 1-63 lowercase letters, digits or hyphens, "
            "starting with a letter and ending with a letter or digit"
        )


def _validate_port(port: int, setting: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{setting} port must be an integer from 1 to 65535")


def _validate_settings(image: str, name: str, container_port: int, exposed_port: int) -> None:
    _validate_image(image)
    _validate_name(name)
    _validate_port(container_port, "container")
    _validate_port(exposed_port, "published")


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def _resources(
    name: str, image: str, container_port: int | str, service_port: int | str
) -> tuple[dict[str, object], dict[str, object]]:
    labels = {"app.kubernetes.io/name": name}
    workload = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [{
                        "name": name,
                        "image": image,
                        "ports": [{"containerPort": container_port, "protocol": "TCP"}],
                    }],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "type": "ClusterIP",
            "selector": labels,
            "ports": [{"port": service_port, "targetPort": container_port, "protocol": "TCP"}],
        },
    }
    return workload, service


def _values_schema() -> dict[str, object]:
    image = {
        "type": "string",
        "minLength": 1,
        "maxLength": _MAX_IMAGE_LENGTH,
        "pattern": f"^{_IMAGE_PATTERN}$",
        "not": {"pattern": r"[^A-Za-z0-9._:/@-]"},
        "allOf": [{
            "if": {"pattern": _REGISTRY_PREFIX_PATTERN},
            "then": {"pattern": f"^{_REGISTRY_IMAGE_PATTERN}$"},
        }],
    }
    port = {"type": "integer", "minimum": 1, "maximum": 65535}
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "image", "containerPort", "servicePort"],
        "properties": {
            "name": {
                "type": "string",
                "maxLength": 63,
                "pattern": f"^{_NAME_PATTERN}$",
                "not": {"pattern": r"[^a-z0-9-]"},
            },
            "image": image,
            "containerPort": port,
            "servicePort": port,
        },
    }


def _chart_template(resource: dict[str, object]) -> str:
    rendered = _json_text(resource)
    for marker, key in _TEMPLATE_VALUES.items():
        rendered = rendered.replace(
            json.dumps(marker), "{{ .Values." + key + " | toJson }}"
        )
    return rendered


def _stage_and_publish(output: Path, contents: dict[Path, str], *, directory: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=".pycontainer-", dir=output.parent) as temp:
        stage = Path(temp)
        for relative, text in contents.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())

        if not directory:
            os.link(stage / next(iter(contents)), output)
            return

        output.mkdir()
        try:
            parents = {
                parent
                for relative in contents
                for parent in relative.parents
                if parent != Path(".")
            }
            for parent in sorted(parents, key=lambda path: len(path.parts)):
                (output / parent).mkdir()
            for relative in contents:
                os.link(stage / relative, output / relative)
        except OSError as error:
            raise OSError(
                f"Could not finish publishing {output}; the new directory may "
                f"contain partial output: {error}"
            ) from error


def generate_kubernetes(
    image: str,
    container_port: int,
    output: Path,
    *,
    name: str = "app",
    service_port: int | None = None,
) -> Path:
    """Write standalone Kubernetes resources and a matching Helm chart."""
    service_port = container_port if service_port is None else service_port
    _validate_settings(image, name, container_port, service_port)
    workload, service = _resources(name, image, container_port, service_port)
    template_workload, template_service = _resources(
        _TEMPLATE_NAME, _TEMPLATE_IMAGE, _TEMPLATE_CONTAINER_PORT, _TEMPLATE_SERVICE_PORT
    )
    values = {
        "name": name,
        "image": image,
        "containerPort": container_port,
        "servicePort": service_port,
    }
    chart = {
        "apiVersion": "v2",
        "name": name,
        "version": "0.1.0",
        "type": "application",
        "kubeVersion": ">=1.16.0-0",
    }
    contents = {
        Path("deployment.yaml"): _json_text(workload),
        Path("service.yaml"): _json_text(service),
        Path("chart") / "Chart.yaml": _json_text(chart),
        Path("chart") / "values.yaml": _json_text(values),
        Path("chart") / "values.schema.json": _json_text(_values_schema()),
        Path("chart") / "templates" / "deployment.yaml": _chart_template(template_workload),
        Path("chart") / "templates" / "service.yaml": _chart_template(template_service),
    }
    output = Path(output)
    _stage_and_publish(output, contents, directory=True)
    return output


def generate_compose(
    image: str,
    container_port: int,
    output: Path,
    *,
    name: str = "app",
    host_port: int | None = None,
) -> Path:
    """Write a Compose service referring to an existing image."""
    host_port = container_port if host_port is None else host_port
    _validate_settings(image, name, container_port, host_port)
    compose = {
        "services": {
            name: {
                "image": image,
                "ports": [{
                    "host_ip": "127.0.0.1",
                    "target": container_port,
                    "published": host_port,
                    "protocol": "tcp",
                }],
            },
        },
    }
    output = Path(output)
    _stage_and_publish(output, {Path("compose.yaml"): _json_text(compose)}, directory=False)
    return output
