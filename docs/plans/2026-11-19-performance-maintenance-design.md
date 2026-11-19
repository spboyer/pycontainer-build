# Performance and maintenance audit

Observed: **2026-11-19**. Status: verified baseline plus proposed follow-up work.
Source: `spboyer/pycontainer-build` at
`628f62340b509102b8820605a750dc158b265cf4`.

## Decision and scope

Use an evidence-first maintenance pass, not another speculative feature list.
The approved deliverables are contributor instructions, this audit, a work plan
and issue updates. **No runtime, dependency-manifest or workflow behavior is
changed by this documentation change.**

Alternatives considered:

| Approach | Trade-off |
| --- | --- |
| Evidence-first audit (selected) | Reproductions and bounded local measurements establish priorities without bundling unreviewed fixes. |
| Documentation inventory only | Faster, but cannot distinguish advertised capabilities from working behavior. |
| Implement fixes during the audit | Could repair immediate defects, but mixes discovery, API decisions and release risk before agreeing on the work. |

Recommendation: make valid, repeatable images before optimizing their delivery.
Local cache identity, verified atomic blob publication and a hermetic regression
gate are prerequisites for remote cache or concurrent transfer work.

## Repository baseline and validation

The last main-branch commit was November 21, 2025; the latest repository release
was v0.3.1 on November 20, 2025. At inspection there were 56 open issues, no open
PRs and no runs returned by the workflow-run listing. That listing does **not**
prove Actions never ran: historical run retention is a separate concern.

Python 3.12.10, Windows 11 x64, uv 0.13.9. The host is shared and uncontrolled;
disk hardware, antivirus activity, OS page-cache state and background workload
were not isolated. No dependencies were installed or upgraded for this audit.

| Check | Result and boundary |
| --- | --- |
| `python -m pytest --collect-only -q` | 46 tests collected. |
| Offline selection in `AGENTS.md` | 42 passed, 4 deselected, 13.75 s. |
| CLI help/dry-run | Working via `python -m pycontainer.cli`; dry-run still performs framework discovery. |
| Reproduction probes | Synthetic files, temporary outputs, mocked base/auth/inventory boundaries; results below. |
| Full registry-pulling suite | Not run; all four exclusions are enumerated in `AGENTS.md`. |
| Runtime import/startup, registry push/pull throughput | Not measured. Docker binary availability was not treated as a running daemon. |
| External plugin install/build, extension compile/lint/test | Not executed; contract mismatches below are source inspection plus direct core API checks. |

The current tests are useful but shallow in critical places: the dependency test
checks that a layer exists, the reproducibility test checks a boolean, and layout
tests do not validate rootfs DiffIDs or start the image.

## Measured performance

### Method

Three samples per case. Input generation occurs outside the timed interval;
each sample times `ImageBuilder(BuildConfig(...))` plus `_create_app_layer`.
This includes framework discovery, file traversal, tar creation, hash/read,
rename and cache work where applicable. Every output directory starts fresh.
These are **app-layer-only** microbenchmarks, not complete image builds.

Large-file fixtures contain 32 or 128 MiB of repeated `x` bytes in one binary
file. Caching is disabled. `tracemalloc` starts immediately before builder
construction and stops after layer creation. Small-file fixtures contain
1,000 `.py` files of 1,024 `x` bytes each. A new explicit cache directory is
used for each repetition; fill precedes hit, and a hit copies to a fresh output.
The small-file timing samples do not enable `tracemalloc`.

No OS cache flush, process isolation, compression, base pull, SBOM, push or
runtime startup is included. Repeated-byte fixtures do not characterize
compression ratios or production source distributions.

| Fixture / mode | Median wall time | Median Python-tracked peak | Tar bytes |
| --- | ---: | ---: | ---: |
| 32 MiB file, uncached | 0.1485 s | 32.014 MiB | 33,556,480 |
| 128 MiB file, uncached | 0.4829 s | 128.020 MiB | 134,225,920 |
| 1,000 x 1 KiB files, uncached | 1.6538 s | Not measured | 1,546,240 |
| Same small files, cache fill | 1.9303 s | Not measured | 1,546,240 |
| Same small files, cache hit into fresh output | 1.2726 s | Not measured | 1,546,240 |

Raw wall-time samples in seconds, in execution order:

```text
32 MiB uncached:  0.1484905, 0.1399572, 0.1635305
128 MiB uncached: 0.5165441, 0.4829017, 0.4740286
small uncached:   2.9938675, 1.5630144, 1.6537556
small fill:       1.9158949, 2.3483948, 1.9303492
small hit:        1.2726187, 1.5679015, 1.1547607
```

### Interpretation and performance needs

1. **Bound memory first.** App-layer Python allocations scale with tar size.
   Both tar writers call `read_bytes()` after creation
   (`builder.py:181-239`). Stream hashing rather than materializing an entire
   tar. Upload and download paths also read whole blobs
   (`registry_client.py:120-126,164-206`), but their allocations/throughput were
   not benchmarked. Measure RSS separately before assigning a memory budget.
2. **Make cache hits correct before faster.** The explicit cache saves some
   local work in this fixture, but still traverses/stats files, loads/writes its
   JSON index and copies a tar to fresh output. Fill adds overhead here.
   Neither these three samples nor a stale hit justifies a product speed claim.
3. **Reduce discovery/context work.** Constructor framework discovery can scan
   the tree separately for FastAPI and Flask; tar and cache then traverse/stats
   files again. Exclude irrelevant trees and profile phase costs before
   designing an index or watcher.
4. **Instrument network costs before parallelizing.** Pull/push loops are
   sequential; network calls lack explicit timeouts, streaming and verified
   publication. First bound each transfer, then benchmark a small worker limit.
   Do not multiply full-layer memory use by adding unbounded parallelism.
5. **Stabilize dependency reuse.** Dependency tar creation is uncached and not
   normalized like the reproducible app tar. Build importable, target-compatible
   dependencies before splitting their layers or exporting their cache.

Follow-up baselines should include complete cold builds, warm same-output
builds, warm fresh-output builds, no-op rebuilds, app-only edits, dependency
edits, cold/warm push and target runtime startup. Record wall time, peak RSS,
Python allocations, disk bytes, transferred bytes, cache hit/miss reasons and
request counts. Start with trend artifacts; adopt regression budgets only after
stable runners and enough repetitions establish their variance.

## Correctness findings

Paths below are relative to `src/pycontainer/` unless stated otherwise.
Line references describe the audited commit, not a future implementation.

| Priority / issue | Evidence | Required outcome |
| --- | --- | --- |
| P0, #69 OCI rootfs | Mocked one-layer base plus app yields 2 manifest layers but 1 DiffID. `builder.py:57-59`; `oci.py:37-70` never append new DiffIDs. | Preserve base order and append uncompressed app/dependency DiffIDs; validate descriptors and runtime import. |
| P0, #70 cache identity/default | Equal-size edit with preserved timestamp hits old data. A later `/srv` builder receives cached `app/app.py`. Defaults create a disabled cache. `cache.py:20-29,57-87`; `builder.py:24-29`. | Versioned content/config-aware identities, explicit activation, atomic index/blob publication and concurrent-writer/corruption tests. |
| P0, existing #67 auth | Synthetic Docker username/password credentials are returned as a bare bearer token. `auth.py:87-90,141-159`; CLI passes token/username but not password to push (`cli.py:84-87`). | Preserve basic credentials versus bearer tokens end to end; retain existing issue ownership and use synthetic regression fixtures. |
| P0, #71 config precedence | `cli.main --config` replaces file context/platform/dependency/SBOM booleans with defaults. `cli.py:35-55`. | Explicit CLI > file > defaults, normalized keys before merge, CLI-level tests. No new environment config tier is implied. |
| P0, #72 repeat builds | Second uncached app-layer write to the same output raises `FileExistsError` on Windows. Both tar writers use fixed temp names and `rename`. `builder.py:189-197,213-237`. | Verified atomic, idempotent publication; test dependencies too and define shared-output concurrency. |
| P1, #73 context/discovery | Fallback traversal includes `.venv` and `dist`. Repo dry-run selects a test fixture as FastAPI and retains Windows backslashes in a module. `fs_utils.py:6-15`; `framework.py:25-60`. | Shared include/exclude boundaries, deduplication and correct app/module discovery; profile scan-only work. |
| P1, #74 dependencies | Requirements-only discovery returns just `requirements.txt`; Unix venv paths remain nested under `/app/.venv/lib/...`; Windows `Lib` is not discovered. `project.py:37-57`; `builder.py:181-199`. | Resolve/stage actual importable target packages, handle ABI compatibility and normalize tar metadata. No runtime import was tested in this audit. |
| P1, #75 streaming/integrity | Whole-file/blob reads, no explicit request timeouts or expected blob digest/size validation. Existing output blobs are trusted by existence. `registry_client.py:60-89,110-126,164-206`; `builder.py:112-125`. | Bounded streaming, timeouts, safe retries, digest/size checks and interrupted-transfer cleanup before concurrency. |
| P1, #76 SBOM | `--sbom spdx` exits 2; builder always uses default SPDX. Mocked host `pip freeze` adds a package to an empty project's inventory. `cli.py:29`; `builder.py:86-90`; `sbom.py:86-117`. | Align formats across callers; inventory shipped packages, validate schemas, version metadata and reproducibility contract. |
| P1, #77 integrations | Poetry/Hatch use invalid `context_path`; direct core invocation raises `TypeError`. VS Code invokes absent package `__main__`; test script targets absent `out/test/runTest.js`. | Test real plugin registration and subprocess/API contracts; repair scripts without speculative compatibility aliases. |
| P1, #78 base selection | No digest-reference parsing branch; Docker manifest lists not accepted/selected; missing OCI platform falls through to absent config digest. `registry_client.py:146-150,208-235`; `builder.py:94-115`. | Correct digest/tag/port parsing and explicit OS/arch/variant selection/failure; distinct from cross-compilation. |
| P1, #79 distribution | PyPI project JSON returned 404; reusable workflow installs that name while publication is disabled. `.github/workflows/pycontainer-build.yml:71-75`; `release.yml:111-129`. | Immutable verified install source or approved publication, isolated wheel/sdist smoke tests and tested release automation. |

Corrupt cache indexes, writer races, eviction/shared-blob accounting, interrupted
transfers and real registry interoperability were **not** exercised. Source
inspection makes them important test requirements, not measured failures.

## Missing maintenance updates

### CI and supported interpreters

There is no pull-request CI workflow. Tests are attached only to release tags,
on Linux/Python 3.11 and 3.12. Keep the 3.11 declared minimum and verify all
claimed interpreter support; add current stable Python coverage rather than
assuming an open-ended `requires-python` has been tested. Python's official
[version-status table](https://devguide.python.org/versions/) was checked on the
audit date. Current stable is the 3.15 series; expanding compatibility is work,
not a change made here.

### Upstream actions and package manifests

Observed from official GitHub release endpoints and PyPI JSON on 2026-11-19:

| Component | Repository declaration/use | Latest observed upstream |
| --- | --- | --- |
| `actions/checkout` | v4 | [v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1) |
| `actions/setup-python` | v5 | [v7.1.0](https://github.com/actions/setup-python/releases/tag/v7.1.0) |
| `actions/upload-artifact` | v4 | [v7.0.2](https://github.com/actions/upload-artifact/releases/tag/v7.0.2) |
| `actions/download-artifact` | v4 | [v8.0.2](https://github.com/actions/download-artifact/releases/tag/v8.0.2) |
| `softprops/action-gh-release` | v1 | [v3.0.4](https://github.com/softprops/action-gh-release/releases/tag/v3.0.4) |
| setuptools | `>=61.0`, not a lock | 84.2.1 |
| pytest | Release installs unbounded `pytest` | 9.2.0 |
| Poetry | Plugin `>=1.2.0` | 2.5.1 |
| hatchling | Plugin `>=1.18.0` | 1.33.0 |

These are dated observations, **not instructions to blind-upgrade**. Broad
minimums can already resolve newer releases; no installed plugin/build-tool
versions were established. Review major-version API/runner requirements,
test migrations and pin reviewed action SHAs. Add automated update PRs and
reproducible development tooling. Core runtime dependencies remain empty.

The VS Code manifest declares TypeScript 5, ESLint 8 and typescript-eslint 6
ranges, but has no tracked lock or working test entrypoint. Its resolved
versions were not audited or installed. Repair its test/lint contract and
assess current compatible tooling together, not via a guessed bulk update.

### Distribution and documentation drift

- PyPI's `pycontainer-build` JSON endpoint returned 404. This does not establish
  why the name is absent or guarantee its future availability. Do not advertise
  the reusable workflow's unverified `pip install` path as working.
- No root license file or project license metadata is tracked; extension/plugin
  metadata claims MIT. The owner must confirm intended licensing; this audit
  does not invent a license.
- Root agent guidance was absent. The old Copilot instructions claimed no tests
  or docs, Python 3.10 support and obsolete output. They are replaced by a pointer
  to the canonical `AGENTS.md`.
- README/API examples use invalid `context_path`, format-valued `--sbom`, and
  stale phase/push claims. `docs/TESTING.md` describes unimplemented directory
  structure and tooling. Update those alongside owning fixes, rather than
  claiming they work now.
- The roadmap links multi-stage builds to #20; the actual multi-stage issue is
  #22. Multiple category/leaf issues repeat the same proposed work.

## Design for follow-up implementation

Preserve the current stdlib-only pipeline and narrow module boundaries.
Discover a bounded source set; resolve validated target dependencies; compute
configuration-aware cache identities; stage reproducible layers; atomically
publish verified content-addressed blobs; assemble consistent OCI config and
manifest metadata; generate inventory from staged contents; push verified
blobs before publishing the manifest.

Error paths must identify unsupported platforms/configuration, invalid
credentials, corrupt data and transfer timeouts. Do not silently downgrade
errors into a successful-looking build or cache hit. Corrupt optional cache
data may be rebuilt only with explicit diagnostics and safe ownership/cleanup.

Tests should progress from hermetic regression fixtures to isolated packaging
and plugin smoke tests, then bounded real registry/runtime checks. Do not
substitute mock success for the final import/startup gate.

A separate rubber-duck critique of the plan reinforced: correctness before
remote/parallel work; context boundaries before dependency staging; explicit
Windows repeat-build handling; cache corruption/concurrency coverage; and
clear separation of microbenchmark allocations from end-to-end RSS.
Its suggestions for new environment precedence or constructor aliases were
not adopted because they expand scope unnecessarily.

## Tracking and references

See [the execution plan](2026-11-19-maintenance-plan.md) and roadmap
`spboyer/pycontainer-build#66`. New implementation issues are #68-#79;
documentation delivery is #80. Existing auth #67, lockfiles #59, remote cache
#61 and cross-compilation #63 remain open.

Normative OCI references:
[image config](https://github.com/opencontainers/image-spec/blob/main/config.md),
[layer format](https://github.com/opencontainers/image-spec/blob/main/layer.md).
Version observations use official
[PyPI JSON](https://docs.pypi.org/api/json/) endpoints for each named package;
the repository release and issue state were read through GitHub APIs.
