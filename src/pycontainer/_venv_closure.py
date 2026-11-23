"""Verify a staged virtualenv's dependency closure for the target image.

This file is executed as ``python -P -c <source>`` by
``dependencies.stage_virtualenv``; it is not imported by the package. It reads
a JSON request on stdin containing parsed ``METADATA`` headers and the target
marker environment, and writes a JSON list of problems to stdout. It only uses
pip's vendored ``packaging`` to parse requirements, versions and markers; it
never imports staged packages. Markers that reference a variable without a
known target value are rejected instead of being evaluated with host values.
"""
import copy
import json
import sys

PATCH_ERROR_EXIT = 87
MAX_PROBLEMS = 20


EXTRA_OPS = {"==", "!=", "in", "not in"}
CONTAINMENT_OPS = {"in", "not in"}
# The value CPython reports on Windows (os_name "nt") and never elsewhere.
WINDOWS_VALUES = {"sys_platform": "win32", "platform_system": "Windows"}


class ClosureError(Exception):
    pass


class UnsupportedExtra(ClosureError):
    pass


def _extra_operand(item):
    """Return (op, literal) for a comparison involving `extra`, else None."""
    if not any(
        type(node).__name__ == "Variable" and node.value == "extra" for node in item
    ):
        return None
    lhs, op, rhs = item
    kinds = (type(lhs).__name__, type(rhs).__name__)
    if (
        type(op).__name__ != "Op"
        or str(op.value) not in EXTRA_OPS
        or kinds not in {("Variable", "Value"), ("Value", "Variable")}
    ):
        raise UnsupportedExtra(
            "only extra ==, !=, in and not in comparisons with a literal value "
            "can be verified"
        )
    return str(op.value), (rhs if kinds[1] == "Value" else lhs)


def _walk(markers, variables, extras, containment):
    for item in markers:
        if isinstance(item, list):
            _walk(item, variables, extras, containment)
        elif isinstance(item, tuple) and len(item) == 3:
            names = {type(node).__name__ for node in item}
            if not names <= {"Variable", "Value", "Op"}:
                raise ClosureError("unrecognized marker structure")
            for node in item:
                if type(node).__name__ == "Variable":
                    variables.add(node.value)
            operand = _extra_operand(item)
            if operand is None:
                continue
            op, literal = operand
            if op in CONTAINMENT_OPS:
                # PEP 508 `in` is a substring test, not list membership, so
                # its literal does not name candidate extras.
                containment.add(op)
            else:
                extras.add(literal.value)
        elif item not in {"and", "or"}:
            raise ClosureError("unrecognized marker structure")


def _canonical_extras(markers, canonical):
    """Copy `markers` with only the literal operands of extra comparisons canonicalized."""
    rewritten = []
    for item in markers:
        if isinstance(item, list):
            rewritten.append(_canonical_extras(item, canonical))
        elif isinstance(item, tuple) and _extra_operand(item) is not None:
            rewritten.append(tuple(
                type(node)(canonical(node.value)) if type(node).__name__ == "Value" else node
                for node in item
            ))
        else:
            rewritten.append(item)
    return rewritten


def _all3(values):
    if any(value is False for value in values):
        return False
    return True if all(value is True for value in values) else None


def _any3(values):
    if any(value is True for value in values):
        return True
    return False if all(value is False for value in values) else None


def _kleene(markers, atom):
    """Evaluate `markers` like packaging, with None for atoms of unknown value."""
    groups = [[]]
    for item in markers:
        if isinstance(item, list):
            groups[-1].append(_kleene(item, atom))
        elif isinstance(item, tuple) and len(item) == 3:
            groups[-1].append(atom(item))
        elif item == "or":
            groups.append([])
        elif item != "and":
            raise ClosureError("unrecognized marker structure")
    return _any3([_all3(group) for group in groups])


def _implied(item, facts):
    """sys_platform/platform_system ==/!= their Windows value, implied by a known os_name."""
    os_name = facts.get("os_name")
    lhs, op, rhs = item
    kinds = (type(lhs).__name__, type(rhs).__name__)
    if (
        os_name not in {"nt", "posix"}
        or str(op.value) not in {"==", "!="}
        or kinds not in {("Variable", "Value"), ("Value", "Variable")}
    ):
        return None
    variable, literal = (lhs, rhs) if kinds[0] == "Variable" else (rhs, lhs)
    if WINDOWS_VALUES.get(variable.value) != literal.value:
        return None
    return (os_name == "nt") == (str(op.value) == "==")


def main():
    if sys.path and sys.path[0] in {"", "."}:
        del sys.path[0]
    request = json.load(sys.stdin)
    environment = request["environment"]
    target_minor = environment["python_version"]
    full_version = environment.get("python_full_version")
    allowed = set(environment) | {"extra"}

    try:
        from pip._vendor.packaging.markers import Marker, UndefinedComparison
        from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
        from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
        from pip._vendor.packaging.utils import canonicalize_name
        from pip._vendor.packaging.version import InvalidVersion, Version

        def marker_info(marker):
            variables, extras, containment = set(), set(), set()
            if marker is not None:
                _walk(marker._markers, variables, extras, containment)
            canonical = {canonicalize_name(extra) for extra in extras} - {""}
            return variables, canonical, containment

        def normalized(marker):
            form = copy.copy(marker)
            form._markers = _canonical_extras(marker._markers, canonicalize_name)
            return form

        def check(text, extra):
            return normalized(Marker(text)).evaluate(dict(probe, extra=extra))

        def three_valued(form, extra, facts):
            """Marker value for a host known only by `facts`; None when it is not determined."""
            known = set(facts) | {"extra"}
            env = dict(facts, extra=extra)

            def atom(item):
                variables = {
                    node.value for node in item if type(node).__name__ == "Variable"
                }
                if not variables <= known:
                    return _implied(item, facts)
                single = copy.copy(form)
                single._markers = [item]
                try:
                    return bool(single.evaluate(env))
                except (InvalidSpecifier, InvalidVersion, UndefinedComparison):
                    return None

            return _kleene(form._markers, atom)

        def tri(text, extra=""):
            return three_valued(
                normalized(Marker(text)), extra, {"os_name": "posix", "python_version": "3.11"}
            )

        machine = environment["platform_machine"]
        other = "aarch64" if machine == "x86_64" else "x86_64"
        probe = dict(environment, extra="")
        matching = Marker(f'platform_machine == "{machine}" and sys_platform == "linux"')
        opposite = Marker(f'platform_machine == "{other}"')
        unknown_variables = marker_info(Marker('platform_release >= "1"'))[0]
        listed = 'extra != "" and extra in "other,Speed_Ups"'
        if (
            not matching.evaluate(probe)
            or opposite.evaluate(probe)
            or unknown_variables != {"platform_release"}
            or marker_info(Marker('"Foo_Bar" == extra'))[1:] != ({"foo-bar"}, set())
            or marker_info(Marker(listed))[1:] != (set(), {"in"})
            or not check('extra == "Foo_Bar"', "foo-bar")
            or check('extra != "Foo_Bar"', "foo-bar")
            or not check('"Foo_Bar" == extra', "foo-bar")
            or not check(listed, "speed-ups")
            or not check(listed, "e")
            or check(listed, "")
            or not check('extra in "a,b"', "")
            or not check('"Speed" in extra', "speed-ups")
            or check('extra not in "other,Speed_Ups"', "peed-ups")
            or tri('extra == "Foo_Bar"', "foo-bar") is not True
            or tri('extra == "speed" and platform_machine != "armv7l"', "speed") is not None
            or tri('sys_platform != "win32"') is not True
            or tri('"Windows" == platform_system') is not False
            or tri('sys_platform == "linux"') is not None
            or tri('os_name == "posix" and python_version >= "3"') is not True
            or tri('platform_machine == "x" and python_version < "3"') is not False
            or tri('platform_machine == "x" or python_version >= "3"') is not True
            or tri('platform_machine == "x" and python_version < "3" or os_name == "posix"')
            is not True
            or tri('(platform_machine == "x" or os_name == "nt") and python_version >= "3"')
            is not None
            or tri('platform_release >= "1"') is not None
        ):
            raise ClosureError("target marker self-test failed")
    except Exception as exc:
        sys.stdout.write(json.dumps({"fatal": f"cannot evaluate target markers: {exc}"}))
        sys.exit(PATCH_ERROR_EXIT)

    problems = []

    def problem(message):
        if message not in problems:
            problems.append(message)

    def evaluate(requirement, extra, env):
        """Evaluate with canonical extras; None when packaging cannot evaluate it."""
        if "extra" not in requirement.info[0]:
            return requirement.parsed.marker.evaluate(dict(env, extra=extra))
        # Canonical operands make every vendored packaging version compare the
        # same spellings; `in` keeps its PEP 508 substring semantics.
        try:
            return requirement.normalized.evaluate(dict(env, extra=extra))
        except (InvalidSpecifier, InvalidVersion, UndefinedComparison):
            return None

    def active(requirement, extra, label):
        if requirement.parsed.marker is None:
            return extra == ""
        return evaluate(requirement, extra, environment) is not False

    # Only marker values the virtualenv establishes for the host that created it;
    # every other variable (platform_machine, ...) may have had any value.
    facts = request.get("host", {})

    def host_value(requirement, extra):
        if requirement.parsed.marker is None:
            return extra == ""
        if requirement.info[2]:
            # Legacy and current packaging disagree on substring matches of
            # differently spelled extras, so containment cannot prove a host.
            return None
        form = (
            requirement.normalized
            if "extra" in requirement.info[0]
            else requirement.parsed.marker
        )
        return three_valued(form, extra, facts)

    def unrequested(missing, extra, label):
        """Whether the venv host would have installed one of `missing` for it, whatever its unknowns."""
        return any(
            host_value(requirement, extra) is True
            and (not extra or host_value(requirement, "") is False)
            for requirement in missing
        )

    def not_installed(requirements):
        return [
            requirement for requirement in requirements
            if canonicalize_name(requirement.parsed.name) not in dists
        ]

    def unsatisfied(requirement, label):
        parsed = requirement.parsed
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
            version = None if spec.operator == "===" else Version(
                spec.version[:-2] if wildcard else spec.version
            )
            release = version.release if version else ()
            padded = (release + (0, 0))[:2]
            if spec.operator == "===" or (padded == minor and not (
                (spec.operator in {">=", "<"} and not any(release[2:])
                 and not (version.is_prerelease or version.is_postrelease or version.local))
                or (spec.operator in {"==", "!="} and wildcard and len(release) <= 2)
                or (spec.operator == "~=" and len(release) == 2
                    and not (version.is_prerelease or version.is_postrelease or version.local))
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
            except UnsupportedExtra as exc:
                raise ClosureError(f"{label} requires {text!r}, but {exc}") from exc
            except (InvalidRequirement, ClosureError) as exc:
                raise ClosureError(f"{label} has malformed Requires-Dist") from exc
            if "extra" in self.info[0]:
                self.normalized = normalized(self.parsed.marker)
            if self.parsed.url:
                raise ClosureError(
                    f"{label} declares direct URL dependency {self.parsed.name!r}"
                )
            unknown = sorted(self.info[0] - allowed)
            if unknown:
                raise ClosureError(
                    f"{label} requires {self.text!r}, whose marker uses "
                    f"{', '.join(unknown)} without a known target value"
                )

    dists = {}
    try:
        for record in request["distributions"]:
            label = record["path"]
            for header in ("Metadata-Version", "Name", "Version"):
                if len(record["headers"].get(header, [])) != 1:
                    raise ClosureError(f"{label} must declare exactly one {header}")
            try:
                metadata_version = Version(record["headers"]["Metadata-Version"][0].strip())
            except InvalidVersion:
                metadata_version = None
            if metadata_version is None or metadata_version.release[0] not in {1, 2}:
                raise ClosureError(f"{label} has an unsupported Metadata-Version")
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
                "provides": bool(record["headers"].get("Provides-Extra")),
            }

        for text in request.get("constraints", []):
            label = "requirements constraints"
            constraint = Parsed(text, label)
            if constraint.parsed.name and active(constraint, "", label):
                installed = dists.get(canonicalize_name(constraint.parsed.name))
                if installed is not None:
                    message = unsatisfied(constraint, label)
                    if message:
                        problem(message)

        selected = set()
        pending = [(key, "") for key in sorted(dists)]
        for label, required, lines in request["roots"]:
            requirements = [Parsed(text, label) for text in lines]
            if not required and unrequested(not_installed(requirements), "", label):
                continue
            for requirement in requirements:
                if not active(requirement, "", label):
                    continue
                message = unsatisfied(requirement, label)
                if message:
                    problem(message if required else (
                        f"Cannot determine whether {label} was selected; {message}"
                    ))
                    continue
                dependency = canonicalize_name(requirement.parsed.name)
                for requested in sorted(requirement.parsed.extras):
                    pending.append((dependency, canonicalize_name(requested)))
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
                if unrequested(not_installed(members), extra, label):
                    continue
                for requirement in members:
                    if not active(requirement, extra, label):
                        continue
                    if unsatisfied(requirement, label):
                        problem(
                            f"Cannot determine whether extra [{extra}] of "
                            f"{dist['name']} was selected when the virtualenv was "
                            f"created; on the target it requires {requirement.text!r}, "
                            "which is not satisfied"
                        )
                        break
            if not dist["provides"]:
                for requirement in dist["requirements"]:
                    if requirement.info[2] and unsatisfied(requirement, dist["label"]):
                        problem(
                            f"Cannot determine which extras of {dist['name']} were "
                            f"selected; {requirement.text!r} matches extras by "
                            "substring, it is not satisfied, and the distribution "
                            "declares no Provides-Extra"
                        )
                        break
    except ClosureError as exc:
        problem(str(exc))

    sys.stdout.write(json.dumps({"problems": problems[:MAX_PROBLEMS]}))


if __name__ == "__main__":
    main()
