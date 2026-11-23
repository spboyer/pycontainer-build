import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pycontainer import cli, tags as tags_module
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file


def _git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo, *args):
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env=_git_env(),
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "source with spaces"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "released source with spaces"
    # This bundle copies existing annotated and lightweight release tags; tests
    # never create or move Git tags, even in their temporary repositories.
    bundle = Path(__file__).with_name("git-tags.bundle")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", str(bundle), str(repo)],
        check=True, capture_output=True, timeout=30, env=_git_env(),
    )
    _git(repo, "switch", "--detach", "v0.1.0")
    return repo


def _config(repo, **kwargs):
    return BuildConfig(
        tag="registry.example:5000/team/app:placeholder",
        context_dir=str(repo),
        tag_from_git=True,
        **kwargs,
    )


@pytest.mark.parametrize("name", ["v0.1.0", "v0.2.0"])
def test_exact_git_tag_composes_with_extra_semver_and_latest(release_repo, name):
    _git(release_repo, "switch", "--detach", name)

    config = _config(
        release_repo, tags=["stable"], semver_tags=True, latest_tag=True
    )

    minor = name.rsplit(".", 1)[0]
    major = minor.split(".")[0]
    assert config.tag == f"registry.example:5000/team/app:{name}"
    assert config.all_tags() == [
        f"registry.example:5000/team/app:{name}",
        "registry.example:5000/team/app:stable",
        f"registry.example:5000/team/app:{minor}",
        f"registry.example:5000/team/app:{major}",
        "registry.example:5000/team/app:latest",
    ]


def test_git_tag_replaces_only_the_primary_tag_and_preserves_a_registry_port(release_repo):
    config = BuildConfig(
        tag="localhost:5000/team/app",
        context_dir=str(release_repo),
        tag_from_git=True,
    )
    assert config.tag == "localhost:5000/team/app:v0.1.0"


def test_untagged_commit_requires_fallback_and_ignores_branch_names(git_repo):
    _git(git_repo, "switch", "-c", "v1.2.3")
    commit = _git(git_repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="(?i)(exact|tagged)"):
        _config(git_repo)
    assert _config(git_repo, git_fallback="sha").tag.endswith(f":sha-{commit[:12]}")


def test_fallback_does_not_use_a_tag_on_an_ancestor(release_repo):
    (release_repo / "README.md").write_text("new\n", encoding="utf-8")
    _git(release_repo, "add", "README.md")
    _git(release_repo, "commit", "-m", "new commit")
    commit = _git(release_repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="(?i)(exact|tagged)"):
        _config(release_repo)
    assert _config(release_repo, git_fallback="sha").tag.endswith(f":sha-{commit[:12]}")


@pytest.mark.parametrize("change", ["modified", "staged", "untracked", "deleted"])
def test_dirty_worktrees_require_explicit_opt_in(release_repo, change):
    path = release_repo / "README.md"
    if change == "untracked":
        (release_repo / "new.py").write_text("pass\n", encoding="utf-8")
    elif change == "deleted":
        path.unlink()
    else:
        path.write_text("print('modified')\n", encoding="utf-8")
        if change == "staged":
            _git(release_repo, "add", "README.md")

    with pytest.raises(ValueError, match="(?i)dirty"):
        _config(release_repo)
    assert _config(release_repo, allow_dirty=True).tag.endswith(":v0.1.0")


@pytest.mark.parametrize("fallback", [None, "sha"])
@pytest.mark.parametrize("name", ["release/v1.2.3", "v1.2.3+build", "v" * 129])
def test_git_tag_must_be_a_valid_oci_tag(release_repo, monkeypatch, name, fallback):
    real_run = subprocess.run

    def invalid_tag(command, **kwargs):
        if "describe" in command or ("tag" in command and "--points-at" in command):
            return subprocess.CompletedProcess(command, 0, f"{name}\n", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", invalid_tag)
    with pytest.raises(ValueError, match="(?i)(invalid|OCI)"):
        _config(release_repo, git_fallback=fallback)


def test_git_context_can_be_a_subdirectory_and_checks_the_whole_worktree(release_repo):
    subdirectory = release_repo / "service"
    subdirectory.mkdir()
    assert _config(subdirectory).tag.endswith(":v0.1.0")

    (release_repo / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)dirty"):
        _config(subdirectory)


def test_missing_repository_and_unborn_head_are_errors(tmp_path):
    non_repo = tmp_path / "not-a-repository"
    non_repo.mkdir()
    with pytest.raises(ValueError, match="(?i)(repository|worktree)"):
        _config(non_repo, git_fallback="sha")
    with pytest.raises(ValueError, match="(?i)(repository|worktree)"):
        _config(tmp_path / "missing", git_fallback="sha")

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "--initial-branch=main")
    with pytest.raises(ValueError, match="(?i)(head|commit)"):
        _config(unborn, git_fallback="sha")


def test_git_executable_missing_is_an_explicit_error(git_repo, monkeypatch):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(tags_module.subprocess, "run", missing_git)
    with pytest.raises(ValueError, match="(?i)git"):
        _config(git_repo, git_fallback="sha")


def test_git_timeout_is_an_explicit_error(git_repo, monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(tags_module.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="(?i)timed out"):
        _config(git_repo, git_fallback="sha")


@pytest.mark.parametrize("returncode", [0, 128])
@pytest.mark.parametrize("allow_dirty", [False, True])
def test_failed_dirty_inspection_is_never_treated_as_clean(
    release_repo, monkeypatch, allow_dirty, returncode
):
    real_run = subprocess.run

    def fail_status(command, **kwargs):
        if "status" in command:
            return subprocess.CompletedProcess(command, returncode, "", "status failed")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", fail_status)
    with pytest.raises(ValueError, match="(?i)(status|inspect)"):
        _config(release_repo, allow_dirty=allow_dirty)


def test_describe_failure_is_not_mistaken_for_an_untagged_commit(git_repo, monkeypatch):
    real_run = subprocess.run

    def fail_describe(command, **kwargs):
        if "describe" in command:
            return subprocess.CompletedProcess(command, 128, "", "fatal: injected failure")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", fail_describe)
    with pytest.raises(ValueError, match="(?i)(describe|injected failure)"):
        _config(git_repo, git_fallback="sha")


def test_failed_tag_inspection_cannot_trigger_the_sha_fallback(git_repo, monkeypatch):
    real_run = subprocess.run

    def fail_tag_listing(command, **kwargs):
        if "tag" in command and "--points-at" in command:
            return subprocess.CompletedProcess(command, 128, "", "cannot inspect tags")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", fail_tag_listing)
    with pytest.raises(ValueError, match="(?i)(tag|inspect)"):
        _config(git_repo, git_fallback="sha")


def test_tag_inspection_warning_cannot_trigger_the_sha_fallback(git_repo, monkeypatch):
    real_run = subprocess.run

    def warn_on_tag_listing(command, **kwargs):
        if "tag" in command and "--points-at" in command:
            return subprocess.CompletedProcess(command, 0, "", "warning: broken ref")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", warn_on_tag_listing)
    with pytest.raises(ValueError, match="(?i)(tag|inspect)"):
        _config(git_repo, git_fallback="sha")


def test_describe_cannot_fallback_when_an_exact_tag_exists(release_repo, monkeypatch):
    real_run = subprocess.run

    def inconsistent_describe(command, **kwargs):
        if "describe" in command:
            return subprocess.CompletedProcess(
                command, 128, "", "fatal: no tag exactly matches HEAD"
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", inconsistent_describe)
    with pytest.raises(ValueError, match="(?i)describe"):
        _config(release_repo, git_fallback="sha")


def test_describe_warning_is_not_ignored(release_repo, monkeypatch):
    real_run = subprocess.run

    def warn_on_describe(command, **kwargs):
        if "describe" in command:
            return subprocess.CompletedProcess(command, 0, "v0.1.0\n", "warning: broken ref")
        return real_run(command, **kwargs)

    monkeypatch.setattr(tags_module.subprocess, "run", warn_on_describe)
    with pytest.raises(ValueError, match="(?i)describe"):
        _config(release_repo)


def test_inherited_git_repository_overrides_do_not_change_context(release_repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "--initial-branch=main")
    (other / "other.py").write_text("pass\n", encoding="utf-8")
    _git(other, "add", "other.py")
    _git(other, "commit", "-m", "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    assert _config(release_repo).tag.endswith(":v0.1.0")


def test_inherited_git_discovery_and_config_overrides_do_not_hide_context(
    release_repo, monkeypatch
):
    subdirectory = release_repo / "service"
    subdirectory.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(release_repo))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    assert _config(subdirectory).tag.endswith(":v0.1.0")


def test_semver_expansion_rejects_sha_fallback_without_a_version_tag(git_repo):
    with pytest.raises(ValueError, match="Semantic version"):
        _config(git_repo, git_fallback="sha", semver_tags=True)

    config = _config(
        git_repo, git_fallback="sha", semver_tags=True, tags=["v2.3.4"]
    )
    assert [ref.rsplit(":", 1)[1] for ref in config.all_tags()][1:] == [
        "v2.3.4", "v2.3", "v2"
    ]


def test_git_options_require_opt_in_and_validate_fallback(git_repo):
    with pytest.raises(ValueError, match="(?i)tag.from.git"):
        BuildConfig(context_dir=str(git_repo), git_fallback="sha")
    with pytest.raises(ValueError, match="(?i)tag.from.git"):
        BuildConfig(context_dir=str(git_repo), allow_dirty=True)
    with pytest.raises(ValueError, match="(?i)fallback"):
        _config(git_repo, git_fallback="unknown")


def test_config_file_git_opt_in_and_cli_dirty_opt_in(release_repo, tmp_path, monkeypatch):
    (release_repo / "README.md").write_text("changed\n", encoding="utf-8")
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        f"[build]\ntag = 'ghcr.io/me/app:placeholder'\n"
        f"context_dir = {json.dumps(str(release_repo))}\ntag_from_git = true\n",
        encoding="utf-8",
    )
    configs = []

    class FakeBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return configs[-1].tag

    monkeypatch.setattr(cli, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--config", str(config_path),
         "--allow-dirty", "--semver-tags", "--latest"],
    )
    cli.main()
    assert configs[0].all_tags() == [
        "ghcr.io/me/app:v0.1.0",
        "ghcr.io/me/app:v0.1",
        "ghcr.io/me/app:v0",
        "ghcr.io/me/app:latest",
    ]


def test_config_file_supports_the_sha_fallback(git_repo, tmp_path):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        f"[build]\ntag = 'app'\ncontext_dir = {json.dumps(str(git_repo))}\n"
        "tag_from_git = true\ngit_fallback = 'sha'\n",
        encoding="utf-8",
    )
    commit = _git(git_repo, "rev-parse", "HEAD")
    assert config_from_file(config_path).tag == f"app:sha-{commit[:12]}"


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
@pytest.mark.parametrize("key", ["allow_dirty", "allow-dirty"])
def test_config_file_cannot_silently_allow_dirty_worktrees(release_repo, tmp_path, table, key):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        f"[{table}]\ncontext_dir = {json.dumps(str(release_repo))}\n"
        f"tag_from_git = true\n{key} = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="--allow-dirty") as error:
        config_from_file(config_path)
    assert f"[{table}].allow_dirty" in str(error.value)


def test_cli_git_tag_options_render_the_effective_build_plan(release_repo, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--context", str(release_repo),
         "--tag", "ghcr.io/me/app", "--tag", "stable", "--tag-from-git",
         "--semver-tags", "--latest", "--dry-run"],
    )
    cli.main()
    output = capsys.readouterr().out
    for version in ("v0.1.0", "stable", "v0.1", "v0", "latest"):
        assert f"ghcr.io/me/app:{version}" in output


def test_cli_sha_fallback_and_latest(git_repo, monkeypatch, capsys):
    commit = _git(git_repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--context", str(git_repo), "--tag", "app",
         "--tag-from-git", "--git-fallback", "sha", "--latest", "--dry-run"],
    )
    cli.main()
    output = capsys.readouterr().out
    assert f"app:sha-{commit[:12]}" in output
    assert "docker.io/library/app:latest" in output


def test_cli_reports_git_changes_during_build_as_an_argument_error(
    release_repo, monkeypatch, capsys
):
    class MutatingBuilder(ImageBuilder):
        def build(self):
            (release_repo / "README.md").write_text("changed\n", encoding="utf-8")
            return super().build()

    monkeypatch.setattr(cli, "ImageBuilder", MutatingBuilder)
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--context", str(release_repo),
         "--tag-from-git", "--dry-run"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "dirty" in capsys.readouterr().err.lower()


def test_git_state_is_rechecked_before_build_and_does_not_replace_old_refs(
    release_repo, tmp_path, monkeypatch
):
    original_tags = _git(release_repo, "show-ref", "--tags")
    config = _config(release_repo, output_dir=str(tmp_path / "out"), use_cache=False)
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    ref = tmp_path / "out" / "refs" / "tags" / "v0.1.0"
    old_digest = ref.read_text()

    (release_repo / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)dirty"):
        builder.build()
    assert ref.read_text() == old_digest
    assert config.tag.endswith(":v0.1.0")
    assert _git(release_repo, "show-ref", "--tags") == original_tags


def test_git_head_change_after_configuration_fails_before_build(release_repo, tmp_path):
    config = _config(
        release_repo, git_fallback="sha", output_dir=str(tmp_path / "out"), dry_run=True
    )
    (release_repo / "README.md").write_text("new\n", encoding="utf-8")
    _git(release_repo, "add", "README.md")
    _git(release_repo, "commit", "-m", "new commit")

    with pytest.raises(ValueError, match="(?i)(changed|drift)"):
        ImageBuilder(config).build()
    assert config.tag.endswith(":v0.1.0")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_head_change_during_build_does_not_replace_published_refs(
    release_repo, tmp_path, monkeypatch, platform
):
    output = tmp_path / "out"
    refs = output / "refs" / "tags"
    refs.mkdir(parents=True)
    (output / "index.json").write_text("previous index", encoding="utf-8")
    (refs / "v0.1.0").write_text("previous digest", encoding="utf-8")
    config = _config(
        release_repo, output_dir=str(output), use_cache=False, platform=platform
    )
    builder = ImageBuilder(config)
    switched = False

    def switch_head(layers_dir, os_name, arch):
        nonlocal switched
        if not switched:
            _git(release_repo, "switch", "--detach", "v0.2.0")
            switched = True
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", switch_head)
    with pytest.raises(ValueError, match="(?i)head changed"):
        builder.build()
    assert (output / "index.json").read_text() == "previous index"
    assert (refs / "v0.1.0").read_text() == "previous digest"


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_worktree_changed_during_build_does_not_replace_published_refs(
    release_repo, tmp_path, monkeypatch, platform, change
):
    output = tmp_path / "out"
    refs = output / "refs" / "tags"
    refs.mkdir(parents=True)
    (output / "index.json").write_text("previous index", encoding="utf-8")
    (refs / "v0.1.0").write_text("previous digest", encoding="utf-8")
    config = _config(
        release_repo, output_dir=str(output), use_cache=False, platform=platform
    )
    builder = ImageBuilder(config)

    def modify_source(layers_dir, os_name, arch):
        path = release_repo / ("README.md" if change == "tracked" else "new.py")
        path.write_text("changed\n", encoding="utf-8")
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", modify_source)
    with pytest.raises(ValueError, match="(?i)dirty"):
        builder.build()
    assert (output / "index.json").read_text() == "previous index"
    assert (refs / "v0.1.0").read_text() == "previous digest"


def test_generated_output_and_cache_do_not_make_a_clean_build_fail(
    release_repo, monkeypatch
):
    config = _config(
        release_repo,
        output_dir=str(release_repo / "generated" / "image"),
        cache_dir=str(release_repo / "generated-cache"),
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    assert builder.build() == config.tag


def test_pulled_base_config_blob_does_not_make_build_dirty(release_repo, monkeypatch):
    base_config = json.dumps({
        "architecture": "amd64", "os": "linux", "config": {},
        "rootfs": {"type": "layers", "diff_ids": []},
    }).encode()
    digest = "sha256:" + hashlib.sha256(base_config).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": digest,
            "size": len(base_config),
        },
        "layers": [],
    }

    class FakeSource:
        def pull_manifest(self, reference, **kwargs):
            return manifest, None

        def pull_blob(self, blob_digest, path, size):
            assert blob_digest == digest and size == len(base_config)
            path.write_bytes(base_config)

    config = _config(
        release_repo, output_dir=str(release_repo / "generated" / "image"), use_cache=False
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image",
        lambda layers_dir, os_name, arch: builder._read_base_image(
            FakeSource(), "latest", layers_dir, os_name, arch
        ),
    )
    assert builder.build() == config.tag


def test_unrelated_untracked_file_in_output_blobs_is_not_ignored(release_repo, monkeypatch):
    output = release_repo / "generated" / "image"
    config = _config(release_repo, output_dir=str(output), use_cache=False)
    builder = ImageBuilder(config)

    def create_untracked(layers_dir, os_name, arch):
        (layers_dir / "unrelated.txt").write_text("new\n", encoding="utf-8")
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", create_untracked)
    with pytest.raises(ValueError, match="(?i)dirty"):
        builder.build()
    assert not (output / "index.json").exists()


def test_generated_output_through_symlink_does_not_make_build_dirty(
    release_repo, tmp_path, monkeypatch
):
    alias = tmp_path / "repo-alias"
    try:
        alias.symlink_to(release_repo, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create directory symlink: {error}")
    config = _config(
        release_repo, output_dir=str(alias / "generated" / "image"),
        cache_dir=str(alias / "generated-cache"),
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    assert builder.build() == config.tag


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_generated_output_does_not_hide_source_changes(release_repo, monkeypatch, change):
    config = _config(
        release_repo,
        output_dir=str(release_repo / "generated" / "image"),
        cache_dir=str(release_repo / "generated-cache"),
    )
    builder = ImageBuilder(config)

    def modify_source(layers_dir, os_name, arch):
        path = release_repo / ("README.md" if change == "tracked" else "new.py")
        path.write_text("changed\n", encoding="utf-8")
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", modify_source)
    with pytest.raises(ValueError, match="(?i)dirty"):
        builder.build()
    assert not (release_repo / "generated" / "image" / "index.json").exists()


def test_allow_dirty_permits_source_changes_during_build(release_repo, tmp_path, monkeypatch):
    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False, allow_dirty=True
    )
    builder = ImageBuilder(config)

    def modify_source(layers_dir, os_name, arch):
        (release_repo / "README.md").write_text("changed\n", encoding="utf-8")
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", modify_source)
    assert builder.build() == config.tag


def test_tracked_changes_inside_output_blobs_are_not_ignored(git_repo, monkeypatch):
    tracked = git_repo / "blobs" / "sha256" / "tracked.txt"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("before\n", encoding="utf-8")
    _git(git_repo, "add", "blobs")
    _git(git_repo, "commit", "-m", "track output file")
    config = _config(
        git_repo, git_fallback="sha", output_dir=str(git_repo), use_cache=False
    )
    builder = ImageBuilder(config)

    def modify_tracked(layers_dir, os_name, arch):
        tracked.write_text("changed\n", encoding="utf-8")
        return [], None

    monkeypatch.setattr(builder, "_pull_base_image", modify_tracked)
    with pytest.raises(ValueError, match="(?i)dirty"):
        builder.build()
    assert not (git_repo / "index.json").exists()


@pytest.mark.parametrize("allow_dirty", [False, True])
def test_worktree_inspection_failure_before_publication_is_an_error(
    release_repo, tmp_path, monkeypatch, allow_dirty
):
    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False,
        allow_dirty=allow_dirty,
    )
    builder = ImageBuilder(config)
    during_build = False
    real_git = tags_module._git

    def status_failure(context, *args, **kwargs):
        if during_build and args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "status warning")
        return real_git(context, *args, **kwargs)

    def pull(layers_dir, os_name, arch):
        nonlocal during_build
        during_build = True
        return [], None

    monkeypatch.setattr(tags_module, "_git", status_failure)
    monkeypatch.setattr(builder, "_pull_base_image", pull)
    with pytest.raises(ValueError, match="(?i)status"):
        builder.build()
    assert not (tmp_path / "out" / "index.json").exists()


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_changed_git_tag_during_build_does_not_replace_published_refs(
    release_repo, tmp_path, monkeypatch, platform
):
    output = tmp_path / "out"
    refs = output / "refs" / "tags"
    refs.mkdir(parents=True)
    (output / "index.json").write_text("previous index", encoding="utf-8")
    (refs / "v0.1.0").write_text("previous digest", encoding="utf-8")
    config = _config(
        release_repo, output_dir=str(output), use_cache=False, platform=platform
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    with pytest.raises(ValueError, match="no longer identifies"):
        builder.build()
    assert (output / "index.json").read_text() == "previous index"
    assert (refs / "v0.1.0").read_text() == "previous digest"


def test_changed_git_tag_reference_prevents_push_before_registry_access(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module

    config = _config(release_repo, output_dir=str(tmp_path / "out"), use_cache=False)
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    def unexpected_registry(*args, **kwargs):
        pytest.fail("Registry was contacted after the Git tag changed")

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    monkeypatch.setattr(builder_module, "get_registry_auth", unexpected_registry)
    monkeypatch.setattr(builder_module, "RegistryClient", unexpected_registry)
    with pytest.raises(ValueError, match="no longer identifies"):
        builder.push(show_progress=False)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_tag_changed_during_blob_upload_prevents_tag_publication(
    release_repo, tmp_path, monkeypatch, platform
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(release_repo, output_dir=str(tmp_path / "out"),
                     use_cache=False, platform=platform)
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    uploaded = False
    published = []
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if uploaded and any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            nonlocal uploaded
            uploaded = True
            return True

        def push_manifest(self, reference, data):
            published.append(reference)

        def push_index(self, reference, data):
            published.append(reference)

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    with pytest.raises(ValueError, match="no longer identifies"):
        builder.push(show_progress=False)
    assert "v0.1.0" not in published
    if platform != "linux/amd64":
        assert builder.pushed_refs == []


def test_tag_changed_between_index_writes_reports_confirmed_tags(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False,
        platform="linux/amd64,linux/arm64", latest_tag=True,
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    published = []
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if "v0.1.0" in published and any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, reference, data):
            pass

        def push_index(self, reference, data):
            published.append(reference)

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    with pytest.raises(ValueError, match="no longer identifies") as error:
        builder.push(show_progress=False)
    assert builder.pushed_refs == config.all_tags()[:1]
    assert config.all_tags()[0] in str(error.value)
    assert config.all_tags()[1] in str(error.value)
    assert "latest" not in published


def test_tag_changed_between_manifest_writes_reports_confirmed_tags(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False, latest_tag=True
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    published = []
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if published and any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, reference, data):
            published.append(reference)

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    with pytest.raises(ValueError, match="no longer identifies") as error:
        builder.push(show_progress=False)
    assert published == ["v0.1.0"]
    assert builder.pushed_refs == config.all_tags()[:1]
    assert config.all_tags()[0] in str(error.value)
    assert config.all_tags()[1] in str(error.value)


def test_failed_git_preflight_clears_previous_multi_platform_push_results(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False,
        platform="linux/amd64,linux/arm64", latest_tag=True,
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, reference, data):
            pass

        def push_index(self, reference, data):
            pass

    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    builder.push(show_progress=False)
    assert builder.pushed_refs == config.all_tags()
    real_git = tags_module._git

    def changed_tag(context, *args, **kwargs):
        if any(arg.startswith("refs/tags/") for arg in args):
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return real_git(context, *args, **kwargs)

    monkeypatch.setattr(tags_module, "_git", changed_tag)
    with pytest.raises(ValueError, match="no longer identifies"):
        builder.push(show_progress=False)
    assert builder.pushed_refs == []


def test_push_uses_built_tag_when_head_changes_but_the_tag_is_unchanged(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(release_repo, output_dir=str(tmp_path / "out"), use_cache=False)
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    _git(release_repo, "switch", "--detach", "v0.2.0")
    pushed = []

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, tag, data):
            pushed.append(tag)

    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    builder.push(show_progress=False)
    assert pushed == ["v0.1.0"]


@pytest.mark.parametrize(
    "destination",
    ["mirror.example:5000/team/app", "mirror.example:5000/team/app:v0.1.0"],
)
def test_registry_override_preserves_git_derived_primary_tag(
    release_repo, tmp_path, monkeypatch, destination
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth

    config = _config(
        release_repo, output_dir=str(tmp_path / "out"), use_cache=False, tags=["stable"]
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()
    pushed = []

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, tag, data):
            pushed.append(tag)

    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    result = builder.push(registry_url=destination, show_progress=False)
    assert result == "mirror.example:5000/team/app:v0.1.0"
    assert pushed == ["v0.1.0", "stable"]
    assert builder.pushed_refs == [
        "mirror.example:5000/team/app:v0.1.0",
        "mirror.example:5000/team/app:stable",
    ]


@pytest.mark.parametrize(
    "destination",
    ["mirror.example/team/app:latest", "mirror.example/team/app@sha256:" + "a" * 64],
)
def test_registry_override_cannot_replace_the_git_derived_version(
    release_repo, tmp_path, monkeypatch, destination
):
    from pycontainer import builder as builder_module

    config = _config(release_repo, output_dir=str(tmp_path / "out"), use_cache=False)
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()

    def unexpected_registry(*args, **kwargs):
        pytest.fail("Registry was contacted with a conflicting Git-derived tag")

    monkeypatch.setattr(builder_module, "get_registry_auth", unexpected_registry)
    with pytest.raises(ValueError, match="--registry"):
        builder.push(registry_url=destination, show_progress=False)


def test_cli_reports_git_tag_errors_during_push(release_repo, monkeypatch, capsys):
    class FakeBuilder:
        def __init__(self, config):
            pass

        def build(self):
            return "layout"

        def push(self, **kwargs):
            raise tags_module.GitTagError("Git tag no longer identifies the build commit")

    monkeypatch.setattr(cli, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(
        sys, "argv",
        ["pycontainer", "build", "--context", str(release_repo),
         "--tag-from-git", "--push"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "no longer identifies" in capsys.readouterr().err


def test_non_git_builds_do_not_inspect_git(tmp_path, monkeypatch):
    def unexpected_git(*args, **kwargs):
        pytest.fail("Git was inspected without --tag-from-git")

    monkeypatch.setattr(tags_module.subprocess, "run", unexpected_git)
    config = BuildConfig(tag="app:dev", context_dir=str(tmp_path), dry_run=True)
    assert config.all_tags() == ["docker.io/library/app:dev"]
    assert ImageBuilder(config).build() == "app:dev"


def test_git_tags_preserve_multi_platform_layout_and_partial_push_accounting(
    release_repo, tmp_path, monkeypatch
):
    from pycontainer import builder as builder_module
    from pycontainer.auth import RegistryAuth
    from pycontainer.registry_client import RegistryRequestError

    config = _config(
        release_repo,
        output_dir=str(tmp_path / "out"),
        use_cache=False,
        platform="linux/amd64,linux/arm64",
        tags=["stable"],
        semver_tags=True,
        latest_tag=True,
    )
    builder = ImageBuilder(config)
    monkeypatch.setattr(
        builder, "_pull_base_image", lambda layers_dir, os_name, arch: ([], None)
    )
    builder.build()

    references = config.all_tags()
    layout = json.loads((tmp_path / "out" / "index.json").read_text())
    assert [
        item["annotations"]["org.opencontainers.image.ref.name"]
        for item in layout["manifests"]
    ] == references
    for reference in references:
        name = reference.rsplit(":", 1)[1]
        assert (tmp_path / "out" / "refs" / "tags" / name).read_text() == builder.index_digest

    class FakeClient:
        def push_blob(self, digest, path, check_exists=True):
            return True

        def push_manifest(self, reference, data):
            pass

        def push_index(self, reference, data):
            if reference == "v0.1":
                raise RegistryRequestError("simulated failure")

    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *a, **k: FakeClient())
    with pytest.raises(RegistryRequestError) as error:
        builder.push(show_progress=False)
    assert builder.pushed_refs == references[:2]
    message = str(error.value)
    assert references[0] in message and references[1] in message
    assert references[2] in message and references[3] in message and references[4] in message
    assert "outcome unknown" in message and "Not attempted" in message
