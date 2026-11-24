"""Hermetic Python-version matrix and non-atomic publication regressions."""
import base64
import json
import hashlib
import tarfile
import subprocess
import zipfile
from dataclasses import dataclass, fields, replace
from pathlib import Path

import pytest

from pycontainer import builder as builder_module, python_matrix as matrix_module
from pycontainer.auth import RegistryAuth
from pycontainer.builder import ImageBuilder, LayerIntegrityError
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.fs_utils import output_directory_lock
from pycontainer.history import BuildHistory, BuildTiming, HistoryStorageError
from pycontainer.local_image import LocalImageError
from pycontainer.python_matrix import MatrixPublicationError, PythonMatrixBuilder, matrix_targets
from pycontainer.python_matrix import verify_python_version
from pycontainer.registry_client import RegistryClient, RegistryHTTPError, RegistryRequestError
from pycontainer.security import KUBERNETES_FRAGMENT, SECURITY_DIR
from test_multi_platform import FakeBaseRegistry, FakePushClient, _digest


def matrix(
    tmp_path, monkeypatch, platform="linux/amd64",
    base_platforms=("amd64", "arm64"), base_env=None, **overrides,
):
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "app.py").write_text("print('matrix')\n")
    bases = {}
    for minor in ("3.11", "3.12"):
        base = FakeBaseRegistry(platforms=base_platforms, base_env=base_env)
        # Rebuild every manifest and descriptor with distinct Python metadata.
        descriptors = []
        original_index = json.loads(base.manifests[base.index_digest])
        for descriptor in original_index["manifests"]:
            old_manifest = base.manifests[descriptor["digest"]]
            manifest = json.loads(old_manifest)
            old_config = manifest["config"]["digest"]
            config = base.blobs[old_config].replace(b"3.11.9", f"{minor}.9".encode())
            base.blobs[_digest(config)] = config
            manifest["config"]["digest"] = _digest(config)
            manifest["config"]["size"] = len(config)
            body = json.dumps(manifest).encode()
            base.manifests[_digest(body)] = body
            descriptors.append({**descriptor, "digest": _digest(body), "size": len(body)})
        index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                            "manifests": descriptors}).encode()
        base.index_digest = _digest(index)
        base.manifests[f"{minor}-slim"] = index
        base.manifests[base.index_digest] = index
        bases[minor] = base
    pulls = []

    def base_client(registry, repository, **kwargs):
        class Client:
            def pull_manifest(self, reference, **expected):
                for minor, base in bases.items():
                    if reference == f"{minor}-slim" or reference in base.manifests:
                        self.base = base
                        pulls.append((minor, reference))
                        return base.client().pull_manifest(reference, **expected)
                raise AssertionError(reference)

            def pull_blob(self, digest, path, size):
                return self.base.client().pull_blob(digest, path, size)
        return Client()

    monkeypatch.setattr(builder_module, "RegistryClient", base_client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(matrix_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    options = dict(
        tag="registry.example/team/app:v1.2.3", context_dir=str(ctx),
        output_dir=str(ctx / "generated"), base_image="python:3.11-slim",
        python_versions=["3.11", "3.12"], platform=platform, use_cache=False,
        latest_tag=True, semver_tags=True, exclude_defaults=False,
    )
    options.update(overrides)
    result = PythonMatrixBuilder(BuildConfig(**options))
    return result, bases, pulls


def test_bases_tags_default_and_independent_layouts(tmp_path, monkeypatch):
    build, bases, pulls = matrix(tmp_path, monkeypatch, default_python_version="3.12")
    # Pre-existing sibling outputs must never leak into either application layer.
    root = Path(build.config.output_dir)
    (root / "py3.13").mkdir(parents=True)
    (root / "py3.13" / "leak.txt").write_text("not an input")
    build.build()
    assert build.targets == {"3.11": "python:3.11-slim", "3.12": "python:3.12-slim"}
    for version, child in build.children.items():
        out = root / f"py{version}"
        assert (out / "oci-layout").is_file()
        assert len(child.platform_images) == 1
        assert child._base_image_tag_verified is True
        layer = child.platform_images[0].layers[-1]
        with tarfile.open(layer.tar_path) as tar:
            assert all("generated" not in name for name in tar.getnames())
        tags = sorted(p.name for p in (out / "refs" / "tags").iterdir())
        expected = [f"v1.2.3-py{version}"]
        if version == "3.12":
            expected += ["v1.2.3", "v1.2", "v1", "latest"]
        assert tags == sorted(expected)
        layout = json.loads((out / "index.json").read_text())
        assert {entry["annotations"]["org.opencontainers.image.ref.name"].rsplit(":", 1)[1]
                for entry in layout["manifests"]} == set(expected)
    assert {minor for minor, _ in pulls} == {"3.11", "3.12"}
    assert not (root / "index.json").exists()


def test_matrix_base_mirror_fallback_uses_endpoint_credentials(tmp_path, monkeypatch):
    build, _, _ = matrix(
        tmp_path, monkeypatch, registry_mirrors={"docker.io": ["mirror.example"]},
    )
    base_client = builder_module.RegistryClient
    connections = []
    requests = []

    def client(registry, repository, **kwargs):
        connections.append((registry, kwargs["auth_token"]))
        delegate = base_client(registry, repository, **kwargs)

        class Client:
            def pull_manifest(self, reference, **expected):
                requests.append((registry, reference))
                if registry == "mirror.example" and reference == "3.11-slim":
                    raise RegistryHTTPError(404, "manifest not found")
                return delegate.pull_manifest(reference, **expected)

            def pull_blob(self, digest, path, size):
                return delegate.pull_blob(digest, path, size)

        return Client()

    def upstream_auth(host, *, include_generic_environment):
        assert host == "docker.io"
        assert not include_generic_environment
        return RegistryAuth(bearer_token="upstream-token")

    monkeypatch.setattr(builder_module, "RegistryClient", client)
    monkeypatch.setattr(builder_module, "get_registry_auth", upstream_auth)
    monkeypatch.setattr(
        builder_module, "get_mirror_auth",
        lambda host: RegistryAuth(bearer_token=f"{host}-token"),
    )
    build.build()

    assert ("mirror.example", "3.11-slim") in requests
    assert ("docker.io", "3.11-slim") in requests
    assert ("mirror.example", "3.12-slim") in requests
    assert ("docker.io", "3.12-slim") not in requests
    assert set(connections) == {
        ("mirror.example", "mirror.example-token"),
        ("docker.io", "upstream-token"),
    }


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_matrix_preserves_image_metadata_for_every_version(tmp_path, monkeypatch, platform):
    labels = {"example.label": "configured"}
    manifest_annotations = {"example.manifest": "manifest-value"}
    index_annotations = {"example.index": "index-value"}
    build, _, _ = matrix(
        tmp_path, monkeypatch, platform=platform, labels=labels,
        manifest_annotations=manifest_annotations, index_annotations=index_annotations,
    )
    build.build()
    for child in build.children.values():
        root = Path(child.config.output_dir)
        blobs = root / "blobs" / "sha256"
        layout = json.loads((root / "index.json").read_text())
        assert layout["annotations"]["example.index"] == "index-value"
        if child.index_digest:
            index = json.loads((blobs / child.index_digest.split(":")[1]).read_text())
            assert index["annotations"] == index_annotations
        for image in child.platform_images:
            manifest = json.loads((blobs / image.manifest_digest.split(":")[1]).read_text())
            config = json.loads((blobs / image.config_digest.split(":")[1]).read_text())
            assert manifest["annotations"] == manifest_annotations
            assert config["config"]["Labels"]["example.label"] == "configured"
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    build.push(show_progress=False)
    assert all(operation.outcome == "confirmed" for operation in build.operations)


@pytest.mark.parametrize("label", ["example.notes", "framework"])
def test_matrix_release_notes_are_snapshotted_once_for_all_versions(tmp_path, monkeypatch, label):
    notes = tmp_path / "notes.md"
    notes.write_text("original notes", encoding="utf-8")
    matrix_build, _, _ = matrix(
        tmp_path, monkeypatch, release_notes_file=str(notes),
        release_notes_label=label,
    )
    (Path(matrix_build.config.context_dir) / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    builder = ImageBuilder(matrix_build.config)
    notes.unlink()
    builder.build()
    builder.build()
    for child in builder._python_matrix.children.values():
        blobs = Path(child.config.output_dir) / "blobs" / "sha256"
        image = child.platform_images[0]
        config = json.loads((blobs / image.config_digest.split(":")[1]).read_text())
        assert config["config"]["Labels"][label] == "original notes"


def test_matrix_history_records_one_run_with_all_child_phases(tmp_path, monkeypatch):
    history_dir = tmp_path / "history"
    matrix_build, _, _ = matrix(
        tmp_path, monkeypatch, platform="linux/amd64,linux/arm64",
        record_history=True, history_dir=str(history_dir),
    )
    phases = []
    original = BuildTiming.phase

    def phase(timing, name):
        phases.append(name)
        return original(timing, name)

    monkeypatch.setattr(BuildTiming, "phase", phase)
    ImageBuilder(matrix_build.config).build()
    records = BuildHistory(history_dir).list()
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert {"base_image", "application"} <= records[0]["phases_ns"].keys()
    assert phases.count("base_image") == 4
    assert phases.count("application") == 2


def test_matrix_history_records_failure_and_blocks_publication(tmp_path, monkeypatch):
    history_dir = tmp_path / "history"
    matrix_build, _, _ = matrix(
        tmp_path, monkeypatch, record_history=True, history_dir=str(history_dir),
    )
    original = ImageBuilder._pull_base_image

    def fail_later(child, *args):
        if child.expected_python_version == "3.12":
            raise RuntimeError("synthetic build failure")
        return original(child, *args)

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", fail_later)
    builder = ImageBuilder(matrix_build.config)
    with pytest.raises(RuntimeError, match="synthetic build failure"):
        builder.build()
    records = BuildHistory(history_dir).list()
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert "base_image" in records[0]["phases_ns"]
    with pytest.raises(RuntimeError, match="every Python"):
        builder.push()


def test_matrix_history_storage_failure_stops_build(tmp_path, monkeypatch):
    matrix_build, _, _ = matrix(
        tmp_path, monkeypatch, record_history=True, history_dir=str(tmp_path / "history"),
    )
    monkeypatch.setattr(
        BuildHistory, "append",
        lambda self, record: (_ for _ in ()).throw(HistoryStorageError("synthetic storage failure")),
    )
    with pytest.raises(HistoryStorageError, match="synthetic storage failure"):
        ImageBuilder(matrix_build.config).build()


def test_matrix_dry_run_does_not_record_history(tmp_path, monkeypatch):
    history_dir = tmp_path / "history"
    matrix_build, _, _ = matrix(
        tmp_path, monkeypatch, record_history=True, history_dir=str(history_dir),
        dry_run=True,
    )
    ImageBuilder(matrix_build.config).build()
    assert not history_dir.exists()


def test_matrix_scan_is_rejected_before_builder_or_push(tmp_path, monkeypatch, capsys):
    from pycontainer import cli

    def fail(*args, **kwargs):
        pytest.fail("matrix scan reached builder or registry")

    monkeypatch.setattr(cli, "ImageBuilder", fail)
    monkeypatch.setattr("sys.argv", [
        "pycontainer", "build", "--python-version", "3.11,3.12", "--scan", "--push",
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "scanning is not supported for Python version matrices" in capsys.readouterr().err


def test_matrix_git_derived_tags_are_rejected_at_configuration(tmp_path):
    # #212 adds BuildConfig.tag_from_git, whose __post_init__ rewrites tag and
    # would erase each child's -pyX.Y suffix; reject before any build exists.
    if "tag_from_git" in {f.name for f in fields(BuildConfig)}:
        GitTagConfig = BuildConfig
    else:
        @dataclass
        class GitTagConfig(BuildConfig):
            tag_from_git: bool = False

    for args in (["init", "-q"], ["-c", "user.email=t@example.invalid", "-c", "user.name=t",
                                  "commit", "-q", "--allow-empty", "-m", "x"], ["tag", "v1.2.3"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    options = dict(context_dir=str(tmp_path), python_versions=["3.11", "3.12"])
    GitTagConfig(**options)
    with pytest.raises(ValueError, match="tag_from_git"):
        GitTagConfig(tag_from_git=True, **options)


def test_matrix_git_derived_tags_prevent_build_and_publication(tmp_path, monkeypatch):
    build, _, pulls = matrix(tmp_path, monkeypatch)
    build.config.tag_from_git = True
    with pytest.raises(ValueError, match="tag_from_git"):
        build.build()
    assert pulls == []
    with pytest.raises(ValueError, match="tag_from_git"):
        PythonMatrixBuilder(build.config)

    build.config.tag_from_git = False
    build.build()
    build.config.tag_from_git = True
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(ValueError, match="tag_from_git"):
        build.push()
    assert push.calls == [] and build.operations == []


def test_enabling_scan_after_matrix_build_prevents_publication(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    build.config.scan = True
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(ValueError, match="scanning is not supported for Python version matrices"):
        build.push()
    assert push.calls == []


def test_rejected_matrix_scan_build_invalidates_previous_build(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    build.config.scan = True
    with pytest.raises(ValueError, match="scanning is not supported for Python version matrices"):
        build.build()
    build.config.scan = False
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_matrix_layout_references_encode_tags_invalid_in_oci(tmp_path, monkeypatch, platform):
    build, _, _ = matrix(
        tmp_path, monkeypatch, platform=platform, tag="registry.example/team/app:_dev",
        tags=["stable"], latest_tag=False, semver_tags=False,
    )
    build.build()
    for version, child in build.children.items():
        root = Path(child.config.output_dir)
        index = json.loads((root / "index.json").read_text())
        first = index["manifests"][0]
        encoded = "pycontainer-" + child.config.tag.encode("utf-8").hex()
        assert first["annotations"]["org.opencontainers.image.ref.name"] == encoded
        assert first["digest"] == child.manifest_digest
        assert (root / "refs" / "tags" / f"_dev-py{version}").read_text() == child.manifest_digest
        if child.index_digest is None:
            assert index["annotations"]["org.opencontainers.image.ref.name"] == encoded


def test_each_minor_has_its_own_architecture_index_and_deps(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64", include_deps=True)
    targets = []

    def deps(child, layers_dir, **kwargs):
        targets.append((child.expected_python_version, kwargs["arch"],
                        kwargs["base_config"]["config"]["Env"][1]))
        return None
    monkeypatch.setattr(ImageBuilder, "_create_deps_layer", deps)
    build.build()
    assert [(v, arch) for v, arch, _ in targets] == [
        ("3.11", "amd64"), ("3.11", "arm64"), ("3.12", "amd64"), ("3.12", "arm64")
    ]
    assert all(f"PYTHON_VERSION={v}.9" == metadata for v, _, metadata in targets)
    for child in build.children.values():
        assert child.index_digest
        assert [image.platform["architecture"] for image in child.platform_images] == ["amd64", "arm64"]


def test_changing_default_removes_current_local_aliases_from_old_default(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    build = PythonMatrixBuilder(replace(build.config, default_python_version="3.12"))
    build.build()
    for version, child in build.children.items():
        refs = Path(child.config.output_dir) / "refs" / "tags"
        assert (refs / f"v1.2.3-py{version}").exists()
        for alias in ("latest", "v1.2.3", "v1.2", "v1"):
            assert (refs / alias).exists() == (version == "3.12")


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_matrix_security_fragments_follow_each_layout_and_are_removed(tmp_path, monkeypatch, platform):
    build, _, _ = matrix(
        tmp_path, monkeypatch, platform=platform,
        security={"kubernetes": {"read_only_root_filesystem": True}},
    )
    build.build()
    digests = {version: child.manifest_digest for version, child in build.children.items()}
    for child in build.children.values():
        fragment = Path(child.config.output_dir) / SECURITY_DIR / KUBERNETES_FRAGMENT
        assert json.loads(fragment.read_text()) == {"readOnlyRootFilesystem": True}

    plain = PythonMatrixBuilder(replace(build.config, security=None))
    plain.build()
    assert {version: child.manifest_digest for version, child in plain.children.items()} == digests
    assert all(not (Path(child.config.output_dir) / SECURITY_DIR).exists()
               for child in plain.children.values())


def test_matrix_invalid_security_profile_fails_before_any_base_pull(tmp_path, monkeypatch):
    build, _, pulls = matrix(
        tmp_path, monkeypatch, security={"oci": {"seccomp_profile": "missing.json"}}
    )
    with pytest.raises(ValueError, match="Seccomp profile not found"):
        build.build()
    assert pulls == []
    assert not build.built


def test_matrix_build_failure_preserves_existing_security_fragments(tmp_path, monkeypatch):
    build, _, _ = matrix(
        tmp_path, monkeypatch, security={"kubernetes": {"read_only_root_filesystem": True}}
    )
    build.build()
    fragments = {
        version: Path(child.config.output_dir) / SECURITY_DIR / KUBERNETES_FRAGMENT
        for version, child in build.children.items()
    }
    previous = {version: path.read_bytes() for version, path in fragments.items()}
    changed = PythonMatrixBuilder(replace(
        build.config, security={"kubernetes": {"read_only_root_filesystem": False}}
    ))

    def fail():
        raise RuntimeError("later Python build failed")

    monkeypatch.setattr(changed.children["3.12"], "_build_image", fail)
    with pytest.raises(RuntimeError, match="later Python build failed"):
        changed.build()
    assert {version: path.read_bytes() for version, path in fragments.items()} == previous
    assert not changed.built


@pytest.mark.parametrize("base", ["custom:stable", "python:3.11-slim@" + "sha256:" + "a" * 64,
                                 "oci:base", "python:latest", "registry.example/python:3.11-slim",
                                 "python:3.11.9-slim"])
def test_custom_and_pinned_bases_require_maps(base):
    with pytest.raises(ValueError, match="explicit"):
        BuildConfig(python_versions=["3.11", "3.12"], base_image=base)
    cfg = BuildConfig(python_versions=["3.11", "3.12"], base_image=base,
                      python_matrix={"3.11": "oci:eleven", "3.12": "custom:twelve"})
    assert matrix_targets(cfg) == cfg.python_matrix


@pytest.mark.parametrize("options", [
    {"python_versions": ["3.11", "3.11"]},
    {"python_versions": ["../3.11"]},
    {"python_versions": ["3.11", ""]},
    {"python_versions": ["3.11"], "default_python_version": "3.12"},
    {"python_versions": ["3.11"], "python_matrix": {"3.12": "python:3.12"}},
    {"python_matrix": {"3.11": "python:3.11"}},
    {"python_versions": ["3.11"], "tag": "app:" + "a" * 125},
    {"python_versions": ["3.11"], "tags": ["latest-py3.11"]},
])
def test_invalid_matrix_fails_before_build(options):
    with pytest.raises(ValueError):
        BuildConfig(**options)


@pytest.mark.parametrize("reference", ["registry.example/base:", "https://registry.example/base:v1",
                                      "oci:", "docker-archive:"])
def test_invalid_explicit_base_rejected_during_configuration(reference):
    with pytest.raises(ValueError, match="Python 3.12: invalid"):
        BuildConfig(python_versions=["3.11", "3.12"], python_matrix={"3.12": reference})


@pytest.mark.parametrize("config", [None, {"config": []}, {"config": {"Env": "PYTHON_VERSION=3.11"}},
                                    {"config": {"Env": [None]}}])
def test_malformed_base_metadata_is_explicit(config):
    with pytest.raises(ValueError, match="Python 3.11.*malformed"):
        verify_python_version("custom:base", config, "3.11", "linux/amd64")


def test_custom_cpython_metadata_is_case_insensitive():
    verify_python_version("custom:base", {"config": {"Env": [
        "PYTHON_VERSION=3.11.9", "PYTHON_IMPLEMENTATION=cpython"
    ]}}, "3.11", "linux/amd64")


@pytest.mark.parametrize("environment", [[], ["PYTHON_VERSION=3.11.9"],
                                         ["PYTHON_VERSION=3.12.9", "PYTHON_IMPLEMENTATION=PyPy"]])
def test_minor_validation_even_without_dependencies(tmp_path, monkeypatch, environment):
    build, _, _ = matrix(tmp_path, monkeypatch, env={"PYTHON_VERSION": "3.12.9"})
    original = ImageBuilder._pull_base_image

    def pull(child, *args):
        layers, config = original(child, *args)
        if child.expected_python_version == "3.12":
            config["config"]["Env"] = environment
        return layers, config
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull)
    push = FakePushClient()
    with pytest.raises(ValueError, match="Python 3.12"):
        build.build()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()
    assert push.calls == []


def test_all_content_before_tags_and_aliases_last(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64")
    build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    build.push(show_progress=False)
    tags = [i for i, call in enumerate(push.calls) if call[0] != "blob" and not call[1].startswith("sha256:")]
    assert all(call[0] == "blob" or call[1].startswith("sha256:") for call in push.calls[:tags[0]])
    assert [push.calls[i][1] for i in tags] == [
        "v1.2.3-py3.11", "v1.2.3-py3.12", "v1.2.3", "v1.2", "v1", "latest"
    ]
    assert all(op.outcome == "confirmed" for op in build.operations)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
@pytest.mark.parametrize("primary", ["v9", "stable", "v1.2"])
def test_matrix_push_override_replaces_primary_and_returns_destination(
    tmp_path, monkeypatch, platform, primary
):
    build, _, _ = matrix(
        tmp_path, monkeypatch, platform=platform, tags=["stable"],
        default_python_version="3.12",
    )
    builder = ImageBuilder(build.config)
    builder.build()
    push = FakePushClient()
    clients = []

    def client(registry, repository, **kwargs):
        clients.append((registry, repository))
        return push

    monkeypatch.setattr(builder_module, "RegistryClient", client)
    destination = f"mirror.example/other/app:{primary}"
    assert builder.push(registry_url=destination, show_progress=False) == destination
    assert clients == [("mirror.example", "other/app")]
    publications = builder._python_matrix.operations
    tag_operations = [op for op in publications if "@" not in op.reference]
    explicit = list(dict.fromkeys([primary, "stable"]))
    expected = [
        f"mirror.example/other/app:{name}-py{version}"
        for version in ("3.11", "3.12") for name in explicit
    ] + [
        f"mirror.example/other/app:{name}"
        for name in dict.fromkeys([primary, "stable", "v1.2", "v1", "latest"])
    ]
    assert [op.reference for op in tag_operations] == expected
    written_tags = [
        call[1] for call in push.calls if call[0] != "blob" and not call[1].startswith("sha256:")
    ]
    assert written_tags == [ref.rsplit(":", 1)[1] for ref in expected]
    assert len(written_tags) == len(set(written_tags))
    assert all(op.outcome == "confirmed" for op in publications)
    first_tag = next(i for i, op in enumerate(publications) if "@" not in op.reference)
    assert all("@" in op.reference for op in publications[:first_tag])
    for op in tag_operations:
        version = "3.11" if op.reference.endswith("-py3.11") else "3.12"
        assert op.path.name == builder._python_matrix.children[version].manifest_digest.split(":")[1]
    assert build.config.tag == "registry.example/team/app:v1.2.3"


def test_matrix_override_failure_reports_destination_without_retry(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    attempts = []

    def fail(call):
        attempts.append(call)
        return call[0] == "manifest" and call[1] == "v9"

    push = FakePushClient(fail_on=fail)
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(MatrixPublicationError) as raised:
        build.push(registry_url="mirror.example/other/app:v9", show_progress=False)
    tags = [op for op in raised.value.operations if "@" not in op.reference]
    assert [(op.reference, op.outcome) for op in tags] == [
        ("mirror.example/other/app:v9-py3.11", "confirmed"),
        ("mirror.example/other/app:v9-py3.12", "confirmed"),
        ("mirror.example/other/app:v9", "unknown"),
        ("mirror.example/other/app:v1.2", "unattempted"),
        ("mirror.example/other/app:v1", "unattempted"),
        ("mirror.example/other/app:latest", "unattempted"),
    ]
    assert "mirror.example/other/app:v9" in str(raised.value)
    assert sum(call[1] == "v9" for call in attempts) == 1
    assert not any(call[1] in ("v1.2", "v1", "latest") for call in push.calls)


@pytest.mark.parametrize("destination", [
    "mirror.example/other/app:" + "x" * 125,
    "mirror.example/other/app@sha256:" + "a" * 64,
    "mirror.example/other/app:stable-py3.11",
])
def test_matrix_invalid_override_starts_no_publication(tmp_path, monkeypatch, destination):
    build, _, _ = matrix(tmp_path, monkeypatch, tags=["stable"])
    build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(ValueError):
        build.push(registry_url=destination, show_progress=False)
    assert push.calls == []
    assert build.operations == []


@pytest.mark.parametrize("destination, insecure", [
    ("registry.example/team/app:v1", True),
    ("other.example/team/app:v1", False),
])
def test_matrix_publication_uses_registry_transport_settings(
    tmp_path, monkeypatch, caplog, destination, insecure
):
    monkeypatch.setattr("pycontainer.registry_client.create_registry_ssl_context", lambda path: None)
    build, _, _ = matrix(
        tmp_path, monkeypatch, insecure_registries=["registry.example"],
        registry_ca_file="example-ca.pem",
    )
    build.build()
    push = FakePushClient()
    calls = []

    def client(registry, repository, **kwargs):
        calls.append((registry, repository, kwargs))
        return push

    monkeypatch.setattr(builder_module, "RegistryClient", client)
    build.push(registry_url=destination, show_progress=False)
    assert calls == [(destination.split("/")[0], "team/app", {
        "auth_token": None, "username": None, "password": None,
        "insecure": insecure, "ca_file": "example-ca.pem",
    })]
    assert ("using plain HTTP" in caplog.text) is insecure
    assert push.calls


@pytest.mark.parametrize("failure_kind", [
    "blob", "manifest", "index", "tag", "unknown", "server-error",
    "rate-limit", "quota", "server-quota", "timeout-quota",
])
def test_failure_report_and_no_retry_or_rollback(tmp_path, monkeypatch, failure_kind):
    build, _, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64")
    build.build()
    calls, remote_tags = [], {}

    class Client(FakePushClient):
        def _record(self, call):
            calls.append(call)
            is_tag = call[0] != "blob" and not call[1].startswith("sha256:")
            fail = (
                failure_kind == "blob" and call[0] == "blob"
                or failure_kind == "manifest" and call[0] == "manifest" and not is_tag
                or failure_kind == "index" and call[0] == "index" and not is_tag
                or failure_kind in (
                    "tag", "unknown", "server-error", "rate-limit", "quota",
                    "server-quota", "timeout-quota",
                ) and is_tag and call[1] == "v1.2"
            )
            if fail:
                if failure_kind == "unknown":
                    remote_tags[call[1]] = call[2]
                    remote_tags[call[1]] = "concurrent-writer"
                    raise RegistryRequestError("timeout after PUT")
                if failure_kind == "server-error":
                    raise RegistryHTTPError(503, "server response may follow a committed write")
                if failure_kind in ("rate-limit", "quota", "server-quota", "timeout-quota"):
                    status = {"rate-limit": 429, "quota": 413,
                              "server-quota": 503, "timeout-quota": 408}[failure_kind]
                    body = b"" if failure_kind == "rate-limit" else (
                        b'{"errors":[{"code":"QUOTA_EXCEEDED","message":"quota"}]}'
                    )
                    raise RegistryClient("registry.example", "team/app")._error(
                        "Index push", "https://registry.example/v2/team/app/manifests/v1.2",
                        status, body, {},
                    )
                raise RegistryHTTPError(403, "rejected")
            if is_tag:
                remote_tags[call[1]] = call[2]
    monkeypatch.setattr(builder_module, "RegistryClient", Client())
    with pytest.raises(MatrixPublicationError) as caught:
        build.push(show_progress=False)
    error = caught.value
    outcome = "unknown" if failure_kind in (
        "unknown", "server-error", "server-quota", "timeout-quota"
    ) else "failed"
    affected = [op for op in error.operations if op.outcome == outcome]
    assert len(affected) == 1
    assert error.operations[-1].outcome == "unattempted"
    assert all(label in str(error) for label in ("confirmed:", "failed:", "unknown:", "unattempted:"))
    if failure_kind in ("blob", "manifest", "index"):
        assert remote_tags == {}
    else:
        assert "v1.2.3" in remote_tags
        assert sum(call[1] == "v1.2" for call in calls) == 1
    if failure_kind == "unknown":
        assert remote_tags["v1.2"] == "concurrent-writer"
    assert "No rollback" in str(error)


def test_corrupt_later_layout_prevents_any_publication(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    child = build.children["3.12"]
    path = Path(child.config.output_dir) / "blobs" / "sha256" / child.manifest_digest.split(":")[1]
    path.write_bytes(b"corrupt")
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(LayerIntegrityError):
        build.push()
    assert push.calls == []


def test_matrix_and_child_output_locks_exclude_concurrent_builds(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    for output in (build.config.output_dir, build.children["3.12"].config.output_dir):
        with output_directory_lock(output):
            with pytest.raises(RuntimeError, match="Another build"):
                build.push()
    assert push.calls == []


def test_locks_remain_held_during_remote_writes(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()

    class Client(FakePushClient):
        def _record(self, call):
            for output in (build.config.output_dir, build.children["3.12"].config.output_dir):
                with pytest.raises(RuntimeError, match="Another build"):
                    with output_directory_lock(output):
                        pytest.fail("publication released its lock")
            super()._record(call)
    push = Client()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    build.push(show_progress=False)
    assert push.calls


def test_authentication_failure_reports_all_unattempted_content(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch)
    build.build()

    def auth(*args, **kwargs):
        raise ValueError("credentials unavailable")
    monkeypatch.setattr(matrix_module, "get_registry_auth", auth)
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(MatrixPublicationError) as caught:
        build.push()
    assert caught.value.operations[0].kind == "authentication"
    assert caught.value.operations[0].outcome == "failed"
    assert all(op.outcome == "unattempted" for op in caught.value.operations[1:])
    assert push.calls == []


def test_cache_directory_inside_context_is_not_an_input(tmp_path, monkeypatch):
    cache = tmp_path / "context" / "private-cache"
    build, _, _ = matrix(tmp_path, monkeypatch, cache_dir=str(cache), use_cache=True)
    cache.mkdir(exist_ok=True)
    (cache / "leak").write_text("not an input")
    build.build()
    build.build()
    for child in build.children.values():
        with tarfile.open(child.platform_images[0].layers[-1].tar_path) as tar:
            assert all("private-cache" not in name and ".pycontainer-build-" not in name
                       for name in tar.getnames())


def test_target_wheel_staging_uses_each_minor_and_architecture(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64", include_deps=True)
    (Path(build.config.context_dir) / "requirements.txt").write_text("synthetic==1\n")
    targets = []

    def install(requirements, site, ctx, os_name, arch, version, base, config, **_):
        targets.append((version, os_name, arch))
        site.mkdir()
        (site / "synthetic.py").write_text(f"TARGET = '{version}/{arch}'\n")
        return [requirements]
    monkeypatch.setattr(builder_module, "install_requirements", install)
    build.build()
    assert targets == [("3.11", "linux", "amd64"), ("3.11", "linux", "arm64"),
                       ("3.12", "linux", "amd64"), ("3.12", "linux", "arm64")]


def _write_matrix_lock_project(context, *, include_stubs=False):
    from test_dependency_staging import _write_named_wheel
    from test_lockfiles import _write_target_marker_uv_project

    _write_target_marker_uv_project(context)
    for path in (context / "pyproject.toml", context / "uv.lock"):
        text = path.read_text(encoding="utf-8")
        for machine, condition in (("x86_64", "< '3.12'"), ("aarch64", ">= '3.12'")):
            marker = f"platform_machine == '{machine}'"
            text = text.replace(
                marker, f"{marker} and python_version {condition}",
                2 if path.name == "uv.lock" else 1,
            )
        path.write_text(text, encoding="utf-8")

    wheelhouse = context / "wheelhouse"
    original = wheelhouse / "fixture_dep-1.0-py3-none-any.whl"
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    requires = [
        'marker-trans-amd; platform_machine == "x86_64"',
        'marker-trans-arm; platform_machine == "aarch64"',
        'marker-trans-armv7; platform_machine == "armv7l"',
        'marker-trans-win; sys_platform == "win32"',
    ]
    wheel_refs = []
    for minor in ("311", "312"):
        for machine in ("x86_64", "aarch64", "armv7l"):
            tag = f"cp{minor}-cp{minor}-manylinux2014_{machine}"
            _write_named_wheel(wheelhouse, "fixture-dep", requires=requires, tag=tag)
            wheel = wheelhouse / f"fixture_dep-1.0-{tag}.whl"
            if include_stubs:
                with zipfile.ZipFile(wheel) as archive:
                    files = {name: archive.read(name) for name in archive.namelist()}
                stub = b"value: int\n"
                stub_hash = base64.urlsafe_b64encode(hashlib.sha256(stub).digest()).rstrip(b"=")
                files["fixture_dep/__init__.pyi"] = stub
                files["fixture_dep-1.0.dist-info/RECORD"] += (
                    f"fixture_dep/__init__.pyi,sha256={stub_hash.decode()},{len(stub)}\n".encode()
                )
                with zipfile.ZipFile(wheel, "w") as archive:
                    for name, content in files.items():
                        archive.writestr(name, content)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            wheel_refs.append(
                f'{{ url = "https://example.invalid/{wheel.name}", hash = "sha256:{digest}" }}'
            )
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    original_ref = (
        f'{{ url = "https://example.invalid/{original.name}", '
        f'hash = "sha256:{original_hash}" }}'
    )
    assert original_ref in text
    lock.write_text(text.replace(original_ref, ", ".join(wheel_refs), 1), encoding="utf-8")
    original.unlink()


@pytest.mark.parametrize("strip_type_stubs", [False, True])
def test_locked_matrix_selects_target_markers_wheels_and_override(
    tmp_path, monkeypatch, strip_type_stubs
):
    build, _, pulls = matrix(
        tmp_path, monkeypatch, platform="linux/amd64,linux/arm64,linux/arm/v7",
        base_platforms=("amd64", "arm64", "arm/v7"), include_deps=True,
        default_python_version="3.12", tags=["stable"], strip_type_stubs=strip_type_stubs,
        build_args={"VALUE": "${NOT_AN_ARGUMENT}"},
        labels={"literal": "${VALUE}", "escaped": "$${VALUE}"},
    )
    context = Path(build.config.context_dir)
    _write_matrix_lock_project(context, include_stubs=True)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    base_auth = []

    def pull_auth(host, *, include_generic_environment, log_redact):
        assert log_redact("${NOT_AN_ARGUMENT}") == "<redacted>"
        base_auth.append((host, include_generic_environment))
        return RegistryAuth(bearer_token="base-token")

    original_client = builder_module.RegistryClient
    pull_clients = []

    def pull_client(host, repository, **kwargs):
        pull_clients.append((host, kwargs["auth_token"], kwargs["username"], kwargs["password"]))
        return original_client(host, repository, **kwargs)

    real_install = builder_module.install_requirements
    installs = []

    def install(requirements, site, ctx, os_name, arch, version, base, config, **kwargs):
        installs.append((version, arch, kwargs["variant"], kwargs["locked"],
                         kwargs["base_image_tag_verified"]))
        return real_install(requirements, site, ctx, os_name, arch, version, base, config, **kwargs)

    monkeypatch.setattr(builder_module, "get_registry_auth", pull_auth)
    monkeypatch.setattr(builder_module, "RegistryClient", pull_client)
    monkeypatch.setattr(builder_module, "install_requirements", install)
    build.build()

    assert installs == [
        (version, arch, variant, True, True)
        for version in ("3.11", "3.12")
        for arch, variant in (("amd64", None), ("arm64", None), ("arm", "v7"))
    ]
    assert base_auth == [("docker.io", False)] * 6
    assert pull_clients == [("docker.io", "base-token", None, None)] * 6
    assert {
        version: sum(minor == version and ref == f"{version}-slim"
                     for minor, ref in pulls)
        for version in ("3.11", "3.12")
    } == {"3.11": 1, "3.12": 1}
    machines = {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7l"}
    suffixes = {"amd64": "amd", "arm64": "arm", "arm": "armv7"}
    for version, child in build.children.items():
        assert child.config.labels["literal"] == "${NOT_AN_ARGUMENT}"
        assert child.config.labels["escaped"] == "${VALUE}"
        assert child.config.redact("${NOT_AN_ARGUMENT}") == "<redacted>"
        for image in child.platform_images:
            arch = image.platform["architecture"]
            with tarfile.open(image.layers[-2].tar_path) as archive:
                names = set(archive.getnames())
                wheel = archive.extractfile(
                    "app/.python/site-packages/fixture_dep-1.0.dist-info/WHEEL"
                ).read().decode()
            tag = f"cp{version.replace('.', '')}-cp{version.replace('.', '')}-manylinux2014_{machines[arch]}"
            assert f"Tag: {tag}" in wheel
            assert ("app/.python/site-packages/fixture_dep/__init__.pyi" in names) != strip_type_stubs
            for candidate, suffix in suffixes.items():
                root = f"app/.python/site-packages/marker_root_{suffix}/__init__.py"
                transitive = f"app/.python/site-packages/marker_trans_{suffix}/__init__.py"
                expected_root = candidate == arch and (
                    candidate == "arm"
                    or (candidate == "amd64" and version == "3.11")
                    or (candidate == "arm64" and version == "3.12")
                )
                assert (root in names) == expected_root
                assert (transitive in names) == (candidate == arch)
            assert not any("marker_trans_win/" in name for name in names)

    push = FakePushClient()
    push_auth = []
    push_clients = []

    def destination_auth(host, **kwargs):
        push_auth.append((host, kwargs))
        return RegistryAuth(bearer_token="push-token")

    def destination_client(host, repository, **kwargs):
        push_clients.append((host, repository, kwargs["auth_token"]))
        return push

    monkeypatch.setattr(matrix_module, "get_registry_auth", destination_auth)
    monkeypatch.setattr(builder_module, "RegistryClient", destination_client)
    destination = "mirror.example/team/app:v9"
    assert build.push(registry_url=destination, auth_token="explicit-token", show_progress=False) == destination
    assert push_auth[0][1].pop("log_redact")("${NOT_AN_ARGUMENT}") == "<redacted>"
    assert push_auth == [("mirror.example", {
        "bearer_token": "explicit-token", "username": None, "password": None,
    })]
    assert push_clients == [("mirror.example", "team/app", "push-token")]
    tag_writes = [
        call[1] for call in push.calls if call[0] != "blob" and not call[1].startswith("sha256:")
    ]
    assert tag_writes == [
        "v9-py3.11", "stable-py3.11", "v9-py3.12", "stable-py3.12",
        "v9", "stable", "v1.2", "v1", "latest",
    ]
    first_tag = next(i for i, op in enumerate(build.operations) if "@" not in op.reference)
    assert all("@" in op.reference for op in build.operations[:first_tag])


def test_matrix_build_args_redact_dry_run_and_publication(tmp_path, monkeypatch, capsys, caplog):
    build, _, _ = matrix(
        tmp_path, monkeypatch, tag="registry.example/team/app:${VERSION}",
        build_args={"VERSION": "sensitive-release"}, dry_run=True, semver_tags=False,
    )
    build.build()
    assert "sensitive-release" not in capsys.readouterr().out + caplog.text
    build.config.dry_run = False
    build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    auth_options = []

    def auth(host, **kwargs):
        auth_options.append(kwargs)
        return RegistryAuth()

    monkeypatch.setattr(matrix_module, "get_registry_auth", auth)
    build.push()
    assert "sensitive-release" not in capsys.readouterr().out + caplog.text
    assert auth_options[0]["log_redact"]("sensitive-release") == "<redacted>"

    def fail(host, **kwargs):
        raise RuntimeError("sensitive-release authentication failed")

    monkeypatch.setattr(matrix_module, "get_registry_auth", fail)
    with pytest.raises(MatrixPublicationError) as error:
        build.push()
    assert "sensitive-release" not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_locked_matrix_checks_base_minor_before_resolving_lock(tmp_path, monkeypatch):
    from test_lockfiles import _write_target_marker_uv_project

    build, _, _ = matrix(tmp_path, monkeypatch, include_deps=True)
    _write_target_marker_uv_project(Path(build.config.context_dir))
    original_pull = ImageBuilder._pull_base_image

    def pull(child, *args):
        layers, config = original_pull(child, *args)
        config["config"]["Env"] = ["PYTHON_VERSION=3.10.1"]
        return layers, config

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull)
    monkeypatch.setattr(
        builder_module, "load_lockfile",
        lambda *args: pytest.fail("must reject base before resolving lock"),
    )
    with pytest.raises(ValueError, match=r"Python 3\.11.*matching PYTHON_VERSION"):
        build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()
    assert push.calls == []


@pytest.mark.parametrize("failure", ["missing", "hash-mismatch"])
def test_locked_matrix_wheel_failure_prevents_all_publication(tmp_path, monkeypatch, failure):
    build, _, _ = matrix(
        tmp_path, monkeypatch, platform="linux/amd64,linux/arm64,linux/arm/v7",
        base_platforms=("amd64", "arm64", "arm/v7"), include_deps=True,
    )
    context = Path(build.config.context_dir)
    _write_matrix_lock_project(context)
    wheel = context / "wheelhouse" / "fixture_dep-1.0-cp312-cp312-manylinux2014_armv7l.whl"
    if failure == "missing":
        wheel.unlink()
    else:
        wheel.write_bytes(b"corrupt")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))

    with pytest.raises(RuntimeError, match="linux/arm/v7"):
        build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()
    assert push.calls == []


@pytest.mark.parametrize("source", ["digest", "unresolved-tag"])
def test_locked_matrix_rejects_unproven_base_before_pip(tmp_path, monkeypatch, source):
    from test_lockfiles import _write_target_marker_uv_project

    build, bases, _ = matrix(
        tmp_path, monkeypatch, include_deps=True, base_env=["PYTHON_VERSION=3.11.9"],
    )
    _write_target_marker_uv_project(Path(build.config.context_dir))
    if source == "digest":
        mapping = {
            version: f"python:{version}-slim@{base.index_digest}"
            for version, base in bases.items()
        }
        build = PythonMatrixBuilder(replace(build.config, python_matrix=mapping))
    else:
        original_client = builder_module.RegistryClient

        def unresolved_client(*args, **kwargs):
            client = original_client(*args, **kwargs)
            pull = client.pull_manifest

            def unresolved_manifest(reference, **expected):
                manifest, digest = pull(reference, **expected)
                return manifest, None if reference.endswith("-slim") else digest

            client.pull_manifest = unresolved_manifest
            return client

        monkeypatch.setattr(builder_module, "RegistryClient", unresolved_client)
    monkeypatch.setattr(
        builder_module, "install_requirements",
        lambda *args, **kwargs: pytest.fail("pip ran without proven base metadata"),
    )

    with pytest.raises(ValueError, match="unverified or custom base image"):
        build.build()
    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()
    assert push.calls == []


def local_matrix(tmp_path, monkeypatch):
    build, bases, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64")
    mapping = {}
    paths = {}
    for version, base in bases.items():
        root = tmp_path / f"base-{version}"
        blobs = root / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        (root / "index.json").write_bytes(base.manifests[base.index_digest])
        for digest, data in {**base.blobs, **base.manifests}.items():
            if digest.startswith("sha256:"):
                (blobs / digest.split(":")[1]).write_bytes(data)
        mapping[version] = f"oci:{root}"
        paths[version] = root
    return PythonMatrixBuilder(replace(build.config, base_image="custom:base", python_matrix=mapping)), paths


def test_local_custom_map_selects_each_platform_without_tag_provenance(tmp_path, monkeypatch):
    build, _ = local_matrix(tmp_path, monkeypatch)
    build.build()
    assert all(child._base_image_tag_verified is False for child in build.children.values())
    assert all(len(child.platform_images) == 2 for child in build.children.values())


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_matrix_layouts_are_reusable_as_digest_pinned_local_bases(tmp_path, monkeypatch, platform):
    source, _, _ = matrix(tmp_path, monkeypatch, platform=platform)
    source.build()
    default = source.children[source.default]
    descriptors = json.loads((Path(default.config.output_dir) / "index.json").read_text())["manifests"]
    assert sum(entry["digest"] == default.manifest_digest for entry in descriptors) > 1

    bases = {
        version: f"oci:{child.config.output_dir}@{child.manifest_digest}"
        for version, child in source.children.items()
    }
    context = tmp_path / "consumer"
    context.mkdir()
    (context / "app.py").write_text("print('consumer')\n")
    consumer = PythonMatrixBuilder(replace(
        source.config, context_dir=str(context), output_dir=str(tmp_path / "consumer-output"),
        base_image="custom:base", python_matrix=bases,
    ))
    consumer.build()
    for version, child in consumer.children.items():
        expected = {
            image.platform["architecture"]: [layer.digest for layer in image.layers]
            for image in source.children[version].platform_images
        }
        assert {image.platform["architecture"] for image in child.platform_images} == set(expected)
        assert child._base_image_tag_verified is False
        for image in child.platform_images:
            assert [layer.digest for layer in image.layers[:-image.app_layer_count]] == (
                expected[image.platform["architecture"]]
            )


@pytest.mark.parametrize("during_build", [True, False])
def test_local_base_change_prevents_matrix_publication(tmp_path, monkeypatch, during_build):
    build, paths = local_matrix(tmp_path, monkeypatch)

    def change():
        index = paths["3.11"] / "index.json"
        index.write_bytes(index.read_bytes() + b"\n")

    push = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    if during_build:
        child = build.children["3.12"]
        original = child._build_image

        def last_build():
            result = original()
            change()
            return result
        monkeypatch.setattr(child, "_build_image", last_build)
        with pytest.raises(LocalImageError, match="changed"):
            build.build()
    else:
        build.build()
        change()
        with pytest.raises(LocalImageError, match="changed"):
            build.push()
    assert push.calls == []


def test_explicit_digest_map_never_claims_tag_provenance(tmp_path, monkeypatch):
    build, bases, _ = matrix(tmp_path, monkeypatch, platform="linux/amd64,linux/arm64")
    mapping = {version: f"python:{version}-slim@{base.index_digest}"
               for version, base in bases.items()}
    build = PythonMatrixBuilder(replace(build.config, python_matrix=mapping))
    build.build()
    assert all(child._base_image_tag_verified is False for child in build.children.values())


def test_dependency_failure_on_later_minor_starts_no_push(tmp_path, monkeypatch):
    build, _, _ = matrix(tmp_path, monkeypatch, include_deps=True)
    (Path(build.config.context_dir) / "requirements.txt").write_text("synthetic==1\n")

    def install(requirements, site, ctx, os_name, arch, version, base, config, **_):
        if version == "3.12":
            raise RuntimeError("No compatible wheels for Python 3.12")
        site.mkdir()
        (site / "synthetic.py").write_text("VALUE = 1\n")
        return [requirements]
    monkeypatch.setattr(builder_module, "install_requirements", install)
    push = FakePushClient()
    with pytest.raises(RuntimeError, match="3.12"):
        build.build()
    monkeypatch.setattr(builder_module, "RegistryClient", push)
    with pytest.raises(RuntimeError, match="every Python"):
        build.push()
    assert push.calls == []


def test_custom_map_toml_and_dry_run(tmp_path, monkeypatch, capsys):
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "app.py").write_text("print('hello')\n")
    config = tmp_path / "pycontainer.toml"
    config.write_text(f"""
[build]
python_versions = ["3.11", "3.12"]
default_python_version = "3.12"
context_dir = "{ctx.as_posix()}"
output_dir = "{(tmp_path / 'out').as_posix()}"
dry_run = true
[build.python_matrix]
"3.11" = "custom:eleven"
"3.12" = "custom:twelve"
""")
    cfg = config_from_file(config)
    build = ImageBuilder(cfg)
    build.build()
    text = capsys.readouterr().out
    assert "custom:eleven" in text and "custom:twelve" in text
    assert "py3.12" in text
    assert not (tmp_path / "out").exists()


def test_local_base_cannot_overlap_any_matrix_output(tmp_path):
    output = tmp_path / "matrix"
    with pytest.raises(ValueError, match="overlaps"):
        BuildConfig(python_versions=["3.11"], output_dir=str(output),
                    python_matrix={"3.11": f"oci:{output / 'sibling-base'}"})
