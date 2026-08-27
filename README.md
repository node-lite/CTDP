# NodeLite Cross-Package-Manager Dependency Preparation

`nodelite-deps` prepares the fixed SWE-smith validation set in
`swe_smith_64_project_ids.txt`. It discovers the official profile and
environment at exact commits, classifies lockfile authority, invokes native
resolvers when required, normalizes npm/pnpm/Yarn Classic/Yarn Berry/Bun
lockfiles, unions artifacts into a package-manager-neutral CAS, and records
native-cache and install-validation results.

## Install and run

The repository wrapper works without installing the package:

```bash
./nodelite-deps discover --ids swe_smith_64_project_ids.txt --out acceptance-out
./nodelite-deps resolve --out acceptance-out
./nodelite-deps normalize --out acceptance-out
./nodelite-deps aggregate --out acceptance-out
./nodelite-deps prefetch --out acceptance-out --jobs 16
./nodelite-deps warm-cache --out acceptance-out
./nodelite-deps validate --out acceptance-out
```

The equivalent one-shot command is:

```bash
./nodelite-deps all --ids swe_smith_64_project_ids.txt --out acceptance-out --jobs 16
```

Every stage accepts `--force`, writes JSONL events under `out/logs/`, and
stores its fingerprint and status under `out/state/`. Successful unchanged
stages are reused; partial stages retain their successful work and retry only
what remains where the package-manager API permits it.

## Output layout

Each profile is stored below `out/projects/<safe-profile-id>/` with its
discovery record, immutable source files, source lockfiles, resolved lockfiles,
normalized manifests, and resolver logs. Global artifacts are written to
`out/cas/` and indexed by `out/global/artifact_index.json`. Required reports
are generated under `out/reports/`:

- `summary.json` and `summary.md`
- `projects.csv`, `resolution.csv`, and `artifacts.csv`
- `dedup.json`, `failures.json`, and `manual_review.json`

`summary.json` contains measured first/second prefetch bytes, lockfile
classification, per-stage timing summaries, native-cache status by package
manager, and dynamic validation counts. `summary.md` is the human-readable
acceptance handoff.

## Native tool availability

Resolution and validation use the real package-manager executable whenever it
is installed. The current acceptance host provides npm but may not provide
pnpm, Yarn, Bun, Corepack, or Docker. Missing tools and unavailable local
artifact sources are recorded explicitly in `warm-cache.json`,
`validation.json`, and the reports; they are never silently treated as
successful installs. Git, workspace, local-file, patch, and unknown protocols
remain typed records and are listed for manual review instead of being fetched
as ordinary registry tarballs.

## Tests

Run the unit and integration fixtures with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```
