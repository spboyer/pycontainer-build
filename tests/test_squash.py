"""Synthetic-layer tests for exact OCI changeset squashing."""
import io
import tarfile

import pytest

from pycontainer.squash import SquashError, squash_layers


def _entry(name, kind="file", data=b"", **fields):
    info = tarfile.TarInfo(name)
    info.mtime = fields.pop("mtime", 0)
    info.mode = fields.pop("mode", 0o755 if kind == "dir" else 0o644)
    if kind == "dir":
        info.type = tarfile.DIRTYPE
    elif kind == "symlink":
        info.type, info.linkname = tarfile.SYMTYPE, fields.pop("target")
    elif kind == "hardlink":
        info.type, info.linkname = tarfile.LNKTYPE, fields.pop("target")
    elif kind == "fifo":
        info.type = tarfile.FIFOTYPE
    else:
        info.size = len(data)
    for key, value in fields.items():
        setattr(info, key, value)
    return info, (data if kind == "file" else None)


def _layer(tmp_path, name, *entries, fmt=tarfile.PAX_FORMAT):
    path = tmp_path / name
    with tarfile.open(path, "w", format=fmt) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _squash(tmp_path, *layers):
    paths = [_layer(tmp_path, f"layer{index}.tar", *entries) for index, entries in enumerate(layers)]
    out = io.BytesIO()
    stats = squash_layers(paths, out)
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        members = {
            info.name: (info, tar.extractfile(info).read() if info.isreg() else None)
            for info in tar
        }
        order = tar.getnames()
    return members, order, stats, out.getvalue()


def test_later_file_shadows_earlier_bytes(tmp_path):
    members, _, stats, data = _squash(
        tmp_path,
        [_entry("app/a.txt", data=b"old" * 1000)],
        [_entry("app/a.txt", data=b"new")],
    )
    assert members["app/a.txt"][1] == b"new"
    assert stats.dropped_entries == 1
    assert stats.dropped_file_bytes == 3000
    assert stats.output_bytes == len(data)
    assert stats.output_bytes < stats.source_bytes


def test_whiteout_of_base_path_is_kept_and_hides_squashed_content(tmp_path):
    members, _, stats, _ = _squash(
        tmp_path,
        [_entry("app/tmp/cache", data=b"x" * 600), _entry("app/keep", data=b"k")],
        [_entry("app/tmp/.wh.cache"), _entry("app/.wh.base-only")],
    )
    assert set(members) == {"app/tmp/.wh.cache", "app/keep", "app/.wh.base-only"}
    assert stats.dropped_file_bytes == 600


def test_whiteout_then_readd_file_drops_marker(tmp_path):
    members, _, _, _ = _squash(
        tmp_path,
        [_entry(".wh.etc-file")],
        [_entry("etc-file", data=b"new")],
    )
    assert set(members) == {"etc-file"}


def test_directory_replacing_deleted_path_becomes_opaque(tmp_path):
    members, order, _, _ = _squash(
        tmp_path,
        [_entry("app/.wh.lib")],
        [_entry("app/lib", "dir"), _entry("app/lib/new.py", data=b"n")],
    )
    assert set(members) == {"app/lib", "app/lib/.wh..wh..opq", "app/lib/new.py"}
    assert order.index("app/lib") < order.index("app/lib/.wh..wh..opq") < order.index("app/lib/new.py")


def test_later_opaque_directory_drops_earlier_children(tmp_path):
    members, order, stats, _ = _squash(
        tmp_path,
        [_entry("app", "dir"), _entry("app/old.py", data=b"o" * 10)],
        [_entry("app", "dir", mode=0o700), _entry("app/.wh..wh..opq"), _entry("app/new.py", data=b"n")],
    )
    assert set(members) == {"app", "app/.wh..wh..opq", "app/new.py"}
    assert members["app"][0].mode == 0o700
    assert order == ["app", "app/.wh..wh..opq", "app/new.py"]
    assert stats.dropped_file_bytes == 10


def test_file_replacing_directory_drops_children(tmp_path):
    members, _, _, _ = _squash(
        tmp_path,
        [_entry("app/pkg", "dir"), _entry("app/pkg/mod.py", data=b"m")],
        [_entry("app/pkg", data=b"file now")],
    )
    assert set(members) == {"app/pkg"}
    assert members["app/pkg"][0].isreg()


def test_directory_replacing_file_is_opaque_to_hide_base(tmp_path):
    members, _, _, _ = _squash(
        tmp_path,
        [_entry("app/pkg", data=b"file")],
        [_entry("app/pkg", "dir"), _entry("app/pkg/mod.py", data=b"m")],
    )
    assert set(members) == {"app/pkg", "app/pkg/.wh..wh..opq", "app/pkg/mod.py"}


def test_symlinks_and_metadata_are_preserved(tmp_path):
    xattrs = {"SCHILY.xattr.user.note": "kept"}
    members, _, _, _ = _squash(
        tmp_path,
        [_entry("app/run", data=b"#!", mode=0o4755, uid=7, gid=8, uname="u", gname="g",
                mtime=1234, pax_headers=xattrs)],
        [_entry("app/link", "symlink", target="../outside/run")],
    )
    info = members["app/run"][0]
    assert (info.mode, info.uid, info.gid, info.uname, info.gname, info.mtime) == (
        0o4755, 7, 8, "u", "g", 1234
    )
    assert info.pax_headers["SCHILY.xattr.user.note"] == "kept"
    assert members["app/link"][0].linkname == "../outside/run"


def test_hardlink_to_squashed_file_is_emitted_after_target(tmp_path):
    members, order, _, _ = _squash(
        tmp_path,
        [_entry("app/a", data=b"same")],
        [_entry("app/b", "hardlink", target="app/a")],
    )
    assert members["app/b"][0].linkname == "app/a"
    assert order.index("app/a") < order.index("app/b")


@pytest.mark.parametrize("layers, message", [
    ([[_entry("app/b", "hardlink", target="etc/passwd")]], "base-image path"),
    ([[_entry("app/a", data=b"1"), _entry("app/b", "hardlink", target="app/a")],
      [_entry("app/a", data=b"2")]], "later layer replaced"),
    ([[_entry("app/link", "symlink", target="/tmp")], [_entry("app/link/x", data=b"x")]], "non-directory"),
    ([[_entry(".wh.app")], [_entry("app/x", data=b"x")]], "implicitly recreates"),
    ([[_entry(".wh.app")], [_entry("app/.wh..wh..opq")]], "without recreating"),
    ([[_entry("app/../etc/passwd", data=b"x")]], "traversal"),
    ([[_entry("app\\evil", data=b"x")]], "backslash"),
    ([[_entry("app/\x01bad", data=b"x")]], "control"),
    ([[_entry("app/fifo", "fifo")]], "FIFO"),
    ([[_entry("app/x", data=b"1"), _entry("./app/x", data=b"2")]], "more than once"),
    ([[_entry(".wh..wh..opq")]], "image root opaque"),
    ([[_entry("app/.wh.x/y", data=b"x")]], "parent directory"),
    ([[_entry("app/.wh.x", data=b"not empty")]], "empty regular file"),
    ([[_entry("app/x", data=b"x", pax_headers={"SCHILY.xattr.trusted.overlay.opaque": "y"})]], "trusted.overlay"),
    ([[_entry("app/x", data=b"x", pax_headers={"VENDOR.custom": "v"})]], "PAX header"),
])
def test_unrepresentable_changesets_are_rejected(tmp_path, layers, message):
    with pytest.raises(SquashError, match=message):
        _squash(tmp_path, *layers)


def test_leading_slash_names_are_normalized(tmp_path):
    members, _, _, _ = _squash(
        tmp_path,
        [_entry("/app/.python/site-packages/pkg.py", data=b"dep")],
        [_entry("app//main.py", data=b"main")],
    )
    assert set(members) == {"app/.python/site-packages/pkg.py", "app/main.py"}


def test_squash_output_is_deterministic(tmp_path):
    layers = (
        [_entry("app/a", data=b"a"), _entry("app/b", data=b"b")],
        [_entry("app/a", data=b"A"), _entry("app/.wh.b"), _entry("app/c", data=b"c")],
    )
    first = _squash(tmp_path / "one" if (tmp_path / "one").mkdir() is None else None, *layers)[3]
    second = _squash(tmp_path / "two" if (tmp_path / "two").mkdir() is None else None, *layers)[3]
    assert first == second


def test_measure_only_matches_written_size(tmp_path):
    paths = [
        _layer(tmp_path, "l0.tar", _entry("app/a", data=b"a" * 700)),
        _layer(tmp_path, "l1.tar", _entry("app/a", data=b"b")),
    ]
    out = io.BytesIO()
    assert squash_layers(paths, None).output_bytes == squash_layers(paths, out).output_bytes == len(out.getvalue())


def test_analysis_prediction_matches_measured_size_for_shadowed_layers(tmp_path):
    from types import SimpleNamespace

    from pycontainer.layer_analysis import _squash_summary

    layers = [
        SimpleNamespace(tar_path=_layer(tmp_path, "l0.tar", _entry("app/big", data=b"x" * 50000),
                                        _entry("app/keep", data=b"k"))),
        SimpleNamespace(tar_path=_layer(tmp_path, "l1.tar", _entry("app/big", data=b"small"))),
    ]
    summary = _squash_summary(layers, None, None)
    assert summary["dropped_file_bytes"] == 50000
    assert summary["predicted_bytes"] == summary["measured_bytes"]
    assert summary["measured_savings_bytes"] >= 50000

@pytest.mark.parametrize("layers", [
    # Directories repeated in every layer: only the last header is emitted.
    [[_entry(f"app/d{i}", kind="dir") for i in range(20)] for _ in range(2)],
    # Synthesized opaque marker and a whiteout consumed by a replacement directory.
    [[_entry("app/x", data=b"x")], [_entry("app/.wh.x"), _entry("app/y", data=b"y")],
     [_entry("app/x", kind="dir"), _entry("app/x/z", data=b"z")]],
])
def test_prediction_matches_written_size(tmp_path, layers):
    _, _, stats, data = _squash(tmp_path, *layers)
    assert stats.predicted_bytes == stats.output_bytes == len(data)


def test_pax_access_and_change_times_are_preserved(tmp_path):
    info, data = _entry("app/t", data=b"t")
    info.pax_headers = {"atime": "12.5", "ctime": "34.25"}
    members, _, _, _ = _squash(tmp_path, [_entry("app/other", data=b"o")], [(info, data)])
    headers = members["app/t"][0].pax_headers
    assert (headers["atime"], headers["ctime"]) == ("12.5", "34.25")
