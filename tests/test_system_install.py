from pathlib import Path

import pytest

from pycontainer.system_install import (
    MirrorFile,
    SystemInstallError,
    check_apt_config,
    parse_apk_fetch_urls,
    parse_apk_simulate,
    parse_apt_print_uris,
    select_apk_downloads,
)

# Outputs captured on 2026-11-22 in disposable debian:bookworm-slim (apt 2.6.2),
# ubuntu:24.04 (apt 2.8.4) and alpine:3.23 (apk-tools 3.0.9) containers with
# --network none against a local copy of the signed upstream indexes.
FIXTURES = Path(__file__).parent / "fixtures" / "system_install"


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
        "",
        "(1/1) Installing libpq (18.7-r0)\n",
        "(1/2) Installing libpq (18.7-r0)\nOK: 1 KiB in 1 packages\n",
        "(2/2) Installing libpq (18.7-r0)\nOK: 1 KiB in 1 packages\n",
        "OK: 1 KiB in 1 packages\n(1/1) Installing libpq (18.7-r0)\n",
    ],
)
def test_apk_simulate_rejects_incomplete_output(output):
    with pytest.raises(SystemInstallError):
        parse_apk_simulate(output)


def test_apk_simulate_accepts_no_new_packages():
    assert parse_apk_simulate("OK: 8558 KiB in 17 packages\n") == []


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
