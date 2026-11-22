"""Verify a staged virtualenv's dependency closure for the target image.

This file is executed as ``python -P -c <source>`` by
``dependencies.stage_virtualenv``; it is not imported by the package. It reads
a JSON request on stdin containing parsed ``METADATA`` headers and the target
marker environment, and writes a JSON list of problems to stdout. It only uses
pip's vendored ``packaging`` to parse requirements, versions and markers; it
never imports staged packages. Markers that reference a variable without a
known target value are rejected instead of being evaluated with host values.
"""
import json
import sys

PATCH_ERROR_EXIT = 87
MAX_PROBLEMS = 20


class ClosureError(Exception):
    pass


def _walk(markers, variables, extras):
    for item in markers:
        if isinstance(item, list):
            _walk(item, variables, extras)
        elif isinstance(item, tuple) and len(item) == 3:
            names = {type(node).__name__ for node in item}
            if not names <= {"Variable", "Value", "Op"}:
                raise ClosureError("unrecognized marker structure")
            for node in item:
                if type(node).__name__ == "Variable":
                    variables.add(node.value)
                    if node.value == "extra":
                        extras.update(
                            other.value
                            for other in item
                            if type(other).__name__ == "Value"
                        )
        elif item not in {"and", "or"}:
            raise ClosureError("unrecognized marker structure")


def main():
    if sys.path and sys.path[0] in {"", "."}:
        del sys.path[0]
    request = json.load(sys.stdin)
    environment = request["environment"]
    target_minor = environment["python_version"]
    full_version = environment.get("python_full_version")
    allowed = set(environment) | {"extra"}

    try:
        from pip._vendor.packaging.markers import Marker
        from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
        from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
        from pip._vendor.packaging.utils import canonicalize_name
        from pip._vendor.packaging.version import InvalidVersion, Version

        def marker_info(marker):
            variables, extras = set(), set()
            if marker is not None:
                _walk(marker._markers, variables, extras)
            return variables, {canonicalize_name(extra) for extra in extras}

        machine = environment["platform_machine"]
        other = "aarch64" if machine == "x86_64" else "x86_64"
        probe = dict(environment, extra="")
        matching = Marker(f'platform_machine == "{machine}" and sys_platform == "linux"')
        opposite = Marker(f'platform_machine == "{other}"')
        unknown_variables, _ = marker_info(Marker('platform_release >= "1"'))
        if (
            not matching.evaluate(probe)
            or opposite.evaluate(probe)
            or unknown_variables != {"platform_release"}
            or marker_info(Marker('extra == "Foo_Bar"'))[1] != {"foo-bar"}
        ):
            raise ClosureError("target marker self-test failed")
    except Exception as exc:
        sys.stdout.write(json.dumps({"fatal": f"cannot evaluate target markers: {exc}"}))
        sys.exit(PATCH_ERROR_EXIT)

    problems = []

    def problem(message):
        if message not in problems:
            problems.append(message)

    def active(requirement, extra, label):
        variables, _ = requirement.info
        unknown = sorted(variables - allowed)
        if unknown:
            raise ClosureError(
                f"{label} requires {requirement.text!r}, whose marker uses "
                f"{', '.join(unknown)} without a known target value"
            )
        if requirement.parsed.marker is None:
            return extra == ""
        return requirement.parsed.marker.evaluate(dict(environment, extra=extra))

    def unsatisfied(requirement, label):
        parsed = requirement.parsed
        if parsed.url:
            return f"{label} requires direct URL dependency {requirement.text!r}"
        installed = dists.get(canonicalize_name(parsed.name))
        if installed is None:
            return f"{label} requires {requirement.text!r}, which is not installed"
        if parsed.specifier and not parsed.specifier.contains(
            installed["version"], prereleases=True
        ):
            return (
                f"{label} requires {requirement.text!r}, but "
                f"{installed['name']} {installed['version']} is installed"
            )
        return None

    def python_compatible(specifiers):
        if full_version:
            return specifiers.contains(full_version, prereleases=True)
        minor = Version(target_minor).release[:2]
        for spec in specifiers:
            wildcard = spec.version.endswith(".*")
            release = () if spec.operator == "===" else Version(
                spec.version[:-2] if wildcard else spec.version
            ).release
            padded = (release + (0, 0))[:2]
            if spec.operator == "===" or (padded == minor and not (
                (spec.operator in {">=", "<"} and not any(release[2:]))
                or (spec.operator in {"==", "!="} and wildcard and len(release) <= 2)
                or (spec.operator == "~=" and len(release) == 2)
            )):
                raise ClosureError(
                    f"Requires-Python {spec} depends on the target patch version; "
                    "set a full PYTHON_VERSION in the base image config"
                )
        return specifiers.contains(f"{target_minor}.0", prereleases=True)

    class Parsed:
        def __init__(self, text, label):
            self.text = text
            try:
                self.parsed = Requirement(text)
                self.info = marker_info(self.parsed.marker)
            except (InvalidRequirement, ClosureError) as exc:
                raise ClosureError(f"{label} has malformed Requires-Dist {text!r}: {exc}")

    dists = {}
    try:
        for record in request["distributions"]:
            label = record["path"]
            for header in ("Name", "Version"):
                if len(record["headers"].get(header, [])) != 1:
                    raise ClosureError(f"{label} must declare exactly one {header}")
            name = record["headers"]["Name"][0].strip()
            try:
                version = Version(record["headers"]["Version"][0].strip())
            except InvalidVersion as exc:
                raise ClosureError(f"{label} has an invalid Version: {exc}")
            key = canonicalize_name(name)
            directory = record["directory"]
            dir_name, separator, dir_version = directory.rpartition("-")
            try:
                dir_matches = (
                    separator
                    and canonicalize_name(dir_name) == key
                    and Version(dir_version) == version
                )
            except InvalidVersion:
                dir_matches = False
            if not dir_matches:
                raise ClosureError(
                    f"{label} does not match its metadata {name} {version}"
                )
            if key in dists:
                raise ClosureError(f"{label} duplicates installed distribution {name}")
            requires_python = record["headers"].get("Requires-Python", [])
            if len(requires_python) > 1:
                raise ClosureError(f"{label} declares Requires-Python more than once")
            try:
                if requires_python and not python_compatible(
                    SpecifierSet(requires_python[0].strip())
                ):
                    problem(
                        f"{label} requires Python {requires_python[0].strip()}, "
                        f"but the target is CPython {full_version or target_minor}"
                    )
            except (InvalidSpecifier, InvalidVersion) as exc:
                raise ClosureError(f"{label} has malformed Requires-Python: {exc}")
            requirements = [
                Parsed(text.strip(), label)
                for text in record["headers"].get("Requires-Dist", [])
            ]
            extras = {
                canonicalize_name(extra.strip())
                for extra in record["headers"].get("Provides-Extra", [])
            }
            if "" in extras:
                raise ClosureError(f"{label} declares an empty Provides-Extra")
            for requirement in requirements:
                extras.update(requirement.info[1])
            dists[key] = {
                "name": name,
                "version": version,
                "label": label,
                "requirements": requirements,
                "extras": extras,
            }

        selected = set()
        pending = [(key, "") for key in sorted(dists)]
        while pending:
            key, extra = pending.pop()
            if (key, extra) in selected:
                continue
            selected.add((key, extra))
            dist = dists[key]
            label = f"{dist['name']} {dist['version']}" + (f" [{extra}]" if extra else "")
            for requirement in dist["requirements"]:
                if extra and "extra" not in requirement.info[0]:
                    continue
                if not active(requirement, extra, label):
                    continue
                message = unsatisfied(requirement, label)
                if message:
                    problem(message)
                    continue
                dependency = canonicalize_name(requirement.parsed.name)
                for requested in sorted(requirement.parsed.extras):
                    pending.append((dependency, canonicalize_name(requested)))

        for key in sorted(dists):
            dist = dists[key]
            for extra in sorted(dist["extras"] - {e for k, e in selected if k == key}):
                label = f"{dist['name']} {dist['version']} [{extra}]"
                members = [
                    requirement
                    for requirement in dist["requirements"]
                    if "extra" in requirement.info[0]
                ]
                invariant = [
                    requirement
                    for requirement in members
                    if requirement.info[0] == {"extra"}
                ]
                if any(
                    active(requirement, extra, label) and unsatisfied(requirement, label)
                    for requirement in invariant
                ):
                    # The host venv would contain this dependency if the extra had been selected.
                    continue
                for requirement in members:
                    if requirement in invariant or not active(requirement, extra, label):
                        continue
                    if unsatisfied(requirement, label):
                        problem(
                            f"Cannot determine whether extra [{extra}] of "
                            f"{dist['name']} was selected when the virtualenv was "
                            f"created; on the target it requires {requirement.text!r}, "
                            "which is not satisfied"
                        )
                        break
    except ClosureError as exc:
        problem(str(exc))

    sys.stdout.write(json.dumps({"problems": problems[:MAX_PROBLEMS]}))


if __name__ == "__main__":
    main()
