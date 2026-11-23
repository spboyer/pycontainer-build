"""Run pip with environment markers evaluated for the target image.

This file is executed as ``python -c <source> <config-json> <pip args...>``
by ``dependencies.install_requirements``; it is not imported by the package.
pip selects wheels with ``--platform``/``--python-version`` but evaluates
markers against the build host, so every marker evaluation (requirements-file
roots, extras and transitive ``Requires-Dist``) is forced through the target
environment here. Markers that reference a variable whose target value is
unknown fail the install instead of falling back to host values. Host pip
configuration (environment and config files) is filtered to index and
authentication keys so it cannot change target selection options.
"""
import sys

if __name__ == "__main__" and sys.path and sys.path[0] == "":
    # Keep build-context modules from shadowing the installer's imports.
    del sys.path[0]

import json
import os
import runpy

MARKER_ERROR_EXIT = 86
PATCH_ERROR_EXIT = 87


class TargetMarkerError(Exception):
    pass


def _variables(markers):
    names = []
    for item in markers:
        if isinstance(item, list):
            names.extend(_variables(item))
        elif isinstance(item, tuple) and len(item) == 3:
            for node in item:
                if type(node).__name__ == "Variable":
                    names.append(node.value)
                elif type(node).__name__ not in {"Value", "Op"}:
                    raise TargetMarkerError("unrecognized marker structure")
        elif item not in {"and", "or"}:
            raise TargetMarkerError("unrecognized marker structure")
    return names


def _pin_pip_configuration(allowed_keys):
    """Ignore pip config-file and environment keys that could change target options."""
    from pip._internal.cli import parser
    from pip._internal.commands import create_command

    original_items = parser.ConfigOptionParser._get_ordered_configuration_items

    def configuration_items(self):
        for key, value in original_items(self):
            if key.replace("_", "-").lower() in allowed_keys:
                yield key, value

    parser.ConfigOptionParser._get_ordered_configuration_items = configuration_items

    probes = {"PIP_PLATFORM": "pycontainer-self-test", "PIP_TIMEOUT": "123"}
    # Hide other host PIP_* values (aliases such as PIP_DEFAULT_TIMEOUT) during the probe.
    saved = {
        name: value
        for name, value in os.environ.items()
        if name.upper().startswith(("PIP_", "_PIP_")) and name.upper() != "PIP_CONFIG_FILE"
    }
    for name in saved:
        del os.environ[name]
    os.environ.update(probes)
    try:
        options, _ = create_command("install").parse_args([])
    finally:
        for name in probes:
            os.environ.pop(name, None)
        os.environ.update(saved)
    if options.platforms or options.timeout != 123:
        raise TargetMarkerError("pip configuration pinning self-test failed")


def main():
    config = json.loads(sys.argv[1])
    pip_args = sys.argv[2:]
    environment = config["environment"]
    allowed = set(environment) | {"extra"}
    violations = []

    try:
        from pip._vendor.packaging import markers
        from pip._vendor.packaging.requirements import Requirement

        original_evaluate = markers.Marker.evaluate
        original_default_environment = markers.default_environment

        def default_environment():
            # Unknown keys keep host values for completeness; markers that use them are rejected.
            values = dict(original_default_environment())
            values.update(environment)
            return values

        def evaluate(self, environment_override=None, *args, **kwargs):
            try:
                referenced = _variables(self._markers)
            except (AttributeError, TypeError, TargetMarkerError) as exc:
                violations.append(f"{self}: cannot inspect marker ({exc})")
                raise TargetMarkerError(str(self)) from exc
            unknown = sorted(set(referenced) - allowed)
            if unknown:
                violations.append(f"{self}: unknown target value for {', '.join(unknown)}")
                raise TargetMarkerError(str(self))
            effective = dict(environment_override or {})
            effective.update(environment)
            effective.setdefault("extra", "")
            return original_evaluate(self, effective, *args, **kwargs)

        markers.Marker.evaluate = evaluate
        markers.default_environment = default_environment

        machine = environment["platform_machine"]
        other = "aarch64" if machine == "x86_64" else "x86_64"
        version = environment["python_version"]
        matching = Requirement(
            f'x; platform_machine == "{machine}" and sys_platform == "linux" '
            f'and python_version == "{version}"'
        )
        opposite = Requirement(f'x; platform_machine == "{other}"')
        if not matching.marker.evaluate({"extra": ""}) or opposite.marker.evaluate(
            {"extra": ""}
        ):
            raise TargetMarkerError("target marker self-test failed")

        _pin_pip_configuration(set(config["allowed_config_keys"]))
    except Exception as exc:
        with open(config["error_file"], "w", encoding="utf-8") as error_file:
            error_file.write(f"pip could not be pinned to the target: {exc}\n")
        sys.exit(PATCH_ERROR_EXIT)

    sys.argv = ["pip", *pip_args]
    try:
        runpy.run_module("pip", run_name="__main__", alter_sys=True)
        code = 0
    except SystemExit as exc:
        code = exc.code
    if violations:
        with open(config["error_file"], "w", encoding="utf-8") as error_file:
            error_file.write("\n".join(dict.fromkeys(violations)) + "\n")
        sys.exit(MARKER_ERROR_EXIT)
    sys.exit(code)


if __name__ == "__main__":
    main()
