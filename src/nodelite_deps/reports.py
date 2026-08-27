from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .util import directory_size, percentile, read_json, utc_now, write_json, write_text


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
        aggregate = warmup_by_manager.setdefault(
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
        aggregate["status"] = "partial" if item.get("status") != "success" else aggregate["status"]
        aggregate["cache_bytes"] += int(item.get("cache_bytes", 0) or 0)
        aggregate["imported"] += int(item.get("imported", 0) or 0)
        aggregate["failed_count"] += len(item.get("failed", []))
        aggregate["elapsed_ms"] += int(item.get("warmup_elapsed_ms", 0) or 0)
        aggregate["policies"].append(
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
    return summary
