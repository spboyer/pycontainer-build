# Codename-to-libc mappings for official `python` tags

Dependency staging raises the target libc above the conservative
`manylinux2014`/`musllinux_1_2` floor only for a codename token in a tag-only
registry pull of the official `python` image. Each mapped value is the lowest
libc minor any image published under that codename tag can carry. Every value
below is pinned to an immutable source: a content-addressed Debian snapshot
file, or a commit SHA in Alpine `aports` or `docker-library/python`. Mutable
pointers such as `sources.debian.org` suite views or `3.N-stable` branch names
are not provenance.

Verified on 2026-11-22. The tables in `src/pycontainer/dependencies.py`
(`_DEBIAN_GLIBC_MINOR`, `_ALPINE_MUSL_MINOR`) must match this document.

## Debian (glibc)

Official images were first published from the earliest
[`docker-library/python`](https://github.com/docker-library/python) commit
adding a Dockerfile for that codename. The glibc floor is the `glibc` source
version in the Debian suite at that commit time, read from the snapshot
archive. `Extra-Source-Only: yes` stanzas, which `Built-Using` keeps for older
versions, are excluded: they are not the version the suite ships.
After first publication, a suite's package versions only increase, so later
rebuilds cannot fall below the floor.

| Codename | Floor | First `docker-library/python` commit | Suite `glibc` at that time | Snapshot file (content-addressed) |
| --- | --- | --- | --- | --- |
| `jessie` | glibc 2.19 | [`4faf7f1b13a1`](https://github.com/docker-library/python/commit/4faf7f1b13a13b24367307acc254bd2d0d48a46e) (2014-11-26) | 2.19-13 | [`86fdd161c47f`](https://snapshot.debian.org/file/86fdd161c47ff046eac8e34c2b9b00739670a9db) |
| `stretch` | glibc 2.24 | [`d8cda13511fc`](https://github.com/docker-library/python/commit/d8cda13511fcfac951ccb5a0d39a7da7568c2004) (2017-07-17) | 2.24-11 | [`a2c07bf36f9c`](https://snapshot.debian.org/file/a2c07bf36f9c494a2d5eeca0938bc476ded10902) |
| `buster` | glibc 2.28 | [`4fc051f0aa8e`](https://github.com/docker-library/python/commit/4fc051f0aa8ef60931474b23567a7f232095d85f) (2019-06-24) | 2.28-10 | [`cdaf35df7daa`](https://snapshot.debian.org/file/cdaf35df7daa46319a10d7d2d718b9474bc79e15) |
| `bullseye` | glibc 2.31 | [`f154e5d1c8f5`](https://github.com/docker-library/python/commit/f154e5d1c8f5b582aa2fd782df880b9cc96bb431) (2021-08-16) | 2.31-13 | [`03025ea20540`](https://snapshot.debian.org/file/03025ea2054005cfcf1a5388db7ab3d331d7d45b) |
| `bookworm` | glibc 2.36 | [`5d908b0477c0`](https://github.com/docker-library/python/commit/5d908b0477c003712550c84ac4d133367b912f78) (2023-06-13) | 2.36-9 | [`054d29885795`](https://snapshot.debian.org/file/054d298857955a2542fccbf2646402cd8e22a2bd) |
| `trixie` | glibc 2.41 | [`093598a0190b`](https://github.com/docker-library/python/commit/093598a0190ba9074b899d6a0a21a00c859aac56) (2025-08-08) | 2.41-12 | [`ade6a38d80ac`](https://snapshot.debian.org/file/ade6a38d80ac022757c3646185ee20047b5e7482) |

Each snapshot file is `dists/<codename>/main/source/Sources.xz` as resolved
from `https://snapshot.debian.org/archive/debian/<UTC commit time>/`, for
example `20250808T182034Z` for trixie. Trixie's Sources also lists `2.40-5`,
but only as an `Extra-Source-Only` stanza.

## Alpine (musl)

Official `python:*-alpine3.N` images build on `alpine:3.N`, which is only
published from the `v3.N.0` release onwards. The floor is the musl `pkgver` in
`main/musl/APKBUILD` at the release tag commit. The current branch head is
pinned as well to show that the release never left that musl minor.

| Tag token | Floor | `v3.N.0` commit: `pkgver` | Branch head commit: `pkgver` |
| --- | --- | --- | --- |
| `alpine3.0` | musl 1.1 | [`b19885138e9f`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=b19885138e9fc79a8115873b780e1495c8accc56): 1.1.1 | [`2c450b43736d`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=2c450b43736d2d8b5b33ed7614abd04bf0e09560): 1.1.4 |
| `alpine3.1` | musl 1.1 | [`8327d72e3684`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=8327d72e3684534eb1b0ea4352d972ed0e52cc8c): 1.1.5 | [`ba3dc3d21018`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=ba3dc3d210189d8b88c35c3b6850f54f8041f3fa): 1.1.5 |
| `alpine3.2` | musl 1.1 | [`6c96fddd7d8d`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=6c96fddd7d8d387d4f60e7c308b22786927d2ee1): 1.1.9 | [`95b875ef631e`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=95b875ef631ebac54850aec71295a0da63bfbf53): 1.1.11 |
| `alpine3.3` | musl 1.1 | [`ae40ed17f110`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=ae40ed17f1105605cbfee4fd8546faf659f50119): 1.1.12 | [`74d5d2e7e054`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=74d5d2e7e054ab3e6fa60384e9c60c8d746f3907): 1.1.12 |
| `alpine3.4` | musl 1.1 | [`00864455a5b1`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=00864455a5b1ef971280c32347288d7f6f14e5a1): 1.1.14 | [`b5087e76b93e`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=b5087e76b93e768aaa081d0219cc0757375746d5): 1.1.14 |
| `alpine3.5` | musl 1.1 | [`9909afdb1f70`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=9909afdb1f7093a2356e07eab2fbc98d75428d91): 1.1.15 | [`ed4ea0821f28`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=ed4ea0821f28616c20c5387e018faf00334b29dd): 1.1.15 |
| `alpine3.6` | musl 1.1 | [`58a0da638390`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=58a0da638390e524a7dda84a09cb0211e257a33d): 1.1.16 | [`f768243373c9`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=f768243373c954098348dc7fc51eaf85babee185): 1.1.16 |
| `alpine3.7` | musl 1.1 | [`4bb0f9934eee`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=4bb0f9934eee863e7954c325843c100f26b1eaed): 1.1.18 | [`cddd1b23022d`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=cddd1b23022d0fb84f0bd740bbaa17e869bb53e7): 1.1.18 |
| `alpine3.8` | musl 1.1 | [`2484b3eda99f`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=2484b3eda99f681c7de0866b438f63cdcc31b5da): 1.1.19 | [`f94de196ca38`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=f94de196ca38cb6586578a0934eb821222da527f): 1.1.19 |
| `alpine3.9` | musl 1.1 | [`8207e0e6e18c`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=8207e0e6e18cb581db15c167b8544f52bbca18aa): 1.1.20 | [`e3401697f466`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=e3401697f466424f5db694f664adc4350a3f3907): 1.1.20 |
| `alpine3.10` | musl 1.1 | [`65ba30dd7704`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=65ba30dd770483d8d4e1adb24c604d1959a6babc): 1.1.22 | [`4c0cc82ff8ef`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=4c0cc82ff8ef1efe037908f6f170abd54901c043): 1.1.22 |
| `alpine3.11` | musl 1.1 | [`3c23ef02cdf8`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=3c23ef02cdf8ad9e4ea8e955536826328bf034ee): 1.1.24 | [`2cfa91a2b43e`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=2cfa91a2b43e469bf6ac37ceeffe3eff40397d5d): 1.1.24 |
| `alpine3.12` | musl 1.1 | [`9a0677284deb`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=9a0677284debf17071d191d8e1c5556ffe564875): 1.1.24 | [`79e71a187dc0`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=79e71a187dc08f640216afd3a911f08c0a787a93): 1.1.24 |
| `alpine3.13` | musl 1.2 | [`09276e69ab25`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=09276e69ab257f4d2ecfa545ae753eaea9fe6fbd): 1.2.2_pre7 | [`9bb51a63ac1f`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=9bb51a63ac1fd53c9e1bbe0745c6b210714039b8): 1.2.2 |
| `alpine3.14` | musl 1.2 | [`cf59c903c95a`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=cf59c903c95a0d42ccae4509a205d46f00c6ceb2): 1.2.2 | [`c6895df0c923`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=c6895df0c923054d12ff9b93a6c69d35eaba8706): 1.2.2 |
| `alpine3.15` | musl 1.2 | [`ffb3e3e05a68`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=ffb3e3e05a688dfd93f2953dd50a2616ff5350f4): 1.2.2 | [`2a2ec278a7b2`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=2a2ec278a7b280206099424fb42a4d33b06edf37): 1.2.2 |
| `alpine3.16` | musl 1.2 | [`e2c6d5931e7f`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=e2c6d5931e7fd56ac46def042ece5552df79d7c6): 1.2.3 | [`6421845cbd09`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=6421845cbd09fc49c3a0a14865c18296ce69be98): 1.2.3 |
| `alpine3.17` | musl 1.2 | [`fea827d99a2e`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=fea827d99a2ecc4267bf7184abaaeec2ab7108e2): 1.2.3 | [`d1bfe4195d0a`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=d1bfe4195d0a52990a4513afcc62e5be1fbafd94): 1.2.3 |
| `alpine3.18` | musl 1.2 | [`8b72c4075123`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=8b72c4075123f7310c54ae09e83016890cadefd3): 1.2.4 | [`d8ae998ae778`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=d8ae998ae778948b15c97d3c0ad3aa35a6f04b5c): 1.2.4 |
| `alpine3.19` | musl 1.2 | [`d01502332b77`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=d01502332b773d59a09220aac959a3c18c429ce3): 1.2.4_git20230717 | [`a1666666ac5b`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=a1666666ac5b1d0464be213cc81ff19130ae2a2f): 1.2.4_git20230717 |
| `alpine3.20` | musl 1.2 | [`1402388a3a74`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=1402388a3a746e9961791846350e35f1d4d4618d): 1.2.5 | [`9d9c124fde04`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=9d9c124fde0435b175731dd3170ee4899f810718): 1.2.5 |
| `alpine3.21` | musl 1.2 | [`59962f87e460`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=59962f87e4603deb5bb4d237927fd7eb84c7c9c9): 1.2.5 | [`913f2351f392`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=913f2351f392cce89ceb0c31afcc2d9cf32fcf2f): 1.2.5 |
| `alpine3.22` | musl 1.2 | [`76769759d295`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=76769759d295378b24d0ed9a9d252dfe525cf1c0): 1.2.5 | [`82fbff58eeec`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=82fbff58eeec49753b605f745b2d1d1dbfb000ae): 1.2.5 |
| `alpine3.23` | musl 1.2 | [`319b82182d15`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=319b82182d156ddb81ab872b5dc368b47c5cb3c4): 1.2.5 | [`a2a1c9ed8570`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=a2a1c9ed8570b5377dae0a6535674a0df7c58a0b): 1.2.5 |
| `alpine3.24` | musl 1.2 | [`d0f20bc945ea`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=d0f20bc945eabe7bac39a6ee168be84046f27875): 1.2.6 | [`d76eb1f5547d`](https://git.alpinelinux.org/aports/plain/main/musl/APKBUILD?id=d76eb1f5547d1294ea66305d592337d246c5a62e): 1.2.6 |

Commit SHAs are the peeled `refs/tags/v3.N.0^{}` and `refs/heads/3.N-stable`
values from `git ls-remote https://git.alpinelinux.org/aports`.

## Limitations

- Pinned commits cover the release point and one later branch commit, not
  every intermediate commit. Both points share the same musl minor, and
  Alpine stable branches only take patch updates.
- New codenames and Alpine releases stay on the codename-less floor until a
  row is verified and added here and in the code table.
- Docker Hub tags are mutable. The mapping is a floor for every published
  image under a codename tag, not a statement about a particular digest, which
  is why digest references still require explicit `PYTHON_LIBC` metadata.
- No tags below glibc 2.17 or musl 1.1 are generated.
