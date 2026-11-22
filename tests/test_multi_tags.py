import sys

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.tags import resolve_tags


def test_single_tag_unchanged():
    assert resolve_tags("ghcr.io/u/app:v1") == ["ghcr.io/u/app:v1"]


def test_semver_expansion_and_latest():
    refs = resolve_tags("ghcr.io/u/app:v1.2.3", semver=True, latest=True)
    assert refs == [
        "ghcr.io/u/app:v1.2.3",
        "ghcr.io/u/app:v1.2",
        "ghcr.io/u/app:v1",
        "ghcr.io/u/app:latest",
    ]


def test_semver_without_v_prefix_and_dedupe():
    refs = resolve_tags("app:1.2.3", ["app:1.2", "1"], semver=True)
    assert [r.rsplit(":", 1)[1] for r in refs] == ["1.2.3", "1.2", "1"]


def test_latest_is_opt_in():
    assert "latest" not in " ".join(resolve_tags("app:v1.2.3", semver=True)).split(":")


def test_semver_requires_version_tag():
    with pytest.raises(ValueError, match="Semantic version"):
        resolve_tags("app:dev", semver=True)


def test_prerelease_not_expanded_but_other_version_is():
    refs = resolve_tags("app:v1.2.3-rc1", ["v1.2.3"], semver=True)
    assert [r.rsplit(":", 1)[1] for r in refs] == ["v1.2.3-rc1", "v1.2.3", "v1.2", "v1"]


def test_invalid_tag_syntax():
    with pytest.raises(ValueError, match="invalid tag"):
        resolve_tags("app:v1", ["app:-bad"])
    with pytest.raises(ValueError, match="Invalid tag"):
        resolve_tags("app:v1", ["-bad"])


def test_cross_repository_mismatch():
    with pytest.raises(ValueError, match="share a repository"):
        resolve_tags("ghcr.io/u/app:v1", ["ghcr.io/u/other:v2"])
    with pytest.raises(ValueError, match="share a repository"):
        resolve_tags("ghcr.io/u/app:v1", ["docker.io/u/app:v2"])


def test_cli_repeatable_tag(monkeypatch):
    configs = []

    class Fake:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", Fake)
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--tag", "app:v1.2.3", "--tag", "app:stable",
         "--semver-tags", "--latest"],
    )
    cli.main()
    cfg = configs[0]
    assert cfg.tag == "app:v1.2.3"
    assert cfg.tags == ["app:stable"]
    assert cfg.semver_tags and cfg.latest_tag


def test_config_file_tags(tmp_path):
    path = tmp_path / "pycontainer.toml"
    path.write_text('[build]\ntag = "app:v2.0.0"\ntags = ["v2"]\nsemver-tags = true\n')
    cfg = config_from_file(path)
    assert cfg.tags == ["v2"] and cfg.semver_tags


def test_config_tags_type_validated(tmp_path):
    path = tmp_path / "pycontainer.toml"
    path.write_text('[build]\ntags = "v2"\n')
    with pytest.raises(ValueError, match="tags"):
        config_from_file(path)


def _fake_build(monkeypatch, tmp_path, **kwargs):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "app.py").write_text("print('hi')\n")
    cfg = BuildConfig(
        context_dir=str(ctx), output_dir=str(tmp_path / "out"),
        use_cache=False, **kwargs,
    )
    builder = ImageBuilder(cfg)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    return builder


def test_build_writes_ref_for_every_tag(monkeypatch, tmp_path):
    b = _fake_build(
        monkeypatch, tmp_path, tag="reg.io/u/app:v1.2.3", semver_tags=True, latest_tag=True
    )
    refs = tmp_path / "out" / "refs" / "tags"
    names = sorted(p.name for p in refs.iterdir())
    assert names == ["latest", "v1", "v1.2", "v1.2.3"]
    assert {p.read_text() for p in refs.iterdir()} == {b.manifest_digest}


def test_push_uploads_blobs_once_and_manifest_per_tag(monkeypatch, tmp_path):
    b = _fake_build(monkeypatch, tmp_path, tag="reg.io/u/app:v1.2.3", tags=["stable"])
    calls = {"clients": 0, "blobs": [], "manifests": []}

    class FakeClient:
        def __init__(self, registry, repo, **kw):
            calls["clients"] += 1

        def push_blob(self, digest, path, check_exists=True):
            calls["blobs"].append(digest)
            return True

        def push_manifest(self, tag, data):
            calls["manifests"].append(tag)
            return True

    from pycontainer import builder as builder_mod
    monkeypatch.setattr(builder_mod, "RegistryClient", FakeClient)
    result = b.push(show_progress=False)
    assert result == "reg.io/u/app:v1.2.3"
    assert calls["manifests"] == ["v1.2.3", "stable"]
    assert len(calls["blobs"]) == len(set(calls["blobs"])) == len(b.layers) + 1
    assert b.pushed_refs == ["reg.io/u/app:v1.2.3", "reg.io/u/app:stable"]
