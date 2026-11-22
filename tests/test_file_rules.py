"""Conditional file rules: grammar, selection precedence, boundaries and identity."""
import os
import sys
import tarfile
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.file_rules import evaluate_condition, evaluate_file_rules


def _context(tmp_path):
    context = tmp_path / "context"
    for name, text in {
        "app.py": "print('app')",
        "debug_tools/inspect.py": "debug",
        "debug_tools/nested/trace.py": "trace",
        "native/amd64.so": "amd64",
        "native/arm64.so": "arm64",
        "tests/test_app.py": "test",
        "docs/guide.md": "guide",
    }.items():
        path = context / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return context


def _config(context, tmp_path, rules, **overrides):
    values = dict(
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
        include_paths=["app.py"],
        build_args={"BUILD_MODE": "production"},
        file_rules=rules,
    )
    values.update(overrides)
    return BuildConfig(**values)


def _selected(config):
    return [relative.as_posix() for _, relative in ImageBuilder(config).context_files]


DEV_TOOLS = {"when": 'BUILD_MODE == "dev"', "include": "debug_tools/**"}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('BUILD_MODE == "dev"', True),
        ('BUILD_MODE != "dev"', False),
        ('  BUILD_MODE=="dev"  and target.arch == "arm64" ', True),
        ('BUILD_MODE == "dev" and target.os != "linux"', False),
        ('and == "x"', True),
        ('QUOTE == "a\\"b\\\\"', True),
    ],
)
def test_condition_grammar(expression, expected):
    values = {
        "BUILD_MODE": "dev", "and": "x", "QUOTE": 'a"b\\',
        "target.os": "linux", "target.arch": "arm64",
    }
    assert evaluate_condition(expression, values, "rule") is expected


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("", "empty"),
        ("BUILD_MODE", "malformed"),
        ('BUILD_MODE = "dev"', "invalid token"),
        ("BUILD_MODE == dev", "malformed"),
        ('BUILD_MODE == "dev" or BUILD_MODE == "x"', "join comparisons"),
        ('BUILD_MODE == "dev" and', "join comparisons"),
        ('BUILD_MODE == "dev" BUILD_MODE == "x"', "join comparisons"),
        ('BUILD_MODE == "unterminated', "invalid token"),
        ('BUILD_MODE == "\\n"', "invalid token"),
        ('__import__("os") == "x"', "invalid token"),
        ('HOME == "x"', "unknown name HOME"),
        ('target.variant == "v8"', "unknown name target.variant"),
        ('BUILD_MODE == "dev"\n', "control characters"),
    ],
)
def test_malformed_or_unknown_conditions_fail(expression, message):
    with pytest.raises(ValueError, match=message):
        evaluate_condition(expression, {"BUILD_MODE": "dev", "target.os": "linux"}, "rule")


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"include": "a"}, "when must be a condition string"),
        ({"when": 'M == "x"', "include": "a", "exclude": "b"}, "exactly one"),
        ({"when": 'M == "x"'}, "exactly one"),
        ({"when": 'M == "x"', "include": "a", "if": "x"}, "unknown key"),
        ({"when": 'M == "x"', "include": []}, "non-empty"),
        ({"when": 'M == "x"', "include": [""]}, "empty pattern"),
        ({"when": 'M == "x"', "include": "../outside/**"}, "stay inside"),
        ({"when": 'M == "x"', "exclude": "/etc/**"}, "stay inside"),
        ({"when": 'M == "x"', "include": "C:\\secrets\\**"}, "stay inside"),
    ],
)
def test_invalid_rules_fail_even_when_inactive(rule, message):
    with pytest.raises(ValueError, match=message):
        evaluate_file_rules([rule], {"M": "other"}, "linux/amd64")


def test_ambient_environment_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_MODE", "dev")
    with pytest.raises(ValueError, match="unknown name"):
        BuildConfig(file_rules=[{"when": 'ENV == "dev"', "include": "x"}])
    context = _context(tmp_path)
    assert _selected(_config(context, tmp_path, [DEV_TOOLS])) == ["app.py"]


def test_build_mode_from_cli_build_arg_selects_files(tmp_path):
    context = _context(tmp_path)
    path = tmp_path / "pycontainer.toml"
    path.write_text(
        f'[tool.pycontainer]\ncontext-dir = {str(context)!r}\n'
        f'output-dir = {str(tmp_path / "out")!r}\ninclude-paths = ["app.py"]\n'
        '[tool.pycontainer.build-args]\nBUILD_MODE = "production"\n'
        '[[tool.pycontainer.file-rules]]\nwhen = \'BUILD_MODE == "dev"\'\n'
        'include = ["debug_tools/**"]\n'
        '[[tool.pycontainer.file-rules]]\nwhen = \'BUILD_MODE == "dev"\'\n'
        'exclude = "debug_tools/nested"\n'
    )
    assert _selected(config_from_file(path)) == ["app.py"]
    assert _selected(config_from_file(path, {}, ["BUILD_MODE=dev"])) == [
        "app.py", "debug_tools/inspect.py",
    ]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("linux/amd64", "native/amd64.so"), ("linux/arm64/v8", "native/arm64.so")],
)
def test_target_platform_rules(tmp_path, platform, expected):
    context = _context(tmp_path)
    rules = [
        {"when": 'target.os == "linux"', "include": "native/*.so"},
        {"when": 'target.arch != "amd64"', "exclude": "native/amd64.so"},
        {"when": 'target.arch != "arm64"', "exclude": "native/arm64.so"},
    ]
    assert _selected(_config(context, tmp_path, rules, platform=platform)) == [
        "app.py", expected,
    ]


def test_excludes_win_over_overlapping_includes(tmp_path):
    context = _context(tmp_path)
    rules = [
        {"when": 'BUILD_MODE == "production"', "include": ["**/*.py", "debug_tools"]},
        {"when": 'BUILD_MODE == "production"', "exclude": "debug_tools/**/trace.py"},
        {"when": 'BUILD_MODE == "production"', "exclude": "app.py"},
    ]
    assert _selected(_config(context, tmp_path, rules)) == ["debug_tools/inspect.py"]


def test_default_excludes_remain_unless_opted_out(tmp_path):
    context = _context(tmp_path)
    rules = [{"when": 'BUILD_MODE == "production"', "include": "tests/**"}]
    assert _selected(_config(context, tmp_path, rules)) == ["app.py"]
    assert _selected(_config(context, tmp_path, rules, exclude_defaults=False)) == [
        "app.py", "tests/test_app.py",
    ]


def test_generated_output_and_cache_are_never_selected(tmp_path):
    context = _context(tmp_path)
    for name in ("out/stale.py", "cache/blob.py"):
        (context / name).parent.mkdir(parents=True)
        (context / name).write_text("generated")
    config = _config(
        context, tmp_path,
        [{"when": 'BUILD_MODE == "production"', "include": ["**", "out/**", "cache"]}],
        output_dir=str(context / "out"), cache_dir=str(context / "cache"),
        exclude_defaults=False,
    )
    selected = _selected(config)
    assert not [name for name in selected if name.startswith(("out/", "cache/"))]
    assert "tests/test_app.py" in selected


def test_generated_output_excluded_through_symlinked_context(tmp_path):
    real = _context(tmp_path)
    (real / "out").mkdir()
    (real / "out" / "stale.py").write_text("generated")
    link = tmp_path / "linked-context"
    _symlink(link, real, directory=True)
    rule = {"when": 'BUILD_MODE == "production"', "include": "out/**"}
    config = _config(link, tmp_path, [rule], output_dir=str(real / "out"))
    assert _selected(config) == ["app.py"]


def test_backslash_and_slash_patterns_select_identically(tmp_path):
    context = _context(tmp_path)
    slash = {"when": 'BUILD_MODE == "production"', "include": "debug_tools/*/trace.py"}
    backslash = {"when": 'BUILD_MODE == "production"', "include": "debug_tools\\*\\trace.py"}
    assert (
        _selected(_config(context, tmp_path, [slash]))
        == _selected(_config(context, tmp_path, [backslash]))
        == ["app.py", "debug_tools/nested/trace.py"]
    )


def test_dry_run_tar_and_discovery_share_one_selection(tmp_path, capsys):
    context = _context(tmp_path)
    (context / "debug_tools" / "api.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    config = _config(context, tmp_path, [DEV_TOOLS], build_args={"BUILD_MODE": "dev"},
                     verbose=True, dry_run=True)
    builder = ImageBuilder(config)
    builder.build()
    expected = [
        "app.py", "debug_tools/api.py", "debug_tools/inspect.py",
        "debug_tools/nested/trace.py",
    ]
    plan = capsys.readouterr().out
    assert "Application Files: 4 selected" in plan
    assert [line.strip() for line in plan.splitlines()[-4:]] == expected
    assert config.entrypoint[1] == "debug_tools.api:app"

    layers = tmp_path / "layers"
    layers.mkdir()
    with tarfile.open(builder._create_app_layer(layers).tar_path) as archive:
        assert sorted(archive.getnames()) == [f"app/{name}" for name in expected]


def test_same_size_same_time_edit_changes_cached_layer(tmp_path):
    context = _context(tmp_path)
    rules = [DEV_TOOLS]
    cache_dir = tmp_path / "cache"

    def build():
        config = _config(context, tmp_path, rules, build_args={"BUILD_MODE": "dev"},
                         use_cache=True, cache_dir=str(cache_dir))
        layers = tmp_path / "layers"
        layers.mkdir(exist_ok=True)
        layer = ImageBuilder(config)._create_app_layer(layers)
        with tarfile.open(layer.tar_path) as archive:
            content = archive.extractfile("app/debug_tools/inspect.py").read()
        return layer.digest, content

    target = context / "debug_tools" / "inspect.py"
    before = target.stat()
    first_digest, _ = build()
    assert build()[0] == first_digest
    target.write_text("DEBUG")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    second_digest, content = build()
    assert second_digest != first_digest
    assert content == b"DEBUG"


def test_cache_identity_uses_canonical_active_rules():
    include_a = {"when": 'BUILD_MODE == "dev"', "include": "a/**"}
    include_b = {"when": 'BUILD_MODE == "dev"', "include": "b/**"}
    inactive = {"when": 'BUILD_MODE == "prod"', "include": "c/**"}

    def identity(rules, mode="dev"):
        return BuildConfig(build_args={"BUILD_MODE": mode}, file_rules=rules).cache_identity()

    assert identity([include_a, include_b]) == identity([include_b, inactive, include_a])
    assert identity([include_a]) != identity([include_a, include_b])
    assert identity([include_a], "prod") != identity([include_a])


def _symlink(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable: {error}")


@pytest.mark.parametrize("kind", ["outside", "dangling", "cyclic", "directory-root"])
def test_unsafe_conditional_symlinks_are_rejected(tmp_path, kind):
    context = _context(tmp_path)
    links = context / "links"
    links.mkdir()
    pattern = "links/**"
    if kind == "outside":
        (tmp_path / "secret.txt").write_text("secret")
        _symlink(links / "secret.txt", tmp_path / "secret.txt")
    elif kind == "dangling":
        _symlink(links / "missing.txt", context / "missing.txt")
    elif kind == "cyclic":
        _symlink(links / "loop", links / "loop")
    else:
        _symlink(context / "alias", context / "debug_tools", directory=True)
        pattern = "alias/**"
    rule = {"when": 'BUILD_MODE == "production"', "include": pattern}
    with pytest.raises(ValueError, match="symlink"):
        ImageBuilder(_config(context, tmp_path, [rule]))


def test_in_context_file_symlink_is_kept_as_link(tmp_path):
    context = _context(tmp_path)
    _symlink(context / "debug_tools" / "alias.py", context / "app.py")
    config = _config(context, tmp_path, [DEV_TOOLS], build_args={"BUILD_MODE": "dev"})
    builder = ImageBuilder(config)
    assert "debug_tools/alias.py" in _selected(config)
    layers = tmp_path / "layers"
    layers.mkdir()
    with tarfile.open(builder._create_app_layer(layers).tar_path) as archive:
        assert archive.getmember("app/debug_tools/alias.py").issym()


def test_cli_rejects_malformed_rule_before_builder(tmp_path, monkeypatch, capsys):
    path = tmp_path / "pycontainer.toml"
    path.write_text(
        '[build.build-args]\nMODE = "private-value"\n'
        '[[build.file-rules]]\nwhen = \'MODE === "dev"\'\ninclude = "x"\n'
    )
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path), "--dry-run"])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit):
        cli.main()
    error = capsys.readouterr().err
    assert "file_rules[1].when" in error
    assert "private-value" not in error
