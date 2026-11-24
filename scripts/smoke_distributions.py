"""Build and smoke-test wheel and sdist installs in temporary virtual environments."""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_in(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _metadata(artifact: Path) -> str:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as wheel:
            metadata_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
            return wheel.read(metadata_name).decode("utf-8")

    with tarfile.open(artifact, "r:gz") as source:
        metadata_name = next(name for name in source.getnames() if name.endswith("/PKG-INFO"))
        member = source.extractfile(metadata_name)
        if member is None:
            raise RuntimeError(f"Missing package metadata in {artifact}")
        return member.read().decode("utf-8")


def _check_lockfile_fixtures(sdist: Path) -> None:
    fixtures = {
        "pdm": ("pdm.lock", "pyproject.toml"),
        "pipenv": ("Pipfile.lock", "Pipfile"),
        "poetry": ("poetry.lock", "pyproject.toml"),
        "uv": ("uv.lock", "pyproject.toml"),
    }
    prefix = f"{sdist.name.removesuffix('.tar.gz')}/tests/fixtures/lockfiles"
    with tarfile.open(sdist, "r:gz") as source:
        names = set(source.getnames())
    missing = [
        f"{kind}/{name}"
        for kind, files in fixtures.items()
        for name in files
        if f"{prefix}/{kind}/{name}" not in names
    ]
    if missing:
        raise RuntimeError(f"Source distribution is missing lockfile test fixtures: {', '.join(missing)}")


def _isolated_environment():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pycontainer-package-smoke-") as temp_dir:
        temp = Path(temp_dir)
        distributions = temp / "dist"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(distributions),
                str(ROOT),
            ],
            cwd=ROOT,
            check=True,
        )

        artifacts = sorted(distributions.iterdir())
        wheel = next(path for path in artifacts if path.suffix == ".whl")
        sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))

        _check_lockfile_fixtures(sdist)

        for artifact in (wheel, sdist):
            if any(line.startswith("Requires-Dist:") for line in _metadata(artifact).splitlines()):
                raise RuntimeError(f"{artifact.name} declares runtime dependencies")

            environment = temp / artifact.name.replace(".", "-")
            venv.EnvBuilder(with_pip=True).create(environment)
            python = _python_in(environment)
            subprocess.run(
                [str(python), "-m", "pip", "install", "--no-deps", str(artifact)],
                check=True,
                env=_isolated_environment(),
            )
            module_path = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import pycontainer; print(pycontainer.__file__)",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=_isolated_environment(),
            ).stdout.strip()
            if environment.resolve() not in Path(module_path).resolve().parents:
                raise RuntimeError(f"{artifact.name} imported pycontainer outside its isolated environment")

            executable = (
                environment / "Scripts" / "pycontainer.exe"
                if os.name == "nt"
                else environment / "bin" / "pycontainer"
            )
            subprocess.run(
                [str(executable), "--help"],
                check=True,
                stdout=subprocess.DEVNULL,
                env=_isolated_environment(),
            )
            package_type = "wheel" if artifact.suffix == ".whl" else "sdist"
            for kind in ("kubernetes", "compose"):
                output = temp / f"{package_type}-{kind}"
                subprocess.run(
                    [
                        str(executable), "generate", kind,
                        "--image", "example/app:v1", "--port", "8000",
                        "--output", str(output),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    env=_isolated_environment(),
                )
                document = output / "service.yaml" if kind == "kubernetes" else output
                data = json.loads(document.read_text(encoding="utf-8"))
                if kind == "kubernetes":
                    valid = data["spec"]["ports"][0]["targetPort"] == 8000
                else:
                    valid = data["services"]["app"]["image"] == "example/app:v1"
                if not valid:
                    raise RuntimeError(f"{package_type} {kind} generation produced invalid output")
            print(f"PASS: deployment generators in isolated {package_type} install")

            sbom_output = temp / f"{package_type}-sbom.json"
            subprocess.run(
                [
                    str(python),
                    "-c",
                    (
                        "import sys; from importlib.metadata import version; "
                        "from pathlib import Path; "
                        "from pycontainer.sbom import generate_sbom; "
                        "output = Path(sys.argv[1]); "
                        "document = generate_sbom(output.parent, output, "
                        "format='cyclonedx', package_inventory=[]); "
                        "assert document['metadata']['tools'][0]['version'] "
                        "== version('pycontainer-build')"
                    ),
                    str(sbom_output),
                ],
                check=True,
                env=_isolated_environment(),
            )
            print(f"PASS: isolated {package_type} install ({artifact.name})")
            print(f"PASS: installed version in {package_type} SBOM metadata")


if __name__ == "__main__":
    main()
