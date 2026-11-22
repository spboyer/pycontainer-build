import io
from pathlib import Path

import pytest

from pycontainer import system_install
from pycontainer.system_install import (
    MirrorFile,
    SystemInstallError,
    check_apt_config,
    fetch_untrusted,
    parse_apk_fetch_urls,
    parse_apk_simulate,
    parse_apt_print_uris,
    select_apk_downloads,
)

# Outputs captured on 2026-11-22 in disposable debian:bookworm-slim (apt 2.6.2),
# ubuntu:24.04 (apt 2.8.4) and alpine:3.23 (apk-tools 3.0.9) containers with
# --network none against a local copy of the signed upstream indexes.
FIXTURES = Path(__file__).parent / "fixtures" / "system_install"
HOST = "deb.debian.org"


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name", ["apt-config-dump-debian-bookworm.txt", "apt-config-dump-ubuntu-24.04.txt"]
)
def test_official_image_apt_config_is_accepted(name):
    check_apt_config(_fixture(name))


@pytest.mark.parametrize(
    "line",
    [
        'Acquire::AllowInsecureRepositories "1";',
        'Acquire::AllowWeakRepositories "true";',
        'Acquire::AllowDowngradeToInsecureRepositories "yes";',
        'APT::Get::AllowUnauthenticated "maybe";',
        'Binary::apt-get::Acquire::AllowInsecureRepositories "on";',
        'APT::Sandbox::User "root";',
        'Acquire::gpgv::Options:: "--ignore-time-conflict";',
        'APT::Key::gpgvcommand "/bin/true";',
        'Acquire::Check-Valid-Until "false";',
        'Acquire::gpgv::Options::custom "--ignore-time-conflict";',
        'Dir::Bin::apt-key "/tmp/fake-apt-key";',
        'Dir::Bin::methods "/tmp/methods";',
        'Dir::Bin::methods::gpgv "/tmp/gpgv";',
        'Acquire::Check-Date "0x0";',
        'Acquire::Check-Valid-Until "00";',
    ],
)
def test_apt_config_rejects_weakened_verification(line):
    dump = _fixture("apt-config-dump-debian-bookworm.txt") + line + "\n"
    with pytest.raises(SystemInstallError, match="weakens repository verification"):
        check_apt_config(dump)


def test_expired_release_override_still_requires_signatures():
    check_apt_config('Acquire::Check-Valid-Until "0";\n', allow_expired_release=True)
    with pytest.raises(SystemInstallError):
        check_apt_config(
            'Acquire::Check-Valid-Until "0";\nAcquire::AllowInsecureRepositories "1";\n',
            allow_expired_release=True,
        )


def test_apt_config_rejects_unparseable_output():
    with pytest.raises(SystemInstallError, match="Unrecognized"):
        check_apt_config("E: Could not open file\n")


def test_print_uris_maps_to_decoded_mirror_paths_and_ignores_weak_hashes():
    files = parse_apt_print_uris(_fixture("apt-print-uris-debian-bookworm.txt"), "/m/deb")
    assert files[0] == MirrorFile(
        "pool/main/k/krb5/libkrb5support0_1.20.1-2+deb12u5_amd64.deb", 33212
    )
    assert len(files) == 3


def test_print_uris_accepts_missing_hash_field():
    # apt 3.0.4 on trixie prints no hash for debian-security packages.
    line = "'file:/m/pool/a/a_1_amd64.deb' a_1_amd64.deb 9 \n"
    assert parse_apt_print_uris(line, "/m") == [MirrorFile("pool/a/a_1_amd64.deb", 9)]


@pytest.mark.parametrize(
    "line",
    [
        "'http://deb.debian.org/debian/pool/a/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file:/other/pool/a/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file:/m/pool/../../etc/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file:/m/pool%2f..%2fx/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file://evil/m/pool/a/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file:/m/pool/a/a_1_amd64.deb' b_1_amd64.deb 9 SHA256:00",
        "'file:/m/pool/a/a_1_amd64.deb' a_1_amd64.deb 0 SHA256:00",
        "'file:/m/C:/outside/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "'file://[bad/m/a_1_amd64.deb' a_1_amd64.deb 9 SHA256:00",
        "W: Some index files failed to download",
    ],
)
def test_print_uris_rejects_unsafe_or_unknown_lines(line):
    with pytest.raises(SystemInstallError):
        parse_apt_print_uris(line + "\n", "/m")


def test_print_uris_rejects_duplicates():
    line = "'file:/m/pool/a/a_1_amd64.deb' a_1_amd64.deb 9 \n"
    with pytest.raises(SystemInstallError, match="Duplicate"):
        parse_apt_print_uris(line * 2, "/m")


def test_apk_selects_only_packages_add_would_install():
    simulated = parse_apk_simulate(_fixture("apk-add-simulate-alpine-3.23.txt"))
    paths = parse_apk_fetch_urls(_fixture("apk-fetch-url-alpine-3.23.txt"), "/m/3.23")
    assert simulated == [("libpq", "18.7-r0")]
    assert select_apk_downloads(simulated, paths) == [
        MirrorFile("main/x86_64/libpq-18.7-r0.apk")
    ]


@pytest.mark.parametrize(
    "output",
    [
        # apk only warns and skips an index with an untrusted signature.
        "WARNING: opening /m/main/x86_64/APKINDEX.tar.gz: UNTRUSTED signature\n",
        "(1/1) Upgrading musl (1.2.5-r13 -> 1.2.5-r24)\n",
        "(1/1) Installing libpq (18.7-r0)\nExecuting something\n",
    ],
)
def test_apk_simulate_fails_closed(output):
    with pytest.raises(SystemInstallError):
        parse_apk_simulate(output)


@pytest.mark.parametrize(
    "output",
    [
        "https://dl-cdn.alpinelinux.org/alpine/v3.23/main/x86_64/libpq-18.7-r0.apk\n",
        "/m/3.23/../x86_64/libpq-18.7-r0.apk\n",
        "/m/3.23/main/x86_64/APKINDEX.tar.gz\n",
    ],
)
def test_apk_fetch_rejects_non_mirror_paths(output):
    with pytest.raises(SystemInstallError):
        parse_apk_fetch_urls(output, "/m/3.23")


def test_apk_selection_requires_every_simulated_package():
    with pytest.raises(SystemInstallError, match="no libpq-18.7-r0.apk"):
        select_apk_downloads([("libpq", "18.7-r0")], ["main/x86_64/libpq-18.6-r0.apk"])


class _Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0)


def _patch_opener(monkeypatch, *responses):
    opener = _Opener(responses)
    monkeypatch.setattr(system_install, "_build_opener", lambda: opener)
    return opener


def test_fetch_verifies_and_publishes_without_credentials(monkeypatch, tmp_path):
    import hashlib

    body = b"package bytes"
    opener = _patch_opener(monkeypatch, _Response(body=body))
    target = tmp_path / "pool" / "a.deb"
    digest, size = fetch_untrusted(
        f"https://{HOST}/debian/pool/a.deb",
        target,
        [HOST],
        max_bytes=100,
        expected_size=len(body),
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )
    assert target.read_bytes() == body and size == len(body)
    assert digest == hashlib.sha256(body).hexdigest()
    assert "Authorization" not in opener.requests[0].headers
    assert list(target.parent.iterdir()) == [target]


def test_fetch_follows_only_allowed_https_redirects(monkeypatch, tmp_path):
    _patch_opener(
        monkeypatch,
        _Response(status=302, headers={"Location": "https://mirror.example.org/a"}),
    )
    with pytest.raises(SystemInstallError, match="outside the allowed"):
        fetch_untrusted(f"https://{HOST}/a", tmp_path / "a", [HOST], max_bytes=10)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/a",
        f"https://user:pw@{HOST}/a",
        f"https://{HOST}.evil.example/a",
        f"https://{HOST}:8443/a",
        f"https://{HOST}/a?x=1",
        "https://DEB.debian.org/a",
        "https://[bad/a",
    ],
)
def test_fetch_rejects_urls_outside_policy(monkeypatch, tmp_path, url):
    _patch_opener(monkeypatch)
    with pytest.raises(SystemInstallError):
        fetch_untrusted(url, tmp_path / "a", [HOST], max_bytes=10)


@pytest.mark.parametrize(
    "response, kwargs",
    [
        (_Response(body=b"x" * 11), {}),
        (_Response(body=b"x", headers={"Content-Length": "11"}), {}),
        (_Response(body=b"abc"), {"expected_size": 4}),
        (_Response(body=b"abc"), {"expected_sha256": "0" * 64}),
        (_Response(status=404), {}),
    ],
)
def test_fetch_fails_closed_and_leaves_no_file(monkeypatch, tmp_path, response, kwargs):
    _patch_opener(monkeypatch, response)
    with pytest.raises(SystemInstallError):
        fetch_untrusted(f"https://{HOST}/a", tmp_path / "a", [HOST], max_bytes=10, **kwargs)
    assert not list(tmp_path.iterdir())


def test_fetch_enforces_total_deadline(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _Response(body=b"abc"))
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(system_install.time, "monotonic", lambda: next(clock))
    with pytest.raises(SystemInstallError, match="deadline"):
        fetch_untrusted(f"https://{HOST}/a", tmp_path / "a", [HOST], max_bytes=10, deadline=5)
    assert not list(tmp_path.iterdir())
