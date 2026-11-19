import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from jsonschema import Draft7Validator, FormatChecker

from pycontainer import sbom as sbom_module
from pycontainer.sbom import generate_sbom


SCHEMAS = {
    "spdx": "spdx-schema-2.3.json",
    "cyclonedx": "cyclonedx-bom-1.4.schema.json",
}


@pytest.mark.parametrize("format", SCHEMAS)
def test_sbom_matches_published_schema_and_uses_tool_metadata(
    tmp_path, monkeypatch, format
):
    context = tmp_path / "schema fixture"
    context.mkdir()
    monkeypatch.setattr(sbom_module, "version", lambda _: "9.8.7")
    inventory = [("Example_Pkg", "1.2.3+local")]
    output = tmp_path / "sbom.json"

    document = generate_sbom(
        context,
        output,
        format=format,
        package_inventory=inventory,
    )
    schema_path = Path(__file__).parent / "schemas" / SCHEMAS[format]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema, format_checker=FormatChecker())

    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    assert not errors, "\n".join(
        f"{list(error.path)}: {error.message}" for error in errors
    )
    assert json.loads(output.read_text(encoding="utf-8")) == document

    if format == "spdx":
        document_id = document["documentNamespace"].rsplit("/", 1)[1]
        UUID(document_id)
        timestamp = document["creationInfo"]["created"]
        assert f"Tool: pycontainer-build-9.8.7" in document["creationInfo"]["creators"]
        assert document["packages"][0]["name"] == "Example_Pkg"
    else:
        document_id = document["serialNumber"].removeprefix("urn:uuid:")
        UUID(document_id)
        timestamp = document["metadata"]["timestamp"]
        assert document["metadata"]["tools"][0]["version"] == "9.8.7"
        assert document["components"][0]["name"] == "Example_Pkg"

    assert datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is not None


@pytest.mark.parametrize("format", SCHEMAS)
def test_empty_staged_inventory_does_not_read_requirements_or_host_packages(
    tmp_path, monkeypatch, format
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "requirements.txt").write_text(
        "not-staged==1.0\nhost-only-package==99.0\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="host-only-package==99.0\n"
        ),
    )

    document = generate_sbom(
        context,
        tmp_path / "sbom.json",
        format=format,
        package_inventory=[],
    )

    if format == "spdx":
        assert document["packages"] == []
    else:
        assert document["components"] == []


@pytest.mark.parametrize("format", SCHEMAS)
def test_sbom_rejects_invalid_staged_inventory_without_suppressing_error(
    tmp_path, format
):
    output = tmp_path / "sbom.json"

    with pytest.raises(ValueError, match="non-empty text"):
        generate_sbom(
            tmp_path,
            output,
            format=format,
            package_inventory=[("missing-version", "")],
        )

    assert not output.exists()


def test_sbom_requires_an_explicit_staged_inventory(tmp_path):
    with pytest.raises(ValueError, match="package_inventory is required"):
        generate_sbom(
            tmp_path,
            tmp_path / "sbom.json",
            package_inventory=None,
        )
