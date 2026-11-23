import os
import tarfile

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.fs_utils import iter_files
from pycontainer.project import find_dependencies

OUTSIDE_BYTES = b"synthetic-outside-secret"


def _link(link, target, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symlinks are unavailable here: {error}")


def _junction(link, target):
    if os.name != "nt":
        pytest.skip("Junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _outside(tmp_path):
    outside = tmp_path / "outside"
    (outside / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    (outside / "secret.py").write_bytes(OUTSIDE_BYTES)
    (outside / "lib" / "python3.11" / "site-packages" / "leak.py").write_bytes(OUTSIDE_BYTES)
    return outside


def _context(tmp_path):
    context = tmp_path / "context"
    (context / "src").mkdir(parents=True)
    (context / "src" / "main.py").write_text("print('app')\n")
    return context


def _app_layer(context, tmp_path, name="build", **overrides):
    config = BuildConfig(
        tag="test:links",
        context_dir=str(context),
        output_dir=str(tmp_path / name / "image"),
        cache_dir=str(tmp_path / "cache"),
        base_image="python:3.11-slim-bookworm",
        **overrides,
    )
    builder = ImageBuilder(config)
    layers = tmp_path / name / "layers"
    layers.mkdir(parents=True)
    layer = builder._create_app_layer(layers, builder.context_include_paths)
    with tarfile.open(layer.tar_path) as archive:
        members = [(member.name, member.type, member.linkname) for member in archive]
        contents = b"".join(
            archive.extractfile(member).read() for member in archive if member.isfile()
        )
    return layer.digest, members, contents


@pytest.mark.parametrize("make_link", ["symlink", "junction"])
def test_linked_default_src_root_is_not_followed(tmp_path, make_link):
    outside = _outside(tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    if make_link == "symlink":
        _link(context / "src", outside, directory=True)
    else:
        _junction(context / "src", outside)

    with pytest.raises(ValueError, match="directory links are never followed"):
        _app_layer(context, tmp_path, use_cache=False)


@pytest.mark.parametrize("include", ["app", "app/nested"])
def test_explicit_include_through_directory_link_is_rejected(tmp_path, include):
    outside = _outside(tmp_path)
    (outside / "nested").mkdir()
    context = _context(tmp_path)
    _link(context / "app", outside, directory=True)

    with pytest.raises(ValueError, match="directory links are never followed"):
        list(iter_files(context, [include], exclude_defaults=False))


@pytest.mark.parametrize("make_link", ["symlink", "junction"])
def test_nested_directory_links_are_pruned_from_app_layer(tmp_path, make_link):
    outside = _outside(tmp_path)
    context = _context(tmp_path)
    inside = context / "shared"
    inside.mkdir()
    (inside / "data.py").write_text("value = 1\n")
    for name, target in (("escape", outside), ("alias", inside)):
        if make_link == "symlink":
            _link(context / "src" / name, target, directory=True)
        else:
            _junction(context / "src" / name, target)

    _, members, contents = _app_layer(context, tmp_path, use_cache=False)

    assert [name for name, _, _ in members] == ["app/src/main.py"]
    assert OUTSIDE_BYTES not in contents


def test_linked_virtualenv_is_not_staged(tmp_path):
    outside = _outside(tmp_path)
    context = _context(tmp_path)
    (context / "requirements.txt").write_text("")
    _link(context / ".venv", outside, directory=True)

    with pytest.raises(ValueError, match="directory links are never followed"):
        find_dependencies(context, exclude_defaults=False)


def test_virtualenv_nested_directory_link_is_pruned(tmp_path):
    outside = _outside(tmp_path)
    context = _context(tmp_path)
    site = context / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "pkg.py").write_text("value = 1\n")
    _link(site / "escape", outside / "lib" / "python3.11" / "site-packages", directory=True)

    dependencies = find_dependencies(context, exclude_defaults=False)

    assert [relative.as_posix() for _, relative in dependencies] == [
        ".venv/lib/python3.11/site-packages/pkg.py"
    ]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (os.path.join("..", "..", "outside", "secret.py"), "inside the build context"),
        ("missing.py", "dangling or cyclic"),
        ("loop.py", "dangling or cyclic|cyclic"),
    ],
)
def test_unsafe_selected_file_symlinks_are_rejected(tmp_path, target, message):
    _outside(tmp_path)
    context = _context(tmp_path)
    if target == "loop.py":
        _link(context / "src" / "loop.py", "link.py")
    _link(context / "src" / "link.py", target)

    with pytest.raises(ValueError, match=message):
        _app_layer(context, tmp_path, use_cache=False)


def test_absolute_selected_file_symlink_is_rejected(tmp_path):
    context = _context(tmp_path)
    _link(context / "src" / "link.py", str((context / "src" / "main.py").resolve()))

    with pytest.raises(ValueError, match="relative target"):
        list(iter_files(context, ["src"], exclude_defaults=False))


def test_excluded_unsafe_symlink_is_skipped(tmp_path):
    _outside(tmp_path)
    context = _context(tmp_path)
    _link(context / "src" / "link.py", os.path.join("..", "..", "outside", "secret.py"))

    _, members, contents = _app_layer(
        context, tmp_path, use_cache=False, exclude_paths=["src/link.py"]
    )

    assert [name for name, _, _ in members] == ["app/src/main.py"]
    assert OUTSIDE_BYTES not in contents


def test_in_context_symlink_to_selected_file_is_preserved(tmp_path):
    context = _context(tmp_path)
    _link(context / "src" / "alias.py", "main.py")

    _, members, _ = _app_layer(context, tmp_path, use_cache=False)

    assert members == [
        ("app/src/alias.py", tarfile.SYMTYPE, "main.py"),
        ("app/src/main.py", tarfile.REGTYPE, ""),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"exclude_paths": ["shared/data.py"]},
        {"include_paths": ["src"]},
    ],
    ids=["excluded-target", "unselected-target"],
)
def test_symlink_to_unselected_file_is_rejected(tmp_path, overrides):
    context = _context(tmp_path)
    (context / "shared").mkdir()
    (context / "shared" / "data.py").write_text("value = 1\n")
    _link(context / "src" / "data.py", os.path.join("..", "shared", "data.py"))
    overrides.setdefault("include_paths", ["src", "shared"])

    with pytest.raises(ValueError, match="another selected file"):
        _app_layer(context, tmp_path, use_cache=False, **overrides)


@pytest.mark.skipif(os.name == "nt", reason="Windows resolves '..' in link targets lexically")
def test_symlink_through_directory_link_dot_dot_is_rejected(tmp_path):
    context = _context(tmp_path)
    (context / "shared" / "inner").mkdir(parents=True)
    (context / "shared" / "data.py").write_text("value = 'excluded'\n")
    (context / "data.py").write_text("value = 'selected'\n")
    _link(context / "alias", os.path.join("shared", "inner"), directory=True)
    _link(context / "link.py", os.path.join("alias", "..", "data.py"))

    with pytest.raises(ValueError, match="another selected file"):
        _app_layer(
            context, tmp_path, use_cache=False,
            include_paths=["link.py", "data.py"], exclude_paths=["shared"],
        )


def test_changed_symlink_target_invalidates_cache(tmp_path):
    context = _context(tmp_path)
    (context / "src" / "other.py").write_text("print('other')\n")
    link = context / "src" / "alias.py"
    _link(link, "main.py")

    first_digest, first, _ = _app_layer(context, tmp_path, "first", use_cache=True)
    link.unlink()
    _link(link, "other.py")
    second_digest, second, _ = _app_layer(context, tmp_path, "second", use_cache=True)

    assert ("app/src/alias.py", tarfile.SYMTYPE, "main.py") in first
    assert ("app/src/alias.py", tarfile.SYMTYPE, "other.py") in second
    assert first_digest != second_digest


def test_context_root_symlink_is_still_supported(tmp_path):
    context = _context(tmp_path)
    alias = tmp_path / "alias"
    _link(alias, context, directory=True)

    _, members, _ = _app_layer(alias, tmp_path, use_cache=False)

    assert [name for name, _, _ in members] == ["app/src/main.py"]
