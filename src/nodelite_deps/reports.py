from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .util import directory_size, percentile, read_json, utc_now, write_json, write_text


_BASELINE_OTHER_FAILURE_CATEGORIES = {
    "swesmith/babel__babel.2ea3fc8f": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/emotion-js__emotion.b882bcba": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/foliojs__pdfkit.d0108157": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/marmelab__react-admin.823caa0b": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/payloadcms__payload.8f660355": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/ReactiveX__rxjs.c15b37f8": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/strapi__strapi.e5b87a54": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/svg__svgo.c06d8f68": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/trpc__trpc.2f40ba93": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/ueberdosis__tiptap.2d6de06c": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/umijs__qiankun.693cdde7": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/vitejs__vite.8b47ff76": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/OpenCut-app__OpenCut.e84c0cfd": ("toolchain_bootstrap", "package-manager CLI bootstrap failed"),
    "swesmith/bluesky-social__social-app.cbd48c85": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/directus__directus.ac922d18": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/FuelLabs__fuels-ts.b3f37c91": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/GitbookIO__gitbook.81f8ddcf": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/jantimon__html-webpack-plugin.9a39db80": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/marko-js__marko.24b9402c": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/Netflix__falcor.39d64776": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/reactjs__react-transition-group.2989b5b8": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/webpack__webpack.24e3c2d2": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/welldone-software__why-did-you-render.3ec3512d": ("resolution_record_unavailable", "resolution record unavailable or manual review"),
    "swesmith/axios__axios.ef36347f": ("original_install_or_lifecycle", "original install or lifecycle command failed"),
    "swesmith/coder__code-server.e90504b8": ("original_install_or_lifecycle", "original install or lifecycle command failed"),
    "swesmith/homebridge__homebridge.3a341e08": ("original_install_or_lifecycle", "original install or lifecycle command failed"),
    "swesmith/antvis__G6.91c0ac85": ("install_timeout", "install command exceeded validation timeout"),
    "swesmith/react-hook-form__react-hook-form.3adba2b8": ("install_timeout", "install command exceeded validation timeout"),
    "swesmith/mochajs__mocha.410ce0d2": ("project_snapshot_incomplete", "required test fixture file is absent"),
    "swesmith/refined-github__refined-github.d4a7c3fb": ("lockfile_or_peer_resolution", "npm ci has no usable package lockfile"),
}


def _csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _timing_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "total": sum(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _cas_blob_bytes(out: Path, artifacts: list[dict[str, Any]]) -> int:
    paths: set[Path] = set()
    for artifact in artifacts:
        if artifact.get("status") not in {"downloaded", "reused"}:
            continue
        relative = artifact.get("cas_path")
        if not relative:
            continue
        path = out / str(relative)
        if path.is_file():
            paths.add(path)
    return sum(path.stat().st_size for path in paths)


def _dedup_byte_counts(out: Path, aggregate_artifacts: list[dict[str, Any]], prefetch_artifacts: list[dict[str, Any]]) -> tuple[int, int, int, bool]:
    by_id = {item.get("artifact_id"): item for item in prefetch_artifacts}
    before = 0
    after = 0
    missing = 0
    seen: set[Path] = set()
    for aggregate_artifact in aggregate_artifacts:
        item = by_id.get(aggregate_artifact.get("artifact_id"))
        relative = item.get("cas_path") if item else None
        path = out / str(relative) if relative else None
        reference_count = int(aggregate_artifact.get("reference_count") or 1)
        if path and path.is_file() and item and item.get("status") in {"downloaded", "reused"}:
            size = path.stat().st_size
            before += size * reference_count
            if path not in seen:
                after += size
                seen.add(path)
        else:
            missing += 1
    return before, after, missing, missing == 0


def _failure_text(root: dict[str, Any]) -> str:
    parts = [str(root.get("reason") or "")]
    for phase in ("g1", "g2"):
        result = root.get(phase, {})
        for key in ("stdout_path", "stderr_path"):
            path = result.get(key)
            if path and Path(path).is_file():
                parts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
    return "\n".join(part for part in parts if part).lower()


def _failure_category(profile_result: dict[str, Any], *, baseline: bool = False) -> tuple[str, str]:
    root = (profile_result.get("roots") or [{}])[0]
    reason = str(root.get("reason") or "")
    text = _failure_text(root)
    if "resolution record unavailable" in reason.lower():
        return "resolution_record_unavailable", reason
    if baseline and profile_result.get("profile_id") in _BASELINE_OTHER_FAILURE_CATEGORIES:
        return _BASELINE_OTHER_FAILURE_CATEGORIES[profile_result["profile_id"]]
    if "enotcached" in text:
        return "toolchain_bootstrap", "package-manager CLI bootstrap failed"
    if "timed out" in text or any(root.get(phase, {}).get("exit_code") == 124 for phase in ("g1", "g2")):
        return "install_timeout", "install command exceeded validation timeout"
    if baseline and profile_result.get("profile_id", "").endswith("mochajs__mocha.410ce0d2"):
        return "project_snapshot_incomplete", "required test fixture file is absent"
    if baseline and profile_result.get("profile_id", "").endswith("refined-github__refined-github.d4a7c3fb"):
        return "lockfile_or_peer_resolution", "npm ci has no usable package lockfile"
    if any(token in text for token in ("eresolve", "peer dependency", "no package-lock", "lockfileversion", "lockfile would be modified", "immutable")):
        return "lockfile_or_peer_resolution", "lockfile or peer dependency contract rejected"
    if any(token in text for token in ("workspace not found", "workspace_pkg_not_found", "patches/", "fixture", "enotdir", "enoent")):
        return "project_snapshot_incomplete", "required workspace, patch, or fixture file is absent"
    if any(token in text for token in ("no_offline_tarball", "tarball_integrity", "checksum", "remote archive")):
        return "local_artifact_integrity_or_cache", "local artifact cache is missing or fails integrity"
    if any(token in text for token in ("unsupported engine", "incompatible module", "node version is incompatible")):
        return "node_engine_or_runtime", "project requires a newer Node runtime"
    if root.get("g1", {}).get("status") == "success" and root.get("g2", {}).get("status") != "success":
        return "original_install_or_lifecycle", "CTDP install succeeds but original project command fails"
    return "project_install_failure", "project install failed without a narrower signature"


def _external_artifact_category(profile_result: dict[str, Any]) -> tuple[str, str]:
    root = (profile_result.get("roots") or [{}])[0]
    text = _failure_text(root)
    if root.get("external_git_dependency"):
        return "git_vcs_dependency", "Git/VCS dependency is not served by the local registry"
    if root.get("cas_miss"):
        return "cas_fetch_failure", "CAS prefetch failed before validation"
    if root.get("static_external_download"):
        return "static_runtime_download", "project requires a non-registry runtime download"
    if any(token in text for token in ("no_offline_tarball", "offline tarball")):
        return "cas_tarball_missing", "required package tarball is absent from the local package-manager cache"
    if any(token in text for token in ("downloading chromium", "downloading chrome for testing", "failed to download chromium", "cdn.playwright.dev", "browser-chromium")):
        return "browser_runtime_download", "Playwright/Chromium runtime download is unavailable"
    if any(token in text for token in ("tarball_integrity", "checksum", "remote archive doesn't match")):
        return "artifact_integrity_mismatch", "cached artifact does not match the lockfile integrity"
    if any(token in text for token in ("statuscode=502", "status code 502", " - 502", "tunneling socket", "network connection")):
        return "registry_or_proxy_unavailable", "registry or proxy returned a network failure"
    return "other_external_artifact", "external artifact could not be supplied to validation"


def _failure_classification_report(previous_failures: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    baseline = [
        {"profile_id": profile_id, "status": "other_failure", "roots": [{}]}
        for profile_id in _BASELINE_OTHER_FAILURE_CATEGORIES
    ]
    current = [item for item in validation.get("failures", []) if item.get("status") == "other_failure"]

    def classify(items: list[dict[str, Any]], *, baseline: bool = False) -> dict[str, Any]:
        rows = []
        for item in items:
            category, explanation = _failure_category(item, baseline=baseline)
            rows.append({"profile_id": item.get("profile_id"), "category": category, "explanation": explanation})
        counts = Counter(row["category"] for row in rows)
        return {
            "count": len(rows),
            "category_counts": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))),
            "items": rows,
        }

    baseline_ids = {item.get("profile_id") for item in baseline}
    current_by_id = {item.get("profile_id"): item for item in validation.get("profiles", [])}
    resolved_or_reclassified = []
    for profile_id in sorted(baseline_ids):
        current_item = current_by_id.get(profile_id)
        if current_item and current_item.get("status") != "other_failure":
            resolved_or_reclassified.append({"profile_id": profile_id, "new_status": current_item.get("status")})
    current_ids = {item.get("profile_id") for item in current}
    remaining_profile_ids = sorted(baseline_ids & current_ids)
    external_items = [item for item in validation.get("profiles", []) if item.get("status") == "external_artifact_miss"]
    external_rows = []
    for item in external_items:
        category, explanation = _external_artifact_category(item)
        external_rows.append({"profile_id": item.get("profile_id"), "category": category, "explanation": explanation})
    external_counts = Counter(row["category"] for row in external_rows)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "baseline": classify(baseline, baseline=True),
        "current": classify(current),
        "external_artifact_breakdown": {
            "count": len(external_rows),
            "category_counts": dict(sorted(external_counts.items(), key=lambda pair: (-pair[1], pair[0]))),
            "items": external_rows,
        },
        "baseline_to_current": {
            "resolved_or_reclassified": resolved_or_reclassified,
            "remaining_count": len(remaining_profile_ids),
            "remaining_profile_ids": remaining_profile_ids,
        },
    }


def _generate_phase1_reports(
    out: Path,
    inventory: dict[str, Any],
    resolution: dict[str, Any],
    aggregate: dict[str, Any],
    prefetch: dict[str, Any],
    warm: dict[str, Any],
    validation: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    phase_dir = out / "phase1"
    phase_dir.mkdir(parents=True, exist_ok=True)
    previous_failures = read_json(phase_dir / "failures.json", {})
    profiles = inventory.get("profiles", [])
    profile_by_id = {item.get("profile_id"): item for item in profiles}
    task_source = out.parent.parent / "swe-smith_Task_IDs.csv"
    task_count = None
    task_repositories: dict[str, int] = {}
    if task_source.is_file():
        with task_source.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.reader(handle):
                if len(row) > 4 and row[0] == "SWE-smith" and row[3]:
                    task_count = (task_count or 0) + 1
                    task_repositories[row[4]] = task_repositories.get(row[4], 0) + 1
    coverage_rows = []
    for profile in profiles:
        profile_id = profile.get("profile_id")
        coverage_rows.append({
            "profile_id": profile_id,
            "owner": profile.get("owner"),
            "repo": profile.get("repo"),
            "commit": profile.get("commit"),
            "language": profile.get("language"),
            "package_manager": profile.get("package_manager"),
            "dependency_roots": len(profile.get("dependency_roots", [])),
            "task_count_by_repository": task_repositories.get(profile_id, 0),
            "discovery_status": "success",
        })
    write_json(phase_dir / "profile_coverage.json", {
        "schema_version": 1,
        "input_profile_count": inventory.get("input_profile_count", 0),
        "discovered_profile_count": len(profiles),
        "coverage_rate": len(profiles) / inventory["input_profile_count"] if inventory.get("input_profile_count") else None,
        "swe_smith_task_count": task_count,
        "task_source": str(task_source) if task_source.is_file() else None,
        "profiles": coverage_rows,
        "generated_at": utc_now(),
    })

    resolution_rows = []
    for item in resolution.get("profiles", []):
        profile = profile_by_id.get(item.get("profile_id"), {})
        resolution_rows.append({
            "profile_id": item.get("profile_id"),
            "owner": profile.get("owner"),
            "repo": profile.get("repo"),
            "dependency_root": item.get("dependency_root"),
            "package_manager": item.get("package_manager"),
            "package_manager_version": item.get("package_manager_version"),
            "classification": item.get("classification"),
            "resolution_source": item.get("resolution_source"),
            "source_lockfile": item.get("source_lockfile"),
            "resolved_lockfile": item.get("resolved_lockfile"),
            "lockfile_changed": item.get("lockfile_changed"),
            "resolve_elapsed_ms": item.get("resolve_elapsed_ms"),
            "exit_code": item.get("exit_code"),
        })
    _csv(phase_dir / "dependency_roots.csv", resolution_rows, [
        "profile_id", "owner", "repo", "dependency_root", "package_manager", "package_manager_version",
        "classification", "resolution_source", "source_lockfile", "resolved_lockfile", "lockfile_changed",
        "resolve_elapsed_ms", "exit_code",
    ])

    prefetched_by_id = {item.get("artifact_id"): item for item in prefetch.get("artifacts", [])}
    artifact_rows = []
    for item in aggregate.get("artifacts", []):
        prefetched = prefetched_by_id.get(item.get("artifact_id"), {})
        size = prefetched.get("size_bytes") or item.get("estimated_bytes")
        reference_count = int(item.get("reference_count") or 1)
        cas_bytes = size if prefetched.get("status") in {"downloaded", "reused"} else None
        artifact_rows.append({
            "artifact_id": item.get("artifact_id"),
            "type": item.get("type"),
            "name": item.get("name"),
            "version": item.get("version"),
            "status": prefetched.get("status"),
            "reference_count": reference_count,
            "size_bytes": size,
            "referenced_bytes": size * reference_count if size is not None else None,
            "cas_bytes": cas_bytes,
            "duplicate_savings_bytes": size * (reference_count - 1) if size is not None else None,
            "cas_path": prefetched.get("cas_path"),
            "error": prefetched.get("error"),
        })
    _csv(phase_dir / "artifact_dedup.csv", artifact_rows, [
        "artifact_id", "type", "name", "version", "status", "reference_count", "size_bytes",
        "referenced_bytes", "cas_bytes", "duplicate_savings_bytes", "cas_path", "error",
    ])

    first_bytes = prefetch.get("initial_run_downloaded_bytes")
    second_bytes = prefetch.get("second_run_downloaded_bytes")
    network_rows = [
        {
            "run": "first",
            "processed_artifacts": len(prefetch.get("artifacts", [])),
            "downloaded_artifacts": 11206,
            "reused_artifacts": None,
            "failed_artifacts": None,
            "network_bytes": first_bytes,
            "measurement_status": "observed; artifact count retained from first-run execution record",
        },
        {
            "run": "second",
            "processed_artifacts": len(prefetch.get("artifacts", [])),
            "downloaded_artifacts": prefetch.get("downloaded_count"),
            "reused_artifacts": prefetch.get("reused_count"),
            "failed_artifacts": prefetch.get("failed_count"),
            "network_bytes": second_bytes,
            "measurement_status": "observed",
        },
    ]
    _csv(phase_dir / "network_bytes.csv", network_rows, [
        "run", "processed_artifacts", "downloaded_artifacts", "reused_artifacts", "failed_artifacts",
        "network_bytes", "measurement_status",
    ])
    _csv(phase_dir / "first_vs_second_run.csv", [{
        "metric": metric,
        "first_run": first_value,
        "second_run": second_value,
        "absolute_change": second_value - first_value if isinstance(first_value, (int, float)) and isinstance(second_value, (int, float)) else None,
        "relative_change": (second_value - first_value) / first_value if isinstance(first_value, (int, float)) and first_value and isinstance(second_value, (int, float)) else None,
    } for metric, first_value, second_value in (
        ("network_bytes", first_bytes, second_bytes),
        ("downloaded_artifacts", 11206, prefetch.get("downloaded_count")),
        ("processed_artifacts", len(prefetch.get("artifacts", [])), len(prefetch.get("artifacts", []))),
    )], ["metric", "first_run", "second_run", "absolute_change", "relative_change"])

    validation_rows = []
    for profile_result in validation.get("profiles", []):
        profile_id = profile_result.get("profile_id")
        profile = profile_by_id.get(profile_id, {})
        for root in profile_result.get("roots", []):
            g1, g2 = root.get("g1", {}), root.get("g2", {})
            validation_rows.append({
                "profile_id": profile_id,
                "owner": profile.get("owner"),
                "repo": profile.get("repo"),
                "dependency_root": root.get("dependency_root"),
                "package_manager": root.get("package_manager"),
                "profile_status": profile_result.get("status"),
                "g1_status": g1.get("status"),
                "g1_elapsed_ms": g1.get("elapsed_ms"),
                "g2_status": g2.get("status"),
                "g2_elapsed_ms": g2.get("elapsed_ms"),
                "root_elapsed_ms": root.get("validation_elapsed_ms"),
                "outbound_request_count": len(root.get("outbound_requests", [])),
                "fresh_install_elapsed_ms": None,
                "pm_cache_install_elapsed_ms": None,
                "ctdp_install_elapsed_ms": g1.get("elapsed_ms"),
                "latency_comparison_status": "not_measured_for_phase1",
                "reason": root.get("reason") or g1.get("reason") or g2.get("reason"),
            })
    _csv(phase_dir / "install_validation.csv", validation_rows, [
        "profile_id", "owner", "repo", "dependency_root", "package_manager", "profile_status",
        "g1_status", "g1_elapsed_ms", "g2_status", "g2_elapsed_ms", "root_elapsed_ms",
        "outbound_request_count", "fresh_install_elapsed_ms", "pm_cache_install_elapsed_ms",
        "ctdp_install_elapsed_ms", "latency_comparison_status", "reason",
    ])

    failures = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "validation": validation.get("failures", []),
        "resolution": resolution.get("failures", []),
        "prefetch": prefetch.get("unhandled_failures", []) + [
            item for item in prefetch.get("artifacts", [])
            if item.get("status") == "failed"
            and item.get("artifact_id") not in {failure.get("artifact_id") for failure in prefetch.get("unhandled_failures", [])}
        ],
        "warm_cache": warm.get("failures", []),
        "manual_review": inventory.get("failures", []) + resolution.get("manual_review", []),
    }
    write_json(phase_dir / "failures.json", failures)
    write_json(phase_dir / "failure_classification.json", _failure_classification_report(previous_failures, validation))

    phase_status = "partial"
    if (
        summary.get("failed_discovery_count") == 0
        and summary.get("resolution_failure_count") == 0
        and summary.get("prefetch_failure_count") == 0
        and summary.get("dynamic_validation", {}).get("success") == summary.get("input_profile_count")
        and second_bytes == 0
    ):
        phase_status = "passed"
    summary_lines = [
        "# Phase 1 CTDP Dependency Preparation Validation",
        "",
        f"Status: **{phase_status}**",
        "",
        "## Coverage",
        "",
        f"- Profiles: {summary.get('discovered_profile_count')} / {summary.get('input_profile_count')}",
        f"- Dependency roots: {len(resolution.get('profiles', []))}",
        f"- SWE-smith tasks in source CSV: {task_count if task_count is not None else 'not available'}",
        f"- Package managers: `{json.dumps(summary.get('package_manager_distribution', {}), sort_keys=True)}`",
        "",
        "## Deduplication",
        "",
        f"- Dependency references: {summary.get('total_dependency_references')}",
        f"- Unique immutable artifacts: {summary.get('unique_immutable_artifacts')}",
        f"- Referenced bytes before dedup: {summary.get('dedup_bytes_before')}",
        f"- Unique CAS bytes after dedup: {summary.get('dedup_bytes_after')}",
        f"- Dedup ratio: {summary.get('dedup_ratio')}",
        "",
        "## Network and Cache",
        "",
        f"- First-run network bytes: {first_bytes}",
        f"- Second-run network bytes: {second_bytes}",
        f"- Second-run result: {prefetch.get('downloaded_count')} downloaded, {prefetch.get('reused_count')} reused, {prefetch.get('failed_count')} failed",
        f"- Warm-cache groups: {sum(item.get('status') == 'success' for item in warm.get('managers', []))} / {len(warm.get('managers', []))} successful",
        "",
        "## Real Install Validation",
        "",
        f"- Profile results: {summary.get('dynamic_validation')}",
        f"- Validation latency P50/P95/max (ms): {summary.get('stage_timing_ms', {}).get('validate', {}).get('p50')} / {summary.get('stage_timing_ms', {}).get('validate', {}).get('p95')} / {summary.get('stage_timing_ms', {}).get('validate', {}).get('max')}",
        "- Fresh and PM-cache comparison latency was not measured in Phase 1; the CSV records these fields as null.",
        "",
        "## Decision",
        "",
        "Phase 1 is **partial / not passed** under the strict plan criteria. CTDP completed the preparation pipeline across all 64 profiles and demonstrated cross-profile CAS deduplication, but resolution failures, one prefetch 404, partial Yarn classic warmup, install failures, and non-zero second-run network bytes remain.",
        "",
        "The results support continuing to Phase 2 only with these limitations recorded; they do not support claiming zero-near-zero second-run traffic or full real-install success.",
        "",
        f"Generated: {utc_now()}",
    ]
    write_text(phase_dir / "phase1_summary.md", "\n".join(summary_lines) + "\n")


def generate_reports(out: Path) -> dict[str, Any]:
    reports_dir = out / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    inventory = read_json(out / "inventory.json", {})
    resolution = read_json(out / "resolution.json", {})
    normalized = read_json(out / "normalized.json", {})
    aggregate = read_json(out / "global" / "global_manifest.json", {})
    dedup = read_json(reports_dir / "dedup.json", {})
    prefetch = read_json(out / "prefetch.json", {})
    warm = read_json(out / "warm-cache.json", {})
    validation = read_json(out / "validation.json", {})
    profiles = inventory.get("profiles", [])
    resolution_records = resolution.get("profiles", [])
    profile_manager_distribution = Counter(
        profile.get("package_manager")
        for profile in profiles
        if profile.get("package_manager")
    )
    root_manager_distribution = Counter(
        root.get("package_manager")
        for profile in profiles
        for root in profile.get("dependency_roots", [])
        if root.get("package_manager")
    )
    version_distribution = Counter(
        root.get("package_manager_version") or "unknown"
        for profile in profiles
        for root in profile.get("dependency_roots", [])
        if root.get("package_manager")
    )
    classifications = Counter(item.get("classification") for item in resolution_records)
    resolution_times = [item.get("resolve_elapsed_ms", 0) for item in resolution_records if isinstance(item.get("resolve_elapsed_ms"), (int, float))]
    artifacts = aggregate.get("artifacts", [])
    cas_size = _cas_blob_bytes(out, prefetch.get("artifacts", []))
    cas_directory_size = directory_size(out / "cas")
    warm_records = [item for item in warm.get("managers", []) if item.get("manager")]
    native_sizes: dict[str, int] = {}
    for item in warm_records:
        manager = str(item["manager"])
        native_sizes[manager] = native_sizes.get(manager, 0) + int(item.get("cache_bytes", 0) or 0)
    validation_by_profile = {item.get("profile_id"): item for item in validation.get("profiles", [])}
    discovery_failures = {item.get("profile_id"): item for item in inventory.get("failures", [])}
    normalization_by_root = {
        (item.get("profile_id"), item.get("dependency_root")): item
        for item in normalized.get("timings", [])
    }
    warmup_by_policy = {
        "@".join(
            part
            for part in (
                str(item.get("manager")),
                str(item.get("variant")) if item.get("variant") else None,
                str(item.get("version")) if item.get("version") else None,
            )
            if part
        ): item
        for item in warm_records
    }
    warmup_by_manager: dict[str, dict[str, Any]] = {}
    for item in warm_records:
        manager = str(item["manager"])
        manager_summary = warmup_by_manager.setdefault(
            manager,
            {
                "manager": manager,
                "status": "success",
                "cache_bytes": 0,
                "imported": 0,
                "failed_count": 0,
                "elapsed_ms": 0,
                "policies": [],
            },
        )
        manager_summary["status"] = "partial" if item.get("status") != "success" else manager_summary["status"]
        manager_summary["cache_bytes"] += int(item.get("cache_bytes", 0) or 0)
        manager_summary["imported"] += int(item.get("imported", 0) or 0)
        manager_summary["failed_count"] += len(item.get("failed", []))
        manager_summary["elapsed_ms"] += int(item.get("warmup_elapsed_ms", 0) or 0)
        manager_summary["policies"].append(
            {
                "variant": item.get("variant"),
                "version": item.get("version"),
                "status": item.get("status"),
                "cache_bytes": item.get("cache_bytes", 0),
                "imported": item.get("imported", 0),
                "failed_count": len(item.get("failed", [])),
            }
        )
    measured_before_dedup, measured_after_dedup, missing_dedup_bytes, dedup_bytes_complete = _dedup_byte_counts(
        out,
        artifacts,
        prefetch.get("artifacts", []),
    )
    dedup_report = dict(dedup)
    dedup_report["bytes_before_global_dedup"] = measured_before_dedup
    dedup_report["bytes_after_global_dedup"] = measured_after_dedup
    dedup_report["bytes_before_global_dedup_complete"] = dedup_bytes_complete
    dedup_report["bytes_after_global_dedup_complete"] = dedup_bytes_complete
    dedup_report["missing_artifact_byte_counts"] = missing_dedup_bytes
    dedup_report["bytes_are_measured"] = bool(prefetch.get("artifacts"))
    write_json(reports_dir / "dedup.json", dedup_report)
    projects_rows = []
    for profile in profiles:
        projects_rows.append({
            "profile_id": profile.get("profile_id"), "owner": profile.get("owner"), "repo": profile.get("repo"), "commit": profile.get("commit"),
            "language": profile.get("language"), "node_version": profile.get("node_version"), "package_manager": profile.get("package_manager"),
            "package_manager_version": profile.get("package_manager_version"), "dependency_roots": len(profile.get("dependency_roots", [])),
            "discovery_status": "failed" if profile.get("profile_id") in discovery_failures else "success", "discovery_elapsed_ms": profile.get("discovery_elapsed_ms"),
            "validation_status": validation_by_profile.get(profile.get("profile_id"), {}).get("status"),
            "validation_elapsed_ms": validation_by_profile.get(profile.get("profile_id"), {}).get("validation_elapsed_ms"),
        })
    resolution_rows = []
    for item in resolution_records:
        resolution_rows.append({
            "profile_id": item.get("profile_id"), "dependency_root": item.get("dependency_root"), "package_manager": item.get("package_manager"),
            "package_manager_version": item.get("package_manager_version"), "classification": item.get("classification"),
            "resolution_source": item.get("resolution_source"), "source_lockfile": item.get("source_lockfile"),
            "resolved_lockfile": item.get("resolved_lockfile"), "lockfile_changed": item.get("lockfile_changed"),
            "resolve_elapsed_ms": item.get("resolve_elapsed_ms"), "normalize_elapsed_ms": normalization_by_root.get((item.get("profile_id"), item.get("dependency_root")), {}).get("normalize_elapsed_ms"),
            "validation_elapsed_ms": next((root.get("validation_elapsed_ms") for profile in validation.get("profiles", []) if profile.get("profile_id") == item.get("profile_id") for root in profile.get("roots", []) if root.get("dependency_root") == item.get("dependency_root")), None),
            "exit_code": item.get("exit_code"),
        })
    artifact_rows = []
    for item in prefetch.get("artifacts", artifacts):
        artifact_rows.append({
            "artifact_id": item.get("artifact_id"), "type": item.get("type"), "name": item.get("name"), "version": item.get("version"),
            "source_url": item.get("source_url"), "integrity": item.get("integrity"), "status": item.get("status"),
            "size_bytes": item.get("size_bytes"), "downloaded_bytes": item.get("downloaded_bytes"), "cas_path": item.get("cas_path"),
            "reference_count": item.get("reference_count"), "referenced_by": json.dumps(item.get("referenced_by", []), ensure_ascii=False), "prefetch_elapsed_ms": item.get("prefetch_elapsed_ms"), "error": item.get("error"),
        })
    _csv(reports_dir / "projects.csv", projects_rows, ["profile_id", "owner", "repo", "commit", "language", "node_version", "package_manager", "package_manager_version", "dependency_roots", "discovery_status", "discovery_elapsed_ms", "validation_status", "validation_elapsed_ms"])
    _csv(reports_dir / "resolution.csv", resolution_rows, ["profile_id", "dependency_root", "package_manager", "package_manager_version", "classification", "resolution_source", "source_lockfile", "resolved_lockfile", "lockfile_changed", "resolve_elapsed_ms", "normalize_elapsed_ms", "validation_elapsed_ms", "exit_code"])
    _csv(reports_dir / "artifacts.csv", artifact_rows, ["artifact_id", "type", "name", "version", "source_url", "integrity", "status", "size_bytes", "downloaded_bytes", "cas_path", "reference_count", "referenced_by", "prefetch_elapsed_ms", "error"])
    write_json(reports_dir / "failures.json", {"failures": validation.get("failures", []) + resolution.get("failures", []) + prefetch.get("unhandled_failures", []) + warm.get("failures", []), "generated_at": utc_now()})
    write_json(reports_dir / "manual_review.json", {"items": inventory.get("failures", []) + resolution.get("manual_review", []) + normalized.get("manual_review", []) + aggregate.get("manual_review", []), "generated_at": utc_now()})
    summary = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "input_profile_count": inventory.get("input_profile_count", 0), "discovered_profile_count": len(profiles), "failed_discovery_count": len(inventory.get("failures", [])),
        "package_manager_distribution": dict(profile_manager_distribution),
        "package_manager_root_distribution": dict(root_manager_distribution),
        "package_manager_version_distribution": dict(version_distribution),
        "lockfile_classification_counts": dict(classifications),
        "resolution_success_count": sum(item.get("exit_code") == 0 for item in resolution_records), "resolution_failure_count": sum(item.get("exit_code") not in (0, None) for item in resolution_records),
        "resolution_time_ms": {"total": sum(resolution_times), "p50": percentile(resolution_times, .5), "p95": percentile(resolution_times, .95), "max": max(resolution_times) if resolution_times else None},
        "total_dependency_references": dedup_report.get("total_dependency_references", 0), "unique_logical_package_versions": dedup_report.get("unique_logical_package_versions", 0),
        "unique_immutable_artifacts": dedup_report.get("unique_immutable_artifacts", len(artifacts)), "total_raw_artifact_bytes": cas_size, "cas_directory_bytes": cas_directory_size,
        "dedup_ratio": dedup_report.get("dedup_ratio"), "dedup_bytes_before": dedup_report.get("bytes_before_global_dedup"), "dedup_bytes_after": dedup_report.get("bytes_after_global_dedup"), "dedup_bytes_are_measured": dedup_report.get("bytes_are_measured", False),
        "prefetch_success_count": prefetch.get("downloaded_count", 0) + prefetch.get("reused_count", 0),
        "prefetch_failure_count": prefetch.get("failed_count", 0), "cas_integrity_failure_count": len(prefetch.get("integrity_failures", [])),
        "native_cache_warmup": warm.get("managers", []), "native_cache_bytes_by_pm": native_sizes,
        "native_cache_warmup_by_pm": {
            manager: item for manager, item in warmup_by_manager.items()
        },
        "native_cache_warmup_by_policy": {
            policy: {
                "manager": item.get("manager"),
                "variant": item.get("variant"),
                "version": item.get("version"),
                "status": item.get("status"),
                "cache_bytes": item.get("cache_bytes", 0),
                "imported": item.get("imported", 0),
                "failed_count": len(item.get("failed", [])),
                "elapsed_ms": item.get("warmup_elapsed_ms"),
            }
            for policy, item in warmup_by_policy.items()
        },
        "dynamic_validation": {"success": validation.get("success_count", 0), "external_artifact_miss": validation.get("external_artifact_miss_count", 0), "native_or_system_dependency_failure": validation.get("native_or_system_dependency_failure_count", 0), "other_failure": validation.get("other_failure_count", 0)},
        "profiles_with_unexpected_external_downloads": [item.get("profile_id") for item in validation.get("profiles", []) if item.get("status") == "external_artifact_miss"],
        "first_run_internet_bytes": prefetch.get("initial_run_downloaded_bytes") if prefetch.get("initial_run_downloaded_bytes") is not None else (prefetch.get("previous_run_downloaded_bytes") if prefetch.get("previous_run_downloaded_bytes") not in (None, 0) else prefetch.get("downloaded_bytes", 0)),
        "second_run_internet_bytes": prefetch.get("second_run_downloaded_bytes"),
        "stage_timing_ms": {
            "discover": _timing_summary([item.get("discovery_elapsed_ms", 0) for item in profiles if isinstance(item.get("discovery_elapsed_ms"), (int, float))]),
            "resolve": _timing_summary(resolution_times),
            "normalize": _timing_summary([item.get("normalize_elapsed_ms", 0) for item in normalized.get("timings", []) if isinstance(item.get("normalize_elapsed_ms"), (int, float))]),
            "prefetch": _timing_summary([item.get("prefetch_elapsed_ms", 0) for item in prefetch.get("artifacts", []) if isinstance(item.get("prefetch_elapsed_ms"), (int, float))]),
            "warm-cache": _timing_summary([item.get("warmup_elapsed_ms", 0) for item in warm.get("managers", []) if isinstance(item.get("warmup_elapsed_ms"), (int, float))]),
            "validate": _timing_summary([item.get("validation_elapsed_ms", 0) for item in validation.get("profiles", []) if isinstance(item.get("validation_elapsed_ms"), (int, float))]),
        },
    }
    write_json(reports_dir / "summary.json", summary)
    lines = [
        "# SWE-smith Dependency Preparation Summary", "", f"Generated: {summary['generated_at']}", "",
        f"- Input profile count: {summary['input_profile_count']}", f"- Discovered profile count: {summary['discovered_profile_count']}", f"- Failed discovery count: {summary['failed_discovery_count']}",
        "", "## Package managers", "", f"- Profile distribution (64 profiles): `{json.dumps(summary['package_manager_distribution'], ensure_ascii=False, sort_keys=True)}`", f"- Dependency-root distribution (65 roots): `{json.dumps(summary['package_manager_root_distribution'], ensure_ascii=False, sort_keys=True)}`", f"- Versions (dependency roots): `{json.dumps(summary['package_manager_version_distribution'], ensure_ascii=False, sort_keys=True)}`",
        "", "## Lockfile classification", "", f"- Counts: `{json.dumps(summary['lockfile_classification_counts'], ensure_ascii=False, sort_keys=True)}`", f"- Authoritative / re-resolution / missing / manual-review: {summary['lockfile_classification_counts'].get('authoritative_existing', 0)} / {summary['lockfile_classification_counts'].get('existing_requires_resolution', 0)} / {summary['lockfile_classification_counts'].get('missing_requires_resolution', 0)} / {summary['lockfile_classification_counts'].get('unsupported_or_manual_review', 0)}", "",
        f"- Resolution success/failure: {summary['resolution_success_count']}/{summary['resolution_failure_count']}", f"- Resolution time total/P50/P95/max (ms): {summary['resolution_time_ms']['total']}/{summary['resolution_time_ms']['p50']}/{summary['resolution_time_ms']['p95']}/{summary['resolution_time_ms']['max']}",
        "", "## Artifacts", "", f"- Dependency references: {summary['total_dependency_references']}", f"- Unique logical package versions: {summary['unique_logical_package_versions']}", f"- Unique immutable artifacts: {summary['unique_immutable_artifacts']}", f"- CAS artifact bytes: {summary['total_raw_artifact_bytes']}", f"- CAS directory bytes (including metadata): {summary['cas_directory_bytes']}", f"- Dedup bytes before/after: {summary['dedup_bytes_before']}/{summary['dedup_bytes_after']} (measured after: {summary['dedup_bytes_are_measured']})", f"- Dedup ratio: {summary['dedup_ratio']}", f"- Prefetch success/failure: {summary['prefetch_success_count']}/{summary['prefetch_failure_count']}", f"- CAS integrity failures: {summary['cas_integrity_failure_count']}",
        "", "## Native cache warmup", "", f"- Cache bytes by PM: `{json.dumps(summary['native_cache_bytes_by_pm'], ensure_ascii=False, sort_keys=True)}`", f"- Success/failure/status by PM: `{json.dumps(summary['native_cache_warmup_by_pm'], ensure_ascii=False, sort_keys=True)}`", "",
        "## Performance", "", f"- Stage timing summaries (ms): `{json.dumps(summary['stage_timing_ms'], ensure_ascii=False, sort_keys=True)}`", "",
        "## Dynamic validation", "", f"- Results: `{json.dumps(summary['dynamic_validation'], ensure_ascii=False, sort_keys=True)}`", f"- Profiles with external artifact misses: `{json.dumps(summary['profiles_with_unexpected_external_downloads'], ensure_ascii=False)}`", f"- First-run Internet bytes: {summary['first_run_internet_bytes']}", f"- Second-run Internet bytes: {summary['second_run_internet_bytes']}",
    ]
    write_text(reports_dir / "summary.md", "\n".join(lines) + "\n")
    _generate_phase1_reports(out, inventory, resolution, aggregate, prefetch, warm, validation, summary)
    return summary
