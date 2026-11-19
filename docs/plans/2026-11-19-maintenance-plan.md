# Maintenance execution plan

Observed: **2026-11-19**. Baseline: `628f623`.
Status: planned, **not implemented**. Roadmap: `spboyer/pycontainer-build#66`.
Evidence and methods: [performance audit/design](2026-11-19-performance-maintenance-design.md).

## Outcomes and delivery rules

1. Produce valid, runnable, repeatable images with explicit failure modes.
2. Establish hermetic PR feedback and truthful benchmark/runtime evidence.
3. Bound memory and transfer behavior before adding remote cache or parallelism.
4. Restore supported integration/install paths before publishing a new release.

Each issue owns a small reviewed fix and focused before/after regression tests.
Independent foundation fixes may run in parallel after the CI baseline.
Use dependency-ordered PR stacks where a change consumes another's contract;
do not manufacture dependencies merely to put unrelated fixes in a stack.
The audit documentation PR closes only #80, not any implementation issues.

## P0: release-blocking correctness and validation

| Work | Issue | Prerequisites | Completion evidence |
| --- | --- | --- | --- |
| Offline PR CI and performance harness baseline | #68 | None | Linux/Windows hermetic suite, declared minimum/current interpreter policy, explicit network-test separation, repeatable trend artifacts. |
| Correct ordered OCI DiffIDs | #69 | #68 | Manifest/rootfs consistency for base+deps+app, compressed/uncompressed digest fixtures, opt-in runtime validation. |
| Local cache identity, activation and atomic index handling | #70 | #68 | Equal-size/time edits miss; workdir/settings differ; defaults truthful; corruption, concurrent-writer and eviction tests. |
| Preserve configuration-file values | #71 | #68 | Actual CLI tests prove omitted args do not override file values; explicit overrides do. |
| Atomic repeat-build publication on Windows | #72 | #68 | Same-output cached/uncached app/dependency rebuilds pass on Linux/Windows; interruption and ownership tested. |
| Basic credentials versus bearer tokens | Existing #67 | None; use #68 gate before merge | CLI/environment/Docker-config flows preserve username/password, challenge exchange works with synthetic HTTP fixtures, credentials not logged. |

CI is a verification prerequisite for merging changes, not a reason to block
writing regressions or investigating the independent P0 defects.

## P1: useful runtime behavior and bounded resource use

| Work | Issue | Prerequisites | Completion evidence |
| --- | --- | --- | --- |
| Context and framework discovery boundaries | #73 | #68 | No output/venv/test false positives by default; flat and src app layouts; portable dotted module names; scan profile. |
| Importable target dependency staging | #74 | #68, #69, #73 | Requirements resolve/install or fail explicitly; correct target import prefix and ABI; real target imports; reproducible dependency tar. |
| Streaming tar/blob I/O and transfer integrity | #75 | #68, #69, #70, #72 | Bounded-chunk hashing/transfers; measured RSS; verified atomic downloads; timeout/retry/interruption fixtures. |
| SBOM CLI formats and shipped inventory | #76 | #68, #71, #74 | Supported format arguments match all callers; schema validation; no host inventory leakage; correct tool metadata. |
| Poetry/Hatch/VS Code contracts | #77 | #68, #71 | Real supported plugin registration, valid BuildConfig usage and subprocess entrypoint; working extension checks. |
| Digest references and platform-selection errors | #78 | #68, #69 | Tags/digests/ports, OCI index/Docker list and missing-platform tests; exact target selection. |
| Installation, packaging, release/action refresh | #79 | #68-#78 and existing #67 before release | Isolated wheel/sdist install; verified workflow install provenance; reviewed/pinned action upgrades; owner-confirmed license; runtime release gates. |

#75 implements shared blob verification/publication; #78 should consume it
rather than introducing a second transport implementation. Their parsing and
streaming changes can be developed independently against agreed interfaces.
The install/action investigation in #79 can also begin early; the dependencies
are final release-readiness gates, not a ban on preparatory work.

## P2: build on validated foundations

| Existing work to reuse | Canonical issue | Gate / scope adjustment |
| --- | --- | --- |
| Lockfile-backed dependency resolution | #59 | After #74. Resolve from a supported lock format into the same validated staging/inventory pipeline; include target selection from #78. |
| Remote/shared cache | #61 | After #70, #72, #75 and benchmark harness #68. Start with one validated backend and explicit integrity/auth/eviction contracts. |
| Dependency layer splitting | #33 | After #74 and cache correctness #70. Prove incremental benefit with dependency-change benchmarks; repair its self-parent link. |
| Cross-compilation | #63 | After #74 and #78. Distinguish wheel target selection, source compilation and emulation; demonstrate native-package runtime behavior. |
| Multi-platform index | #29 | After #69, #74 and #78; unsupported targets fail explicitly. Native compilation support is needed only for targets that require it. |
| Bounded parallel transfer | Parent #49, implementation to scope after #75 | Benchmark sequential versus small worker counts with RSS/byte/request metrics; no fixed speedup claim. |
| Multi-stage builds | #22 | Revisit after actual runtime staging works; roadmap previously pointed incorrectly to #20. |

Signing, vulnerability scanning, provenance, migration tools and additional
platform integrations remain existing roadmap work, not silently canceled
or promoted ahead of the measured foundation gaps.

## Duplicate candidates and canonical routing

Preserve discussions and compare remaining scope before closing anything.
No issue was closed as part of this audit.

| Canonical issue | Candidate duplicate(s) |
| --- | --- |
| #47 platform parent | #27 |
| #48 dependency parent | #30 |
| #49 performance parent | #35 |
| #50 tooling parent | #37 |
| #51 configuration parent | #36 |
| #52 observability parent | #39 |
| #53 standards parent | #38 |
| #54 user-experience parent | #46 |
| #55 testing parent | #43 |
| #56 Python parent | #44 |
| #57 registry parent | #41 |
| #58 CI/CD parent | #42 |
| #59 lockfiles | #32 |
| #61 remote cache | #34 |
| #62 Dockerfile import | #40 |
| #63 cross-compilation | #28 |
| #64 system packages | #31 |
| #60 multi-tag | #45 |

Original bodies and comments stay available. New issue bodies provide evidence,
acceptance checklists and dependencies. Roadmap #66 provides the current sequence;
GitHub issue references and this dated plan must be kept consistent as work lands.
