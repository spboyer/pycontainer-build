"""Run pip with environment markers evaluated for the target image.

This file is executed as ``python -c <source> <config-json> <pip args...>``
by ``dependencies.install_requirements``; ``venv_markers`` imports only its
side-effect-free marker helpers.
pip selects wheels with ``--platform``/``--python-version`` but evaluates
markers against the build host, so every marker evaluation (requirements-file
roots, extras and transitive ``Requires-Dist``) is forced through the target
environment here. Markers that reference a variable whose target value is
unknown fail the install instead of falling back to host values.
"""
import json
import runpy
import sys

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


def main():
    if sys.path and sys.path[0] == "":
        # Keep a pip package in the build context from shadowing the builder's pip.
        del sys.path[0]
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
    except Exception as exc:
        with open(config["error_file"], "w", encoding="utf-8") as error_file:
            error_file.write(f"pip marker evaluation could not be targeted: {exc}\n")
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
