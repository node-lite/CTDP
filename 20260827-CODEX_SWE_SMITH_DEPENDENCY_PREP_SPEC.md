# Codex Task: Build a Cross-Package-Manager Dependency Preparation System for SWE-smith

## 0. Objective

Build a dependency preparation subsystem for NodeLite that can preprocess a fixed set of SWE-smith JavaScript/TypeScript RepoProfiles before task execution.

The input validation set is:

```text
swe_smith_64_project_ids.txt
```

It contains exactly 64 SWE-smith project/profile IDs. Treat this file as the source of truth for the validation workload. Do not replace it with a different SWE-smith subset.

The system must:

1. Discover the exact environment information for every RepoProfile.
2. Decide whether an existing lockfile is authoritative or whether the package manager must resolve dependencies.
3. Produce a final resolved lockfile or equivalent resolved dependency state for every environment.
4. Normalize npm, pnpm, Yarn Classic, Yarn Berry, and Bun dependency information into one NodeLite manifest format.
5. Deduplicate all network package artifacts globally.
6. Prefetch each unique artifact once into a global content-addressed store.
7. Materialize/warm package-manager-native caches from that global store without manually reimplementing native cache formats.
8. Validate that later task installs can reuse the prepared artifacts and minimize or eliminate external network access.
9. Produce machine-readable reports and human-readable acceptance summaries.

The system is a preparation pipeline. Do **not** attempt to replace npm/pnpm/Yarn/Bun dependency resolution algorithms.

---

## 1. Core Design Principles

### 1.1 Separate dependency resolution from artifact storage

Package-manager version matters for resolution, but it should not be part of the identity of an immutable downloaded package artifact.

```text
pnpm 9 ─┐
npm 10 ─┼─> lodash@4.17.21 ─> one immutable tarball/content hash
Yarn 1 ─┤
Yarn 4 ─┤
Bun    ─┘
```

Store the raw artifact once. Use the package manager only to determine what exact artifacts are needed and to build its own native cache/view.

### 1.2 Existing lockfiles are inputs, not always absolute truth

Treat a lockfile as authoritative when the SWE-smith environment uses a strict install mode such as:

```text
npm ci
pnpm install --frozen-lockfile
yarn install --frozen-lockfile
yarn install --immutable
```

Revalidate or resolve when the environment:

```text
uses --no-frozen-lockfile
modifies package.json before install
modifies workspace manifests before install
has no lockfile
uses an install command that may update the lockfile
```

Do not blindly regenerate all lockfiles. Do not blindly trust all existing lockfiles.

### 1.3 Never manually synthesize package-manager cache internals

Do not reverse-engineer and manually generate:

```text
npm _cacache
pnpm store
Yarn cache
Bun cache
```

Instead:

1. Put immutable raw artifacts in the NodeLite global CAS.
2. Expose them through a local artifact/registry layer or another manager-compatible local source.
3. Invoke the real package manager to warm/materialize its native cache.

### 1.4 Do not equate "lockfile packages" with "all environment downloads"

Distinguish:

```text
registry packages
git dependencies
HTTP tarballs
workspace/local dependencies
patches
optional/platform-specific dependencies
git submodules
postinstall/build-time external downloads
```

A lockfile may fully describe package resolution while still missing external files downloaded by install scripts.

---

## 2. Input

Primary input:

```text
swe_smith_64_project_ids.txt
```

Each line is a stable external profile ID, for example:

```text
swesmith/axios__axios.ef36347f
swesmith/GitbookIO__gitbook.81f8ddcf
swesmith/trpc__trpc.2f40ba93
```

Parse all 64 non-empty lines and preserve the original line as `profile_id`.

Do not infer a full commit only from the short suffix. Resolve the exact RepoProfile from the official SWE-smith profile registry/source and use its exact full commit.

Inspect the official SWE-smith JavaScript and TypeScript profile definitions and their environment/Dockerfile definitions to recover:

```text
owner
repo
exact commit
Node version
package manager
package-manager version when explicit
install workdir(s)
install command(s)
manifest edits
lockfile-related flags
```

---

## 3. Pipeline

Implement these seven stages:

```text
RepoProfile IDs
      |
      v
1. Environment Discovery
      |
      v
2. Lockfile Decision + Native Resolution
      |
      v
3. Final Resolved Lockfile / Resolution State
      |
      v
4. Normalize to NodeLite Dependency Manifest
      |
      v
5. Global Union + Dedup
      |
      v
6. Global Prefetch -> Raw Artifact CAS
      |
      v
7. Native Cache Warmup + Dynamic Validation
```

Each stage must be independently runnable, idempotent, and resumable.

---

## 4. Stage 1: Environment Discovery

For each of the 64 profile IDs, emit at least:

```json
{
  "profile_id": "swesmith/trpc__trpc.2f40ba93",
  "owner": "trpc",
  "repo": "trpc",
  "commit": "<full commit>",
  "language": "typescript",
  "node_version": "<detected>",
  "package_manager": "pnpm",
  "package_manager_version": "<detected or null>",
  "install_workdirs": ["."],
  "install_commands": ["pnpm install"],
  "lockfiles": ["pnpm-lock.yaml"],
  "manifest_files": ["package.json"],
  "environment_source": "<SWE-smith profile reference>",
  "discovery_evidence": []
}
```

### 4.1 Package-manager detection priority

Use this priority:

1. SWE-smith environment install command.
2. Explicit PM version in SWE-smith profile/Dockerfile.
3. `package.json.packageManager`.
4. Corepack configuration.
5. Lockfile type.
6. PM-specific config files.

Do not classify Bun as a package manager merely because Bun is installed. Require package-manager evidence such as:

```text
bun install
bun.lock / bun.lockb in the actual dependency root
packageManager: bun@...
```

### 4.2 Detect multiple dependency roots

Do not assume repo root is the only install root.

Recognize patterns such as:

```bash
cd client && npm ci
cd frontend && bun install
WORKDIR /repo/apps/react-vite
pnpm --filter ...
```

Store every dependency root.

### 4.3 Package-manager version

Capture exact versions when explicit, for example:

```text
npm install -g pnpm@9.4.0
corepack prepare pnpm@10.28.2 --activate
yarn set version 4.12.0
corepack prepare yarn@4.8.1 --activate
FROM oven/bun:1.3.7
packageManager field
```

If exact version cannot be proven, store `null` and the evidence. Never invent it.

---

## 5. Stage 2: Lockfile Decision and Native Resolution

Classify each dependency root into:

```text
A. authoritative_existing
B. existing_requires_resolution
C. missing_requires_resolution
D. unsupported_or_manual_review
```

Save the reason and evidence.

### 5.1 Mode A: authoritative existing lockfile

Typical evidence:

```text
npm ci
pnpm install --frozen-lockfile
yarn install --frozen-lockfile
yarn install --immutable
```

Behavior:

1. Preserve original lockfile.
2. Do not regenerate it.
3. Copy/hash it into output.
4. Mark:

```json
{
  "resolution_source": "existing_lockfile",
  "lockfile_authoritative": true
}
```

### 5.2 Mode B: existing lockfile but resolution may change

Examples:

```text
pnpm install --no-frozen-lockfile
plain install after package.json is edited
plain install where the PM may update lockfile
```

Behavior:

1. Reproduce all manifest edits performed by SWE-smith before install.
2. Run the native package manager in a temporary checkout.
3. Use resolve-only/lockfile-only where supported.
4. Save the resulting lockfile separately.
5. Diff source and resolved lockfiles.
6. Never modify the canonical checkout.

### 5.3 Mode C: no lockfile

Use the real package manager to resolve once.

#### npm

Prefer:

```bash
npm install --package-lock-only --ignore-scripts
```

#### pnpm

Prefer:

```bash
pnpm install --lockfile-only --ignore-scripts
```

#### Yarn Berry

Prefer a lockfile-update mode that skips link/build for the detected version, e.g. the appropriate `yarn install --mode=update-lockfile` behavior.

#### Bun

Prefer:

```bash
bun install --lockfile-only
```

Use `--ignore-scripts` only if the detected Bun version supports it.

#### Yarn Classic v1

Do not implement a custom resolver.

- Existing valid `yarn.lock`: use it.
- No `yarn.lock`: use a temporary checkout, invoke real Yarn v1 with scripts disabled if possible, save generated `yarn.lock`, then remove generated `node_modules`.
- Record that this fallback performed more work than pure resolve-only.

### 5.4 Preserve exact environment context

Resolution should use, as far as practical:

```text
exact repo commit
same PM family
same PM version when known
compatible Node version
same dependency root
same manifest edits as SWE-smith
relevant .npmrc/.yarnrc.yml/pnpm config
```

For every resolution record:

```text
command
tool versions
elapsed time
exit code
stdout/stderr paths
whether package scripts ran
whether network metadata was accessed
```

---

## 6. Stage 3: Final Resolved Lockfile

Recommended layout:

```text
out/
  projects/
    <safe-profile-id>/
      discovery.json
      resolution.json
      source-lockfiles/
      resolved-lockfiles/
      logs/
```

Example `resolution.json`:

```json
{
  "profile_id": "...",
  "dependency_root": ".",
  "package_manager": "pnpm",
  "package_manager_version": "9.4.0",
  "resolution_mode": "existing_requires_resolution",
  "resolution_source": "native_resolver",
  "source_lockfile": "pnpm-lock.yaml",
  "resolved_lockfile": "resolved-lockfiles/pnpm-lock.yaml",
  "source_lockfile_sha256": "...",
  "resolved_lockfile_sha256": "...",
  "lockfile_changed": true,
  "resolve_elapsed_ms": 1234,
  "exit_code": 0
}
```

A profile with multiple dependency roots gets multiple resolution records.

---

## 7. Stage 4: Normalize to a NodeLite Manifest

Create package-manager-specific adapters that emit one normalized format so downstream code does not repeatedly parse five lockfile formats.

Example:

```json
{
  "profile_id": "swesmith/GitbookIO__gitbook.81f8ddcf",
  "dependency_root": ".",
  "platform": {
    "os": "linux",
    "arch": "x64"
  },
  "artifacts": [
    {
      "type": "registry",
      "name": "lodash",
      "version": "4.17.21",
      "source": "https://registry.npmjs.org/",
      "resolved_url": "...",
      "integrity": "sha512-...",
      "optional": false
    },
    {
      "type": "workspace",
      "name": "@example/local-package",
      "path": "packages/local-package"
    },
    {
      "type": "git",
      "url": "https://github.com/owner/repo.git",
      "commit": "<full commit>"
    }
  ]
}
```

Required artifact types:

```text
registry
git
http_tarball
workspace
local_file
patch
unknown
```

Rules:

- Do not download `workspace:` / `link:` / local-only entries.
- Do not silently drop unknown protocols.
- Put unsupported entries in `manual_review`.

---

## 8. Stage 5: Global Union and Dedup

Create:

```text
out/global/global_manifest.json
out/global/artifact_index.json
out/reports/dedup.json
```

### 8.1 Artifact identity

Prefer immutable content integrity/hash as the CAS identity.

Bad:

```text
pnpm9/lodash@4.17.21
npm10/lodash@4.17.21
yarn1/lodash@4.17.21
```

Good:

```text
cas/sha512/<digest>
```

If integrity is unknown before download, use a temporary ID, hash after download, and atomically move to the final content-addressed path.

### 8.2 Dedup report

Report:

```text
total dependency references
unique logical package versions
unique immutable artifacts
duplicate references eliminated
bytes before global dedup
bytes after global dedup
dedup ratio
```

Distinguish measured byte counts from estimates.

---

## 9. Stage 6: Global Prefetch

Download each unique network artifact once.

Suggested layout:

```text
out/cas/
  blobs/
    sha512/
      <digest>
    sha256/
      <digest>
  metadata/
    <artifact-id>.json
```

Metadata:

```json
{
  "artifact_type": "registry",
  "name": "lodash",
  "version": "4.17.21",
  "source_url": "...",
  "integrity": "sha512-...",
  "content_sha256": "...",
  "size_bytes": 123456,
  "downloaded_at": "...",
  "referenced_by": ["profile A", "profile B"]
}
```

Requirements:

- atomic writes
- integrity verification
- content hashing
- bounded parallelism
- retry/backoff
- no corrupt cache hits
- concurrent duplicate fetches coalesce into one physical download
- second identical prefetch downloads 0 bytes for already-valid artifacts

The raw CAS must be package-manager-neutral.

---

## 10. Stage 7: Native Cache Warmup

Optional native cache roots:

```text
out/native-cache/
  npm/
  pnpm/
  yarn-v1/
  yarn-berry/
  bun/
```

Do not manually synthesize internal cache formats.

Invoke the real package managers and let them create native cache/store state while obtaining already-prefetched artifacts from the local NodeLite artifact/registry layer.

If needed, implement a thin local npm-compatible registry/artifact service backed by the CAS.

Preparation mode may fall back to upstream, but every upstream fallback must be logged.

After prefetch, an expected registry package must not be externally downloaded again.

---

## 11. Dynamic Warmup and External Artifact Discovery

Lockfiles do not necessarily capture:

```text
postinstall binary downloads
Electron/browser downloads
native prebuilt binaries
git submodules
arbitrary install-script HTTP fetches
```

Add a validation pass:

```text
prepared CAS
   |
   v
run original package-manager install semantics
   |
   v
observe outbound requests
   |
   v
record unexpected misses
   |
   v
capture supported immutable external artifacts
   |
   v
repeat validation
```

Save external artifacts separately:

```text
out/cas/external/
```

If full generic HTTPS capture is out of MVP scope, at minimum detect and report:

```text
URL/domain
process/profile
failure category
```

and classify the profile as:

```text
external_artifact_miss
```

Do not erase successful static-resolution/CAS results because full offline validation failed.

---

## 12. CLI

Provide one CLI with independently runnable stages, e.g.:

```bash
nodelite-deps discover   --ids swe_smith_64_project_ids.txt   --out out/

nodelite-deps resolve --out out/

nodelite-deps normalize --out out/

nodelite-deps aggregate --out out/

nodelite-deps prefetch --out out/ --jobs 16

nodelite-deps warm-cache --out out/

nodelite-deps validate --out out/

nodelite-deps all   --ids swe_smith_64_project_ids.txt   --out out/   --jobs 16
```

Use the repository's existing naming/style if there is already a CLI framework.

Every stage must:

```text
be idempotent
be resumable
avoid redoing unchanged successful work
support --force
emit structured logs
return non-zero for unhandled failures
```

---

## 13. State and Fingerprinting

Fingerprint at least:

```text
profile ID
repo
exact commit
dependency root
manifest hashes
source lockfile hashes
PM family
PM version when known
Node version when relevant
relevant config hashes
SWE-smith manifest transformations
```

Do not rerun resolution if the fingerprint is unchanged and prior output is valid.

---

## 14. Safety and Reproducibility

- Never edit the input ID file.
- Never modify canonical source checkouts in place.
- Resolve in temporary worktrees/checkouts.
- Do not commit generated lockfiles.
- Do not execute package scripts during static resolution unless unavoidable.
- Record whenever scripts are executed.
- Use timeouts and bounded concurrency.
- Preserve failed logs.
- Never silently skip unsupported profiles or protocols.
- Preserve source and resolved lockfiles separately.
- Record every heuristic with evidence.

---

## 15. Required Reports

Generate:

```text
out/reports/summary.json
out/reports/summary.md
out/reports/projects.csv
out/reports/resolution.csv
out/reports/artifacts.csv
out/reports/dedup.json
out/reports/failures.json
out/reports/manual_review.json
```

`summary.md` must contain:

```text
input profile count
discovered profile count
failed discovery count

package-manager distribution
package-manager version distribution

authoritative existing lockfile count
existing lockfile requiring re-resolution count
missing lockfile count
manual-review count

resolution success/failure counts
total resolution time
P50/P95/max resolution time

total dependency references
unique logical package versions
unique immutable artifacts
total raw artifact bytes
dedup ratio

prefetch success/failure counts
CAS integrity failures

native cache warmup success/failure by PM
native cache bytes by PM

dynamic validation results
profiles with unexpected external downloads
```

Do not hard-code PM counts. Derive them from the 64 profiles.

---

## 16. Tests

Add unit tests for:

```text
npm package-lock parsing
pnpm lockfile parsing
Yarn v1 lock parsing
Yarn Berry lock parsing
Bun lock parsing
workspace:/link:/file: classification
git dependency classification
HTTP tarball classification
integrity verification
CAS dedup
concurrent duplicate-download coalescing
environment fingerprint changes
lockfile authority decision logic
multiple dependency roots
```

If a PM family is absent from the 64-profile set, include a small fixture for it.

Also add small integration tests independent of the full 64-profile acceptance run.

---

# 17. Acceptance Procedure

After implementation, perform the following acceptance.

## Acceptance A: Input and Discovery

```bash
nodelite-deps discover   --ids swe_smith_64_project_ids.txt   --out acceptance-out/
```

PASS:

```text
64 non-empty input IDs
64 unique IDs
64/64 mapped to an official SWE-smith RepoProfile
64/64 have exact owner/repo/full commit
64/64 have package-manager evidence
64/64 have install command/workdir evidence
0 silently skipped profiles
```

Any undiscovered profile fails this acceptance stage.

## Acceptance B: Lockfile Classification

Run resolution.

PASS:

```text
every dependency root classified
every classification has evidence
source lockfiles preserved
generated/resolved lockfiles stored separately
strict/frozen/immutable/ci environments are not unnecessarily rewritten
original repo checkouts remain clean
```

Print counts for:

```text
authoritative_existing
existing_requires_resolution
missing_requires_resolution
manual_review
```

Do not hard-code expected counts.

## Acceptance C: Native Resolution

PASS:

```text
all non-authoritative roots either resolve successfully
or appear explicitly in failures/manual_review

no profile disappears from reports
resolve elapsed time recorded
PM and PM version used recorded
```

For Yarn v1 without lockfile, verify the fallback generates `yarn.lock` only in a temporary checkout and cleans generated `node_modules`.

## Acceptance D: Normalized Manifest

PASS:

```text
every successfully resolved dependency root has a normalized manifest
registry entries retain name/version/source/integrity when available
workspace/local entries are not treated as network downloads
unknown protocols are reported rather than dropped
```

## Acceptance E: Global Dedup and CAS

```bash
nodelite-deps aggregate --out acceptance-out/
nodelite-deps prefetch --out acceptance-out/ --jobs 16
```

PASS:

```text
same immutable artifact referenced by multiple profiles is stored once
same immutable artifact referenced by different PM families is stored once when such overlap exists
all CAS blobs pass integrity/content-hash validation
concurrent duplicate requests produce one physical blob
second prefetch run downloads 0 bytes for already-valid artifacts
```

Show one concrete cross-profile dedup example if present.

Show one concrete cross-PM dedup example if present.

Do not fabricate examples if absent.

## Acceptance F: Native Cache Warmup

```bash
nodelite-deps warm-cache --out acceptance-out/
```

PASS:

```text
native caches are created by real package managers
cache internals are not manually synthesized
cache roots are shared per PM family/version policy, not per task
cache byte sizes are reported
prefetched local artifacts are reused where supported
unexpected upstream fallbacks are logged
```

A second warm-cache run should show no unnecessary repeated downloads for unchanged inputs.

## Acceptance G: Offline / Network-Miss Validation

```bash
nodelite-deps validate --out acceptance-out/
```

Run two modes.

### G1. Registry-package offline validation

Disable upstream registry access but allow the local prepared artifact service.

PASS:

```text
all prefetched registry artifacts are served locally
zero expected registry package artifacts are redownloaded from the Internet
```

### G2. Full install validation

Attempt SWE-smith install semantics while observing outbound requests.

A profile passes when:

```text
install succeeds
and
no untracked external artifact download occurs
```

Otherwise explicitly classify:

```text
external_artifact_miss
native_or_system_dependency_failure
other_failure
```

Report all counts.

---

## 18. Performance Acceptance

For each profile/root measure:

```text
discovery time
resolution time
normalization time
new-artifact prefetch time
native-cache warmup time
validation time
```

Report resolution:

```text
P50
P95
max
total
```

Also measure:

```text
first-run bytes downloaded from Internet
second identical run bytes downloaded from Internet
```

Expected property:

```text
second identical prefetch run -> 0 bytes for already-valid CAS artifacts
```

Use measured values only.

---

## 19. Definition of Done

The feature is done when:

1. All 64 IDs are accounted for.
2. No profile is silently skipped.
3. Exact SWE-smith environment metadata is discovered.
4. Lockfile authority is explicitly classified.
5. Missing/stale lockfiles are resolved with the real PM.
6. Yarn v1 works without a custom resolver.
7. All successful resolutions are normalized into one NodeLite schema.
8. Immutable artifacts are globally deduplicated.
9. CAS integrity is verified.
10. Native PM caches are created by the real PMs.
11. Re-running unchanged preparation reuses prior work.
12. Offline/network-miss validation classifies every profile.
13. Human- and machine-readable reports are generated.
14. Unit and integration tests pass.
15. Documentation contains the exact 64-profile acceptance commands.

---

## 20. What Codex Must Return After Implementation

Do not only say "implemented".

Return:

```text
1. Files/modules added or changed.
2. Architecture summary.
3. Exact commands used for the 64-profile acceptance run.
4. Discovered/resolved/failed/manual-review profile counts.
5. Package-manager distribution found.
6. Lockfile classification counts.
7. Resolve P50/P95/max/total time.
8. Total dependency references and unique artifacts.
9. Raw CAS size and dedup ratio.
10. Native cache sizes by PM.
11. First-run vs second-run Internet download bytes.
12. Offline validation results.
13. external_artifact_miss profile list.
14. Remaining limitations/TODOs.
```

Include paths to:

```text
acceptance-out/reports/summary.md
acceptance-out/reports/summary.json
acceptance-out/reports/failures.json
acceptance-out/reports/manual_review.json
```

Do not hide partial failures. Every one of the 64 input profiles must be explicitly accounted for.
