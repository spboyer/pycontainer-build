# Codename-to-libc mappings for official `python` tags

Dependency staging selects libc tags using explicit `PYTHON_LIBC` metadata
or a mapped codename token in a tag-only registry pull of the official
`python` image. Only glibc keeps the codename-less `manylinux2014` floor;
unproven Alpine tags fail before staging. Mapped values are conservative
bounds based on the source evidence below, not measurements of individual
image digests. The evidence is pinned to content-addressed Debian snapshot
files and commits in Alpine `aports` or `docker-library/python`. Mutable
pointers such as `sources.debian.org` suite views or `3.N-stable` branch names
are not provenance.

Verified on 2026-11-22. The tables in `src/pycontainer/dependencies.py`
(`_DEBIAN_GLIBC_MINOR`, `_ALPINE_MUSL_MINOR`) must match this document.

## Debian (glibc)

An official Python Dockerfile refers to a Debian base tag, not a pinned base
digest. A suite snapshot at the Python Dockerfile commit therefore does not
establish which libc the image contains. These bounds instead use the suite
version when the codename first appeared in the official Debian image
declarations. Source snapshots exclude `Extra-Source-Only: yes` stanzas.

| Codename | Bound | First Debian declaration | Suite glibc and snapshot |
| --- | --- | --- | --- |
| `jessie` | 2.17 | [2013-10-16](https://github.com/docker-library/official-images/blob/3e681864479143fb9181da30535537b82e5d0a10/library/debian) | [2.17-93](https://snapshot.debian.org/file/04b8bae698b81a912f8de7674a0a790a1b037363) |
| `stretch` | 2.19 | [2015-04-29](https://github.com/docker-library/official-images/blob/3268afd76587dee2bd822b80ffad094a26366ce8/library/debian) | [2.19-18](https://snapshot.debian.org/file/2e2a7fc2d5e9432881c561f1940dda10f607e094) |
| `buster` | 2.24 | [2017-06-20](https://github.com/docker-library/official-images/blob/abbad2317e1d87ed1e18415daf3449761749120c/library/debian) | [2.24-11](https://snapshot.debian.org/file/8ff887859eb9de9cbe75fdecfd6579b65f53f3f9) |
| `bullseye` | 2.28 | [2019-07-09](https://github.com/docker-library/official-images/blob/30378f3a56360e87b280dd8fea13fa6d1846dfb5/library/debian) | [2.28-10](https://snapshot.debian.org/file/3eb44153c215e6fab6d7e1a010ccbb729d7d0075) |
| `bookworm` | 2.31 | [2021-08-16](https://github.com/docker-library/official-images/blob/2b8712cd578f655aff4e26face274d8f702dadb9/library/debian) | [2.31-13](https://snapshot.debian.org/file/03025ea2054005cfcf1a5388db7ab3d331d7d45b) |
| `trixie` | 2.36 | [2023-06-12](https://github.com/docker-library/official-images/blob/da90f58ec995f7ab5a5c871a42f46b9bd84c4709/library/debian) | [2.36-9](https://snapshot.debian.org/file/602efa7f4269b8718797aea88b35b439b8055279) |

On 2026-11-22, the archived image manifests below were fetched from Docker Hub.
Their manifest and layer digests and sizes were verified; the `libc6` version
was read from `var/lib/dpkg/status` inside each layer without extracting it.
The date tags identify the observed images; the SHA-256 manifest digests, not
the mutable tags, identify the checked artifacts.

| Debian image | Architecture | Manifest SHA-256 | `libc6` |
| --- | --- | --- | --- |
| `buster-20170620` | amd64 | `a4f78f38cf60ed790efe91fcea9333ba634fe2ecbbc1418a55ac5d04047d32eb` | 2.24-11 |
| `bullseye-20190708-slim` | amd64 | `62adebce7f39b0603644bca1aaff7678f5abe50776bf4334cd5a7e7facbb1d85` | 2.28-10 |
| `bullseye-20190708-slim` | arm64 | `a342b414ce3c37fd6e12b7ba416a7c4f0aa29b7302eb2a8cf7b257be2d164de5` | 2.28-10 |
| `bookworm-20210816-slim` | amd64 | `454b407ad1612c8a30f55a71b49333ea2d88855eadec9c10f16cfaaf59786d81` | 2.31-13 |
| `bookworm-20210816-slim` | arm64 | `b6fac6322e14853f243e0385e7d9983bcc5b5c1491ad30429945a763b002f834` | 2.31-13 |
| `trixie-20230612-slim` | amd64 | `89e094999244cf2481fd2af9535f18c647f9f0b216bdaed98e7d6350af2e6191` | 2.36-9 |
| `trixie-20230612-slim` | arm64 | `9e76f56013f737abb3e980dcbaa05c0bc7c7a210a6b270fde5bcd9de48af6bb1` | 2.36-9 |

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

- Safety amendment (2026-11-22, #143 / #191): the original unchanged
  codename-less Alpine acceptance is superseded. The coordinator's verified
  Docker Hub pull of `python:3.8.0-alpine`, linux/amd64 manifest
  `sha256:3996a4b86240b6d41b57055cd7f626cbb15e2baef149b4fd841e04fec7308290`,
  has Alpine 3.10.3 and final musl `1.1.22-r3` in `/lib/apk/db/installed`.
  The manifest, config and all five layer digests and sizes were verified;
  package-database updates were applied in layer order. The verified config
  has `PYTHON_VERSION=3.8.0` and no `PYTHON_LIBC`. No container was run.
  The previous `musllinux_1_2` fallback permitted a newer-musl wheel.
  A hermetic CPython 3.8 wheel fixture reproduces that pip selection before
  the fix. Codename-less and unmapped Alpine tags now require explicit
  supported `PYTHON_LIBC`, or a mapped release tag, before requirements or
  virtualenv staging. Neither the Python version nor an unspecified Alpine
  tag proves even a musl 1.1 floor. This is an intentional compatibility
  change, not evidence that runtime package databases are inspected by the
  builder.
- Pinned commits cover the release point and one later branch commit, not
  every intermediate commit. Both points share the same musl minor, and
  Alpine stable branches only take patch updates.
- New Debian codenames stay on the codename-less glibc floor until a row is
  verified and added here and in the code table. New or unmapped Alpine
  releases require explicit libc metadata until their row is verified.
- Docker Hub tags are mutable. These source records do not verify the libc
  installed in every published Python image digest, or prove that no
  intermediate suite update ever contained an older version. These
  conservative bounds may reject wheels that a particular newer image could
  run. Digest references still require explicit `PYTHON_LIBC` metadata.
- No tags below glibc 2.17 or musl 1.1 are generated.
